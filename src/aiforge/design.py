"""AI 3D Forge —— 设计规格（DesignSpec）。

解决的核心问题：以前每一次生成都是「把用户的一句散文丢给模型，让它自由发挥」，
改动时也只说「保持原有结构」——没有契约、没有记忆、没有验收判据。
结果是模型方向一开始就偏，而且越改越歪。

现在的做法：先把用户的描述解析成**结构化规格**，它是一份可复核的设计契约：

    kind          品类（例如「桌面手机支架」）
    summary       一句话设计说明
    specs         带数值的尺寸表：名称 / 值 / 单位 / 公差 / 是否用户明确指定
    features      必须存在的结构（例如「背面有充电线穿孔」）
    prohibitions  禁止项（用户说过「不要底座」就记在这里，永不违反）
    acceptance    验收判据（可被程序或评审逐条核对）

要点：
  * **只提取用户说过的**，绝不自行发明尺寸。用户没说的进 open_questions。
  * 规格持久化到项目，跨会话累积：用户后续说的约束会合并进来，不再丢失。
  * 规格带指纹（fingerprint），写进生成的 SCAD 注释里，
    这样修改时能核对「改完还是同一个设计吗」。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

SPEC_VERSION = 1

# 尺寸名称的常见同义词 → 归一化名称。用于「改高度」时能找到对应参数，
# 以及判断某次修改是否真的动了它应该动的东西。
SPEC_ALIASES: dict[str, str] = {
    "height": "height", "h": "height", "高度": "height", "总高": "height", "高": "height",
    "width": "width", "w": "width", "宽度": "width", "宽": "width",
    "depth": "depth", "d": "depth", "深度": "depth", "深": "depth", "厚度": "depth",
    "length": "length", "l": "length", "长度": "length", "长": "length",
    "diameter": "diameter", "dia": "diameter", "直径": "diameter",
    "radius": "radius", "r": "radius", "半径": "radius",
    "thickness": "thickness", "wall": "wall", "壁厚": "wall", "壁": "wall",
    "angle": "angle", "角度": "angle", "倾角": "angle",
    "hole": "hole_diameter", "孔径": "hole_diameter", "孔": "hole_diameter",
    "fillet": "fillet", "圆角": "fillet", "倒角": "chamfer", "chamfer": "chamfer",
}

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

# 长度单位 → 毫米。**只有单位紧跟在数字后面时才换算**，
# 这样「加 3 个孔」「45 度」「2 层墙」里的数字不会被误乘。
# 顺序很关键：必须把更长的候选放在前面，否则 "15mm" 会先匹配到 "m"
# 而变成 15000mm。ASCII 单位后面还要排除字母（"3 in the box" 不是英寸）。
_LENGTH_UNIT_FACTORS: dict[str, float] = {
    "毫米": 1.0,
    "厘米": 10.0,
    "公分": 10.0,
    "英寸": 25.4,
    "inch": 25.4,
    "inches": 25.4,
    "mm": 1.0,
    "cm": 10.0,
    "in": 25.4,
    "米": 1000.0,
    "m": 1000.0,
    '"': 25.4,
    "″": 25.4,
}

_MM_NUMBER_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*"
    r"(毫米|厘米|公分|英寸|inches|inch|mm|cm|in|米|m|\"|″)?"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def iter_mm_numbers(text: str) -> list[tuple[int, float]]:
    """按出现顺序返回 (数字在原文里的位置, 换算成毫米后的值)。

    为什么需要「位置」：调用方要按子句归属把数值分配到对应参数上。
    旧实现是「先取出数值，再用 `text.find(str(value))` 回头找位置」——
    同一句话里出现**相同数字**时 find 永远返回第一处，
    于是「宽度改成90mm，深度改成90mm」两个 90 都被算成第一个子句的，
    结果只改了一个参数，另一个被静默丢掉（实测 4/5 用例出错）。
    边扫描边记录位置就没有这个问题：每个数字都有自己的下标。

    为什么需要「单位」：用户会说「高度改成 15cm」。
    旧实现把 15cm 当成 15mm，生成小 10 倍的模型，
    而契约核对还认为目标已达成、报成功。
    """
    found: list[tuple[int, float]] = []
    for match in _MM_NUMBER_RE.finditer(text or ""):
        try:
            value = float(match.group(1))
        except (TypeError, ValueError):
            continue
        unit = (match.group(2) or "").lower()
        factor = _LENGTH_UNIT_FACTORS.get(unit, 1.0) if unit else 1.0
        found.append((match.start(1), value * factor))
    return found


@dataclass
class SpecItem:
    """一条带数值的设计规格。"""

    name: str
    label: str
    value: float | None = None
    unit: str = "mm"
    min: float | None = None
    max: float | None = None
    tolerance: float | None = None
    # 是否由用户明确指定 —— 决定修改时它能不能被随意改动
    user_specified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SpecItem":
        name = str(raw.get("name") or "").strip()
        label = str(raw.get("label") or name).strip() or name
        return cls(
            name=name,
            label=label,
            value=_to_float(raw.get("value")),
            unit=str(raw.get("unit") or "mm").strip() or "mm",
            min=_to_float(raw.get("min")),
            max=_to_float(raw.get("max")),
            tolerance=_to_float(raw.get("tolerance")),
            user_specified=bool(raw.get("user_specified", False)),
        )

    def describe(self) -> str:
        value = "未指定" if self.value is None else f"{self.value:g}{self.unit}"
        suffix = ""
        if self.min is not None or self.max is not None:
            lo = f"{self.min:g}" if self.min is not None else "—"
            hi = f"{self.max:g}" if self.max is not None else "—"
            suffix = f"（允许 {lo}–{hi}{self.unit}）"
        if self.tolerance:
            suffix += f" 公差±{self.tolerance:g}{self.unit}"
        if self.user_specified:
            suffix += "  ← 你指定"
        return f"{self.label}：{value}{suffix}"


@dataclass
class DesignSpec:
    """一份完整的设计契约。"""

    version: int = SPEC_VERSION
    kind: str = ""
    summary: str = ""
    specs: list[SpecItem] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    prohibitions: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    # 用户后续的澄清与修改要求，累积保留 —— 这是「记忆」的载体
    clarifications: list[str] = field(default_factory=list)

    # ---------------- 序列化 ----------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "kind": self.kind,
            "summary": self.summary,
            "specs": [s.to_dict() for s in self.specs],
            "features": list(self.features),
            "prohibitions": list(self.prohibitions),
            "acceptance": list(self.acceptance),
            "open_questions": list(self.open_questions),
            "clarifications": list(self.clarifications),
            "fingerprint": self.fingerprint(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "DesignSpec":
        if not isinstance(raw, dict):
            return cls()
        items: list[SpecItem] = []
        for entry in raw.get("specs") or []:
            if isinstance(entry, dict):
                item = SpecItem.from_dict(entry)
                if item.name:
                    items.append(item)
        return cls(
            version=int(raw.get("version") or SPEC_VERSION),
            kind=str(raw.get("kind") or "").strip(),
            summary=str(raw.get("summary") or "").strip(),
            specs=items,
            features=[str(x).strip() for x in (raw.get("features") or []) if str(x).strip()],
            prohibitions=[str(x).strip() for x in (raw.get("prohibitions") or []) if str(x).strip()],
            acceptance=[str(x).strip() for x in (raw.get("acceptance") or []) if str(x).strip()],
            open_questions=[str(x).strip() for x in (raw.get("open_questions") or []) if str(x).strip()],
            clarifications=[str(x).strip() for x in (raw.get("clarifications") or []) if str(x).strip()],
        )

    # ---------------- 指纹 ----------------
    def fingerprint(self) -> str:
        """规格指纹：只由「用户明确指定的硬指标」决定。

        为什么不用全部字段：AI 解析出来的 features/acceptance 每次措辞都会变，
        把那些算进去指纹就不稳定了，也就无法用来判断「还是不是同一个设计」。
        只取用户真正指定的尺寸 + 禁止项 + 品类，才既稳定又有意义。
        """
        material = {
            "kind": normalize_kind(self.kind),
            "specs": sorted(
                f"{s.name}={s.value:g}{s.unit}"
                for s in self.specs
                if s.user_specified and s.value is not None
            ),
            "prohibitions": sorted(normalize_text(p) for p in self.prohibitions),
        }
        blob = json.dumps(material, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    # ---------------- 查询与合并 ----------------
    def is_empty(self) -> bool:
        return not (self.kind or self.specs or self.features or self.summary)

    def find(self, name: str) -> SpecItem | None:
        """按名称（含同义词与中文标签）找一条规格。"""
        wanted = normalize_name(name)
        for item in self.specs:
            if normalize_name(item.name) == wanted or normalize_name(item.label) == wanted:
                return item
        return None

    def user_specified_items(self) -> list[SpecItem]:
        return [s for s in self.specs if s.user_specified and s.value is not None]

    def merge_clarification(self, text: str) -> None:
        """记下用户的新要求/澄清，跨会话累积，避免下一轮又忘。"""
        cleaned = normalize_text(text)
        if cleaned and cleaned not in {normalize_text(c) for c in self.clarifications}:
            self.clarifications.append(text.strip())
        # 只保留最近若干条，避免提示词无限膨胀
        if len(self.clarifications) > 12:
            del self.clarifications[:-12]

    def mark_user_specified(self, names: list[str]) -> None:
        """把指定名称的规格标记为用户明确指定（用于纠偏）。"""
        wanted = {normalize_name(n) for n in names if n}
        for item in self.specs:
            if normalize_name(item.name) in wanted or normalize_name(item.label) in wanted:
                item.user_specified = True

    # ---------------- 提示词渲染 ----------------
    def to_prompt(self) -> str:
        """渲染成给 AI 看的契约文本。只包含有内容的段落。"""
        lines: list[str] = []
        if self.kind:
            lines.append(f"设计对象：{self.kind}")
        if self.summary:
            lines.append(f"一句话说明：{self.summary}")

        if self.specs:
            lines.append("尺寸规格（必须严格采用这些数值，不得自行改动）：")
            for item in self.specs:
                flag = "【用户指定】" if item.user_specified else "【建议值，可微调】"
                lines.append(f"  - {item.describe()} {flag}")

        if self.features:
            lines.append("必须包含的结构：")
            lines.extend(f"  - {f}" for f in self.features)

        if self.prohibitions:
            lines.append("绝对不要出现（用户明确排除）：")
            lines.extend(f"  - {p}" for p in self.prohibitions)

        if self.clarifications:
            lines.append("用户此前补充说明过的要求（必须一并满足，不要遗忘）：")
            lines.extend(f"  - {c}" for c in self.clarifications)

        if self.acceptance:
            lines.append("验收判据（生成后会被逐条核对）：")
            lines.extend(f"  - {a}" for a in self.acceptance)

        return "\n".join(lines)

    def to_display(self) -> dict[str, Any]:
        """给界面的展示用结构（让用户能确认/纠正 AI 的理解）。"""
        return {
            "kind": self.kind,
            "summary": self.summary,
            "specs": [
                {
                    "name": s.name,
                    "label": s.label,
                    "value": s.value,
                    "unit": s.unit,
                    "min": s.min,
                    "max": s.max,
                    "user_specified": s.user_specified,
                    "display": s.describe(),
                }
                for s in self.specs
            ],
            "features": list(self.features),
            "prohibitions": list(self.prohibitions),
            "acceptance": list(self.acceptance),
            "open_questions": list(self.open_questions),
            "clarifications": list(self.clarifications),
            "fingerprint": self.fingerprint(),
        }


# --------------------------------------------------------------------------
# 归一化工具
# --------------------------------------------------------------------------
def merge_specs(base: DesignSpec, update: DesignSpec) -> DesignSpec:
    """把一次新解析出的规格并入既有规格。

    为什么需要「合并」而不是直接替换：用户第一轮说「高度100mm」，
    第二轮只说「加个孔」——此时解析出的新规格里根本没有高度信息，
    直接替换就把「高度100mm」这个用户亲口指定的指标丢了，
    下一轮修改就失去约束（这正是以前「说过的话会忘」的根因之一）。

    合并规则：
      * 旧规格里用户指定过的项，若新规格没提到 → 保留
      * 新规格提到的项 → 用新值（用户刚说的算数）；但新值为 null 时不抹掉旧值
      * 禁止项、澄清记录 → 只增不减
      * kind / summary → 新的优先，为空则保留旧的
    """
    merged = DesignSpec(
        version=SPEC_VERSION,
        kind=update.kind or base.kind,
        summary=update.summary or base.summary,
    )

    update_by_name = {normalize_name(item.name): item for item in update.specs}
    used: set[str] = set()

    for item in base.specs:
        key = normalize_name(item.name)
        newer = update_by_name.get(key)
        if newer is None:
            merged.specs.append(item)
            continue
        used.add(key)
        if newer.value is None and item.value is not None:
            # 新解析没给出数值，不能抹掉旧值
            merged.specs.append(
                SpecItem(
                    name=newer.name or item.name,
                    label=newer.label or item.label,
                    value=item.value,
                    unit=newer.unit or item.unit,
                    min=newer.min if newer.min is not None else item.min,
                    max=newer.max if newer.max is not None else item.max,
                    tolerance=newer.tolerance if newer.tolerance is not None else item.tolerance,
                    user_specified=newer.user_specified or item.user_specified,
                )
            )
        else:
            newer.user_specified = newer.user_specified or item.user_specified
            merged.specs.append(newer)

    for item in update.specs:
        if normalize_name(item.name) not in used:
            merged.specs.append(item)

    def _union(*groups: list[str]) -> list[str]:
        seen: list[str] = []
        for group in groups:
            for entry in group:
                cleaned = (entry or "").strip()
                if cleaned and cleaned not in seen:
                    seen.append(cleaned)
        return seen

    merged.features = _union(base.features, update.features)
    merged.prohibitions = _union(base.prohibitions, update.prohibitions)
    merged.acceptance = _union(update.acceptance, base.acceptance)
    merged.clarifications = _union(base.clarifications, update.clarifications)
    merged.open_questions = list(update.open_questions or base.open_questions)
    return merged


def normalize_text(text: str) -> str:
    """用于比较的归一化：去空白、统一全角标点、小写。"""
    cleaned = (text or "").strip().lower()
    for full, half in (("（", "("), ("）", ")"), ("，", ","), ("。", "."), ("：", ":"), ("；", ";")):
        cleaned = cleaned.replace(full, half)
    return re.sub(r"\s+", "", cleaned)


_ALIASES_BY_LENGTH: list[tuple[str, str]] = sorted(
    SPEC_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True
)

# 归一化 key 的前缀：区分「整体尺寸」与「某个局部尺寸」。
# 见 normalize_name() 的说明 —— 不加前缀会让 phone_slot_width 与 width 撞在一起。
COMPOUND_PREFIX = "x_"
EXACT_PREFIX = "d_"


def normalize_name(name: str) -> str:
    """把尺寸名归一化到统一 key（含中英文同义词）。

    关键点：**复合名要保留独立性**。`phone_slot_width`（手机槽宽）与
    `width`（整体宽度）是两个不同的参数，绝不能归一化成同一个 key。

    这里给「复合匹配」出来的 key 加一个 `compound:` 前缀来区分，
    原因是一个实测踩到、后果很严重的 bug：
      * 单纯做「包含已知别名」匹配时，`phone_slot_width` 与 `width` 都变成
        `width`，`rib_thickness` / `lip_thickness` / `thickness` 都变成
        `thickness`；
      * 于是改动核对里 `_lookup()` 按 key 取值时会取到**错的参数**
        （查 `phone_slot_width` 拿到整体宽度），把没被改的参数报成
        「未授权改动」，用户看到的就是「明明只让改高度，它却说这也不对」；
      * 更糟的是「把目标值写回源码」这条确定性兜底也会因此找不到正确参数。
    加前缀后：整体尺寸 = `width`，槽宽 = `x_phone_slot_width`，互不污染。

    注意复合名保留**完整原名**（而不是折叠成别名）：`rib_thickness` 与
    `lip_thickness` 是两个不同参数，折叠成同一个 key 同样会互相污染。
    「规格叫 height、源码叫 stand_height」这种跨命名约定由
    `changes.resolve_param_name()` 的词元匹配负责，不靠归一化。
    """
    cleaned = normalize_text(name)
    cleaned = re.sub(r"[^a-z0-9_\u4e00-\u9fff]", "", cleaned)
    # 已经是归一化过的 key（带前缀）就原样返回，保证幂等
    for prefix in (COMPOUND_PREFIX, EXACT_PREFIX):
        if cleaned.startswith(prefix):
            inner = cleaned[len(prefix) :]
            if inner in SPEC_ALIASES:
                return SPEC_ALIASES[inner]
            return cleaned
    if cleaned in SPEC_ALIASES:
        # 单字母的 **ASCII** 别名（h/w/d/l/r）加前缀，避免 `header` 这类词被误判。
        # 注意只对 ASCII 单字符这么做：中文的单字别名（高/宽/深/长/孔/壁）
        # 本身就是完整词，加了前缀反而会让「高度」这种同义别名对不上。
        if len(cleaned) == 1 and cleaned.isascii():
            return EXACT_PREFIX + SPEC_ALIASES[cleaned]
        return SPEC_ALIASES[cleaned]
    # 「支架高度」「phone_slot_width」这类复合名：确认它确实含某个别名，
    # 但 key 用**完整原名**，避免与别的复合名撞车
    tokens = [part for part in re.split(r"[_\-]+", cleaned) if part]
    for part in tokens:
        if part in SPEC_ALIASES and len(part) >= 2:
            return COMPOUND_PREFIX + cleaned
    # 中文没有词边界，只能退回「包含」匹配；此时仅接受长度 >= 2 的别名
    for alias, _canonical in _ALIASES_BY_LENGTH:
        if len(alias) >= 2 and alias in cleaned:
            return COMPOUND_PREFIX + cleaned
    return cleaned


def normalize_kind(kind: str) -> str:
    return normalize_text(kind)[:40]


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def extract_numbers(text: str) -> list[float]:
    """从一段文本里取出所有数字（用于核对「用户要求的数值」是否出现在代码里）。"""
    return [float(m) for m in _NUMBER_RE.findall(text or "")]


# --------------------------------------------------------------------------
# 规格解析的提示词与响应处理
# --------------------------------------------------------------------------
SPEC_SYSTEM_PROMPT = """你是需求分析员。你的任务是把用户的一句口语化需求，整理成一份**结构化设计规格**。

