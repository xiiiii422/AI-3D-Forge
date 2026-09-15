"""AI 3D Forge —— DeepSeek 客户端与提示词。

对应需求：DeepSeek 生成参数化 SCAD、自然语言修改、AI 打印性检查。

设计要点：
  * 提示词强制 AI 遵守固定骨架（参数区 + 元数据注释），这样「参数微调」和
    「自然语言改高度」都能在不重新问 AI 的情况下秒级完成。
  * 错误全部翻译成中文人话 + 明确的下一步操作，绝不把 HTTP 状态码糊到用户脸上。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncIterator

import httpx

from .config import DEFAULT_BASE_URL, DEFAULT_MODEL
from .design import SPEC_SYSTEM_PROMPT, DesignSpec, parse_spec_response, spec_user_prompt

if TYPE_CHECKING:  # 仅用于类型标注，避免运行时循环导入
    from .changes import ChangeContract, ChangeVerdict

log = logging.getLogger(__name__)

MAX_HISTORY_CHARS = 12_000

# 连接阶段失败后的重试间隔（秒）。只用于「连接根本没建立」这一类错误，
# 见 DeepSeekClient._post_with_connect_retry 里对可重试性的说明。
CONNECT_RETRY_DELAY_S = 1.5


def _describe_network_error(exc: Exception) -> str:
    """把网络异常翻译成一句**有信息**的话。

    踩过的坑：系统代理抖动时抛出的 httpx 异常 `str()` 是空字符串，
    界面上于是出现「网络请求失败：」——冒号后面什么都没有。
    用户拿这句话既判断不了是不是自己网络断了，也没法向客服描述。
    兜底用异常类型名，至少能区分「连不上 / 读到一半断了 / 协议错乱」。
    """
    detail = str(exc).strip()
    kind = type(exc).__name__
    if detail:
        return f"{kind}: {detail}"
    return f"{kind}（系统没有给出具体原因）"

# --------------------------------------------------------------------------
# 采样温度（objective ⑥：统一调低，减少随机漂移）
#
# 这套值不是随手定的，每一档都有理由：
#   SPEC      0.10  解析需求 → 要的是「照实提取」，不是发挥
#   GENERATE  0.20  从零设计 → 保留一点创造性，但受规格强约束
#   MODIFY    0.10  改现有代码 → 偏离原文的每一步都是风险
#   REPAIR    0.05  带差异重做 → 只准修被指出的地方
#   REVIEW    0.00  复核/判定 → 同一份改动必须得到同一个结论
#
# 为什么集中在常量里而不是散在各方法内：温度是**产品行为**的一部分
# （它决定「同一个需求会不会得到不同结果」），必须一眼看得全、改得一致，
# 也能被自检脚本断言，而不是埋在十个调用点里各自漂移。
# --------------------------------------------------------------------------
TEMPERATURE_SPEC = 0.10
TEMPERATURE_GENERATE = 0.20
TEMPERATURE_MODIFY = 0.10
TEMPERATURE_REPAIR = 0.05
TEMPERATURE_REVIEW = 0.0

# 能做图片复核的模型。实测（2026-09，对着 api.deepseek.com）：
#   deepseek-flash    收图并正确描述（64×64 红图答「红色」；
#                     桶形渲染图答「四棱台，上宽下窄」，与模型实际尺寸一致）
#   deepseek-v4-pro   收到图后回答「我这边无法查看你上传的图片」，静默忽略
# 所以图片复核固定用 flash，不跟随用户配置里的模型 ——
# 否则用户把模型换成 v4-pro 后，图片复核会退化成「模型说没问题」的假阳性。
VISION_MODEL = "deepseek-flash"
# 图片复核的 token 预算。**必须给足，这不是保守取值而是实测要求**：
# 这个模型在回答前会先思考，而思考过程也计入 max_tokens。实测同一个请求：
#   max_tokens=2048 -> completion_tokens=2048（全是 reasoning），
#                      finish_reason="length"，content 是空字符串
#   max_tokens=8192 -> completion_tokens=4186（reasoning 4099），正常给出结论
# 也就是说额度给小了会表现成「模型没意见」，而实际上是它话没说完 ——
# 这是最危险的一类失败：看起来通过了，其实什么都没检查。
VISION_MAX_TOKENS = 8192
# 还不够就再放大一档重试一次（实测正常请求在 8192 内能说完，留个兜底）
VISION_MAX_TOKENS_RETRY = 16384


class LlmError(RuntimeError):
    """带用户可读信息与修复建议的 AI 调用错误。"""

    def __init__(self, message: str, *, hint: str | None = None, code: str = "INTERNAL") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------
SCAD_CONVENTIONS = """你是资深 3D 打印结构设计师，同时精通 OpenSCAD 参数化建模。

输出要求（必须严格遵守，这是程序解析的前提）：
1. 只输出 OpenSCAD 代码，不要任何解释文字，不要 Markdown 代码围栏。
2. 代码第一行必须是模型名注释：// @name <中文模型名>
3. 第二行必须是设计说明：// @summary <一句话中文说明，不超过 40 字>
4. 随后是参数区，每个参数一行，格式固定为：
   // @param <变量名> <默认数值mm> <说明> [最小] [最大]
   例如：// @param height 100 支架高度 40 300
5. 参数区之后是「顶层数值赋值」，且必须使用与 @param 完全相同的变量名，
   例如：height = 100; 这些赋值必须位于所有 module/函数定义之前，且是纯数字。
