"""AI 3D Forge —— 调用 OpenSCAD CLI 把 SCAD 编译为 STL。

关键实现说明：
  * OpenSCAD 的控制台输出使用系统 ANSI 代码页（中文 Windows = GBK），
    因此统一走 openscad.output_of() 解码，绝不按 UTF-8 硬解。
  * 编译前剥离 BOM：OpenSCAD 2021.01 遇到 BOM 会直接 Parser error。
  * 编译后必须「文件存在且非空」才算成功 —— 只看退出码会漏判。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import openscad as scad_cli
from . import scad as scad_src
from .mesh import MeshMetrics

log = logging.getLogger(__name__)

COMPILE_TIMEOUT_S = 300
DEFAULT_FN = 64

# $fn 的硬性下限：低于这个值，圆弧面在屏幕上就是明显的多边形。
# （用户反馈「建模圆形时边数太少，完全达不到圆的效果」——
#   实测 AI 自己写过 `$fn = 24`，把 Ø80 的圆画成了 24 边形。）
FN_FLOOR = 48

LEVEL_MAP = {
    "ERROR": "error",
    "WARNING": "warn",
    "WARN": "warn",
    "DEPRECATED": "warn",
    "DEPRECATION": "warn",
    "TRACE": "info",
    "INFO": "info",
}

# 这些警告意味着「OpenSCAD 把某一块几何丢掉了」，模型**能编译成功**但不是想要的形状。
#
# 为什么必须单独识别：OpenSCAD 对这类问题只发 WARNING，不报错，
# 退出码仍是 0，STL 也照常生成 —— 于是整条流水线都会认为「成功」。
# 实测踩到过：AI 把 3D 的 `rotate()` 写在 `linear_extrude()` 的**子节点**位置
# （`linear_extrude(h) rotate([90,0,90]) polygon(...)`），
# OpenSCAD 只回一句 `Scaling a 2D object with 0 - removing object`，
# 把主体整块丢掉，只把底座交了出来：用户要一个 100mm 高的手机支架，
# 拿到一块 8.7mm 厚的板，而界面显示「模型生成成功」。
# 危害在于「静默」：底座还在，所以 STL 非空、编译返回 ok=True。
DROPS_GEOMETRY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"removing object", "OpenSCAD 因为缩放系数为 0 把一块几何整个丢掉了"),
    (r"may not be a valid 2d object", "把不是平面轮廓的东西当成 2D 轮廓挤出，该块几何无效"),
    (r"may not be a valid 3d object", "该块几何不是有效实体，已被忽略"),
    (r"top level object is not a 2d object", "顶层对象不是 2D 轮廓（linear_extrude 的输入必须是平面图形）"),
    (r"ignoring unknown module", "用了 OpenSCAD 不认识的模块，这块几何没有生成"),
    (r"empty geometry|object is empty|geometry is empty", "该块几何是空的，没有产生任何实体"),
    (r"ignoring (?:linear|rotate)_extrude", "挤出被忽略，该块几何没有生成"),
)


def geometry_dropped_warnings(diagnostics: list[Diagnostic]) -> list[str]:
    """挑出「几何被丢掉」的那几条警告原文。"""
    found: list[str] = []
    for item in diagnostics:
        lowered = item.text.lower()
        for pattern, _explain in DROPS_GEOMETRY_PATTERNS:
            if re.search(pattern, lowered):
                found.append(item.text.strip())
                break
    return found


class CompileError(RuntimeError):
    def __init__(self, message: str, *, hint: str | None = None, code: str = "OPENSCAD_FAILED") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code


@dataclass
class Diagnostic:
    severity: str
    text: str

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "text": self.text}


@dataclass
class CompileResult:
    ok: bool
    stl_path: Path | None
    scad_path: Path
    duration_s: float
    diagnostics: list[Diagnostic] = field(default_factory=list)
    message: str = ""
    hint: str | None = None
    fatal: str | None = None  # 致命错误原文（给 AI 自动修复用）
    # 针对这类报错的**可执行改法**（给 AI 修复用，不直接展示给用户）。
    # 为什么单列：`CGAL error in CGALUtils::applyUnion3D: assertion violation`
    # 这种原文对 AI 几乎是噪音 —— 实测自动修复拿着它改两轮还是同样的错。
    # 把它翻译成「共面重合 / 零厚度 / 非流形」三条常见成因与具体改法，
    # 修复才有方向。
    advice: str | None = None
    # 「几何被静默丢掉」的警告原文（编译成功也可能非空）。
    # 调用方必须据此提示用户并让 AI 修正 —— 否则会交出一个形状不对、
    # 却显示「生成成功」的模型。
    dropped_geometry: list[str] = field(default_factory=list)


# 已知报错 → 可执行改法。键是原文里的特征片段（大小写不敏感）。
_KNOWN_FAILURES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("cgal error", "cgaltools", "assertion violation"),
        "这是 CGAL 布尔运算（union/difference/intersection）失败，按出现频率排查：\n"
        "  1) 两个实体**面完全重合**（例如两个等高的圆柱顶面贴在一起、"
        "挖孔圆柱的端面与本体端面共面）—— 把相减/相加的实体沿该方向错开 0.01mm；\n"
        "  2) 零厚度或退化实体（高度为 0 的 linear_extrude、半径为 0 的 cylinder、"
        "两个相同的实体做 difference）；\n"
        "  3) 参与布尔的 polyhedron / hull / minkowski 网格自相交或非流形。\n"
        "改法：给所有 extrude 一个正的高度、把共面的面错开 0.01mm、"
        "避免用 2D 图形直接参与 3D 布尔。",
    ),
    (
        ("not a valid 2-manifold", "may not be a valid 2-manifold"),
        "网格不封闭（不是 2-流形）：常见于把 2D 图形混进 3D 布尔、"
        "或 difference 把实体挖穿后留下零厚度薄片。"
        "改法：所有参与布尔的都必须是 3D 实体；挖穿时让刀具两端各伸出 0.01mm。",
    ),
    (
        ("current top level object is not a 3d object", "not a 3d object"),
        "顶层对象不是三维实体：多半是只画了 2D 图形（circle/square/polygon）"
        "而忘了 linear_extrude / rotate_extrude。",
    ),
    (
        ("parser error", "syntax error"),
        "语法错误：检查括号与分号是否配对、模块名/参数名是否拼错、"
        "是否在变换块里定义了 module。",
    ),
    (
        # AI 自己发明变量名是实测过的常见失败（把提示词里的中文尺寸词直接当变量用、
        # 或把参数名改了大小写）。OpenSCAD 变量名区分大小写，拼错就是未定义。
        ("unknown variable", "undefined variable", "no such variable", "unknown module"),
        "用了未定义的变量或模块：OpenSCAD 区分大小写，逐字校对名字；"
        "确认赋值/定义写在使用之前（顶层变量必须先于引用），"
        "并且名字只用了 ASCII 字母与下划线；若意图是调用固定零件库，"
        "先确认文件顶部有 use <aiforge_parts.scad>。",
    ),
    (
        ("object may not be a valid 2-manifold and may need repair",
         "simple: not a valid 2-manifold"),
        "实体不是流形：请用 3D 基本体构造，不要用 2D 图形做 union。",
    ),
    (
        ("is not closed", "not closed"),
        "polyhedron 的面列表不封闭：检查 points/faces 是否漏了端面或重复索引、"
        "法向是否一致；手写螺旋/螺纹极易踩这个坑 —— 优先改用程序自带的固定零件库"
        "（use <aiforge_parts.scad>），它们的 polyhedron 已实测封闭。",
    ),
    (
        ("self-intersection", "self intersecting", "self-intersecting"),
        "网格自相交：多见于 hull/minkowski 包住了互相穿插的实体，或 polyhedron 顶点顺序错乱。"
        "改法：先让参与体彼此只重叠不相穿，或把复杂包络拆成多次 union。",
    ),
    (
        ("top level object is empty", "no top level object", "empty geometry"),
        "顶层几何为空：检查是否定义了 module 却忘记调用、或 difference 把整体挖没、"
        "或 for 循环范围为空（如 [0 : n-1] 里 n=0）。",
    ),
    (
        ("bad_alloc", "out of memory"),
        "内存不足：降低 $fn（曲面分段）、减少 minkowski/hull 的参与体数量、"
        "或把大阵列拆成几次 union。",
    ),
    (
        ("nan",),
        "坐标出现 NaN：检查除零、对负数开方、或引用了未定义变量参与运算"
        "（如 radius = d/2 而 d 没赋值）。",
    ),
)


#: 必须**按整词**匹配的特征片段。短片段直接做子串匹配会命中无关文本：
#: 本机实测 `"nan" in "error: unknown variable 'nano_gap'"` 为真，于是
#: 「变量名拼错」被翻译成「坐标出现 NaN：检查除零、对负数开方」——
#: 自动修复循环照着错的病因改两轮，用户等到的还是同一个错。
_WORD_BOUNDARY_NEEDLES = frozenset({"nan"})


def _needles_match(needles: tuple[str, ...], lowered: str) -> bool:
    for needle in needles:
        if needle in _WORD_BOUNDARY_NEEDLES:
            if re.search(rf"(?<![\w$]){re.escape(needle)}(?![\w$])", lowered):
                return True
        elif needle in lowered:
            return True
    return False


def explain_failure(text: str | None) -> str | None:
    """把 OpenSCAD/CGAL 的原始报错翻译成**可执行的改法**（给 AI 修复用）。

    返回 None 表示这条报错不在已知清单里（那就只把原文交给 AI）。
    """
    if not text:
        return None
    lowered = text.lower()
    for needles, advice in _KNOWN_FAILURES:
        if _needles_match(needles, lowered):
            return advice
    return None


def parse_diagnostics(output: str) -> list[Diagnostic]:
    """把 OpenSCAD 输出拆成结构化诊断；过滤掉缓存统计等噪音。"""
    diagnostics: list[Diagnostic] = []
    noise = re.compile(
        r"^(geometries in cache|geometry cache size|cgal polyhedrons in cache|"
        r"cgal cache size|total rendering time|top level object|facets|"
        r"rendering finished|compiling design|parsing design|"
        r"wrote|loaded design|documentation)",
        re.IGNORECASE,
    )
    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if not line or noise.match(line):
            continue
        severity = "info"
        match = re.match(r"^(ERROR|WARNING|WARN|DEPRECATED|DEPRECATION|TRACE|INFO)\s*:?\s*(.*)$", line, re.IGNORECASE)
        if match:
            severity = LEVEL_MAP.get(match.group(1).upper(), "info")
            line = match.group(2).strip() or line
        diagnostics.append(Diagnostic(severity=severity, text=line[:600]))
    return diagnostics


def first_error(diagnostics: list[Diagnostic]) -> str | None:
    for item in diagnostics:
        if item.severity == "error":
            return item.text
    return None


def build_scad_source(
    source: str,
    params: dict[str, float] | None = None,
    fn: int | None = None,
) -> tuple[str, list[str], list[str]]:
    """规范化源码（去 BOM、全角标点）并按需覆盖参数、注入/校准 $fn。

    fn 由目标打印机的喷嘴直径与表面质量要求算出（见 printers.resolve_fn）：
    喷嘴越细、模型越大，需要的分段数越多，否则圆弧面会出现肉眼可见的棱面。
    """
    cleaned = scad_src.ensure_no_bom(source)
    applied: list[str] = []
    ignored: list[str] = []
    if params:
        cleaned, applied, ignored = scad_src.apply_params(cleaned, params)
    target_fn = int(fn or DEFAULT_FN)
    # 只有「顶层真的赋值过 $fn」才尊重作者。
    # 不能用 `"$fn" not in cleaned` 这种子串检查：注释里提一句 $fn、
    # 模块内部局部设一次 $fn、甚至字符串里出现，都会让子串检查为真而跳过注入 ——
    # 于是按喷嘴与模型尺寸算好的细分度被静默丢弃，圆弧面出现肉眼可见的棱面。
    # （实测同一个圆柱：注入后 380 面，跳过注入只有 116 面。）
    # 而我们的提示词本身就会让 AI 在参数区写 `// @param $fn …` 注释，
    # 所以「只有注释、没有赋值」是很容易命中的情况。
    if "$fn" not in scad_src.top_level_assignments(cleaned):
        cleaned = f"$fn = {target_fn};\n" + cleaned
    # 不管顶层有没有写 $fn，都要把**过小**的那些抬起来。
    # 曾经把这一步放在 else 分支里，于是 `cylinder(..., $fn = 24)` 这种
    # 逐图元写法（优先级最高，顶层注入根本盖不住）被漏掉了 ——
    # 用户看到的仍然是一个 24 边形。
    cleaned = _raise_small_fn(cleaned, target_fn)
    # $fa/$fs 是另一套细分控制：$fn>0 时它优先，所以只要保证 $fn 够大即可；
    # 但若模型里用 $fa/$fs 且被设得很粗（$fa=30 → 12 段圆），同样要拉回来。
    cleaned = _clamp_coarse_fa_fs(cleaned, target_fn)
    return cleaned, applied, ignored


FN_ASSIGN_RE = re.compile(r"(?<![\w$])\$fn\s*=\s*(\d+)(\s*[;,)]|$)")


def _raise_small_fn(source: str, target: int) -> str:
    """把代码里**所有**过小的 $fn 赋值抬到下限（顶层与逐图元都算）。

    为什么连逐图元的都改：`cylinder(r = 40, $fn = 24)` 这种写法优先级最高，
    顶层注入再大也没用 —— 用户看到的还是 24 边形。

    注意必须**保留原来的结尾符**（`;` / `)` / `,`）：早先版本一律替换成 `$fn = N;`，
    结果 `cylinder(..., $fn = 52;)` 直接语法错误 —— 修一个显示问题却把模型弄编译不过。
    """
    floor = max(FN_FLOOR, int(target * 0.6))
    changed = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal changed
        value = int(match.group(1))
        tail = match.group(2)
        if 0 < value < floor:
            changed += 1
            return f"$fn = {floor}{tail}"
        return match.group(0)

    updated = FN_ASSIGN_RE.sub(replace, source)
    if changed:
        log.info("把 %d 处过小的 $fn 抬到 %d（避免圆弧面出现明显棱面）", changed, floor)
    return updated


FA_ASSIGN_RE = re.compile(r"(?<![\w$])\$fa\s*=\s*([\d.]+)(\s*[;,)]|$)")
FS_ASSIGN_RE = re.compile(r"(?<![\w$])\$fs\s*=\s*([\d.]+)(\s*[;,)]|$)")


def _clamp_coarse_fa_fs(source: str, target: int) -> str:
    """$fa 太大 / $fs 太大时收紧（它们会让圆变成十几边形）。"""
    max_fa = max(2.0, 360.0 / max(target, 1))
    max_fs = max(0.2, 400.0 / max(target, 1))

    def fa_replace(match: re.Match[str]) -> str:
        try:
            value = float(match.group(1))
        except ValueError:
            return match.group(0)
        return f"$fa = {max_fa:g}{match.group(2)}" if value > max_fa else match.group(0)

    def fs_replace(match: re.Match[str]) -> str:
        try:
            value = float(match.group(1))
        except ValueError:
            return match.group(0)
        return f"$fs = {max_fs:g}{match.group(2)}" if value > max_fs else match.group(0)

    return FS_ASSIGN_RE.sub(fs_replace, FA_ASSIGN_RE.sub(fa_replace, source))


def _library_search_path(existing: str | None) -> str:
    """OpenSCAD 的库搜索路径：把自带的固定零件库目录接在用户已有路径前面。

    为什么用 OPENSCADPATH 而不是把库拷到项目目录：项目目录是用户的数据，
    往里塞一个 .scad 会让"这个项目到底有哪些文件"变得含糊；而 OPENSCADPATH
    是 OpenSCAD 官方支持的库搜索路径，指向程序自己的安装目录，只读、干净。
    """
    from .parts import catalog as parts_lib

    library_dir = str(parts_lib.library_path().parent)
    if not existing:
        return library_dir
    # OpenSCAD 在 Windows 上用 ';' 分隔多个路径
    return library_dir + os.pathsep + existing


def compile_to_stl(
    source: str,
    work_dir: Path,
    basename: str = "model",
    params: dict[str, float] | None = None,
    timeout_s: int = COMPILE_TIMEOUT_S,
    fn: int | None = None,
) -> CompileResult:
    """把 SCAD 源码编译为 STL。

    work_dir 由调用方提供（通常是项目目录），源代码与 STL 都写在这里。
    fn 为目标打印机推荐的 OpenSCAD 分段数（不传则用通用默认值）。
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    scad_path = work_dir / f"{basename}.scad"
    stl_path = work_dir / f"{basename}.stl"

    text, applied, ignored = build_scad_source(source, params, fn)
    if ignored:
        log.info("以下参数未能在源码中找到顶层赋值，已忽略：%s", ignored)

    try:
        # newline="" + utf-8 无 BOM，OpenSCAD 2021.01 不接受 BOM
        scad_path.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise CompileError(
            f"无法写入模型文件：{exc.strerror or exc}",
            hint="请检查项目目录是否有写入权限，或磁盘是否已满。",
            code="DISK_FULL" if getattr(exc, "errno", None) == 28 else "INTERNAL",
        ) from exc

    try:
        if stl_path.exists():
            stl_path.unlink()
    except OSError:
        pass

    exe = scad_cli.resolve_executable()
    if exe is None:
        raise CompileError(
            "没有找到 OpenSCAD，无法生成 3D 模型。",
            hint="请点击「自动安装 OpenSCAD」，软件会自动完成安装与配置。",
            code="OPENSCAD_MISSING",
        )

    started = time.perf_counter()
    cmd = [str(exe), "-o", str(stl_path), str(scad_path)]
    log.debug("编译：%s", " ".join(cmd))
    try:
        # 走 procguard：登记 PID，应用退出时先把它收掉，
        # 避免"软件关了 OpenSCAD 还在算"（详见 procguard 的模块说明）。
        from . import procguard

        kwargs = dict(scad_cli._popen_kwargs())  # noqa: SLF001 - 同一包内的私有约定
        # 让 `use <aiforge_parts.scad>` 能解析到我们自带的固定零件库。
        # 不能靠改当前目录：编译的工作目录是用户项目目录，放库进去会污染项目。
        env = dict(kwargs.get("env") or os.environ)
        env["OPENSCADPATH"] = _library_search_path(env.get("OPENSCADPATH"))
        kwargs["env"] = env

        proc = procguard.run(cmd, timeout=timeout_s, **kwargs)
    except subprocess.TimeoutExpired:
        raise CompileError(
            f"生成模型超时（超过 {timeout_s} 秒）。",
            hint="模型可能过于复杂。可以简化需求（例如降低圆柱面数、减少镂空细节）后重试。",
            code="OPENSCAD_FAILED",
        ) from None
    except OSError as exc:
        raise CompileError(
            f"无法启动 OpenSCAD：{exc.strerror or exc}",
            hint="请在「系统诊断」中运行一次 3D 引擎检测，确认 OpenSCAD 可用。",
            code="OPENSCAD_FAILED",
        ) from exc

    duration = time.perf_counter() - started
    output = scad_cli.output_of(proc)
    diagnostics = parse_diagnostics(output)

    if not stl_path.is_file() or stl_path.stat().st_size == 0:
        error_text = first_error(diagnostics)
        message = "模型代码存在语法或几何错误，OpenSCAD 无法生成模型。"
        if error_text:
            message = f"{message}（{error_text}）"
        fatal_text = error_text or output.strip()[-800:]
        return CompileResult(
            ok=False,
            stl_path=None,
            scad_path=scad_path,
            duration_s=duration,
            diagnostics=diagnostics,
            message=message,
            hint="可以让 AI 根据错误信息自动修复，或调整需求描述后重新生成。",
            fatal=fatal_text,
            advice=explain_failure(fatal_text) or explain_failure(output),
        )

    return CompileResult(
        ok=True,
        stl_path=stl_path,
        scad_path=scad_path,
        duration_s=duration,
        diagnostics=diagnostics,
        message=f"模型生成成功（耗时 {duration:.1f} 秒）。",
        dropped_geometry=geometry_dropped_warnings(diagnostics),
    )


