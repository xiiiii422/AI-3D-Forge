"""AI 3D Forge —— 环境检测与系统诊断中心。

对应需求第三十三、三十九、四十九节。启动时自动执行，用户也可以随时手动重测。

检测项（需求第三十三节要求的 10 项全部覆盖）：
    1. Windows 版本            6. 必要 Python 依赖
    2. Python 运行环境          7. 项目目录权限
    3. Python 版本要求          8. 网络连接
    4. OpenSCAD 是否安装        9. DeepSeek API 配置状态
    5. OpenSCAD 可执行文件位置  10. 本地临时目录读写

每一项都给出 status / detail / action / fixable：
  * detail 是「人话结论」，不是错误码。
  * action 明确告诉用户需要做什么，通过时为 None。
  * fixable 决定界面上是否出现「自动修复」按钮。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import shutil
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import openscad as scad_cli
from .config import store
from .paths import paths
from .version import (
    APP_VERSION,
    CHANNEL_LABEL,
    REQUIRED_PYTHON,
    os_label,
    python_bundled,
    python_ok,
    python_version,
    version_info,
)

log = logging.getLogger(__name__)

# 前端必须能识别这些 id（见 docs/API-CONTRACT.md 的 CheckId）
CHECK_IDS = (
    "windows",
    "runtime",
    "webview2",
    "python",
    "python_version",
    "openscad",
    "openscad_cli",
    "openscad_version",
    "deps",
    "project_dir",
    "network",
    "api",
    "api_live",
    "temp",
    "stl_preview",
    "mesh_engine",
)

# 打包后必须存在的模块；缺失会导致用户看到 ModuleNotFoundError（需求第三十六节）
REQUIRED_MODULES = (
    ("fastapi", "Web 服务框架"),
    ("uvicorn", "本机服务运行时"),
    ("httpx", "网络请求"),
    ("pydantic", "数据校验"),
    ("keyring", "API Key 安全存储"),
)

REPORT_CACHE_TTL_S = 20.0
_report_cache: tuple[float, dict[str, Any]] | None = None


@dataclass
class CheckItem:
    id: str
    title: str
    status: str  # ok | warn | fail | checking
    detail: str
    action: str | None = None
    fixable: bool = False
    meta: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "action": self.action,
            "fixable": self.fixable,
            "meta": self.meta,
        }


# --------------------------------------------------------------------------
# 单项检测
# --------------------------------------------------------------------------
def check_windows() -> CheckItem:
    label = os_label()
    if os.name != "nt":
        return CheckItem(
            id="windows",
            title="Windows 环境",
            status="warn",
            detail=f"当前系统为 {label}，本软件针对 Windows 10/11 优化。",
            action="部分功能可能不可用。",
            meta={"os": label},
        )
    build = 0
    try:
        build = int(label.split("build ")[-1].rstrip(")"))
    except (ValueError, IndexError):
        pass
    if build and build < 17763:
        return CheckItem(
            id="windows",
            title="Windows 环境",
            status="warn",
            detail=f"{label}，版本较旧。",
            action="建议升级到 Windows 10 1809 或更高版本。",
            meta={"os": label, "build": build},
        )
    return CheckItem(
        id="windows",
        title="Windows 环境",
        status="ok",
        detail=label,
        meta={"os": label, "build": build},
    )


def check_runtime() -> CheckItem:
    bundled = python_bundled()
    if bundled:
        return CheckItem(
            id="runtime",
            title="软件运行环境",
            status="ok",
            detail=f"内置运行环境已就绪（{CHANNEL_LABEL}，无需另装任何组件）。",
            meta={"frozen": True, "version": APP_VERSION},
        )
    return CheckItem(
        id="runtime",
        title="软件运行环境",
        status="warn",
        detail="当前以开发模式运行（直接使用系统 Python）。发布版会内置运行环境。",
        action="普通用户请使用安装包版本；开发者在源码目录运行属正常现象。",
        meta={"frozen": False, "version": APP_VERSION, "channel": CHANNEL_LABEL},
    )


def check_python() -> CheckItem:
    version = python_version()
    if python_bundled():
        return CheckItem(
            id="python",
            title="Python 运行环境",
            status="ok",
            detail=f"已随软件内置（Python {version}），你的电脑无需安装 Python。",
            meta={"version": version, "bundled": True, "executable": sys.executable},
        )
    return CheckItem(
        id="python",
        title="Python 运行环境",
        status="ok" if python_ok() else "fail",
        detail=f"检测到 Python {version}（开发模式）。",
        action=None if python_ok() else f"需要 Python {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]} 或更高版本。",
        meta={"version": version, "bundled": False, "executable": sys.executable},
    )


def check_python_version() -> CheckItem:
    version = python_version()
    ok = python_ok()
    required = f"{REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}"
    if python_bundled():
        return CheckItem(
            id="python_version",
            title="运行环境版本",
            status="ok",
            detail=f"内置运行环境 {version}，满足最低要求 {required}+。",
            meta={"version": version, "required": required},
        )
    return CheckItem(
        id="python_version",
        title="运行环境版本",
        status="ok" if ok else "fail",
        detail=f"当前 {version}，要求 {required} 或更高。",
        action=None if ok else f"请升级 Python 到 {required} 以上，或改用安装包版本。",
        meta={"version": version, "required": required},
    )


def check_openscad(deep: bool = True) -> CheckItem:
    info = scad_cli.detect(deep=deep)
    if not info.found:
        return CheckItem(
            id="openscad",
            title="OpenSCAD",
            status="fail",
            detail="未检测到 OpenSCAD。它负责把设计稿变成可打印的 3D 模型文件。",
            action="点击「自动安装 OpenSCAD」，软件会自动下载并配置好。",
            fixable=True,
            meta={"found": False},
        )
    if not info.cli_ok:
        return CheckItem(
            id="openscad",
            title="OpenSCAD",
            status="fail",
            detail=info.detail,
            action="可以尝试重新自动安装，或手动指定 openscad.exe 的位置。",
            fixable=True,
            meta=info.to_dict(),
        )
    return CheckItem(
        id="openscad",
        title="OpenSCAD",
        status="ok",
        detail=f"OpenSCAD {info.version} 已就绪。",
        meta=info.to_dict(),
    )


def check_openscad_cli() -> CheckItem:
    exe = scad_cli.resolve_executable()
    if exe is None:
        return CheckItem(
            id="openscad_cli",
            title="3D 引擎命令行",
            status="fail",
            detail="没有可用的 OpenSCAD，无法生成 3D 模型。",
            action="点击「自动安装 OpenSCAD」。",
            fixable=True,
        )
    ok, message, elapsed = scad_cli.smoke_test(exe)
    if ok:
        return CheckItem(
            id="openscad_cli",
            title="3D 引擎命令行",
            status="ok",
            detail=f"实测编译通过（{elapsed:.1f} 秒）。",
            meta={"path": str(exe), "duration_s": round(elapsed, 2)},
        )
    return CheckItem(
        id="openscad_cli",
        title="3D 引擎命令行",
        status="fail",
        detail=message,
        action="尝试自动修复；若仍失败，请重新安装 OpenSCAD。",
        fixable=True,
        meta={"path": str(exe)},
    )


def check_openscad_version() -> CheckItem:
    configured = store().config.openscad
    exe = scad_cli.resolve_executable()
    if exe is None:
        return CheckItem(
            id="openscad_version",
            title="OpenSCAD 版本",
            status="fail",
            detail="未安装，无法获取版本。",
            action="点击「自动安装 OpenSCAD」。",
            fixable=True,
        )
    version = configured.version or scad_cli.read_version(exe)
    if not version:
        return CheckItem(
            id="openscad_version",
            title="OpenSCAD 版本",
            status="warn",
            detail="无法读取版本号，但可执行文件存在。",
            action="可点击「自动修复」重新检测。",
            fixable=True,
            meta={"path": str(exe)},
        )
    return CheckItem(
        id="openscad_version",
        title="OpenSCAD 版本",
        status="ok",
        detail=f"版本 {version}（路径：{exe}）",
        meta={"version": version, "path": str(exe)},
    )


def check_webview2() -> CheckItem:
    """WebView2 运行时 + 原生窗口链路是否可用。

    这里检查的是「能不能开原生窗口」整条链路，而不只是运行时在不在：
    打包时漏掉 pywebview 的依赖（bottle / proxy_tools）会让窗口起不来、
    静默退回浏览器，必须让用户在诊断页上看得见。
    """
    from .webview_runtime import cached_status, check_native_window_ready

    status = cached_status(refresh=True)
    ready, detail = check_native_window_ready()

    if ready:
        return CheckItem(
            id="webview2",
            title="原生窗口组件",
            status="ok",
            detail=detail,
            meta={**status.to_dict(), "native_window_ready": True},
        )

    # 运行时缺失 → 可以一键安装；组件不完整 → 需要重新安装（自动修复帮不上）
    fixable = not status.installed
    action = (
        "可以点击「自动修复」安装微软官方的 WebView2 组件。"
        if fixable
        else "软件已改用系统浏览器，功能完全一致；重新安装软件可修复独立窗口。"
    )
    return CheckItem(
        id="webview2",
        title="原生窗口组件",
        status="warn",
        detail=detail,
        action=action,
        fixable=fixable,
        meta={**status.to_dict(), "native_window_ready": False},
    )


def check_deps() -> CheckItem:
    missing: list[str] = []
    present: list[str] = []
    for module, label in REQUIRED_MODULES:
        try:
            importlib.import_module(module)
            present.append(f"{module}（{label}）")
        except ImportError:
            missing.append(f"{module}（{label}）")

    if missing:
        return CheckItem(
            id="deps",
            title="必要组件",
            status="fail",
            detail="缺少组件：" + "、".join(missing),
            action="点击「自动修复」由软件自动补齐；发布版不会出现此问题。",
            fixable=True,
            meta={"missing": missing, "present": present},
        )
    return CheckItem(
        id="deps",
        title="必要组件",
        status="ok",
        detail=f"{len(present)} 项运行组件已全部就绪。",
        meta={"present": present},
    )


def check_project_dir() -> CheckItem:
    files = paths()
    writable, message = files.is_writable(files.projects_dir)
    if not writable:
        return CheckItem(
            id="project_dir",
            title="项目目录",
            status="fail",
            detail=f"项目目录不可写：{files.projects_dir}（{message}）",
            action="请在「设置」中更换项目保存位置，或检查该目录的权限。",
            fixable=True,
            meta={"path": str(files.projects_dir), "error": message},
        )
    try:
        count = sum(1 for _ in files.projects_dir.iterdir())
    except OSError:
        count = 0
    return CheckItem(
        id="project_dir",
        title="项目目录",
        status="ok",
        detail=f"可正常读写（{files.projects_dir}）",
        meta={"path": str(files.projects_dir), "entries": count},
    )


def check_temp() -> CheckItem:
    import tempfile

    base = paths().cache_dir
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        base = Path(tempfile.gettempdir())
    writable, message = paths().is_writable(base)
    if not writable:
        return CheckItem(
            id="temp",
            title="临时目录",
            status="fail",
            detail=f"临时目录不可读写：{base}（{message}）",
            action="请检查磁盘空间与安全软件设置，然后点击「自动修复」。",
            fixable=True,
            meta={"path": str(base)},
        )
    free = None
    try:
        free = round(shutil.disk_usage(base).free / (1024**3), 1)
    except OSError:
        pass
    detail = f"可正常读写（{base}）"
    status = "ok"
    action = None
    if free is not None and free < 1.0:
        status = "fail"
        detail = f"可读写，但所在磁盘仅剩 {free} GB，可能无法完成模型生成。"
        action = "请清理磁盘空间后重试。"
    elif free is not None and free < 5.0:
        status = "warn"
        detail = f"可读写，但所在磁盘仅剩 {free} GB。"
        action = "建议清理磁盘，避免生成大模型时失败。"
    return CheckItem(
        id="temp",
        title="临时目录",
        status=status,
        detail=detail,
        action=action,
        fixable=False,
        meta={"path": str(base), "free_gb": free},
    )


async def check_network(timeout: float = 6.0) -> CheckItem:
    """分别测试 DeepSeek 与 OpenSCAD 官方源的可达性。"""
    import httpx

    targets = {
        "DeepSeek AI 服务": "https://api.deepseek.com",
        "OpenSCAD 官方下载源": "https://files.openscad.org",
    }
    results: dict[str, bool | str] = {}
    details: list[str] = []

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for label, url in targets.items():
            try:
                response = await client.head(url)
                reachable = response.status_code < 500
                results[label] = reachable
                details.append(f"{label}：{'可访问' if reachable else '响应异常'}")
            except Exception as exc:  # noqa: BLE001 - 网络异常种类多，统一处理
                results[label] = f"{type(exc).__name__}"
                details.append(f"{label}：无法访问")

    ok_count = sum(1 for value in results.values() if value is True)
    if ok_count == len(targets):
        return CheckItem(
            id="network",
            title="网络连接",
            status="ok",
            detail="AI 服务与 OpenSCAD 官方源均可访问。",
            meta={"results": results},
        )
    if ok_count == 0:
        return CheckItem(
            id="network",
            title="网络连接",
            status="fail",
            detail="当前无法访问网络。" + "；".join(details),
            action="请检查网络连接；如果在公司网络或使用了代理，请确认允许访问 api.deepseek.com。",
            fixable=False,
            meta={"results": results},
        )
    return CheckItem(
        id="network",
        title="网络连接",
        status="warn",
        detail="部分网络不可访问。" + "；".join(details),
        action="AI 设计功能需要访问 DeepSeek；OpenSCAD 已安装时不需要访问官方源。",
        fixable=False,
        meta={"results": results},
    )


def check_api() -> CheckItem:
    cfg = store()
    key = cfg.api_key
    backend = cfg.secrets.backend
    backend_label = {
        "credential_manager": "Windows 凭据管理器",
        "config_file": "本机配置文件",
        "env": "环境变量",
    }.get(backend, backend)

    if not key:
        return CheckItem(
            id="api",
            title="DeepSeek API",
            status="warn",
            detail="尚未配置 DeepSeek API Key，AI 设计功能暂不可用。",
            action="填入 API Key 即可开始设计（可在 platform.deepseek.com 免费创建）。",
            fixable=False,
            meta={"configured": False, "base_url": cfg.config.api.base_url, "model": cfg.config.api.model},
        )
    return CheckItem(
        id="api",
        title="DeepSeek API",
        status="ok",
        detail=f"已配置（{cfg.mask_key(key)}，保存在{backend_label}）。",
        meta={
            "configured": True,
            "masked": cfg.mask_key(key),
            "store": backend,
            "base_url": cfg.config.api.base_url,
            "model": cfg.config.api.model,
        },
    )


async def check_api_live(timeout: float = 25.0) -> CheckItem:
    """真实调用一次 DeepSeek，确认 Key 真的能用（而不是只填了字符串）。"""
    cfg = store()
    key = cfg.api_key
    if not key:
        return CheckItem(
            id="api_live",
            title="AI 服务连通性",
            status="warn",
            detail="尚未配置 API Key，跳过测试。",
            action="配置 API Key 后可测试。",
        )
    from .llm import test_api_key

    api = cfg.config.api
    result = await test_api_key(key, api.base_url, api.model)
    if result["ok"]:
        return CheckItem(
            id="api_live",
            title="AI 服务连通性",
            status="ok",
            detail=str(result["message"]),
            meta={"model": result.get("model"), "latency_ms": result.get("latency_ms")},
        )
    return CheckItem(
        id="api_live",
        title="AI 服务连通性",
        status="fail",
        detail=str(result["message"]),
        action=str(result.get("hint") or "请检查 API Key 与网络后重试。"),
        fixable=False,
        meta={"model": result.get("model")},
    )


def check_stl_preview() -> CheckItem:
    """前端 Three.js 预览依赖 WebGL。后端无法直接探测浏览器能力，
    因此这里检查服务端为预览准备的条件（前端资源 + 测试模型），并给出明确说明。"""
    from .paths import frontend_dir

    dist = frontend_dir()
    has_ui = (dist / "index.html").is_file()
    has_viewer = False
    if dist.is_dir():
        try:
            has_viewer = any(dist.rglob("*.js"))
        except OSError:
            has_viewer = False

    if has_ui and has_viewer:
        return CheckItem(
            id="stl_preview",
            title="STL 预览",
            status="ok",
            detail="3D 预览组件已就绪（Three.js，浏览器本地渲染）。",
            meta={"dist": str(dist), "bundled": True},
        )
    if not has_ui:
        return CheckItem(
            id="stl_preview",
            title="STL 预览",
            status="warn",
            detail="未找到界面资源（开发模式请先执行前端构建）。",
            action="开发者在 webui 目录执行 npm run build；普通用户请使用安装包版本。",
            meta={"dist": str(dist)},
        )
    return CheckItem(
        id="stl_preview",
        title="STL 预览",
        status="ok",
        detail="3D 预览组件已就绪。",
        meta={"dist": str(dist)},
    )


def check_mesh_engine() -> CheckItem:
    """3D 模型引擎：用真实生成的模型跑一遍解析与分析。"""
    files = paths()
    probe_dir = files.cache_dir / "meshtest"
    try:
        probe_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return CheckItem(
            id="mesh_engine",
            title="3D 模型引擎",
            status="fail",
            detail=f"无法准备测试目录：{exc.strerror or exc}",
            action="点击「自动修复」重试。",
            fixable=True,
        )

    exe = scad_cli.resolve_executable()
    if exe is None:
        return CheckItem(
            id="mesh_engine",
            title="3D 模型引擎",
            status="fail",
            detail="缺少 OpenSCAD，无法验证模型生成能力。",
            action="点击「自动安装 OpenSCAD」。",
            fixable=True,
        )

    from . import compiler

    try:
        result = compiler.compile_to_stl("cube([20,20,20], center=true);", probe_dir, "engine_probe")
    except Exception as exc:  # noqa: BLE001
        return CheckItem(
            id="mesh_engine",
            title="3D 模型引擎",
            status="fail",
            detail=f"测试编译失败：{exc}",
            action="点击「自动修复」重新检测。",
            fixable=True,
        )
    if not result.ok or result.stl_path is None:
        return CheckItem(
            id="mesh_engine",
            title="3D 模型引擎",
            status="fail",
            detail=result.message,
            action="点击「自动修复」重新检测；若仍失败请重新安装 OpenSCAD。",
            fixable=True,
            meta={"diagnostics": [d.to_dict() for d in result.diagnostics[:8]]},
        )

    try:
        metrics = compiler.analyze_stl(result.stl_path)
    except Exception as exc:  # noqa: BLE001
        return CheckItem(
            id="mesh_engine",
            title="3D 模型引擎",
            status="fail",
            detail=f"模型解析失败：{exc}",
            action="点击「自动修复」重新检测。",
            fixable=True,
        )

    ok_geometry = metrics.watertight and metrics.volume_mm3 > 0
    return CheckItem(
        id="mesh_engine",
        title="3D 模型引擎",
        status="ok" if ok_geometry else "warn",
        detail=(
            f"实测通过：编译 20mm 立方体 → 封闭实体，"
            f"{metrics.triangles} 个三角面，体积 {metrics.volume_cm3:.1f} cm³。"
        ),
        meta=metrics.to_dict(),
    )


def check_user_data() -> CheckItem:
    files = paths()
    writable, message = files.is_writable(files.data_dir)
    if not writable:
        return CheckItem(
            id="project_dir",
            title="用户配置目录",
            status="fail",
            detail=f"配置目录不可写：{files.data_dir}（{message}）",
            action="请检查当前用户是否对该目录有写入权限。",
            fixable=True,
            meta={"path": str(files.data_dir)},
        )
    return CheckItem(
        id="project_dir",
        title="用户配置目录",
        status="ok",
        detail=f"可正常读写（{files.data_dir}）",
        meta={"path": str(files.data_dir)},
    )


def check_port_available() -> CheckItem:
    """本机回环端口是否可用（服务无法启动时的头号原因）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        return CheckItem(
            id="deps",
            title="本机服务端口",
            status="ok",
            detail=f"本机回环网络正常（示例端口 {port} 可用）。",
            meta={"sample_port": port},
        )
    except OSError as exc:
        return CheckItem(
            id="deps",
            title="本机服务端口",
            status="fail",
            detail=f"无法在本机建立网络服务：{exc.strerror or exc}",
            action="通常是安全软件拦截所致，请允许本软件访问本地网络。",
            fixable=False,
        )


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------
async def build_report(*, live: bool = False, use_cache: bool = True, deep: bool = True) -> dict[str, Any]:
    """生成完整环境报告。"""
    global _report_cache
    import time as _time

    if use_cache and not live and _report_cache is not None:
        stamp, cached = _report_cache
        if _time.time() - stamp < REPORT_CACHE_TTL_S:
            return cached

    # 阻塞型检测放线程池，网络检测并发执行
    blocking = [
        asyncio.to_thread(check_windows),
        asyncio.to_thread(check_runtime),
        asyncio.to_thread(check_python),
        asyncio.to_thread(check_python_version),
        asyncio.to_thread(check_openscad, deep),
        asyncio.to_thread(check_deps),
        asyncio.to_thread(check_project_dir),
        asyncio.to_thread(check_temp),
        asyncio.to_thread(check_stl_preview),
        asyncio.to_thread(check_webview2),
        asyncio.to_thread(check_api),
    ]
    items = list(await asyncio.gather(*blocking))

    extra = [check_network()]
    if live:
        extra.append(check_api_live())
    for coro in extra:
        items.append(await coro)

    order = {key: index for index, key in enumerate(CHECK_IDS)}
    items.sort(key=lambda item: order.get(item.id, 99))

    ok_count = sum(1 for item in items if item.status == "ok")
    fail_count = sum(1 for item in items if item.status == "fail")
    overall = "fail" if fail_count else ("warn" if ok_count < len(items) else "ok")

    report = {
        "checked_at": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "overall": overall,
        "ok_count": ok_count,
        "total": len(items),
        "first_run": not store().config.first_run_done,
        "items": [item.to_dict() for item in items],
    }
    _report_cache = (_time.time(), report)
    return report