6. 单位统一为毫米(mm)。模型必须落在打印平台上，即最低点 z = 0。
7. 模型必须是封闭实体（可用 difference/union 组合，但结果要实心），不能是薄面或空壳。
8. 只使用 OpenSCAD 内置几何体：cube / sphere / cylinder / polyhedron / circle /
   linear_extrude / rotate_extrude / polygon / text，以及 union / difference /
   intersection / hull / minkowski / translate / rotate / scale / mirror / color。
   严禁使用 import、surface、dxf_* 等任何读取外部文件的函数。
9. 代码末尾直接写出最终模型（顶层对象），例如 base(); 或组合后的 union 块。
10. 严禁输出 BOM 字符、全角标点、中文变量名。注释可用中文。

**打印机适配（必须遵守）**
用户会告诉你目标打印机与材料。你必须按那台机器的真实限制来设计，而不是按「一般打印机」：
- 模型三轴尺寸都不得超过给定成型范围。如果用户要求的尺寸放不下，
  请把它拆成可拼接的分件（用凸台/凹槽定位）或按比例缩小，并在 @summary 里说明。
- 所有壁厚不小于「最小壁厚」，所有孔径不小于「最小孔径」，
  所有凸起/浮雕不小于「最小凸起」。
- 每一层都必须有足够的材料支撑：悬垂角不超过给定上限，桥接跨度不超过给定上限；
  超过的地方要自动加倒角、圆角、支撑结构或改成阶梯状。
- 文件顶部使用给定的 $fn 值（它已按喷嘴直径与表面质量算好），不要自己乱改。
- 如果材料是 ABS/ASA/PA 而机型是开放式结构，避免大面积平坦薄壁（会翘边），
  改用加肋、圆角与分件。
- 配合尺寸（需要插进去的轴、需要穿过螺丝的孔）要按给定公差留间隙，
  不要把孔径写成与轴完全相等。

可打印性自查（生成前在脑内完成，不要输出过程）：
- 最薄处的厚度是否 ≥ 最小壁厚
- 所有孔是否 ≥ 最小孔径（竖直孔可更小）
- 重心是否稳定、底座是否足够宽（底座宽度建议 ≥ 高度的 40%）
- 是否避免了超过上限角的悬空
- 是否在整机成型范围内
- 圆角/倒角是否已加到承力处
- 容器类零件：掏空是否是**最后一步**（见下）

**壳体/容器的正确写法（强烈建议照此结构组织代码）**

容器类的通病是「先掏空、再加外壁特征」，结果加强筋从外壁穿进腔体。
把顺序反过来就永远不会错 —— 布尔运算替你收拾位置：

    // 1) 先做「实心外坯」：外形 + 所有外部特征都 union 进来
    module blank() {
        union() {
            hull() {                      // 渐变的形体：两层轮廓 + hull
                linear_extrude(0.01) body_profile(z = 0);
                translate([0, 0, height - 0.01])
                    linear_extrude(0.01) body_profile(z = height);
            }
            ribs();                       // 加强筋：哪怕摆得不准也没关系
            handle();                     // 其他外部特征同理
        }
    }
    // 2) 最后一步才掏空：内腔以外的部分会被自动切掉，筋正好停在内壁上
    module shell() {
        difference() {
            blank();
            translate([0, 0, wall]) body_cavity();   // 内腔（比外形小两倍壁厚）
        }
    }

摆加强筋时不要用「固定半径 + rotate 转一圈」：矩形/扇形壳体的外壁到中轴
距离随角度变化，那样只有少数几根贴住壁、其余会飘进腔体。用上面的顺序，
或者逐条边单独算出该处的壁面坐标。

**`linear_extrude` 的输入必须是平面 2D 图形 —— 旋转要放在它外面**

这是最容易被静默丢掉一整块几何的写法。OpenSCAD 只会打一句 warning、
退出码仍是 0，你从返回值里看不出任何异常：

    // 错误：把 3D 的 rotate 放在 linear_extrude 的子节点位置
    // OpenSCAD 会把这块几何整个丢掉（Scaling a 2D object with 0 - removing object），
    // 结果模型只剩其它部分 —— 用户要 100mm 高的支架，拿到一块 8.7mm 的板
    linear_extrude(height = 30) rotate([90, 0, 90]) square([20, 40]);

    // 正确：先在平面里挤出成体，再整体旋转到目标朝向
    rotate([90, 0, 90]) linear_extrude(height = 30) square([20, 40]);

    // 也正确：只在平面内转角度（只绕 Z）时，rotate 放在里面没问题
    linear_extrude(height = 30) rotate([0, 0, 90]) square([20, 40]);

规则：`rotate` 的前两个分量只要不是 0，就**必须**放在 `linear_extrude` 外面。"""


def design_system_prompt() -> str:
    return SCAD_CONVENTIONS


def create_user_prompt(requirement: str, extra: str | None = None, printer_block: str | None = None) -> str:
    body = ""
    if printer_block:
        body += f"目标打印机与工艺约束：\n{printer_block}\n\n"
    body += f"请设计以下 3D 打印模型：\n\n{requirement.strip()}\n"
    if extra:
        body += f"\n补充要求：{extra.strip()}\n"
    body += "\n请直接输出符合上述规范、并且能在这台打印机上直接打印成功的完整 OpenSCAD 代码。"
    return body


def modify_user_prompt(current_scad: str, instruction: str, printer_block: str | None = None) -> str:
    scad = current_scad
    if len(scad) > MAX_HISTORY_CHARS:
        scad = scad[:MAX_HISTORY_CHARS] + "\n// …（代码过长已截断）"
    body = ""
    if printer_block:
        body += f"目标打印机与工艺约束：\n{printer_block}\n\n"
    body += (
        "下面是一个已经可以正常打印的 OpenSCAD 模型代码：\n\n"
        f"```openscad\n{scad}\n```\n\n"
        f"用户提出了修改要求：{instruction.strip()}\n\n"
        "请在保持原有结构、参数命名和整体风格的前提下完成修改，"
        "并输出修改后的完整 OpenSCAD 代码（仍然必须遵守全部输出规范）。"
        "如果该修改正好对应一个已有参数，请只改该参数的默认值。"
        "修改后仍需满足上面列出的打印机约束（尺寸、壁厚、最小特征、悬垂）。"
    )
    return body


REVIEW_SYSTEM_PROMPT = """你是资深的 3D 打印工艺工程师，负责在打印前审查模型是否可打印。