铁律：
1. **只提取用户说过或明确可推断的量**。用户没说的尺寸，value 一律给 null，
   并把「需要确认什么」写进 open_questions。**绝不自行编造具体数值。**
2. 用户明确给出的数值必须原样保留（含单位），并标 user_specified = true。
3. 用户说「不要 X」「别做 Y」这类排除项，写进 prohibitions —— 后续任何版本都不得出现。
4. **必须写成「一整件」的要求**：除非用户明确要求「分件 / 可拆卸 / 多件组合」，
   否则 features 里要有一条明确写「整体为单一连通实体，各结构相互交汇」。
   用户描述里出现「能夹住 / 能挂住 / 卡在桌沿」这类**依附性**功能时，
   依附结构（夹口、挂钩、卡爪）必须与主体是**同一个壳体**，不能是独立零件 ——
   features 里要写清楚这一点，否则后续生成很容易做成两个各自独立的模型。
5. 只输出 JSON，不要 Markdown 围栏，不要解释。

JSON 结构：
{
  "kind": "设计对象的简短品类名，例如「桌面手机支架」",
  "summary": "一句话说明这个物件是做什么的（20 字内）",
  "specs": [
    {
      "name": "英文小写下划线参数名，例如 height / width / wall / angle",
      "label": "中文名称，例如 支架高度",
      "value": 数值或 null,
      "unit": "mm 或 度 或 个",
      "min": 数值或 null,
      "max": 数值或 null,
      "tolerance": 数值或 null,
      "user_specified": true/false
    }
  ],
  "features": ["必须存在的结构，逐条，例如 背面有充电线穿孔"],
  "prohibitions": ["用户明确排除的东西"],
  "acceptance": ["可以被核对通过的判据，例如 总高等于 100mm"],
  "open_questions": ["用户没说、但会影响设计的关键点，最多 2 条。没有就给空数组"]
}