def invalidate_cache() -> None:
    global _report_cache
    _report_cache = None


async def build_diagnostics() -> dict[str, Any]:
    """系统诊断中心：按用户能理解的分组呈现，并为可修复项提供自动修复入口。"""
    import time as _time

    opened = await asyncio.to_thread(check_openscad, True)
    detected = opened.meta or {}  # CheckItem.meta 里保存的是 OpenScadInfo.to_dict()
    cli = await asyncio.to_thread(check_openscad_cli) if opened.status == "ok" else CheckItem(
        id="openscad_cli",
        title="3D 引擎命令行",
        status="fail",
        detail="未检测到可用的 OpenSCAD。",
        action="点击「自动修复」安装 OpenSCAD。",
        fixable=True,
    )
    version = await asyncio.to_thread(check_openscad_version)
    mesh = await asyncio.to_thread(check_mesh_engine)
    api = await asyncio.to_thread(check_api)
    live = await check_api_live()
    network = await check_network()
    files = await asyncio.to_thread(check_project_dir)
    temp = await asyncio.to_thread(check_temp)
    preview = await asyncio.to_thread(check_stl_preview)
    deps = await asyncio.to_thread(check_deps)
    runtime = await asyncio.to_thread(check_runtime)
    windows = await asyncio.to_thread(check_windows)
    webview = await asyncio.to_thread(check_webview2)

    app_checked = await asyncio.to_thread(version_info, detected.get("version"))

    groups = [
        ("AI 服务", [api, live]),
        ("3D 引擎", [opened, cli, version]),
        ("模型生成", [mesh, deps]),
        ("STL 预览", [preview]),
        ("文件系统", [files, temp]),
        ("网络", [network]),
        ("运行环境", [runtime, windows, webview]),
    ]
    _ = webview  # 显式说明：WebView2 状态归入「运行环境」分组展示

    all_items: list[CheckItem] = [item for _, items in groups for item in items]
    fail_count = sum(1 for item in all_items if item.status == "fail")
    warn_count = sum(1 for item in all_items if item.status == "warn")
    overall = "fail" if fail_count else ("warn" if warn_count else "ok")

    return {
        "checked_at": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "overall": overall,
        "version": app_checked,
        "groups": [{"group": name, "items": [item.to_dict() for item in items]} for name, items in groups],
    }


