"""OpenSCAD 检测、定位、手动指定与自动安装接口（含 SSE 实时进度）。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import envcheck, openscad as scad_cli
from ..config import store
from .errors import AppError

log = logging.getLogger(__name__)

router = APIRouter(tags=["openscad"])

# 安装阶段 → 整体进度百分比（阶段内部再用下载进度细化）
STAGE_BASE = {
    "prepare": (0.0, 4.0),
    "download": (4.0, 62.0),
    "verify": (62.0, 70.0),
    "install": (70.0, 88.0),
    "locate": (88.0, 92.0),
    "config": (92.0, 95.0),
    "cli_test": (95.0, 99.0),
    "done": (100.0, 100.0),
}


class InstallSession:
    """一次自动安装的进度广播。"""

    def __init__(self, method: str) -> None:
        self.job_id = f"scad-{int(time.time() * 1000)}"
        self.method = method
        self.created_at = time.time()
        self.finished = False
        self.ok = False
        self.result: dict[str, Any] | None = None
        self.error: dict[str, Any] | None = None
        self.logs: list[str] = []
        self.subscribers: list[asyncio.Queue] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    # --- 生命周期 -------------------------------------------------------
    def bind_loop(self) -> None:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        for queue in list(self.subscribers):
            try:
                queue.put_nowait((event, payload))
            except asyncio.QueueFull:
                pass

    def publish_threadsafe(self, event: str, payload: dict[str, Any]) -> None:
        """安装在线程里执行，事件必须安全地投递回事件循环。"""
        loop = self._loop
        if loop is None or not loop.is_running():
            self._emit(event, payload)
            return
        try:
            loop.call_soon_threadsafe(self._emit, event, payload)
        except RuntimeError:
            self._emit(event, payload)

    def progress_callback(self, stage: str, percent: float | None, message: str, extra: dict[str, Any]) -> None:
        base, ceiling = STAGE_BASE.get(stage, (0.0, 100.0))
        if stage == "download" and percent is not None and ceiling > base:
            overall = base + (ceiling - base) * (percent / 100.0)
        else:
            overall = ceiling if percent is None else base + (ceiling - base) * (percent / 100.0)
        overall = max(0.0, min(100.0, overall))
        payload: dict[str, Any] = {
            "stage": stage,
            "percent": round(overall, 1),
            "message": message,
        }
        payload.update(extra)
        self.logs.append(message)
        self.publish_threadsafe("progress", payload)

    def finish_ok(self, info: scad_cli.OpenScadInfo) -> None:
        self.finished = True
        self.ok = True
        self.result = {"info": info.to_dict(), "message": "OpenSCAD 已安装并配置完成。"}
        self.publish_threadsafe("done", self.result)

    def finish_error(self, message: str, hint: str | None, need_admin: bool = False, code: str = "OPENSCAD_MISSING") -> None:
        self.finished = True
        self.ok = False
        self.error = {
            "ok": False,
            "message": message,
            "hint": hint,
            "code": code,
            "need_admin": need_admin,
            "manual_url": scad_cli.OFFICIAL_SITE,
            "manual_steps": scad_cli.manual_instructions()["steps"],
        }
        self.publish_threadsafe("error", self.error)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        if self.finished:
            queue.put_nowait(("done" if self.ok else "error", self.result or self.error or {}))
        self.subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        try:
            self.subscribers.remove(queue)
        except ValueError:
            pass


_sessions: dict[str, InstallSession] = {}
_active: InstallSession | None = None


def _gc_sessions() -> None:
    now = time.time()
    for key, session in list(_sessions.items()):
        if session.finished and now - session.created_at > 3600:
            _sessions.pop(key, None)
    if len(_sessions) > 20:
        ordered = sorted(_sessions.values(), key=lambda s: s.created_at)
        for session in ordered[: len(_sessions) - 20]:
            _sessions.pop(session.job_id, None)


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------
@router.get("/api/openscad/detect")
async def detect(refresh: bool = True) -> dict[str, Any]:
    info = await asyncio.to_thread(scad_cli.detect, True)
    if refresh:
        envcheck.invalidate_cache()
    return info.to_dict()


class LocateRequest(BaseModel):
    path: str = Field(..., description="用户选择的 openscad.exe 路径，或包含它的目录")


@router.post("/api/openscad/locate")
async def locate(payload: LocateRequest) -> dict[str, Any]:
    target = Path(payload.path.strip().strip('"'))
    ok, message, version = await asyncio.to_thread(scad_cli.validate_executable, target)
    if not ok:
        raise AppError(
            message,
            code="OPENSCAD_MISSING",
            hint="请选择 OpenSCAD 安装目录里的 openscad.exe（通常在 OpenSCAD 文件夹内）。",
        )
    if target.is_dir():
        nested = scad_cli.find_in_directory(target)
        if nested:
            target = nested

    store().update("openscad", {"path": str(target), "version": version})
    envcheck.invalidate_cache()
    info = await asyncio.to_thread(scad_cli.detect, True)
    info.detail = f"已使用你指定的 OpenSCAD {version}。"
    return info.to_dict()


@router.post("/api/openscad/browse")
async def browse() -> dict[str, Any]:
    """弹出 Windows 文件选择对话框（需要桌面会话）。"""
    result = await asyncio.to_thread(_open_file_dialog)
    if result is None:
        raise AppError(
            "无法打开文件选择窗口（当前环境没有桌面会话）。",
            code="INTERNAL",
            hint="请直接粘贴 openscad.exe 的完整路径，例如 D:\\\\OpenSCAD\\\\openscad.exe。",
            status_code=501,
        )
    return {"path": result}


def _open_file_dialog() -> str | None:
    # 优先使用 pywebview 原生对话框；不可用时退回 tkinter
    try:
        import webview

        window = webview.active_window()
        if window is not None:
            picked = window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("OpenSCAD 程序 (*.exe)", "所有文件 (*.*)"),
            )
            if picked:
                return picked[0] if isinstance(picked, (list, tuple)) else str(picked)
            return None
    except Exception:  # noqa: BLE001 - 无桌面会话时必然失败，继续降级
        pass

    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        picked = filedialog.askopenfilename(
            title="请选择 OpenSCAD 的 openscad.exe",
            filetypes=[("OpenSCAD 程序", "openscad.exe"), ("可执行文件", "*.exe"), ("所有文件", "*.*")],
        )
        root.destroy()
        return str(picked) if picked else None
    except Exception:  # noqa: BLE001
        return None


class InstallRequest(BaseModel):
    confirm: bool = False
    method: Literal["auto", "installer", "portable"] = "auto"


@router.post("/api/openscad/install", status_code=202)
async def install(payload: InstallRequest) -> dict[str, Any]:
    global _active

    if not payload.confirm:
        raise AppError(
            "需要先确认才能开始安装。",
            code="INTERNAL",
            hint="安装会从 OpenSCAD 官方网站下载安装包，请点击确认后继续。",
        )

    if _active is not None and not _active.finished:
        raise AppError(
            "OpenSCAD 正在安装中，请稍候。",
            code="INTERNAL",
            hint="安装进度可以在界面上看到。",
            status_code=409,
        )

    _gc_sessions()
    session = InstallSession(payload.method)
    session.bind_loop()
    _sessions[session.job_id] = session
    _active = session

    async def worker() -> None:
        global _active
        try:
            info = await asyncio.to_thread(scad_cli.install, payload.method, session.progress_callback)
            envcheck.invalidate_cache()
            session.finish_ok(info)
        except scad_cli.OpenScadError as exc:
            need_admin = "管理员" in (exc.hint or "") or "UAC" in (exc.hint or "")
            session.finish_error(exc.message, exc.hint, need_admin=need_admin, code=exc.code)
        except Exception as exc:  # noqa: BLE001
            log.exception("OpenSCAD 自动安装失败")
            session.finish_error(
                "安装过程中出现了意外问题。",
                hint=f"技术细节：{type(exc).__name__}: {exc}",
            )
        finally:
            _active = None

    asyncio.create_task(worker())
    return {"job_id": session.job_id, "method": payload.method}


@router.get("/api/openscad/install/{job_id}/stream")
async def install_stream(job_id: str) -> StreamingResponse:
    session = _sessions.get(job_id)
    if session is None:
        raise AppError("没有找到这次安装任务。", code="INTERNAL", hint="请重新点击「自动安装 OpenSCAD」。", status_code=404)

    queue = session.subscribe()

    async def event_source() -> AsyncIterator[str]:
        try:
            # 补发历史日志，保证中途打开页面也能看到完整进度
            for line in session.logs[-20:]:
                yield _sse("log", {"message": line})
            if session.finished:
                payload = session.result if session.ok else session.error
                yield _sse("done" if session.ok else "error", payload or {})
                return
            while True:
                try:
                    event, payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(event, payload)
                if event in ("done", "error"):
                    return
        finally:
            session.unsubscribe(queue)

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


@router.post("/api/openscad/test")
async def test_cli() -> dict[str, Any]:
    exe = scad_cli.resolve_executable()
    if exe is None:
        raise AppError(
            "还没有安装 OpenSCAD。",
            code="OPENSCAD_MISSING",
            hint="点击「自动安装 OpenSCAD」，软件会自动完成安装与配置。",
            status_code=409,
        )
    ok, message, elapsed = await asyncio.to_thread(scad_cli.smoke_test, exe)
    envcheck.invalidate_cache()
    return {
        "ok": ok,
        "message": message,
        "duration_ms": int(elapsed * 1000),
        "diagnostics": None if ok else "请查看「系统诊断」中的 3D 引擎一项。",
    }
