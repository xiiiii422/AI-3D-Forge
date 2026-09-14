"""设置接口：API Key、模型、路径、行为开关、首次运行标记。"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .. import envcheck
from .. import openscad as scad_cli
from ..config import store
from ..llm import test_api_key
from ..paths import APP_SLUG, free_space_gb, paths
from ..version import APP_VERSION, CHANNEL_LABEL
from .errors import AppError

router = APIRouter(tags=["settings"])

ALLOWED_EXTERNAL_HOSTS = {
    "platform.deepseek.com",
    "api-docs.deepseek.com",
    "www.deepseek.com",
    "deepseek.com",
    "openscad.org",
    "www.openscad.org",
    "files.openscad.org",
    "github.com",
}


def _serialize() -> dict[str, Any]:
    cfg = store()
    files = paths()
    key = cfg.api_key
    backend = cfg.secrets.backend
    from ..printers import registry as printer_registry

    preg = printer_registry()
    active_printer = preg.selected()
    active_material = preg.effective_material()

    # Key 来自环境变量时，界面上的「清除」清不掉它（环境变量不归本软件管）。
    # 与其让用户困惑，不如如实说明来源与办法。
    env_var = "DEEPSEEK_API_KEY" if os.environ.get("DEEPSEEK_API_KEY") else (
        "AIFORGE_API_KEY" if os.environ.get("AIFORGE_API_KEY") else None
    )
    note: str | None = None
    if backend == "env" and env_var:
        note = (
            f"当前 API Key 来自环境变量 {env_var}（不是软件里保存的）。"
            f"如需更换，请先删除该系统环境变量，或在「设置」中保存一个新的 Key。"
        )

    return {
        "api_key_set": bool(key),
        "api_key_masked": cfg.mask_key(key),
        "api_key_store": backend,
        "api_key_source_note": note,
        "base_url": cfg.config.api.base_url,
        "model": cfg.config.api.model,
        "timeout_s": cfg.config.api.timeout_s,
        "openscad_path": cfg.config.openscad.path,
        "openscad_version": cfg.config.openscad.version,
        "openscad_auto_install": cfg.config.openscad.auto_install,
        "data_dir": str(files.data_dir),
        "projects_dir": str(files.projects_dir),
        "runtime_dir": str(files.runtime_dir),
        "keep_stl_history": cfg.config.behavior.keep_stl_history,
        "open_in_browser": cfg.config.behavior.open_in_browser,
        "auto_install_openscad": cfg.config.openscad.auto_install,
        "first_run_done": cfg.config.first_run_done,
        "theme": cfg.config.ui.theme,
        "last_project_id": cfg.config.ui.last_project_id,
        # 目标打印机（生成与检查的基准）
        "printer_id": active_printer.id,
        "printer_name": active_printer.name,
        "printer_material": active_material,
        # 生成流程上的两个「要不要先跟用户对一下」开关
        "announce_in_chat": cfg.config.printer.announce_in_chat,
        "confirm_spec": cfg.config.printer.confirm_spec,
        "app_version": APP_VERSION,
        "channel": CHANNEL_LABEL,
        "disk_free_gb": {
            "data": free_space_gb(files.data_dir),
            "projects": free_space_gb(files.projects_dir),
        },
    }


@router.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return _serialize()


class SettingsUpdate(BaseModel):
    api_key: str | None = Field(None, description="传入则写入；空字符串表示清除")
    base_url: str | None = None
    model: str | None = None
    timeout_s: int | None = None
    openscad_path: str | None = None
    keep_stl_history: bool | None = None
    open_in_browser: bool | None = None
    auto_install_openscad: bool | None = None
    theme: str | None = None
    last_project_id: str | None = None
    printer_id: str | None = None
    printer_material: str | None = None
    announce_in_chat: bool | None = None
    confirm_spec: bool | None = None


@router.put("/api/settings")
async def update_settings(payload: SettingsUpdate) -> dict[str, Any]:
    cfg = store()

    if payload.api_key is not None:
        backend = cfg.set_api_key(payload.api_key)
        envcheck.invalidate_cache()
        if payload.api_key.strip() and backend == "config_file":
            # 如实告知用户密钥的存放方式，不夸大安全性
            cfg.save()

    api_updates: dict[str, Any] = {}
    if payload.base_url is not None:
        api_updates["base_url"] = payload.base_url.strip().rstrip("/") or "https://api.deepseek.com"
    if payload.model is not None:
        api_updates["model"] = payload.model.strip() or "deepseek-chat"
    if payload.timeout_s is not None:
        api_updates["timeout_s"] = max(30, min(600, payload.timeout_s))
    if api_updates:
        cfg.update("api", api_updates)

    if payload.openscad_path is not None:
        path = payload.openscad_path.strip()
        if path and not os.path.isfile(path):
            from .errors import AppError

            raise AppError(
                "指定的 OpenSCAD 路径不存在。",
                code="OPENSCAD_MISSING",
                hint="请重新选择 openscad.exe，或在「系统诊断」中点击「自动修复」。",
            )
        cfg.update(
            "openscad",
            {"path": path or None, "version": scad_cli.read_version(path) if path else None},
        )
        envcheck.invalidate_cache()

    behavior_updates: dict[str, Any] = {}
    if payload.keep_stl_history is not None:
        behavior_updates["keep_stl_history"] = payload.keep_stl_history
    if payload.open_in_browser is not None:
        behavior_updates["open_in_browser"] = payload.open_in_browser
    if behavior_updates:
        cfg.update("behavior", behavior_updates)

    if payload.auto_install_openscad is not None:
        cfg.update("openscad", {"auto_install": payload.auto_install_openscad})

    ui_updates: dict[str, Any] = {}
    if payload.theme in ("light", "dark", "system"):
        ui_updates["theme"] = payload.theme
    if payload.last_project_id is not None:
        ui_updates["last_project_id"] = payload.last_project_id or None
    if ui_updates:
        cfg.update("ui", ui_updates)

    if payload.printer_id is not None or payload.printer_material is not None:
        from ..printers import registry as printer_registry

        preg = printer_registry()
        target_id = payload.printer_id or preg.selected().id
        if preg.get(target_id) is None:
            raise AppError(
                "没有找到这台打印机配置。",
                code="INTERNAL",
                hint="请在「设置 → 打印机」中重新选择。",
            )
        preg.select(target_id, payload.printer_material)
        cfg.update(
            "printer",
            {"printer_id": target_id, "material": preg.effective_material()},
        )

    # 生成流程上的两个开关：都属于 printer 段（它们只影响「生成前的对齐方式」）
    flow_updates: dict[str, Any] = {}
    if payload.announce_in_chat is not None:
        flow_updates["announce_in_chat"] = payload.announce_in_chat
    if payload.confirm_spec is not None:
        flow_updates["confirm_spec"] = payload.confirm_spec
    if flow_updates:
        cfg.update("printer", flow_updates)

    cfg.save()
    return _serialize()


class ApiKeyTestRequest(BaseModel):
    api_key: str | None = None


@router.post("/api/settings/api-key/test")
async def test_key(payload: ApiKeyTestRequest) -> dict[str, Any]:
    cfg = store()
    key = (payload.api_key or "").strip() or cfg.api_key
    if not key:
        # 未配置 Key 是「前置条件不满足」，不是「测试结果为失败」。
        # 返回 400 + NO_API_KEY，前端据此显示「去配置 AI 服务」按钮而不是一个红叉。
        raise AppError(
            "尚未配置 DeepSeek API Key。",
            code="NO_API_KEY",
            hint="请填入 API Key 后再测试（可在 platform.deepseek.com 免费创建）。",
            status_code=400,
        )
    result = await test_api_key(key, cfg.config.api.base_url, cfg.config.api.model)
    envcheck.invalidate_cache()
    return result


@router.post("/api/settings/first-run/complete")
async def complete_first_run() -> dict[str, Any]:
    cfg = store()
    cfg.config.first_run_done = True
    cfg.save()
    envcheck.invalidate_cache()
    return {"ok": True}


@router.post("/api/settings/reset-wizard")
async def reset_wizard() -> dict[str, Any]:
    """重新打开首次向导（用于演示或重新配置）。"""
    cfg = store()
    cfg.config.first_run_done = False
    cfg.save()
    envcheck.invalidate_cache()
    return {"ok": True}


@router.get("/api/settings/storage")
async def storage_info() -> dict[str, Any]:
    files = paths()
    projects = []
    total = 0
    try:
        from .. import projects as projects_mod

        for project in projects_mod.store().list():
            size = 0
            try:
                for item in project.dir.rglob("*"):
                    if item.is_file():
                        size += item.stat().st_size
            except OSError:
                pass
            total += size
            projects.append({"id": project.id, "name": project.name, "size_bytes": size})
    except OSError:
        pass
    return {
        "data_dir": str(files.data_dir),
        "projects_dir": str(files.projects_dir),
        "runtime_dir": str(files.runtime_dir),
        "log_dir": str(files.log_dir),
        "projects_total_bytes": total,
        "projects": projects,
        "free_gb": {
            "data": free_space_gb(files.data_dir),
            "projects": free_space_gb(files.projects_dir),
        },
        "note": f"用户数据保存在你的用户目录（{APP_SLUG}），卸载或升级软件都不会删除。",
    }
