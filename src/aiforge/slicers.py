r"""切片软件检测与「送到切片软件」。

对标差距（docs/COMPETITIVE-GAPS.md 的 G2 / S4）：Meshy 这类产品能把模型一键送进
Bambu Studio、OrcaSlicer、Cura 等切片软件，而我们以前只到「下载」为止 ——
用户还得自己打开切片器、自己找到那个文件。

为什么不能简单地 `os.startfile("x.3mf")` 交给系统，也不能只靠文件关联（本机实测）：

1. `.3mf` 的 UserChoice 指向一个 UWP 应用（`AppX…`），双击打开的**不是** Bambu Studio；
2. `.3mf` 的 ProgID 默认值实测**带一个前导空格**（`' Bambu.Studio.1'`），不 strip 连
   查询键名都拼错；更糟的是 strip 之后 `Bambu.Studio.1` 这个键在本机**根本不存在**，
   `shell\open\command` 全为空 —— 也就是说「顺着文件关联找切片器」在本机是条死路；
3. Bambu Studio 装在非系统盘的 `Bambu Studio\bambu-studio.exe`，注册表卸载项里
   **没有** `InstallLocation`，只能从 `DisplayIcon` 拿到真实路径（实测只有卸载项这条
   路找到了它，并顺带拿到版本 `02.08.02.61`）；
4. OrcaSlicer 同一台机器上**根本没有卸载项**，只能靠"扫各固定磁盘根目录下的同名目录"
   才发现（它还带过 `_OrcaSlicer_Trash` 这种干扰目录）。

所以检测按四种来源各扫一遍再合并：文件关联 → 卸载项 → App Paths → 常见安装目录，
外加用户在设置里手动指定的 exe。拿不到就老实说"没找到"，并给出手动指定的入口。

（本机实测结果：Bambu Studio 来自「已安装程序」、OrcaSlicer 来自「常见安装目录」，
文件关联一条为空 —— 四种来源不是理论上的冗余，是少一个就少一个软件。）
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import procguard

log = logging.getLogger(__name__)

if os.name == "nt":  # pragma: no cover - 平台分支
    import winreg
else:  # pragma: no cover - 非 Windows 上只跑纯函数部分
    winreg = None  # type: ignore[assignment]

#: 可以交给切片软件的文件类型（3MF 首选：带毫米单位与机型元数据）
MODEL_KINDS: tuple[str, ...] = ("3mf", "stl")

#: 交给切片软件前在项目的 slicer\ 目录里留几份快照（超过就删最旧的）
STAGE_KEEP = 5


@dataclass(frozen=True)
class SlicerKind:
    """一种"认识"的切片软件。"""

    id: str
    name: str
    exes: tuple[str, ...]  # 可执行文件名（小写）
    dir_globs: tuple[str, ...] = ()  # 在安装目录根下匹配文件夹（小写、支持 *）
    #: 是否在本机真发过一次（自检据此选验证对象；不拿未实测的当承诺）
    verified: bool = False


#: 内置识别列表。exe 名与各软件官方安装包的落盘名一致（不认识的程序走"文件关联"那一条）。
#: 这张表只影响"能自动发现谁"，不增加任何界面入口，所以宁可宽一点；
#: 但界面文案只报前几个常见名字，不给用户背一长串清单。
SLICER_KINDS: tuple[SlicerKind, ...] = (
    SlicerKind(
        "bambu_studio",
        "Bambu Studio",
        ("bambu-studio.exe", "bambustudio.exe"),
        ("bambu studio", "bambu lab/bambu studio"),
        verified=True,
    ),
    SlicerKind(
        "orcaslicer",
        "OrcaSlicer",
        ("orca-slicer.exe", "orcaslicer.exe"),
        ("orcaslicer", "softfever/orcaslicer"),
        verified=True,
    ),
    SlicerKind(
        "prusa_slicer",
        "PrusaSlicer",
        ("prusa-slicer.exe",),
        ("prusaslicer", "prusa3d/prusaslicer"),
    ),
    SlicerKind(
        "cura",
        "UltiMaker Cura",
        ("cura.exe", "ultimaker cura.exe"),
        ("ultimaker cura*", "ultimaker/cura"),
    ),
    SlicerKind("superslicer", "SuperSlicer", ("super-slicer.exe", "superslicer.exe"), ("superslicer",)),
    SlicerKind("creality_print", "Creality Print", ("creality print.exe", "crealityprint.exe"),
               ("creality print*", "creality*/creality print*")),
    SlicerKind("elegoo_slicer", "ElegooSlicer", ("elegoo-slicer.exe", "elegoo slicer.exe", "egoo3d.exe"),
               ("elegoo*",)),
    SlicerKind("anycubic_slicer", "Anycubic Slicer", ("anycubic-slicer.exe", "anycubic slicer next.exe"),
               ("anycubic*")),
    SlicerKind("chitubox", "CHITUBOX", ("chitubox.exe", "chitubox pro.exe"), ("chitubox", "chitu*")),
    SlicerKind("lychee_slicer", "Lychee Slicer", ("lycheeslicer.exe",), ("lychee-slicer", "lychee slicer")),
)

_KIND_BY_EXE: dict[str, SlicerKind] = {
    exe: kind for kind in SLICER_KINDS for exe in kind.exes
}

#: 这些"可执行文件"只是壳，拿着启动等于什么也没打开（甚至弹错误框）
_LAUNCHER_NAMES = {
    "rundll32.exe",
    "dllhost.exe",
    "explorer.exe",
    "cmd.exe",
    "powershell.exe",
    "wscript.exe",
    "cscript.exe",
    "msiexec.exe",
    "openwith.exe",
    "openwithpreview.exe",
    "shellhost.exe",
}

#: 检测到这些来源时的展示优先级（同一软件被多种方式发现时，保留先命中的说明）
_SOURCE_ORDER = ("association", "uninstall", "app_paths", "known_dir", "custom")
_SOURCE_LABELS = {
    "association": "文件关联",
    "uninstall": "已安装程序",
    "app_paths": "系统注册",
    "known_dir": "常见安装目录",
    "custom": "手动指定",
}


@dataclass
class Slicer:
    """一个检测到的切片软件（可执行文件已经确认在磁盘上存在）。"""

    id: str
    name: str
    exe: Path
    source: str
    version: str | None = None
    #: 是否是内置识别表里的（否则只可能来自文件关联/手动指定）
    builtin: bool = True
    running: bool = False

    @property
    def source_label(self) -> str:
        return _SOURCE_LABELS.get(self.source, self.source)

    def to_dict(self) -> dict[str, object]:
        # 只给界面用得上的：不做"内置/实测/官网/支持类型"那一堆字段，
        # 用户只需要知道哪个软件、什么版本、从哪找到的、开没开。
        return {
            "id": self.id,
            "name": self.name,
            "exe": str(self.exe),
            "version": self.version,
            "source_label": self.source_label,
            "running": self.running,
        }


# ---------------------------------------------------------------------------
# 纯函数：注册表字符串的解析（自检直接喂实测过的原文）
# ---------------------------------------------------------------------------
def classify_exe(exe: Path) -> SlicerKind | None:
    return _KIND_BY_EXE.get(exe.name.casefold())


def parse_display_icon(raw: str) -> str:
    """`DisplayIcon` 常见写法是 `C:\\...\\app.exe,0`（带图标序号），去掉尾巴。"""
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith('"'):
        end = text.find('"', 1)
        if end > 0:
            return text[1:end]
        return text.strip('"')
    # 只在"逗号后面是纯数字"时当图标序号处理，免得把带逗号的目录名切错
    head, sep, tail = text.rpartition(",")
    if sep and tail.isdigit():
        return head
    return text


def parse_open_command(raw: str, *, require_exists: bool = True) -> Path | None:
    """把注册表里的 `shell\\open\\command` 解析成可执行文件路径。

    要处理的真实写法（都是实测或官方安装包里的形态）::

        "D:\\...\\bambu-studio.exe" "%1"
        "C:\\...\\orca-slicer.exe" --load "%1"
        C:\\Program Files\\...\\app.exe "%1"      ← 路径含空格又没引号，只能一段段试
        "C:\\...\\app.exe" -p "%~1" / %V / %L

    一律拒绝：只有占位符的、指向 `rundll32`/`dllhost` 这类壳的、以及
    `WindowsApps`（UWP 应用不能这样直接启动，本机 `.3mf` 的 UserChoice 正是这种）。

    `require_exists=False`（单测里不碰磁盘）时，没引号的写法只有"首词就以 .exe 结尾"
    才敢认：`D:\\Bambu Studio\\bambu-studio.exe %1` 去掉引号后，在不能查磁盘的前提下
    没有任何办法判断路径在第几个空格处结束 —— 硬猜会给出 `D:\\Bambu` 这种假路径，
    与其返回一个错的，不如返回 None 让别的来源接手。
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith('"'):
        end = text.find('"', 1)
        if end <= 0:
            return None
        candidate = text[1:end]
    else:
        parts = text.split()
        candidate = parts[0]
        if not candidate.casefold().endswith(".exe") and (require_exists or len(parts) > 1):
            # 首词不是完整 exe 路径：要么靠磁盘把它拼全，要么直接放弃
            if not require_exists:
                return None
            for i in range(2, min(len(parts), 8) + 1):
                candidate = " ".join(parts[:i])
                if _is_file(candidate):
                    break
            else:
                return None
    candidate = os.path.expandvars(candidate).strip().strip('"')
    if not candidate or "%" in candidate:
        return None
    path = Path(candidate)
    if not path.is_absolute():
        return None
    lowered = str(path).casefold()
    if path.name.casefold() in _LAUNCHER_NAMES:
        return None
    if "windowsapps" in lowered or "protectedprogram files" in lowered:
        return None
    if require_exists and not _is_file(path):
        return None
    return path


