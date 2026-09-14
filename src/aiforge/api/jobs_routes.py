"""生成作业接口：创建、进度流、取消、参数微调、直接编译。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import jobs as jobs_mod
from .. import projects as projects_mod
from ..config import store
from .errors import AppError, need_api_key, not_found

log = logging.getLogger(__name__)

router = APIRouter(tags=["jobs"])


class CreateJobRequest(BaseModel):
    prompt: str = Field("", description="自然语言需求")
    project_id: str | None = None
    instruction: str | None = None
    mode: str = "create"
    name: str | None = None


def reject_if_busy(project_id: str) -> None:
    """同一个项目上已经有作业在跑时，拒绝新的作业（409）。

    为什么必须挡：两个作业会并发地对同一个项目目录
    「归档上一版 → 写 model.scad → 存 meta」，而且读到的 revision 相同 ——
    互相覆盖、静默丢模型，多出来的那次还会白花用户的 API 额度。
    前端虽然会在点下去之后立刻禁用发送键，但那只防手滑，
    防不住「响应还没回来就连点两次」、直接调接口、或第二个实例。

    等待确认（awaiting_confirmation）也算占着项目：那时作业正停在确认卡片上，
    用户应当先确认或取消，而不是再发一条。

    **但有一个例外（无人监听的等待确认）**：
    停在确认卡片上、而且**没有任何界面在监听**的作业，说明它的主人已经把它丢下了
    （刷新页面、切走项目、关窗口都会断开 SSE → unsubscribe）。
    这种作业会一直挂到进程结束；如果继续拿它挡住新任务，用户就会撞上
    「界面上根本没有停止按钮」的死锁 —— 实测路径：
    生成 → 弹出规格卡片 → 按 F5 → 再发一条 → 409「还有一次生成没有结束，
    请先点停止」，而卡片没了、停止按钮也没了，**只能重启软件**。
    所以这里把它就地退役（真取消，不是忽略），再放行新任务。

    为什么用「有没有人监听」而不是「等了多久」：时间判据会误伤 ——
    用户接杯水回来还要点确认，凭什么把他的作业取消掉。
    """
    busy = jobs_mod.active_job_for(project_id)
    if busy is None:
        return
    if busy.status == "awaiting_confirmation" and not busy.has_watcher:
        log.info("退役无人监听的等待确认作业 %s（项目 %s）", busy.job_id, project_id)
        busy.cancel()
        return
    raise AppError(
        "这个项目上还有一次生成没有结束。",
        code="BUSY",
        hint="请等它完成，或先点「停止」再重新发送。",
        status_code=409,
    )


@router.post("/api/jobs", status_code=202)
async def create_job(payload: CreateJobRequest) -> dict[str, Any]:
    pstore = projects_mod.store()

    instruction = (payload.instruction or "").strip()
    prompt = (payload.prompt or "").strip()
    mode = payload.mode

    if mode == "modify" or (payload.project_id and instruction):
        mode = "modify"
        if not instruction:
            raise AppError(
                "请说出你想怎么改，例如「高度改成 150mm」。",
                code="INTERNAL",
                hint="在对话框里输入修改要求即可。",
            )
    else:
        mode = "create"
        if not prompt:
            raise AppError(
                "请先描述你想设计的模型。",
                code="INTERNAL",
                hint="例如：「设计一个桌面手机支架，高度 100mm」。",
            )

    # 快速失败：没有 API Key 就不要让用户等到最后才看到错误
    if not store().api_key:
        raise need_api_key()

    if payload.project_id:
        project = pstore.get(payload.project_id)
        if project is None:
            raise not_found("这个项目")
    else:
        project = pstore.create(payload.name or "未命名模型")

    if mode == "modify" and not project.read_scad():
        # 项目还是空的，把修改要求当成一次全新设计
        mode = "create"
        prompt = instruction

    reject_if_busy(project.id)

    next_revision = project.revision + 1
    job = jobs_mod.create_job(
        project_id=project.id,
        revision=next_revision,
        mode=mode,
        prompt=prompt or None,
        instruction=instruction or None,
    )

    runner = jobs_mod.GenerationRunner(job)
    task = asyncio.create_task(
        runner.run(
            project=project,
            prompt=prompt if mode == "create" else None,
            instruction=instruction if mode == "modify" else None,
        )
    )
    job.task = task

    return {"job_id": job.job_id, "project_id": project.id, "revision": next_revision}


class ParamsRequest(BaseModel):
    params: dict[str, float]


@router.post("/api/projects/{project_id}/params", status_code=202)
async def apply_params(project_id: str, payload: ParamsRequest) -> dict[str, Any]:
    project = projects_mod.store().get(project_id)
    if project is None:
        raise not_found("这个项目")
    if not project.read_scad():
        raise AppError(
            "这个项目还没有模型，请先用一句话描述你要设计的东西。",
            code="INTERNAL",
            hint="在对话框里输入需求即可。",
        )
    if not payload.params:
        raise AppError("没有需要应用的参数。", code="INTERNAL", hint="请先调整参数数值。")

    reject_if_busy(project.id)

    job = jobs_mod.create_job(
        project_id=project.id,
        revision=project.revision + 1,
        mode="params",
    )
    runner = jobs_mod.GenerationRunner(job)
    job.task = asyncio.create_task(runner.run(project=project, params=payload.params))
    return {"job_id": job.job_id, "project_id": project.id, "revision": project.revision + 1}


class CompileRequest(BaseModel):
    scad: str
    project_id: str | None = None
    name: str | None = None
    params: dict[str, float] | None = None


@router.post("/api/compile", status_code=202)
async def compile_source(payload: CompileRequest) -> dict[str, Any]:
    if not payload.scad.strip():
        raise AppError("模型代码为空。", code="INTERNAL", hint="请粘贴 OpenSCAD 代码后再试。")

    pstore = projects_mod.store()
    if payload.project_id:
        project = pstore.get(payload.project_id)
        if project is None:
            raise not_found("这个项目")
    else:
        project = pstore.create(payload.name or "手动编译的模型")

    reject_if_busy(project.id)

    job = jobs_mod.create_job(
        project_id=project.id,
        revision=project.revision + 1,
        mode="compile",
    )
    runner = jobs_mod.GenerationRunner(job)
    job.task = asyncio.create_task(
        runner.run(
            project=project,
            scad_source=payload.scad,
            compile_params=payload.params,
        )
    )
    return {"job_id": job.job_id, "project_id": project.id, "revision": project.revision + 1}


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = jobs_mod.get(job_id)
    if job is None:
        raise not_found("这次生成任务")
    return job.snapshot()


@router.get("/api/jobs")
async def list_jobs(project_id: str | None = None) -> dict[str, Any]:
    items = [
        job.snapshot()
        for job in sorted(jobs_mod.all_jobs(), key=lambda j: j.created_at, reverse=True)
        if project_id is None or job.project_id == project_id
    ]
    return {"jobs": items[:20]}


@router.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, Any]:
    job = jobs_mod.get(job_id)
    if job is None:
        raise not_found("这次生成任务")
    job.cancel()
    return {"ok": True}


class ConfirmSpecRequest(BaseModel):
    """确认（或纠正）软件对需求的理解。

    spec 传 null 表示「就按你理解的做」；传回修改后的规格则采用用户的版本。
    用户还能在这一步补充说明（note），例如「底部要圆角」——
    这条说明会进入设计契约并持久化，后续轮次不会再被忘掉。
    """

    spec: dict[str, Any] | None = None
    note: str | None = None


@router.post("/api/jobs/{job_id}/confirm-spec")
async def confirm_spec(job_id: str, payload: ConfirmSpecRequest) -> dict[str, Any]:
    from ..design import DesignSpec

    job = jobs_mod.get(job_id)
    if job is None:
        raise not_found("这次生成任务")
    if not job.waiting_for_spec:
        raise AppError(
            "这次任务当前不需要确认规格。",
            code="INTERNAL",
            hint="它可能已经在生成中，或已经完成。",
            status_code=409,
        )

    spec: DesignSpec | None = None
    if payload.spec:
        spec = DesignSpec.from_dict(payload.spec)

    note = (payload.note or "").strip()
    if note:
        if spec is None:
            spec = job.awaiting_spec
        if spec is not None:
            spec.merge_clarification(note)

    job.confirm_spec(spec)
    return {"ok": True}


@router.get("/api/jobs/{job_id}/stream")
async def job_stream(job_id: str) -> StreamingResponse:
    job = jobs_mod.get(job_id)
    if job is None:
        raise not_found("这次生成任务")

    queue = job.subscribe()

    async def event_source() -> AsyncIterator[str]:
        try:
            while True:
                try:
                    event, payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # awaiting_confirmation 是「活着但在等人」：既不是结束，也不能只发心跳
                    # 就完事 —— 用户在这个状态刷新页面时，靠的就是接下来这份快照里的 spec。
                    if job.status == "awaiting_confirmation":
                        yield _sse("snapshot", job.snapshot())
                        continue
                    if job.status != "running":
                        # 作业已结束但没有收到终止事件：主动收尾，避免前端一直转圈。
                        # 必须推**完整快照**（只用 job.result 会丢掉 status/messages，
                        # 前端走宽松兜底分支时就会丢消息）。
                        if job.status == "done":
                            yield _sse("done", job.snapshot())
                        elif job.status == "cancelled":
                            yield _sse("cancelled", job.snapshot())
                        else:
                            yield _sse("error", job.error or {"message": "生成失败。"})
                        return
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(event, payload)
                if event in ("done", "error", "cancelled"):
                    return
        finally:
            job.unsubscribe(queue)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
