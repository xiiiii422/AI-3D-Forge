"""AI 3D Forge —— 需求可执行测试（把设计契约编译成能跑在实测网格上的断言）。

为什么补这一层（对标结论见 docs/COMPETITIVE-GAPS.md 的 G3 / 路线 S1）：
  * 我们早就有「实测几何 → 带证据回喂 AI 修复」的闭环，但它的产物是**给人读的中文段落**，
    不是**能统计、能回归、能摊给用户看的判定表**。结果两个问题一直答不上来：
      - 「这次生成到底满足了几条需求？」（用户视角：能不能直接用）
      - 「改了提示词 / 换了模型，质量是涨还是跌？」（工程视角：没有基线就没法回归）
  * arXiv 2605.07807《Text-to-CAD Evaluation with CADTests》的做法是把每条需求编译成
    **可执行测试**，用三个指标汇报：
      PR pass-rate          全部测试通过（= 整份需求达成）
      RS requirement-score   需求组满足占比（组内任一测试不过则该组不过）
      IR invalid-ratio       没能判定（跑不起来 / 量不到）的占比
    并把**失败测试的执行日志回喂生成端**；论文与开源 harness earthtojake/text-to-cad
    的共同立场是：难点不是"生成一个形状"，而是"生成之后怎么证明它没错"。
    论文口径里 invalid 计为失败（不许把测不出来当成通过），这里照搬。

与修复循环的分工（避免两套判据互相打架）：
  * `jobs._geometry_integrity_problems` 继续负责给 AI 的**详细改法**（那些改法是实测踩坑攒出来的）；
  * 本模块负责**逐条判定 + 数值偏差 + 公差 + 可统计指标**，并把失败项压成紧凑表格
    追加进修复日志（尺寸偏差与公差这些数字，中文段落里本来没有）；
  * 阈值与 jobs 保持一致：尺寸只认「明显偏小 / 超出可解释的偏大」、孔径容差 max(0.6, 6%)、
    螺纹看 thread_is_helix。自检里带一条**单向一致性守卫**：
    jobs 报出来的尺寸问题，在本清单里必须也是 fail（不允许一边报一边判通过）。

四种状态，必须区分清楚：
  pass     测到了，达成
  fail     测到了，没达成
  invalid  本该能测却没测出来（网格缺失、量不到剖面）→ 按失败计入 IR，绝不粉饰
  manual   本质上要人/视觉模型判断的语义需求（「外观像猫」）→ 不打分，但必须摊开列出，
           否则用户会以为「绿色 = 全部需求都过了」
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .design import diameter_is_fit_scope
# 贴边材料占比的阈值从测量层拿（同一个文件里再写一遍就是一个新的尺子）。
from .mesh import _END_MIN_COVERAGE  # noqa: PLC0414

PASS = "pass"
FAIL = "fail"
INVALID = "invalid"
MANUAL = "manual"

# 水平包络轴（与 jobs.HORIZONTAL_ENVELOPE_AXIS 同义：外径类尺寸对应 max(bbox.x, bbox.y)）。
# 在这里重复一个常量而不是反向 import jobs —— jobs 会 import 本模块，不能形成环。
HORIZONTAL_AXIS = 3

# 面数预算的兜底值：拓竹 MakerWorld 负责人公开说过 AI 模型「动辄几十百万面，
# 导入切片软件慢且易出问题」。真实判据取 limits.max_triangles（机型相关，默认 40 万），
# 这里只在没有机型信息时兜底 —— 必须与 mesh.AnalysisLimits 的默认值一致，
# 否则同一个软件里会有两把尺子量同一件事（清单说合格、打印性检查说超标）。
TRIANGLE_BUDGET = 400_000

# 支撑占比高于这个值就提示换摆放方向（已有 orientation_hint 给具体方向）
SUPPORT_RATIO_PCT = 25.0

_THREAD_TOKENS = re.compile(r"螺纹|螺距|滤镜|转接|拧|丝口|丝锥|thread", re.IGNORECASE)

# 用户点名「底要封闭」的说法。为什么只认这些词（与第 4 节螺纹同一个做法）：
# 管子/环形件/支架本来就该两头通，无条件判“底是开的”会把正确的模型报成缺陷；
# 而“底部不要实心”“镂空底座”这类说法不命中下表，不会被误拦。
_BOTTOM_TOKENS = re.compile(
    r"实心底|封闭的底|底部封闭|封底|不漏|平底|加厚底座|底板"
)

# 需求里点名“顶部要开口”的说法。为什么需要它：光看“底开着、顶封着”分不出
# **倒过来建的容器**和**本来就该开口朝下的盖子**，而拿错的那一档去驱动修复会把盖子改成塞子。
# 实测：笔筒需求一句含“顶部敞开”，而 27cc8c 那版网格正好上下相反。
_TOP_OPEN_TOKENS = re.compile(r"顶部敞开|顶部开口|顶部开了|顶部不封|无盖|敞口|开口朝上|上端开口|上面开口")

# “没底”的标准改法（开口的和挖穿的用同一个措辞，避免两处漂移）。
_SOLID_BOTTOM_FIX = (
    "内腔圆柱不能从 z=0 开始挖：先写底厚，再 `translate([0,0,底厚])` 挖腔，"
    "或把腔高写成 `height - 底厚`；排水/走线孔单独用小直径圆柱挖，不要拿内腔当孔。"
)


def _footprint_span(metrics: Any) -> float:
    """足迹沿 X 方向的总宽（那正是 `measure_end_coverage` 扫线用的轴）。量不到时 0。"""
    try:
        return max(float(metrics.bbox_max[0]) - float(metrics.bbox_min[0]), 0.0)
    except (AttributeError, TypeError, ValueError, IndexError):
        return 0.0


def _end_is_closed(coverage: float, hole_at_end: float | None, span: float) -> bool:
    """这一端算不算“封住了”。阈值会随**实测到的真通孔**放宽，不是拿固定 50% 卡天下。

    为什么必须放宽：固定阈值会把大孔件报成缺陷 —— Ø45 通孔的漏斗件，贴底一层
    本来只剩约 20% 是料，拿 50% 去卡就是把正确设计判成“底被挖穿了”，
    然后驱动修复循环去封一个用户明令要通的孔。

    拿谁来放宽：**孔径剖面**（产品自己的尺子）在这一端量到的直径，就是
    “用户点名的缺口”。`_hole_profile_heights` 第一个探针贴底、最后一个贴顶，
    所以两端各自拿自己那个探针的实测值，不拿中部去替底层说话。
    开口管拿不到剖面（没有孔，是“根本没有底”），于是不会被误放宽。

    实测两个端点（OpenSCAD 真实编译的夹具）：Ø80 带 Ø10 排水孔 → 允许缺口
    max(0.5, 10/80)=0.5，门槛 0.5，贴底实测 0.958 过；开口管 → 门槛仍 0.5，
    实测 0.085 不过。两个数跟 mesh.`_END_MIN_COVERAGE` 那段注释是同一批实测。
    """
    allowed = _END_MIN_COVERAGE
    if hole_at_end is not None and span > 1e-6:
        allowed = max(allowed, min(float(hole_at_end) / span, 1.0))
    return float(coverage) >= 1.0 - allowed


@dataclass
class ReqTest:
    """一条可执行需求测试的结果。"""

    group: str          # 需求组：同一条需求可以拆多个断言，组内全过才算这条需求达成
    requirement: str    # 给人看的需求描述，例如「总高」
    kind: str           # dimension / hole / thread / integrity / print / semantic / feature-gap
    status: str         # PASS / FAIL / INVALID / MANUAL
    expected: str = ""
    actual: str = ""
    deviation_pct: float | None = None
    tolerance_mm: float | None = None
    fix_hint: str = ""
    # 咨询类（打印工艺建议等）只进清单、不进修复日志：
    # 修复循环的采纳判据里没有这些维度，喂给它只会白烧一次调用。
    advisory: bool = False

    @property
    def scored(self) -> bool:
        """是否参与 PR/RS 打分。

        manual 不参与：语义需求要靠人/外观复核，打分等于自评。
        advisory 不参与：那是**工艺建议**（壁厚、支撑、面数、超范围），不是用户提的需求。
        第一轮基准实测：一个把用户要求全做到的圆垫片因为 p05 采样噪声被判 advisory 失败，
        整件 PR 直接归零 —— 让"建议"参与"需求"打分，指标就失去意义了。
        invalid 仍然参与且算失败（论文口径：不许把"没测出来"当通过）。
        """
        return self.status != MANUAL and not self.advisory

    @property
    def ok(self) -> bool:
        return self.status == PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "requirement": self.requirement,
            "kind": self.kind,
            "status": self.status,
            "expected": self.expected,
            "actual": self.actual,
            "deviation_pct": (
                round(self.deviation_pct, 2) if self.deviation_pct is not None else None
            ),
            "tolerance_mm": (
                round(self.tolerance_mm, 3) if self.tolerance_mm is not None else None
            ),
            "fix_hint": self.fix_hint,
            "advisory": self.advisory,
        }


@dataclass
class ReqSuite:
    """一次生成/修改的需求测试集合，以及 PR/RS/IR 三个指标。"""

    tests: list[ReqTest] = field(default_factory=list)
    # 水平尺寸择优配对后的目标：{轴: (标签, 目标mm, 规格名)}。
    # 为什么要回传给 jobs：逐轴缩放以前只按"约定轴"记账（X=宽、Y=深），
    # 模型把长边放在 X 时它既会误判、也会朝错误方向缩放。这里把判定层
    # 已经确认过的方向交回给缩放层，两层用同一套事实，不再各量各的。
    dim_targets: dict[int, tuple[str, float, str]] = field(default_factory=dict)

    def dimension_targets(self) -> dict[int, tuple[str, float, str]]:
        return dict(self.dim_targets)

    # ---------------- 指标（口径对齐 CADTests） ----------------
    @property
    def scored_groups(self) -> list[str]:
        seen: list[str] = []
        for t in self.tests:
            if t.scored and t.group not in seen:
                seen.append(t.group)
        return seen

    def _group_failed(self, group: str) -> bool:
        return any(t.group == group and t.status in (FAIL, INVALID) for t in self.tests)

    @property
    def counts(self) -> dict[str, int]:
        out = {PASS: 0, FAIL: 0, INVALID: 0, MANUAL: 0}
        for t in self.tests:
            out[t.status] = out.get(t.status, 0) + 1
        return out

    @property
    def requirement_score(self) -> float:
        """RS：达成的需求组占比。没有可打分项时返回 1.0（无需求=无违反）。"""
        groups = self.scored_groups
        if not groups:
            return 1.0
        hit = sum(1 for g in groups if not self._group_failed(g))
        return hit / len(groups)

    @property
    def invalid_ratio(self) -> float:
        """IR：没能判定的占比（论文口径：invalid 也是失败，不许拿"没测"当通过）。"""
        groups = self.scored_groups
        if not groups:
            return 1.0
        bad = sum(
            1
            for g in groups
            if any(t.group == g and t.status == INVALID for t in self.tests)
        )
        return bad / len(groups)

    @property
    def pass_rate(self) -> float:
        """PR：整份需求是否全部达成（0/1，单件场景下就是「全过 / 有没过」）。"""
        scored = self.scored_groups
        if not scored:
            return 0.0
        return 1.0 if all(not self._group_failed(g) for g in scored) else 0.0

    @property
    def failures(self) -> list[ReqTest]:
        return [t for t in self.tests if t.status in (FAIL, INVALID) and not t.advisory]

    @property
    def blocking_failures(self) -> list[ReqTest]:
        """真正「用户提的要求没做到」的项：不含 INVALID，不含咨询类。

        为什么排除 INVALID：按论文口径 invalid 在**打分**时算失败（不许把"没测出来"
        当通过），但它不该去驱动修复 —— 给 AI 一句「剖面量不到孔」只会让它去改
        本来没坏的东西（jobs._hole_problems 特意"量不到就不报"，同一个道理）。
        INVALID 照样进清单、进 IR，摊给用户看。
        """
        return [t for t in self.tests if t.status == FAIL and not t.advisory]

    def repair_lines(self, limit: int = 8) -> list[str]:
        """把未达成项压成回喂给 AI 的紧凑执行日志（带数字与公差，这是中文段落里没有的）。"""
        bad = self.blocking_failures[:limit]
        if not bad:
            return []
        head = [
            "【需求核对表】下面是**逐条需求的实测判定**（数值来自导出的 STL，公差来自机型/规格）："
        ]
        body = []
        for t in bad:
            line = f"  ✗ {t.requirement}：要求 {t.expected or '—'}，实测 {t.actual or '没测出来'}"
            if t.deviation_pct is not None:
                line += f"，偏差 {t.deviation_pct:+.1f}%"
            if t.tolerance_mm is not None:
                line += f"，公差 ±{t.tolerance_mm:g}mm"
            body.append(line)
        rest = len(self.blocking_failures) - limit
        if rest > 0:
            body.append(f"  （另有 {rest} 条未列出）")
        return head + body

    def summary(self) -> dict[str, Any]:
        return {
            "pass_rate": round(self.pass_rate, 4),
            "requirement_score": round(self.requirement_score, 4),
            "invalid_ratio": round(self.invalid_ratio, 4),
            "counts": self.counts,
            "groups": len(self.scored_groups),
            "manual": sum(1 for t in self.tests if t.status == MANUAL),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "tests": [t.to_dict() for t in self.tests],
        }


def _fmt(value: float) -> str:
    return f"{value:g}"


def _tolerance_for(spec_item: Any, base_mm: float, want: float) -> float:
    """该条尺寸的判定公差：规格自带的用户公差 > 机型配合公差，再兜底 2%。

    为什么要有兜底比例：实测 STL 有舍入与网格离散化误差（0.01mm 导出精度 + CGAL 重建），
    拿 0.2mm 的机型公差去卡 300mm 的长边会因为几根线的网格抖动误判。
    """
    own = getattr(spec_item, "tolerance", None) if spec_item is not None else None
    try:
        own_mm = float(own) if own is not None else 0.0
    except (TypeError, ValueError):
        own_mm = 0.0
    return max(own_mm, float(base_mm or 0.25), want * 0.02)


def evaluate(
    *,
    spec: Any = None,
    metrics: Any = None,
    requirement: str = "",
    contract: Any = None,
    limits: Any = None,
    tolerance_mm: float = 0.25,
    declared_dimensions: dict[int, tuple[str, float, str]] | None = None,
    planar_dimensions: list[tuple[str, float, str]] | None = None,
    declared_holes: list[tuple[str, float, str]] | None = None,
    declared_gaps: list[tuple[str, float, str]] | None = None,
    spec_items: list[Any] | None = None,
) -> ReqSuite:
    """把设计契约 + 机型约束编译成逐条判定。

    declared_dimensions / planar_dimensions / declared_holes / declared_gaps
    由调用方（jobs）传进来：「哪个规格项算整体尺寸、哪个算孔径、哪个算间隙」
    的词表判断已经在 jobs 里定标过，这里不另立一套判据，否则两处会慢慢漂移。
    declared_gaps 不传时退回读 metrics.declared_gaps（单测与老调用路径）。

    planar_dimensions（宽/深/长那一圈）在这里会被**整组择优配对**后再比：
    模型绕竖直轴转 90° 是等价摆放，按固定轴比会造出假失败。
    """
    tests: list[ReqTest] = []
    dims = dict(declared_dimensions or {})
    planar = [(label, float(want), name) for label, want, name in (planar_dimensions or [])]
    holes = list(declared_holes or [])
    if declared_gaps is None:
        gaps_declared = list(getattr(metrics, "declared_gaps", []) or []) if metrics else []
    else:
        gaps_declared = list(declared_gaps)
    items = list(spec_items if spec_items is not None else (getattr(spec, "specs", []) or []))

    # ---------- 0. 能不能测（网格缺失时后面全是 invalid，但仍然要出表） ----------
    measurable = metrics is not None and getattr(metrics, "triangles", 0) > 0
    if not measurable:
        tests.append(
            ReqTest(
                group="mesh:measurable",
                requirement="导出可用的实体网格",
                kind="integrity",
                status=INVALID,
                expected="能解析出三角形与体积",
                actual="网格缺失或为空",
                fix_hint="编译或解析失败：先让代码能正常编译出一个封闭实体。",
            )
        )
        return ReqSuite(tests=tests)

    # ---------- 1. 完整性（一整件 / 封闭 / 无穿腔 / 无薄膜） ----------
    solid_parts = int(getattr(metrics, "solid_parts", 0) or 0)
    stray = int(getattr(metrics, "stray_parts", 0) or 0)
    tests.append(
        ReqTest(
            group="integrity:one-body",
            requirement="模型是一整件（不需要自己拼）",
            kind="integrity",
            status=PASS if solid_parts <= 1 else FAIL,
            expected="1 个实体",
            actual=f"{solid_parts} 个实体（散件 {stray} 个）",
            fix_hint="让附件与主体**实体相交**（重叠 ≥0.4mm），不是摆得近就行。",
        )
    )
    boundary = int(getattr(metrics, "boundary_edges", 0) or 0)
    watertight = bool(getattr(metrics, "watertight", False))
    tests.append(
        ReqTest(
            group="integrity:watertight",
            requirement="网格封闭（切片软件能判断内外）",
            kind="integrity",
            status=PASS if (watertight and boundary <= 10) else FAIL,
            expected="封闭、开放边 ≤10 条",
            actual=f"封闭={'是' if watertight else '否'}，开放边 {boundary} 条",
            fix_hint="少用会留下开口的布尔差集；difference 的刀具要整块穿出表面。",
        )
    )
    # ---------- 1.5 用户点名的“实心底”：贴底那一层到底有没有料 ----------
    # 为什么必须单独一条（实测）：AI 写的容器经常把内腔圆柱从 z=0 开挖
    # （`cylinder(r=inner, h=height+0.01)`），底部整个是开的。这种件
    # `solid_parts=1`、`watertight=True`、包围盒与体积都对、壁厚不报、孔径剖面全 None
    # （而剖面判据“量不到就不报”），现有指标一项都不红 —— 本仓 31 份历史真模型里
    # 18 份底是开口的（14 份两端都开、4 份开口朝下），产品当时对用户说的都是“改好了”。
    if _BOTTOM_TOKENS.search(requirement or ""):
        coverage = getattr(metrics, "bottom_coverage", None)
        top_cov = getattr(metrics, "top_coverage", None)
        profile = list(getattr(metrics, "hole_profile", []) or [])
        span = _footprint_span(metrics)
        bottom_hole = profile[0] if profile else None
        top_hole = profile[-1] if profile else None
        if coverage is None or top_cov is None:
            tests.append(
                ReqTest(
                    group="bottom:solid",
                    requirement="底部是封闭的实心底",
                    kind="integrity",
                    status=INVALID,
                    expected="贴底一层里足迹内部基本是材料",
                    actual="底层/顶层扫线没能量出来（网格超预算或退化）",
                    fix_hint="这一项没能判定，不算通过；重新导出或降低面数后再看。",
                )
            )
        else:
            bottom_closed = _end_is_closed(coverage, bottom_hole, span)
            top_closed = _end_is_closed(top_cov, top_hole, span)
            text = f"贴底实测 {round(float(coverage) * 100.0, 1):g}%，贴顶实测 {round(float(top_cov) * 100.0, 1):g}%"
            if bottom_closed:
                status, advisory, actual, hint = PASS, False, text, _SOLID_BOTTOM_FIX
            elif top_closed and _TOP_OPEN_TOKENS.search(requirement or ""):
                # 底开着、顶封着，而需求里同时点名了“顶部敞开”：这件是**倒过来建的**。
                # 有了“顶要开”那句话才能区分它和盖子/罩壳（那种本来就该开口朝下），
                # 所以这一档可以拦下修复：实测 27cc8c 的源码注释写着“顶部开口，底部封闭”，
                # 而网格正好反了 —— 不报就会把一个开口朝下的笔筒交付出去。
                status, advisory = FAIL, False
                actual = text + "（方向反了：贴顶一层是封的，贴底一层开着）"
                hint = (
                    "腔体要从**顶面**往下挖（`translate([0,0,底厚])` 起挖、顶部留出开口），"
                    "现在上下正好反了：底是开的、顶是封的。"
                )
            elif top_closed:
                # 底开着、顶封着，而需求没说顶部要开：这是**开口朝下**的件（盖子、罩壳）。
                # 不拦修复循环：拿“补底”去要它，会把一个本来正确的盖子改成塞子；
                # 但也不能报 PASS —— 用户说的是“实心底”，现状确实不封底。
                status, advisory = MANUAL, True
                actual = text + "（贴顶一层是封的：这件开口朝下，翻过来就不漏 —— 要不要翻转由你定）"
                hint = "如果这件应当开口朝上：腔体从顶面开挖、底部留 `底厚`；如果本来就是盖子，这一项可以忽略。"
            else:
                status, advisory = FAIL, False
                actual = text + "（两端都开着：无论怎么放都装不住东西，会漏掉）"
                hint = _SOLID_BOTTOM_FIX
            tests.append(
                ReqTest(
                    group="bottom:solid",
                    requirement="底部是封闭的实心底",
                    kind="integrity",
                    status=status,
                    advisory=advisory,
                    expected=f"贴底一层材料占比 ≥{round((1.0 - _END_MIN_COVERAGE) * 100)}%"
                             "（允许用户点名的通孔占掉相应面积）",
                    actual=actual,
                    fix_hint=hint,
                )
            )

    islands = list(getattr(metrics, "cavity_islands", []) or [])
    tests.append(
        ReqTest(
            group="integrity:no-cavity-island",
            requirement="内腔里没有伸进去的多余结构",
            kind="integrity",
            status=PASS if not islands else FAIL,
            expected="内腔截面里没有孤岛",
            actual="、".join(f"高 {z:.0f}mm 处 {n} 处" for z, n in islands[:4]) or "无",
            fix_hint="外壁特征先做掏空再贴外壁，或改用与腔体形状一致的路径。",
        )
    )
    wall = getattr(metrics, "wall", None)
    if wall is None:
        tests.append(
            ReqTest(
                group="integrity:no-film",
                requirement="没有≈0 厚的薄膜/重合面",
                kind="integrity",
                status=INVALID,
                expected="射线法能测出壁厚",
                actual="壁厚未能测量",
                fix_hint="网格过小或异常导致测不到壁厚：确认模型不是空壳/单面。",
            )
        )
    else:
        p05 = float(getattr(wall, "p05_mm", 0.0) or 0.0)
        tests.append(
            ReqTest(
                group="integrity:no-film",
                requirement="没有≈0 厚的薄膜/重合面",
                kind="integrity",
                status=PASS if p05 >= 0.05 else FAIL,
                expected="壁厚 p05 ≥0.05mm",
                actual=f"壁厚 p05 {_fmt(p05)}mm（中位 {_fmt(float(getattr(wall, 'median_mm', 0) or 0))}mm）",
                fix_hint="difference 的刀具沿挖空方向两端各伸出 ≥0.1mm。",
            )
        )

    # ---------- 2. 用户点名的整体尺寸（带公差与偏差百分比） ----------
    targets: dict[str, float] = {}
    if contract is not None:
        raw = getattr(contract, "target_values", {}) or {}
        from .design import normalize_name  # 延迟导入：只在有契约时才需要

        targets = {
            normalize_name(str(k)): float(v)
            for k, v in raw.items()
            if isinstance(v, (int, float))
        }
    item_by_name: dict[str, Any] = {}
    from .design import normalize_name as _nn

    for it in items:
        item_by_name.setdefault(_nn(str(getattr(it, "name", ""))), it)
        item_by_name.setdefault(_nn(str(getattr(it, "label", ""))), it)

    bbox = tuple(getattr(metrics, "bbox", (0.0, 0.0, 0.0)) or (0.0, 0.0, 0.0))

    # 组装要核对的尺寸：竖直/水平包络按轴，水平那一圈按「整组择优配对」。
    # 为什么必须配对：产品约定 X=宽、Y=深，但模型绕竖直轴转 90° 完全等价 ——
    # 第一版质量基准实测「长 60、宽 40、高 20」的长方体按固定轴被判成两条未达成。
    # 假失败比漏报更糟：用户一旦不信这张表，真失败那条也没人看了。
    entries: list[tuple[str, float, str, int]] = []  # (标签, 目标, 规格名, 轴)
    for axis, (label, want, spec_name) in sorted(dims.items()):
        if axis in (0, 1):
            continue  # 水平方向统一由 planar 那组处理，避免同一条被核对两次
        entries.append((label, float(want), spec_name, axis))
    if not planar:
        # 调用方没给水平组时退回按轴记录（老数据/单测路径）
        for axis, (label, want, spec_name) in sorted(dims.items()):
            if axis in (0, 1):
                entries.append((label, float(want), spec_name, axis))
    else:
        planar_entries = [(str(label), float(want), str(name)) for label, want, name in planar]
        if len(planar_entries) >= 2:
            ex, ey = float(bbox[0] or 0.0), float(bbox[1] or 0.0)
            first = planar_entries[0]
            second = planar_entries[1]
            swapped = (
                abs(ey - first[1]) + abs(ex - second[1])
                < abs(ex - first[1]) + abs(ey - second[1])
            )
            for i, (label, want, name) in enumerate((first, second)):
                axis = (1 - i) if swapped else i
                entries.append((label, want, name, axis))
            for label, want, name in planar_entries[2:]:
                entries.append((label, want, name, HORIZONTAL_AXIS))
        else:
            label, want, name = planar_entries[0]
            ex, ey = float(bbox[0] or 0.0), float(bbox[1] or 0.0)
            axis = 0 if abs(ex - want) <= abs(ey - want) else 1
            entries.append((label, want, name, axis))

    dim_targets: dict[int, tuple[str, float, str]] = {}
    for label, want, spec_name, axis in entries:
        # 契约目标优先：用户说了「改成 50」，就不能拿旧规格的 100 去判失败
        # （实测踩过：拿旧值判 → 报「没做到」→ AI 修复把模型改回 100 → 用户看到「改了没反应」）。
        for key in (_nn(spec_name), _nn(label)):
            if key in targets:
                want = targets[key]
                break
        if want <= 0:
            continue
        measured = (
            max(bbox[0], bbox[1]) if axis == HORIZONTAL_AXIS else float(bbox[axis] or 0.0)
        )
        # 把判定用的方向与目标回传（契约目标已套上），供 jobs 的逐轴校正复用
        dim_targets.setdefault(axis, (label, float(want), spec_name))
        item = item_by_name.get(_nn(spec_name)) or item_by_name.get(_nn(label))
        tol = _tolerance_for(item, tolerance_mm, want)
        dev = measured - want
        dev_pct = (dev / want * 100.0) if want else 0.0
        base = ReqTest(
            group=f"dim:{spec_name or label}",
            requirement=f"{label}（{_axis_label(axis)}）",
            kind="dimension",
            status=PASS,
            expected=f"{_fmt(want)}mm",
            actual=f"{_fmt(round(measured, 2))}mm",
            deviation_pct=round(dev_pct, 2),
            tolerance_mm=round(tol, 3),
        )
        if abs(dev) <= tol:
            base.status = PASS
        elif measured < want - tol:
            base.status = FAIL
            base.fix_hint = (
                f"该轴实测比要求短 {_fmt(abs(dev))}mm：检查是不是把参数用成了「某一段」的尺寸，"
                "或者 rotate 之后挤出方向已经不是竖直方向（斜长与竖直升高隔着一个三角函数）。"
            )
        elif measured <= want * 1.10:
            # 偏大但在可解释范围内：底座、托边、翻边这类附加结构本来就会让外形更大。
            base.status = PASS
            base.actual = f"{_fmt(round(measured, 2))}mm（含附加结构，偏大 {dev_pct:+.1f}%）"
        elif diameter_is_fit_scope(label, spec_name):
            # 杯径/口径这类**配合尺寸**：包围盒只能核对下界 —— 座子本来就要包住那个
            # 东西，整件比它大是必然的。实测「杯径 70 的水杯架、整件 132」被这里
            # 判成 FAIL，修复日志跟着报「超出 89%」（一条根本不存在的缺陷）。
            # 但也不能说它**验证过了**：真要做到 70 的杯位，得靠外观复核确认。
            base.status = MANUAL
            base.requirement = f"{label}（配合尺寸）"
            base.actual = (
                f"{_fmt(round(measured, 2))}mm 是整件外轮廓，比要求的 {_fmt(want)}mm 大是正常的；"
                "包围盒测不出这个局部尺寸，请外观复核确认"
            )
        else:
            base.status = FAIL
            base.fix_hint = (
                f"实测比要求大 {_fmt(abs(dev))}mm（{dev_pct:+.1f}%），超出可解释的附加结构范围："
                "把多出来的结构收掉，或让该尺寸直接由对应参数决定。"
            )
        tests.append(base)

    # ---------- 3. 用户点名的孔径（射线剖面，任一高度命中即达成） ----------
    profile = [d for d in (getattr(metrics, "hole_profile", []) or [])]
    hole_through = getattr(metrics, "hole_through", None)
    if hole_through is False:
        # 剖面里某些高度量到了空档，但贴底/贴顶两端探针都没量到——完全封闭的内部腔体，
        # 不是能用的孔。以前只看「任一高度命中」会把这种密封腔当成「孔径做到了」，
        # 拿一个根本没有开口的东西去匹配用户声明的孔需求。与「没测出来」不能混：
        # `hole_inconclusive` 为真时 mesh 已经把 `hole_through` 定成 None，这里不会误报。
        tests.append(
            ReqTest(
                group="hole:sealed-void",
                requirement="孔必须是开口的（不是封闭内部腔体）",
                kind="integrity",
                status=FAIL,
                expected="竖直贯通、两端至少一端开口",
                actual="剖面某些高度量到了空档，但两端都没开口（像是完全封闭的内腔）",
                fix_hint="检查 difference() 里挖孔的圆柱是否真的比模型在该方向上更长"
                         "（从一端到另一端）；内腔不是孔，东西插不进去也漏不出去。",
            )
        )
    for label, want, spec_name in holes:
        for key in (_nn(spec_name), _nn(label)):
            if key in targets:
                want = targets[key]
                break
        measured = [float(d) for d in profile if d is not None]
        if not measured:
            tests.append(
                ReqTest(
                    group=f"hole:{spec_name or label}",
                    requirement=f"{label}（孔径）",
                    kind="hole",
                    status=INVALID,
                    expected=f"{_fmt(want)}mm",
                    actual="剖面没量到任何孔（不限居中）",
                    fix_hint="确认模型里真的有竖直贯穿孔；横向孔/槽这类本判据看不见，"
                             "量不到时不下结论也不给分。",
                )
            )
            continue
        tol = max(0.6, want * 0.06)
        hit = min(measured, key=lambda d: abs(d - want))
        tests.append(
            ReqTest(
                group=f"hole:{spec_name or label}",
                requirement=f"{label}（孔径）",
                kind="hole",
                status=PASS if abs(hit - want) <= tol else FAIL,
                expected=f"{_fmt(want)}mm",
                actual=f"最接近的剖面 {_fmt(round(hit, 2))}mm（剖面 {'、'.join(_fmt(round(d, 1)) for d in measured[:6])}）",
                deviation_pct=round((hit - want) / want * 100.0, 2) if want else None,
                tolerance_mm=round(tol, 3),
                fix_hint=(
                    "参数名改对了不等于孔真变了：若源码写成表达式（d = outer - 2*wall），"
                    "请直接改 difference() 里那个挖孔圆柱的直径。"
                ),
            )
        )

    # ---------- 3.5 声明的间隙/夹口：减法功能特征到底切出来没有 ----------
    # 为什么必须单独判这一条（实测翻车）：夹口/卡槽/插槽这类特征是**减法**做出来的，
    # 刀具没切到实体时编译照样成功、STL 照样非空、连 warning 都没有，而所有既有指标全绿：
    # 零件数 1、封闭、包围盒对，壁厚甚至因为「根本没有腔」而显得“很充足”。
    # 实测交付给用户的那版手机夹：源码声明 clamp_gap=14，网格沿 Y 实测内部空隙 0.00mm，
    # 需求核对表却给出 9 项全过 / pass_rate 1.0 / 打印性 pass 85 分。
    # 判据只讲**自洽**：不评价设计好坏，只要求 AI 自己声明的尺寸在网格里量得到；
    # 因此这一条不需要用户先说清数字，也永远不会因为「用户没写公差」而误判。
    gaps_to_check = [
        (str(label), float(want), str(name))
        for label, want, name in gaps_declared
        if float(want) >= 3.0
    ]
    if gaps_to_check:
        interior = getattr(metrics, "interior_gaps", None)
        if interior is None:
            for label, want, name in gaps_to_check:
                tests.append(
                    ReqTest(
                        group=f"feature:gap-{name}",
                        requirement=f"{label}（间隙实测）",
                        kind="feature-gap",
                        status=INVALID,
                        expected=f"≥{_fmt(want)}mm 的被夹空档",
                        actual="内部空隙未能测量（面数超限或包围盒过小）",
                        fix_hint="模型过大时本判据跳过：不下结论也不给分，改用外观复核确认夹口存在。",
                    )
                )
        else:
            per_axis = [float(v) for v in (getattr(interior, "per_axis", (0.0, 0.0, 0.0)) or ())]
            longest = [
                float(v) for v in (getattr(interior, "longest_material", (0.0, 0.0, 0.0)) or ())
            ]
            scan_lines = int(getattr(interior, "lines", 0) or 0)
            best_gap = max(per_axis) if per_axis else 0.0
            detail = "、".join(
                f"{_axis_label(a)} {_fmt(round(v, 2))}mm" for a, v in enumerate(per_axis)
            )
            # 最长连续材料那一条轴 = 「这一刀该切进去却整条是实心」最可能的方向，
            # 光说「没空隙」AI 不知道往哪改，给出坐标轴与长度它才能对回自己的 difference()。
            solid_axis = max(range(len(longest)), key=lambda i: longest[i]) if longest else None
            for label, want, name in gaps_to_check:
                if best_gap >= want * 0.7:
                    tests.append(
                        ReqTest(
                            group=f"feature:gap-{name}",
                            requirement=f"{label}（间隙实测）",
                            kind="feature-gap",
                            status=PASS,
                            expected=f"≥{_fmt(want)}mm 的被夹空档",
                            actual=(
                                f"{detail}（扫描线 {scan_lines}²）；"
                                "本条只证明「存在空隙」，空隙位置对不对仍需外观复核"
                            ),
                        )
                    )
                    continue
                solid_note = ""
                if solid_axis is not None and longest[solid_axis] > 1.0:
                    solid_note = (
                        f"；沿{_axis_label(solid_axis)}最长有 {_fmt(round(longest[solid_axis], 1))}mm"
                        " 是连续材料（中间一点空档都没有）"
                    )
                tests.append(
                    ReqTest(
                        group=f"feature:gap-{name}",
                        requirement=f"{label}（间隙实测）",
                        kind="feature-gap",
                        status=FAIL,
                        expected=f"≥{_fmt(want)}mm 的被夹空档",
                        actual=f"{detail}{solid_note}",
                        fix_hint=(
                            f"声明了 {label}={_fmt(want)}mm，但网格里量不到这个尺度的空隙 —— "
                            "difference 的刀具那一刀很可能完全没切到实体（能编译、STL 非空、"
                            "无 warning，结果是一整块料）。两条硬性要求："
                            "① 空隙两侧的表面坐标必须直接由该参数决定（下侧顶面 = 下侧底 + jaw_h，"
                            "上侧底面 = 下侧顶面 + clamp_gap），**禁止**写成 "
                            "`+ clamp_gap + jaw_h - jaw_h` 这种自我抵消的表达式；"
                            "② 刀具必须和主体在同一坐标系 —— 主体若是 linear_extrude 沿 +Z 挤出的，"
                            "刀具写成 `rotate([-90,0,0]) linear_extrude(2D轮廓)` 会被搬到 z<0 的模型外，"
                            "什么都切不到；要么不 rotate，要么 rotate 后重算 translate。"
                        ),
                    )
                )

    # ---------- 4. 螺纹（需求里提到就要有真螺旋） ----------
    if _THREAD_TOKENS.search(requirement or ""):
        depth = getattr(metrics, "thread_depth_mm", None)
        is_helix = getattr(metrics, "thread_is_helix", None)
        if depth is None:
            tests.append(
                ReqTest(
                    group="thread:real",
                    requirement="有能拧得上的真螺纹",
                    kind="thread",
                    status=FAIL,
                    expected="沿轴线能量到螺旋牙型",
                    actual="量不到牙型起伏",
                    fix_hint="改用固定零件库 use <aiforge_parts.scad> 的 filter_adapter / thread_sleeve。",
                )
            )
        elif is_helix is None:
            # mesh 写明了「样本不足时返回 None（不猜）」（见 detect_helix_thread）。
            # 旧写法只判了 `is_helix is False` 一档，None 落到 else → PASS，
            # 还给用户写着「真螺旋」—— 把「一次都没判定成」粉饰成「判定通过」，
            # 而本模块的硬规矩是 invalid 算失败；且这与当年转接环翻车同构：
            # 同心环看着就有“螺纹纹理”，只是拧不进去。
            tests.append(
                ReqTest(
                    group="thread:real",
                    requirement="有能拧得上的真螺纹",
                    kind="thread",
                    status=INVALID,
                    expected="螺旋",
                    actual=f"有牙型（{_fmt(round(float(depth), 2))}mm），但样本不足，没能判定是不是螺旋",
                    fix_hint="这一项没能判定，不算通过：改用固定零件库 "
                             "use <aiforge_parts.scad> 的 filter_adapter / thread_sleeve，"
                             "或降低面数后重导再量。",
                )
            )
        elif is_helix is False:
            tests.append(
                ReqTest(
                    group="thread:real",
                    requirement="有能拧得上的真螺纹",
                    kind="thread",
                    status=FAIL,
                    expected="螺旋",
                    actual=f"有牙型（{_fmt(round(float(depth), 2))}mm）但是同心环",
                    fix_hint="rotate_extrude 做出来的是同心环，拧不进去；改用零件库的真螺旋。",
                )
            )
        else:
            tests.append(
                ReqTest(
                    group="thread:real",
                    requirement="有能拧得上的真螺纹",
                    kind="thread",
                    status=PASS,
                    expected="螺旋",
                    actual=f"牙深 {_fmt(round(float(depth), 2))}mm，真螺旋",
                )
            )

    # ---------- 5. 工艺类（咨询项：进清单、不进修复日志） ----------
    if limits is not None:
        bx = float(getattr(limits, "build_x", 0) or 0)
        by = float(getattr(limits, "build_y", 0) or 0)
        bz = float(getattr(limits, "build_z", 0) or 0)
        over = [
            f"{_axis_label(i)} {_fmt(round(bbox[i], 1))}>{_fmt(v)}"
            for i, v in ((0, bx), (1, by), (2, bz))
            if v and bbox[i] > v + 0.01
        ]
        tests.append(
            ReqTest(
                group="print:build-volume",
                requirement=f"放得进 {getattr(limits, 'printer_name', '所选打印机')}",
                kind="print",
                status=PASS if not over else FAIL,
                expected=f"≤{_fmt(bx)}×{_fmt(by)}×{_fmt(bz)}mm",
                actual=f"{_fmt(round(bbox[0], 1))}×{_fmt(round(bbox[1], 1))}×{_fmt(round(bbox[2], 1))}mm",
                fix_hint="缩小、改摆放方向，或分件打印后拼装。",
                advisory=True,
            )
        )
        if over:
            tests[-1].actual += "（超出：" + "、".join(over) + "）"
        min_wall = float(getattr(limits, "min_wall_mm", 0) or 0)
        if wall is not None and min_wall:
            p05 = float(getattr(wall, "p05_mm", 0.0) or 0.0)
            median = float(getattr(wall, "median_mm", 0.0) or 0.0)
            # 必须同时看中位数：射线在棱边/圆角处**天然**会量到极小值
            # （实测 Ø50×5 的垫片 p05=0.29mm、中位 5mm）。只拿 p05 判"壁厚不达标"
            # 是把测量噪声当成缺陷报给用户，第一轮基准就是这么翻车的。
            thin_overall = p05 < min_wall and median < min_wall * 2.0
            note = "" if thin_overall else "（p05 偏小来自棱边/圆角处的切向采样）"
            tests.append(
                ReqTest(
                    group="print:min-wall",
                    requirement=f"壁厚 ≥{_fmt(min_wall)}mm",
                    kind="print",
                    status=FAIL if thin_overall else PASS,
                    expected=f"≥{_fmt(min_wall)}mm",
                    actual=f"p05 {_fmt(round(p05, 2))}mm、中位 {_fmt(round(median, 2))}mm{note}",
                    fix_hint="加厚壁或加筋；壁厚由那个参数决定，不要只改标签。",
                    advisory=True,
                )
            )
        if bool(getattr(metrics, "too_thin_rough", False)):
            tests.append(
                ReqTest(
                    group="print:min-size",
                    requirement="整体外形不小于机型最小可成型尺寸",
                    kind="print",
                    status=FAIL,
                    expected=f"≥{_fmt(float(getattr(limits, 'rough_min_dim_mm', 0) or 0))}mm",
                    actual=f"最小边 {_fmt(round(min([v for v in bbox if v > 0] or [0]), 2))}mm",
                    fix_hint="按打印比例放大，或把该特征做粗。",
                    advisory=True,
                )
            )
        tests.append(
            ReqTest(
                group="print:on-plate",
                requirement="底面平整贴台（不用先垫东西）",
                kind="print",
                status=PASS if bool(getattr(metrics, "sits_on_plate", False)) else FAIL,
                expected="最低面是一整片平面",
                actual="已贴台" if getattr(metrics, "sits_on_plate", False) else "最低点是点/棱",
                fix_hint="加平底，或改成最大面朝下摆放。",
                advisory=True,
            )
        )
        support = getattr(metrics, "support", None)
        if support is not None:
            ratio = float(getattr(support, "ratio_pct", 0.0) or 0.0)
            hint = getattr(metrics, "orientation_hint", None)
            suggest = ""
            if isinstance(hint, dict) and hint.get("label"):
                suggest = f"建议方向：{hint['label']}"
            tests.append(
                ReqTest(
                    group="print:support",
                    requirement=f"支撑量不超过 {_fmt(SUPPORT_RATIO_PCT)}%",
                    kind="print",
                    status=PASS if ratio <= SUPPORT_RATIO_PCT else FAIL,
                    expected=f"≤{_fmt(SUPPORT_RATIO_PCT)}%",
                    actual=f"{_fmt(round(ratio, 1))}%" + (f"（{suggest}）" if suggest else ""),
                    fix_hint=suggest or "减小悬垂/桥接跨度，或改成自支撑形状（倒角、拱形）。",
                    advisory=True,
                )
            )

    tri = int(getattr(metrics, "triangles", 0) or 0)
    budget = int(getattr(limits, "max_triangles", 0) or 0) or TRIANGLE_BUDGET
    tests.append(
        ReqTest(
            group="print:triangle-budget",
            requirement="面数在切片软件舒服的范围",
            kind="print",
            status=PASS if tri <= budget else FAIL,
            expected=f"≤{_fmt(budget)} 三角面",
            actual=f"{tri:,} 三角面",
            fix_hint="降低 $fn / 减少阵列复制；细节靠打印精度而不是堆面。",
            advisory=True,
        )
    )

    # ---------- 6. 语义需求（程序测不了，摊开交给外观复核/人） ----------
    features = [str(x).strip() for x in (getattr(spec, "features", []) or []) if str(x).strip()]
    for i, text in enumerate(features[:8]):
        tests.append(
            ReqTest(
                group=f"semantic:feature-{i + 1}",
                requirement=text,
                kind="semantic",
                status=MANUAL,
                expected="结构存在且位置正确",
                actual="程序测不了；只有外观复核或人工能确认，确认前不算达成",
            )
        )
    return ReqSuite(tests=tests, dim_targets=dim_targets)


def _axis_label(axis: int) -> str:
    return {0: "X 方向", 1: "Y 方向", 2: "Z 方向/高度", HORIZONTAL_AXIS: "水平最大外形"}.get(
        axis, f"轴 {axis}"
    )