def _is_file(candidate) -> bool:
    try:
        return Path(candidate).is_file()
    except OSError:
        return False


def match_known_dir(root: Path, pattern: str) -> Path | None:
    """在某个安装目录根下按小写通配符找子目录（可以带一层父目录，如 `bambu lab/bambu studio`）。

    NTFS 本身不区分大小写，所以不带 `*` 的段直接拼路径试；带 `*` 的段才去列目录。
    """
    parts = pattern.split("/")
    current = root
    for part in parts:
        if "*" not in part:
            current = current / part
            if not current.is_dir():
                return None
            continue
        if not current.is_dir():
            return None
        try:
            children = sorted(current.iterdir())
        except OSError:
            return None
        for child in children:
            if child.is_dir() and _glob_casefold(child.name, part):
                current = child
                break
        else:
            return None
    return current if current.is_dir() else None


def _glob_casefold(name: str, pattern: str) -> bool:
    import fnmatch

    return fnmatch.fnmatchcase(name.casefold(), pattern.casefold())


def exe_in_dir(directory: Path, kind: SlicerKind, depth: int = 2) -> Path | None:
    """在软件安装目录里找它的 exe（先看本级，再往下看一层：Cura 会再套一层版本号）。"""
    names = set(kind.exes)
    try:
        for entry in sorted(directory.iterdir()):
            if entry.is_file() and entry.name.casefold() in names:
                return entry
    except OSError:
        return None
    if depth <= 1:
        return None
    try:
        for sub in sorted(directory.iterdir()):
            if not sub.is_dir():
                continue
            for entry in sorted(sub.iterdir()):
                if entry.is_file() and entry.name.casefold() in names:
                    return entry
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# 各来源的扫描（只有 Windows 上有注册表可读）
# ---------------------------------------------------------------------------
def _reg_value(root, sub_key: str, value: str | None = None, wow64=None) -> str | None:
    if winreg is None:  # pragma: no cover
        return None
    try:
        access = winreg.KEY_READ | (wow64 or 0)
        with winreg.OpenKey(root, sub_key, 0, access) as key:
            if value is None:
                return winreg.QueryValue(key, None)
            found = winreg.QueryValueEx(key, value)[0]
            return found if isinstance(found, str) else str(found)
    except OSError:
        return None


