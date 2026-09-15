"""AI 3D Forge —— 变更契约与改动校验。

解决两个具体症状：

1. 「改完可能不会改善」—— 因为以前只是把整个文件丢回去说「保持原有结构」，
   模型只好重写一遍，很容易顺手改掉别的东西，于是这处好了那处坏了。
   现在先产出**变更契约**：本次只允许改哪些参数、哪些代码区块必须保持不变。

2. 「改完看起来一样」—— 因为没有校验。现在改完会做三层核对：
     a) 请求的改动是否真的生效（数值确实变了 / 结构确实加了）
     b) 是否发生了**未授权改动**（用户没提的参数被改了）
     c) 设计规格是否仍被满足（用户之前指定的硬指标没有被破坏）

   不达标就带着**具体差异**重做一次；仍不达标就如实报告，不假装成功。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from . import scad as scad_src
from .design import (
    COMPOUND_PREFIX,
    EXACT_PREFIX,
    SPEC_ALIASES,
    DesignSpec,
    SpecItem,
    extract_numbers,
    iter_mm_numbers,
    normalize_name,
)

# 代码里记录规格指纹的注释格式（写入时用，校验时读回）
FINGERPRINT_RE = re.compile(r"//\s*@spec\s+([0-9a-f]{6,32})", re.IGNORECASE)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 变更契约
# --------------------------------------------------------------------------
@dataclass
class ChangeContract:
    """本次修改的授权范围。"""

    instruction: str
    # 明确要改的规格名（用户提到了它们，而且给了新的目标值）
    changed: list[str] = field(default_factory=list)
    # 必须保持不变的规格名（用户指定过、但本次没提）
    unchanged: list[str] = field(default_factory=list)
    # 用户**明确说了别动**的项（「高度不变」）。
    # 为什么要单独列出来：这类项以前会被算进 `changed`（因为句子提到了它），
    # 于是 AI 被授权去改一个用户明确要求冻结的参数 —— 方向恰好相反。
    frozen: list[str] = field(default_factory=list)
    # 用户说不要动/不要出现的东西
    prohibited: list[str] = field(default_factory=list)
    # 目标值（能从指令里解析出来时）
    target_values: dict[str, float] = field(default_factory=dict)

    def describe(self) -> str:
        lines = [f"本次只允许修改：{'、'.join(self.changed) if self.changed else '（未识别到具体项，请只按用户原话最小改动）'}"]
        if self.frozen:
            lines.append("用户明确要求不要改动：")
            for name in self.frozen:
                lines.append(f"  - {name}")
        if self.unchanged:
            lines.append("以下必须逐字保持不变（用户指定过，本次没有要求改动）：")
            for name in self.unchanged:
                lines.append(f"  - {name}")
        if self.prohibited:
            lines.append("绝对不要出现：" + "、".join(self.prohibited))
        if self.target_values:
            pairs = "、".join(f"{k} = {v:g}" for k, v in self.target_values.items())
            lines.append(f"目标数值：{pairs}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instruction": self.instruction,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "frozen": self.frozen,
            "prohibited": self.prohibited,
            "target_values": self.target_values,
        }


def resolve_param_name(key: str, current_params: dict[str, float]) -> str | None:
    """把规格里的参数名解析成源码里真实存在的参数名。

    为什么必须有这一步：规格是 AI 从用户口语里提炼的（`height`），
    而源码是另一次 AI 调用生成的（`stand_height`）。两者经常对不上，
    于是「把目标值直接写回源码」这条确定性兜底会因为找不到参数而失效，
    「你要的改动没生效」就会被误报出来 —— 而实际上 AI 是对的。

    **为什么不能用「唯一命中才算」那套分层匹配**（这里踩过一次）：
    真实模型里同时存在 `stand_height`（整体高度）与 `lip_height`（前挡边高度），
    用户说「高度改成150mm」时两者都命中 `height`，分层匹配会判「有歧义」而返回
    None。E2E 里表现为：AI 明明把 `stand_height` 从 100 改到了 150，
    程序却报「要求把 height 改成 150，但代码里是未找到该参数」，
    然后把一次正确的改动整个丢掉。

    正确做法是**打分排序**，因为这里的信号天然有强弱之分：
      * 名字完全一致              → 最强
      * 归一化后与整体尺寸同名     → 很强（stand_width / 支架高度）
      * 词元前缀是「整体」语义     → 加分（stand_/total_/overall_/model_/main_/outer_）
      * 落在已知局部部件里         → 减分（lip_/pad_/rib_/slot_/screw_/hole_…）
      * 名字里还有别的尺寸词元     → 减分（`lip_height` 描述的是「挡边的高度」）
    并且**只在最高分唯一时采用**；同分则返回 None（宁可让 AI 改，也不猜错参数）。
    """
    wanted = normalize_name(key)
    if not wanted or not current_params:
        return None
    if key in current_params:
        return key

    # 中文别名索引：把「参数名里出现的每个中文别名」都登记到该参数上。
    # 中文没有词边界，「支架高度」这种标签只能这样对上 `stand_height`。
    by_alias: dict[str, list[str]] = {}
    for name in current_params:
        raw = name.lower()
        for alias in SPEC_ALIASES:
            if alias.isascii() or len(alias) < 2 or alias not in raw:
                continue
            by_alias.setdefault(alias, []).append(name)

    # 把 key 展开成「它可能对应的归一化 key 集合」：
    #   height          → {height}
    #   支架高度          → {支架高度, height}（中文别名逐个拆开）
    #
    # 只对**非 ASCII**（中文标签）做展开，而且别名至少 2 个字符。
    # 这里踩过一个很隐蔽的坑：单字母别名 `h` 是 `thickness` 的子串，
    # 于是「厚度」的 key 集合被掺进了 `height`，最后把厚度解析成了高度
    # （`thickness` → `stand_height`）。ASCII 参数名本身就是归一化后的名字，
    # 不需要也不应该再拆。
    alias_keys: set[str] = {wanted}
    for alias, canonical in SPEC_ALIASES.items():
        if len(alias) >= 2 and not alias.isascii() and alias in wanted:
            alias_keys.add(canonical)
            alias_keys.add(alias)

    scores: dict[str, int] = {}

    def bump(name: str, points: int) -> None:
        scores[name] = max(scores.get(name, 0), points)

    for name in current_params:
        raw = name.lower()
        norm = normalize_name(name)
        base = raw
        if norm.startswith(COMPOUND_PREFIX):
            base = norm[len(COMPOUND_PREFIX) :]
        elif norm.startswith(EXACT_PREFIX):
            base = norm[len(EXACT_PREFIX) :]

        tokens = [t for t in re.split(r"[_\-]+", raw) if t]
        aliases_here = by_alias.get(name, [])

        for alias_key in alias_keys:
            # 匹配必须**精确**，不能做宽松包含 —— 实测踩过：
            # `normalize_name("thickness")` 会因为别名包含规则被折叠成 `height`，
            # 于是 `thickness` 通过「包含」匹配到了 `stand_height`，
            # 把厚度改成了高度。只认「整体等价」或「是一个完整词元」。
            hit = (
                base == alias_key
                or norm == alias_key
                or alias_key in tokens
                or alias_key in aliases_here
            )
            if not hit:
                continue

            score = 50
            score += _OVERALL_PREFIX_BONUS if any(raw.startswith(p) for p in _OVERALL_PREFIXES) else 0
            score += _LOCAL_PART_PENALTY if any(raw.startswith(p) for p in _LOCAL_PART_PREFIXES) else 0
            # 「名称里还有别的尺寸词元」说明它是某个局部尺寸，不是整体尺寸
            other_dims = [t for t in tokens if t in SPEC_ALIASES and t != alias_key]
            score -= 20 * len(other_dims)
            if norm == alias_key:
                score += 20  # 归一化后完全就是整体尺寸，最可信
            bump(name, score)

    if not scores:
        return None
    best = max(scores.values())
    winners = [name for name, score in scores.items() if score == best]
    if len(winners) != 1:
        # 同分说明真的有歧义（例如两个平级的局部尺寸），交给 AI 改
        log.debug("参数名 %s 无法唯一解析，候选=%s", key, winners)
        return None
    return winners[0]


# 「整体尺寸」语义前缀：命中说明这个参数描述的是整个模型
_OVERALL_PREFIXES = (
    "stand_", "total_", "overall_", "model_", "main_", "outer_", "body_",
    "part_", "whole_", "size_", "width_", "height_", "depth_",
)

# 「局部部件」语义前缀：命中说明这个参数描述的是某个细节
_LOCAL_PART_PREFIXES = (
    "lip_", "pad_", "rib_", "slot_", "screw_", "hole_", "foot_", "feet_",
    "peg_", "pin_", "tab_", "clip_", "hook_", "arm_", "leg_", "wall_",
    "base_", "back_", "front_", "side_", "top_", "bottom_", "inner_",
    "phone_", "cable_", "edge_", "corner_", "fillet_", "chamfer_",
)
_OVERALL_PREFIX_BONUS = 35
_LOCAL_PART_PENALTY = -25


# 表示「保持不变」的说法。用户明确说了「XX 不变」时，句子里别的数字绝不能
# 落到 XX 上 —— 实测踩过：「高度不变，宽度改成90mm」被解析成 height=90，
# 也就是把用户**明确要求别动**的那个参数改掉了。
_UNCHANGED_MARKERS = (
    "不变", "保持不变", "别改", "不要改", "不动", "别动", "不要动",
    "维持", "原样", "不用改", "无需改", "不用动", "保持",
)
# 表示「改小 / 缩短」这类**相对**修改的说法：后面的数字是增量，不是目标值
_SHRINK_WORDS = ("改小", "减小", "缩短", "变矮", "降低", "减少", "调小", "小一点", "矮一点", "短一点")
# 表示「改大 / 加长」这类相对修改
_GROW_WORDS = ("改大", "增大", "加长", "变高", "提高", "增加", "调大", "大一点", "高一点", "长一点")

# 子句分隔符：一句话里改多个参数时靠它把「哪个数字归哪个参数」切开
_CLAUSE_SPLIT_RE = re.compile(r"[，,。；;、\n]|然后|并且|同时|另外")


def _find_current_value(
    key: str,
    display: str,
    current_params: dict[str, float],
) -> float | None:
    """取某个参数在源码里的**当前值**，用于相对修改（「改小 20mm」）。"""
    resolved = resolve_param_name(display, current_params) or key
    if resolved in current_params:
        return current_params[resolved]
    for name, value in current_params.items():
        if normalize_name(name) == key:
            return value
    return None


def build_change_contract(instruction: str, spec: DesignSpec | None, current_params: dict[str, float]) -> ChangeContract:
    """把一句修改指令变成明确的授权范围。

    识别方式：指令里出现的规格名（含中文同义词）即视为「要改的」；
    规格里被用户指定过、但指令没提到的，全部列为「必须保持不变」。
    数值按**子句**就近归属（「高度改成150mm，宽度改成120mm」→ 两个目标值）。

    这里有三处曾经出错、现在必须守住的地方（都来自实测）：
      1. 用户说「XX 不变」时，别的数字不能落到 XX 上；
      2. 一句话改多个参数时，每个数字要各自归属，不能只取一个；
      3. 「改小 20mm」是**相对**修改，目标值应是 当前值−20，而不是 20。
    """
    contract = ChangeContract(instruction=instruction)
    if spec is not None:
        contract.prohibited = list(spec.prohibitions)

    text = instruction or ""
    # 中文没有词边界，「支架高度」这类复合标签必须靠**子串包含**才能对上
    # 「高度改成150mm」这种口语。但直接对整个句子做归一化是不行的：
    # 句子里的中文会被当成一个复合名（`x_高度改成150mm`），于是任何别名都
    # 不是它的子串。这里单独抽出句中的中文片段，把最长的那个也纳入比较。
    chinese_runs = re.findall(r"[\u4e00-\u9fff]+", text)
    text_norm = normalize_name(text)
    run_norms = [normalize_name(run) for run in chinese_runs]
    text_variants = [text_norm, *run_norms]

    # 1) 名称匹配：指令里提到哪个规格名，并记下**出现在哪个子句**
    clauses = [part for part in _CLAUSE_SPLIT_RE.split(text) if part.strip()]
    candidates: list[tuple[str, SpecItem | None]] = []
    if spec is not None:
        for item in spec.specs:
            candidates.append((item.name, item))
            if item.label:
                candidates.append((item.label, item))
    for name in current_params:
        candidates.append((name, None))

    hit_clauses: dict[str, list[int]] = {}
    hit_names: dict[str, list[str]] = {}  # 归一名 → 实际写进 changed 的名字

    for raw_name, item in candidates:
        key = normalize_name(raw_name)
        if not key:
            continue
        display = item.name if item else raw_name

        def name_in(fragment: str) -> bool:
            if raw_name and len(raw_name) >= 2 and raw_name in fragment:
                return True
            if len(key) >= 2 and key in normalize_name(fragment):
                return True
            # 「支架高度」这类复合标签：片段里只说了「高度」也算命中
            return any(
                alias in fragment and normalize_name(alias) == key
                for alias in SPEC_ALIASES
                if len(alias) >= 2 and not alias.isascii()
            )

        where: list[int] = []
        for index, clause in enumerate(clauses):
            if name_in(clause):
                where.append(index)
        if not where and name_in(text):
            where = list(range(len(clauses)))
        if where:
            hit_clauses.setdefault(display, where)
            hit_names.setdefault(key, []).append(display)

    changed: list[str] = []
    for names in hit_names.values():
        changed.extend(names)

    # 2) 数值：按子句归属；**被显式声明「不变」的参数记为 frozen 并从授权名单里剔除**。
    #    实测踩过：「高度不变，宽度改成90mm」里 height 因为出现在句子里而被算进
    #    `changed`，于是 AI 被授权去改一个用户明确要求冻结的参数 —— 方向恰好相反。
    frozen: list[str] = []
    # 位置 + 毫米值 一起取：位置用来判子句归属，值已经按单位换算过。
    # 旧实现是 extract_numbers() 取完值再 text.find() 找回位置，两个坑：
    #   * 相同数字（「宽90，深90」）永远定位到第一处 → 只改一个参数，另一个静默丢失
    #   * 没有单位概念（「15cm」当成 15mm）→ 生成小 10 倍的模型还报成功
    measured = iter_mm_numbers(text)
    numbers = [value for _, value in measured]
    numbers_by_clause: dict[int, list[float]] = {}
    for pos, value in measured:
        index = 0
        consumed = 0
        for i, clause in enumerate(clauses):
            consumed += len(clause)
            if pos <= consumed + i:
                index = i
                break
            index = i
        numbers_by_clause.setdefault(index, []).append(value)

    for key, names in hit_names.items():
        display = names[0]
        for index in hit_clauses.get(display, []):
            clause = clauses[index] if index < len(clauses) else text
            if any(marker in clause for marker in _UNCHANGED_MARKERS):
                # 这个子句在说「别动它」：既不给目标值，也不授予改动权限
                if display not in frozen:
                    frozen.append(display)
                continue
            values = numbers_by_clause.get(index) or []
            if not values and len(numbers) == 1:
                values = numbers
            if not values:
                continue
            value = values[0]
            # 相对修改：「高度改小 20mm」= 当前值 − 20
            current = _find_current_value(key, display, current_params)
            if current is not None:
                if any(word in clause for word in _SHRINK_WORDS):
                    value = max(0.1, current - value)
                elif any(word in clause for word in _GROW_WORDS):
                    value = current + value
            target_name = resolve_param_name(display, current_params) or key
            contract.target_values[target_name] = value
            break  # 一个参数只取第一处命中，避免被后续提及覆盖

    # 只给「真的拿到了目标值」的参数授权；点名但没说数值的留在 changed 里
    # （表示用户在意它），由 AI 按原话处理。
    contract.frozen = frozen
    frozen_keys = {normalize_name(name) for name in frozen}
    changed = [name for name in changed if normalize_name(name) not in frozen_keys]

    # 3) 去重，并算出「必须保持不变」的集合
    deduped: list[str] = []
    for name in changed:
        if name and name not in deduped:
            deduped.append(name)
    # 「改的是什么」必须落到**源码里真实存在的参数名**上。
    #
    # 实测坑：规格里叫 `height`，AI 生成的源码里叫 `stand_height`。
    # 若 `changed` 只记规格名，`_apply_targets()` 就找不到可写的参数、
    # 确定性兜底整条失效 —— 用户看到的仍然是「AI 两次都没改干净」。
    # 因此这里把第一个能解析到现有参数的名字也补进 changed。
    changed_keys = {normalize_name(n) for n in deduped}
    for raw_name in list(changed):
        resolved = resolve_param_name(raw_name, current_params)
        if resolved and normalize_name(resolved) not in changed_keys:
            deduped.append(resolved)
            changed_keys.add(normalize_name(resolved))
            break

    contract.changed = deduped

    if spec is not None:
        contract.unchanged = [
            item.name
            for item in spec.user_specified_items()
            if normalize_name(item.name) not in changed_keys
        ]
    # 没被点名的其它现有参数也应当保持不动
    for name in current_params:
        if normalize_name(name) in changed_keys:
            continue
        if name not in contract.unchanged:
            contract.unchanged.append(name)
    # 「我明确说了别动」的项已经在 frozen 里单独说过一次，不再重复列进
    # unchanged —— 同一件事在提示词里说两遍，模型反而更容易记混主次。
    frozen_norm = {normalize_name(name) for name in contract.frozen}
    contract.unchanged = [
        name for name in contract.unchanged if normalize_name(name) not in frozen_norm
    ]

    return contract


def modify_prompt(
    current_scad: str,
    contract: ChangeContract,
    spec: DesignSpec | None,
    printer_block: str | None = None,
) -> str:
    """构造「最小改动」的修改提示词。

    与旧版的差别：旧版只是说「保持原有结构」，模型无从判断什么算「原有结构」，
    于是经常顺手重写。现在给出明确的允许/禁止清单与目标数值。
    """
    parts: list[str] = []
    if printer_block:
        parts.append(f"目标打印机与工艺约束：\n{printer_block}")
    if spec is not None and not spec.is_empty():
        parts.append(f"该设计的既定规格（必须继续满足）：\n{spec.to_prompt()}")

    parts.append(
        "下面是**当前版本**的 OpenSCAD 代码（这是唯一允许改动的文件，"
        "请在它上面做最小修改）：\n\n```openscad\n" + _trim(current_scad) + "\n```"
    )
    parts.append(f"用户这次的修改要求：{contract.instruction.strip()}")
    parts.append("变更契约（严格遵守）：\n" + contract.describe())
    parts.append(
        "修改规则：\n"
        "1. 只做用户这次要求的改动，其他一律不动 —— 包括模块名、参数名、注释、"
        "整体结构、以及用户没有提到的任何数值。\n"
        "2. 未在契约里列出的参数，其**默认值必须与当前版本逐字相同**。\n"
        "3. 不要美化、不要重构、不要改注释文字、不要调整代码顺序。\n"
        "4. 如果改动需要新增参数，新参数名要语义清晰，并在 @param 注释里补上。\n"
        "5. 输出完整代码（不是 diff），但内容必须与当前版本高度一致，只有授权处不同。\n"
        "6. 保留文件顶部的 // @spec 指纹行（如存在）——程序用它核对设计一致性。"
    )
    return "\n\n".join(parts)


def initial_prompt(requirement: str, spec: DesignSpec, printer_block: str | None = None) -> str:
    """构造「按规格设计」的生成提示词。"""
    parts: list[str] = []
    if printer_block:
        parts.append(f"目标打印机与工艺约束：\n{printer_block}")
    # 命中固定零件库时，先给出「现成零件」块：螺纹等功能几何不要现写
    from .llm import _parts_block_for

    parts_spec_block = _parts_block_for(requirement, spec.to_prompt())
    if parts_spec_block:
        parts.append(parts_spec_block)
    parts.append(
        "这是一份已经和用户确认过的设计规格，请严格按它设计，"
        "不要改变其中【用户指定】的数值，也不要引入被排除的结构：\n\n"
        + spec.to_prompt()
    )
    parts.append(f"用户的原始描述（供参考语气与细节）：{requirement.strip()}")
    parts.append(
        "设计规则：\n"
        "1. 规格里【用户指定】的尺寸必须原样使用，不要四舍五入、不要「优化成整数」。\n"
        "2. 规格里 value 为 null 的项，由你按该品类的合理工程惯例取值，"
        "并在 @param 注释里说明取值依据。\n"
        "3. 规格里列出的每条 features 都必须实现；每条 prohibitions 都不得出现。\n"
        "4. 尺寸规格要能逐一对应到代码顶层的参数赋值（参数名与规格 name 一致），"
        "这样用户后续可以只调参数。\n"
        f"5. 在文件顶部注释里写入这行指纹：// @spec {spec.fingerprint()}\n"
        "6. **不要交半成品**：不许出现 TODO / 占位符 / 把某处留空让用户自己补；\n"
        "   规格里所有 null 的项和 open_questions 提到的点，都要给出具体取值并真正实现出来。\n"
        "7. **该有的结构一个都不能少**：这个品类实际使用中必需的细节（加强筋、倒角、"
        "   防滑、装配间隙、壁厚、装配孔等）要主动补全，而不是只做一个外形的空壳。\n"
        "   判定标准：把这份代码交给别人打印，他拿到的应当是一件能直接用的成品，"
        "   而不是「还需要再补几处」的草模。\n"
        "8. **必须是一整件连通的实体**：所有结构都要真正相交、连成一体。"
        "绝对不许把「一个成品」做成「几个各自独立的零件」——\n"
        "   例如用户要「能夹在桌沿的垃圾桶」，你要做的是**一个**连通壳体"
        "（夹持口与桶身是同一个壳体的两部分），"
        "而不是「一个夹子模型 + 一个普通垃圾桶模型」两个互不相连的东西。\n"
        "   自检方法：在脑子里对每一对结构问一句「它们相交吗」；"
        "任何一处只靠「摆得近」而不相交，都是错的。\n"
        "9. **面要有细节，不要只有直棱直角**：优先用圆弧、锥度、圆角（fillet/round）、"
        "   倒角（chamfer）收边；外形可带渐变收分、扇形展开、非对称轮廓。\n"
        "   曲面的 $fn 至少 64（更细的喷嘴用 96+），不要出现肉眼可见的棱面。\n"
        "10. **加一层看得见的高频细节**，让成品有「设计感」而不是「练习作」："
        "竖向加强筋、齿纹/防滑纹、镂空阵列、渐变开孔、分层台阶都算。\n"
        "    但细节必须服务于功能或结构强度，不要堆无意义的装饰。\n"
        "11. **凡是容器/壳体类零件，掏空必须是最后一步**：\n"
        "    正确顺序 = 先把外形和所有外壁特征（加强筋、凸台、把手、卡爪）union 成一个实心外坯，"
        "再统一 `difference()` 掉内腔。\n"
        "    错误顺序 = 先把壳体掏空、再 union 外部特征 —— 特征会**从外壁穿进腔体内部**，"
        "桶里平白长出一块挡板。实测翻车案例就是这样：生成的垃圾桶内壁上伸出了 4 块隔板。\n"
        "    为什么正确顺序一定安全：最后掏空时布尔运算会自动把伸进腔体的部分切掉，"
        "特征正好停在内壁面上，严丝合缝。\n"
        "12. **特征的位置要贴合真实外形**：壳体不是圆的（矩形、扇形、椭圆）时，"
        "外壁到中轴的距离**随角度变化**，绝不能用「固定半径 + rotate 转一圈」来摆加强筋 ——"
        "那样只有少数几根贴住壁，其余全飘进腔体。\n"
        "    自检：对每个外部特征问一句「它在每一条边上都真的碰到外壁了吗」；"
        "拿不准就用第 11 条的顺序，让布尔运算替你收拾。\n"
        "13. **形体本身要有设计**：不要拿一个 cube 或 cylinder 当主体交差。"
        "截面应当有变化（下小上大、带收分或外扩、圆角过渡、非对称轮廓），"
        "用「两层轮廓 + hull」或 `linear_extrude(scale=)` 做出渐变，外露棱边都要倒圆角或倒角。\n"
        "14. **凡是贴在表面上的特征，一律要往主体里埋 0.5~1mm**"
        "（齿纹、凸点、卡槽、加强筋、把手都算）：\n"
        "    定位坐标要取「表面位置 − 埋入量」，绝不要正好等于表面坐标。\n"
        "    实测过：用 `for` 循环阵列出来的防滑齿纹，只要每颗齿的外表面正好落在"
        "主体表面上，导出的模型就会变成「1 个主体 + 几十个各自独立的小齿」——"
        "差 0.01mm 就是两个零件，打印出来是一堆散件。\n"
        "    自检：对每个特征问一句「它和主体有多少体积是重叠的」；答不上来就把它往里推 1mm。\n"
        "15. **贴面特征首选「画进 2D 截面」，不要用一颗颗 3D 小方块去摆**：\n"
        "    齿纹、加强筋、卡爪这类贴在外表面上的结构，最可靠的画法是先在 2D 里"
        "把它们和截面 `union()` 成**一个完整轮廓**，再统一 `linear_extrude()` / `hull()` 成体 ——\n"
        "    这样它们天生就是同一块料，不可能散开。\n"
        "    例如外壁齿纹：`linear_extrude(h) union(){ circle(r=20);"
        " for (a=[0:15:345]) rotate([0,0,a]) translate([18,0]) square([4,2]); }`，"
        "而不是在 3D 里 `for` 循环摆 24 个小方块贴到圆柱面上。\n"
        "    实测教训：用 3D 小方块阵列贴表面的写法，6 个真实复杂实例里有 4 个"
        "导出成「1 个主体 + 几十个散件」，而且让 AI 修也修不回来 —— 必须一开始就别这么画。"
    )
    return "\n\n".join(parts)


def _trim(text: str, limit: int = 14_000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n// …（代码过长已截断）"


def embed_fingerprint(source: str, spec: DesignSpec | None) -> str:
    """确保源码顶部带有规格指纹（已存在则替换）。"""
    if spec is None or spec.is_empty():
        return source
    fingerprint = spec.fingerprint()
    line = f"// @spec {fingerprint}"
    if FINGERPRINT_RE.search(source):
        return FINGERPRINT_RE.sub(line, source, count=1)
    # 插到最前面（元数据注释之前也无妨，程序按前缀解析）
    return f"{line}\n{source}"


def read_fingerprint(source: str) -> str | None:
    match = FINGERPRINT_RE.search(source or "")
    return match.group(1).lower() if match else None


# --------------------------------------------------------------------------
# 改动校验
# --------------------------------------------------------------------------
@dataclass
class ChangeVerdict:
    """改动的核对结论。"""

    ok: bool
    performed: bool = True          # 用户要求的改动是否生效
    unauthorized: list[str] = field(default_factory=list)  # 未授权改动的参数
    spec_intact: bool = True        # 规格是否仍被满足
    fingerprint_kept: bool = True
    reasons: list[str] = field(default_factory=list)
    # 目标值没达成的相对偏离（参数名 → 偏离比例）。用于在多个「都不达标」的
    # 版本之间比高下：改到 140（目标 150）应当优于完全没改。
    target_misses: dict[str, float] = field(default_factory=dict)
    # 参数值在几何上根本站不住（例如 height=150 但模型总高只有 38mm）。
    # 这类「参数写对了、跟几何完全脱节」的改动必须单独标记：
    # 否则契约核对会通过，用户却拿到一个完全不对的模型。
    implausible: list[str] = field(default_factory=list)
    # AI 复核（第二道校验）的结论。程序化核对只能比参数值，
    # 「参数对了但结构被改坏」只能靠这一道看出来。
    ai_summary: str | None = None
    ai_concerns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "performed": self.performed,
            "unauthorized": self.unauthorized,
            "spec_intact": self.spec_intact,
            "fingerprint_kept": self.fingerprint_kept,
            "reasons": self.reasons,
            "target_misses": self.target_misses,
            "implausible": self.implausible,
            "ai_summary": self.ai_summary,
            "ai_concerns": self.ai_concerns,
        }

    def describe(self) -> str:
        return "；".join(self.reasons) if self.reasons else "改动符合契约。"

    def penalty(self) -> float:
        """违规代价（越小越好），用于在多个不达标版本之间取舍。

        为什么必须分档而不是二元判定：如果只判「达标 / 不达标」，
        「改到了 140（目标 150，且没破坏别处）」与「完全没改」代价相同，
        取舍逻辑就会错误地选后者 —— 用户的要求永远得不到推进。
        """
        score = 0.0
        if not self.spec_intact:
            score += 6.0            # 破坏用户此前指定的指标：最严重
        if not self.performed:
            score += 3.0            # 该改的完全没改
        # 顺手改坏别处。权重必须**高于** performed(3.0)：实测踩过这个坑 ——
        # AI 把高度从 100 改到 150（+3.0 的收益），却把 pad_dia 从 8 改成 80，
        # 按 2.0/项 算总代价 2.0 < 3.0，于是程序宁愿接受「改坏别处的版本」，
        # 也不接受「高度没改但其它都完好」。用户看到的正是「越改越歪」。
        score += 4.0 * len(self.unauthorized)
        # 达标的参数在几何上站不住：比「没改到位」更糟（用户拿到的是错的模型）
        score += 5.0 * len(self.implausible)
        # AI 复核提出的结构性问题同样是「改坏了」，但权重略低于程序化证据：
        # AI 会误报，不能让它一个人的判断压过确定性结论。
        score += 3.0 * len(self.ai_concerns)
        if not self.fingerprint_kept:
            score += 1.0
        score += sum(self.target_misses.values())   # 离目标值有多远
        return score


def verify_change(
    before_scad: str,
    after_scad: str,
    contract: ChangeContract,
    spec: DesignSpec | None,
) -> ChangeVerdict:
    """核对一次修改是否「只改了该改的」。"""
    verdict = ChangeVerdict(ok=True)

    before_params = scad_src.extract_top_level_numbers(before_scad)
    after_params = scad_src.extract_top_level_numbers(after_scad)

    changed_keys = {normalize_name(n) for n in contract.changed}
    target_keys = set(contract.target_values)

    # --- a) 请求的改动是否生效 ---------------------------------------
    if contract.changed:
        effective = False
        for name in contract.changed:
            key = normalize_name(name)
            # 「该改的那个参数」在源码里的真实名字：规格叫 height、源码可能叫
            # stand_height，必须解析后再取前后值，否则会误判成「未找到该参数」。
            resolved = (
                name
                if name in before_params or name in after_params
                else (
                    resolve_param_name(name, after_params)
                    or resolve_param_name(name, before_params)
                    or name
                )
            )
            look_key = normalize_name(resolved)
            before_value = (
                before_params.get(resolved)
                if resolved in before_params
                else _lookup(before_params, look_key)
            )
            after_value = (
                after_params.get(resolved)
                if resolved in after_params
                else _lookup(after_params, look_key)
            )
            target = contract.target_values.get(key)
            if target is None:
                for raw_name, value in contract.target_values.items():
                    if normalize_name(raw_name) == key:
                        target = value
                        break
            if target is not None:
                if after_value is not None and abs(after_value - target) <= max(
                    0.01, abs(target) * 0.005
                ):
                    effective = True
                else:
                    # 记录偏离比例，供取舍逻辑判断「哪个版本更接近要求」
                    scale = max(abs(target), 1.0)
                    actual = after_value if after_value is not None else 0.0
                    verdict.target_misses[key] = min(2.0, abs(actual - target) / scale)
                    verdict.reasons.append(
                        f"要求把 {name} 改成 {target:g}，但代码里是 "
                        f"{'未找到该参数' if after_value is None else f'{after_value:g}'}"
                    )
            elif before_value != after_value:
                effective = True
            elif _structure_changed(before_scad, after_scad):
                # 结构类改动（加孔、加圆角）不体现在参数上
                effective = True
        # 只有明确要求了但没做到，才算失败；结构类改动宽松放行
        if not effective and target_keys:
            verdict.performed = False
            verdict.ok = False
        elif not effective and not target_keys and not _structure_changed(before_scad, after_scad):
            verdict.performed = False
            verdict.reasons.append("改动前后代码实质相同，用户要求的变化可能没有生效")
            verdict.ok = False
    else:
        # 契约里**没有任何被点名的参数**（用户只说了结构性改动，或指令没解析出
        # 参数名）。这种情况以前直接跳过核对、判「通过」，于是「什么都没改」
        # 也会报成功 —— 正是最该避免的「假装成功」。
        # 这里的处理原则：能证明没改就如实说，但不能因此判失败，
        # 因为结构类改动（加个孔）本来就无法用参数证明；交给 AI 复核兜底。
        if not _structure_changed(before_scad, after_scad):
            verdict.performed = False
            verdict.reasons.append(
                "这次没有再点名任何尺寸，而代码前后完全一样 —— 无法确认改动已经生效"
            )

    # --- b) 未授权改动 -------------------------------------------------
    for name, before_value in before_params.items():
        key = normalize_name(name)
        if key in changed_keys:
            continue
        after_value = _lookup(after_params, key)
        if after_value is None:
            verdict.unauthorized.append(f"{name}（被删除）")
            continue
        if abs(after_value - before_value) > max(0.001, abs(before_value) * 1e-6):
            verdict.unauthorized.append(f"{name}（{before_value:g} → {after_value:g}）")
    # 新增参数不算未授权（实现新功能常需要新参数）
    if verdict.unauthorized:
        verdict.ok = False
        verdict.reasons.append("以下参数不该改动：" + "、".join(verdict.unauthorized[:6]))

    # --- b2) 用户**点名要求别动**的项，动了就是明确的违背 -----------------
    #     单独报出来，是因为「你说别动它，它还是动了」比一般越权更刺眼，
    #     混在 unauthorized 长列表里用户看不出来。
    frozen_hits: list[str] = []
    for raw in contract.frozen:
        key = normalize_name(raw)
        resolved = resolve_param_name(raw, after_params) or resolve_param_name(raw, before_params)
        before_value = (
            before_params.get(resolved)
            if resolved and resolved in before_params
            else _lookup(before_params, key)
        )
        after_value = (
            after_params.get(resolved)
            if resolved and resolved in after_params
            else _lookup(after_params, key)
        )
        if before_value is None or after_value is None:
            continue
        if abs(after_value - before_value) > max(0.001, abs(before_value) * 1e-6):
            frozen_hits.append(f"{raw}（{before_value:g} → {after_value:g}）")
    if frozen_hits:
        verdict.ok = False
        verdict.reasons.append(
            "你明确要求不要改动的项被改了：" + "、".join(frozen_hits[:4])
        )

    # --- c) 规格是否仍被满足 -------------------------------------------
    if spec is not None:
        broken: list[str] = []
        for item in spec.user_specified_items():
            key = normalize_name(item.name)
            if key in changed_keys:
                continue  # 本次授权改动的项不参与这项检查
            after_value = _lookup(after_params, key)
            if after_value is None:
                continue
            if item.value is not None and abs(after_value - item.value) > max(0.01, abs(item.value) * 0.005):
                broken.append(f"{item.label}应为 {item.value:g}{item.unit}，实际 {after_value:g}")
        if broken:
            verdict.spec_intact = False
            verdict.ok = False
            verdict.reasons.append("破坏了你之前指定的指标：" + "；".join(broken[:4]))

    # --- d) 指纹是否保留 -----------------------------------------------
    before_fp = read_fingerprint(before_scad)
    after_fp = read_fingerprint(after_scad)
    if before_fp and after_fp and before_fp != after_fp:
        # 指纹变化本身不一定错（用户改了尺寸，指纹就该变），只记录
        verdict.reasons.append("规格指纹已更新（因为你改了指定尺寸，属预期）")
    elif before_fp and not after_fp:
        verdict.fingerprint_kept = False
        verdict.reasons.append("代码里的规格指纹被删除了")

    if verdict.ok and not verdict.reasons:
        verdict.reasons.append("只改动了授权内容，其它参数与规格均保持一致")
    return verdict


def _lookup(params: dict[str, float], key: str) -> float | None:
    for name, value in params.items():
        if normalize_name(name) == key:
            return value
    return None


def _structure_changed(before: str, after: str) -> bool:
    """粗判结构是否变化：比较去掉数值与空白后的形状。"""
    return _shape(before) != _shape(after)


def _shape(source: str) -> str:
    stripped = scad_src.strip_comments(source)
    stripped = re.sub(r"-?\d+(?:\.\d+)?", "#", stripped)
    return re.sub(r"\s+", "", stripped)


def repair_prompt(original_scad: str, bad_scad: str, verdict: ChangeVerdict, contract: ChangeContract) -> str:
    """把校验失败的具体差异回灌给模型，让它带着明确目标重做。"""
    return (
        "你上一次的修改不满足要求，请重做。\n\n"
        "上一次的代码：\n```openscad\n" + _trim(bad_scad) + "\n```\n\n"
        "必须改回成的**当前版本**（这是唯一正确的基准）：\n```openscad\n"
        + _trim(original_scad)
        + "\n```\n\n"
        f"问题所在：{verdict.describe()}\n\n"
        f"变更契约：\n{contract.describe()}\n\n"
        "请重新输出完整代码：只做契约允许的改动，其余部分必须与基准完全一致。"
    )


def summarize_verdict(verdict: ChangeVerdict) -> str:
    """给界面用的一句话结论。"""
    if verdict.ok:
        return "已按要求修改，其它部分保持不变。"
    problems: list[str] = []
    if not verdict.performed:
        problems.append("你要的改动没有真正生效")
    if verdict.unauthorized:
        problems.append("有未要求的参数被改动：" + "、".join(verdict.unauthorized[:4]))
    if not verdict.spec_intact:
        problems.append("破坏了你之前指定的尺寸")
    if not verdict.fingerprint_kept:
        problems.append("设计指纹被删除")
    return "修改未完全达标：" + "；".join(problems) if problems else "修改未完全达标。"