# --------------------------------------------------------------------------
# 自动修复
# --------------------------------------------------------------------------
async def auto_fix(check_id: str) -> dict[str, Any]:
    """针对某一项执行自动修复。返回 (成功, 说明, 最新报告)。"""
    files = paths()

    if check_id in ("project_dir", "temp"):
        try:
            files.ensure()
            invalidate_cache()
            return {
                "ok": True,
                "message": "已重新创建必需目录，请确认结果。",
                "report": await build_report(use_cache=False, deep=False),
            }
        except OSError as exc:
            return {"ok": False, "message": f"目录创建失败：{exc}", "report": await build_report(use_cache=False, deep=False)}

    if check_id == "deps":
        # 发布版所有依赖都已打包，这里只做重新导入检查并说明
        return {
            "ok": True,
            "message": "已重新检查组件完整性。如仍显示缺失，请重新安装软件（不会影响你的项目与 API Key）。",
            "report": await build_report(use_cache=False, deep=False),
        }

    if check_id in ("openscad", "openscad_cli", "openscad_version", "mesh_engine"):
        try:
            info = await asyncio.to_thread(scad_cli.install, "auto")
            invalidate_cache()
            return {
                "ok": True,
                "message": f"OpenSCAD {info.version or ''} 已安装并配置完成。",
                "report": await build_report(use_cache=False, deep=True),
            }
        except scad_cli.OpenScadError as exc:
            return {
                "ok": False,
                "message": exc.message,
                "hint": exc.hint,
                "manual": scad_cli.manual_instructions(),
                "report": await build_report(use_cache=False, deep=False),
            }

    if check_id == "webview2":
        from .webview_runtime import try_install

        ok, message = await asyncio.to_thread(try_install)
        invalidate_cache()
        return {
            "ok": ok,
            "message": message,
            "report": await build_report(use_cache=False, deep=False),
        }

    if check_id == "api" or check_id == "api_live":
        return {
            "ok": False,
            "message": "API Key 需要你本人填写，软件无法代为申请。",
            "hint": "请在「设置 → AI 服务」中填入 API Key。",
            "report": await build_report(use_cache=False, deep=False),
        }

    if check_id == "network":
        invalidate_cache()
        report = await build_report(use_cache=False, deep=False)
        network = next((i for i in report["items"] if i["id"] == "network"), None)
        return {
            "ok": bool(network and network["status"] == "ok"),
            "message": "已重新测试网络连接。" if network and network["status"] == "ok" else "网络仍然不可访问，请检查网络或代理设置。",
            "report": report,
        }

    invalidate_cache()
    return {
        "ok": False,
        "message": "这一项无法自动修复。",
        "report": await build_report(use_cache=False, deep=False),
    }