def from_association(extension: str = ".3mf") -> Path | None:
    """顺着文件关联找到真正处理这种扩展名的桌面程序。"""
    if winreg is None:  # pragma: no cover
        return None
    prog = _reg_value(winreg.HKEY_CLASSES_ROOT, extension) or _reg_value(
        winreg.HKEY_CURRENT_USER, f"Software\\Classes\\{extension}"
    )
    if not prog:
        return None
    prog = str(prog).strip()  # 实测：Bambu Studio 写进来的值前面多一个空格
    if not prog:
        return None
    for site, prefix in (
        (winreg.HKEY_CLASSES_ROOT, ""),
        (winreg.HKEY_CURRENT_USER, "Software\\Classes\\"),
    ):
        for verb in ("shell\\open\\command", "command"):
            raw = _reg_value(site, f"{prefix}{prog}\\{verb}")
            exe = parse_open_command(raw or "")
            if exe:
                return exe
    return None


def from_uninstall() -> list[tuple[Path, str | None]]:
    """从「程序和功能」的注册表项里找切片软件。

    两个坑都实测过：`InstallLocation` 可能根本没有（只能靠 `DisplayIcon`）；
    32 位安装包写在 WOW6432Node 下，必须两个视图都扫。
    """
    if winreg is None:  # pragma: no cover
        return []
    found: list[tuple[Path, str | None]] = []
    keywords = tuple(kind.name.casefold() for kind in SLICER_KINDS) + ("slic", "3mf")
    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in views:
            base = "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall"
            try:
                with winreg.OpenKey(hive, base, 0, winreg.KEY_READ | view) as key:
                    count, _, _ = winreg.QueryInfoKey(key)
                    sub_keys = [winreg.EnumKey(key, i) for i in range(count)]
            except OSError:
                continue
            for sub in sub_keys:
                name = _reg_value(hive, f"{base}\\{sub}", "DisplayName", view)
                if not name or not any(word in name.casefold() for word in keywords):
                    continue
                version = _reg_value(hive, f"{base}\\{sub}", "DisplayVersion", view)
                icon = _reg_value(hive, f"{base}\\{sub}", "DisplayIcon", view)
                location = _reg_value(hive, f"{base}\\{sub}", "InstallLocation", view)
                for raw in (icon, location):
                    if not raw:
                        continue
                    exe = parse_open_command(f'"{parse_display_icon(raw)}"')
                    if exe and classify_exe(exe):
                        found.append((exe, version))
                        break
                else:
                    if location:
                        dir_path = Path(location)
                        for kind in SLICER_KINDS:
                            hit = exe_in_dir(dir_path, kind)
                            if hit:
                                found.append((hit, version))
                                break
    return found


