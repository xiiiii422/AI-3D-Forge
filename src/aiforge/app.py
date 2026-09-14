"""AI 3D Forge —— FastAPI 应用装配。

安全模型（需求第三十三、四十五节）：
  * 服务只监听 127.0.0.1，且只接受本机来源的请求 —— 局域网内其他电脑无法访问。
  * CORS 只放行本机开发端口，避免任意网页调用本机接口。
  * API Key 只存在于后端进程与配置目录，前端拿到的永远是掩码值（sk-xxxx****yyyy）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import (
    app_routes,
    env,
    jobs_routes,
    openscad_routes,
    printer_routes,
    projects_routes,
    settings_routes,
)
from .api.errors import AppError
from .paths import frontend_dir
from .version import APP_NAME, APP_VERSION, CHANNEL_LABEL

log = logging.getLogger(__name__)

LOCAL_ORIGIN_RE = (
    "http://127.0.0.1",
    "http://localhost",
)


def create_app() -> FastAPI:
    app = FastAPI(
        title=APP_NAME,
        version=APP_VERSION,
        description=f"{APP_NAME} {APP_VERSION}（{CHANNEL_LABEL}）本机服务",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        """拒绝非本机来源，防止局域网或恶意网页访问本机服务。"""
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if host and host not in ("127.0.0.1", "localhost", "[::1]", "::1"):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "本服务只允许本机访问。",
                    "code": "INTERNAL",
                    "hint": "请通过桌面快捷方式启动的窗口访问。",
                },
            )

        origin = request.headers.get("origin")
        if origin and not origin.startswith(LOCAL_ORIGIN_RE):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "请求来源不被允许。",
                    "code": "INTERNAL",
                    "hint": "本软件的本机接口只接受自身界面的调用。",
                },
            )
        return await call_next(request)

    # --- 统一错误结构 ---------------------------------------------------
    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        return JSONResponse(
            status_code=422,
            content={
                "detail": f"请求参数不正确（{location or '未知字段'}）。",
                "code": "INTERNAL",
                "hint": "这通常是界面与后台版本不一致导致的，请刷新页面后重试。",
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
        log.exception("未处理的异常")
        return JSONResponse(
            status_code=500,
            content={
                "detail": "软件内部出现了意外问题，已记录到日志。",
                "code": "INTERNAL",
                "hint": "请重试一次；若反复出现，可在「系统诊断」中查看详情或重启软件。",
            },
        )

    # --- 路由 -----------------------------------------------------------
    app.include_router(app_routes.router)
    app.include_router(env.router)
    app.include_router(openscad_routes.router)
    app.include_router(printer_routes.router)
    app.include_router(jobs_routes.router)
    app.include_router(projects_routes.router)
    app.include_router(settings_routes.router)

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """托管前端构建产物；找不到时给出可执行的排查提示而不是白屏。"""
    dist = frontend_dir()
    index = dist / "index.html"

    if not index.is_file():
        @app.get("/")
        async def missing_frontend() -> JSONResponse:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "界面资源缺失，无法显示操作界面。",
                    "code": "INTERNAL",
                    "hint": (
                        "开发模式请先在 webui 目录执行 npm run build；"
                        "安装版请重新运行安装程序修复。"
                    ),
                },
            )

        return

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/")
    async def root() -> FileResponse:
        return FileResponse(index)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Any:
        icon = dist / "favicon.ico"
        if icon.is_file():
            return FileResponse(icon)
        # 必须用 Response(204) 而不是 JSONResponse(204, content=None)：
        # JSONResponse 会实际写出 4 字节 "null" 实体，而 204 按规范不允许有实体。
        # Starlette 不会为 204 设置 Content-Length，h11 因此认为「多出了数据」并抛
        # LocalProtocolError，直接切断这条 keep-alive 连接 —— 浏览器每次加载页面都会
        # 请求 favicon，于是紧随其后的 API 调用可能报 ECONNRESET，
        # 用户看到的是「无法连接到本地服务」。实测一次会话可刷出 20+ 次该异常。
        return Response(status_code=204)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> Any:
        # 前端路由（/studio、/settings…）刷新时也必须能正确返回入口页
        if full_path.startswith("api/"):
            return JSONResponse(
                status_code=404,
                content={"detail": "没有找到该接口。", "code": "INTERNAL", "hint": None},
            )
        candidate = (dist / full_path).resolve()
        try:
            candidate.relative_to(dist.resolve())
        except ValueError:
            return JSONResponse(status_code=404, content={"detail": "没有找到该资源。", "code": "INTERNAL"})
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)


def frontend_available() -> bool:
    return (Path(frontend_dir()) / "index.html").is_file()