def analyze_stl(stl_path: Path, limits: Any | None = None, declared_gaps: list | None = None):
    """分析生成的 STL，失败时抛出 StlError（由上层翻译为用户提示）。

    limits 为 mesh.AnalysisLimits（按目标打印机推导），不传则用通用默认值。
    declared_gaps 只有 jobs 能从源码里提出来；非空时 mesh.analyze 会去量内部空隙
    （夹口/插槽这类减法特征到底切出来没有）。
    """
    from .mesh import analyze as _analyze

    return _analyze(stl_path, limits, declared_gaps)


def make_thumbnail_svg(metrics: MeshMetrics | None, name: str, size: int = 320) -> str:
    """依据模型包围盒生成一个等轴测线框缩略图（SVG）。

    这样做的好处：不依赖任何图形库、不占额外磁盘、加载极快，
    且能真实反映模型的尺寸比例，而不是一张通用占位图。
    """
    pad = size * 0.16
    usable = size - pad * 2
    label = (name or "模型")[:14]

    if metrics is None or metrics.triangles == 0:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
            f'viewBox="0 0 {size} {size}" role="img" aria-label="{label}">'
            f'<rect width="{size}" height="{size}" fill="#F7F7F8"/>'
            f'<text x="{size/2}" y="{size/2}" font-family="Segoe UI, sans-serif" font-size="14"'
            f' fill="#8E8E93" text-anchor="middle" dominant-baseline="middle">{label}</text></svg>'
        )

    dx, dy, dz = metrics.bbox
    dx = max(dx, 0.001)
    dy = max(dy, 0.001)
    dz = max(dz, 0.001)
    scale = usable / max(dx + dy * 0.5, dz + (dx + dy) * 0.28)
    w = dx * scale
    d = dy * scale
    h = dz * scale

    # 等轴测投影：x 向右下、y 向右上、z 向上
    ix, iy = w * 0.78, w * 0.45
    jx, jy = d * 0.78, -d * 0.45
    ox = pad + max(jx, 0) * 0 + (usable - (ix + jx)) / 2 + (d * 0.78 if jx < 0 else 0)
    oy = size - pad - h

    def point(px: float, py: float, pz: float) -> tuple[float, float]:
        return (ox + px * ix + py * jx, oy - pz * h + px * iy + py * jy + h)

    def box(px: float, py: float, pz: float) -> str:
        pts = [
            point(px, py, pz),
            point(px + 1, py, pz),
            point(px + 1, py + 1, pz),
            point(px, py + 1, pz),
        ]
        top = [(x, y - h) for x, y in pts]
        bottom = pts
        polygons = []
        # 顶面
        polygons.append(" ".join(f"{x:.1f},{y:.1f}" for x, y in top))
        # 可见的两个侧面
        polygons.append(" ".join(f"{x:.1f},{y:.1f}" for x, y in (bottom[1], bottom[2], top[2], top[1])))
        polygons.append(" ".join(f"{x:.1f},{y:.1f}" for x, y in (bottom[2], bottom[3], top[3], top[2])))
        return "".join(
            f'<polygon points="{p}" fill="#FFFFFF" stroke="#111111" stroke-width="1.4" '
            f'stroke-linejoin="round"/>'
            for p in polygons
        )

    art = box(0.0, 0.0, 0.0)
    dims = f"{dx:.0f}×{dy:.0f}×{dz:.0f}mm"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}" role="img" aria-label="{label} {dims}">'
        f'<rect width="{size}" height="{size}" fill="#F7F7F8"/>'
        f"{art}"
        f'<text x="{size/2}" y="{size - pad*0.35:.0f}" font-family="Segoe UI, sans-serif" font-size="12"'
        f' fill="#8E8E93" text-anchor="middle">{dims}</text></svg>'
    )


def estimate_mass_g(metrics: MeshMetrics, density_g_cm3: float = 1.24) -> float:
    """按材料密度估算耗材重量 —— **这是实体等效（100% 填充）的上限**。

    密度由目标打印机当前选用的材料决定（见 printers.material_density）：
    PLA 1.24、PETG 1.27、ABS 1.04、TPU 1.21……

    注意它不等于实际用量：真实打印按「几层墙 + 一定填充率」走，
    通常远小于实体体积。实测参照物（桌面垃圾桶：110×186×110、2 层墙、15% 填充）
    实体等效 191g，实际约 111g —— 直接拿这个数字报给用户会高估七成。
    需要给用户看的数字用 `mesh.estimate_usage()`，它会给出区间、支撑量和线材长度。
    """
    return round(metrics.volume_cm3 * density_g_cm3, 2)