def from_app_paths() -> list[Path]:
    if winreg is None:  # pragma: no cover
        return []
    base = "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths"
    found: list[Path] = []
    for kind in SLICER_KINDS:
        for exe_name in kind.exes:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                raw = _reg_value(hive, f"{base}\\{exe_name}") or _reg_value(
                    hive, f"{base}\\{exe_name}", "Path"
                )
                exe = parse_open_command(f'"{parse_display_icon(raw or "")}"')
                if exe:
                    found.append(exe)
    return found


def install_roots() -> list[Path]:
    """常见安装位置 + 每个固定磁盘的根目录。

    磁盘根目录必须扫：本机实测 OrcaSlicer 与 Bambu Studio 都直接装在非系统盘根下。
    """
    roots: list[Path] = []
    env_keys = (
        "ProgramFiles",
        "ProgramFiles(x86)",
        "ProgramW6432",
    )
    for key in env_keys:
        value = os.environ.get(key)
        if value:
            roots.append(Path(value))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "Programs")
    for drive in _fixed_drive_roots():
        roots.append(drive)
    uniq: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        tag = str(root).casefold().rstrip("\\/")
        if tag in seen:
            continue
        seen.add(tag)
        uniq.append(root)
    return uniq


def _fixed_drive_roots() -> list[Path]:
    if os.name != "nt":  # pragma: no cover
        return []
    try:
        mask = _logical_drives_mask()
    except Exception:  # noqa: BLE001 - 拿不到磁盘就不扫磁盘根
        return []
    out: list[Path] = []
    for index in range(26):
        if not (mask >> index) & 1:
            continue
        letter = chr(ord("A") + index)
        path = Path(f"{letter}:\\")
        try:
            if path.exists():
                out.append(path)
        except OSError:
            continue
    return out


