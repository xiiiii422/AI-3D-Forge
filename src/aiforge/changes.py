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
    # **必须跟着授权改动一起动**的参数。
    #
    # 为什么需要这一档（实测翻车现场）：用户说「把高度改成 50mm」，源码里还有
    # `rib_z1 = 88`（加强筋结束高度）。只改高度不动它，加强筋就比筒身高出 38mm,
    # 几何上根本立不住。AI 通常会顺手把 rib_z1 一起降到 44（这是**对的**），
    # 但旧逻辑把这次调整算成「未授权改动」，每条罚 4.0 分 —— 于是
    # 「高度改对了、顺手调了筋高」的版本输给「什么都没改」的版本，
    # 用户拿到的是纹丝不动的模型，界面还报成功。
    #
    # 判据很保守：只有**同一维度家族**（改高度 → 注释标签里也带「高度」）
    # 且方位词不冲突（改内径不许把外径算进来）的参数才进这个名单。
    follow: list[str] = field(default_factory=list)
    # 整体缩放的比例（"整体缩小到80%" → 0.8）；None = 不是缩放类指令。
    # 为什么要单独记：缩放是**相对**改动，它会让所有尺寸一起变 ——
    # 如果还拿规格里的旧绝对值去要求模型，改动会被判成"没做到"然后被回滚
    # （实测：「整体缩小到80%」被回滚，模型纹丝不动）。
    scale_factor: float | None = None

    def describe(self) -> str:
        lines = [f"本次只允许修改：{'、'.join(self.changed) if self.changed else '（未识别到具体项，请只按用户原话最小改动）'}"]
        if self.follow:
            lines.append(
                "为了跟这次改动保持一致，下面这些**需要一起调整**"
                "（例如总高变矮后，加强筋的结束高度必须跟着降下来）："
            )
            for name in self.follow:
                lines.append(f"  - {name}")
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
        if self.scale_factor:
            lines.append(
                f"这是**整体缩放**：所有尺寸一起变成原来的 {self.scale_factor * 100:.0f}%"
                "（长宽高按同一比例，不要只改某一项）"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instruction": self.instruction,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "frozen": self.frozen,
            "follow": self.follow,
            "prohibited": self.prohibited,
            "target_values": self.target_values,
            "scale_factor": self.scale_factor,
        }


