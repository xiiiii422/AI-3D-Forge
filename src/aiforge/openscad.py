"""AI 3D Forge —— OpenSCAD 检测、定位、验证与自动安装。

对应需求第三十四、四十三、四十四节：
  * 不假设安装位置（绝不只是查 C:\\Program Files\\OpenSCAD）。
  * 自动搜索常见位置 + 注册表，并允许用户手动指定。
  * 自动安装只用官方渠道（files.openscad.org），下载后做 SHA-256 校验。
  * 安装后必须再次检测、真实执行 CLI、写入配置、给出明确结论。
  * 失败不静默：区分「需要管理员权限」「网络失败」「文件校验失败」并给出官方手动安装入口。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from .config import store
from .paths import APP_SLUG, paths
from .procguard import Popen as guarded_popen, run as guarded_run

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 官方发布信息（第三十四、四十三节：只用官方渠道 + 完整性校验）
# --------------------------------------------------------------------------
OFFICIAL_SITE = "https://openscad.org/downloads.html"
OFFICIAL_MIRROR = "https://files.openscad.org"

# 官方发布文件的 SHA-256（2025 年实测自官方源 files.openscad.org，见 docs/OPENSCAD-SOURCES.md）
# 任一字节不符即拒绝安装 —— 这是「不把未经验证的二进制装进用户电脑」的技术保证。
PINNED_SHA256: dict[str, str] = {
    "OpenSCAD-2021.01-x86-64.zip": "fb0caabf5bbc89f8f2f80c10b79ae64d697aaff6efd58b2756f5d6270edb7ba7",
    "OpenSCAD-2021.01-x86-64-Installer.exe": "fbe1e590e1af2af863f5d92bd263eb4644d0ee29352e3ab566366e87a8f6ed12",
}

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

VERSION_RE = re.compile(r"OpenSCAD version\s+([0-9][0-9A-Za-z.\-]*)")
EXE_NAMES = ("openscad.exe", "openscad.com", "openscad")

# 允许自动修复 / 需要重新下载时的官方文件名
ASSET_ZIP = "OpenSCAD-2021.01-x86-64.zip"
ASSET_INSTALLER = "OpenSCAD-2021.01-x86-64-Installer.exe"


class OpenScadError(RuntimeError):
    """带用户可读信息与修复建议的 OpenSCAD 错误。"""

    def __init__(self, message: str, *, hint: str | None = None, code: str = "OPENSCAD_FAILED") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class Candidate:
    path: Path
    version: str | None = None
    source: str = "common"

    def to_dict(self) -> dict[str, object]:
        return {"path": str(self.path), "version": self.version, "source": self.source}


@dataclass
class OpenScadInfo:
    found: bool = False
    path: str | None = None
    version: str | None = None
    source: str | None = None
    candidates: list[Candidate] = field(default_factory=list)
    cli_ok: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "found": self.found,
            "path": self.path,
            "version": self.version,
            "source": self.source,
            "candidates": [c.to_dict() for c in self.candidates],
            "cli_ok": self.cli_ok,
            "detail": self.detail,
        }


# --------------------------------------------------------------------------
# 低层执行
# --------------------------------------------------------------------------
# 实测（Windows 11 中文版 + OpenSCAD 2021.01）：OpenSCAD 把控制台输出按系统 ANSI
# 代码页（中文 Windows 为 GBK/936）编码，而不是 UTF-8。若按 UTF-8 解码，
# 用户看到的报错路径会变成乱码（路径里的汉字会被解成替换字符）。
# 因此必须按「启动器代码页」解码，否则等于把可读的错误信息变成噪音。
def console_encoding() -> str:
    """OpenSCAD 控制台输出使用的编码。"""
    if os.name != "nt":
        import locale

        return locale.getpreferredencoding(False) or "utf-8"
    try:
        import ctypes

        acp = int(ctypes.windll.kernel32.GetACP())  # type: ignore[attr-defined]
        return f"cp{acp}" if acp else "utf-8"
    except Exception:  # noqa: BLE001
        return "utf-8"


_CONSOLE_ENCODING = console_encoding()


def _decode(raw: bytes) -> str:
    """把 OpenSCAD 的原始输出字节解码为可读文本，依次尝试：系统代码页 → UTF-8。"""
    for encoding in (_CONSOLE_ENCODING, "utf-8"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _popen_kwargs() -> dict[str, object]:
    """统一 subprocess 参数。

    注意：不使用 text=True，改为捕获 bytes 后显式解码，
    这样输出编码始终可控（见 console_encoding 的说明）。
    """
    kwargs: dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "creationflags": CREATE_NO_WINDOW,
    }
    if os.name != "nt":
        kwargs.pop("creationflags", None)
    return kwargs


def output_of(proc: subprocess.CompletedProcess) -> str:
    """取出进程输出文本（兼容 bytes / str 两种捕获方式）。"""
    raw = proc.stdout
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return _decode(raw)
    return str(raw)


def read_version(exe: Path, timeout: int = 20) -> str | None:
    """执行 `openscad --version` 并解析版本号。"""
    try:
        # 走 procguard：这些子进程要登记在册，应用退出时才会被一起收走
        # （否则关掉软件后 openscad.exe 还会在后台跑，见 procguard 的说明）。
        proc = guarded_run([str(exe), "--version"], timeout=timeout, **_popen_kwargs())
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("读取版本失败 %s：%s", exe, exc)
        return None
    output = output_of(proc)
    match = VERSION_RE.search(output)
    if match:
        return match.group(1).strip()
    match = re.search(r"([0-9]{4}\.[0-9]{2}(?:\.[0-9]+)?)", output)
    return match.group(1) if match else None


def validate_executable(path: Path) -> tuple[bool, str, str | None]:
    """校验一个用户手动指定的路径是否真的是可用的 OpenSCAD。

    返回 (是否有效, 说明, 版本)。
    """
    if not path.exists():
        return False, "指定的文件不存在。", None
    if path.is_dir():
        nested = find_in_directory(path)
        if nested:
            version = read_version(nested)
            if version:
                return True, f"在所选目录中找到 OpenSCAD {version}。", version
        return False, "所选目录里没有找到 openscad.exe。", None
    if path.suffix.lower() not in (".exe", ".com", "") and os.name == "nt":
        return False, "请选择 openscad.exe 可执行文件。", None
    version = read_version(path)
    if not version:
        return False, "该文件无法作为 OpenSCAD 运行（尝试执行 --version 失败）。", None
    return True, f"OpenSCAD {version} 可用。", version


def run_cli(
    args: list[str],
    exe: Path | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess:
    """执行 openscad CLI，返回完整结果（调用方判断 returncode）。

    输出为原始 bytes，请用 `output_of()` 解码后再展示给用户。
    """
    target = exe or resolve_executable()
    if target is None:
        raise OpenScadError(
            "没有找到 OpenSCAD，无法生成 3D 模型。",
            hint="请在设置中点击「自动安装 OpenSCAD」，或手动指定 openscad.exe 的位置。",
            code="OPENSCAD_MISSING",
        )
    cmd = [str(target), *args]
    log.debug("执行：%s", " ".join(cmd))
    # 同样走 procguard（登记 + 退出时连带结束）
    return guarded_run(cmd, timeout=timeout, **_popen_kwargs())


# --------------------------------------------------------------------------
# 搜索常见安装位置
# --------------------------------------------------------------------------
def search_directories() -> Iterator[tuple[Path, str]]:
    """产出待搜索目录；顺序即优先级。"""
    files = paths()

    # 1) 本程序自己的免安装运行目录（最低占用、无需管理员）
    yield files.runtime_dir / "openscad", "managed"
    yield files.runtime_dir / "OpenSCAD", "managed"

    if os.name != "nt":
        for base in ("/usr/bin", "/usr/local/bin", "/snap/bin", "/Applications/OpenSCAD.app/Contents/MacOS"):
            yield Path(base), "common"
        return

    # 2) 环境变量显式指定
    for env_name in ("OPENSCAD_PATH", "OPENSCAD_HOME", "OPENSCAD_DIR"):
        raw = os.environ.get(env_name)
        if raw:
            candidate = Path(raw)
            yield (candidate.parent if candidate.is_file() else candidate), "env"

    # 3) 各盘符的常见目录（不假设只有 C 盘 —— 用户可能装在 D/E 盘）
    drives = ["C:", "D:", "E:", "F:"]
    # 从当前程序所在盘与系统盘补充，去重后保持顺序
    for letter in (Path(sys.executable).drive, os.environ.get("SystemDrive", "C:")):
        if letter and letter not in drives:
            drives.insert(0, letter)

    sub_paths = (
        r"Program Files\OpenSCAD",
        r"Program Files (x86)\OpenSCAD",
        r"OpenSCAD",
        r"Programs\OpenSCAD",
        r"Software\OpenSCAD",
        r"apps\OpenSCAD",
    )
    for drive in drives:
        root = Path(drive + "\\")
        if not root.exists():
            continue
        for sub in sub_paths:
            yield root / sub, "common"
        # 4) 用户级安装（winget / 便携）
        yield root / "Users" / (os.environ.get("USERNAME") or "") / "AppData" / "Local" / "Programs" / "OpenSCAD", "user"

    # 5) LOCALAPPDATA 用户级安装
    local = os.environ.get("LOCALAPPDATA")
    if local:
        yield Path(local) / "Programs" / "OpenSCAD", "user"
        yield Path(local) / "OpenSCAD", "user"
        yield Path(local) / "Microsoft" / "WinGet" / "Packages", "winget"

    # 6) 包管理器
    programdata = os.environ.get("ProgramData")
    if programdata:
        yield Path(programdata) / "chocolatey" / "bin", "chocolatey"
        yield Path(programdata) / "chocolatey" / "lib" / "openscad" / "tools", "chocolatey"
    home = Path.home()
    yield home / "scoop" / "apps" / "openscad" / "current", "scoop"
    yield home / "scoop" / "shims", "scoop"


def find_in_directory(directory: Path, depth: int = 3) -> Path | None:
    """在目录内（含有限层子目录）寻找 openscad.exe。"""
    if not directory.exists() or not directory.is_dir():
        return None
    for name in EXE_NAMES:
        direct = directory / name
        if direct.is_file():
            return direct
    # 便携包常见结构：openscad-2021.01/openscad.exe
    try:
        for child in sorted(directory.iterdir(), key=lambda p: p.name):
            if child.is_dir() and depth > 0:
                found = find_in_directory(child, depth - 1)
                if found:
                    return found
    except (OSError, PermissionError):
        return None
    return None


def search_pathext() -> Path | None:
    """在 PATH 中查找（用户可能已经手工配置过）。"""
    for name in EXE_NAMES:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def search_registry() -> list[tuple[Path, str]]:
    """查注册表卸载信息，拿到用户实际安装位置。"""
    if os.name != "nt":
        return []
    results: list[tuple[Path, str]] = []
    try:
        import winreg
    except ImportError:
        return []

    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, subkey in roots:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                count = winreg.QueryInfoKey(key)[0]
                for index in range(count):
                    try:
                        name = winreg.EnumKey(key, index)
                    except OSError:
                        continue
                    if "openscad" not in name.lower():
                        continue
                    try:
                        with winreg.OpenKey(key, name) as entry:
                            display, _ = winreg.QueryValueEx(entry, "DisplayName")
                            if "openscad" not in str(display).lower():
                                continue
                            location, _ = winreg.QueryValueEx(entry, "InstallLocation")
                            if location:
                                results.append((Path(os.path.expandvars(str(location))), "registry"))
                    except OSError:
                        continue
        except OSError:
            continue
    return results


# --------------------------------------------------------------------------
# 汇总检测
# --------------------------------------------------------------------------
def discover(deep: bool = True) -> list[Candidate]:
    """找出所有可用的 OpenSCAD 副本。deep=True 时逐个执行 --version 验证。"""
    seen: set[str] = set()
    candidates: list[Candidate] = []

    def add(path: Path | None, source: str) -> None:
        if path is None:
            return
        try:
            key = str(path.resolve()).lower()
        except OSError:
            key = str(path).lower()
        if key in seen or not path.is_file():
            return
        seen.add(key)
        candidates.append(Candidate(path=path, source=source))

    # 1) 用户配置中记录的位置优先
    configured = store().config.openscad.path
    if configured:
        add(Path(configured), "config")

    # 2) PATH
    add(search_pathext(), "path")

    # 3) 注册表
    for location, source in search_registry():
        add(find_in_directory(location) or (location / "openscad.exe"), source)

    # 4) 常见目录
    for directory, source in search_directories():
        add(find_in_directory(directory), source)

    if deep:
        for candidate in candidates:
            candidate.version = read_version(candidate.path)

    # 有版本号的排前面，版本号高的优先
    def sort_key(item: Candidate) -> tuple[int, tuple[int, ...]]:
        parts: list[int] = []
        if item.version:
            for chunk in re.split(r"[.\-]", item.version):
                parts.append(int(chunk) if chunk.isdigit() else 0)
        return (0 if item.version else 1, tuple(-p for p in parts))

    candidates.sort(key=sort_key)
    return candidates


def detect(deep: bool = True) -> OpenScadInfo:
    """完整检测：找到副本 → 选中最佳 → 真实执行 CLI 自检。

    这里以前还挂着一个 `prefer_deep: bool = True`：它承诺「多个副本时优先选深扫
    出来的那一个」，但函数体里一行都没用过它，全部调用方（main.py / envcheck /
    两条 API 路由 / 自检）也只传 `deep` —— 挂着形参的装饰性承诺比没有承诺更坏：
    下一个人会以为改这个开关能影响选型。选型规则实际只有一条（见下面的 sort_key
    与 `verified[0]`），要加「优先深扫」就得先说清哪个才算深扫出来的副本。
    """
    info = OpenScadInfo()
    candidates = discover(deep=deep)
    info.candidates = candidates

    if not candidates:
        info.detail = "未检测到 OpenSCAD。可点击「自动安装 OpenSCAD」由软件自动完成安装与配置。"
        return info

    verified = [c for c in candidates if c.version]
    chosen = verified[0] if verified else candidates[0]

    info.found = True
    info.path = str(chosen.path)
    info.version = chosen.version
    info.source = chosen.source

    if not chosen.version:
        info.detail = f"找到 {chosen.path}，但执行 --version 失败，可能文件损坏或缺少运行库。"
        info.cli_ok = False
        return info

    # CLI 冒烟测试：真的能转出 STL 才算通过（第四十四节：启动时检查版本与可用性）
    ok, message, _ = smoke_test(chosen.path)
    info.cli_ok = ok
    if ok:
        info.detail = f"OpenSCAD {chosen.version} 已就绪。"
    else:
        info.detail = f"检测到 OpenSCAD {chosen.version}，但无法正常执行：{message}"

    # 把检测结果写回配置，减少下次启动耗时
    try:
        cfg = store()
        if cfg.config.openscad.path != str(chosen.path) or cfg.config.openscad.version != chosen.version:
            cfg.update("openscad", {"path": str(chosen.path), "version": chosen.version})
    except Exception:  # noqa: BLE001
        log.debug("写回 OpenSCAD 配置失败", exc_info=True)

    return info


def resolve_executable() -> Path | None:
    """给出当前应该使用的 openscad.exe。"""
    configured = store().config.openscad.path
    if configured:
        path = Path(configured)
        if path.is_file():
            return path
    found = search_pathext()
    if found:
        return found
    candidates = discover(deep=True)
    for candidate in candidates:
        if candidate.version:
            return candidate.path
    return candidates[0].path if candidates else None


SMOKE_SCAD = "cube([10,10,10], center=true);\n"


def smoke_test(exe: Path | str | None = None, work_dir: Path | None = None) -> tuple[bool, str, float]:
    """真实编译一个 10mm 立方体 → 能输出 STL 才认为 OpenSCAD 可用。

    返回 (是否成功, 说明, 耗时秒)。
    """
    import time

    target = Path(exe) if exe else resolve_executable()
    if target is None:
        return False, "未找到 OpenSCAD。", 0.0

    base = work_dir or paths().cache_dir / "selftest"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"无法创建自检目录：{exc.strerror or exc}", 0.0

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="smoke_", dir=str(base)) as tmp:
        scad = Path(tmp) / "selftest.scad"
        stl = Path(tmp) / "selftest.stl"
        try:
            scad.write_text(SMOKE_SCAD, encoding="utf-8")
        except OSError as exc:
            return False, f"无法写入自检文件：{exc.strerror or exc}", 0.0
        try:
            proc = guarded_run(
                [str(target), "-o", str(stl), str(scad)],
                timeout=120,
                **_popen_kwargs(),
            )
        except subprocess.TimeoutExpired:
            return False, "OpenSCAD 执行超时（超过 120 秒没有响应）。", time.perf_counter() - started
        except OSError as exc:
            return False, f"无法启动 OpenSCAD：{exc.strerror or exc}", time.perf_counter() - started

        elapsed = time.perf_counter() - started
        output = output_of(proc)
        if stl.is_file() and stl.stat().st_size > 0:
            return True, f"CLI 测试成功（生成 {stl.stat().st_size} 字节 STL，耗时 {elapsed:.1f} 秒）。", elapsed
        tail = "\n".join(output.strip().splitlines()[-6:]) or f"退出码 {proc.returncode}"
        return False, f"OpenSCAD 未能生成 STL。{tail}", elapsed


# --------------------------------------------------------------------------
# 安装
# --------------------------------------------------------------------------
def managed_install_dir() -> Path:
    """本程序自己管理的 OpenSCAD 运行目录。

    位置策略（「少占用 C 盘」）：优先放在程序所在盘（通常 D 盘）的用户目录下，
    辅以本程序 LOCALAPPDATA 目录作为兜底。两个位置都「不需要管理员权限」。
    """
    files = paths()
    preferred = files.runtime_dir / "openscad"
    return preferred


def official_url(asset: str) -> str:
    """只用官方渠道。"""
    return f"{OFFICIAL_MIRROR}/{asset}"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_download(path: Path, asset: str, expected: str | None = None) -> tuple[bool, str]:
    """校验下载文件完整性。官方发布了 SHA-256 时按官方值比对。"""
    pinned = expected or PINNED_SHA256.get(asset)
    actual = sha256_of(path)
    if not pinned:
        return True, f"官方未提供该文件的校验值，已记录本机 SHA-256：{actual[:16]}…"
    if actual.lower() == pinned.lower():
        return True, "已校验文件完整性（SHA-256 与官方发布值一致）。"
    return False, (
        "下载文件校验失败，已中止安装以保护您的电脑。\n"
        f"期望：{pinned}\n实际：{actual}\n"
        "这通常意味着下载被中断或被网络中的第三方篡改，请重试或改为手动安装。"
    )


ProgressFn = Callable[[str, float | None, str, dict[str, object]], None]


def _emit(progress: ProgressFn | None, stage: str, percent: float | None, message: str, **extra: object) -> None:
    log.info("[openscad-install][%s] %s", stage, message)
    if progress:
        try:
            progress(stage, percent, message, dict(extra))
        except Exception:  # noqa: BLE001
            log.debug("进度回调异常", exc_info=True)


def _replace_with_retry(tmp: Path, target: Path, attempts: int = 6) -> None:
    """把下载好的 .part 改名成正式文件，容忍 Windows 上的短暂文件占用。

    实测：刚写完 20MB 的文件立刻 `Path.replace()` 会偶发
    `WinError 32 另一个程序正在使用此文件`（杀软扫描 / 索引服务）。
    这类占用是瞬时的，等几百毫秒就能过；如果不重试，用户会
    「进度条走到 100% 却提示下载失败」，而且要重新下 20MB。
    """
    import time as _time

    last: OSError | None = None
    for index in range(attempts):
        try:
            tmp.replace(target)
            return
        except OSError as exc:  # noqa: PERF203
            last = exc
            _time.sleep(0.2 * (index + 1))
    assert last is not None
    raise last


def _rename_with_retry(src: Path, dst: Path, attempts: int = 8) -> None:
    """把**目录**改名，容忍 Windows 上的短暂占用（文件版见 `_replace_with_retry`）。

    实测触发场景：安装流程会先在旁路目录里跑一次 `openscad.exe` 冒烟测试，
    紧接着就把这个目录改名到正式位置。Windows 在进程退出后仍会短暂持有该
    exe 的映像句柄，此时 `rename` 报 `[WinError 5] 拒绝访问` ——
    不是"权限不够"，只是"还没放手"。等几百毫秒到几秒就会过去，
    所以这里退避重试（0.2s 起，累计约 7 秒），而不是让用户看到"安装失败"。
    """
    import time as _time

    last: OSError | None = None
    for index in range(attempts):
        try:
            src.rename(dst)
            return
        except OSError as exc:  # noqa: PERF203
            last = exc
            _time.sleep(0.2 * (index + 1))
    assert last is not None
    raise last


def _swap_install_dir(staging: Path, target_dir: Path) -> None:
    """把旁路目录换成正式安装目录，**失败时保证原来的目录还在**。

    顺序刻意做成「三明治」，而不是先删后换：
        正式目录 → .old 备份  →  旁路目录 → 正式目录  →  删掉 .old
    第二步失败就把 .old 改回去。失败抛 OSError（调用方负责翻译成用户可读的错误）。
    """
    backup = target_dir.with_name(target_dir.name + ".old")
    shutil.rmtree(backup, ignore_errors=True)
    moved_aside = False
    if target_dir.exists():
        _rename_with_retry(target_dir, backup)
        moved_aside = True
    try:
        _rename_with_retry(staging, target_dir)
    except OSError:
        if moved_aside:
            try:
                _rename_with_retry(backup, target_dir)
            except OSError as rollback_exc:
                log.error("回滚 OpenSCAD 目录失败：%s", rollback_exc)
                raise
        raise
    # 新目录已就位，旧目录可以安全清掉（删不掉也不影响使用）
    shutil.rmtree(backup, ignore_errors=True)


def download_asset(
    asset: str,
    progress: ProgressFn | None = None,
    dest_dir: Path | None = None,
) -> Path:
    """从官方源下载文件，带进度、超时与重试。"""
    import httpx

    target_dir = dest_dir or paths().downloads_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / asset
    url = official_url(asset)

    if target.exists() and target.stat().st_size > 0:
        ok, message = verify_download(target, asset)
        if ok:
            _emit(progress, "download", 100.0, "已存在完整安装包，跳过下载。")
            return target
        target.unlink(missing_ok=True)

    _emit(progress, "download", 0.0, "正在从 OpenSCAD 官方源下载…", url=url)

    last_error: Exception | None = None
    for attempt in range(1, 4):
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            # 上一轮的残留 .part 必须先删掉：如果它被杀软/索引服务短暂占用，
            # 新一轮会先写完 20MB 再在 replace 时失败 —— 白白浪费一次下载。
            tmp.unlink(missing_ok=True)
            with httpx.stream(
                "GET",
                url,
                follow_redirects=True,
                timeout=httpx.Timeout(30.0, read=120.0),
                headers={"User-Agent": f"{APP_SLUG}-installer"},
            ) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length") or 0)
                received = 0
                with tmp.open("wb") as handle:
                    for chunk in response.iter_bytes(chunk_size=256 * 1024):
                        handle.write(chunk)
                        received += len(chunk)
                        if total:
                            percent = min(99.0, received * 100.0 / total)
                            _emit(
                                progress,
                                "download",
                                percent,
                                f"正在下载 OpenSCAD… {received / 1048576:.1f} / {total / 1048576:.1f} MB",
                                received=received,
                                total=total,
                            )
                        else:
                            _emit(
                                progress,
                                "download",
                                None,
                                f"正在下载 OpenSCAD… {received / 1048576:.1f} MB",
                                received=received,
                                total=0,
                            )
            # Windows 上杀软/索引服务会短暂持有刚写完的文件，replace 会报 WinError 32。
            # 这不是网络问题，重试几百毫秒就能过，不该让用户重新下 20MB。
            _replace_with_retry(tmp, target)
            return target
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            log.warning("下载失败（第 %d 次）：%s", attempt, exc)
            if attempt < 3:
                _emit(progress, "download", None, f"网络中断，正在重试（{attempt}/2）…")

    # 三次都失败：把半截的 .part 删掉再报错。
    # 留着的后果很实在：那是一个**永远用不上**的文件（正因为它不完整才会重下），
    # 却要占用户最多 20MB 的磁盘、还会在下一次下载时先被删一遍。
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        tmp.unlink(missing_ok=True)
    except OSError as exc:
        log.info("清理未完成的下载文件失败（不影响使用）：%s", exc)

    raise OpenScadError(
        f"无法从 OpenSCAD 官方源下载安装包：{last_error}",
        hint="请检查网络连接或代理设置。也可以访问 " + OFFICIAL_SITE + " 手动下载安装。",
        code="API_UNREACHABLE",
    ) from last_error


def install_portable(progress: ProgressFn | None = None) -> OpenScadInfo:
    """免管理员安装：下载官方 zip 并解压到程序自己的运行目录。"""
    asset = ASSET_ZIP
    _emit(progress, "prepare", 0.0, "准备安装 OpenSCAD（免管理员方式）…")

    archive = download_asset(asset, progress)

    _emit(progress, "verify", None, "正在校验下载文件…")
    ok, message = verify_download(archive, asset)
    _emit(progress, "verify", None, message)
    if not ok:
        archive.unlink(missing_ok=True)
        raise OpenScadError(message, hint="请重试，或访问 " + OFFICIAL_SITE + " 手动下载。", code="SCAD_INVALID")

    target_dir = managed_install_dir()
    # 解压到**旁路目录**，全部验证通过后再替换正式目录。
    #
    # 为什么不能「先删正式目录、再解压」：解压失败（磁盘满、杀软拦截、
    # 压缩包损坏）会让用户连**原来那份能用的** OpenSCAD 都没了 ——
    # 一次失败的「重新安装」把可用环境砸掉，是最不该发生的一类退化
    # （和几何修复里「绝不采纳更差的那一版」是同一条原则）。
    staging = target_dir.with_name(target_dir.name + ".new")
    _emit(progress, "install", None, f"正在解压到临时目录 {staging} …")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        staging.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(staging)
    except (OSError, zipfile.BadZipFile) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise OpenScadError(
            f"解压失败：{exc}",
            hint="原来的 OpenSCAD 没有被改动，可以继续使用；请确认磁盘空间充足后重试。",
            code="INTERNAL",
        ) from exc

    staged_exe = find_in_directory(staging, depth=3)
    if staged_exe is None:
        shutil.rmtree(staging, ignore_errors=True)
        raise OpenScadError(
            "安装包已解压，但没有找到 openscad.exe。",
            hint="原来的 OpenSCAD 没有被改动；也可以访问 " + OFFICIAL_SITE + " 手动安装。",
            code="OPENSCAD_MISSING",
        )

    # 先在旁路里跑一次冒烟测试：确认**新的这一份**真能出图，再动正式目录。
    # 这样「下载对了但装起来用不了」也不会把用户手里那份好的换掉。
    _emit(progress, "cli_test", None, "正在测试新下载的 OpenSCAD 能否出图…")
    staged_ok, staged_message, _staged_elapsed = smoke_test(staged_exe)
    if not staged_ok:
        shutil.rmtree(staging, ignore_errors=True)
        raise OpenScadError(
            f"新下载的 OpenSCAD 无法生成模型：{staged_message}",
            hint="原来的 OpenSCAD 没有被改动，可以继续使用；请稍后重试或访问 "
            + OFFICIAL_SITE
            + " 手动安装。",
            code="SCAD_INVALID",
        )

    _emit(progress, "install", 100.0, "解压与自检通过，正在替换正式目录…")
    # 换目录必须**可回滚**，而且必须容忍"刚跑完冒烟测试"的短暂占用。
    #
    # 两个真实踩坑（一次端到端实测里同时撞上）：
    #   1. 原实现是 `先 rmtree(正式目录) 再 rename`。冒烟测试是在 staging 里跑
    #      openscad.exe 的，Windows 在进程退出后还会短暂持有该 exe 的句柄，
    #      于是 rename 报 `[WinError 5] 拒绝访问` —— 而那时**正式目录已经被删掉了**，
    #      用户手里那份能用的 OpenSCAD 就这么没了（本文件上面刚写过
    #      "一次失败的重新安装把可用环境砸掉，是最不该发生的一类退化"）。
    #   2. rename 只有一次机会，没有重试；而文件版早就有了 _replace_with_retry。
    # 现在交给 _swap_install_dir：备份 → 换入 → 成功才清理；换入失败自动回滚。
    try:
        _swap_install_dir(staging, target_dir)
    except OSError as exc:
        raise OpenScadError(
            f"替换安装目录失败：{exc}",
            hint="原来的 OpenSCAD 没有被改动，可以继续使用。"
            "通常是杀毒软件或资源管理器正在扫描刚解压的文件，稍等几秒重试即可。",
            code="INTERNAL",
        ) from exc

    exe = find_in_directory(target_dir, depth=3)
    if exe is None:
        raise OpenScadError(
            "安装目录替换后没有找到 openscad.exe。",
            hint="请重试；若反复失败，请访问 " + OFFICIAL_SITE + " 手动安装。",
            code="OPENSCAD_MISSING",
        )

    # 解压成功后就地删掉压缩包：它已经有 21MB，留在用户盘上没有任何用处
    # （免安装方式每次都会重新解压，不依赖这个缓存）。这是「少占用 C 盘」的一部分。
    release_archive(archive, progress)

    store().update(
        "openscad",
        {"path": str(exe), "last_install_method": "portable", "version": read_version(exe)},
    )
    _emit(progress, "config", None, "已写入本机配置。")

    return _finalize(exe, progress)


def release_archive(archive: Path, progress: ProgressFn | None = None) -> None:
    """删除已用完的安装包，释放磁盘空间。失败不影响安装结果。"""
    try:
        if archive.is_file():
            freed = archive.stat().st_size
            archive.unlink()
            _emit(progress, "install", None, f"已清理临时安装包（释放 {freed / 1048576:.1f} MB）。")
    except OSError as exc:
        log.info("清理安装包失败（不影响使用）：%s", exc)


def _finalize(exe: Path, progress: ProgressFn | None) -> OpenScadInfo:
    """安装后的统一收尾：再次检测 → CLI 测试 → 写配置 → 汇报。"""
    _emit(progress, "cli_test", None, "正在测试 OpenSCAD 命令行功能…")
    ok, message, elapsed = smoke_test(exe)
    if not ok:
        raise OpenScadError(
            f"OpenSCAD 安装完成但无法生成模型：{message}",
            hint="可能是系统缺少运行库。请访问 " + OFFICIAL_SITE + " 手动安装完整版本。",
            code="OPENSCAD_FAILED",
        )

    version = read_version(exe)
    store().update("openscad", {"path": str(exe), "version": version})

    info = detect(deep=False)
    info.cli_ok = True
    info.found = True
    info.path = str(exe)
    info.version = version
    info.detail = "OpenSCAD 已安装并配置完成。"
    _emit(progress, "done", 100.0, "OpenSCAD 已安装并配置完成。", version=version, path=str(exe), elapsed=elapsed)
    return info


def _is_admin() -> bool:
    if os.name != "nt":
        return os.geteuid() == 0  # type: ignore[attr-defined]
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False


def install_with_installer(progress: ProgressFn | None = None, silent: bool = True) -> OpenScadInfo:
    """用官方安装程序安装（NSIS）。需要管理员权限；自动申请 UAC。"""
    asset = ASSET_INSTALLER
    _emit(progress, "prepare", 0.0, "准备安装 OpenSCAD（官方安装程序）…")
    installer = download_asset(asset, progress)

    _emit(progress, "verify", None, "正在校验下载文件…")
    ok, message = verify_download(installer, asset)
    _emit(progress, "verify", None, message)
    if not ok:
        installer.unlink(missing_ok=True)
        raise OpenScadError(message, hint="请重试或手动安装。", code="SCAD_INVALID")

    _emit(progress, "install", None, "正在启动安装程序…")
    if silent:
        args = ["/S"]
        if _is_admin():
            args.append("/D=" + str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "OpenSCAD"))
        try:
            # 走 procguard：安装程序（以及它自己派生的子进程）留在作业里，
            # 用户中途关掉本软件不会留下一个没人管的安装在后台跑。
            # 注意别重复传 creationflags：`_popen_kwargs()` 里已经有一份，
            # 写两处会在参数绑定阶段就 TypeError（安装一行都跑不到）。
            proc = guarded_run([str(installer), *args], timeout=600, **_popen_kwargs())
            log.info("安装程序退出码：%s", proc.returncode)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OpenScadError(
                f"安装程序执行失败：{exc}",
                hint="请访问 " + OFFICIAL_SITE + " 手动下载安装。",
                code="INTERNAL",
            ) from exc
    else:
        # 交互式：让用户自己点下一步（UAC 由安装程序自己申请）
        guarded_popen([str(installer)])

    # 等待安装完成并再次检测（第四十四节：安装后必须再次检测安装结果）
    import time

    exe: Path | None = None
    for attempt in range(30):
        for candidate in discover(deep=True):
            if candidate.version:
                exe = candidate.path
                break
        if exe:
            break
        _emit(progress, "locate", None, f"正在等待安装完成…（{attempt + 1}/30）")
        time.sleep(2)

    if exe is None:
        raise OpenScadError(
            "安装程序已运行，但仍未检测到 OpenSCAD。",
            hint=(
                "可能安装被取消，或需要管理员权限。请访问 "
                + OFFICIAL_SITE
                + " 手动安装，或在图形界面中点击「下一步」完成安装。"
            ),
            code="OPENSCAD_MISSING",
        )

    store().update("openscad", {"path": str(exe), "last_install_method": "installer"})
    return _finalize(exe, progress)


def install(method: str = "auto", progress: ProgressFn | None = None) -> OpenScadInfo:
    """自动安装入口。

    method:
      auto      —— 免管理员方式优先（最稳妥，不弹 UAC）；失败再尝试官方安装程序
      portable  —— 只用免管理员方式
      installer —— 只用官方安装程序（需要管理员权限）

    先决条件：**已经装好就直接复用**。这一点对用户体验很关键 ——
    重装软件、点一次「自动修复」、或在安装包里跑首次准备时，
    如果机器上已有可用的 OpenSCAD，就不该让用户再等一次 20MB 下载。
    """
    files = paths().ensure()

    # 0) 已有可用的 OpenSCAD？直接复用，不做任何下载。
    existing = _reusable_installation()
    if existing is not None:
        exe, version = existing
        _emit(progress, "locate", None, f"已检测到可用的 OpenSCAD {version or ''}，直接使用，无需重新安装。")
        store().update("openscad", {"path": str(exe), "version": version})
        return _finalize(exe, progress)

    free = shutil.disk_usage(files.runtime_dir if files.runtime_dir.exists() else files.data_dir).free
    if free < 600 * 1024 * 1024:
        raise OpenScadError(
            f"磁盘剩余空间不足（仅剩 {free / 1048576:.0f} MB），无法安装 OpenSCAD。",
            hint="请清理磁盘后重试，或访问 " + OFFICIAL_SITE + " 手动安装到其他磁盘。",
            code="DISK_FULL",
        )

    errors: list[str] = []

    if method in ("auto", "portable"):
        try:
            return install_portable(progress)
        except OpenScadError as exc:
            errors.append(exc.message)
            if method == "portable":
                raise
            _emit(progress, "install", None, "免管理员方式失败，正在尝试官方安装程序…")

    if method in ("auto", "installer"):
        try:
            return install_with_installer(progress, silent=True)
        except OpenScadError as exc:
            errors.append(exc.message)
            raise OpenScadError(
                "自动安装未能完成。" + ("；".join(errors) if errors else ""),
                hint=(
                    "您也可以手动安装（只需一步）：访问 "
                    + OFFICIAL_SITE
                    + " 下载 Windows 安装包，双击安装后回到本软件点击「重新检测」。"
                ),
                code="OPENSCAD_MISSING",
            ) from exc

    raise OpenScadError("未知的安装方式。", hint="请选择自动安装或手动指定路径。", code="INTERNAL")


def _reusable_installation() -> tuple[Path, str | None] | None:
    """找出机器上已经可用（能跑 CLI）的 OpenSCAD，用于跳过重复安装。

    只看「文件存在」不够 —— 用户可能已经把文件删了或装坏了，
    因此这里做一次真实的 --version 验证，避免复用一个坏掉的副本。
    """
    try:
        configured = store().config.openscad.path
    except Exception:  # noqa: BLE001 - 配置读不出来就走完整检测
        configured = None

    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    path_hit = search_pathext()
    if path_hit:
        candidates.append(path_hit)
    for candidate in discover(deep=False):
        candidates.append(candidate.path)

    seen: set[str] = set()
    for candidate in candidates:
        try:
            key = str(candidate.resolve()).lower()
        except OSError:
            key = str(candidate).lower()
        if key in seen or not candidate.is_file():
            continue
        seen.add(key)
        version = read_version(candidate)
        if version:
            log.info("复用已有的 OpenSCAD：%s (%s)", candidate, version)
            return candidate, version
    return None


def manual_instructions() -> dict[str, object]:
    """自动安装失败时给出的最小用户操作说明（第四十三、四十六节要求）。"""
    return {
        "url": OFFICIAL_SITE,
        "mirror": OFFICIAL_MIRROR,
        "steps": [
            "点击上方链接打开 OpenSCAD 官方网站（openscad.org）。",
            "在 “Windows” 一栏下载 OpenSCAD-2021.01-x86-64-Installer.exe。",
            "双击下载的安装包，一路点击「下一步」完成安装（Windows 可能弹出权限确认，点「是」）。",
            "回到本软件，点击「重新检测」按钮即可自动识别，无需手动填写路径。",
        ],
        "note": "本软件不会打包或分发 OpenSCAD 二进制文件，始终从官方渠道获取。",
    }