def _logical_drives_mask() -> int:
    import ctypes

    return int(ctypes.windll.kernel32.GetLogicalDrives())  # type: ignore[attr-defined]


def from_known_dirs(roots: list[Path] | None = None) -> list[Path]:
    roots = roots if roots is not None else install_roots()
    found: list[Path] = []
    for kind in SLICER_KINDS:
        for root in roots:
            if not root.is_dir():
                continue
            for pattern in kind.dir_globs:
                directory = match_known_dir(root, pattern)
                if directory is None:
                    continue
                hit = exe_in_dir(directory, kind)
                if hit:
                    found.append(hit)
    return found


def custom_paths() -> list[Path]:
    """用户在设置里手动指定的 exe（自动检测总会有漏网的机器）。"""
    from .config import store

    out: list[Path] = []
    for raw in store().config.slicers.custom_paths:
        try:
            path = Path(str(raw)).expanduser()
        except OSError:
            continue
        if path.is_file() and path.suffix.casefold() == ".exe":
            out.append(path)
    return out


# ---------------------------------------------------------------------------
# 合并结果
# ---------------------------------------------------------------------------
_cache: list[Slicer] | None = None


def _candidate_pairs() -> list[tuple[str, Path, str | None]]:
    pairs: list[tuple[str, Path, str | None]] = []
    assoc = from_association(".3mf") or from_association(".stl")
    if assoc:
        pairs.append(("association", assoc, None))
    for exe, version in from_uninstall():
        pairs.append(("uninstall", exe, version))
    for exe in from_app_paths():
        pairs.append(("app_paths", exe, None))
    for exe in from_known_dirs():
        pairs.append(("known_dir", exe, None))
    for exe in custom_paths():
        pairs.append(("custom", exe, None))
    return pairs


def scan(force: bool = False) -> list[Slicer]:
    """扫描（带缓存）并返回去重后的切片软件列表，按内置顺序 + 名字排序。"""
    global _cache
    if _cache is not None and not force:
        return _cache
    collected: dict[str, Slicer] = {}
    for source, exe, version in _candidate_pairs():
        kind = classify_exe(exe)
        if kind is not None:
            sid = kind.id
            item = collected.get(sid)
            if item is None:
                collected[sid] = Slicer(
                    id=sid,
                    name=kind.name,
                    exe=exe,
                    source=source,
                    version=version,
                )
            elif version and not collected[sid].version:
                collected[sid].version = version
            continue
        # 不认识的程序只有"确实是切片器"才有意义：只接受文件关联与手动指定这两种来源，
        # 卸载项/App Paths/目录扫出来的陌生 exe 一律不猜（会冒出一堆无关程序）。
        if source not in ("association", "custom"):
            continue
        sid = f"ext:{exe.stem.casefold()}"
        if sid in collected:
            continue
        collected[sid] = Slicer(
            id=sid,
            name=exe.stem,
            exe=exe,
            source=source,
            builtin=False,
            version=version,
        )
    order = {kind.id: index for index, kind in enumerate(SLICER_KINDS)}
    items = sorted(
        collected.values(),
        key=lambda s: (order.get(s.id, len(order)), s.name.casefold(), _SOURCE_ORDER.index(s.source)),
    )
    names = procguard.list_running_process_names()
    for item in items:
        item.running = item.exe.name.casefold() in names
    _cache = items
    if items:
        log.info("检测到 %d 个切片软件：%s", len(items), "、".join(i.name for i in items))
    else:
        log.info("没有检测到任何切片软件")
    return items