你会收到三份材料：
1. 模型的 OpenSCAD 源码
2. 程序对导出的 STL 文件做的客观测量数据（尺寸、体积、封闭性等）
3. **用户实际使用的打印机规格与工艺约束**（成型范围、喷嘴、最小壁厚、悬垂/桥接上限、材料、腔体）

要求：
- 一切结论都必须以「这台具体的打印机 + 这种材料」为基准，不要用泛泛的通用经验值。
  同样一个模型在不同机器上结论可以完全不同（例如 1.2mm 薄壁在 0.4mm 喷嘴上是临界，
  在 0.8mm 喷嘴上则完全不可接受；ABS 在开放机身上会翘边，在封闭腔体里没问题）。
- 只依据这三份材料下结论，不要凭空猜测打印机型号、切片参数或使用场景。
- 如果测量数据显示已经封闭、尺寸在成型范围内、贴平台、壁厚达标，就应当给出正面结论，
  不要为了显得严谨而编造问题。
- 每条问题都必须指出它在**这台机器上**为什么是问题，并给出可执行的修改建议
  （例如「把壁厚从 1.2mm 增加到 1.6mm」或「改用 0.6mm 喷嘴，或把孔径放大到 3.0mm」）。
- 如果模型超出成型范围，明确给出处理方案：缩小到多少、怎么旋转摆放、或如何分件。
- 输出必须是合法的 JSON，不要 Markdown 围栏，不要额外文字。

JSON 结构：
{
  "score": 0-100 的整数，针对这台打印机的可打印性评分,
  "verdict": "pass" | "warn" | "fail",
  "summary": "一句话中文结论，40 字以内，要点明是在哪台机器上",
  "issues": [
    {
      "severity": "info" | "warn" | "error",
      "title": "问题标题，10 字以内",
      "detail": "问题说明，说明为什么在这台机器上会影响打印",
      "suggestion": "具体怎么改，要可执行"
    }
  ]
}

判定标准参考：
- 已封闭实体、最小尺寸 ≥ 该机型最小壁厚、能放进该机型成型范围、贴平台 → pass（score ≥ 80）
- 存在可接受的小瑕疵（需要支撑、轻微悬空、接近公差下限）→ warn（60 ≤ score < 80）
- 非封闭实体、体积为 0、超出成型范围且未分件、壁厚低于该机型下限、材料与腔体不匹配
  → fail（score < 60）
- issues 为空时说明模型在这台机器上没有明显问题。"""


def review_user_prompt(
    scad: str,
    metrics_summary: str,
    user_requirement: str | None,
    printer_block: str | None = None,
) -> str:
    scad_trimmed = scad if len(scad) <= MAX_HISTORY_CHARS else scad[:MAX_HISTORY_CHARS] + "\n// …（已截断）"
    parts = []
    if printer_block:
        parts.append(f"用户实际使用的打印机与工艺约束：\n{printer_block}")
    if user_requirement:
        parts.append(f"用户的设计需求：{user_requirement.strip()}")
    parts.append(f"程序测量的 STL 数据：\n{metrics_summary}")
    parts.append(f"模型源码：\n```openscad\n{scad_trimmed}\n```")
    parts.append("请针对上面这台打印机，输出 JSON 格式的打印性检查结论。")
    return "\n\n".join(parts)


CHANGE_REVIEW_SYSTEM_PROMPT = """你是改动的复核员。用户对 3D 模型提了一句修改要求，程序已经照着改了一版。
你的任务是**检查这版改动是否真的做到了用户要的事**，以及有没有顺手破坏别的东西。

判定要点：
1. requested_change_applied：用户点名要改的那件事，在**新代码**里是否真的生效了。
   注意：改注释、改 @param 文字说明、改参数名，都不算「生效」；
   必须是模型的实际结构或尺寸真的会跟着变。
2. functional_ok：新代码是否仍然是一个能用的、结构完整的模型。
   特别注意「表面改了、其实改坏了」：例如把支撑结构改成了悬空、把能站住的底座改窄到站不稳、
   把承力处削薄、把零件改成装不起来。这类问题参数上往往看不出来，只能靠你比对两份代码判断。
3. summary：一句话中文说明你的结论（40 字内）。
4. concerns：你发现的疑点，逐条写清楚「哪一处、为什么可疑」。没有就给空数组。

铁律：
- 只依据给你的两份代码和改动契约判断，不要臆测用户没提过的需求。
- 不确定就写进 concerns，不要编造结论，也不要说「看起来没问题」来敷衍。
- 只输出 JSON，不要 Markdown 围栏，不要解释。

