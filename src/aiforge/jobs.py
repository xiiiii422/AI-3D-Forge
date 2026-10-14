"""AI 3D Forge —— 生成作业流水线。

一次「描述 → 3D 模型」的完整链路：
    AI 设计 → 校验代码 → OpenSCAD 编译 → 网格分析 → AI 打印性检查 → 导出

设计要点：
  * 每个阶段都向界面推送一句「人话」进度，用户永远知道现在在做什么。
  * 编译/校验失败会自动把真实报错回灌给 AI 修复一次（错误恢复，第四十九节）。
  * 所有阻塞操作（子进程、磁盘）都放到线程里跑，界面不会卡住。
  * 作业状态保存在内存中并带 TTL，页面刷新后可凭 job_id 重新订阅。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import compiler, openscad as scad_cli, printers, projects, render, scad as scad_src
from . import llm as llm_mod
from . import reqtests
from .changes import (
    ChangeContract,
    ChangeVerdict,
    build_change_contract,
    embed_fingerprint,
    follow_values,
    resolve_param_name,
    summarize_verdict,
    verify_change,
)
from .config import store
from .design import DesignSpec, diameter_is_fit_scope, merge_specs, normalize_name
from .llm import DeepSeekClient, LlmError, detect_provider
from .mesh import MeshMetrics, StlError, estimate_usage, summarize_for_ai
from .projects import HistoryEntry, Project, now_iso

log = logging.getLogger(__name__)

JOB_TTL_S = 6 * 3600
MAX_JOBS = 200

# 自动缩放到成型范围时留出的边距（mm）：切片软件的裙边/擦料塔需要空间，
# 而且模型贴边放容易第一层附着不良。
SAFETY_MARGIN_MM = 5.0

# 直出标准零件时允许的「特征」白名单：需求里一旦出现白名单之外的特征
# （挂绳孔、刻字、卡扣等），就认为用户要的是「零件 + 额外结构」，回退 AI 自由设计。
PLAIN_PART_FEATURES = ("螺纹", "内螺纹", "外螺纹", "中空", "通孔", "镂空", "挡肩", "转接")

# 编译修复两轮仍失败时的「换路径重生成」附加约束（P0：生成随机性加固）。
# 不是再修同一份代码，而是带着硬约束重新生成，避免卡在同一个错误写法上。
_REGEN_EXTRA = (
    "上一次生成的代码编译失败（多为 CGAL 布尔运算失败）。这次请**换一种建模路径**："
    "用最少的 union/difference 层级；所有相减/相加的实体沿接触方向错开 ≥0.01mm，"
    "杜绝共面重合与零厚度；所有 extrude 高度与 cylinder 半径必须为正；"
    "不要复用上一次的具体写法与模块划分。"
)
# 完整性修复两轮仍散件时的「换路径重生成」约束：散件靠微调坐标修不好（实测），
# 必须换画法（先 2D 截面 union 再挤出 / 保证实体重叠）。
_INTEGRITY_REGEN_EXTRA = (
    "上一次生成的模型经实测由多个互不相连的零件组成（散件）。这次请**换一种建模路径**："
    "把全部特征（支臂、底座、卡槽、阵列孔等）先在一个 2D 截面里 union 成完整轮廓再统一挤出，"
    "或确保每个特征与主体有 ≥0.2mm 的实体重叠；最终必须是**一个连通实体**，"
    "不允许任何只靠「摆得近」而不相交的结构。"
)

# 阶段 → (用户看到的中文说明, 起点百分比, 终点百分比)
#
# 为什么是「区间」而不是一个点：以前每个阶段只有一个固定百分比（78 → 86 → 95），
# 而最长的生成阶段干脆是 None（界面显示不定态转圈）。用户的实际反馈是
# 「进度条不能体现准确的进度」——既不动，也不知道卡没卡。
#
# 现在的规则：
#   * 阶段内**按时间推进**（渐近逼近终点，永不越过），所以长阶段也在动；
#   * 百分比**单调不减**，切阶段只会往前走；
#   * 终点都留有余量，**只有真正 done 才是 100%**；
#   * `awaiting_confirmation` 的起点=终点：在等人做决定，进度条不该自己往前爬
#     （那是在骗人）。
#
# 数值依据：AI 调用实测 6~15 秒、编译 0.3~3 秒、分析 0.5~3 秒，
# 所以生成阶段给的区间最宽（12→45），编译/分析各占一段。
STAGE_BANDS: dict[str, tuple[str, float, float]] = {
    "queued": ("正在准备…", 0.0, 2.0),
    "understanding": ("正在理解你的需求…", 2.0, 12.0),
    "awaiting_confirmation": ("等待你确认我对需求的理解", 12.0, 12.0),
    "generating_scad": ("正在按规格设计结构…", 12.0, 45.0),
    "validating_scad": ("正在检查模型代码…", 45.0, 52.0),
    "compiling": ("正在生成 3D 模型…", 52.0, 78.0),
    "analyzing": ("正在检查模型尺寸与结构…", 78.0, 86.0),
    "verifying": ("正在核对改动是否符合你的要求…", 86.0, 90.0),
    "reviewing": ("正在进行打印性检查…", 90.0, 96.0),
    "exporting": ("正在保存项目…", 96.0, 99.0),
    "done": ("已完成。", 100.0, 100.0),
}

# 每个阶段「大概多久算走完一个区间」（秒）：用于把时间映射成区间内的进度。
# 取实测耗时量级，偏大一点 —— 宁可走得慢，也不要早早顶到上限然后干等。
STAGE_TAU_S: dict[str, float] = {
    "queued": 1.0,
    "understanding": 8.0,
    "generating_scad": 12.0,
    "validating_scad": 1.5,
    "compiling": 4.0,
    "analyzing": 4.0,
    "verifying": 6.0,
    "reviewing": 8.0,
    "exporting": 1.5,
}

# 派生表：阶段 → 该阶段的展示文案。（曾经还有一张 `STAGE_PERCENT` 阶段→上限百分比，
# 注释里写着「保留给外部脚本/诊断用」，实测全仓（含自检与构建脚本）没有任何代码读它，
# 只剩文档里一处历史描述提到——典型的「写句理由把没人用的东西留在原地」。进度真正的来源是
# STAGE_BANDS 区间 + Job.creep() 按时间推进（见 §69），已删。）
STAGE_TEXT: dict[str, str] = {key: value[0] for key, value in STAGE_BANDS.items()}

# 阶段内进度的刷新间隔（秒）。3 秒是折中：比这更密只是徒增 SSE 流量，
# 更稀则长阶段看起来像卡住。测试会临时调小它来跑真实的推进循环。
PROGRESS_TICK_S = 3.0


@dataclass
class JobMessage:
    seq: int
    text: str
    detail: str | None = None
    kind: str = "info"  # info | success | warn | error

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "text": self.text, "detail": self.detail, "kind": self.kind}


@dataclass
class Job:
    job_id: str
    project_id: str
    revision: int
    mode: str  # create | modify | params | compile
    prompt: str | None = None
    instruction: str | None = None
    status: str = "running"  # running | done | error | cancelled
    stage: str = "queued"
    percent: float | None = 2.0
    messages: list[JobMessage] = field(default_factory=list)
    error: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    # 阶段区间与阶段开始时间：进度条在阶段内按时间推进（见 Job.creep）
    stage_floor: float = 0.0
    stage_ceiling: float = 0.0
    stage_started_at: float = field(default_factory=time.time)
    task: asyncio.Task | None = field(default=None, repr=False)

    # --- 事件广播 -------------------------------------------------------
    _subscribers: list[asyncio.Queue] = field(default_factory=list, repr=False)
    _seq: int = 0
    # 规格确认关卡：进入 awaiting_confirmation 后在这里等用户确认
    awaiting_spec: DesignSpec | None = field(default=None, repr=False)
    _spec_event: asyncio.Event | None = field(default=None, repr=False)
    _spec_decision: DesignSpec | None = field(default=None, repr=False)
    # 最近一次公布给界面的规格 —— 放进快照，这样用户刷新页面/断线重连后
    # 仍然能看到并确认那张规格卡片（否则确认关卡会变成「看不见的死等」）
    latest_spec: dict[str, Any] | None = field(default=None, repr=False)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # --- 规格确认 -------------------------------------------------------
    async def wait_for_spec(self) -> DesignSpec | None:
        """等待用户确认规格。

        返回用户（可能修改过的）规格；用户跳过则返回 None（沿用原规格）。
        用事件而不是轮询：既省 CPU，也能被取消立刻唤醒。
        """
        self._spec_event = asyncio.Event()
        try:
            await self._spec_event.wait()
        except asyncio.CancelledError:
            self.cancel()
            raise
        finally:
            self._spec_event = None
        return self._spec_decision

    def confirm_spec(self, spec: DesignSpec | None = None) -> None:
        """用户确认了规格（可带修改后的版本）。"""
        self._spec_decision = spec
        if self._spec_event is not None:
            self._spec_event.set()

    @property
    def waiting_for_spec(self) -> bool:
        return self.status == "awaiting_confirmation"

    @property
    def has_watcher(self) -> bool:
        """现在有没有界面正在监听这个作业。

        这是「这个作业还有没有人管」**唯一可靠**的信号：
        刷新页面 / 切走项目 / 关窗口都会关掉 SSE，`job_stream` 的 finally
        立刻 unsubscribe，订阅数就归零。
        不能靠时间猜 —— 用户可能只是去接杯水，2 分钟后回来还要点确认。
        """
        return bool(self._subscribers)

    def _publish(self, event: str, payload: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait((event, payload))
            except asyncio.QueueFull:  # pragma: no cover - 慢消费者保护
                log.debug("订阅队列已满，丢弃一条事件")

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        # 首帧补发当前完整状态，保证刷新页面/断线重连后界面能立刻还原
        queue.put_nowait(("snapshot", self.snapshot()))
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    # --- 状态更新 -------------------------------------------------------
    def say(self, text: str, *, detail: str | None = None, kind: str = "info") -> None:
        message = JobMessage(seq=self._next_seq(), text=text, detail=detail, kind=kind)
        self.messages.append(message)
        if len(self.messages) > 60:
            del self.messages[:-60]
        self._publish("message", message.to_dict())

    def set_stage(self, stage: str, percent: float | None = None, text: str | None = None) -> None:
        """切阶段：进入该阶段的**区间起点**，并记下开始时间（供阶段内按时间推进）。"""
        self.stage = stage
        floor, ceiling = STAGE_BANDS.get(stage, ("", 0.0, 100.0))[1:]
        self.stage_floor = float(floor)
        self.stage_ceiling = float(ceiling)
        self.stage_started_at = time.time()
        if percent is not None:
            target = float(percent)
        else:
            target = self.stage_floor
        # 单调不减：切阶段不该让进度条往回退
        self.percent = max(self.percent or 0.0, target)
        if text:
            self.say(text)
        self._publish("stage", {"stage": stage, "percent": self.percent, "text": STAGE_TEXT.get(stage, "")})

    def creep(self) -> bool:
        """阶段内按时间推进百分比（渐近逼近本阶段上限，永不越过）。

        返回是否发生了变化（变了才推送，避免刷屏）。

        为什么用渐近而不是线性：我们**不知道**这次 AI 调用要跑 5 秒还是 30 秒。
        线性会在超时的时候顶到上限然后干等（看起来像卡死），
        渐近则是"越接近上限走得越慢"，既一直在动，又永远不到 100%
        —— 只有真正 done 才是 100%。
        """
        if self.status != "running":
            return False
        if self.stage_ceiling <= self.stage_floor:
            # 等人确认这类区间起点=终点，故意不涨（进度条不能替用户做决定）
            return False
        elapsed = max(0.0, time.time() - self.stage_started_at)
        tau = STAGE_TAU_S.get(self.stage, 6.0)
        ratio = 1.0 - math.exp(-elapsed / tau)
        value = self.stage_floor + (self.stage_ceiling - self.stage_floor) * ratio
        current = self.percent or 0.0
        if value <= current + 0.05:
            return False
        self.percent = min(value, self.stage_ceiling)
        self._publish("stage", {"stage": self.stage, "percent": self.percent, "text": ""})
        return True

    def progress(self, percent: float, text: str | None = None) -> None:
        # 同样单调不减：显式进度也不该把已经走过的部分退回去
        self.percent = max(self.percent or 0.0, float(percent))
        if text:
            self.say(text)
        self._publish("stage", {"stage": self.stage, "percent": self.percent, "text": text or ""})

    def fail(self, message: str, *, hint: str | None = None, code: str = "INTERNAL") -> None:
        self.status = "error"
        self.error = {"message": message, "hint": hint, "code": code}
        self.percent = None
        self.finished_at = time.time()
        self.say(message, detail=hint, kind="error")
        self._publish("error", {"message": message, "hint": hint, "code": code})

    def succeed(self) -> None:
        self.status = "done"
        self.stage = "done"
        self.percent = 100.0
        self.finished_at = time.time()
        # 契约 3.3：done 事件的 data 必须是「完整 Job 快照」（含 status/result/error/messages），
        # 而不是只推 JobResult。前端据此判定结束并读 result；只推 result 会强制它走宽松
        # 兜底分支，从而丢掉 messages。
        self._publish("done", self.snapshot())

    def cancel(self) -> None:
        # awaiting_confirmation 也算「进行中」：用户在确认卡片上点取消时，
        # 作业正卡在 wait_for_spec() 上，只认 running 会让取消按钮彻底失效。
        if self.status in ("running", "awaiting_confirmation"):
            self.status = "cancelled"
            self.percent = None
            self.finished_at = time.time()
            self.say("已取消生成。", kind="warn")
            # 唤醒 wait_for_spec()，否则任务仍在挂起
            self.confirm_spec(None)
            # 同样推完整快照，保证前端能按统一的 Job 结构收尾
            self._publish("cancelled", self.snapshot())
        if self.task and not self.task.done():
            self.task.cancel()

    def snapshot(self) -> dict[str, Any]:
        """完整的作业快照（契约 3.2 的 Job 结构）。

        created_at / finished_at 必须是 ISO8601 字符串（前端类型如此声明）；
        类内部的 created_at / finished_at 是 epoch 秒，仅用于 GC 与排序。
        """
        from datetime import datetime, timezone

        def iso(epoch: float | None) -> str | None:
            if not epoch:
                return None
            return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone().isoformat(timespec="seconds")

        return {
            "job_id": self.job_id,
            "project_id": self.project_id,
            "revision": self.revision,
            "mode": self.mode,
            "status": self.status,
            "stage": self.stage,
            "percent": self.percent,
            "messages": [m.to_dict() for m in self.messages],
            "error": self.error,
            "result": self.result,
            # 等待确认时把规格一并给出去：页面刷新后卡片还能重建，
            # 否则用户只能看到一个永远转圈的作业
            "spec": self.latest_spec,
            "created_at": iso(self.created_at),
            "finished_at": iso(self.finished_at),
        }


_jobs: dict[str, Job] = {}


def _gc() -> None:
    now = time.time()
    stale = [key for key, job in _jobs.items() if job.finished_at and now - job.finished_at > JOB_TTL_S]
    for key in stale:
        _jobs.pop(key, None)
    if len(_jobs) > MAX_JOBS:
        ordered = sorted(_jobs.values(), key=lambda j: j.created_at)
        for job in ordered[: len(_jobs) - MAX_JOBS]:
            _jobs.pop(job.job_id, None)


def get(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def all_jobs() -> list[Job]:
    return list(_jobs.values())


def create_job(project_id: str, revision: int, mode: str, **kwargs: Any) -> Job:
    _gc()
    job = Job(job_id=uuid.uuid4().hex[:16], project_id=project_id, revision=revision, mode=mode, **kwargs)
    _jobs[job.job_id] = job
    return job


def active_job_for(project_id: str) -> Job | None:
    """该项目上是否已经有正在进行的作业（运行中或等待用户确认）。

    为什么必须挡住并发：两个作业会对**同一个项目目录**做
    「归档上一版 → 写 model.scad → 存 meta」这一整套操作，而且它们读到的
    `project.revision` 是同一个值 —— 于是 revision 相同、互相覆盖，
    其中一个模型被静默丢弃，`archive_revision` 还可能在同目录上交叉执行。
    多出来的那次如果调用 AI，还会真的花掉用户的额度。

    这条守卫是**权威层**：前端也会做乐观锁（点下去就先禁用发送键），
    但前端只防手滑，防不住直接调接口、或第二个实例。
    """
    for job in _jobs.values():
        if job.project_id == project_id and job.status in ("running", "awaiting_confirmation"):
            return job
    return None


def publish_spec(job: Job, spec: DesignSpec, awaiting: bool = True) -> None:
    """把设计规格推给界面，并记进快照。

    独立成模块函数（而不是执行器的私有方法）有两个理由：
      * 用户刷新页面 / 断线重连后靠 `Job.snapshot()` 里的这份数据重建确认卡片，
        逻辑必须与推送时完全一致，不能有第二份实现；
      * 确认关卡是「用户点不动」级别的高危路径，需要能在不调用 AI 的情况下自检。
    """
    payload: dict[str, Any] = {
        "spec": spec.to_display(),
        "awaiting_confirmation": awaiting,
    }
    job.latest_spec = payload
    job._publish("spec", payload)  # noqa: SLF001


async def _analyze_with_heartbeat(
    job: Job,
    stl_path: Path,
    limits: Any,
    *,
    source: str | None = None,
    heartbeat_s: float = 10.0,
) -> MeshMetrics:
    """在线程里做网格分析，同时每隔几秒报一次「还在跑」。

    source 传进来时顺带核对「源码里声明了的间隙/夹口在网格里量不量得到」：
    夹口这一刀完全没切到时，零件数/封闭性/包围盒/壁厚全绿，只有「找不到任何空隙」
    能揭穿它（实测交付给用户的那版手机夹就是这么假绿的）。不传则完全不测，零成本。

    为什么需要心跳：网格分析是纯 Python 的逐面计算，面数大的模型要跑很久
    （实测 100 万面约 50 秒），这段时间里**一个事件都不推**。
    而前端的卡死看门狗判据正是「120 秒没有任何进展就判定卡住并取消作业」——
    于是一个完全正常的大模型分析会被自己的看门狗杀掉。
    心跳同时解决两件事：用户看得到「还在分析（已 20 秒）」，
    看门狗的进度标记也一直在变。

    分析本身仍然在 worker 线程里跑（不阻塞事件循环）；
    这里的 `say()` 只是状态行，不会往对话里刷屏（界面只显示最后一条）。
    """
    declared_gaps = _declared_gaps_from_source(source)
    task = asyncio.create_task(
        asyncio.to_thread(compiler.analyze_stl, stl_path, limits, declared_gaps or None)
    )
    started = time.monotonic()
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=heartbeat_s)
            if done:
                return task.result()
            elapsed = time.monotonic() - started
            job.say(f"模型面数较多，仍在分析…（已 {elapsed:.0f} 秒）")
    finally:
        # 作业被取消 / 超时退出时别把任务悬在那里（线程无法真正打断，
        # 但至少要把它从事件循环里摘掉，避免留下未取回的异常）
        if not task.done():
            task.cancel()


# 需求原文的长度上限：它会被发给 AI（散件重生成），不能把整个历史拼上去。
_REQUIREMENT_MAX_CHARS = 900


def _project_baseline_requirement(project: Project) -> str:
    """项目建立时那句用户原话（history.jsonl 第一条里的 prompt/instruction）。

    为什么取“第一条有字的”而不是取 prompt 类型的那条：实测真实项目里
    history.jsonl 的第一行就是创建那一次的 prompt（“设计一个圆形的桌面笔筒：…底部是
    封闭的实心底…”）；而“回退到第N版”那类条目只写 instruction。所以按顺序
    拿第一条非空的原文最稳。

    为什么不拿 meta.summary / spec.summary：那是 AI 转述的一句话（“收纳笔具的桌面
    圆柱形容器”），用户点名的约束在转述过程中会丢 —— 实测过“实心底”被改写成
    “底部为实心封闭结构”，按词表门控就命中不了。
    """
    for entry in getattr(project, "history", None) or []:
        text = " ".join(
            str(part) for part in (getattr(entry, "prompt", None), getattr(entry, "instruction", None)) if part
        ).strip()
        if text:
            return text[:_REQUIREMENT_MAX_CHARS]
    return ""


# --------------------------------------------------------------------------
# 执行器
# --------------------------------------------------------------------------
class GenerationRunner:
    """把一次生成请求跑完，并全程推送进度。"""

    def __init__(self, job: Job) -> None:
        self.job = job
        self.config = store()
        self._printer = None
        self._constraints = None
        self._printer_block: str | None = None
        # 修复循环跑完后仍然存在的问题（实测证据）。它必须带到最终判定里去：
        # 以前只在对话里 say 一句就丢了，结果「残留问题」与「全部通过」在结果卡上长得一样。
        self._integrity_remaining: list[str] = []
        # 需求原文：本次指令（`_requirement`）+ 项目最初那句诉求（`_requirement_base`）。
        # 两个都在 __init__ 里给空默认值，而不是等 `run()` 赋值：任何一条分支先问了
        # `_requirement_text()` 都不该 AttributeError（“接线比方法后到”的坑踩过）。
        self._requirement: str = ""
        self._requirement_base: str = ""

    # --- 打印机上下文 -----------------------------------------------------
    # 只在「AI 从零设计」时强制模型必须放得进成型范围。
    # 不强制的情况都各有道理：
    #   modify   —— 用户可能故意做大件准备分件打印，不该被拦住
    #   compile  —— 用户粘贴的是自己的代码，程序不该擅自改动
    #   params   —— 只是改参数，越界事实如实报给用户即可
    # 这些模式仍然会照常分析并明确提示「超出成型范围」。
    _BUILD_VOLUME_STRICT_MODES = ("create",)

    @property
    def printer(self):
        """当前选中的打印机（作业创建时确定，整个流程内保持一致）。"""
        if self._printer is None:
            from .printers import registry

            self._printer = registry().selected()
        return self._printer

    @property
    def material(self) -> str:
        from .printers import registry

        return registry().effective_material()

    @property
    def constraints(self):
        """按「机型 + 材料」推导出的工艺约束。"""
        if self._constraints is None:
            from .printers import resolve_process

            self._constraints = resolve_process(self.printer, self.material)
        return self._constraints

    @property
    def printer_block(self) -> str:
        """注入 AI 提示词的打印机约束段落。"""
        if self._printer_block is None:
            from .printers import compact_constraints

            self._printer_block = compact_constraints(self.printer, self.constraints, self.material)
        return self._printer_block

    @property
    def analysis_limits(self):
        """几何判断用的阈值（按选中机型推导）。"""
        from .mesh import limits_from_printer

        return limits_from_printer(self.printer, self.material)

    def resolve_fn_for(self, source: str) -> int:
        """按「真实模型尺寸」决定 OpenSCAD 的 $fn。

        为什么要按实际尺寸算：$fn 决定圆弧面的分段数，而同样分段数下，
        模型越大棱面越明显。小模型用大 $fn 只会白白增加面数与编译时间。
        模型尺寸取源码里参数的最大值（参数通常直接对应外形尺寸）作为上界估计，
        再用该机型的成型尺寸封顶，避免某个异常大参数把面数炸掉。
        """
        from .printers import resolve_fn

        profile = self.printer
        try:
            numbers = scad_src.extract_top_level_numbers(source)
        except Exception:  # noqa: BLE001 - 解析失败就用最保守的估计
            numbers = {}
        candidates = [abs(v) for v in numbers.values() if abs(v) >= 1.0]
        estimate = max(candidates) if candidates else max(profile.build_x, profile.build_z) * 0.5
        ceiling = max(profile.build_x, profile.build_y, profile.build_z)
        estimate = max(10.0, min(estimate, ceiling))
        fn = resolve_fn(profile, estimate)
        log.debug("按模型尺寸 %.0fmm 计算 $fn = %d", estimate, fn)
        return fn

    @staticmethod
    def _bbox_volume(metrics: MeshMetrics) -> float:
        """包围盒体积，用于在多个候选结果里挑「更小的那个」。"""
        x, y, z = metrics.bbox
        return max(x, 0.0) * max(y, 0.0) * max(z, 0.0)

    async def _review_snapshot(
        self,
        client: DeepSeekClient,
        compile_result: Any,
        metrics: MeshMetrics,
        requirement: str,
        project: Project,
    ) -> str:
        """渲染模型并让多模态模型复核外观，返回它的原始结论文本（失败返回空串）。

        **为什么必须做这件事**：确定性指标能测的都测了，仍然会漏。
        实测过的例子：桌面垃圾桶有 4 根外壁加强筋从外壁穿进了桶内腔，
        而 `solid_parts==1`、`watertight==True`、包围盒与体积全部正常 ——
        指标全绿，用户拿到的是不能用的桶。这类问题渲染一张图一眼就能看出来。

        失败一律降级为「不复核」，绝不因为它失败而让用户拿不到模型。
        """
        job = self.job
        job.set_stage("reviewing", text="正在渲染模型外观并请 AI 复核…")
        try:
            shots = await asyncio.to_thread(
                render.render_views,
                compile_result.stl_path,
                job_work_dir(project),
                prefix=f"r{project.revision + 1}",
            )
            # 面数过多时 render 会按步长抽样，并在 `sampled` 里如实标出来。
            # 这一句必须**真的执行**：原来这里写了个赋值表达式却从不读它，
            # 于是「外观复核用的是抽样渲染图（细节可能有孔洞）」这条告警
            # 永远不出现 —— 用户以为看的是完整模型，实际看到的是抽稀的。
            # 判据要出现在 AI 结论**之前**，否则用户先看到"未发现明显问题"
            # 再看到告警，注意力已经被前者带走了。
            if any(shot.sampled for shot in shots):
                job.say(
                    "模型面数较多，外观复核用的是抽样渲染图（细节可能有孔洞）。",
                    kind="warn",
                )
            images = [shot.path.read_bytes() for shot in shots]
            summary = summarize_for_ai(metrics, project.name, self.analysis_limits)
            text = await client.review_snapshot(images, requirement, summary)
        except (LlmError, StlError, OSError) as exc:
            log.warning("图片复核跳过：%s", exc)
            return ""
        except Exception as exc:  # noqa: BLE001 - 复核失败不该让整个作业失败
            log.warning("图片复核异常：%s", exc)
            return ""
        if not text:
            # 空回复必须如实说成「没有结论」。当成「没问题」会静默放过缺陷。
            job.say("图片复核没有给出结论（已跳过，不影响模型本身）。", kind="warn")
            return ""
        verdict = llm_mod.parse_snapshot_verdict(text)
        if verdict == llm_mod.VERDICT_PROBLEM:
            job.say("AI 看过外观后提出了问题。", detail=text[:400], kind="warn")
        elif verdict == llm_mod.VERDICT_CLEAR:
            job.say("AI 看过外观，未发现明显问题。", detail=text[:400], kind="success")
        else:
            # 回复非空，但既不守首行契约也不含强问题标记（实测 glm-4v-flash 会这样只
            # 复述指标、或说一句含「不确定」的话）—— 以前这会被当成 else 分支，播成
            # success「未发现明显问题」，而它其实只是「没给出可判定的结论」。
            job.say("图片复核的回答无法判定（已跳过，不影响模型本身）。", detail=text[:400], kind="warn")
        return text

    def _dim_deviation(
        self,
        metrics: MeshMetrics,
        spec: DesignSpec | None,
        contract: "ChangeContract | None" = None,
        declared: dict[int, tuple[str, float, str]] | None = None,
    ) -> float:
        """点名尺寸实测偏差的归一化和（越小越好）；用于逐轴校正的采纳比较。

        declared 传 None 时退回按轴记账的老判据；有需求核对表时应传它的
        `dimension_targets()` —— 那是**水平尺寸择优配对之后**的方向，
        跟校正用的是同一套事实，不会出现「照着 X 校、按 Y 打分」。
        """
        if declared is None:
            declared = _spec_declared_dimensions(spec)
        if not declared or not metrics.bbox:
            return 0.0
        targets: dict[str, float] = {}
        if contract is not None:
            targets = {
                normalize_name(key): float(value)
                for key, value in contract.target_values.items()
                if isinstance(value, (int, float))
            }
        total = 0.0
        for axis, (label, want, spec_name) in declared.items():
            for key in (normalize_name(spec_name), normalize_name(label)):
                if key in targets:
                    want = targets[key]
                    break
            if want <= 0:
                continue
            got = (
                max(metrics.bbox[0], metrics.bbox[1])
                if axis == HORIZONTAL_ENVELOPE_AXIS
                else metrics.bbox[axis]
            )
            total += abs(got - want) / want
        return total

    def _requirement_suite(
        self, metrics: MeshMetrics | None, spec: DesignSpec | None
    ) -> reqtests.ReqSuite | None:
        """把设计契约编译成**逐条可执行判定**（PR/RS/IR），对标 CADTests 口径。

        与 `_geometry_integrity_problems` 的分工：那边是给 AI 的详细改法（实测踩坑攒出来的），
        这边是能统计、能展示、能回归的判定表。两边的阈值同源（都取 jobs 的判据与机型公差），
        自检带一条单向一致性守卫，防止两处慢慢漂移。
        """
        if metrics is None:
            return None
        try:
            tolerance = float(getattr(self.printer, "dimensional_tolerance_mm", 0.25) or 0.25)
        except (TypeError, ValueError):
            tolerance = 0.25
        return reqtests.evaluate(
            spec=spec,
            metrics=metrics,
            requirement=self._requirement_text(),
            contract=self._change_contract,
            limits=self.analysis_limits,
            tolerance_mm=tolerance,
            declared_dimensions=_spec_declared_dimensions(spec),
            planar_dimensions=spec_planar_dimensions(spec),
            declared_holes=_spec_declared_holes(spec),
            spec_items=list(getattr(spec, "specs", []) or []) if spec is not None else [],
        )

    def _axis_dimension_fix(
        self,
        source: str,
        metrics: MeshMetrics,
        spec: DesignSpec | None,
        contract: "ChangeContract | None" = None,
        declared: dict[int, tuple[str, float, str]] | None = None,
    ) -> str | None:
        """点名整体尺寸实测偏差过大时，算出逐轴校正倍率并包裹几何；不该修时返回 None。

        判据保守：偏差 ≤2% 不修（测量/舍入噪声）；倍率超出 `_MIN_SHRINK_FACTOR`~2.0 不修
        （那不像「写错一段」，更像需求理解错了，宁可报问题不要乱缩）。
        缩小方向的下界与「超出 50% 就不可能是多了个底座」那条判据用同一把尺子：
        要缩到 2/3 以下才能达标的目标，说明这两个数字讲的压根不是同一个东西 ——
        实测的「杯径 70 vs 整件 132」倍率 0.53 在旧的 0.5 下界下是**放行**的，
        会把整件缩掉 47%（破坏性「修复」比不修更糟）。
        目标值优先取**契约里的新目标**（修改流程），其次取规格声明值 ——
        否则「把高度改成 50」会拿旧规格的 100 去算倍率，算出 2.4 倍被拒修。
        方向优先取需求核对表配对后的结果（`declared`）：模型绕竖直轴转 90° 是等价摆放，
        按固定轴缩放会把正确的模型放大一倍。
        """
        if declared is None:
            declared = _spec_declared_dimensions(spec)
        if not declared:
            return None
        targets: dict[str, float] = {}
        if contract is not None:
            targets = {
                normalize_name(key): float(value)
                for key, value in contract.target_values.items()
                if isinstance(value, (int, float))
            }
        fx = fy = fz = 1.0
        for axis, (label, value, spec_name) in declared.items():
            for key in (normalize_name(spec_name), normalize_name(label)):
                if key in targets:
                    value = targets[key]
                    break
            if value <= 0:
                continue
            if axis == HORIZONTAL_ENVELOPE_AXIS:
                measured = max(metrics.bbox[0], metrics.bbox[1])
                factor = value / measured if measured > 0 else 1.0
                # 「杯径 70」这类**配合尺寸**不能拿来缩整件：包围盒比它大是正常的
                # （座子要包住杯子），拿它当外形目标会把夹口、底座、筋一起缩掉。
                # 放大方向仍然放行 —— 整件连 70 都不到，那个杯径必然是没做到的。
                if factor < 1.0 and diameter_is_fit_scope(label, spec_name):
                    continue
                fx *= factor
                fy *= factor
            else:
                measured = metrics.bbox[axis]
                factor = value / measured if measured > 0 else 1.0
                if axis == 0:
                    fx *= factor
                elif axis == 1:
                    fy *= factor
                else:
                    fz *= factor
        if all(abs(f - 1.0) <= 0.02 for f in (fx, fy, fz)):
            return None
        if any(f > 2.0 or f < _MIN_SHRINK_FACTOR for f in (fx, fy, fz)):
            return None
        return _wrap_geometry_in_scale_axes(
            source,
            fx,
            fy,
            fz,
            f"实测包围盒与点名尺寸偏差过大，已按轴校正（x{fx:.3f} y{fy:.3f} z{fz:.3f}）",
        )

    async def _repair_geometry_integrity(
        self,
        client: DeepSeekClient,
        source: str,
        compile_result: Any,
        metrics: MeshMetrics,
        limits: Any,
        params: dict[str, float] | None,
        project: Project,
        spec: DesignSpec | None = None,
        visual: str = "",
        dropped: list[str] | None = None,
    ) -> tuple[str, Any, MeshMetrics]:
        """模型「不成一整件」「特征穿进腔体」或「点名尺寸没做到」时，带着实测证据让 AI 定点重做一次。

        为什么必须做这件事：这两条已经写进了生成提示词的第 11/12 条（铁律级别），
        但拿 6 个真实复杂实例实测，第一次生成就有 5 个翻车 ——
        其中「线缆收纳底座」被拆成了 80 个互不相连的小块。
        文字约束不住，而这三个缺陷恰好都是**程序能精确观测**的：
        连通分量数、水平截面上材料环的嵌套层数、以及实测包围盒 vs 规格声明值。
        所以正确做法是：测出来 → 把证据（不是泛泛的建议）交回给 AI → 只修这一处。

        取舍原则与改动校验一致：**修不好就保留原样**，
        绝不因为修复失败而让用户拿到比原来更差的东西。
        """
        job = self.job
        suite0 = self._requirement_suite(metrics, spec)
        targets0 = suite0.dimension_targets() if suite0 is not None else None
        problems = _geometry_integrity_problems(
            metrics, spec, visual, dropped, self._requirement_text(), self._change_contract,
            targets0,
        )
        best_source, best_compile, best_metrics = source, compile_result, metrics
        best_rank = _integrity_rank(metrics, spec, self._change_contract)

        # AI 修复轮只在确实报了问题时才花网络调用。但**不能提前 return**：
        # 偏 2%~10% 这一档「不报错、也确实在规格上没做到」的偏差，
        # 正是下面那个确定性逐轴校正的活（第二轮基准实测到的就是这个空档）。
        if problems:
            job.say(
                "模型有尺寸或结构问题，正在让 AI 修正…",
                detail="；".join(problems)[:300],
                kind="warn",
            )
            # 最多两轮。为什么要第二轮：实测「线缆收纳底座」一次修复只把
            # 77 个散件降到 73 个 —— 模型容易只去调坐标，越调越乱。
            # 第二轮会把上一版的实测数字一并给它，并明确要求**换策略**
            # （用 hull 连起来 / 改成深入主体），而不是继续微调。
            for attempt in (1, 2):
                job.progress(90.0, f"正在修正尺寸/结构问题（第 {attempt} 次）…")
                try:
                    # 先算清单：水平尺寸配对后的方向由它给出，下面的问题段落与回喂都用它，
                    # 两层不再各量各的。
                    suite = self._requirement_suite(best_metrics, spec)
                    suite_targets = suite.dimension_targets() if suite is not None else None
                    feedback = _geometry_integrity_problems(
                        best_metrics, spec, visual, dropped,
                        self._requirement_text(), self._change_contract, suite_targets,
                    )
                    # 需求核对表：把「要求 / 实测 / 偏差 / 公差」的**数字**一并回喂。
                    # 中文段落说的是「偏小到什么程度」这类判据，表格里则是每条需求的确切差距 ——
                    # 论文的对照实验显示，带数值的执行日志回喂比只给文字建议更能提升达成率。
                    if suite is not None:
                        feedback.extend(suite.repair_lines())
                    repaired = await client.repair_geometry(
                        best_source,
                        "\n".join(feedback),
                        attempt,
                    )
                except LlmError as exc:
                    job.say("结构修正未成功，保留当前模型。", detail=exc.message, kind="warn")
                    break
                except Exception as exc:  # noqa: BLE001 - 修复失败不该让整个作业失败
                    log.warning("几何完整性修复调用失败：%s", exc)
                    break

                candidate = scad_src.strip_code_fences(repaired.content)
                if not scad_src.validate(candidate).ok:
                    job.say("结构修正后的代码没通过安全检查，保留当前模型。", kind="warn")
                    break

                retry = await asyncio.to_thread(
                    compiler.compile_to_stl,
                    candidate,
                    job_work_dir(project),
                    "model",
                    params,
                    None,
                    self.resolve_fn_for(candidate),
                )
                if not retry.ok:
                    job.say("结构修正后的代码编译失败，保留当前模型。", kind="warn")
                    break

                try:
                    retry_metrics = await _analyze_with_heartbeat(
                        job, retry.stl_path, limits, source=candidate
                    )
                except StlError as exc:
                    job.say("结构修正后的模型无法分析，保留当前模型。", detail=exc.message, kind="warn")
                    break

                # 采纳条件：**封闭性不变差** + 完整性严格更好
                # （散件更少 → 点名尺寸更准 → 穿腔更少）。
                # 平手一律保留原版 —— 改动越少，用户越不容易发现别的结构被动过。
                #
                # 不能简单写成「候选必须封闭」：如果原版本来就不封闭
                # （例如网格有非流形边），那么「把 3 个散件修成一整件」的候选
                # 也会因为仍不封闭而被拒绝，用户只能继续拿着更差的那一版 ——
                # 修复明明有效，却永远不会生效。所以判据是「不比原版更差」：
                #   * 原版封闭 → 候选也必须封闭（绝不能用封闭性换别的指标）
                #   * 原版不封闭 → 已经是失败状态，允许候选继续改善别的方面
                # 不能只比 watertight 布尔值：两边都不水密时它恒为真，
                # 于是「28 条非流形边」可以被换成「287 条」还算改善。
                if _mesh_quality_not_worse(best_metrics, retry_metrics) and _integrity_rank(retry_metrics, spec, self._change_contract) < best_rank:
                    best_source, best_compile, best_metrics = candidate, retry, retry_metrics
                    best_rank = _integrity_rank(retry_metrics, spec, self._change_contract)
                if best_rank == (0, 0, 0, 0, 0, 0, 0):
                    break

        # 两轮定点修复仍散件 → 换一条生成路径重试一次（带着「必须一整件」的硬约束）。
        if best_rank[1] > 0:
            requirement = self._requirement_text()
            if requirement:
                job.say("散件仍未消除，正在换一条建模路径重新生成…", kind="warn")
                try:
                    regen = await client.generate_scad(
                        requirement, _INTEGRITY_REGEN_EXTRA, self.printer_block
                    )
                    candidate = scad_src.strip_code_fences(regen.content)
                    if scad_src.validate(candidate).ok:
                        retry = await asyncio.to_thread(
                            compiler.compile_to_stl,
                            candidate,
                            job_work_dir(project),
                            "model",
                            params,
                            None,
                            self.resolve_fn_for(candidate),
                        )
                        if retry.ok:
                            retry_metrics = await _analyze_with_heartbeat(
                                job, retry.stl_path, limits, source=candidate
                            )
                            wt_ok = _mesh_quality_not_worse(best_metrics, retry_metrics)
                            rank = _integrity_rank(retry_metrics, spec, self._change_contract)
                            if wt_ok and rank < best_rank:
                                best_source, best_compile, best_metrics = candidate, retry, retry_metrics
                                best_rank = rank
                                job.say("换路径重新生成后结构更完整。", kind="success")
                except LlmError as exc:
                    log.warning("完整性换路径重生成失败：%s", exc)

        # 声明的整体尺寸实测偏差过大 → 确定性逐轴缩放兜底。
        # 为什么需要：弱模型（实测 glm-4.7-flash）常把「总高」写成某一段的高度、
        # 「外径」写成不含唇边的直径，AI 修两轮也不收敛；而包围盒偏差是纯几何事实，
        # 逐轴缩放可以确定性命中目标（只动偏差的轴，不缩壁厚方向）。
        # 它必须在 problems 门外无条件尝试：第二轮基准实测到的正是
        # 「要求 60×40、做出 56×36（偏 6.7%）」—— 不报问题、也没人修。
        # 门控放在 _axis_dimension_fix 内部（偏差≤2% 返回 None），这里不再依赖
        # 「是否已报尺寸问题」——报问题的阈值（偏小 10%）比校正阈值（2%）宽，
        # 用它当门会把 8% 这种「没报问题但也没做到」的偏差漏掉。
        suite_now = self._requirement_suite(best_metrics, spec)
        fix_targets = suite_now.dimension_targets() if suite_now is not None else None
        fixed = self._axis_dimension_fix(
            best_source, best_metrics, spec, self._change_contract, fix_targets
        )
        if fixed:
            retry = await asyncio.to_thread(
                compiler.compile_to_stl,
                fixed,
                job_work_dir(project),
                "model",
                params,
                None,
                self.resolve_fn_for(fixed),
            )
            if retry.ok:
                retry_metrics = await _analyze_with_heartbeat(
                    job, retry.stl_path, limits, source=fixed
                )
                wt_ok = _mesh_quality_not_worse(best_metrics, retry_metrics)
                parts_ok = retry_metrics.stray_parts <= best_metrics.stray_parts
                dev_before = self._dim_deviation(
                    best_metrics, spec, self._change_contract, fix_targets
                )
                fixed_suite = self._requirement_suite(retry_metrics, spec)
                dev_after = self._dim_deviation(
                    retry_metrics,
                    spec,
                    self._change_contract,
                    fixed_suite.dimension_targets() if fixed_suite is not None else fix_targets,
                )
                # 采纳判据用「实测尺寸偏差更小」而不是 integrity rank：
                # 偏差 8% 这种「没报问题但也没做到」的情况 rank 不变，比 rank 会漏掉改善。
                if wt_ok and parts_ok and dev_after < dev_before - 1e-3:
                    best_source, best_compile, best_metrics = fixed, retry, retry_metrics
                    best_rank = _integrity_rank(retry_metrics, spec, self._change_contract)
                    job.say("已按点名的尺寸对模型做逐轴校正。", kind="success")

        if best_source is source:
            # 「一开始就没报问题」是正常路径，不能对它播报失败
            # （以前由开头的 early-return 挡掉，去掉 early-return 后必须在这里补上）。
            if problems:
                job.say("修正没有变得更好，保留当前模型。", kind="warn")
                # 一轮都没修掉：这些问题全部残留，必须交给最终判定
                self._integrity_remaining = list(problems)
            return source, compile_result, metrics

        remaining = _geometry_integrity_problems(
            best_metrics, spec, visual, dropped, self._requirement_text(), self._change_contract
        )
        self._integrity_remaining = list(remaining)
        if remaining:
            job.say(
                "已改善，但仍有残留问题。",
                detail="；".join(remaining)[:300],
                kind="warn",
            )
        else:
            job.say("已修正。", kind="success")
        return best_source, best_compile, best_metrics

    async def _refit_to_build_volume(
        self,
        source: str,
        compile_result: Any,
        metrics: MeshMetrics,
        limits: Any,
        params: dict[str, float] | None,
        project: Project,
    ) -> tuple[str, Any, MeshMetrics]:
        """模型放不进目标打印机时，让 AI 依据实测尺寸重做一次。

        为什么必须做这件事：提示词里已经写了成型范围，但实测表明 AI 会忽略它
        （实测案例：明确要求不得超 180mm，AI 仍生成了 218mm 的横梁）。
        只给建议是拦不住的，必须用实测尺寸做确定性兜底，并把真实数字回灌给 AI。

        返回 (最终源码, 最终编译结果, 最终测量结果)。任何一步失败都保留原结果，
        绝不因为「修不好」而让用户拿不到已经能用的模型。
        """
        job = self.job
        overflow_note = next((i for i in metrics.issues if "成型范围" in i), "")
        job.say("模型放不进这台打印机，正在按成型范围重新设计…", detail=overflow_note, kind="warn")
        job.progress(90.0, "正在缩小或改成分件…")

        try:
            client = self._client()
            redesign = await client.repair_scad(
                source,
                "模型实测尺寸超出了目标打印机的成型范围。\n"
                f"实测尺寸：{metrics.bbox[0]:.1f} × {metrics.bbox[1]:.1f} × {metrics.bbox[2]:.1f} mm\n"
                f"{overflow_note}\n"
                f"目标打印机：{self.printer.name}\n"
                f"{self.printer_block}\n"
                "请重新设计，使三轴尺寸都严格小于成型范围（每轴留出 5mm 余量），"
                "同时仍满足该机型的最小壁厚、最小孔径与悬垂限制。"
                "如果原尺寸是功能必需的，请输出可拼接的分件（每件都在范围内，"
                "带定位凸台与对应凹槽），并在 @summary 中说明如何拼装。只输出完整代码。",
            )
        except LlmError as exc:
            job.say("按成型范围重新设计未成功，保留当前结果。", detail=exc.message, kind="warn")
            return source, compile_result, metrics

        candidate = scad_src.strip_code_fences(redesign.content)
        if not scad_src.validate(candidate).ok:
            job.say("重新设计的版本未通过代码校验，保留原结果。", kind="warn")
            return source, compile_result, metrics

        retry = await asyncio.to_thread(
            compiler.compile_to_stl,
            candidate,
            job_work_dir(project),
            "model",
            params,
            None,
            self.resolve_fn_for(candidate),
        )
        if not retry.ok or retry.stl_path is None:
            job.say("重新设计的版本编译失败，保留原结果。", kind="warn")
            return source, compile_result, metrics

        retry_metrics = await _analyze_with_heartbeat(job, retry.stl_path, limits, source=candidate)
        if not retry_metrics.exceeds_build_volume:
            job.say("已按成型范围重新设计，现在可以放进这台打印机。", kind="success")
            return candidate, retry, retry_metrics

        # AI 只缩小了一点点（实测：要求留 5mm 余量，它把 212mm 缩到 182mm，仍越界）。
        # 再问一次 AI 不可靠，改用确定性兜底：等比缩放到刚好装得下。
        # 这一步在几何上必定成立，是关于「懂打印机」的硬保证。
        job.say(
            "重新设计后仍超出成型范围，正在按比例缩放到刚好装得下…",
            detail="；".join(retry_metrics.issues[:2]),
            kind="warn",
        )
        scaled = await self._scale_to_fit(
            candidate, project, limits, retry_metrics
        )
        if scaled is not None:
            scaled_source, scaled_compile, scaled_metrics, factor = scaled
            job.say(
                f"已等比缩放到 {factor * 100:.0f}%，现在可以放进这台打印机。"
                "如需原始尺寸，请改为分件打印。",
                detail=f"缩放后尺寸 {scaled_metrics.bbox[0]:.1f} × {scaled_metrics.bbox[1]:.1f} "
                f"× {scaled_metrics.bbox[2]:.1f} mm",
                kind="success",
            )
            return scaled_source, scaled_compile, scaled_metrics

        job.say(
            "自动缩放未能完成，已保留更小的那一版。",
            detail="建议改小需求尺寸，或换一台成型范围更大的打印机。",
            kind="warn",
        )
        if self._bbox_volume(retry_metrics) < self._bbox_volume(metrics):
            return candidate, retry, retry_metrics
        return source, compile_result, metrics

    async def _scale_to_fit(
        self,
        source: str,
        project: Project,
        limits: Any,
        metrics: MeshMetrics,
    ) -> tuple[str, Any, MeshMetrics, float] | None:
        """等比缩放源码里的所有顶层尺寸参数，使模型刚好落进成型范围。

        做法：在源码前插入 `scale(factor) { ... }` 包裹 —— 这是 OpenSCAD 的
        标准做法，等比缩放不会破坏壁厚比例与结构关系，而且结果一定成立。
        只有当缩放比例不至于把模型缩得面目全非（> 0.25）时才采用，
        否则宁可把越界事实如实告诉用户，也不悄悄交出一个 5cm 的小块。
        """
        x, y, z = metrics.bbox
        bx, by, bz = limits.build_x, limits.build_y, limits.build_z
        ratios = [
            (bx - SAFETY_MARGIN_MM) / x if x > 0 else 1.0,
            (by - SAFETY_MARGIN_MM) / y if y > 0 else 1.0,
            (bz - SAFETY_MARGIN_MM) / z if z > 0 else 1.0,
        ]
        factor = min(ratios)
        if factor >= 1.0:
            return None
        factor = max(0.25, math.floor(factor * 1000) / 1000)
        if factor >= 0.999:
            return None

        # 缩放有两条互补的路子，必须**二选一**，同时用会缩两次：
        #   a) 参数驱动：改写顶层参数值。AI 生成的代码绝大多数是这种形态
        #      （提示词强制「尺寸规格要对应顶层参数赋值」），而且编译时
        #      `apply_params` 不会再把它们改回去（我们把 params 传了 None）。
        #   b) 几何驱动：把几何语句包进 `scale(){...}`（声明留块外）。
        #      用于「尺寸直接写死在几何里、没有参数」的源码。
        #
        # 先试 (a)：(a) 能过就用它 —— 它保留参数语义，用户在参数微调里看到的
        # 就是真实的最终尺寸。(a) 过不了（例如只缩了一个无关紧要的装饰参数）
        # 再退回 (b)：用**原始源码**整段缩放，避免与 (a) 叠加。
        for candidate_source, candidate_params in (
            (_scale_param_values(source, factor), None),
            # 几何路径已经把参数留在块外、只缩放几何，因此**不能**再传原始参数：
            # 传了会被 `build_scad_source` 写回原值，缩放又被冲掉。
            (_scale_geometry(source, factor, self.printer.name), None),
        ):
            if candidate_source is None:
                continue
            try:
                result = await asyncio.to_thread(
                    compiler.compile_to_stl,
                    candidate_source,
                    job_work_dir(project),
                    "model",
                    candidate_params,
                    None,
                    self.resolve_fn_for(source),
                )
            except compiler.CompileError as exc:
                log.warning("缩放版编译异常：%s", exc.message)
                continue
            if not result.ok or result.stl_path is None:
                # 让失败原因可见：以前这里只 return None，日志里什么都不说，
                # 排查时只能靠猜。
                log.warning("缩放版编译失败：%s", result.message[:300])
                for diag in result.diagnostics[:5]:
                    log.warning("  缩放诊断 [%s] %s", diag.severity, diag.text[:200])
                continue

            new_metrics = await _analyze_with_heartbeat(
                self.job, result.stl_path, limits, source=candidate_source
            )
            if new_metrics.exceeds_build_volume:
                log.warning(
                    "缩放后仍超界：%s（factor=%.3f）", new_metrics.bbox, factor
                )
                continue
            return candidate_source, result, new_metrics, factor
        return None

    def printer_context(self) -> dict[str, Any]:
        """写进结果里、供界面展示的打印机上下文。"""
        profile = self.printer
        return {
            "id": profile.id,
            "name": profile.name,
            "manufacturer": profile.manufacturer,
            "nozzle_mm": profile.nozzle_mm,
            "material": self.material,
            "build_volume_mm": {"x": profile.build_x, "y": profile.build_y, "z": profile.build_z},
            "enclosure": profile.enclosure,
            "is_resin": profile.extruder == "resin" or profile.nozzle_mm <= 0,
            "constraints": self.constraints.to_dict(),
            "checklist": self.constraints.summary_lines(),
        }

    # --- AI 客户端 -------------------------------------------------------
    def _client(self) -> DeepSeekClient:
        key = self.config.api_key
        if not key:
            raise LlmError(
                "还没有配置 DeepSeek API Key，无法使用 AI 设计功能。",
                hint="请在「设置 → AI 服务」中填入 API Key，或点击右上角的「配置 API」。",
                code="NO_API_KEY",
            )
        api = self.config.config.api
        # glm-4.7-flash 免费档高峰常连发 1305；给它一个同提供商内的降级模型，
        # 限流时自动临时改用，避免整条作业因过载失败（降级会留日志）。
        fallback = None
        if detect_provider(api.base_url) == "glm" and api.model == "glm-4.7-flash":
            fallback = "glm-4-flash"
        return DeepSeekClient(key, api.base_url, api.model, api.timeout_s, fallback)

    def _client_economy(self) -> DeepSeekClient:
        """混合·性价比模式的经济档客户端：规格解析/打印性评审/AI 复核走免费档。

        生成/修改/几何修复仍用主模型（付费质量档）—— 结构质量是钱该花的地方；
        解析/评审这类「判断型」调用免费档足够，省下的就是性价比。
        未配置经济档时退回主客户端，行为与原来完全一致。
        """
        api = self.config.config.api
        if not (api.economy_model and api.economy_base_url):
            return self._client()
        base = api.economy_base_url
        key = self.config.api_key_for(base) or self.config.api_key
        fallback = None
        if detect_provider(base) == "glm" and api.economy_model == "glm-4.7-flash":
            fallback = "glm-4-flash"
        return DeepSeekClient(key, base, api.economy_model, api.timeout_s, fallback)

    # --- 主流程 -----------------------------------------------------------
    def _requirement_text(self) -> str:
        """需求原文：**本次指令 + 项目最初那句诉求**（生成用 prompt / 修改用 instruction）。

        为什么要有个方法而不是各处自己拼：几何完整性判定要用它（例如判断需求里
        有没有提"螺纹"），少传一处就少判一类问题。踩过的坑：加了这个调用却没把
        方法本身加进类里，运行到"几何修复"这条分支就直接 AttributeError ——
        而自检当时是绿的（那条分支离线不跑），必须补一条接线测试。

        为什么必须算上最初那句（实测）：项目第一条 prompt 写着「底部是封闭的实心底」，
        而**把底挖穿的那一步**指令是「在顶部正中间增加一个直径10mm 的圆孔，
        要完全贯通到底部。」——只看本次指令，所有按需求文本门控的判定（实心底、螺纹）
        都不会出场，产品就对着一个没有底的笔筒说“改好了”。同一个字符串还是【散件
        重生成】的输入（下面 `generate_scad`），只喂「把高度改成50mm」会重生成一个
        与项目无关的东西。

        为什么不拿 spec.features 那种 AI 改写去补：实测同一句“实心底”被改写成
        「底部为实心封闭结构」（不在词表里）——门控要用**用户原话**，不用二手转述。
        """
        current = getattr(self, "_requirement", "") or ""
        base = getattr(self, "_requirement_base", "") or ""
        if not base or base in current:
            return current
        if current in base:
            return base
        return f"{base} {current}"[:_REQUIREMENT_MAX_CHARS]

    async def run(
        self,
        *,
        project: Project,
        prompt: str | None = None,
        instruction: str | None = None,
        params: dict[str, float] | None = None,
        scad_source: str | None = None,
        compile_params: dict[str, float] | None = None,
    ) -> None:
        job = self.job
        # 本次作业的「需求原文」：几何完整性判定要用它（例如需求里有没有提螺纹）。
        # 用一个方法取，而不是各处自己拼 prompt/instruction —— 少一处就等于漏判一类。
        self._requirement = " ".join(part for part in (prompt, instruction) if part)
        # 项目最初那句诉求（存于 history.jsonl 第一条）：拼到需求原文里去，见
        # `_requirement_text()` 的实测理由。
        self._requirement_base = _project_baseline_requirement(project)
        # 本次改动的授权契约（生成类作业为 None）：几何判定要用它区分
        # 「用户授权改的项」与「规格里原来点名、本次没让动的项」。
        self._change_contract = None
        # **归档当前版本必须放在最前面**，因为编译会把新的 model.scad / model.stl
        # 直接写进项目目录（job_work_dir 就是项目目录）。放在收尾阶段归档，
        # 存进 revisions/rNNN 的其实是第 N+1 版的内容 —— 实测就是这样：
        # revisions/r001 里躺着第 2 版（高度已改）的源码，
        # 于是"恢复历史版本"会把用户带回一个错误的模型。
        if project.revision > 0:
            try:
                projects.store().archive_revision(
                    project,
                    project.revision,
                    keep_stl=self.config.config.behavior.keep_stl_history,
                )
            except Exception as exc:  # noqa: BLE001 - 归档失败不该挡住生成
                log.warning("归档当前版本失败（不影响本次生成）：%s", exc)
        # 进度心跳：阶段内按时间推进百分比（用户反馈"进度条不能体现准确的进度"）。
        # 单开一个任务，而不是塞进各个步骤里 —— 否则每个耗时步骤都得记得推一次，
        # 漏一处进度条就停在那儿。3 秒一拍足够让细条"在动"，又不会把事件流刷爆。
        ticker = asyncio.create_task(self._tick_progress())
        try:
            if job.mode == "create":
                assert prompt is not None
                await self._pipeline_generate(project, prompt)
            elif job.mode == "modify":
                assert instruction is not None
                await self._pipeline_modify(project, instruction)
            elif job.mode == "params":
                await self._pipeline_params(project, params or {})
            elif job.mode == "compile":
                assert scad_source is not None
                await self._pipeline_compile_source(project, scad_source, compile_params)
            else:  # pragma: no cover - 防御
                job.fail("未知的任务类型。", code="INTERNAL")
        except asyncio.CancelledError:
            job.cancel()
            raise
        except LlmError as exc:
            job.fail(exc.message, hint=exc.hint, code=exc.code)
        except scad_cli.OpenScadError as exc:
            job.fail(exc.message, hint=exc.hint, code=exc.code)
        except compiler.CompileError as exc:
            job.fail(exc.message, hint=exc.hint, code=exc.code)
        except StlError as exc:
            job.fail(exc.message, hint=exc.hint, code=exc.code)
        except OSError as exc:
            job.fail(
                f"文件操作失败：{exc.strerror or exc}",
                hint="请检查项目目录是否可写、磁盘是否已满。可在「系统诊断」中查看文件系统状态。",
                code="DISK_FULL" if getattr(exc, "errno", None) == 28 else "INTERNAL",
            )
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不把堆栈丢给用户
            log.exception("生成作业异常")
            job.fail(
                "生成过程中出现了意外问题。",
                hint=f"技术细节：{type(exc).__name__}: {exc}",
                code="INTERNAL",
            )
        finally:
            # 无论成功失败都要停掉心跳，否则它会在作业结束后继续改 percent
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await ticker

    async def _tick_progress(self) -> None:
        """每 PROGRESS_TICK_S 秒推进一次阶段内进度（见 Job.creep）。"""
        try:
            while True:
                await asyncio.sleep(PROGRESS_TICK_S)
                self.job.creep()
        except asyncio.CancelledError:
            return

    # --- 各模式 -----------------------------------------------------------
    async def _pipeline_generate(self, project: Project, prompt: str) -> None:
        """从零设计：先解析规格 → 必要时请用户确认 → 再按规格生成。

        为什么要有「确认规格」这一步：以前是把用户的一句散文直接丢给模型自由发挥，
        方向一开始就可能偏，用户只能看到结果才发现「这不是我要的」。
        现在模型必须先交出它理解的规格（品类、尺寸表、必需结构、禁止项、验收判据），
        用户看一眼就能发现理解偏差，改一句描述即可 —— 代价远低于重生成。
        """
        job = self.job
        profile = self.printer
        if self.config.config.printer.announce_in_chat:
            job.say(
                f"目标打印机：{profile.name}，材料：{self.material}。",
                detail="；".join(self.constraints.summary_lines()[:5]),
            )

        client = self._client()

        # --- 规格解析 ---------------------------------------------------
        job.set_stage("understanding", text="正在理解你的需求…")
        spec = DesignSpec()
        try:
            previous = DesignSpec.from_dict(project.spec) if project.spec else None
            spec = await self._client_economy().parse_design_spec(prompt, previous)
        except LlmError:
            raise
        except Exception as exc:  # noqa: BLE001 - 规格解析失败不应阻断生成
            log.warning("规格解析失败，退化为直接生成：%s", exc)

        if spec.is_empty():
            # 解析不出来：标准零件仍走确定性直出（不依赖规格），其余退回旧路径。
            source = self._deterministic_part_source(prompt, spec)
            if source:
                job.say("命中标准零件，直接生成已验证几何（不经 AI 画螺纹）。", kind="success")
            else:
                job.say("未能整理出明确规格，将按你的原话直接设计。", kind="warn")
                result = await client.generate_scad(prompt, None, self.printer_block)
                source = scad_src.strip_code_fences(result.content)
                self._log_usage(result)
            source = await self._ensure_compilable(client, source, project=project, prompt=prompt)
            await self._finish(project, source, prompt=prompt, requirement=prompt)
            return

        spec.merge_clarification(prompt)
        self._announce_spec(job, spec)

        # --- 确认关卡 ---------------------------------------------------
        if self._should_confirm(spec):
            job.awaiting_spec = spec
            job.status = "awaiting_confirmation"
            job.set_stage("awaiting_confirmation", text="请确认我对需求的理解是否正确")
            self._publish_spec(job, spec)
            approved = await job.wait_for_spec()

            if job.status == "cancelled":
                return
            if approved is not None:
                # 用户可能改了尺寸或补了说明
                spec = approved
                spec.merge_clarification(prompt)
                # 已经确认过了 → awaiting=False，界面据此把卡片收成只读版
                self._publish_spec(job, spec, awaiting=False)
            job.status = "running"
            job.say("已确认，开始设计。", kind="success")
        else:
            job.say("规格已明确，直接开始设计。")

        # --- 按规格生成 -------------------------------------------------
        job.set_stage("generating_scad", text="正在按确认的规格设计结构…")
        # 标准零件且需求明确时**确定性直出**已验证几何，不经 AI 画螺纹/填参 ——
        # 这是「67 转 77 滤镜环始终做不对」这类问题的根治：口径映射与几何都由程序保证。
        source = self._deterministic_part_source(prompt, spec)
        if source:
            job.say("命中标准零件，直接生成已验证几何（不经 AI 画螺纹）。", kind="success")
        else:
            result = await client.generate_from_spec(prompt, spec, self.printer_block)
            source = scad_src.strip_code_fences(result.content)
            self._log_usage(result)
        source = embed_fingerprint(source, spec)

        source = await self._ensure_compilable(client, source, project=project, prompt=prompt)
        await self._finish(project, source, prompt=prompt, requirement=prompt, spec=spec)

    def _announce_spec(self, job: Job, spec: DesignSpec) -> None:
        """在对话里用最少的话说明「我理解成了什么」，并把**我替你定的具体取值**摊开。

        只说「有几点你没说清楚，我按常见做法先定了」而不给出数字，用户就没有可纠正的对象 ——
        实测用户反馈「对话大模型无法领会意思」，很大一块正是这类没露面就生效的假设
        （夹口开度、配合孔径、壁厚全部在内）：程序以为已经交代了，用户根本不知道定了什么。
        所以必须把值念出来，并明确鼓动用户纠正。
        """
        headline = spec.summary or spec.kind or "你的需求"
        job.say(f"我理解你要的是：{headline}", detail=spec.to_prompt()[:400])
        assumed = [it for it in spec.specs if not it.user_specified and it.value is not None]
        undecided = [it for it in spec.specs if not it.user_specified and it.value is None]
        if not assumed and not undecided and not spec.open_questions:
            return
        chunks: list[str] = []
        if assumed:
            chunks.append(
                "我按常见做法取了值："
                + "、".join(
                    f"{it.label or it.name} {float(it.value):g}{it.unit or 'mm'}"
                    for it in assumed[:8]
                )
            )
        if undecided:
            chunks.append(
                "这几项规格里没数值，我在代码里按品类惯例定了值："
                + "、".join(str(it.label or it.name) for it in undecided[:5])
            )
        if spec.open_questions:
            chunks.append("不确定处：" + "；".join(str(q).strip() for q in spec.open_questions[:2]))
        job.say(
            "；".join(chunks) + "。哪一项不对，直接说「把××改成多少」就行。",
            kind="warn",
        )

    def _publish_spec(self, job: Job, spec: DesignSpec, awaiting: bool = True) -> None:
        """把规格推给界面（用于渲染成可确认/可纠正的卡片）。"""
        publish_spec(job, spec, awaiting=awaiting)

    def _should_confirm(self, spec: DesignSpec) -> bool:
        """什么时候需要用户确认规格。

        策略：默认要确认（这是防「方向跑偏」最有效的一步），但两种情况下跳过 ——
        用户在设置里关掉了，或者需求本身就极简（一句话、无尺寸），
        此时确认带来的打扰大于收益。
        """
        if not self.config.config.printer.confirm_spec:
            return False
        # 极简需求：没有数值、没有禁止项、结构项也很少 → 直接做
        numeric = [s for s in spec.specs if s.value is not None]
        if not numeric and not spec.prohibitions and len(spec.features) <= 1:
            return False
        return True

    @staticmethod
    def _spec_is_plain(spec: DesignSpec) -> bool:
        """规格是否「只是一个标准零件」：无禁止项、特征全在白名单内。

        只要用户提了白名单之外的特征（挂绳孔、刻字、卡扣……），就回退 AI ——
        直出只保证「纯零件」正确，带额外结构时必须让 AI 在零件之上加东西。
        """
        if spec.prohibitions:
            return False
        return all(
            any(word in feature for word in PLAIN_PART_FEATURES)
            for feature in spec.features
            if feature.strip()
        )

    def _deterministic_part_source(self, requirement: str, spec: DesignSpec) -> str | None:
        """需求明确命中标准零件时，直接渲染已验证几何的源码；否则返回 None 走 AI。"""
        from .parts import catalog as parts_lib

        call = parts_lib.extract_call(requirement, spec)
        if call is None or not self._spec_is_plain(spec):
            return None
        part, values = call
        return parts_lib.render_source(
            part,
            values,
            name=(spec.kind or spec.summary or part.name),
            summary=(spec.summary or part.summary),
        )

    async def _pipeline_modify(self, project: Project, instruction: str) -> None:
        """续改：先立变更契约 → 只改授权内容 → 校验 → 不达标带差异重做一次。

        这是「改完可能不会改善」的正面解法。旧版只说「保持原有结构」，
        模型无从判断什么算原有结构，于是经常顺手重写别的部分。
        """
        job = self.job
        current = project.read_scad()
        if not current:
            # 没有历史模型时，把修改指令当成一次全新设计
            await self._pipeline_generate(project, instruction)
            return

        client = self._client()

        # --- 规格增量更新（把这次的要求并进既有契约，避免遗忘）----------
        spec = DesignSpec.from_dict(project.spec) if project.spec else DesignSpec()
        job.set_stage("understanding", text="正在理解你的修改要求…")
        try:
            updated = await self._client_economy().parse_design_spec(instruction, spec if not spec.is_empty() else None)
            if not updated.is_empty():
                # 保留旧规格里用户指定过的项与禁止项，再并入新信息
                spec = merge_specs(spec, updated)
        except Exception as exc:  # noqa: BLE001
            log.info("修改时的规格更新失败（继续按契约改）：%s", exc)
        spec.merge_clarification(instruction)

        # --- 变更契约 ---------------------------------------------------
        current_params = scad_src.extract_top_level_numbers(current)
        # 注释标签（`// @param total_h 100 总高`）是规格名 ↔ 源码参数名之间最可靠的桥：
        # 少了它，「规格叫 height、源码叫 total_h」时整条修改链路会静默失效（§73）。
        contract = build_change_contract(
            instruction,
            spec,
            current_params,
            scad_src.extract_param_labels(current),
        )
        self._change_contract = contract
        job.say(
            "本次只改这些，其余保持不变。",
            detail=contract.describe(),
        )

        # --- 按契约修改 -------------------------------------------------
        job.set_stage("generating_scad", text="正在按要求修改模型…")
        result = await client.modify_with_contract(current, contract, spec, self.printer_block)
        source = scad_src.strip_code_fences(result.content)
        self._log_usage(result)

        # --- 校验第一道：程序化核对（参数级，确定性）--------------------
        job.set_stage("verifying", text="正在核对改动是否符合你的要求…")
        verdict = verify_change(current, source, contract, spec)
        # --- 校验第二道：AI 复核（结构级，看「参数对了但改坏了」这类问题）----
        _merge_ai_review(verdict, await self._ai_second_opinion(current, source, contract, spec, verdict))

        if not verdict.ok:
            job.say(
                "这次修改没有完全达标，正在带着具体差异重做…",
                detail=verdict.describe(),
                kind="warn",
            )
            try:
                repaired = await client.repair_change(current, source, verdict, contract)
                candidate = scad_src.strip_code_fences(repaired.content)
                recheck = verify_change(current, candidate, contract, spec)
                # 程序化通过后再请 AI 看一眼结构有没有被改坏。
                # 程序化已经不通过时就不再问 AI —— 那个版本注定会被丢掉，问了是白花钱。
                if recheck.ok:
                    _merge_ai_review(
                        recheck,
                        await self._ai_second_opinion(current, candidate, contract, spec, recheck),
                    )
                if recheck.ok:
                    source = candidate
                    verdict = recheck
                    job.say("已修正，改动符合契约。", kind="success")
                else:
                    # 两次都不达标：先试确定性方案 —— 把授权参数的目标值直接写回
                    # 原始源码，别处一个字都不动。这比让 AI 再猜一次可靠得多：
                    # 「只改该改的」本来就是文本替换能做到的事。
                    surgical = _apply_targets(current, contract)
                    if surgical is not None and surgical != current:
                        surgical_verdict = verify_change(current, surgical, contract, spec)
                        if surgical_verdict.ok:
                            source = surgical
                            verdict = surgical_verdict
                            job.say(
                                "已直接按你的目标值改动，其余部分原样保留。",
                                kind="success",
                            )
                    if not verdict.ok and contract.scale_factor:
                        # 整体缩放没有"目标参数值"可写（它改的是所有尺寸的比例），
                        # 所以上面那条兜底对它是空转 —— 实测「整体缩小到80%」
                        # 因此变成静默无操作：作业报成功，模型一毫米没变。
                        # 缩放本身是纯变换，程序自己就能做（拿现成的
                        # build_scaled_source，成型范围兜底一直在用它）。
                        scaled = build_user_scaled_source(
                            current, float(contract.scale_factor), self.printer.name
                        )
                        if scaled and scaled != current:
                            scale_verdict = verify_change(current, scaled, contract, spec)
                            if scale_verdict.ok:
                                source = scaled
                                verdict = scale_verdict
                                job.say(
                                    "已按你的比例整体缩放，其余结构保持不变。",
                                    kind="success",
                                )
                    if not verdict.ok:
                        # 仍不达标：在「重做版 / 首版 / 完全没改」三者里挑代价最小的那一版，
                        # 并如实说明。注意「完全没改」也参与比较，不允许它靠无害白占便宜。
                        chosen = _pick_least_bad(
                            current,
                            [candidate, source, current],
                            contract,
                            spec,
                        )
                        source = chosen
                        verdict = verify_change(current, source, contract, spec)
                        # 最终版本如果是被 AI 判过结构问题的，把那条结论带回来
                        if source == candidate:
                            _merge_ai_review(
                                verdict,
                                await self._ai_second_opinion(
                                    current, source, contract, spec, verdict
                                ),
                            )
                        # 把「选了哪一版」写进结论，否则线上只能从最终尺寸反推
                        if source == current:
                            verdict.reasons.append(
                                "两个候选都会改坏别处，已保留改动前的版本（宁可不改，也不改坏）"
                            )
                        job.say(
                            "两次都没能完全按要求改好，已保留最接近的一版，并保留你的要求记录。",
                            detail=verdict.describe(),
                            kind="warn",
                        )
            except LlmError as exc:
                job.say("重做时调用 AI 失败，保留当前结果。", detail=exc.message, kind="warn")
            except Exception as exc:  # noqa: BLE001
                # 重做路径里任何意外都不该让整次修改失败：现有结果已经可用，
                # 只是「不够干净」。如实记一笔，继续走兜底与收尾。
                log.warning("重做/兜底改动的过程中出错（保留现有结果）：%s", exc, exc_info=True)
        else:
            job.say("改动符合你的要求，其它部分未变。", kind="success")

        # 改动生效后，把用户授权的目标值写进**规格本身**。
        # 不这么做的话，规格永远是项目最初那版（例如"总高 100mm"），
        # 下一轮检查又会拿它去要求已经改成 50mm 的模型 —— 互相打架（见 §72）。
        if verdict.ok and contract.target_values:
            updated = spec.apply_authorized_change(contract.target_values)
            if updated:
                job.say(
                    "已把这次改动的数值记进设计规格。",
                    detail="更新：" + "、".join(updated),
                )

        source = embed_fingerprint(source, spec)
        # 改动前的实测尺寸：上一次生成时已经测量过并存在项目里，直接取即可，
        # 不必为了对比再编译一次（那是几秒的纯浪费）。
        previous_values = scad_src.extract_top_level_numbers(current)
        previous_largest = _cached_largest_dimension(project)
        source = await self._ensure_compilable(
            client, source, project=project, prompt=instruction, previous=current
        )
        await self._finish(
            project,
            source,
            instruction=instruction,
            requirement=instruction,
            spec=spec,
            change_verdict=verdict,
            contract=contract,
            previous_values=previous_values,
            previous_largest=previous_largest,
        )

    async def _ai_second_opinion(
        self,
        before: str,
        after: str,
        contract: ChangeContract,
        spec: DesignSpec | None,
        verdict: ChangeVerdict,
    ) -> dict[str, object] | None:
        """AI 复核这次改动（第二道校验）。任何失败都返回 None，绝不让它拖垮作业。

        什么时候才值得问：
          * 程序化核对已经失败 → 这一版注定会被丢掉/重做，问了是白花钱，直接跳过；
          * 改动前后代码完全相同 → 没有可复核的内容。

        为什么要问：程序化核对只能比参数值，看不出「参数对了但结构被改坏」——
        底座改窄到站不稳、承力处被削薄、支撑改成悬空，这些在参数 diff 里毫无痕迹。
        """
        if not verdict.ok:
            return None
        if scad_src.strip_comments(before).strip() == scad_src.strip_comments(after).strip():
            return None
        job = self.job  # 实测踩到：漏了这行，AI 复核一提出异议就 NameError 崩掉整个 modify 作业
        try:
            client = self._client_economy()  # 复核属「判断型」调用，混合模式走免费档
            review = await client.review_change(before, after, contract, spec)
        except LlmError as exc:
            log.info("AI 复核未完成（程序化核对结果仍然有效）：%s", exc.message)
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("AI 复核出错（忽略）：%s", exc, exc_info=True)
            return None
        if review.get("requested_change_applied") is not True or review.get("functional_ok") is not True:
            # 只有真的提出异议才把结论说出来，避免每次改动都刷一条「AI 看过了」
            log.info("AI 复核提出异议：%s", _describe_ai_review(review))
            job.say(
                "AI 复核提出了疑问，正在按它指出的地方重做…",
                detail=_describe_ai_review(review),
                kind="warn",
            )
        else:
            log.info("AI 复核通过：%s", review.get("summary") or "（无摘要）")
        return review

    async def _pipeline_params(self, project: Project, params: dict[str, float]) -> None:
        """参数微调：不调用 AI，直接改源码里的数值并重新编译，秒级完成。"""
        job = self.job
        current = project.read_scad()
        if not current:
            job.fail("这个项目还没有模型，请先用一句话描述你要设计的东西。", code="INTERNAL")
            return
        job.set_stage("validating_scad", text="正在应用参数…")
        await self._finish(project, current, params=params, requirement=None)

    async def _pipeline_compile_source(
        self,
        project: Project,
        source: str,
        params: dict[str, float] | None = None,
    ) -> None:
        job = self.job
        job.set_stage("validating_scad", text="正在检查模型代码…")
        validation = scad_src.validate(source)
        if not validation.ok:
            reason = "；".join(i.detail for i in validation.issues if i.severity == "error")
            job.fail(
                "模型代码没有通过安全检查。",
                hint=reason or "请检查代码后重试。",
                code="SCAD_INVALID",
            )
            return
        await self._finish(project, source, params=params, requirement=None)

    # --- 校验 / 自动修复 --------------------------------------------------
    async def _ensure_compilable(
        self,
        client: DeepSeekClient,
        source: str,
        *,
        project: Project,
        prompt: str | None,
        previous: str | None = None,
    ) -> str:
        """校验代码；必要时让 AI 修复一次。返回最终用于编译的源码。"""
        job = self.job
        job.set_stage("validating_scad", text="正在检查模型代码…")
        validation = scad_src.validate(source)

        if not validation.ok:
            problem = "；".join(f"{i.title}：{i.detail}" for i in validation.issues if i.severity == "error")
            for issue in validation.issues:
                if issue.severity == "error":
                    job.say(f"检查发现问题：{issue.title}", detail=issue.detail, kind="warn")
            job.progress(52.0, "正在让 AI 修正代码问题…")
            # 校验修复给两次机会：实测 glm 偶发产出触发安全校验的代码（禁用调用/外部引用），
            # 一次修复有时删不干净，直接终止会让用户拿不到模型。
            candidate = source
            for _attempt in range(2):
                repaired = await client.repair_scad(candidate, problem)
                candidate = scad_src.strip_code_fences(repaired.content)
                revalidation = scad_src.validate(candidate)
                if revalidation.ok:
                    job.say("已修正代码问题。", kind="success")
                    return candidate
                problem = "；".join(
                    f"{i.title}：{i.detail}" for i in revalidation.issues if i.severity == "error"
                )
            reason = problem
            raise LlmError(
                "AI 生成的模型代码没有通过安全检查，已停止生成。",
                hint=f"原因：{reason}。请换一种描述方式再试一次。",
                code="SCAD_INVALID",
            )

        for issue in validation.issues:
            if issue.severity == "warn":
                job.say(f"提示：{issue.title}", detail=f"{issue.detail} {issue.suggestion}", kind="warn")

        # 编译一次，失败则让 AI 依据真实报错修复（第四十九节：错误恢复）
        job.set_stage("compiling", text="正在生成 3D 模型…")
        compile_result = await asyncio.to_thread(
            compiler.compile_to_stl,
            source,
            job_work_dir(project),
            "model",
            None,
            None,
            self.resolve_fn_for(source),
        )
        if compile_result.ok:
            job.say("模型生成成功。", kind="success")
            return source

        job.say("模型代码编译失败，正在让 AI 自动修复…", detail=compile_result.message, kind="warn")
        job.progress(70.0, "正在自动修复模型代码…")
        # 修复证据 = 已知成因的**可执行改法**（如果有）+ OpenSCAD 原文。
        # 只丢原文时（CGAL assert 这种），AI 两轮改的还是同一个错 —— 实测踩过。
        evidence = compile_result.fatal or compile_result.message
        if compile_result.advice:
            evidence = f"{compile_result.advice}\n\nOpenSCAD 原始报错：\n{evidence}"
        repaired = await client.repair_scad(previous or source, evidence)
        candidate = scad_src.strip_code_fences(repaired.content)
        revalidation = scad_src.validate(candidate)
        if not revalidation.ok:
            reason = "；".join(i.detail for i in revalidation.issues if i.severity == "error")
            raise compiler.CompileError(
                "模型代码存在错误，自动修复未能成功。",
                hint=f"原因：{reason or compile_result.message}。请调整你的描述后重试。",
                code="OPENSCAD_FAILED",
            )

        retry = await asyncio.to_thread(
            compiler.compile_to_stl,
            candidate,
            job_work_dir(project),
            "model",
            None,
            None,
            self.resolve_fn_for(candidate),
        )
        if retry.ok:
            job.say("已自动修复并生成成功。", kind="success")
            return candidate

        # 修复两轮仍失败 → 换一条生成路径重试一次（P0：生成随机性加固）。
        # 再修同一份代码往往卡在同一个错误写法上；带着硬约束重新生成是另一条路。
        if prompt:
            job.say("自动修复仍失败，正在换一条建模路径重新生成…", kind="warn")
            try:
                regen = await client.generate_scad(prompt, _REGEN_EXTRA, self.printer_block)
                candidate2 = scad_src.strip_code_fences(regen.content)
                if scad_src.validate(candidate2).ok:
                    retry2 = await asyncio.to_thread(
                        compiler.compile_to_stl,
                        candidate2,
                        job_work_dir(project),
                        "model",
                        None,
                        None,
                        self.resolve_fn_for(candidate2),
                    )
                    if retry2.ok:
                        job.say("换路径重新生成成功。", kind="success")
                        return candidate2
            except LlmError as exc:
                log.warning("换路径重生成失败：%s", exc)

        raise compiler.CompileError(
            "模型代码无法编译为 3D 模型，自动修复后仍然失败。",
            hint=f"OpenSCAD 报告：{retry.fatal or retry.message}",
            code="OPENSCAD_FAILED",
        )

    # --- 收尾：编译 → 分析 → 打印性检查 → 存盘 --------------------------
    async def _finish(
        self,
        project: Project,
        source: str,
        *,
        prompt: str | None = None,
        instruction: str | None = None,
        params: dict[str, float] | None = None,
        requirement: str | None = None,
        spec: DesignSpec | None = None,
        change_verdict: ChangeVerdict | None = None,
        contract: ChangeContract | None = None,
        previous_values: dict[str, float] | None = None,
        previous_largest: float | None = None,
    ) -> None:
        job = self.job
        revision = project.revision + 1
        job.revision = revision

        validation = scad_src.validate(source)
        # 只取注释元数据里的名字与摘要。`validation.params`（从源码解析出的参数）
        # **不在这里取**：参数是经 `params` / `param_meta` 单独传给落盘层的
        # （见本函数后面的 _persist 调用），那一路才是权威来源。原来在这里
        # 多解构一个 `meta_params` 却从不使用，读代码的人会误以为参数走的是
        # 这条路 —— 与「算出来却不用」是同一类误导，直接不取。
        meta_name, meta_summary = validation.model_name, validation.summary

        # 1) 编译（$fn 按这台打印机 + 这个模型的真实尺寸决定）
        job.set_stage("compiling", text="正在生成 3D 模型…")
        limits = self.analysis_limits
        compile_result = await asyncio.to_thread(
            compiler.compile_to_stl,
            source,
            job_work_dir(project),
            "model",
            params,
            None,
            self.resolve_fn_for(source),
        )
        if not compile_result.ok:
            job.fail(
                compile_result.message,
                hint=compile_result.hint,
                code="OPENSCAD_FAILED",
            )
            return

        job.say(f"3D 模型已生成（{compile_result.duration_s:.1f} 秒）。", kind="success")

        # 3) 网格分析（阈值来自选中的打印机）
        job.set_stage("analyzing", text="正在检查模型尺寸与结构…")
        metrics: MeshMetrics | None = None
        metrics_error: str | None = None
        try:
            metrics = await _analyze_with_heartbeat(job, compile_result.stl_path, limits, source=source)
            # 耗材用量需要材料密度，而 analyze 只拿到工艺约束 —— 在这里补算。
            # 密度查表见 printers.material_density（PLA 1.24、ABS 1.04、PETG 1.27 …）。
            metrics.usage = estimate_usage(
                metrics,
                material=self.material,
                density_g_cm3=printers.material_density(self.material),
                support=metrics.support,
                wall_lines=2,
                line_width_mm=limits.nozzle_mm if limits.nozzle_mm > 0 else 0.4,
                infill_pct=15.0,
            )
        except StlError as exc:
            metrics_error = exc.message
            job.say("模型尺寸分析未完成。", detail=exc.message, kind="warn")

        # 3.5) 成型范围硬校验：光靠提示词约束不住，必须用实测尺寸兜底重做一次。
        #      这是「懂打印机」这件事真正生效的地方 —— 尺寸不对就重来，
        #      而不是把一个装不进机器的模型交给用户。
        if metrics is not None and metrics.exceeds_build_volume and job.mode in self._BUILD_VOLUME_STRICT_MODES:
            source, compile_result, metrics = await self._refit_to_build_volume(
                source, compile_result, metrics, limits, params, project
            )

        # 3.6) 渲染 + 让多模态模型**真的看一眼**。
        #      必须放在几何修复**之前**：视觉复核能看出程序测不出来的问题
        #      （实测它报告过「桶口边缘有两个小凸起，与加强筋和夹持功能不对应」，
        #      而那个模型 solid_parts=1、无孤岛、包围盒正常，程序全是绿灯）。
        #      把它的结论并入修复证据，AI 就能连这些一起修。
        #      渲染或复核失败只是跳过这一条，绝不影响出图。
        visual_review = ""
        review_client: DeepSeekClient | None = None
        if metrics is not None and requirement:
            try:
                review_client = self._client()
            except Exception as exc:  # noqa: BLE001 - 没有可用 AI 时跳过复核
                log.warning("跳图片复核（无法创建 AI 客户端）：%s", exc)
            if review_client is not None:
                visual_review = await self._review_snapshot(
                    review_client, compile_result, metrics, requirement, project
                )

        # 3.7) 几何完整性硬校验：模型「是不是一整件」「点名尺寸有没有做到」
        #      「特征有没有穿进腔体」。
        #       提示词里已经把这几条写成了铁律，实测仍然有 5/6 的用例第一次就翻车 ——
        #       文字约束不住，但这些都是程序能精确观测的
        #       （连通分量数、截面上的材料环嵌套关系、实测包围盒 vs 规格声明值），
        #       所以最可靠的做法是把实测证据交回给 AI，让它只修这一处。
        if metrics is not None:
            repair_client = review_client
            if repair_client is None:
                try:
                    repair_client = self._client()
                except Exception as exc:  # noqa: BLE001 - 没有可用 AI 时跳过修复，不影响出图
                    log.warning("跳几何完整性修复（无法创建 AI 客户端）：%s", exc)
            if repair_client is not None:
                source, compile_result, metrics = await self._repair_geometry_integrity(
                    repair_client,
                    source,
                    compile_result,
                    metrics,
                    limits,
                    params,
                    project,
                    spec,
                    visual=visual_review,
                    dropped=list(getattr(compile_result, "dropped_geometry", []) or []),
                )

        if metrics is not None and metrics.issues:
            # 让用户马上看到按这台机器判定的问题，而不是等 AI 回复
            job.say(
                f"按 {self.printer.name} 检查发现 {len(metrics.issues)} 处需要注意。",
                detail="；".join(metrics.issues[:2]),
                kind="warn",
            )

        # 3.6) 参数与几何自洽性：被授权的参数改了，几何必须跟着变。
        #      实测踩到过：用户要求「高度改成150mm」，AI 把 height 参数写成 150，
        #      模型总高却从 101mm 掉到 38mm —— 旧契约核对（只看参数）判了「通过」，
        #      用户拿到的却是一个完全不对的模型。这里用两次实测尺寸把它拦住。
        if change_verdict is not None and metrics is not None and contract is not None:
            mismatched = _implausible_targets(
                source,
                contract,
                metrics,
                previous_values=previous_values,
                previous_largest=previous_largest,
            )
            scale_gap = _scale_mismatch(contract, metrics, previous_largest)
            if scale_gap:
                mismatched = [*mismatched, scale_gap]
            if mismatched:
                change_verdict.ok = False
                change_verdict.implausible.extend(mismatched)
                change_verdict.reasons.append("参数与几何对不上：" + "；".join(mismatched[:3]))
                job.say(
                    "改完的参数和模型实际尺寸对不上，这次改动不算成功。",
                    detail="；".join(mismatched[:2]),
                    kind="warn",
                )

        # 2) 读回真正写入磁盘的源码 —— 必须放在成型范围重做之后，
        #    否则重做成功时这里读到的还是被丢弃的那一版。
        final_source = source
        try:
            final_source = compile_result.scad_path.read_text(encoding="utf-8")
        except OSError:
            pass

        effective_params = scad_src.extract_top_level_numbers(final_source)
        param_meta = build_param_meta(final_source, effective_params)

        # 3.8) 需求核对：把契约编译成逐条判定，既进清单也进落盘数据。
        #      必须算在打印性检查**之前**：打印性判定的降级要靠这些实测结论。
        requirement_suite = self._requirement_suite(metrics, spec)
        if requirement_suite is not None:
            # 只对“用户提的要求没做到”开门；工艺类（壁厚/支撑/面数/超范围）
            # 归打印性检查那条链路去说，否则同一件事会被提醒两次。
            blocking = [
                t
                for t in requirement_suite.tests
                if t.status == reqtests.FAIL and not t.advisory
            ]
            summary = requirement_suite.summary()
            if blocking:
                # 以前只在「修复循环残留问题」时说话，未达成项的具体数字用户完全看不到。
                # 现在明确摊开：要求什么、实测什么、差多少。
                job.say(
                    f"需求核对：{round(summary['requirement_score'] * 100)}% 的需求达成，"
                    f"{len(blocking)} 项未达成。",
                    detail="；".join(
                        f"{t.requirement}（要求 {t.expected} / 实测 {t.actual}）"
                        for t in blocking[:4]
                    )[:300],
                    kind="warn",
                )
            else:
                # “全部达成”只能说“能测的都测到了”：manual 项是程序测不了的语义需求，
                # 拿 success 报「全部达成」会让用户以为连外观都算过了。
                manual_tests = [
                    t for t in requirement_suite.tests if t.status == reqtests.MANUAL
                ]
                manual_count = len(manual_tests)
                manual_names = "、".join(t.requirement for t in manual_tests[:4])
                job.say(
                    "需求核对：可测项全部达成"
                    + (f"（另有 {manual_count} 项程序测不了）" if manual_count else "。"),
                    detail=f"{summary['counts']['pass']} 项实测通过"
                    + (
                        f"；「{manual_names[:80]}」这几项只有外观复核/人工能确认"
                        if manual_count
                        else ""
                    ),
                    kind="info" if manual_count else "success",
                )

        # 3.9) 实测硬失败也必须让「改动核对结论」闭嘴。
        #      实测过：AI 复核说「改动成功，满足用户要求且未破坏其他功能」，
        #      而网格上声明的夹口根本不存在 —— 参数级核对看不出这一类，几何能。
        hard_fails = _hard_failures(requirement_suite)
        if change_verdict is not None and hard_fails and change_verdict.ok:
            change_verdict.ok = False
            change_verdict.reasons.append(
                "实测发现要求没做到："
                + "；".join(
                    f"{t.requirement}（要求 {t.expected or '—'} / 实测 {t.actual or '量不到'}）"
                    for t in hard_fails[:3]
                )[:300]
            )
            job.say(
                "改动后的模型实测下来还有要求没做到，这次改动不算完全成功。",
                detail=change_verdict.reasons[-1],
                kind="warn",
            )

        # 4) AI 打印性检查（以选中打印机为准绳）
        printability: dict[str, Any] | None = None
        job.set_stage("reviewing", text=f"正在按 {self.printer.name} 做打印性检查…")
        if metrics is not None:
            try:
                client = self._client_economy()
                summary = summarize_for_ai(metrics, project.name, limits)
                review = await client.review_printability(
                    final_source, summary, requirement, self.printer_block
                )
                if review is None:
                    # 以前这里写的是 `or {}`：拿到 None 就用空字典接着往下跑，而下游
                    # `_downgrade_printability` 与 `verdict_key = str(printability.get("verdict"))`
                    # 对「没有这一栏」都会默默拿默认值（以前甚至是 70/warn）继续往下走，
                    # 于是「AI 回复格式不对」与「AI 检查过了，没发现问题」在界面上看不出区别。
                    # `parse_review_json` 现在把这两种情况分开了，调用方必须自己接住「None」。
                    job.say(
                        "打印性检查未完成（AI 回复不是预期结构），但模型已生成成功。",
                        detail="可以点击「重新生成」再试一次；程序自己量的几何数据仍然可靠，不受影响。",
                        kind="warn",
                    )
                else:
                    printability = review
                    if visual_review:
                        printability["visual_review"] = visual_review
                    metrics_note = "；".join(metrics.issues) if metrics.issues else "程序测量未发现明显几何问题。"
                    printability["metrics_note"] = metrics_note
                    printability["printer"] = self.printer_context()
                    # 判定诚实化：实测硬失败 / 外观复核报的问题 / 修复残留，都必须拖低 verdict 与 score。
                    # 不加这一步的后果（实测）：夹口完全没切出来，AI 仍给 pass/85 分，
                    # 而唯一看出问题的外观复核只被当成一段附注文字，用户根本看不到。
                    honesty = _downgrade_printability(
                        printability,
                        requirement_suite,
                        visual_review,
                        self._integrity_remaining,
                    )
                    if honesty:
                        log.info("打印性判定已按实测证据降级：%s", honesty)
                    verdict_key = str(printability.get("verdict"))
                    verdict_text = {
                        "pass": "可以打印",
                        "warn": "可以打印，但有注意事项",
                        # 不要弱化：判 fail 时模型往往是**非封闭/严重超差**，
                        # 实测用 kind="success" 播报「存在打印风险」，
                        # 界面上看起来就是一条成功提示，用户很容易照打。
                        "fail": "存在必须修复的问题，现在这版不建议直接打印",
                    }.get(verdict_key, "检查完成")
                    job.say(
                        f"打印性检查：{verdict_text}。",
                        detail=str(printability.get("summary") or ""),
                        kind="success" if verdict_key == "pass" else ("error" if verdict_key == "fail" else "warn"),
                    )
            except LlmError as exc:
                job.say("打印性检查未完成，但模型已生成成功。", detail=exc.message, kind="warn")
        else:
            job.say("跳过打印性检查（缺少几何数据）。", kind="warn")

        # 5) 存盘（含设计规格 —— 它是跨会话的记忆载体，必须持久化）
        job.set_stage("exporting", text="正在保存项目…")
        await asyncio.to_thread(
            self._persist,
            project,
            final_source,
            revision,
            metrics,
            printability,
            effective_params,
            param_meta,
            meta_name,
            meta_summary,
            prompt,
            instruction,
            spec,
            requirement_suite,
        )

        stl_path = compile_result.stl_path
        assert stl_path is not None
        stl_metrics = metrics.to_dict() if metrics else None
        filename = projects.store().download_name(project, "stl")
        from .printers import material_density

        printer_ctx = self.printer_context()
        density = material_density(self.material)
        within_build = bool(
            metrics
            and metrics.bbox[0] <= limits.build_x + 0.01
            and metrics.bbox[1] <= limits.build_y + 0.01
            and metrics.bbox[2] <= limits.build_z + 0.01
        )

        job.result = {
            "project_id": project.id,
            "revision": revision,
            "name": project.name,
            "scad": final_source,
            "params": param_meta,
            "summary": meta_summary or "",
            # 本次生成是针对哪台机器、哪种材料做的 —— 界面必须显示，
            # 否则用户换机后无法判断旧模型是否仍适用。
            "printer": printer_ctx,
            "stl": {
                "url": f"/api/projects/{project.id}/stl/raw",
                "filename": filename,
                "size_bytes": stl_path.stat().st_size,
                "triangles": metrics.triangles if metrics else 0,
                "volume_mm3": round(metrics.volume_mm3, 2) if metrics else 0.0,
                "bbox_mm": (
                    {"x": round(metrics.bbox[0], 2), "y": round(metrics.bbox[1], 2), "z": round(metrics.bbox[2], 2)}
                    if metrics
                    else {"x": 0.0, "y": 0.0, "z": 0.0}
                ),
                "watertight": metrics.watertight if metrics else False,
                "watertight_ratio": round(metrics.watertight_ratio, 4) if metrics else 0.0,
                "metrics": stl_metrics,
                # mass_g 是**实体等效**（按 100% 填充算）的重量，也就是用量上限。
                # 别把它当成实际用量：实测的桌面垃圾桶实体等效 191g，
                # 而按「2 层墙 + 15% 填充」（用户参照物 3MF 用的就是这套参数）只有约 111g。
                # 要展示给用户的是下面的 usage（区间 + 支撑 + 线材长度）。
                "mass_g": compiler.estimate_mass_g(metrics, density) if metrics else None,
                "usage": metrics.usage.to_dict() if metrics and metrics.usage else None,
                "material": self.material,
                "material_density": density,
                # 是否放得进这台机器 —— 界面用它决定要不要提示「需要分件/缩小」
                "within_build_volume": within_build,
                "min_dimension_mm": (
                    round(min(v for v in metrics.bbox if v > 0), 2)
                    if metrics and any(v > 0 for v in metrics.bbox)
                    else None
                ),
                "meets_min_wall": bool(metrics and min(v for v in metrics.bbox if v > 0) >= limits.min_wall_mm)
                if metrics and any(v > 0 for v in metrics.bbox)
                else None,
            }
            if stl_path.is_file()
            else None,
            "printability": printability,
            # 设计契约与本次改动核对结论 —— 界面用它说明「我按什么做的、改了什么」
            "spec": spec.to_display() if spec is not None else None,
            # 逐条需求判定 + PR/RS/IR —— 用户判断「这版能不能直接用」的依据
            "requirement_tests": requirement_suite.to_dict() if requirement_suite else None,
            "change_verdict": change_verdict.to_dict() if change_verdict is not None else None,
            "change_summary": summarize_verdict(change_verdict) if change_verdict is not None else None,
            "diagnostics": metrics_error
            or "\n".join(f"[{d.severity}] {d.text}" for d in compile_result.diagnostics[:40])
            or None,
            "export_dir": str(project.export_dir),
        }
        job.say("已保存到你的项目文件夹。", detail=str(project.dir), kind="success")
        job.succeed()

    # --- 落盘 -------------------------------------------------------------
    @staticmethod
    def _metrics_payload(
        metrics: MeshMetrics | None, req_suite: reqtests.ReqSuite | None
    ) -> dict[str, Any] | None:
        """实测数据 + 需求核对清单。

        为什么把清单塞进 metrics：项目重开时界面要显示同一份核对表，
        而 metrics 是唯一已经持久化、且 /api/projects/{id}/metrics 会回传的载荷。
        """
        if metrics is None:
            return None
        payload = metrics.to_dict()
        if req_suite is not None:
            payload["requirement_tests"] = req_suite.to_dict()
        return payload

    def _persist(
        self,
        project: Project,
        source: str,
        revision: int,
        metrics: MeshMetrics | None,
        printability: dict[str, Any] | None,
        params: dict[str, float],
        param_meta: list[dict[str, Any]],
        meta_name: str | None,
        meta_summary: str | None,
        prompt: str | None,
        instruction: str | None,
        spec: DesignSpec | None = None,
        req_suite: reqtests.ReqSuite | None = None,
    ) -> None:
        store_projects = projects.store()

        # 归档放在这里已经太晚：编译早把新的 model.scad / model.stl 写进项目目录了。
        # 现在归档在作业开始时做（见 GenerationRunner.run 的说明）。
        # 这里保留一次幂等兜底：只有当对应目录还不存在时才补归档，
        # 避免把"已经写入的新版本"当成旧版本存进去。
        if project.revision > 0 and not project.revision_dir(project.revision).exists():
            store_projects.archive_revision(
                project,
                project.revision,
                keep_stl=self.config.config.behavior.keep_stl_history,
            )

        projects.atomic_write_text(project.scad_path, source)

        if meta_name and project.revision == 0 and project.name in ("未命名模型", "", None):
            project.name = meta_name

        project.revision = revision
        project.updated_at = now_iso()
        project.summary = meta_summary or project.summary
        project.params = params
        project.param_meta = param_meta
        project.metrics = self._metrics_payload(metrics, req_suite)
        project.printability = printability
        project.printer = self.printer_context()
        # 规格随每次生成更新：它是用户意图的累积记录，跨会话不丢
        if spec is not None and not spec.is_empty():
            project.spec = spec.to_dict()
        project.last_status = "ok"

        # 缩略图
        try:
            svg = compiler.make_thumbnail_svg(metrics, project.name)
            projects.atomic_write_text(project.thumbnail_path, svg)
        except OSError:
            log.debug("缩略图写入失败", exc_info=True)

        store_projects.save(project)
        store_projects.append_history(
            project,
            HistoryEntry(
                revision=revision,
                created_at=now_iso(),
                prompt=prompt,
                instruction=instruction,
                summary=meta_summary or "",
                name=project.name,
                stl=True,
                metrics=self._metrics_payload(metrics, req_suite),
                printability=printability,
                params=params,
            ),
        )
        # 3MF：切片软件真正想要的格式。STL 不带单位，导进 Bambu Studio /
        # OrcaSlicer / Cura 时常见“尺寸差 10/100 倍”后手动缩放；3MF 明确声明
        # unit="millimeter"，并把机型/材料/设计尺寸写进元数据。
        # 从**已交付的那个 STL** 直接转（不重跑 OpenSCAD），保证两者面数/几何一致。
        try:
            if project.stl_path.is_file():
                from .threemf import write_3mf_from_stl

                bbox = metrics.bbox if metrics is not None else (0.0, 0.0, 0.0)
                write_3mf_from_stl(
                    project.stl_path,
                    project.three_mf_path,
                    title=project.name,
                    metadata={
                        "Title": project.name,
                        "Application": "AI 3D Forge",
                        "Description": project.summary or "",
                        "Printer": f"{self.printer.name}（喷嘴 {self.printer.nozzle_mm}mm）",
                        "Material": self.material or "",
                        "DesignSizeMM": f"{bbox[0]:.1f}x{bbox[1]:.1f}x{bbox[2]:.1f}",
                        "Revision": str(revision),
                    },
                )
        except Exception:  # noqa: BLE001 - 3MF 是附加交付物，写不了不影响 STL 主流程
            log.debug("3MF 导出失败", exc_info=True)

        # 自动导出一份到 export\，方便用户直接拖进切片软件
        try:
            store_projects.export_copy(project)
        except OSError:
            log.debug("导出副本失败", exc_info=True)

    def _log_usage(self, result: Any) -> None:
        log.info(
            "AI 返回完成（模型 %s，提示 %s tokens，生成 %s tokens）",
            getattr(result, "model", "?"),
            getattr(result, "prompt_tokens", 0),
            getattr(result, "completion_tokens", 0),
        )


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def job_work_dir(project: Project) -> Any:
    """编译工作目录：直接使用项目目录，这样 STL 天然生成在用户项目里。"""
    return project.dir


def _prefer(before: str, candidate: str, contract: ChangeContract, spec: DesignSpec | None) -> str:
    """两次都不达标时，挑「损失更小」的那一版。

    不能简单地「不达标就用旧的」——那样用户的要求永远得不到满足；
    也不能「不达标就用新的」——可能顺手改坏了别处，对用户是纯损失。
    因此按 ChangeVerdict.penalty() 比较代价，得分低者胜出；
    「完全没改」也作为一种候选参与比较，这样它不会因为「无害」而白占便宜。
    """
    return _pick_least_bad(before, [candidate, before], contract, spec)


def _pick_least_bad(
    current: str,
    candidates: list[str],
    contract: ChangeContract,
    spec: DesignSpec | None,
) -> str:
    """在多个不达标版本里取舍，**永远给出一个不比原版更差的结果**。

    为什么按「代价」而不是二元判定：如果只判「达标 / 不达标」，
    「改到了 140（目标 150，且没破坏别处）」与「完全没改」代价相同，
    取舍逻辑就会错误地选后者 —— 用户的要求永远得不到推进。

    两条硬保证（都来自实测踩到的缺陷）：
      * **原版本身永远是候选之一**：否则当所有候选都把别处改坏时，
        会挑出一个「比不改还糟」的版本交给用户（实测 picked_penalty=10
        而原版只有 3.3）。显式传入原版也让调用方不必自己记得加。
      * **候选为空也不崩**：以前直接 `min([])` 会抛 ValueError，
        把整次修改变成「生成过程中出现了意外问题」。
    """
    pool = [*candidates, current]

    def score(text: str) -> float:
        return verify_change(current, text, contract, spec).penalty()

    return min(pool, key=score)


def build_scaled_source(
    source: str,
    factor: float,
    printer_name: str,
    note: str | None = None,
) -> str | None:
    """构造一份「等比缩放到刚好装得下」的源码（两条路径，二选一）。

    **路径 A：改写顶层参数值**（`_scale_param_values`）。
    AI 生成的代码绝大多数是「尺寸走顶层参数」的形态（提示词强制要求），
    直接按倍率改写参数即可。附带好处：参数微调里显示的就是最终真实尺寸。

    **路径 B：把几何包进 `scale(){...}`**（`_wrap_geometry_in_scale`）。
    给「尺寸直接写死在几何里、没有可改写参数」的源码用。
    声明（参数赋值、`module`/`function` 定义）必须留在块外 ——
    OpenSCAD 禁止在变换块内定义 module，而参数进了块内会被算两次。

    两条路径**绝不能叠加**：实测 240mm 的试件叠起来会缩成 135mm（期望 180mm）。
    调用方（`GenerationRunner._scale_to_fit`）先试 A，A 的结果仍超界时再单独试 B。

    返回 None 表示这份源码没法自动缩放（例如只有声明没有几何、
    或有多个 module 却一个都没调用 —— 猜哪个是模型本体不安全）。
    """
    if not _has_geometry(source):
        # 源码里没有任何会产出几何的东西（只有声明，或只有 module 定义却从未
        # 实例化）——OpenSCAD 会给出空模型，缩放它没有意义。
        # 唯一例外：只有一个 module 时补上实例化还能救（见 `_wrap_geometry_in_scale`）。
        return _wrap_geometry_in_scale(source, factor, printer_name, note)

    # 尺寸走参数的形态（AI 生成的绝大多数）：改写参数值即可。
    # 注意不能与 scale 块叠加 —— 叠加就缩两次（实测 240 → 135 而非 180）。
    param_scaled = _scale_param_values(source, factor)
    if param_scaled is not None:
        return param_scaled
    return _wrap_geometry_in_scale(source, factor, printer_name, note)


_INSTANCE_CALL_RE = re.compile(r"^[ \t]*[A-Za-z_][A-Za-z0-9_]*\s*\([^;]*\)\s*;\s*$", re.MULTILINE)


def _has_geometry(source: str) -> bool:
    """源码是否真的会产出几何（含「定义了 module 且确实实例化了」）。

    只有声明（参数赋值 + module 定义）而不实例化任何东西的源码，
    在 OpenSCAD 里是空模型 —— 缩放它没有意义。
    """
    for line, kind in zip(source.splitlines(), _classify_lines(source.splitlines())):
        if kind == "geometry" and line.strip() and not line.strip().startswith("//"):
            return True
    return bool(_INSTANCE_CALL_RE.search(scad_src.strip_comments(source)))


def _wrap_geometry_in_scale(
    source: str,
    factor: float,
    printer_name: str,
    note: str | None = None,
) -> str | None:
    """把源码里的**几何语句**包进 `scale(){...}`，声明一律留在顶层。

    为什么必须把 module/function 定义与赋值挑出来放在块外：
    OpenSCAD 禁止在变换块内定义 module/function，而参数赋值一旦进了块内，
    模型尺寸就会被「参数 × 倍率」再算一次，等于缩两次。

    行分类必须按 **OpenSCAD 的顶层作用域** 判断（用大括号深度跟踪），
    而不是「遇到第一个几何行就全算几何」——实测踩到过：源码在几何之前
    先写了一行 `$fn = 67;`，简单实现把它当几何，于是后面的赋值与 `module`
    定义全被包进了 `scale` 块，直接 `Parser error`。

    返回 None 表示源码里根本没有几何语句（只有声明），此时自动缩放没有意义。
    """
    lines = source.splitlines()
    kinds = _classify_lines(lines)

    kept: list[str] = []
    geometry: list[str] = []
    for line, kind in zip(lines, kinds):
        if kind == "keep":
            kept.append(line)
        else:
            geometry.append(f"  {line}")

    if not any(kind == "geometry" for kind in kinds):
        # 「只有 module 定义、一个都没调用」——AI 重做越界模型时吐出过这种形态。
        # 此时源码在 OpenSCAD 眼里是空模型，没法靠包裹缩放；但若只有一个自定义
        # module，实例化它并缩放是安全的救法（多个就放弃，不猜）。
        only = _module_names(source)
        if len(only) != 1:
            return None
        name = next(iter(only))
        head = "\n".join(kept)
        if head:
            head += "\n"
        return (
            f"// {note or f'已按目标打印机（{printer_name}）的成型范围等比缩放 {factor:.3f} 倍'}\n"
            f"__aiforge_fit = {factor:.6f};\n"
            f"{head}// 原源码没有实例化任何 module，这里按成型范围补上\n"
            f"scale([__aiforge_fit, __aiforge_fit, __aiforge_fit]) {name}();\n"
        )

    head = "\n".join(kept)
    if head:
        head += "\n"
    body = "\n".join(geometry)
    return (
        f"// {note or f'已按目标打印机（{printer_name}）的成型范围等比缩放 {factor:.3f} 倍'}\n"
        f"__aiforge_fit = {factor:.6f};\n"
        f"{head}"
        f"scale([__aiforge_fit, __aiforge_fit, __aiforge_fit]) {{\n{body}\n}}\n"
    )


def _scale_param_values(source: str, factor: float) -> str | None:
    """把源码里所有顶层数值参数按倍率改写；没有可改写的参数时返回 None。

    表达式赋值（`seg_len = length / 2`）不需要单独缩放：它引用被缩放的参数，
    OpenSCAD 求值时自然就是缩放后的值。
    """
    values = {
        name: value * factor
        for name, value in scad_src.extract_top_level_numbers(source).items()
        if value
    }
    if not values:
        return None
    scaled_source, applied, _ignored = scad_src.apply_params(source, values)
    if not applied:
        return None
    return (
        "// 已按倍率改写顶层参数（尺寸走参数的模型用这一条）\n" + scaled_source
        + ("" if scaled_source.endswith("\n") else "\n")
    )


def build_user_scaled_source(source: str, factor: float, printer_name: str) -> str | None:
    """按**用户要求**整体缩放（「整体缩小到原来的80%」）。

    与 `build_scaled_source`（服务于"装不下就缩到装得下"）的区别只有一条：
    这里只缩**尺寸类**参数，数量/角度类保持不变 —— 用户说"缩小到 80%"时，
    把 `rib_count` 从 16 缩成 12.8（渲染成 13 条筋）是**改了结构**，
    不是等比缩小。装不下时的兜底则宁可整体缩，所以两条路各自独立。
    """
    note = f"已按你的要求整体缩放 {factor:.3f} 倍（长宽高同比例，其余结构未改）"
    if _has_geometry(source):
        scaled = scale_dimension_params(source, factor)
        if scaled:
            return scaled
    return _wrap_geometry_in_scale(source, factor, printer_name, note=note)


# 数量/角度类参数：整体缩放时**不**跟着变（它们是"有几个""转多少度"，
# 不是长度）。判据来自参数注释标签，例如「竖向加强筋数量」「顶部外扩量」。
_COUNT_ANGLE_WORDS = (
    "数量", "个数", "齿数", "条数", "层数", "圈数", "角度", "倾角", "度数",
    "count", "angle", "number", "segments", "sides",
)


def scale_dimension_params(source: str, factor: float, keep_labels: tuple[str, ...] = _COUNT_ANGLE_WORDS) -> str | None:
    """只按倍率改写**尺寸类**顶层参数；数量/角度类原样保留。

    没有注释标签的参数**照缩**：标签缺失时无法判断语义，而"模型没缩到位"
    比"某个数值跟着缩了"严重得多（前者直接违背用户要求）。
    """
    labels = scad_src.extract_param_labels(source)
    values: dict[str, float] = {}
    for name, value in scad_src.extract_top_level_numbers(source).items():
        if not value:
            continue
        label = labels.get(name, "")
        if label and any(word in label.lower() for word in keep_labels):
            continue
        values[name] = value * factor
    if not values:
        return None
    scaled_source, applied, _ignored = scad_src.apply_params(source, values)
    if not applied:
        return None
    return (
        "// 已按倍率改写顶层尺寸参数（数量/角度类保持不变）\n" + scaled_source
        + ("" if scaled_source.endswith("\n") else "\n")
    )


def _scale_geometry(source: str, factor: float, printer_name: str) -> str | None:
    """把几何语句包进 `scale(){...}`（声明留在块外）；无法构造时返回 None。

    给「尺寸直接写死在几何里、没有可改写参数」的源码用。
    """
    return _wrap_geometry_in_scale(source, factor, printer_name)


def _wrap_geometry_in_scale_axes(
    source: str, fx: float, fy: float, fz: float, note: str
) -> str | None:
    """逐轴缩放：把几何语句包进 `scale([fx,fy,fz]){...}`，声明留在块外。

    与等比缩放的区别：只校正偏差的那个轴（如总高偏了就只动 z），
    不会把没偏的轴（壁厚方向）一起缩掉。
    """
    lines = source.splitlines()
    kinds = _classify_lines(lines)
    kept = [line for line, kind in zip(lines, kinds) if kind == "keep"]
    geometry = [f"  {line}" for line, kind in zip(lines, kinds) if kind == "geometry"]
    if not geometry:
        return None
    head = "\n".join(kept)
    if head:
        head += "\n"
    body = "\n".join(geometry)
    return (
        f"// {note}\n{head}"
        f"scale([{fx:.6f}, {fy:.6f}, {fz:.6f}]) {{\n{body}\n}}\n"
    )


_DECLARATION_RE = re.compile(
    r"^[ \t]*(module|function|include|use)\b"
    r"|^[ \t]*\$?[A-Za-z_][A-Za-z0-9_]*\s*="
)


def _classify_lines(lines: list[str]) -> list[str]:
    """把每一行标成 "keep"（留在 scale 块外）或 "geometry"（包进 scale 块）。

    规则（按 OpenSCAD 的顶层作用域）：
      * `module`/`function` 定义的**整段函数体** → keep
        （OpenSCAD 禁止在变换块内定义 module，所以定义必须整体留外）
      * 顶层的空行 / 注释 / 声明（module、function、include、变量赋值）→ keep
      * 其余（真正产生几何的语句与块）→ geometry

    定义区间必须**先算出来**再逐行分类。只靠「括号深度是否 > 0」是不够的：
    几何块（如 `difference() { ... }`）深度也 > 0，会被误判成定义体。
    """
    protected = _definition_line_range(lines)
    kinds: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if index in protected:
            kinds.append("keep")
        elif not stripped or stripped.startswith("//") or _DECLARATION_RE.match(line):
            kinds.append("keep")
        else:
            kinds.append("geometry")
    return kinds


def _definition_line_range(lines: list[str]) -> set[int]:
    """找出所有 `module`/`function` 定义（含函数体）所覆盖的行号。

    用括号深度判断定义体的结束：定义首行之后的深度回到起点时即结束。
    同时支持 `module m() cube(1);` 这种没有大括号的单行定义。
    """
    protected: set[int] = set()
    depth = 0
    open_at: int | None = None
    body_floor = 0
    for index, line in enumerate(lines):
        is_header = bool(re.match(r"^[ \t]*(module|function)\b", line))
        if open_at is not None:
            protected.add(index)

        if is_header and open_at is None:
            if "{" in line:
                open_at = index
                # 定义体开始的深度 = 本行之前的深度 + 1（本行的 `{`）。
                # 结束条件用 `depth < body_floor`：`<` 而不是 `<=`，
                # 否则函数体内部（depth == body_floor）会被当成已经结束，
                # 函数体后半段与后面的所有语句都会被漏判成几何（实测踩过）。
                body_floor = depth + line.count("{") - line.count("}")
                protected.add(index)
            else:
                # 单行定义（无大括号）：`module m() cube(1);`
                protected.add(index)

        depth += line.count("{") - line.count("}")
        if depth < 0:
            depth = 0
        if open_at is not None and depth < body_floor:
            open_at = None
    return protected


_MODULE_RE = re.compile(r"^\s*module\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.MULTILINE)
_FUNCTION_RE = re.compile(r"^\s*function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.MULTILINE)


def _module_names(source: str) -> set[str]:
    cleaned = scad_src.strip_comments(source)
    return set(_MODULE_RE.findall(cleaned)) | set(_FUNCTION_RE.findall(cleaned))


def _describe_ai_review(review: dict[str, object]) -> str:
    """把 AI 复核结论压成一句话，用于进度消息的 detail。"""
    parts: list[str] = []
    summary = str(review.get("summary") or "").strip()
    if summary:
        parts.append(summary)
    concerns = review.get("concerns")
    if isinstance(concerns, list) and concerns:
        parts.append("疑点：" + "；".join(str(c) for c in concerns[:3]))
    return "\n".join(parts) or "AI 未能说明具体原因。"


def _is_ai_objection(review: dict[str, object]) -> bool:
    """AI 是否明确提出了异议（只认明确的 False，拿不到结论时不拦）。"""
    return review.get("requested_change_applied") is False or review.get("functional_ok") is False


def _merge_ai_review(verdict: ChangeVerdict, review: dict[str, object] | None) -> None:
    """把 AI 复核结论并进程序化判定。

    合并原则（为什么是这个方向）：
      * 两边都通过 → 通过；
      * 程序化通过但 AI 提出明确异议 → **判不通过**，让上层带着差异重做一次。
        这是这套机制的价值所在：参数全对、结构却被改坏的情况只有 AI 看得出来；
      * AI 拿不到结论（网络错误、JSON 解析失败）→ 不影响程序化结论，绝不因为
        「AI 没回话」就把一个干净的改动判成失败。
    """
    if review is None:
        return
    verdict.ai_summary = str(review.get("summary") or "").strip() or None
    concerns = review.get("concerns")
    verdict.ai_concerns = [str(c).strip() for c in concerns if str(c).strip()][:6] if isinstance(concerns, list) else []
    if not _is_ai_objection(review):
        return
    verdict.ok = False
    if review.get("requested_change_applied") is False:
        verdict.performed = False
        verdict.reasons.append("AI 复核认为你要的改动并没有真正生效")
    if review.get("functional_ok") is False:
        verdict.reasons.append("AI 复核认为这次改动把模型结构改坏了")
    if verdict.ai_concerns:
        verdict.reasons.append("AI 指出的疑点：" + "；".join(verdict.ai_concerns[:3]))


def _thread_problems(metrics: MeshMetrics, requirement: str) -> list[str]:
    """需求里要螺纹、但模型上量不到**真螺旋**时，明确报出来。

    为什么单列一条（用户反馈的翻车现场）：用户要"72 转 67 滤镜转接环"，
    模型做出来了、也封闭、尺寸也对，但螺纹是用 `rotate_extrude` 把牙型绕一圈做的 ——
    那是**同心环**：看着有螺纹纹路，实际根本拧不进去。
    这类问题包围盒、体积、封闭性、零件数**全都看不出来**，只能靠专门量。

    判据来自 mesh.detect_helix_thread / measure_thread_depth（都经过实物定标）。
    """
    if not re.search(r"螺纹|螺距|滤镜|转接|拧|丝口|丝锥|thread", requirement, re.IGNORECASE):
        return []
    depth = metrics.thread_depth_mm
    is_helix = metrics.thread_is_helix
    if depth is None:
        return [
            "【问题 7】需求里要的是**能拧上的螺纹**，但程序在模型上量不到螺纹牙型"
            "（沿轴线取一条跨一个螺距的高度带，量外表面的半径起伏，结果≈0）。"
            "推测做成了光滑圆柱或者只有装饰性凹槽 —— 这样是拧不进去的。\n"
            "不要自己手写螺旋几何（实测会不封闭或被 OpenSCAD 丢掉整块几何）。"
            "请改用程序自带的固定零件库：`use <aiforge_parts.scad>` 后调用 "
            "`filter_adapter(...)` / `thread_sleeve(...)` / `threaded_cap(...)`，"
            "把用户给的公称直径与螺距填进参数。"
        ]
    if is_helix is False:
        return [
            f"【问题 7】程序量到了牙型起伏（约 {depth:.2f}mm），但**不是螺旋** ——"
            "同一个高度上四周到处都有牙顶，说明是用 `rotate_extrude` 把牙型绕轴转了一圈，"
            "得到的是同心环：看着像螺纹，实际拧不进去（旋转时不会沿轴向前进）。\n"
            "请改用程序自带的固定零件库（真螺旋、已实测）：`use <aiforge_parts.scad>` + "
            "`filter_adapter(...)` / `thread_sleeve(...)` / `threaded_cap(...)`。"
        ]
    return []


def _sliver_problems(metrics: MeshMetrics) -> list[str]:
    """≈0 厚的薄膜/重合面（实测 p05 < 0.05mm）报为完整性问题。

    为什么单列：这不是「壁偏薄」的设计取舍，而是建模 bug —— 实测 glm 挖空时
    刀具没伸出底面，留下一层 0.01mm 的膜把腔体封住（杯子底变成纸一样薄）。
    这类缺陷水密、单件、尺寸全对，只有射线壁厚能量出来；不报的话用户
    拿到的是一个打印必失败的模型。
    """
    wall = metrics.wall
    if wall is None or wall.p05_mm >= 0.05:
        return []
    loc = wall.thinnest[0][0] if wall.thinnest else None
    where = f"（最薄处约 {[round(v, 1) for v in loc]}）" if loc else ""
    return [
        f"【问题 9】模型里存在≈0 厚的薄膜/重合面：射线法实测壁厚 p05 只有 "
        f"{wall.p05_mm:.2f}mm{where}，而中位壁厚 {wall.median_mm:.2f}mm —— "
        "说明掏空/挖孔的刀具**没有伸出本体表面**，留下一层膜把腔体封住。\n"
        "改法：让 difference() 的刀具沿挖空方向**两端各伸出 ≥0.1mm**"
        "（例如底部挖空时刀具 z 从 -0.1 开始），不要正好与本体表面对齐。"
    ]


def _geometry_integrity_problems(
    metrics: MeshMetrics,
    spec: DesignSpec | None = None,
    visual: str = "",
    dropped: list[str] | None = None,
    requirement: str = "",
    contract: "ChangeContract | None" = None,
    dim_targets: dict[int, tuple[str, float, str]] | None = None,
) -> list[str]:
    """把「几何不成立」翻译成给 AI 的实测证据（而不是泛泛的建议）。

    报十一类，因为它们既严重又**可核对**：
      1. 模型不是一整件（连通分量数 > 1）
      2. 外壁特征穿进了腔体（水平截面上空腔里有材料环）
      3. 用户在规格里点名的整体尺寸没做到（实测 vs 声明）
      4. 多模态模型看渲染图后明确报告的问题（程序测不出来的那一类）
      5. OpenSCAD 明明警告过「这块几何被丢掉了」（编译成功也没用）
      6. 网格有开放边（不是封闭实体 → 切不出来）
      7. 要螺纹却没有真螺旋（同心环 / 光滑圆柱 → 拧不上）
      8. 用户点名的孔径没做到（射线法实测剖面 vs 声明；参数核对看不到表达式写法）
      9. ≈ 0 厚的薄膜/重合面（挖空刀具没伸出表面，把腔体封住）
     10. 源码自己声明的间隙/夹口在网格里量不到（那一刀完全没切到实体）
    其余的几何瑕疵（壁厚、悬垂）交给打印性检查那一条链路，职责不重叠。
    """
    problems: list[str] = []
    problems.extend(_thread_problems(metrics, requirement))
    problems.extend(_sliver_problems(metrics))
    problems.extend(_gap_problems(metrics))
    if dropped:
        # 这一类最阴：编译返回成功、STL 非空，只是**少了一部分几何**。
        # 实测：把 3D 的 rotate() 写在 linear_extrude() 的子节点位置，
        # OpenSCAD 只发一句 warning 就把整个主体丢掉，只剩底座，
        # 于是「100mm 高的手机支架」变成一块 8.7mm 的板，界面还显示成功。
        detail = "；".join(dict.fromkeys(dropped))[:400]
        problems.append(
            "【问题 5】OpenSCAD 警告说有一块几何被丢掉了，模型虽然能编译成功，"
            f"但形状不完整：{detail}\n"
            "最常见的原因是**挤出的写法不对**：`linear_extrude()` 的输入必须是"
            "**平面 2D 图形**，把 3D 的 `rotate([x,y,z])`（x 或 y 不为 0）写在它的"
            "子节点位置会被直接丢掉。正确写法是把旋转放在**外面**：\n"
            "    正确：rotate([90,0,90]) linear_extrude(h) polygon(...);\n"
            "    错误：linear_extrude(h) rotate([90,0,90]) polygon(...);\n"
            "如果只是想在平面内转角度，用 rotate([0,0,角度]) 才是合法的。"
        )
    problems.extend(_spec_dimension_problems(spec, metrics, contract, dim_targets))
    problems.extend(_hole_problems(spec, metrics))
    if metrics.stray_parts > 0:
        lines = [
            f"【问题 1】模型不是一整件：程序对导出的 STL 做连通性分析，"
            f"实测模型由 {metrics.solid_parts} 个互不相连的零件组成"
            f"（1 个主体 + {metrics.stray_parts} 个散件）。"
            "散件与主体只是「摆得近」，没有实体相交 —— 打印出来是几件东西，需要自己拼装。"
        ]
        if metrics.stray_details:
            lines.append(
                "散件明细（包围盒中心与尺寸，单位 mm）："
                + "；".join(
                    f"#{i} 中心({d['center'][0]},{d['center'][1]},{d['center'][2]}) "
                    f"尺寸{d['size'][0]}×{d['size'][1]}×{d['size'][2]} "
                    f"体积{d['volume_mm3']}mm³"
                    for i, d in enumerate(metrics.stray_details[:8], 1)
                )
                + (f"（共 {metrics.stray_parts} 件）" if metrics.stray_parts > 8 else "")
            )
            lines.append(
                "请用这些位置尺寸对回自己写的代码，找出是哪一段（通常是 for 循环阵列"
                "或者单独摆放的模块）没有和主体真正相交。"
            )
        problems.append("\n".join(lines))
    if metrics.cavity_islands:
        detail = "、".join(f"高度约 {z:.0f}mm 处 {n} 处" for z, n in metrics.cavity_islands)
        problems.append(
            "【问题 2】外壁特征穿进了腔体：程序用水平面剖切模型，"
            f"在内腔范围内检测到被空腔完全包围的材料截面（{detail}）。"
            "这说明加强筋一类的**外壁**特征有一部分已经伸到容器内部了 ——"
            "典型原因是先掏空壳体、再 union 外部特征，或者用固定半径绕圈摆放"
            "加强筋而壳体并不是圆的。放东西时会被它挡住。"
        )
    # 6) 网格有开放边 —— 直接不可切片的那一类。
    #
    #    阈值刻意不是 0：几条开放边通常来自 CGAL 在共面/相切处留下的退化面，
    #    主流切片软件会自动补上；成规模（≥10 条）才是真的破了 ——
    #    这种 STL 切出来会缺层、漏面，用户拿到的就是废件。
    #    **非流形边单独出现（开放边为 0）不算**：CGAL 在两个实体恰好相切时
    #    极容易产出这种边，它极常见、多数切片软件也能处理，
    #    拿它去驱动修复只会白花用户一次 AI 调用。
    if metrics.boundary_edges >= 10:
        problems.append(
            f"【问题 6】网格不是封闭实体：程序对导出的 STL 做拓扑检查，"
            f"实测 {metrics.boundary_edges} 条**开放边**、"
            f"封闭率只有 {metrics.watertight_ratio * 100:.1f}%（合格应当接近 100%）。"
            "这种网格切片时会报错或产生空洞，等于打印不出来。\n"
            "最常见的原因是写成「刚好贴合」而不是真正相交：例如柱子的底面尺寸"
            "被写成与底座顶面完全相等，union/difference 之后留下一圈零厚度的面。\n"
            "请让相邻实体**真正重叠** 0.05–0.2mm，而不是刚好贴合；"
            "同时检查有没有把 2D 图形当成 3D 用、或者 difference() 把整面壁切穿。"
        )
    # 视觉复核：程序量不出来的那一类。只有模型**明确说有问题**才并进来，
    # 空回复/说不确定都不算 —— 否则会拿一段含糊的话去驱动修改。
    # 不能直接拿 `parse_snapshot_verdict` 的结果做真值判断：三态返回值都是非空字符串，
    # 都算真 —— 必须显式比 `VERDICT_PROBLEM`。
    if visual and llm_mod.parse_snapshot_verdict(visual) == llm_mod.VERDICT_PROBLEM:
        body = "\n".join(
            line for line in visual.strip().splitlines()[1:] if line.strip()
        ) or visual.strip()
        problems.append(
            "【问题 4】多模态模型看过渲染图后报告的问题（程序指标看不出来，"
            "但图上确实可见）：\n" + body[:800]
        )
    return problems


# 流形边占比允许的抖动：同一段几何重算一次的量级差异远小于此
_MESH_RATIO_EPS = 0.005


def _mesh_quality_not_worse(before: MeshMetrics, after: MeshMetrics) -> bool:
    """修复后的网格拓扑质量是否「不比原版差」（True 才允许采纳）。

    为什么不能写成 `after.watertight or not before.watertight`：
    这个判据在原版已经不水密时恒为真，等于「一旦原版破了，修成多破都行」。
    第三轮基准实测到逐轴校正把 28 条非流形边（流形边占比 0.9948）
    换成 287 条（占比 0.3376）并作为成品交付 —— 三分之二的面互相穿插，
    切片软件会当成废件，而这正是「修复」环节自己造成的。
    逐轴缩放是纯仿射变换，本不该改变拓扑；一旦计数变了，
    说明缩放把原本重合的面拉开了，属于必须拒绝的候选。

    判据（全部满足才算不差）：
      * 原版水密 → 候选也必须水密（不能用封闭性去换别的指标）
      * 候选的流形边占比不得低于原版（允许 _MESH_RATIO_EPS 的抖动）
      * 候选的非流形边、开放边、退化三角形都不得比原版多
    """
    if before.watertight and not after.watertight:
        return False
    if after.watertight_ratio < before.watertight_ratio - _MESH_RATIO_EPS:
        return False
    return (
        after.non_manifold_edges <= before.non_manifold_edges
        and after.boundary_edges <= before.boundary_edges
        and after.degenerate_triangles <= before.degenerate_triangles
    )


def _integrity_rank(
    metrics: MeshMetrics,
    spec: DesignSpec | None = None,
    contract: "ChangeContract | None" = None,
) -> tuple[int, int, int, int, int, int, int]:
    """尺寸/结构完整性的排序键（越小越好）。

    顺序体现优先级，不能随便调：
      0. **装不进这台打印机** —— 放不下就是废件，比其它任何缺陷都严重
      1. 散件数 —— 「不是一件东西」是最根本的错误
      2. 点名尺寸没做到 —— 用户明确说过的数字，做不到就是没听懂需求
      3. 穿腔处数 —— 内部结构缺陷，用户不一定第一眼看得出来
      4. **网格不封闭**（有开放边/非流形边）
      5. 碎三角
      6. **网格干净度**（连续的流形边占比）—— 排在最后：
         前面全部持平时，非流形边更少的那一版胜出

    第 0 项是实测踩出来补上的：修复环节排在「成型范围重做」之后，
    而它原本只看散件/尺寸/穿腔，**不看模型还放不放得下** ——
    于是它把一个已经缩到 180mm、刚好能打印的模型，
    换成了一个「更连通但 234mm」的版本，扫描范围校验又被打破。
    装不下必须排在第一位，这样任何修复都不可能把「能打印」改成「不能打印」。

    第 4 项（封闭性）为什么放在最后、而不是放前面：
    往前放就意味着修复可以拿它去交换别的指标 ——
    比如「多了一个散件、但网格封闭了」会被判为更优，
    那是拿「是不是一整件」去赌网格封闭性，两个都是用户不想要的。
    放在最后的效果正好是我们要的：**其它指标持平时，封闭的那一版胜出**。
    补这一项是因为实测发现修复循环**从来不会去修一个破网格**：
    它以「问题类别」为触发条件，而破网格当时不在任何一类里；
    就算被触发，平手的候选也会被「严格更好」的门槛挡掉。
    端到端实测就送出过一个 736 条开放边的 STL（切出来是废件），
    程序只在打印性报告里如实写了「建议重新生成」。
    """
    return (
        1 if metrics.exceeds_build_volume else 0,
        metrics.stray_parts,
        len(_spec_dimension_problems(spec, metrics, contract)) + len(_hole_problems(spec, metrics))
        # 声明的间隙量不到与「点名尺寸没做到」同级：都是 AI 自己写下的数字没兑现
        + len(_gap_problems(metrics)),
        sum(n for _, n in metrics.cavity_islands),
        # 0 = 封闭，1 = 不封闭。用实测字段而不是只看 watertight 标志：
        # 只有「开放边和非流形边都为 0」才算真的能切片。
        0 if (metrics.watertight or (metrics.boundary_edges == 0 and metrics.non_manifold_edges == 0)) else 1,
        1 if _sliver_problems(metrics) else 0,
        # 最后一项：网格干净度（连续的流形边占比）。
        # 为什么要有：上一项是 0/1 的粗粒度，28 条和 287 条非流形边在它眼里等价，
        # 于是修复循环在「都不封闭」的两版之间随便挑，实测就挑中了更破的那一版。
        # 放最后，所以它永远不会拿散件/尺寸/穿腔去换网格质量。
        int(round((1.0 - metrics.watertight_ratio) * 1000)),
    )


# 规格里「整体尺寸」的字段名 → 实测包围盒的轴（0=X, 1=Y, 2=Z）。
# 只有这几个词才算整体尺寸：`wall_thickness 2.4` 不该拿去和包围盒比。
_OVERALL_AXIS_TOKENS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (2, ("height", "tall", "高度", "总高", "立高", "身高")),
    (0, ("width", "wide", "宽度", "总宽", "正面宽")),
    (1, ("depth", "deep", "深度", "总深", "进深", "前后")),
)
# 命中这些词的字段是「零件内部尺寸」，不是整体尺寸，一律跳过。
# 注意「直径」不在里面：对圆件（笔筒/杯子/盖子/转接环）**外径就是最主要的整体尺寸**，
# 一律跳过等于对圆件完全不校验尺寸（实测：用户要 Ø80 的笔筒，出来 92×146，
# 程序一声不吭）。真正的内部直径（孔径/内径/螺孔）由 _INNER_DIA_MARKERS 排除。
_LOCAL_DIM_TOKENS = (
    "wall", "thickness", "rib", "gap", "fillet", "chamfer", "radius", "clearance",
    "angle", "count", "hole", "slot", "lip", "clamp", "gusset",
    "壁厚", "加强筋", "间隙", "圆角", "倒角", "半径", "孔径", "角度", "数量", "个数",
)
# 「内孔」语义：这些词说明说的是内部直径，不能拿去跟外形包围盒比
_INNER_DIA_MARKERS = (
    "孔", "内径", "内孔", "内直", "内圆", "轴孔", "螺孔",
    "hole", "inner", "bore", "shaft", "screw", "bolt", "pin", "thread", "socket",
)
# 「外形/整体」语义
_OUTER_MARKERS = ("外径", "外圆", "最大外", "整体直", "outer", "overall", "max")
# 水平包络：外径对应 max(bbox.x, bbox.y)
HORIZONTAL_ENVELOPE_AXIS = 3

# 逐轴校正允许的最小倍率。取 1/1.5 是为了与【问题 8】用**同一把尺子**：
# 「实测超出要求 50%」本来就被判为「不可能是多长了底座」，那反过来看，
# 需要缩到 2/3 以下才能凑上的目标，就是**尺寸与包围盒对不上**，
# 该交给 AI 重做（或直接报问题），而不是把整件缩成一团。
_MIN_SHRINK_FACTOR = 1.0 / 1.5



# 水平面内的整体尺寸词：宽 / 深 / 长 都算（都是"躺在台面上的两个方向"）。
# 与 _OVERALL_AXIS_TOKENS 分开维护：那份是**按轴记账**、给逐轴缩放用的，
# 这里只回答"这条尺寸是不是整体水平尺寸"，不参与缩放。
_PLANAR_DIM_TOKENS = (
    "width", "wide", "宽度", "总宽", "正面宽",
    "depth", "deep", "深度", "总深", "进深", "前后",
    "length", "long", "长度", "总长", "全长", "纵向",
)


def spec_planar_dimensions(spec: DesignSpec | None) -> list[tuple[str, float, str]]:
    """用户点名的**水平方向**整体尺寸：(标签, 目标值mm, 规格名)。

    为什么需要它（第一版质量基准抓到的覆盖洞）：按轴记账的
    `_spec_declared_dimensions` 一个轴只留一条，「长 60、宽 40、高 20」
    里的 长度 从来没被核对过 —— 只做了宽和高的模型也能全绿。
    需求核对表面向用户，宁可多核对一条也不能假绿。
    """
    found: list[tuple[str, float, str]] = []
    if spec is None:
        return found
    for item in getattr(spec, "specs", []) or []:
        if not getattr(item, "user_specified", False):
            continue
        if item.value is None or (item.unit or "mm") not in ("mm", "毫米"):
            continue
        try:
            value = float(item.value)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        haystack = f"{item.name} {item.label}".lower()
        if any(token in haystack for token in _INNER_DIA_MARKERS):
            continue
        if any(token in haystack for token in _LOCAL_DIM_TOKENS):
            continue
        if any(token in haystack for token in _OUTER_MARKERS):
            continue  # 外径走水平包络那条，不重复核对
        if not any(token in haystack for token in _PLANAR_DIM_TOKENS):
            continue
        found.append((item.label or item.name, value, item.name))
    return found


def _spec_declared_dimensions(spec: DesignSpec | None) -> dict[int, tuple[str, float, str]]:
    """从规格里取出「用户明确指定的整体尺寸」，映射到包围盒的轴。

    为什么要做：`acceptance` 里的「支架总高等于 100mm」以前只是写给 AI 看的，
    **从来没有任何程序去核对过**。用户说 100mm、模型出来 62mm 这种情况，
    在第一次生成时是没有任何东西拦得住的（改动流程有 `_implausible_targets`，
    首次生成没有）。实测端到端日志里已经有「参数 240、实测含底座故不等高」
    这类现象，说明光看参数不行，必须拿实测尺寸比。
    """
    found: dict[int, tuple[str, float, str]] = {}
    if spec is None:
        return found
    for item in getattr(spec, "specs", []):
        if not getattr(item, "user_specified", False):
            continue
        if item.value is None or (item.unit or "mm") not in ("mm", "毫米"):
            continue
        try:
            value = float(item.value)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        haystack = f"{item.name} {item.label}".lower()
        if any(token in haystack for token in _INNER_DIA_MARKERS):
            continue
        if any(token in haystack for token in _LOCAL_DIM_TOKENS):
            continue
        # 外径（含只写「直径 / diameter」的）：圆件的主要整体尺寸，
        # 对应水平方向的最大外形；单独一个轴表达不了，用 HORIZONTAL_ENVELOPE_AXIS。
        if any(token in haystack for token in _OUTER_MARKERS) or any(
            token in haystack for token in ("直径", "diameter", "dia")
        ):
            found.setdefault(
                HORIZONTAL_ENVELOPE_AXIS, (item.label or item.name, value, item.name)
            )
            continue
        for axis, tokens in _OVERALL_AXIS_TOKENS:
            if any(token in haystack for token in tokens):
                # 同一个轴出现多次时以第一个为准，避免互相覆盖。
                # 同时带上**规格名**（label 是给用户看的中文，契约里用的是 name）——
                # 少了它就没法判断"这一项本次是否被授权修改"（实测踩过）。
                found.setdefault(axis, (item.label or item.name, value, item.name))
                break
    return found


def _spec_declared_holes(spec: DesignSpec | None) -> list[tuple[str, float, str]]:
    """从规格里取用户明确指定的「孔径/内径」类尺寸（交给射线剖面去核对）。"""
    found: list[tuple[str, float, str]] = []
    if spec is None:
        return found
    for item in getattr(spec, "specs", []):
        if not getattr(item, "user_specified", False):
            continue
        if item.value is None or (item.unit or "mm") not in ("mm", "毫米"):
            continue
        try:
            value = float(item.value)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        haystack = f"{item.name} {item.label}".lower()
        if not any(token in haystack for token in _INNER_DIA_MARKERS):
            continue
        found.append((item.label or item.name, value, item.name))
    return found


# 声明的间隙/夹口/插槽：AI 自己在源码顶层参数里写了这些尺寸，就必须能在网格里量到。
# 词表必须**不含 clamp**：`clamp_w` 是夹爪宽度（实体尺寸）而不是空隙，
# 拿它去要求「量得到 14mm 空档」会把完全正确的模型判成缺陷 ——
# 假失败比漏报更糟：用户一旦不信这张表，真失败那条也没人看了。
_GAP_PARAM_TOKENS = (
    "gap", "clearance", "slot", "mouth", "cradle",
    "间隙", "缝隙", "空档", "夹口", "卡口", "卡槽", "插槽", "张口", "夹距",
)
# 同时命中这些词的参数是在说「实体/数量」而不是空隙，不拿来打分。
_GAP_PARAM_EXCLUDE = (
    "wall", "thickness", "count", "radius", "angle", "pitch",
    "壁厚", "厚度", "数量", "半径", "角度", "螺距", "间距",
)


def _axis_name(axis: int) -> str:
    return "XYZ"[axis] if 0 <= axis <= 2 else str(axis)


def _declared_gaps_from_source(source: str | None) -> list[tuple[str, float, str]]:
    """从源码顶层参数里提出「声明了的间隙/夹口/插槽」：(标签, mm, 参数名)。

    为什么从**源码**取而不从规格取：实测那版手机夹，规格里几乎全是 null（AI 自己
    取的值只记在源码里），只有 `clamp_gap = 14;` 这一行是硬事实。判据是**自洽**：
    不评价设计好坏、不要求用户先说清数字，只问「你自己写的这个数在网格里有吗」。

    只看顶层纯数值赋值（与参数微调同一套解析），表达式赋值不猜。
    """
    if not source:
        return []
    values = scad_src.extract_top_level_numbers(source)
    if not values:
        _model_name, _summary, values = scad_src.extract_metadata(source)
    if not values:
        return []
    labels = scad_src.extract_param_labels(source)
    found: list[tuple[str, float, str]] = []
    for name, value in values.items():
        try:
            mm = float(value)
        except (TypeError, ValueError):
            continue
        if mm < 3.0 or mm > 500.0:
            continue
        label = str(labels.get(name) or "")
        haystack = f"{name} {label}".lower()
        if not any(token in haystack for token in _GAP_PARAM_TOKENS):
            continue
        if any(token in haystack for token in _GAP_PARAM_EXCLUDE):
            continue
        found.append((label or name, mm, name))
        if len(found) >= 4:
            break
    return found


def _gap_problems(metrics: MeshMetrics) -> list[str]:
    """把「声明的间隙在网格里量不到」翻成回喂给 AI 的具体改法。

    与 `_hole_problems` 同样的保守原则：只拿实测事实说话；根本没测（模型过大）
    时一句都不说，只留需求核对表里的 invalid 项 —— 给 AI 一句「量不到」只会让它去改本来没坏的东西。
    """
    declared = list(getattr(metrics, "declared_gaps", []) or [])
    gaps = getattr(metrics, "interior_gaps", None)
    if not declared or gaps is None:
        return []
    smallest = min(float(mm) for _label, mm, _name in declared)
    if gaps.max_mm >= smallest * 0.7:
        return []
    per_axis = "、".join(f"{_axis_name(i)} {v:.2f}mm" for i, v in enumerate(gaps.per_axis))
    solid_axis = max(range(len(gaps.longest_material)), key=lambda i: gaps.longest_material[i])
    names = "、".join(f"{label}({name})={mm:g}mm" for label, mm, name in declared[:4])
    return [
        (
            "【问题 10】你声明了 "
            + names
            + f"，但网格实测被材料夹住的空档只有：{per_axis}（三轴最大值 "
            f"{gaps.max_mm:.2f}mm）—— 这一刀完全没切到实体：编译成功、STL 非空、没有 warning，"
            f"而沿{_axis_name(solid_axis)}最长有 {gaps.longest_material[solid_axis]:.1f}mm 是连续材料。"
            "只要求两件事：① 空隙两侧的表面坐标必须直接由该参数决定"
            "（下侧顶面 = 下侧底 + jaw_h，上侧底面 = 下侧顶面 + clamp_gap），"
            "并逐轴核对**有没有别的材料横跨这个区域** —— 真实项目实测到的首要成因"
            "是整片底板/背板把夹口填平（拿它那个源码去化简 `+ jaw_h - jaw_h` 这类"
            "表达式，编译出来 1116 个面一模一样）：恒等式简对几何毫无影响，"
            "不要在那里花功夫；"
            "② 刀具与主体必须在同一坐标系 —— 主体是 linear_extrude 沿 +Z 挤出的，"
            "刀具若写成 `rotate([-90,0,0]) linear_extrude(2D轮廓)` 会被搬到 z<0 的模型外，"
            "什么都切不到。这两条都是实测踩过的坑，不是泛泛的建议。"
        )
    ]


# 哪些实测失败算「硬伤」（必须拖低最终判定）。
# 故意不包含网格不封闭/穿腔/薄膜这几类：它们驱动修复循环是对的，但拿来把
# 一个功能完整的模型判成「不能打印」过于锐利 —— CGAL 在相切处留几条非流形边极常见。
_HARDCHECK_KINDS = ("dimension", "hole", "thread", "feature-gap")
_HARDCHECK_GROUPS = ("integrity:one-body",)
_VERDICT_ORDER = {"pass": 0, "warn": 1, "fail": 2}


def _hard_failures(suite: "reqtests.ReqSuite | None") -> list[Any]:
    """从需求核对表里挑出「硬伤」级别的实测失败（只降不升的判据共用一份）。"""
    if suite is None:
        return []
    return [
        t
        for t in suite.blocking_failures
        if t.kind in _HARDCHECK_KINDS or t.group in _HARDCHECK_GROUPS
    ]


def _downgrade_printability(
    printability: dict[str, Any] | None,
    suite: "reqtests.ReqSuite | None",
    visual_review: str = "",
    remaining: list[str] | None = None,
) -> str | None:
    """让最终判定**听实测的**：程序量到的硬失败必须反映到 verdict / score / issues。

    为什么要单独做这一步（实测的假绿）：交付给用户的那版手机夹，夹口这一刀完全没切到
    实体，而打印性判定仍是 `pass` / 85 分；唯一看出问题的多模态外观复核只被存进
    `visual_review`，既不参与 verdict，也不上结果卡。判定链自己不给自己的证据付账，
    用户就只能靠肉眼发现（而肉眼本来就是我们要替代的东西）。

    降级分两档，证据强度不同、结果也不同：
      * 实测硬失败（尺寸/孔径/螺纹/间隙/散件）→ fail，分数封顶 49，进一条 error
      * 只有外观复核报问题、或修复后仍有非硬伤残留 → 至少 warn，分数封顶 79
    只降不升：AI 自己给的分数本来就偏乐观，不能拿这个函数去提高它。
    返回一句人话说明（供日志），没改动时返回 None。
    """
    if not isinstance(printability, dict):
        return None
    hard = _hard_failures(suite)
    visual_bad = bool(
        visual_review and llm_mod.parse_snapshot_verdict(visual_review) == llm_mod.VERDICT_PROBLEM
    )
    leftovers = [str(p) for p in (remaining or []) if str(p).strip()]
    if not hard and not visual_bad and not leftovers:
        return None

    issues = printability.get("issues")
    if not isinstance(issues, list):
        issues = []
    try:
        score = int(printability.get("score"))
    except (TypeError, ValueError):
        # 以前拿 70 当默认分数——`parse_review_json` 已经改过一轮：拿不到真实分数就不该拿
        # 一个中性值充数（同一个道理用在这）。这里变成 None：下面每个降级分支遇到 None
        # 直接把分数钉在该档的天花板上，而不是假装 AI 给了个 70 再去 min。
        score = None
    verdict = str(printability.get("verdict") or "pass")
    notes: list[str] = []

    if hard:
        issues.insert(
            0,
            {
                "severity": "error",
                "title": "实测没做到",
                "detail": "；".join(
                    f"{t.requirement}：要求 {t.expected or '—'}，实测 {t.actual or '量不到'}"
                    for t in hard[:4]
                )[:600],
                "suggestion": (
                    hard[0].fix_hint
                    or "这几条是程序在导出的 STL 上直接量出来的，不是推测。"
                )[:600],
            },
        )
        verdict = "fail"
        score = 49 if score is None else min(score, 49)
        notes.append(f"{len(hard)} 项实测硬失败已计入判定")
    elif leftovers:
        issues.insert(
            0,
            {
                "severity": "warn",
                "title": "修复后仍有残留",
                "detail": "；".join(leftovers)[:600],
                "suggestion": "这几条程序已经定位到具体尺寸/位置，可以再说一句让 AI 只改这一处。",
            },
        )
        if _VERDICT_ORDER.get(verdict, 0) < 1:
            verdict = "warn"
        score = 79 if score is None else min(score, 79)
        notes.append("残留问题已计入判定")

    if visual_bad:
        body = "\n".join(
            line for line in str(visual_review).strip().splitlines()[1:] if line.strip()
        ) or str(visual_review).strip()
        issues.insert(
            0 if not hard else 1,
            {
                "severity": "warn",
                "title": "外观复核发现问题",
                "detail": body[:600],
                "suggestion": "渲染图上能看到的结构缺失，程序指标不一定量得出来；可以直接说哪里不对让 AI 改。",
            },
        )
        if _VERDICT_ORDER.get(verdict, 0) < 1:
            verdict = "warn"
        score = 79 if score is None else min(score, 79)
        notes.append("外观复核报告的问题已计入判定")

    printability["issues"] = issues[:12]
    printability["verdict"] = verdict
    printability["score"] = score
    printability["evidence_note"] = "、".join(notes)
    return "、".join(notes)


def _hole_problems(spec: DesignSpec | None, metrics: MeshMetrics) -> list[str]:
    """用户点名的孔径 vs 实测孔径剖面（HANDOVER P0）。

    为什么必须实测：参数核对看不到「把孔写成表达式」的改法（如 d = outer - 2*wall），
    改参数名/参数值都不会改变实际孔径；而 mesh 的射线剖面能量出来。

    判据保守：剖面里**任一高度**匹配声明值即视为做到；剖面全 None（没有居中孔）
    时不下结论（量不到就不报），避免误报驱动无谓的修复。
    """
    declared = _spec_declared_holes(spec)
    if not declared or not metrics.hole_profile:
        return []
    problems: list[str] = []
    # 剖面量到了空档，但贴底/贴顶两端都没开口 —— 完全封闭的内部腔体，不是孔。
    # 以前「任一高度命中即视为做到」会把它当成孔（拿一个根本没开口的东西去匹配
    # 用户点名的孔径需求）；`hole_through` 把这种情况与「真通孔」「只开一面的盲孔」分开。
    # None（盲孔/没测完）不强行下结论，只有 False（确认两端都没开口）才报。
    if getattr(metrics, "hole_through", None) is False:
        problems.append(
            "剖面在部分高度量到了空档，但两端都没开口 —— 这是完全封闭的内部腔体，不是能用的孔。"
            "检查 difference() 里挖孔的圆柱是否真的沿该方向比模型更长（从头穿到尾）。"
        )
        return problems
    measured = [d for d in metrics.hole_profile if d is not None]
    if not measured:
        return []
    for label, value, _name in declared:
        tol = max(0.6, value * 0.06)
        if any(abs(d - value) <= tol for d in measured):
            continue
        problems.append(
            f"【问题 8】用户点名的孔径没做到：规格里「{label}」要求 {value:g}mm，"
            f"程序用射线法在 6 个高度实测居中孔径为 "
            f"{'、'.join(f'{d:g}' for d in measured)} mm，没有一个落在 ±{tol:g}mm 容差内。\n"
            "注意：参数名改对了不等于孔真的变了 —— 若源码把孔写成表达式"
            "（如 d = outer - 2*wall），改参数不会改变实际孔径。"
            "请直接核对 difference() 里挖孔那个圆柱的直径是否等于要求值。"
        )
    return problems


def _scale_mismatch(
    contract: "ChangeContract",
    metrics: MeshMetrics,
    previous_largest: float | None,
) -> str | None:
    """「整体缩小到 80%」这类请求，实测尺寸是不是真的按比例变了。

    为什么必须有这条（实测踩到）：缩放是唯一的"所有尺寸一起变"的改动，
    如果只比参数值，「AI 把 scale(0.8) 包在外面、参数一个没动」和
    「AI 什么都没做」在参数层面长得一模一样 —— 用户要缩小，模型纹丝不动，
    作业却报完成。这里用**改动前后的实测最大尺寸之比**兜住它。
    """
    factor = getattr(contract, "scale_factor", None)
    if not factor or not metrics.bbox or not previous_largest:
        return None
    largest = max(metrics.bbox)
    ratio = largest / float(previous_largest)
    # 容差 ±8%：允许"缩放后加了倒角/圆角导致的微小差异"，但挡住"根本没缩"
    if abs(ratio - float(factor)) <= max(0.08, float(factor) * 0.1):
        return None
    return (
        f"要求整体缩放到 {float(factor) * 100:.0f}%，实测最大尺寸从 {previous_largest:.1f}mm "
        f"变成 {largest:.1f}mm（{ratio * 100:.0f}%）—— 缩放没有真正生效"
    )


def _spec_dimension_problems(
    spec: DesignSpec | None,
    metrics: MeshMetrics,
    contract: "ChangeContract | None" = None,
    declared: dict[int, tuple[str, float, str]] | None = None,
) -> list[str]:
    """规格里用户点名的整体尺寸，实测有没有做到。

    只报「明显做不到」这一个方向（实测 < 声明值的 90%）：
    模型常有多出来的结构（底座、托边、翻边），实测**偏大**是正常的 ——
    实测日志里就有「参数值 240.0，含底座等结构故不等高」。
    反过来实测偏小则一定是没做到，没有合理解释。

    **本次改动授权的项必须按新目标值判，不能拿旧规格否决**（实测踩到的严重 Bug）：
    规格是项目最初那版（"总高 100mm"）。用户后来说"把高度改成 50mm"，
    模型真的改成 50mm 之后，这条检查却拿旧规格去判 —— 50 < 100×0.9，
    于是报「用户点名的尺寸没做到」，接着 AI 修复**把模型改回 100mm**，
    而作业仍然报"完成"。用户看到的是：说了改高度，模型纹丝不动。
    隐蔽之处：这条只查"偏小"，所以只有把尺寸**改小**时才暴露（改大不会）。

    证据里必须带上**差多少**和**最可能的成因**：只写「要 150mm、实测 123mm」
    时，实测发现 AI 会反复重写同一段公式（它以为自己算对了），
    两轮修复都修不动。所以这里把「参数没真正决定竖直尺寸」这件事点破 ——
    倾斜件的斜长与竖直升高之间隔着一个三角函数，而两次 `rotate` 之后
    挤出方向通常已经不是竖直方向。
    """
    if declared is None:
        declared = _spec_declared_dimensions(spec)
    if not declared or not metrics.bbox:
        return []
    authorized: set[str] = set()
    targets: dict[str, float] = {}
    if contract is not None:
        # 契约里的项名是**规格名**（例如 height），而这里拿到的 label 是给人看的
        # 中文（「总高」）—— 两边都要归一化后再比，否则豁免永远匹配不上（实测踩过）。
        for key in (*contract.changed, *contract.unchanged, *contract.frozen):
            authorized.add(normalize_name(key))
        targets = {
            normalize_name(key): float(value)
            for key, value in contract.target_values.items()
            if isinstance(value, (int, float))
        }
    shorts: list[str] = []
    overshoots: list[str] = []
    vertical_short = False
    for axis, (label, want, spec_name) in sorted(declared.items()):
        keys = {normalize_name(spec_name), normalize_name(label)}
        if keys & authorized:
            # 本次授权的项：以用户新给的目标值为准；没解析出目标值就不判它。
            target = next((targets[key] for key in keys if key in targets), None)
            if target is None or target <= 0:
                continue
            want = target
        if axis == HORIZONTAL_ENVELOPE_AXIS:
            got = max(metrics.bbox[0], metrics.bbox[1])
            axis_name = "水平最大（XY）"
        else:
            got = metrics.bbox[axis]
            axis_name = "XYZ"[axis]
        if got < want * 0.9:
            gap = want - got
            shorts.append(
                f"{label} 要求 {want:g}mm，实测{axis_name}方向只有 {got:.1f}mm"
                f"（差 {gap:.1f}mm，只有要求的 {got / want * 100:.0f}%）"
            )
            if axis == 2:
                vertical_short = True
        elif got > want * 1.5 and not diameter_is_fit_scope(label, spec_name):
            # 明显偏大也要报：模型多出底座/托边是正常的（偏大 10~40% 都算正常，
            # 所以阈值放到 1.5 倍），但**超出 50% 就不可能是"多长了一点"** ——
            # 实测踩到：要 Ø80 的笔筒，AI 把外壁的筋旋转到了错误的平面，
            # 出来一个 92×145.8×100 的块料，程序全程沉默、只报"warn"。
            # 例外：杯径/口径这类**配合尺寸**本来就该比整件小，包围盒只能给它兑下界。
            overshoots.append(
                f"{label} 要求 {want:g}mm，实测{axis_name} {got:.1f}mm"
                f"（超出 {(got / want - 1) * 100:.0f}%，约多 {got - want:.1f}mm）"
            )
    if not shorts and not overshoots:
        return []
    advice = (
        "必须由**几何本身**达到，把参数值改大不算 —— 那会让参数表跟你的要求对不上。"
        "请检查是不是把它写成了别的参数的从属值，或者被某个结构尺寸限制了。"
    )
    problems: list[str] = []
    if shorts:
        vertical_advice = ""
        if vertical_short:
            vertical_advice = (
                "\n这一类最常见的成因是**倾斜件**：长度用斜长算出来、"
                "却按「斜长 × cos(倾角) = 竖高」反推总高，"
                "而经过两次 `rotate` 之后挤出方向通常**已经不是竖直方向**，"
                "于是竖直升高与斜长之间差了三角函数关系，总高自然达不到。"
                "两种稳妥写法（二选一）：\n"
                "  * 不做倾斜：竖直件直接 `linear_extrude(height = 总高 - 底座高)`；\n"
                "  * 要倾斜：先把**竖直高度**当已知量建模，再只用一个 `rotate` 倾斜，"
                "并在注释里写出顶端 Z 坐标的计算式，自己核对一遍。"
            )
        problems.append(
            "【问题 3】用户点名的尺寸没有做到："
            + "；".join(shorts)
            + "。这是用户在需求里明确给出的数字，" + advice + vertical_advice
        )
    if overshoots:
        problems.append(
            "【问题 8】用户点名的尺寸**远远超出**要求："
            + "；".join(overshoots)
            + "。这不是「多长了一点底座/托边」能解释的，通常是把某个结构画到了"
            "错误的平面/方向（例如把周向的筋按轴向旋转，于是筋从侧面伸出去）。"
            "请让几何真的落在要求的尺寸上：外形按该尺寸建模，"
            "其余结构都必须收在它之内（或明确说明为什么必须超出去）。"
        )
    return problems


def _cached_largest_dimension(project: Project) -> float | None:
    """取项目里缓存的「上一次实测最大尺寸」，用于改动前后对比。

    为什么用缓存而不是重新编译：上次生成时已经测过并写进 project.metrics，
    再编译一次只为拿一个数字是几秒的纯浪费，而且还要处理编译失败。
    拿不到就返回 None（自洽性检查会自动降级到只做硬性判断）。
    """
    metrics = project.metrics or {}
    bbox = metrics.get("bbox")
    if isinstance(bbox, dict):
        values = [float(v) for v in bbox.values() if isinstance(v, (int, float))]
        if values:
            return max(values)
    return None


def _implausible_targets(
    source: str,
    contract: ChangeContract,
    metrics: MeshMetrics,
    previous_values: dict[str, float] | None = None,
    previous_largest: float | None = None,
) -> list[str]:
    """找出「参数改了、几何根本没跟着动」的授权改动。

    实测踩到过：用户要求「高度改成 150mm」，AI 把 `height` 参数写成 150，
    模型总高却从 101mm 掉到 38mm（几何完全没跟着参数走）。
    旧契约核对只看参数，于是判「通过」——用户拿到的却是一个不对的模型。

    判据必须**相对**，不能是「参数值 > 模型最大尺寸」这种绝对关系：
    实测反例——`height = 150` 的支架实测最大尺寸 142.8mm（倾斜靠背让总高略小于
    参数值）是完全正常的。因此分两档：
      * 硬性：参数值比模型最大尺寸还大 25% 以上 → 这个参数根本不在决定尺寸；
      * 相对：改动前该参数与模型主尺寸几乎相等（说明它确实在驱动尺寸，
        例如 100mm 高 → 实测 101mm），改动后却仍停留在原尺寸附近 → 几何没跟上。
    """
    values = scad_src.extract_top_level_numbers(source)
    labels = scad_src.extract_param_labels(source)
    largest = max(metrics.bbox) if metrics.bbox else 0.0
    problems: list[str] = []
    for name, target in contract.target_values.items():
        resolved = name if name in values else resolve_param_name(name, values, labels)
        if resolved is None or resolved not in values:
            continue
        actual = abs(values[resolved])
        if actual > largest * 1.25:
            problems.append(f"{resolved}={actual:g}mm，但模型实测最大尺寸只有 {largest:.1f}mm")
            continue
        if previous_values is None or previous_largest is None:
            continue
        before_value = previous_values.get(resolved)
        if before_value is None:
            continue
        # 改动前这个参数几乎等于模型主尺寸 → 它确实在驱动尺寸
        if abs(before_value - previous_largest) > max(3.0, previous_largest * 0.1):
            continue
        if abs(actual - before_value) <= max(0.5, abs(before_value) * 0.01) and abs(
            previous_largest - largest
        ) <= 3.0:
            problems.append(
                f"{resolved} 已改成 {actual:g}mm，但模型尺寸仍是 {largest:.1f}mm（没有跟着变）"
            )
    return problems


def _apply_targets(source: str, contract: ChangeContract) -> str | None:
    """把改动契约里的目标值直接写回源码，别处一个字都不动。

    为什么需要这条确定性兜底：AI 两次都没做到「只改该改的」时，
    旧逻辑只能在两个都不达标的版本里挑一个 —— 用户看到的还是「越改越歪」。
    但「只改 height 到 150」这件事对文本来说就是一次赋值替换，
    用 `scad.apply_params()` 精确改写顶层数值赋值即可，风险远低于再让 AI 猜一次。

    只有当目标参数确实存在于原源码的顶层数值赋值里时才采用
    （`apply_params` 会告诉我们哪些没应用上）；否则返回 None，
    交回给「两害相权取其轻」的取舍逻辑。
    """
    if not contract.target_values:
        return None

    # 底线：授权改动的名字在源码里是**表达式赋值**时，不许改写别的参数充数。
    # 例：`height = design_h * 1.5;` —— 把 design_h 写成 150 得到的是 225mm 高，
    # 而界面会显示「已按你的目标值改动」。宁可放弃兜底，也不交半成品。
    for raw in contract.changed:
        if raw in contract.target_values or raw in scad_src.extract_top_level_numbers(source):
            continue
        if scad_src.is_assigned(source, raw):
            return None

    # 契约里的目标名可能和源码里的参数名对不上（规格叫 height、源码叫 stand_height）。
    # 契约构建时已经解析过一次，但那时可能还没有可用的参数表，
    # 所以这里对着**真实源码的参数**再解析一次作为兜底。
    actual = scad_src.extract_top_level_numbers(source)
    labels = scad_src.extract_param_labels(source)
    values: dict[str, float] = {}
    for name, value in contract.target_values.items():
        resolved = name if name in actual else resolve_param_name(name, actual, labels)
        if resolved is None:
            continue
        if resolved != name and scad_src.is_assigned(source, name):
            # 源码里确实有 `name = 表达式;`（不是纯数值赋值）：这时**不能**
            # 退而改另一个参数，那会改出另一个尺寸，还对外宣称改了目标值。
            return None
        values[resolved] = value
    if not values:
        return None

    # 跟随项（`contract.follow`）一并按比例改写：总高 100→50 时，加强筋的
    # 起始/结束高度必须跟着降，否则筋会捅出筒身 —— 那是另一个错模型，
    # 不是"改动更小"。跟随项只在没被显式指定目标值时参与。
    for name, value in follow_values(contract, actual, labels).items():
        values.setdefault(name, value)

    rewritten, applied, _ignored = scad_src.apply_params(source, values)
    if not applied or set(applied) != set(values):
        # 目标里有的参数没能写进去 —— 说明它不是顶层数值赋值（可能是表达式），
        # 这种情况下的部分改写可能造成「半改」，宁可不做
        return None
    return rewritten


def build_param_meta(source: str, params: dict[str, float]) -> list[dict[str, Any]]:
    """合并源码里的 @param 注释与真实数值，产出界面上的参数控件描述。"""
    descriptions: dict[str, dict[str, Any]] = {}
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped.startswith("//"):
            continue
        body = stripped[2:].strip()
        if not body.lower().startswith("@param"):
            continue
        parts = body[6:].split()
        if len(parts) < 2:
            continue
        name = parts[0]
        try:
            default = float(parts[1])
        except ValueError:
            continue
        label = parts[2] if len(parts) > 2 else name
        minimum: float | None = None
        maximum: float | None = None
        numbers: list[float] = []
        for token in parts[3:]:
            try:
                numbers.append(float(token))
            except ValueError:
                continue
        if len(numbers) >= 2:
            minimum, maximum = numbers[0], numbers[1]
        descriptions[name] = {
            "name": name,
            "label": label,
            "value": params.get(name, default),
            "unit": "mm",
            "min": minimum,
            "max": maximum,
        }

    result: list[dict[str, Any]] = []
    for name, value in params.items():
        if name in descriptions:
            descriptions[name]["value"] = value
            result.append(descriptions[name])
        else:
            result.append(
                {"name": name, "label": name, "value": value, "unit": "mm", "min": None, "max": None}
            )
    return result