def invalidate_cache() -> None:
    global _cache
    _cache = None


def describe() -> dict[str, object]:
    """给界面的形状（含"默认用哪个"）。"""
    from .config import store

    items = scan()
    default_id = store().config.slicers.default_id
    if default_id and all(item.id != default_id for item in items):
        default_id = None  # 记过的软件被卸载了：回到"没有默认"，而不是发一个无效 id
    return {
        "slicers": [item.to_dict() for item in items],
        "default_id": default_id,
        "count": len(items),
        # 我们认得哪些切片软件：没检测到时界面要能告诉用户"装这些里的任意一个就行"。
        # 只给名字，不给下载地址 —— 本软件不管用户的电脑该装什么。
        "recognized_names": [kind.name for kind in SLICER_KINDS],
    }


def resolve(slicer_id: str | None) -> Slicer:
    """把界面给的 id 变成检测到的条目；不认识就报错，绝不照着请求里的路径启动。"""
    items = scan()
    if not items:
        raise LookupError(
            "没有检测到切片软件。请先安装 Bambu Studio / OrcaSlicer / PrusaSlicer / Cura "
            "其中之一，或在「设置 → 切片软件」里手动指定它的主程序。"
        )
    if slicer_id:
        for item in items:
            if item.id == slicer_id:
                return item
        raise LookupError(f"不认识这个切片软件：{slicer_id}（可能已被卸载，请重新检测）")
    from .config import store

    default_id = store().config.slicers.default_id
    if default_id:
        for item in items:
            if item.id == default_id:
                return item
    return items[0]


# ---------------------------------------------------------------------------
# 交给切片软件的文件：快照，不交出 model.3mf 本体
# ---------------------------------------------------------------------------
def stage_file(project, kind: str = "3mf") -> Path:
    """把要送出的文件复制成一份**快照**，返回快照路径。

    为什么不直接把 `model.3mf` 给它：切片软件可能一直开着这个文件，而我们下次生成会
    原地重写 `model.stl` / `model.3mf` —— 轻则写入失败，重则切片软件读到半截网格。
    快照按版本号命名（`<名字>-r07.3mf`），只保留最近 STAGE_KEEP 份。

    为什么“最近”得由我们自己标时间：`shutil.copy2` 会连**源文件的旧 mtime** 一起复制，
    于是所有快照的时间一模一样，“按 mtime 留最新几份”就退化成“按目录顺序留前几份” ——
    实测后果是**刚送出去那一份会被当场删掉**（送到第 6 次之后开始咬人），
    切片软件再去读它就是“文件不存在”。现在既把 mtime 标成我们写出它的时刻，
    也在清理时给这一份上一个保险（时钟往回跳也不删它）。
    """
    kind = kind if kind in MODEL_KINDS else "3mf"
    source = project.three_mf_path if kind == "3mf" else project.stl_path
    if not source.is_file():
        raise FileNotFoundError(f"缺少要送出的文件：{source.name}")
    from .projects import safe_name

    destination = project.dir / "slicer"
    destination.mkdir(parents=True, exist_ok=True)
    stem = safe_name(project.name, "model")
    target = destination / f"{stem}-r{int(project.revision):02d}.{kind}"
    shutil.copy2(source, target)
    os.utime(target, None)  # 覆盖掉从源文件带过来的旧时间：这份就是“刚刚送出去的”
    _prune(destination, kind, keep=target)
    return target


