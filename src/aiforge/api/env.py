"""环境检测、系统诊断与版本信息接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from .. import envcheck, openscad as scad_cli
from ..version import version_info

router = APIRouter(tags=["env"])


@router.get("/api/env/report")
async def env_report(live: bool = Query(False, description="是否执行联网的真实 API 可用性测试")) -> dict[str, Any]:
    return await envcheck.build_report(live=live, use_cache=not live)


@router.post("/api/env/detect")
async def env_detect() -> dict[str, Any]:
    envcheck.invalidate_cache()
    return await envcheck.build_report(use_cache=False, deep=True)


@router.get("/api/env/version")
async def env_version() -> dict[str, Any]:
    info = scad_cli.detect(deep=False)
    return version_info(info.version)


@router.get("/api/diagnostics")
async def diagnostics() -> dict[str, Any]:
    return await envcheck.build_diagnostics()


class FixRequest(BaseModel):
    id: str


@router.post("/api/diagnostics/fix")
async def diagnostics_fix(payload: FixRequest) -> dict[str, Any]:
    return await envcheck.auto_fix(payload.id)