JSON 结构：
{
  "requested_change_applied": true/false,
  "functional_ok": true/false,
  "summary": "一句话结论",
  "concerns": ["疑点 1", "疑点 2"]
}"""


def change_review_user_prompt(
    before_scad: str,
    after_scad: str,
    contract: "ChangeContract",
    spec: "DesignSpec | None" = None,
) -> str:
    """构造「复核这次改动」的用户消息。

    为什么要让 AI 复核一遍（程序已经核对过了）：
    程序核对只能比较参数值，看不出「参数对了但结构被改坏」——
    例如把底座改窄到站不稳、把承力处削薄、把支撑改到悬空。
    这类问题在参数 diff 里完全不可见，只有把两份代码放在一起比对才能发现。
    """
    before = before_scad if len(before_scad) <= MAX_HISTORY_CHARS else before_scad[:MAX_HISTORY_CHARS] + "\n// …（已截断）"
    after = after_scad if len(after_scad) <= MAX_HISTORY_CHARS else after_scad[:MAX_HISTORY_CHARS] + "\n// …（已截断）"
    parts = [f"用户的修改要求：{contract.instruction.strip()}"]
    parts.append("本次改动契约（程序据此授权/禁止）：\n" + contract.describe())
    if spec is not None and not spec.is_empty():
        parts.append("该设计的既定规格（必须继续满足）：\n" + spec.to_prompt())
    parts.append(f"改动前的代码：\n```openscad\n{before}\n```")
    parts.append(f"改动后的代码：\n```openscad\n{after}\n```")
    parts.append("请判断这次改动是否真的达成了用户的要求，以及有没有改坏别处，输出 JSON。")
    return "\n\n".join(parts)


def parse_change_review_json(text: str) -> dict[str, object]:
    """从 AI 回复里稳健地取出改动复核 JSON。

    解析失败时返回「无法判定」而不是「没问题」：复核的默认立场是**怀疑**，
    拿不到结论时不能让一个可能有问题的改动因为解析失败而蒙混过关。
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
        log.warning("改动复核返回的不是合法 JSON，按「无法判定」处理")
        return {
            "requested_change_applied": None,
            "functional_ok": None,
            "summary": "AI 未能给出结构化复核结论（程序化校验结果仍然有效）。",
            "concerns": ["AI 复核回复格式异常"],
        }
    if not isinstance(data, dict):
        data = {}

    def as_tri_state(value: object) -> bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "yes", "1", "是"):
                return True
            if lowered in ("false", "no", "0", "否"):
                return False
        return None

    concerns = data.get("concerns")
    return {
        "requested_change_applied": as_tri_state(data.get("requested_change_applied")),
        "functional_ok": as_tri_state(data.get("functional_ok")),
        "summary": str(data.get("summary") or "").strip()[:200],
        "concerns": [str(c).strip() for c in concerns if str(c).strip()][:6]
        if isinstance(concerns, list)
        else [],
    }


GEOMETRY_REPAIR_SYSTEM_PROMPT = """你是 3D 打印结构修复员。

程序已经把模型导出成 STL 并做了**实测**，发现了几何上不成立的地方。
这些不是猜测，是量出来的事实（连通分量数、水平截面上的材料环嵌套关系）。
你的任务：只修这一处，其余设计意图、参数名、参数值、整体造型全部保持不变。

两类问题的标准修法：

一、模型由多个互不相连的零件组成
   说明有些结构只是「摆得近」，没有真正相交。
   **首选修法是换画法，而不是微调坐标**（实测：只改坐标往往修不好）：
   - 把贴在表面上的特征（齿纹、凸点、卡槽、加强筋）改成**画进 2D 截面**：
     先在 2D 里把它们与截面 `union()` 成一个完整轮廓，再统一 `linear_extrude()` / `hull()`
     成体 —— 这样它们天生就是同一块料，不可能散开。
   - 如果必须留在 3D 里，就把它们的定位坐标整体往主体内部**推 1mm 以上**，
     并确认真的与主体有体积重叠（差 0.01mm 就是两个零件）。
   - 阵列出来的特征（for 循环里生成的）最容易犯这个错，优先重画它们。

二、内腔里出现不与内壁相连的材料
   说明外壁特征（加强筋/凸台/把手等）从外壁**穿进了腔体内部**。修法（首选）：
   - 把「掏空」改成整个模型的**最后一步**：先把外形和所有外壁特征 union 成
     一个实心外坯，最后统一 difference 掉内腔。这样布尔运算会自动把伸进腔体的
     部分切掉，特征正好停在内壁面上。
   - 如果特征是按「固定半径 + rotate 转一圈」摆的，而壳体不是圆的
     （矩形/扇形/椭圆），那就必须改成第 1 种顺序 —— 因为壁面到中轴的距离
     随角度变化，固定半径必然有一半会飘进腔里。

输出要求与平时一致：只输出完整的 OpenSCAD 代码，不要解释。"""


def geometry_repair_user_prompt(scad: str, problem: str, attempt: int = 1) -> str:
    """几何不成立（散件/穿腔）后，把实测证据回灌给 AI 做一次定点修复。"""
    trimmed = scad if len(scad) <= MAX_HISTORY_CHARS else scad[:MAX_HISTORY_CHARS] + "\n// …（已截断）"
    retry_note = (
        "这是第 2 次修复：上一版仍然有散件。**请换一种画法**，不要再微调原来的坐标 ——"
        "把散件那些结构改成「先画进 2D 截面轮廓、再统一挤出」的写法，"
        "让它们天生就是同一块料（例如齿纹：在 2D 里 union 出带齿的完整截面再 linear_extrude）。\n\n"
        if attempt >= 2
        else ""
    )
    return (
        "下面这份 OpenSCAD 代码能正常编译，但导出的模型在几何上不成立。\n\n"
        f"代码：\n```openscad\n{trimmed}\n```\n\n"
        f"程序对导出的 STL 实测结果：\n{problem.strip()[:1600]}\n\n"
        f"{retry_note}"
        "请只修复上述问题，保持参数名、参数值、整体造型和其余结构不变，"
        "输出完整的、可直接编译的 OpenSCAD 代码。只输出代码本身，不要解释。"
    )