def _prune(directory: Path, kind: str, keep: Path | None = None) -> None:
    try:
        files = [
            entry
            for entry in directory.iterdir()
            if entry.is_file() and entry.suffix.casefold() == f".{kind}"
        ]
    except OSError:
        return
    # 按“我们写出它的时刻”从新到旧排；同刻度时拿文件名兜底（名字里的版本号是零填充的）
    stale = sorted(files, key=lambda p: (_mtime(p), p.name), reverse=True)[STAGE_KEEP:]
    keep_name = keep.name if keep is not None else ""
    for entry in stale:
        if keep_name and entry.name == keep_name:
            continue  # 同一个目录里名字相同就是同一个文件；刚交给切片软件那一份无论如何不能删
        try:
            entry.unlink()
        except OSError:
            # 正在被切片软件占用的删不掉，忽略即可（不谎报成功）
            log.debug("清理旧的送出副本失败（可能被切片软件占用）：%s", entry)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------
#: 单实例软件（Bambu Studio / OrcaSlicer）把文件转交给已开着的窗口后，
#: 自己会立刻退出且返回 0。给它多长的观察窗口来判断"是转发还是失败"。
_WATCH_SECONDS = 2.0


def launch(slicer: Slicer, model_file: Path) -> dict[str, object]:
    """带文件参数启动切片软件，并如实报告看到了什么。

    判断依据（都是可观测的，不靠"看起来成功了"）：
      - 进程还活着 → started；再回查它的镜像路径是不是我们要启动的那个 exe；
      - 进程已退出且返回 0 → forwarded（单实例转发给了已经开着的窗口，无法再确认导入）；
      - 进程已退出且返回非 0 → failed，带上退出码。
    """
    if not slicer.exe.is_file():
        raise FileNotFoundError(f"切片软件主程序不存在：{slicer.exe}")
    argv = [str(slicer.exe), str(model_file)]
    started = time.time()
    proc, detached = procguard.spawn_detached(argv)
    deadline = started + _WATCH_SECONDS
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.1)
    code = proc.poll()
    if code is None:
        image = procguard.image_path(proc.pid)
        confirmed = bool(image) and Path(image).name.casefold() == slicer.exe.name.casefold()
        status = "started"
        detail = f"已启动 {slicer.name}（进程 {proc.pid}）" if confirmed else f"已启动 {slicer.name}"
        if not confirmed:
            detail += "（没能回查它的进程路径，若没自动导入请把文件拖进去）"
    elif code == 0:
        status = "forwarded"
        detail = f"{slicer.name} 已经在运行，文件已交给它的现有窗口"
    else:
        status = "failed"
        detail = f"{slicer.name} 启动后立即退出（退出码 {code}）"
    return {
        "status": status,
        "detail": detail,
        "pid": proc.pid,
        "exit_code": code,
        "detached": detached,
        "slicer": slicer.id,
        "slicer_name": slicer.name,
        "file": str(model_file),
        "exe": str(slicer.exe),
    }


def send(project, slicer_id: str | None = None, kind: str = "3mf") -> dict[str, object]:
    """检测 → 备文件 → 启动，返回可直接给界面的结果。"""
    slicer = resolve(slicer_id)
    model_file = stage_file(project, kind)
    result = launch(slicer, model_file)
    result["kind"] = kind if kind in MODEL_KINDS else "3mf"
    result["source_label"] = slicer.source_label
    if result["status"] == "started" and not result.get("detached"):
        # 这个提醒只有在“真的确认它还在我们那个一死就杀光的作业里、或者根本问不出来”
        # 时才会出现。宁可多说一句，也不拿“没报错”当“已送达”。
        result["note"] = "它和本软件的进程被关在同一组里：退出本软件可能连带关掉它。"
    return result