注意：
- specs 里既要有用户明确指定的（user_specified=true），也可以有「这个品类必然涉及、
  但用户没说」的（value 给 null，user_specified=false），方便后续追问与设计。
- acceptance 要写成可核对的句子（含数值），不要写「美观」「结实」这类空话。
"""


def spec_user_prompt(requirement: str, previous: DesignSpec | None = None) -> str:
    """构造规格解析的用户消息。

    续改时把**上一版规格**一起给它，这样它是在已有契约上做增量，
    而不是从零重新理解 —— 这是「不忘记用户之前说过什么」的关键。
    """
    parts = [f"用户的需求：{requirement.strip()}"]
    if previous is not None and not previous.is_empty():
        parts.append(
            "这是该设计此前已经确定的规格（请在此基础上更新，"
            "不要丢弃用户之前指定过的数值与禁止项）：\n"
            + previous.to_prompt()
        )
    parts.append("请输出更新后的完整 JSON 规格。")
    return "\n\n".join(parts)


def parse_spec_response(text: str) -> DesignSpec:
    """把 AI 的回复解析成 DesignSpec；格式异常时返回空规格而不是抛错。

    宁可退化成「没有规格」（后续仍能生成），也不要因为解析失败让用户看不到模型。
    """
    raw = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    if fenced:
        raw = fenced.group(1).strip()
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end > start:
            raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return DesignSpec()
    if not isinstance(data, dict):
        return DesignSpec()
    spec = DesignSpec.from_dict(data)
    if not spec.summary and spec.kind:
        spec.summary = spec.kind
    return spec