SNAPSHOT_REVIEW_SYSTEM_PROMPT = """你是 3D 打印结构复核员。你会看到同一个模型在固定视角下的渲染图。

**只看图上真实可见的东西。** 你的价值在于「程序测不出来、但人一眼能看出」的问题，
所以不要把程序已经量过的数字再复述一遍。

重点看这四类：
1. 形体是否和需求描述相符（比如要求「上宽下窄的扇形桶身」，图上却是上下一样宽的方盒）；
2. 有没有**悬空/断裂**的结构 —— 某块东西和主体之间看得出脱离；
3. 有没有**特征长错位置** —— 比如本该在外壁上的加强筋，出现在腔体内部；
4. 比例是否明显失调（极度细长、头重脚轻、薄得像纸）。

输出要求：
- **第一行必须是机器可读的结论**，二选一，不要加别的字：
  `结论：有问题` 或 `结论：无明显问题`
- 第一行之后，用中文说清楚问题，3 句话以内；没问题就不用再写。
- 每个问题都要说清「在图上的什么位置、看到了什么」。看不清或不确定就说不确定。
- 不要复述需求原文，不要给改进建议清单，不要客套话。
- **不要为了凑内容编问题**：没有确实看得见的问题就必须写「无明显问题」。"""


# 图片复核结论的第一行标记。用它把「模型没意见」和「模型话没说完」区分开：
# 只有明确写了「有问题」才算数，其余一律当成无结论。
SNAPSHOT_VERDICT_FLAG = "结论：有问题"
SNAPSHOT_VERDICT_CLEAR = "结论：无明显问题"


def parse_snapshot_verdict(text: str) -> bool:
    """图片复核是否明确报告了问题。

    注意判据方向：**只有明确说「有问题」才返回 True**。
    空回复、格式跑偏、说不确定，全部算「没有结论」——
    因为把「没结论」当成「没问题」会静默放过缺陷，代价远大于多问一次。
    """
    head = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    head = head.replace(" ", "").replace("**", "")
    if SNAPSHOT_VERDICT_CLEAR.replace(" ", "") in head:
        return False
    return SNAPSHOT_VERDICT_FLAG.replace(" ", "") in head


def snapshot_review_user_prompt(requirement: str, metrics_summary: str = "") -> str:
    """图片复核的用户消息：告诉它用户要什么，然后让它看图。"""
    parts = [f"用户的需求：{requirement.strip()[:600]}"]
    if metrics_summary:
        parts.append(
            "程序对模型做的实测（这些数字已经核对过对应的问题，不用你重复）：\n"
            + metrics_summary.strip()[:1200]
        )
    parts.append(
        "下面是该模型在固定视角下的渲染图（正交投影、灰度着色）。请按系统提示复核。"
    )
    return "\n\n".join(parts)


def repair_user_prompt(scad: str, problem: str) -> str:
    """编译/校验失败后，把真实报错回灌给 AI 做一次自动修复（第四十九节的「错误恢复」）。"""
    scad_trimmed = scad if len(scad) <= MAX_HISTORY_CHARS else scad[:MAX_HISTORY_CHARS] + "\n// …（已截断）"
    return (
        "下面这份 OpenSCAD 代码无法生成 3D 模型，请修复它。\n\n"
        f"代码：\n```openscad\n{scad_trimmed}\n```\n\n"
        f"实际报错信息：\n{problem.strip()[:1200]}\n\n"
        "请仔细分析报错原因（常见原因：括号或分号缺失、变量在赋值前被使用、"
        "difference 减法对象没有正确重叠、生成了非实体几何、单位不是毫米）。"
        "修复后输出完整的、可以直接编译成功的 OpenSCAD 代码，并遵守全部输出规范。"
        "只输出代码本身，不要解释。"
    )


# --------------------------------------------------------------------------
# 响应解析
# --------------------------------------------------------------------------
def parse_review_json(text: str) -> dict[str, object]:
    """从 AI 回复里稳健地取出打印性检查 JSON；失败则给出温和的兜底结论。"""
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
        log.warning("打印性检查返回的不是合法 JSON，使用兜底结论")
        return {
            "score": 70,
            "verdict": "warn",
            "summary": "AI 未能给出结构化结论，请参考下方的测量数据自行判断。",
            "issues": [
                {
                    "severity": "info",
                    "title": "AI 回复格式异常",
                    "detail": "AI 的检查结论不是预期的结构，已按中性处理。",
                    "suggestion": "可以点击「重新生成」再试一次。",
                }
            ],
        }
    if not isinstance(data, dict):
        data = {}

    issues_raw = data.get("issues")
    issues: list[dict[str, str]] = []
    if isinstance(issues_raw, list):
        for item in issues_raw[:12]:
            if not isinstance(item, dict):
                continue
            severity = str(item.get("severity", "info")).lower()
            if severity not in ("info", "warn", "error"):
                severity = "info"
            issues.append(
                {
                    "severity": severity,
                    "title": str(item.get("title") or "注意")[:40],
                    "detail": str(item.get("detail") or "")[:600],
                    "suggestion": str(item.get("suggestion") or "")[:600],
                }
            )

    try:
        score = int(float(data.get("score", 70)))
    except (TypeError, ValueError):
        score = 70
    score = max(0, min(100, score))

    verdict = str(data.get("verdict", "")).lower()
    if verdict not in ("pass", "warn", "fail"):
        verdict = "pass" if score >= 80 else ("warn" if score >= 60 else "fail")

    return {
        "score": score,
        "verdict": verdict,
        "summary": str(data.get("summary") or "")[:200] or "已完成打印性检查。",
        "issues": issues,
    }


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------
@dataclass
class ChatResult:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str | None = None