def resolve_param_name(
    key: str,
    current_params: dict[str, float],
    param_labels: dict[str, str] | None = None,
) -> str | None:
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

    `param_labels` 是 `// @param` 注释里的说明（`scad.extract_param_labels`）。
    给了它就**先走这座最可靠的桥**，见 `_resolve_by_label`。
    """
    wanted = normalize_name(key)
    if not wanted or not current_params:
        return None
    if key in current_params:
        return key

    bridged = _resolve_by_label(key, current_params, param_labels)
    if bridged:
        return bridged

    # 这里以前还建过一张「中文别名 → 参数名」索引（by_alias），并在下面按
    # `by_alias.get(name)` 读它。两个坑叠在一起，让它永远只能返回空：
    #   * 写入那侧只收「alias 是参数名的子串」的项，而参数名是 OpenSCAD 里的
    #     ASCII 变量名（`extract_top_level_numbers` 产出的），中文别名永远不是它的子串；
    #   * 读取那侧又拿**参数名**去查一张以**别名**为键的表，即使表非空也对不上。
    # 真正做成「支架高度 → stand_height」的是下面的 `alias_keys` 展开（把中文别名
    # 拆成归一化 key 后去碰词元），与这张表无关。实测对账：拿全仓 158 个真实参数名
    # × 34 个规格名/别名（共 50388 次调用）删前删后结果全同，且「参数名里含中文
    # 别名」的实例数为 0 —— 删它是安在的。留着的话，下一个读者会以为改这张表
    # 就能改中文名解析（上一轮的注释就是这么写的）。

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
    wanted_tokens = {t for t in re.split(r"[_\-]+", wanted) if t}
    for alias, canonical in SPEC_ALIASES.items():
        if len(alias) < 2:
            continue
        if alias in wanted_tokens:
            # **词元级**的 ASCII 别名也要展开：规格叫 `total_height`（归一化后
            # 是 `x_total_height`），源码叫 `total_h`。不展开 `height` 这个
            # 词元，两边就永远对不上 —— 实测表现为「把高度改成50mm」的目标值
            # 落在 `height` 这个名字上（源码里根本没有），
            # 于是 AI 改对了 `total_h` 也被判成"没改到"，最后回退成什么都没改。
            # 词元匹配是精确匹配，不会重蹈 `thickness` 里含 `h` 的覆辙。
            alias_keys.add(canonical)
            alias_keys.add(alias)
        elif not alias.isascii() and alias in wanted:
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

        # 方位相反的参数直接排除：`inner_diameter`（内径）绝不能落到 `outer_d`
        # （外径）上 —— 实测正是这样把「把孔的内径改成8mm」变成了「改外径」。
        if _modifier_conflict(key, name):
            continue

        tokens = [t for t in re.split(r"[_\-]+", raw) if t]

        # **结尾的单字母缩写**：`total_h` / `outer_d` / `pad_w` / `arm_l` 是
        # 工程代码里最常见的写法。单字母别名（h/w/d/l/r）在 normalize_name()
        # 里被刻意排除（否则 `header` 会被当成 height），但在「结尾词元」这个
        # 位置上它没有歧义 —— 不认它，「规格叫 height、源码叫 total_h」这种
        # 最常见的情况就解析不出来（实测：改高度整条链路静默失效）。
        # 例外：`d` 既可能是 diameter 也可能是 depth（`outer_d` vs `cavity_d`），
        # 靠名字前缀定夺，没有线索就不认。
        tail_aliases: set[str] = set()
        if tokens and len(tokens[-1]) == 1 and tokens[-1].isascii():
            letter = tokens[-1]
            if letter == "d":
                tail_aliases.update(
                    canonical for prefix, canonical in _TAIL_LETTER_PREFIXES
                    if raw.startswith(prefix)
                )
            elif letter in SPEC_ALIASES:
                tail_aliases.add(SPEC_ALIASES[letter])

        for alias_key in alias_keys:
            # 匹配必须**精确**，不能做宽松包含 —— 实测踩过：
            # `normalize_name("thickness")` 会因为别名包含规则被折叠成 `height`，
            # 于是 `thickness` 通过「包含」匹配到了 `stand_height`，
            # 把厚度改成了高度。只认「整体等价」或「是一个完整词元」。
            hit = (
                base == alias_key
                or norm == alias_key
                or alias_key in tokens
                or alias_key in tail_aliases
            )
            if not hit:
                continue

            score = 50
            score += _OVERALL_PREFIX_BONUS if any(raw.startswith(p) for p in _OVERALL_PREFIXES) else 0
            score += _LOCAL_PART_PENALTY if any(raw.startswith(p) for p in _LOCAL_PART_PREFIXES) else 0
            # 「名称里还有别的尺寸词元」说明它是某个局部尺寸，不是整体尺寸。
            # 与本次匹配同一个维度的词元不算（`total_h` 里的 `h` 就是高度本身）。
            other_dims = [
                t for t in tokens if t in SPEC_ALIASES and SPEC_ALIASES[t] != alias_key
            ]
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


# 与 `normalize_name` 同一套词表：判断一个名字/标签「说的是哪个维度」。
_CANONICAL_DIMS = set(SPEC_ALIASES.values())

# 「长度类」维度：只有这些维度的改动才谈得上「有参数必须跟着走」。
# 圆角/孔径/角度这类改动的联动关系太发散，不自动授权（宁可让 AI 按原话最小改）。
_LENGTH_DIMS = {"height", "length", "width", "depth", "diameter"}

# 方位/部位修饰词：规格名与注释标签必须**不冲突**才算同一座桥。
# 「外径」与「内径」都含「径」，靠修饰词才能区分开。
# 方位/部位词 → 统一的方向 id（跨中英文）。
# 为什么必须归一：`inner_diameter`（内径）与 `外径` 是**相反**的两个尺寸，
# 但一个写英文、一个写中文时，逐字的相反判断看不出来 —— 实测于是把
# 「把内径改成 8mm」解析到了 `outer_d`（外径），AI 顺手把外径也改了。
# 归一化后 `inner` 与 `内` 是同一个 id，相反关系一判就中。
_DIRECTION_CANON: dict[str, str] = {
    "外": "outer", "outer": "outer", "outside": "outer",
    "内": "inner", "inner": "inner", "inside": "inner",
    "上": "top", "顶": "top", "top": "top", "upper": "top",
    "下": "bottom", "底": "bottom", "bottom": "bottom", "lower": "bottom",
    "前": "front", "front": "front", "后": "back", "back": "back",
    "左": "left", "left": "left", "右": "right", "right": "right",
    "侧": "side", "side": "side", "中": "center", "middle": "center", "center": "center",
}
_DIRECTION_OPPOSITES = (
    ("outer", "inner"), ("top", "bottom"), ("front", "back"), ("left", "right"),
)
# 方位词的检索列表：长的优先（`outer` 先于 `top`，同一处不会拆错）
_DIRECTION_KEYS = sorted(_DIRECTION_CANON, key=len, reverse=True)
# 只把「自成一个词元」的方位词算成方位：ASCII 词按驼峰/下划线切开，中文单字按子串。
# 为什么不能直接子串匹配（旧写法就是子串）：`laptop_bottom_h` 里的「top」只是
# `laptop` 的中间一段，旧写法却把它当成方位词 —— 于是「底部厚度」与
# `laptop_bottom_h` 被判成方位相反，那个参数从候选里整个删掉（实测对账：
# 子串判冲突、词元判不冲突）。笔记本支架是本品类的常见型号，这不是抬杠。
# 为什么也不能只按 `[_-\s]` 切：本仓零件库里有 `NemaBackAxleLength` /
# `CenterCandleWidth` 这种**驼峰**名，切不开就把真方位词一起丢了。
_CAMEL_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+")


def _direction_words(text: str) -> set[str]:
    """文本里的方位/部位词，归一化成统一 id（外/outer → outer）。

    中英文的「词边界」规律根本不同，所以判据分两套：
      * ASCII：必须**自成词元**（`outer_d` 的 outer、`NemaBackAxleLength` 的 back）；
      * 中文：没有词边界，单字方位词（内/外/顶/底/前/后…）仍按子串认。
    """
    lowered = (text or "").lower()
    ascii_words: set[str] = set()
    for run in re.findall(r"[A-Za-z]+", text or ""):
        ascii_words.update(word.lower() for word in _CAMEL_WORD_RE.findall(run))
    found: set[str] = set()
    for word in _DIRECTION_KEYS:
        canonical = _DIRECTION_CANON[word]
        if word.isascii():
            if word in ascii_words:
                found.add(canonical)
        elif word in lowered:
            found.add(canonical)
    return found

# 「新增一个特征」的说法：这时句子里的数字描述的是**新特征自身**，
# 不能拿去当已有整体尺寸的增量。见 _adds_feature()。
_ADD_FEATURE_RE = re.compile(
    r"(增加|添加|加上|加个|加一|开个|开一|添加|添|放|装|做|加)"
    r"[^，。；,;]{0,20}?(孔|槽|筋|齿|柱|凸台|螺纹|沉孔|倒角|圆角|台阶|凹槽|缺口)"
)

# 单字母结尾缩写里 `d` 有歧义（diameter / depth）：靠名字前缀定夺，
# 没有线索就不认（宁可让 AI 改，也不把「内腔深度」对到 `outer_d` 上）。
_TAIL_LETTER_PREFIXES: tuple[tuple[str, str], ...] = (
    ("outer_", "diameter"), ("inner_", "diameter"), ("hole_", "diameter"),
    ("dia", "diameter"), ("bore_", "diameter"), ("od_", "diameter"), ("id_", "diameter"),
    ("cavity_", "depth"), ("pocket_", "depth"), ("chamber_", "depth"), ("slot_d", "depth"),
)


def _adds_feature(clause: str) -> str | None:
    """这一句是不是「新增一个特征」？是的话返回那个特征名（孔/槽/筋…）。"""
    match = _ADD_FEATURE_RE.search(clause or "")
    return match.group(2) if match else None


# 动词暗指的维度：用户说「加厚」时，那个数字说的是**厚度** ——
# 可句子里并没有"厚度"两个字。实测「把底座加厚到6mm」因此什么都没匹配上：
# 契约空转（changed/target_values 都是空的），AI 什么都没改，
# 而作业照样报"完成"（第 5 版体积与第 4 版一模一样）。
_VERB_DIM_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("加厚", "变厚", "增厚", "厚一点", "厚一些"), "thickness"),
    (("加高", "变高", "增高", "高一点", "高一些"), "height"),
    (("加宽", "变宽", "宽一点", "宽一些"), "width"),
    (("加长", "变长", "长一点", "长一些"), "length"),
    (("加粗", "变粗", "粗一点", "粗一些"), "diameter"),
)


def _verb_dimension_hints(text: str) -> set[str]:
    """句子里由**动词**暗指的维度（加厚 → 厚度）。"""
    lowered = (text or "").lower()
    return {
        dimension
        for words, dimension in _VERB_DIM_HINTS
        if any(word in lowered for word in words)
    }



def _dimension_keys(text: str) -> set[str]:
    """名字/标签里出现的**维度**（直径、高度、厚度…），中英文统一到同一套 key。"""
    if not text:
        return set()
    cleaned = normalize_name(text)
    cleaned = cleaned.lower()
    keys = {cleaned} if cleaned in _CANONICAL_DIMS else set()
    for alias, canonical in SPEC_ALIASES.items():
        if len(alias) >= 2 and alias in cleaned:
            keys.add(canonical)
    return keys


def _modifier_conflict(left: str, right: str) -> bool:
    """两个名字里的方位词是否**相反**（外 vs 内、顶 vs 底…，跨中英文）。"""
    a, b = _direction_words(left), _direction_words(right)
    for first, second in _DIRECTION_OPPOSITES:
        if (first in a and second in b) or (second in a and first in b):
            return True
    return False


def _resolve_by_label(
    key: str,
    current_params: dict[str, float],
    param_labels: dict[str, str] | None,
) -> str | None:
    """用 `@param` 注释里的说明文字把规格名对到源码参数名（最可靠的一座桥）。

    例：规格 `height` + 源码 `// @param total_h 100 总高` → 标签「总高」归一化后
    就是 `height` 本身，直接对上，不用猜 `total_h` 的缩写含义。

    打分而不是「命中即用」：同一个模型里常有两个都含「厚度」的参数
    （`wall_t`「筒壁厚度」、`bottom_t`「底部厚度」）。这时靠修饰词区分 ——
    规格名里有「底」而标签里也有「底」的胜出；方位词相反的（外径 vs 内径）
    直接排除。仍然只在最高分唯一时采用，有歧义就交回给 AI 改。
    """
    if not param_labels:
        return None
    wanted_dims = _dimension_keys(key)
    if not wanted_dims:
        return None

    scores: dict[str, int] = {}
    key_dirs = _direction_words(key)
    for name in current_params:
        label = param_labels.get(name)
        if not label or _modifier_conflict(key, label):
            continue
        shared = _dimension_keys(label) & wanted_dims
        if not shared:
            continue
        score = 50 * len(shared)
        if normalize_name(label) == normalize_name(key):
            score += 30  # 标签归一化后就是规格名本身，最可信
        # 方位词一致（都带「底」、都带「外」）再加分；缺了方位词的降权，
        # 避免「内径」这种带限定的规格名落到一个没有限定的参数上。
        label_dirs = _direction_words(label)
        score += 10 * len(key_dirs & label_dirs)
        if key_dirs and not label_dirs:
            score -= 15
        # 修饰词一致（都带「底」、都带「外」）再加分
        for token in re.split(r"[_\-\s]+", (key or "").lower()):
            if len(token) >= 2 and token in label.lower():
                score += 10
        for char in ("底", "顶", "外", "内", "上", "下", "侧"):
            if char in key and char in label:
                score += 10
        scores[name] = score

    if not scores:
        return None
    best = max(scores.values())
    winners = [name for name, score in scores.items() if score == best]
    if len(winners) != 1:
        log.debug("参数名 %s 按注释标签无法唯一解析，候选=%s", key, winners)
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


def _split_clauses(text: str) -> list[tuple[int, int, str]]:
    """把指令切成子句，并保留每句在**原文里的真实字符区间** `(起, 止, 文本)`。

    为什么要带上区间而不是只返回文本：调用方要把「数字」归到「它所在的那一句」，
    而 `iter_mm_numbers` 给的是数字在原文里的下标。旧实现只拿子句文本，靠
    「前面各句长度之和 + 句序号」去推算区间 —— 那个推算有两处漏记：
      * 分隔符不都是 1 个字符：`然后 / 并且 / 同时 / 另外` 是 2 个字符；
      * `，然后` 会被正则匹配成**两个**分隔符，中间那个空片段又被 `strip()` 丢了，
        于是推算里根本没算这一段。
    推算出来的区间比真实区间**短**，句尾的个位数就会掉进下一句。实测（本函数
    的对照用例）：「高度改成15，然后宽度改成5，然后深度改成20」被算成
    `宽度=没提到、深度=5` —— 程序于是**主动授权**把深度改成 5mm（用户说的是 20），
    同时把宽度的要求整句丢掉。少一个确定性兜底只是保守，下错命令是另一种错。

    空片段（只剩标点或空格的一句）仍然不占子句位，但**区间是原文区间**，
    所以数字归属不受影响。
    """
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    for match in _CLAUSE_SPLIT_RE.finditer(text):
        part = text[cursor : match.start()]
        if part.strip():
            spans.append((cursor, match.start(), part))
        cursor = match.end()
    tail = text[cursor:]
    if tail.strip():
        spans.append((cursor, len(text), tail))
    return spans


# 整体缩放的说法：比例关键词 + 百分数或倍数
_SCALE_WORDS = ("缩放", "缩小", "放大", "扩大", "比例", "整体", "scale")
_PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*[%％]")
_TIMES_RE = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*倍")


def _detect_scale_factor(text: str) -> float | None:
    """从指令里识别「整体缩放」的比例（0.8 = 缩小到 80%）。

    为什么必须单独识别：缩放会让**所有**尺寸一起变。如果契约不把它标出来，
    规格里那些"用户点名过的绝对值"就会变成一票否决 —— 实测「整体缩小到80%」
    被判定为"点名尺寸没做到"然后回滚，用户看到的模型纹丝不动。

    只认"确实在说缩放"的句子：必须同时出现比例关键词与百分数/倍数，
    并且比例落在合理区间（5%~2000%）。宁可漏判（当成普通改动），
    也不要把「孔径改成8mm」这类误判成缩放。
    """
    if not text:
        return None
    factor: float | None = None
    percent = _PERCENT_RE.search(text)
    times = _TIMES_RE.search(text)
    if percent and any(word in text for word in _SCALE_WORDS):
        try:
            factor = float(percent.group(1)) / 100.0
        except ValueError:
            factor = None
    elif times and any(word in text for word in _SCALE_WORDS):
        try:
            factor = float(times.group(1))
        except ValueError:
            factor = None
    if factor is None or not (0.05 <= factor <= 20.0) or abs(factor - 1.0) < 1e-9:
        return None
    return round(factor, 4)


def _find_current_value(
    key: str,
    display: str,
    current_params: dict[str, float],
    param_labels: dict[str, str] | None = None,
) -> float | None:
    """取某个参数在源码里的**当前值**，用于相对修改（「改小 20mm」）。"""
    resolved = resolve_param_name(display, current_params, param_labels) or key
    if resolved in current_params:
        return current_params[resolved]
    for name, value in current_params.items():
        if normalize_name(name) == key:
            return value
    return None


def _dependent_params(
    contract: ChangeContract,
    current_params: dict[str, float],
    param_labels: dict[str, str] | None,
) -> list[str]:
    """算出**必须跟着授权改动一起动**的参数（写进 `contract.follow`）。

    判据（保守：宁可少认几个，也不放纵乱改）：
      * 触发项必须是**长度类**授权改动（高度/长度/宽度/深度/直径）；
      * 候选参数的注释标签要落在**同一个维度家族**
        （改高度 → 「加强筋结束高度」「防滑齿带高度」跟着走）；
      * 方位词不能相反：用户改的是「内径」，就不许把「外径」算成跟随项 ——
        否则等于给了 AI 一个顺手改外径的许可。
    """
    follows: list[str] = []
    if not current_params or not contract.target_values:
        return follows
    labels = param_labels or {}
    changed_keys = {normalize_name(n) for n in contract.changed}

    for raw_name in contract.target_values:
        subject = labels.get(raw_name) or raw_name
        dims = _dimension_keys(subject) & _LENGTH_DIMS
        if not dims:
            continue
        for name in current_params:
            if name == raw_name or normalize_name(name) in changed_keys:
                continue
            if name in follows:
                continue
            label = labels.get(name) or name
            if _modifier_conflict(subject, label):
                continue
            if _dimension_keys(label) & dims:
                follows.append(name)
    return follows


# 「尺寸类」与「位置类」的说法：等比缩放只适用于**沿轴的位置/跨度**。
# `rib_z1`（加强筋结束高度）该跟着总高一起降；
# `rib_h`（加强筋凸出高度）是特征自身的尺寸，跟着缩会让 1.2mm 的筋变成
# 0.6mm —— 低于 0.4mm 喷嘴能可靠成型的下限，那是把模型改坏而不是改小。
_SIZE_WORDS = ("凸出", "凸起", "厚度", "壁厚", "宽度", "齿宽", "直径", "半径", "圆角", "孔径", "间距")
_POSITION_WORDS = ("起始", "起点", "开始", "结束", "终点", "位置", "坐标", "距离", "带高", "离底")


def _scales_with_axis(label: str) -> bool:
    """这个参数是「沿轴的位置/跨度」（跟着尺寸等比走）还是「特征自身尺寸」（不动）？"""
    text = label or ""
    if any(word in text for word in _POSITION_WORDS):
        return True
    return not any(word in text for word in _SIZE_WORDS)


def follow_values(
    contract: ChangeContract,
    current_params: dict[str, float],
    param_labels: dict[str, str] | None = None,
) -> dict[str, float]:
    """按授权改动的比例，算出跟随项应当取的新值（给确定性兜底用）。

    语义与工程直觉一致：总高 100 → 50（比例 0.5）时，加强筋起始/结束高度
    12/88 应该变成 6/44。**只改总高、留下 rib_z1 = 88** 会让加强筋捅出筒身
    38mm —— 那不是"更小的改动"，那是另一个错模型。

    返回的是「参数名 → 新值」，调用方与授权目标值合并后一起写回源码；
    结果仍要过几何检测，不对就照旧保留原版（宁可不改，也不改坏）。
    """
    if not contract.follow or not current_params:
        return {}
    labels = param_labels or {}
    result: dict[str, float] = {}
    for name in contract.follow:
        current_value = current_params.get(name)
        if current_value is None or name in contract.target_values:
            continue
        for raw_name, target in contract.target_values.items():
            base = raw_name if raw_name in current_params else resolve_param_name(
                raw_name, current_params, labels
            )
            if base is None or not current_params.get(base):
                continue
            subject = labels.get(raw_name) or raw_name
            if not (_dimension_keys(labels.get(name) or name) & _dimension_keys(subject)):
                continue
            # 只缩放「沿轴的位置/跨度」；特征自身尺寸（凸出高度、宽度…）保持不变 ——
            # 见 _scales_with_axis 的说明（把 1.2mm 的筋缩成 0.6mm 是改坏模型）。
            if not _scales_with_axis(labels.get(name) or ""):
                continue
            ratio = target / current_params[base]
            if ratio <= 0 or not (0.05 <= ratio <= 20):
                continue
            result[name] = max(0.1, round(current_value * ratio, 3))
            break
    return result


def build_change_contract(
    instruction: str,
    spec: DesignSpec | None,
    current_params: dict[str, float],
    param_labels: dict[str, str] | None = None,
) -> ChangeContract:
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
    # 这里原先还算了一份 `text_variants`（整句归一名 + 句中各中文片段），想靠
    # 「把中文片段拿来和参数名比子串」解决「支架高度」对上「高度改成150mm」。
    # 那套从来没有被读过 —— 真正做成这件事的是下面的**子句匹配**：先把句子按
    # 标点/数字切开，再逐个中文片段与规格名比对。实测（三个陷阱用例）子句匹配
    # 已经把复合标签、多子句各自归属、相对修改都覆盖住了，所以删掉死码，
    # 避免下一个读者以为「改了 text_variants 就能改匹配行为」。

    # 1) 名称匹配：指令里提到哪个规格名，并记下**出现在哪个子句**
    clause_spans = _split_clauses(text)
    clauses = [part for _start, _end, part in clause_spans]
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
            # 「支架高度」这类复合标签：片段里只说了「高度」也算命中。
            #
            # 但**带方位限定的尺寸名**（外径 / 内径 / 顶面孔径）不能靠裸尺寸词命中：
            # 句子里的方位词必须与它自己的一致。实测翻车——用户说
            # 「增加一个直径10mm的圆孔」，裸别名「直径」把**外径**也算了进来，
            # 再叠加「增加」的相对解读，目标值成了 80+10=90，
            # 程序等于主动下令把外径改成 90mm（用户只想要个孔）。
            item_text = f"{raw_name} {item.label if item else ''}".lower()
            item_dirs = _direction_words(item_text)
            fragment_dirs = _direction_words(fragment)
            if item_dirs - fragment_dirs:
                return False
            # 「加厚 / 加高」这类动词暗指的维度也算提到过，但**方位必须完全对上**：
            # 说「底座加厚」时命中的应当是「底部…厚度」，而不是同样含"厚度"的
            # 「筒壁厚度」—— 后者没有"底"这个方位，靠等号把它排除在外。
            hints = _verb_dimension_hints(fragment)
            if hints and item_dirs and item_dirs == fragment_dirs and (
                _dimension_keys(item_text) & hints
            ):
                return True
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
    # 归属规则：数字落进「起始位置最靠前、且不超过这个数字」的那一句 —— 也就是
    # 它自己所在的那一句（数字落在分隔符空隙里时归给前一句，实际不会出现）。
    # 位置一律用原文下标，不再做任何「按句长推算」的中间步骤。
    numbers = [value for _, value in measured]
    numbers_by_clause: dict[int, list[float]] = {}
    for pos, value in measured:
        index = 0
        for i, (start, _end, _part) in enumerate(clause_spans):
            if start <= pos:
                index = i
            else:
                break
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
            # 「新增一个特征」的句子里，数字属于那个新特征，不属于整体尺寸。
            # 实测翻车：`增加一个直径10mm的圆孔` → 被读成「外径 +10mm = 90mm」，
            # 于是程序主动把外径从 80 改成 90（用户全程没提外径）。
            # 只有「被点名的项本身就是这个特征」（标签里含同一个特征名）才继续取数。
            feature = _adds_feature(clause)
            if feature and feature not in f"{display} {key}":
                continue
            values = numbers_by_clause.get(index) or []
            if not values and len(numbers) == 1:
                values = numbers
            if not values:
                continue
            value = values[0]
            # 相对修改：「高度改小 20mm」= 当前值 − 20
            current = _find_current_value(key, display, current_params, param_labels)
            if current is not None:
                if any(word in clause for word in _SHRINK_WORDS):
                    value = max(0.1, current - value)
                elif any(word in clause for word in _GROW_WORDS):
                    value = current + value
            # 解析不到源码参数时，用**被点名的那一项的名字**（而不是归一化 key）
            # 当目标名：归一化 key 会把「内径」压成 `diameter`，方位信息全丢，
            # 于是「依赖项」判定会以为外径也必须跟着改（实测：改内径把 outer_d
            # 列进了 follow，等于授权 AI 顺手改外径）。
            target_name = (
                resolve_param_name(display, current_params, param_labels) or display
            )
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
    # 实测坑：规格里叫 `height`，AI 生成的源码里叫 `stand_height`／`total_h`。
    # 若 `changed` 只记规格名，会同时踩两个坑：
    #   * `_apply_targets()` 找不到可写的参数 → 确定性兜底整条失效；
    #   * 参数核对把 `total_h` 的改动算成「未授权改动」，与「没改到目标值」
    #     一起给一次**完全正确**的改动定了两条罪，最终取舍时输给「什么都没改」
    #     —— 用户看到的就是「AI 说改好了，模型纹丝不动」（实测复现）。
    # 所以每个规格名都要解析到源码参数名，**能解析到的全部补进 changed**。
    changed_keys = {normalize_name(n) for n in deduped}
    for raw_name in list(changed):
        resolved = resolve_param_name(raw_name, current_params, param_labels)
        if resolved and normalize_name(resolved) not in changed_keys:
            deduped.append(resolved)
            changed_keys.add(normalize_name(resolved))

    # 用户明确说「XX 不变」时，**解析出来的源码参数名**也必须一起冻结。
    # 实测：「高度不变，把外径改成70mm」里 frozen 记的是规格名 total_height，
    # 而 changed 通过解析补进了源码名 total_h —— 于是冻结形同虚设，
    # AI 被授权去改用户明确要求别动的高度。
    frozen_sources = {
        resolved
        for raw in frozen
        if (resolved := resolve_param_name(raw, current_params, param_labels))
    }
    frozen_keys |= {normalize_name(name) for name in frozen_sources}
    deduped = [name for name in deduped if normalize_name(name) not in frozen_keys]
    changed_keys = {normalize_name(n) for n in deduped}
    contract.frozen = [*frozen, *sorted(frozen_sources - set(frozen))]

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

    # 3b) 依赖项：授权改动会牵动的同一维度参数。
    #     必须**同时**从 unchanged 里拿掉 —— 否则提示词自相矛盾：
    #     一边要求「总高改成 50」，一边要求「rib_z1 逐字保持 88」，
    #     AI 只能二选一，实测它选了后者（返回原文，什么都没改）。
    contract.follow = [
        name for name in _dependent_params(contract, current_params, param_labels)
        if normalize_name(name) not in frozen_norm
    ]
    if contract.follow:
        follow_norm = {normalize_name(name) for name in contract.follow}
        contract.unchanged = [
            name for name in contract.unchanged if normalize_name(name) not in follow_norm
        ]

    # 4) 整体缩放：所有"用户点名过的尺寸"都不再按旧绝对值要求，且不存在必须不变的项
    factor = _detect_scale_factor(text)
    if factor is not None:
        contract.scale_factor = factor
        for item in (spec.specs if spec is not None else []):
            if item.value is None or normalize_name(item.name) in frozen_norm:
                continue
            if item.name not in contract.changed:
                contract.changed.append(item.name)
        # 缩放时**没有**"必须逐字不变"的项：长宽高本来就都要按比例变。
        contract.unchanged = []
        contract.follow = []
        # 但授权名单必须落到**源码参数名**上：只写规格名（outer_diameter…）时，
        # 参数核对会把缩放后的 outer_d / total_h 全部判成"越权改动"，
        # 于是「整体缩小到80%」永远不达标 → 最终被回退成"什么都没改"
        # （实测：模型一毫米没动，作业报成功）。缩放本来就该改所有尺寸参数。
        changed_keys = {normalize_name(name) for name in contract.changed}
        for name in current_params:
            if normalize_name(name) in changed_keys or name in contract.changed:
                continue
            contract.changed.append(name)
            changed_keys.add(normalize_name(name))

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
        "6. 保留文件顶部的 // @spec 指纹行（如存在）——程序用它核对设计一致性。\n"
        "7. 用户没点名的尺寸（外径/壁厚/底座厚度等）必须保持原值：改某个孔或某处特征时，"
        "**不得顺带改变其它任何尺寸**（实测有模型改孔径时把外径从 80 改成 60）。\n"
        "8. 涉及「加孔/贯通」的要求：挖孔刀具必须沿轴向伸出本体两端各 ≥0.1mm 确保完全贯通；"
        "孔的直径用独立参数写死为要求值，不要写成依赖其它尺寸的表达式"
        "（表达式会让后续改孔径失效）。\n"
        "9. 「总高/高度/外径/宽度」指**整体包围盒**尺寸（含底座/唇边/凸台的贡献）："
        "改高度后实测包围盒 Z 必须等于目标值，不能只改某一段而让总高偏差。\n"
        "10. 动到夹口/卡槽/插槽这类**减法特征**时，刀具必须真的与主体相交："
        "程序会拿导出的 STL 量「被材料夹住的空档」，你声明的间隙尺寸量不到就是没做到"
        "（实测过：源码写 `clamp_gap = 14` 但夹口坐标写成 `+ clamp_gap + jaw_h - jaw_h`，"
        "自我抵消后一整块料没切，编译成功、没有任何 warning）。"
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
        "导出成「1 个主体 + 几十个散件」，而且让 AI 修也修不回来 —— 必须一开始就别这么画。\n"
        "16. **用户给的「总高/高度/外径/宽度」指整体包围盒尺寸**，不是某一段的尺寸：\n"
        "    总高 = 模型包围盒 Z 轴尺寸（含底座、唇边、凸台、支脚的全部贡献）；\n"
        "    外径/宽度 = 包围盒水平方向最大尺寸（含唇边、把手、外扩台肩）。\n"
        "    程序会拿导出 STL 的实测包围盒逐轴核对，写成「某一段的高度」或\n"
        "    「不含唇边的直径」都会被判为没做到（实测 glm 把总高 100 写成筒身 100、\n"
        "    加底座后实测 110；外径 80 写成筒身 80、加唇边后实测 83.5）。\n"
        "    做法：先定包围盒目标，再往内分配各段尺寸（如筒身 = 总高 − 底座厚）。\n"
        "17. **声明了的间隙/夹口必须在实体上量得到**（程序会拿导出的 STL 实测）：\n"
        "    实测翻车案例：源码写 `clamp_gap = 14;` 表示手机夹的夹口开度，但夹口这一刀\n"
        "    完全没切进实体，导出的 STL 沿 Y 实测内部空隙 **0.00mm**（沿该轴 113mm 全是连续材料）——\n"
        "    而编译成功、STL 非空、没有任何 warning，参数表里还写着 14。两条硬性写法：\n"
        "    a) 空隙两侧的表面坐标必须由该参数**直接**决定：下侧顶面 = 下侧底 + jaw_h，\n"
        "       上侧底面 = 下侧顶面 + clamp_gap。**禁止**写成 `+ clamp_gap + jaw_h - jaw_h`\n"
        "       这种自我抵消的表达式（看着用了参数，实际等于没加）。\n"
        "    b) 刀具与主体必须在**同一坐标系**：主体若是 `linear_extrude` 沿 +Z 挤出的，\n"
        "       刀具写成 `rotate([-90,0,0]) linear_extrude(2D轮廓)` 会把刀具搬到 z<0 的模型外面，\n"
        "       什么都切不到 —— 要么不 rotate，要么 rotate 后重算 translate。\n"
        "    自检：对每一处 `difference()` 问一句「刀具的包围盒与主体的包围盒重叠多少」；\n"
        "    再对每个间隙参数问一句「它决定的是**哪个面**的坐标」。\n"
        "18. **与真实物体配合的尺寸要写依据、并做成可调参数**：\n"
        "    例如「夹手机的支架」的夹口开度必须能容下主流手机（宽 70–78mm、厚 7–12mm），\n"
        "    凭感觉写 `clamp_w = 62` 会得到一个手机根本放不进去的夹子。\n"
        "    做法：把配合对象的实际尺寸写进 @param 注释作为取值依据，并让该尺寸成为顶层参数"
        "（用户可以说「改成夹 82mm 的设备」）。"
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
    # 注释标签是规格名 ↔ 源码参数名之间最可靠的桥（见 extract_param_labels）
    before_labels = scad_src.extract_param_labels(before_scad)
    after_labels = scad_src.extract_param_labels(after_scad)

    changed_keys = {normalize_name(n) for n in contract.changed}
    target_keys = set(contract.target_values)
    # 必须跟着改动走的参数：允许变（否则一次正确的联动调整会被判成越权），
    # 但要**如实记录**下来给用户看，不能让它们在结论里消失。
    follow_keys = {normalize_name(n) for n in contract.follow}

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
                    resolve_param_name(name, after_params, after_labels)
                    or resolve_param_name(name, before_params, before_labels)
                    or name
                )
            )
            if resolved != name and scad_src.is_assigned(after_scad, name):
                # 源码里有 `name = 表达式;`：解析到别的参数上会读错值
                # （读的是那个参数，不是用户要的这个尺寸），因此不认这条解析，
                # 让 §a 如实报「未找到该参数」，交给 AI 复核与几何检测兜底。
                resolved = name
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
    followed: list[str] = []
    for name, before_value in before_params.items():
        key = normalize_name(name)
        if key in changed_keys:
            continue
        after_value = _lookup(after_params, key)
        if after_value is None:
            verdict.unauthorized.append(f"{name}（被删除）")
            continue
        if abs(after_value - before_value) > max(0.001, abs(before_value) * 1e-6):
            if key in follow_keys:
                # 跟随项：允许变，但要报出来（用户有权知道哪些数值一起动了）
                followed.append(f"{name}（{before_value:g} → {after_value:g}）")
                continue
            verdict.unauthorized.append(f"{name}（{before_value:g} → {after_value:g}）")
    if followed:
        verdict.reasons.append(
            "为配合这次改动一起调整了：" + "、".join(followed[:6])
        )
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
        resolved = (
            resolve_param_name(raw, after_params, after_labels)
            or resolve_param_name(raw, before_params, before_labels)
        )
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
