"""固定零件库（catalog）：让模型"选现成零件"，而不是每次手写几何。

为什么要有这个模块（用户反馈"72mm 转 67mm 滤镜转接环到现在也没有完成效果"）：
    螺纹这类**功能性几何**靠大模型每次现写，实测几乎必错：
      * `linear_extrude(twist)` 画出来的是螺旋肋，不是螺纹，而且不封闭；
      * 自建 polyhedron 一放进 union/difference 就被 OpenSCAD 判为
        "mesh is not closed" 并**静默丢掉整块几何**（用户拿到光滑圆筒）；
      * 用小方块沿螺旋堆，编译 190 秒仍然不封闭。
    而"72 转 67 滤镜环"这种需求，本质是**选一个标准零件 + 填两个数字**。

参考做法：earthtojake/text-to-cad 的 step.parts 技能 —— 标准件不在模型里现画，
而是去**零件库**里查（库里有规范尺寸、编号、校验值）。我们没有在线库，
但可以把**验证过的参数化零件**随程序分发：`src/aiforge/parts/aiforge_parts.scad`
里的每个模块都由 `scripts/verify_parts.py` 真实编译并实测过
（封闭 / 单一实体 / 尺寸正确 / 牙深正确），自检里也会再验一遍。

匹配到的零件会以「现成零件」块注入提示词：告诉模型可以直接 `use` 这个库并调用，
把用户给的尺寸填进去，**不要自己写螺纹几何**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

LIBRARY_NAME = "aiforge_parts.scad"


@dataclass(frozen=True)
class PartParam:
    name: str
    label: str
    unit: str
    default: float
    minimum: float
    maximum: float
    note: str = ""


@dataclass(frozen=True)
class PartSpec:
    id: str
    name: str
    module: str
    summary: str
    params: tuple[PartParam, ...]
    keywords: tuple[str, ...]
    usage: str
    verified: str
    extra_rules: tuple[str, ...] = field(default=())

    def signature(self) -> str:
        parts = []
        for param in self.params:
            default = f"{param.default:g}"
            parts.append(f"{param.name} = {default}")
        return f"{self.module}(" + ", ".join(parts) + ")"


PARTS: tuple[PartSpec, ...] = (
    PartSpec(
        id="filter_adapter",
        name="滤镜转接环",
        module="filter_adapter",
        summary="一端外螺纹、一端内螺纹、中间带挡肩的转接环（相机滤镜/镜头口径转换）",
        params=(
            PartParam("male_d", "外螺纹公称直径", "mm", 72, 20, 150, "拧进镜头的那一端"),
            PartParam("male_pitch", "外螺纹螺距", "mm", 0.75, 0.5, 2.0, "相机滤镜螺纹固定 0.75"),
            PartParam("female_d", "内螺纹公称直径", "mm", 67, 20, 150, "拧上滤镜的那一端"),
            PartParam("female_pitch", "内螺纹螺距", "mm", 0.75, 0.5, 2.0, "相机滤镜螺纹固定 0.75"),
            PartParam("height", "总高", "mm", 8.4, 4, 30, "滤镜螺纹咬合 3~5mm 即可"),
            PartParam("shoulder_t", "挡肩厚度", "mm", 1.4, 0.6, 5, "挡住滤镜不让它拧穿"),
            PartParam("clearance", "内螺纹单边间隙", "mm", 0.35, 0.15, 0.8, "打印件要留松一点"),
            PartParam("wall", "最小壁厚", "mm", 2.0, 1.2, 5, "升口径时决定外扩后外径；太薄易裂"),
        ),
        keywords=(
            "滤镜", "转接环", "转接圈", "转接头", "滤镜环", "口径转换", "镜头",
            "螺纹", "螺距", "拧上", "拧进", "接环", "adapter", "filter", "step-up", "step-down",
        ),
        usage="use <aiforge_parts.scad>\nfilter_adapter(male_d = 72, female_d = 67);",
        verified="scripts/verify_parts.py：升/降口径均封闭、单一实体；67→77 实测 Ø81.7×8.4、中心通孔 Ø62、内外牙深 0.458mm",
        extra_rules=(
            "口径映射（最常填错，务必照此）：『A 转 B 转接环 / A to B』= male_d=A、female_d=B —— "
            "male_d 是拧进镜头那端的外螺纹，female_d 是接滤镜那端的内螺纹。"
            "例：『67 转 77』→ male_d=67、female_d=77；『77 转 67』→ male_d=77、female_d=67。"
            "升口径（male_d<female_d）时程序会自动把外径扩到 female_d+2*(clearance+wall) 并把大端翻到贴平台，无需额外处理。",
            "height 建议 ≥8mm：要同时容纳外螺纹咬合、外扩台肩、内螺纹咬合三段，太矮螺纹过短拧不牢。",
        ),
    ),
    PartSpec(
        id="thread_sleeve",
        name="螺纹套筒",
        module="thread_sleeve",
        summary="内外都有螺纹的短管，用于延长筒、双公/双母转接套",
        params=(
            PartParam("outer_d", "外螺纹公称直径", "mm", 72, 20, 150),
            PartParam("outer_pitch", "外螺纹螺距", "mm", 0.75, 0.5, 2.0),
            PartParam("inner_d", "内螺纹公称直径", "mm", 67, 20, 150),
            PartParam("inner_pitch", "内螺纹螺距", "mm", 0.75, 0.5, 2.0),
            PartParam("height", "长度", "mm", 10, 4, 60),
            PartParam("clearance", "内螺纹单边间隙", "mm", 0.35, 0.15, 0.8),
        ),
        keywords=("套筒", "延长筒", "延长管", "转接套", "双螺纹", "sleeve", "coupler", "extension"),
        usage="use <aiforge_parts.scad>\nthread_sleeve(outer_d = 72, inner_d = 67, height = 10);",
        verified="scripts/verify_parts.py：封闭、单一实体、Ø72×10、牙深 0.458mm",
    ),
    PartSpec(
        id="threaded_cap",
        name="螺纹盖",
        module="threaded_cap",
        summary="带内螺纹与顶盖的旋盖（镜头盖、防尘盖、瓶盖），外圈带滚花便于手拧",
        params=(
            PartParam("inner_d", "内螺纹公称直径", "mm", 67, 15, 160),
            PartParam("pitch", "螺距", "mm", 0.75, 0.5, 3.0),
            PartParam("height", "总高", "mm", 12, 4, 60),
            PartParam("top_t", "顶盖厚度", "mm", 2.0, 0.8, 6),
            PartParam("clearance", "内螺纹单边间隙", "mm", 0.35, 0.15, 0.8),
        ),
        # 「盖」这种单字关键词必须写成复合词，不能只留单字：
        # 实测 `match("帮我掩盖一下表面瑕疵")`、`match("这个设计要覆盖所有表面")`
        # 都因为「盖」这一个字命中 threaded_cap，把零件块塞进完全不相关的提示词 ——
        # 而 `_parts_block_for` 自己的注释写的是「prompt 只在真正相关时才变长」，
        # 这就是那句注释在骗人。中文单字（盖/罩/座…）在散文里几乎都有别的词义，
        # 必须连词成词才能当关键词，对应守卫见 selfcheck.py::t_parts_library 的阴性对照。
        keywords=("旋盖", "镜头盖", "防尘盖", "瓶盖", "盖子", "螺纹盖", "cap", "lid", "cover"),
        usage="use <aiforge_parts.scad>\nthreaded_cap(inner_d = 67);",
        verified="scripts/verify_parts.py：封闭、单一实体、Ø72.5×12、牙深 0.35mm",
    ),
)

# 螺纹常识（滤镜口径 → 螺距）。相机滤镜螺纹几乎全是 0.75mm 螺距，
# 但大模型经常自己编成 1.0 或 1.25 —— 那样拧不上，所以直接写进提示词。
COMMON_FILTER_PITCH = 0.75
FILTER_SIZES = (37, 39, 40.5, 43, 46, 49, 52, 55, 58, 62, 67, 72, 77, 82, 86, 95, 105)


def library_path() -> Path:
    """零件库 .scad 的位置（打包后也能找到）。"""
    here = Path(__file__).resolve().parent / LIBRARY_NAME
    if here.is_file():
        return here
    # PyInstaller onedir：数据被放到 _internal/parts（见 AI-3D-Forge.spec）
    import sys

    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    for candidate in (base / "parts" / LIBRARY_NAME, base / LIBRARY_NAME):
        if candidate.is_file():
            return candidate
    return here


def _score(spec: PartSpec, text: str) -> int:
    lowered = text.lower()
    score = 0
    for keyword in spec.keywords:
        if keyword.lower() in lowered:
            # 长的关键词更有信息量：口径/结构类的词权重更高
            score += 2 + min(len(keyword), 6) // 2
    return score


def match(requirement: str, limit: int = 2) -> list[PartSpec]:
    """按关键词匹配现成零件（简单、可解释；不做语义猜测）。"""
    if not requirement or not requirement.strip():
        return []
    scored = [(spec, _score(spec, requirement)) for spec in PARTS]
    hits = [(spec, score) for spec, score in scored if score > 0]
    hits.sort(key=lambda item: item[1], reverse=True)
    return [spec for spec, _score_value in hits[:limit]]


def thread_sizes_in(text: str) -> list[float]:
    """从需求里抠出「M72×0.75」「72mm 转 67mm」这类螺纹尺寸（诊断/提示用）。"""
    found: list[float] = []
    for match_obj in re.finditer(r"[Mm]\s*(\d{2,3}(?:\.\d)?)\s*[×xX*]\s*(\d(?:\.\d+)?)", text):
        try:
            found.append(float(match_obj.group(1)))
        except ValueError:
            continue
    for match_obj in re.finditer(r"(\d{2,3}(?:\.\d)?)\s*(?:mm|毫米)", text, re.IGNORECASE):
        try:
            value = float(match_obj.group(1))
        except ValueError:
            continue
        if value in FILTER_SIZES or 20 <= value <= 150:
            found.append(value)
    return found


# 「A 转 B」这类**明确的口径转换**表述。出现顺序即 male→female（outer→inner）。
# 只认这种强结构：像「77mm 滤镜用在 67mm 镜头上」这种两个口径之间夹着别的词的
# 写法匹配不上 → 返回 None → 回退 AI，避免语序陷阱把 male/female 填反。
CONVERSION_RE = re.compile(
    r"(?P<a>\d{2,3}(?:\.\d+)?)\s*(?:mm|毫米)?\s*(?:[×xX*]\s*\d(?:\.\d+)?)?\s*"
    r"(?:转到|转|换成|到|to|->|→|—|-)\s*"
    r"(?:[Mm]\s*)?(?P<b>\d{2,3}(?:\.\d+)?)\s*(?:mm|毫米)?\s*(?:[×xX*]\s*\d(?:\.\d+)?)?",
    re.IGNORECASE,
)

# 用户显式给的总高（「高10mm」「高度 10」「总高:12」）。
HEIGHT_RE = re.compile(r"(?:总高|高度|高|厚)\s*[:：]?\s*(\d{1,3}(?:\.\d+)?)\s*(?:mm|毫米)?")
# 用户显式给的螺距（「M67×0.75」「螺距1.0」）。滤镜螺纹统一 0.75，
# 一旦出现别的螺距就回退 AI，不硬套 0.75。
PITCH_RE = re.compile(r"[×xX*]\s*(\d(?:\.\d+)?)|(?:螺距|pitch)\s*[:：]?\s*(\d(?:\.\d+)?)", re.IGNORECASE)


def _height_in(requirement: str, spec) -> float | None:
    """取用户显式给的总高：优先用确认过的规格（用户可能在确认卡片里改过），
    其次从需求文本里抠。取不到返回 None（用零件默认高）。"""
    if spec is not None:
        for item in getattr(spec, "specs", []) or []:
            if item.value is None:
                continue
            if re.search(r"height|高", f"{item.name} {item.label}", re.IGNORECASE):
                return float(item.value)
    m = HEIGHT_RE.search(requirement or "")
    return float(m.group(1)) if m else None


def _nonstandard_pitch_in(requirement: str) -> bool:
    """需求里是否出现了**非 0.75** 的螺距。出现就回退 AI（不硬套标准螺距）。"""
    for m in PITCH_RE.finditer(requirement or ""):
        raw = m.group(1) or m.group(2)
        if raw is None:
            continue
        try:
            pitch = float(raw)
        except ValueError:
            continue
        if abs(pitch - COMMON_FILTER_PITCH) > 1e-6:
            return True
    return False


def extract_call(requirement: str, spec=None):
    """需求足够明确时，直接给出「零件 + 参数值」，供程序确定性直出（不靠 AI 填参）。

    返回 None 的情况（都应当回退 AI）：没命中零件；口径转换表述不明确
    （语序可能颠倒）；出现了非标准螺距；任一参数超出零件允许范围。
    """
    if not requirement or not requirement.strip():
        return None
    matched = match(requirement, limit=1)
    if not matched:
        return None
    part = matched[0]

    values: dict = {}
    conv = CONVERSION_RE.search(requirement)
    if part.id == "filter_adapter":
        if not conv:
            return None
        values = {"male_d": float(conv.group("a")), "female_d": float(conv.group("b"))}
    elif part.id == "thread_sleeve":
        if not conv:
            return None
        values = {"outer_d": float(conv.group("a")), "inner_d": float(conv.group("b"))}
    elif part.id == "threaded_cap":
        sizes = thread_sizes_in(requirement)
        if not sizes:
            return None
        values = {"inner_d": sizes[0]}
    else:
        return None

    # 用户显式给了非标准螺距 → 不直出（滤镜螺纹统一 0.75，别的螺距交给 AI 判断）。
    if _nonstandard_pitch_in(requirement):
        return None
    # 用户显式给了总高就照用，否则直出会 silently 用默认高、违背用户意图。
    height = _height_in(requirement, spec)
    if height is not None:
        values["height"] = height

    # 未显式给出的参数用零件默认值（螺距即 COMMON_FILTER_PITCH）。
    for param in part.params:
        values.setdefault(param.name, param.default)
    # 任一参数越界 → 不直出（回退 AI），绝不生成超出验证范围的几何。
    for param in part.params:
        value = values.get(param.name)
        if value is None or value < param.minimum or value > param.maximum:
            return None
    return part, values


def render_source(spec: PartSpec, values: dict, name: str, summary: str) -> str:
    """把「零件 + 参数值」渲染成符合 SCAD_CONVENTIONS 的完整源码。

    关键点：@param 行与顶层赋值一一对应，且**调用处引用顶层变量**（而非字面量），
    这样 params 模式（滑杆调参）改写顶层赋值后调用会跟着变，模型可直接微调。
    """
    clean_name = (name or spec.name).strip().splitlines()[0][:40] or spec.name
    clean_summary = (summary or spec.summary).strip().splitlines()[0][:40] or spec.summary
    lines = [f"// @name {clean_name}", f"// @summary {clean_summary}"]
    assigns = []
    call_args = []
    for param in spec.params:
        if param.name not in values:
            continue
        value = values[param.name]
        label = "".join(param.label.split())  # 说明里不留空格，保证 @param 可解析
        lines.append(
            f"// @param {param.name} {value:g} {label} {param.minimum:g} {param.maximum:g}"
        )
        assigns.append(f"{param.name} = {value:g};")
        call_args.append(f"{param.name} = {param.name}")
    lines.extend(assigns)
    lines.append(f"use <{LIBRARY_NAME}>")
    lines.append(f"{spec.module}(" + ", ".join(call_args) + ");")
    return "\n".join(lines) + "\n"


def prompt_block(matched: list[PartSpec], requirement: str = "") -> str:
    """把匹配到的零件渲染成提示词块。没有命中就返回空串（保持提示词精简）。"""
    if not matched:
        return ""
    lines: list[str] = [
        "【现成零件库：能用就直接用，不要自己写这些几何】",
        "程序内置了一个**已经实测验证过**的参数化零件库。"
        "如果用户要的就是下面某个零件，请直接引用它并填写尺寸，"
        "**不要自己用 polyhedron/linear_extrude 去手写螺纹**"
        "（手写螺旋几何实测会不封闭或整块几何被丢弃，用户会拿到一个拧不上的光滑圆筒）。",
        "",
        "用法（把这两行原样放在代码里，模块名与参数名不得改动）：",
        "    use <aiforge_parts.scad>",
        "    <零件调用>",
        "",
    ]
    for spec in matched:
        lines.append(f"· {spec.name}（{spec.id}）：{spec.summary}")
        lines.append(f"  调用：{spec.usage.replace(chr(10), ' ')}")
        params = "；".join(
            f"{p.name}={p.default:g}{p.unit}（{p.label}，范围 {p.minimum:g}~{p.maximum:g}）"
            for p in spec.params
        )
        lines.append(f"  参数：{params}")
        lines.append(f"  已验证：{spec.verified}")
        for rule in spec.extra_rules:
            lines.append(f"  注意：{rule}")
        lines.append("")
    sizes = thread_sizes_in(requirement)
    if sizes:
        lines.append(f"需求里出现的口径：{', '.join(f'{s:g}mm' for s in sizes)}"
                     f"（相机滤镜螺纹统一 {COMMON_FILTER_PITCH:g}mm 螺距，不要自行改成 1.0/1.25）")
    lines.append(
        "常见滤镜口径与螺距："
        + "、".join(f"M{s:g}×{COMMON_FILTER_PITCH:g}" for s in FILTER_SIZES[:10])
        + " … 一直到 M105×0.75，全部是 0.75mm 螺距。"
    )
    lines.append(
        "除零件库调用之外的部分（例如加挂绳孔、加标识）仍按常规写法；"
        "整件必须是**一个连通的实体**，不能出现分离的小零件。"
    )
    return "\n".join(lines).strip()


__all__ = [
    "COMMON_FILTER_PITCH",
    "CONVERSION_RE",
    "FILTER_SIZES",
    "LIBRARY_NAME",
    "PARTS",
    "PartParam",
    "PartSpec",
    "extract_call",
    "library_path",
    "match",
    "prompt_block",
    "render_source",
    "thread_sizes_in",
]