def _friendly_http_error(status: int, body: str, base_url: str) -> LlmError:
    snippet = (body or "").strip()[:300]
    if status == 401:
        return LlmError(
            "DeepSeek API Key 无效或已失效。",
            hint="请在「设置 → AI 服务」中重新填入正确的 API Key（可在 platform.deepseek.com 创建）。",
            code="API_KEY_INVALID",
        )
    if status == 402:
        return LlmError(
            "DeepSeek 账户余额不足，无法继续生成。",
            hint="请前往 DeepSeek 开放平台充值后重试。",
            code="API_KEY_INVALID",
        )
    if status == 429:
        return LlmError(
            "请求过于频繁，已被 DeepSeek 限流。",
            hint="请稍等十几秒再试，或减少同时进行的任务。",
            code="API_UNREACHABLE",
        )
    if status >= 500:
        return LlmError(
            "DeepSeek 服务暂时不可用。",
            hint="这是对方服务端的问题，请稍后重试。",
            code="API_UNREACHABLE",
        )
    if status == 404:
        return LlmError(
            f"接口地址不正确（{base_url}）。",
            hint="请在「设置 → AI 服务」中把接口地址恢复为 https://api.deepseek.com。",
            code="API_UNREACHABLE",
        )
    return LlmError(
        f"调用 DeepSeek 失败（HTTP {status}）。{snippet}",
        hint="可稍后重试；若持续失败请检查网络或代理设置。",
        code="INTERNAL",
    )


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout_s: int = 180,
    ) -> None:
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self.timeout = httpx.Timeout(connect=20.0, read=float(timeout_s), write=60.0, pool=20.0)

    # --- 基础请求 --------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _post_with_connect_retry(self, url: str, payload: dict[str, object]) -> httpx.Response:
        """POST 一次；**只在「连都连不上」时**重试一次。

        为什么只重试连接阶段：连接没建立起来 = 请求根本没发出去，
        重试不会重复计费。反过来，`ReadError` / `RemoteProtocolError`
        是"请求已发出、响应断了"——那时模型可能已经生成完并计费，
        盲目重试会让用户为同一句话付两次钱，所以那类错误一律直接报错。

        （实测触发场景：系统代理抖动那次，抛出的异常 `str()` 是空的，
        界面上只显示「网络请求失败：」，用户完全无从下手 —— 所以
        下面报错时必须把**异常类型**和**目标地址**都写出来。）
        """
        attempts = 2
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    return await client.post(url, headers=self._headers(), json=payload)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(CONNECT_RETRY_DELAY_S)
                    continue
            except httpx.TimeoutException as exc:
                raise LlmError(
                    "等待 DeepSeek 响应超时。",
                    hint="已生成的内容较多或网络较慢，请重试；也可以在设置中调大超时时间。",
                    code="API_UNREACHABLE",
                ) from exc
            except httpx.HTTPError as exc:
                raise LlmError(
                    f"网络请求失败：{_describe_network_error(exc)}。",
                    hint="请检查网络后重试；若使用代理或公司网络，请确认能访问 api.deepseek.com。",
                    code="API_UNREACHABLE",
                ) from exc
        assert last is not None
        raise LlmError(
            f"无法连接到 DeepSeek 服务器（{self.base_url}）：{_describe_network_error(last)}。",
            hint="请检查网络连接；若使用代理或公司网络，请在「设置」中确认可以访问 api.deepseek.com。",
            code="API_UNREACHABLE",
        ) from last

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = TEMPERATURE_GENERATE,
        max_tokens: int = 8192,
        json_mode: bool = False,
    ) -> ChatResult:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        url = f"{self.base_url}/chat/completions"
        response = await self._post_with_connect_retry(url, payload)
        if response.status_code >= 400:
            raise _friendly_http_error(response.status_code, response.text, self.base_url)

        try:
            data = response.json()
        except ValueError as exc:
            raise LlmError("DeepSeek 返回的内容无法解析。", hint="请稍后重试。") from exc

        choices = data.get("choices") or []
        if not choices:
            raise LlmError(
                "DeepSeek 没有返回任何内容。",
                hint="请重试；若持续出现，请换一种更具体的描述。",
            )
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        if not content and message.get("reasoning_content"):
            content = message["reasoning_content"]
        usage = data.get("usage") or {}
        return ChatResult(
            content=content,
            model=str(data.get("model") or self.model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            finish_reason=choices[0].get("finish_reason"),
        )

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = TEMPERATURE_GENERATE,
        max_tokens: int = 8192,
    ) -> AsyncIterator[str]:
        """流式返回增量文本（用于界面上的实时进度感知）。"""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        url = f"{self.base_url}/chat/completions"
        # 连接阶段照样只重试一次「连都连不上」；一旦开始收流就不再重试 ——
        # 半截的流重放会让用户看到重复内容，且可能重复计费。
        for attempt in range(2):
            started = False
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    async with client.stream("POST", url, headers=self._headers(), json=payload) as response:
                        started = True
                        if response.status_code >= 400:
                            body = (await response.aread()).decode("utf-8", errors="replace")
                            raise _friendly_http_error(response.status_code, body, self.base_url)
                        async for line in response.aiter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            chunk = line[5:].strip()
                            if chunk == "[DONE]":
                                break
                            try:
                                parsed = json.loads(chunk)
                            except ValueError:
                                continue
                            for choice in parsed.get("choices") or []:
                                delta = choice.get("delta") or {}
                                text = delta.get("content")
                                if text:
                                    yield str(text)
                return
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if not started and attempt == 0:
                    await asyncio.sleep(CONNECT_RETRY_DELAY_S)
                    continue
                raise LlmError(
                    f"无法连接到 DeepSeek 服务器（{self.base_url}）：{_describe_network_error(exc)}。",
                    hint="请检查网络连接；若使用代理或公司网络，请在「设置」中确认可以访问 api.deepseek.com。",
                    code="API_UNREACHABLE",
                ) from exc
            except httpx.TimeoutException as exc:
                raise LlmError(
                    "等待 DeepSeek 响应超时。",
                    hint="已生成的内容较多或网络较慢，请重试；也可以在设置中调大超时时间。",
                    code="API_UNREACHABLE",
                ) from exc
            except httpx.HTTPError as exc:
                raise LlmError(
                    f"网络请求失败：{_describe_network_error(exc)}。",
                    hint="请检查网络后重试；若使用代理或公司网络，请确认能访问 api.deepseek.com。",
                    code="API_UNREACHABLE",
                ) from exc

    # --- 业务方法 --------------------------------------------------------
    async def parse_design_spec(
        self,
        requirement: str,
        previous: "DesignSpec | None" = None,
    ) -> "DesignSpec":
        """把口语化需求解析成结构化设计规格。

        这是整个流程里最关键的一次调用：后面生成什么、改什么、怎么验收，
        全都以这份规格为准。温度压到 0.1 —— 这是「提取」任务，
        需要的是忠实与稳定，不是创造力。
        """
        from .design import parse_spec_response, spec_user_prompt

        result = await self.chat(
            [
                {"role": "system", "content": SPEC_SYSTEM_PROMPT},
                {"role": "user", "content": spec_user_prompt(requirement, previous)},
            ],
            temperature=TEMPERATURE_SPEC,
            max_tokens=2048,
            json_mode=True,
        )
        return parse_spec_response(result.content)

    async def generate_from_spec(
        self,
        requirement: str,
        spec: "DesignSpec",
        printer_block: str | None = None,
    ) -> ChatResult:
        from .changes import initial_prompt

        return await self.chat(
            [
                {"role": "system", "content": design_system_prompt()},
                {"role": "user", "content": initial_prompt(requirement, spec, printer_block)},
            ],
            temperature=TEMPERATURE_GENERATE,
            max_tokens=8192,
        )

    async def modify_with_contract(
        self,
        current_scad: str,
        contract: "ChangeContract",
        spec: "DesignSpec | None" = None,
        printer_block: str | None = None,
    ) -> ChatResult:
        from .changes import modify_prompt

        return await self.chat(
            [
                {"role": "system", "content": design_system_prompt()},
                {
                    "role": "user",
                    "content": modify_prompt(current_scad, contract, spec, printer_block),
                },
            ],
            temperature=TEMPERATURE_MODIFY,
            max_tokens=8192,
        )

    async def repair_change(
        self,
        original_scad: str,
        bad_scad: str,
        verdict: "ChangeVerdict",
        contract: "ChangeContract",
    ) -> ChatResult:
        from .changes import repair_prompt

        return await self.chat(
            [
                {"role": "system", "content": design_system_prompt()},
                {
                    "role": "user",
                    "content": repair_prompt(original_scad, bad_scad, verdict, contract),
                },
            ],
            temperature=TEMPERATURE_REPAIR,
            max_tokens=8192,
        )

    async def review_change(
        self,
        before_scad: str,
        after_scad: str,
        contract: "ChangeContract",
        spec: "DesignSpec | None" = None,
    ) -> dict[str, object]:
        """AI 侧的改动复核（程序化校验之外的第二道）。

        温度取 0.0：复核要的是稳定判断，不是创造力 —— 同一份改动不该有时说行有时说不行。
        """
        result = await self.chat(
            [
                {"role": "system", "content": CHANGE_REVIEW_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": change_review_user_prompt(before_scad, after_scad, contract, spec),
                },
            ],
            temperature=TEMPERATURE_REVIEW,
            max_tokens=1200,
        )
        return parse_change_review_json(result.content)

    async def generate_scad(self, requirement: str, extra: str | None = None, printer_block: str | None = None) -> ChatResult:
        return await self.chat(
            [
                {"role": "system", "content": design_system_prompt()},
                {"role": "user", "content": create_user_prompt(requirement, extra, printer_block)},
            ],
            temperature=TEMPERATURE_GENERATE,
            max_tokens=8192,
        )

    async def modify_scad(self, current_scad: str, instruction: str, printer_block: str | None = None) -> ChatResult:
        return await self.chat(
            [
                {"role": "system", "content": design_system_prompt()},
                {"role": "user", "content": modify_user_prompt(current_scad, instruction, printer_block)},
            ],
            temperature=TEMPERATURE_MODIFY,
            max_tokens=8192,
        )

    async def repair_scad(self, current_scad: str, problem: str) -> ChatResult:
        return await self.chat(
            [
                {"role": "system", "content": design_system_prompt()},
                {"role": "user", "content": repair_user_prompt(current_scad, problem)},
            ],
            temperature=TEMPERATURE_REPAIR,
            max_tokens=8192,
        )

    async def repair_geometry(self, current_scad: str, problem: str, attempt: int = 1) -> ChatResult:
        """几何不成立（散件 / 特征穿腔）时的定点修复。

        与 repair_scad 分开的理由：那个是「编译不过」的修复，这个是
        「编译过了、但实测几何不成立」的修复，给模型的证据和修法完全不同。
        """
        return await self.chat(
            [
                {"role": "system", "content": GEOMETRY_REPAIR_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": geometry_repair_user_prompt(current_scad, problem, attempt),
                },
            ],
            temperature=TEMPERATURE_REPAIR,
            max_tokens=8192,
        )

    async def review_snapshot(
        self,
        images: list[bytes],
        requirement: str,
        metrics_summary: str = "",
    ) -> str:
        """把渲染出来的模型图片交给多模态模型看一眼，返回它的观察结论。

        **为什么要做**：所有确定性指标全绿，模型仍然可能是错的 ——
        实测过的桌面垃圾桶：6 根外壁加强筋里 4 根从外壁穿进了桶内腔，
        而 `solid_parts==1`、`watertight==True`、包围盒体积全部正常。
        这个问题渲染一张图一眼就能看出来。

        **两个必须注意的接口事实**（都是实测出来的，不是推测）：
          1. 只有 `deepseek-flash` 真的会看图。实测 `deepseek-v4-pro` 收到图后
             回答「我这边无法查看你上传的图片」—— 它会静默忽略图片。
             所以这里固定用视觉可用的模型，而不是用户配置里的那个。
          2. 这类模型的思考过程也计入 max_tokens。实测 max_tokens=300 时，
             589 个 reasoning token 把额度吃光，`content` 直接是空字符串 ——
             看起来像「模型没问题」，其实是没说完。所以这里给足额度，
             并且把空回复如实当成「无法判定」，绝不当成「没问题」。
        """
        if not images:
            return ""
        content: list[dict[str, object]] = [
            {"type": "text", "text": snapshot_review_user_prompt(requirement, metrics_summary)}
        ]
        for raw in images:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
                    },
                }
            )
        payload: dict[str, object] = {
            "model": VISION_MODEL,
            "messages": [{"role": "user", "content": content}],
            "temperature": TEMPERATURE_REVIEW,
            "max_tokens": VISION_MAX_TOKENS,
            "stream": False,
        }
        url = f"{self.base_url}/chat/completions"
        for budget in (VISION_MAX_TOKENS, VISION_MAX_TOKENS_RETRY):
            payload["max_tokens"] = budget
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(url, headers=self._headers(), json=payload)
            except httpx.HTTPError as exc:
                raise LlmError(
                    f"复核模型图片时网络失败：{exc}",
                    hint="图片复核失败不影响模型本身，可稍后重试。",
                    code="API_UNREACHABLE",
                ) from exc
            if response.status_code >= 400:
                raise _friendly_http_error(response.status_code, response.text, self.base_url)
            try:
                data = response.json()
            except ValueError as exc:
                raise LlmError("图片复核返回的内容无法解析。", hint="请稍后重试。") from exc
            choices = data.get("choices") or []
            if not choices:
                return ""
            choice = choices[0]
            message = choice.get("message") or {}
            text = (message.get("content") or "").strip()
            if text:
                return text
            # 空正文 + finish_reason=length = 思考过程把额度吃光了，不是「没问题」。
            # 放大额度重试一次；仍然为空就返回空串，由调用方如实说成「没有结论」。
            if choice.get("finish_reason") != "length" or budget >= VISION_MAX_TOKENS_RETRY:
                return ""
            log.info(
                "图片复核额度用尽（reasoning 吃满 %d tokens），放大额度重试", budget
            )
        return ""

    async def review_printability(
        self,
        scad: str,
        metrics_summary: str,
        user_requirement: str | None = None,
        printer_block: str | None = None,
    ) -> dict[str, object]:
        result = await self.chat(
            [
                {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": review_user_prompt(scad, metrics_summary, user_requirement, printer_block),
                },
            ],
            temperature=TEMPERATURE_REVIEW,
            max_tokens=2048,
            json_mode=True,
        )
        return parse_review_json(result.content)


