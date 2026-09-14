"""应用控制接口：打开文件夹、打开外部链接、健康检查、退出。"""

from __future__ import annotations

import logging
import os
import time
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter
from pydantic import BaseModel

from ..paths import paths
from ..version import APP_VERSION
from .errors import AppError
from .settings_routes import ALLOWED_EXTERNAL_HOSTS

log = logging.getLogger(__name__)

router = APIRouter(tags=["app"])

_started_at = time.time()


@router.get("/api/app/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "version": APP_VERSION, "uptime_s": round(time.time() - _started_at, 1)}


@router.get("/api/app/window-runtime")
async def window_runtime() -> dict[str, Any]:
    """原生窗口组件（WebView2）状态。

    前端在「用浏览器打开」时会带上 `?browser_fallback=1`，据此查询本接口
    并在页面上说明原因 —— 否则用户只会觉得「这软件怎么开了个网页」。
    """
    from ..webview_runtime import cached_status

    return cached_status(refresh=True).to_dict()


@router.post("/api/app/window-runtime/install")
async def install_window_runtime() -> dict[str, Any]:
    """一键安装 WebView2 运行时（走 winget，失败回退官方 Bootstrapper）。"""
    from .. import envcheck
    from ..webview_runtime import try_install

    ok, message = await asyncio.to_thread(try_install)
    envcheck.invalidate_cache()
    return {"ok": ok, "message": message}


class OpenFolderRequest(BaseModel):
    kind: str = "data"
    project_id: str | None = None


@router.post("/api/app/open-folder")
async def open_folder(payload: OpenFolderRequest) -> dict[str, Any]:
    files = paths()
    target = {
        "data": files.data_dir,
        "projects": files.projects_dir,
        "exports": files.projects_dir,
        "runtime": files.runtime_dir,
        "logs": files.log_dir,
    }.get(payload.kind)

    if payload.kind == "project" and payload.project_id:
        from .. import projects as projects_mod

        project = projects_mod.store().get(payload.project_id)
        if project is None:
            raise AppError("没有找到这个项目。", code="INTERNAL", hint="它可能已被删除。", status_code=404)
        target = project.dir

    if target is None:
        raise AppError("无法识别要打开的位置。", code="INTERNAL", hint="可用的位置：data、projects、project。")

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    try:
        os.startfile(str(target))  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        raise AppError(
            f"无法打开文件夹：{exc}",
            code="INTERNAL",
            hint=f"你可以手动在资源管理器地址栏粘贴：{target}",
        ) from exc
    return {"ok": True, "path": str(target)}


class OpenExternalRequest(BaseModel):
    url: str


@router.post("/api/app/open-external")
async def open_external(payload: OpenExternalRequest) -> dict[str, Any]:
    import webbrowser

    parsed = urlparse(payload.url)
    if parsed.scheme != "https":
        raise AppError(
            "只允许打开 https 链接。",
            code="INTERNAL",
            hint="为安全起见，本软件不会打开其他类型的链接。",
        )
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_EXTERNAL_HOSTS:
        raise AppError(
            f"不允许打开该网址（{host}）。",
            code="INTERNAL",
            hint="本软件只允许打开 DeepSeek 与 OpenSCAD 官方网站。",
        )
    try:
        webbrowser.open(payload.url, new=2)
    except Exception as exc:  # noqa: BLE001
        raise AppError(
            f"无法打开浏览器：{exc}",
            code="INTERNAL",
            hint=f"你可以手动访问：{payload.url}",
        ) from exc
    return {"ok": True}


@router.get("/api/app/shortcuts")
async def shortcuts() -> dict[str, Any]:
    """快捷方式创建情况（安装器会在安装时创建，这里提供运行期兜底）。"""
    from ..shortcuts import describe_shortcuts

    return describe_shortcuts()


@router.post("/api/app/shortcuts/repair")
async def repair_shortcuts() -> dict[str, Any]:
    from ..shortcuts import create_shortcuts

    result = create_shortcuts()
    return result


@router.post("/api/app/shutdown")
async def shutdown() -> dict[str, Any]:
    """退出程序：由 server 层注入的回调负责真正结束进程。"""
    import asyncio

    from .. import runtime_state

    runtime_state.request_shutdown()
    asyncio.get_running_loop().call_later(0.4, runtime_state.force_exit)
    return {"ok": True, "message": "正在退出 AI 3D Forge…"}