# --------------------------------------------------------------------------
# 连通性测试（设置页面的「测试连接」）
# --------------------------------------------------------------------------
async def test_api_key(api_key: str, base_url: str = DEFAULT_BASE_URL, model: str = DEFAULT_MODEL) -> dict[str, object]:
    """真实调用一次 DeepSeek，验证 Key 是否可用。返回结构化结果而不抛异常。"""
    import time

    if not api_key or not api_key.strip():
        return {"ok": False, "message": "尚未填写 API Key。", "model": None, "latency_ms": None}

    client = DeepSeekClient(api_key.strip(), base_url, model, timeout_s=45)
    started = time.perf_counter()
    try:
        result = await client.chat(
            [
                {"role": "system", "content": "你是连通性测试助手，只回复要求的字符。"},
                {"role": "user", "content": "回复两个字：正常"},
            ],
            temperature=TEMPERATURE_REVIEW,
            max_tokens=16,
        )
    except LlmError as exc:
        return {"ok": False, "message": exc.message, "model": None, "latency_ms": None, "hint": exc.hint}
    latency = int((time.perf_counter() - started) * 1000)
    return {
        "ok": True,
        "message": f"连接正常，模型可以响应（耗时 {latency} 毫秒）。",
        "model": result.model,
        "latency_ms": latency,
    }
