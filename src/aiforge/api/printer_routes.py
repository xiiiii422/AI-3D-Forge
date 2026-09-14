"""打印机配置与知识库接口。

需求对应：用户可以选择自己的打印机（机型 + 喷嘴 + 材料），
并且这个选择会真正影响 AI 生成、几何检查与打印性评审。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..config import store
from ..printers import (
    BUILTIN_PRINTERS,
    DEFAULT_PRINTER_ID,
    PrinterProfile,
    material_options,
    registry,
    resolve_fn,
    resolve_process,
)
from .errors import AppError, not_found

log = logging.getLogger(__name__)

router = APIRouter(tags=["printers"])


def _profile_payload(profile: PrinterProfile, material: str | None = None) -> dict[str, Any]:
    """机型详情 + 推导出的工艺约束（界面直接展示，无需前端自己算）。"""
    constraints = resolve_process(profile, material)
    return {
        **profile.to_public(),
        "constraints": constraints.to_dict(),
        "checklist": constraints.summary_lines(),
        # 该机型在常见尺寸下的推荐 $fn（让用户能理解为什么面数会变）
        "recommended_fn_examples": {
            "50mm": resolve_fn(profile, 50),
            "150mm": resolve_fn(profile, 150),
        },
    }


@router.get("/api/printers")
async def list_printers() -> dict[str, Any]:
    reg = registry()
    selected = reg.selected()
    material = reg.effective_material()
    return {
        "selected_id": selected.id,
        "selected_material": material,
        "default_id": DEFAULT_PRINTER_ID,
        "materials": material_options(reg.all()),
        "printers": [_profile_payload(p, material if p.id == selected.id else None) for p in reg.all()],
    }


@router.get("/api/printers/selected")
async def selected_printer() -> dict[str, Any]:
    reg = registry()
    profile = reg.selected()
    material = reg.effective_material()
    return {
        "printer": _profile_payload(profile, material),
        "material": material,
        "material_options": profile.materials,
        "note": "所有生成与检查都会以这台打印机为基准。",
    }


@router.get("/api/printers/knowledge/summary")
async def knowledge_summary() -> dict[str, Any]:
    """知识库自述：告诉用户我们到底懂哪些打印机特性，以及每一项怎么影响模型。"""
    from ..printers import MATERIAL_DENSITY, MATERIAL_HEAT_DEFlection

    return {
        "builtin_count": len(BUILTIN_PRINTERS),
        "features": [
            {
                "key": "build_volume",
                "label": "成型尺寸",
                "affects": "限制模型三轴最大尺寸；超限时提示缩小、旋转或分件。",
            },
            {
                "key": "nozzle_mm",
                "label": "喷嘴直径",
                "affects": "决定最小可靠壁厚（3×喷嘴）、最小孔径、最小凸起与表面分段数 $fn。",
            },
            {
                "key": "layer",
                "label": "层高范围",
                "affects": "决定可达的细节精度与打印时间；上限受喷嘴约束（不超过 0.8×喷嘴）。",
            },
            {
                "key": "overhang",
                "label": "最大悬垂角",
                "affects": "超过该角度的面需要支撑；AI 会被要求自动加倒角或改结构。",
            },
            {
                "key": "bridge",
                "label": "最大桥接跨度",
                "affects": "限制无支撑的横向跨距；超限时建议加肋或改为拱形。",
            },
            {
                "key": "tolerance",
                "label": "配合公差",
                "affects": "决定轴孔配合该留多少间隙，避免打出来装不上。",
            },
            {
                "key": "enclosure",
                "label": "腔体与温度",
                "affects": "决定能否用 ABS/ASA/PA；开放机身用高温材料会翘边开裂。",
            },
            {
                "key": "materials",
                "label": "可用材料",
                "affects": "决定材料密度（估算耗材克数）与热变形温度（使用场景限制）。",
            },
            {
                "key": "kinematics",
                "label": "运动结构",
                "affects": "Delta 的有效区域是圆形，按外接正方形保守判断。",
            },
        ],
        "material_density_g_cm3": MATERIAL_DENSITY,
        "material_heat_deflection_c": MATERIAL_HEAT_DEFlection,
    }


@router.get("/api/printers/{printer_id}")
async def get_printer(printer_id: str) -> dict[str, Any]:
    profile = registry().get(printer_id)
    if profile is None:
        raise not_found("这台打印机配置")
    return _profile_payload(profile)


class SelectPrinterRequest(BaseModel):
    printer_id: str
    material: str | None = None


@router.post("/api/printers/select")
async def select_printer(payload: SelectPrinterRequest) -> dict[str, Any]:
    reg = registry()
    try:
        profile = reg.select(payload.printer_id, payload.material)
    except KeyError:
        raise not_found("这台打印机配置") from None
    material = reg.effective_material()
    # 同步写进用户配置，方便界面首屏直接读到
    store().update("printer", {"printer_id": profile.id, "material": material})
    return {"printer": _profile_payload(profile, material), "material": material}


class PrinterWriteRequest(BaseModel):
    """新建/修改机型。字段全部可选，未提供的沿用默认值或原值。"""

    id: str | None = None
    name: str | None = None
    manufacturer: str | None = None
    nozzle_mm: float | None = Field(None, gt=0, le=2.0)
    build_x: float | None = Field(None, gt=0, le=2000)
    build_y: float | None = Field(None, gt=0, le=2000)
    build_z: float | None = Field(None, gt=0, le=2000)
    nozzle_options: list[float] | None = None
    layer_min_mm: float | None = Field(None, gt=0, le=2.0)
    layer_max_mm: float | None = Field(None, gt=0, le=2.0)
    max_speed_mm_s: int | None = Field(None, ge=0, le=2000)
    max_accel_mm_s2: int | None = Field(None, ge=0, le=100000)
    max_flow_mm3_s: float | None = Field(None, ge=0, le=200)
    has_heated_bed: bool | None = None
    max_bed_temp_c: int | None = Field(None, ge=0, le=200)
    max_hotend_temp_c: int | None = Field(None, ge=0, le=500)
    enclosure: bool | None = None
    materials: list[str] | None = None
    filament_diameter_mm: float | None = Field(None, ge=0, le=5)
    extruder: str | None = None
    bed_leveling: str | None = None
    bed_surface: str | None = None
    kinematics: str | None = None
    recommended_max_overhang_deg: int | None = Field(None, ge=10, le=89)
    recommended_max_bridge_mm: float | None = Field(None, gt=0, le=500)
    dimensional_tolerance_mm: float | None = Field(None, gt=0, le=2)
    notes: str | None = None
    sources: str | None = None


@router.post("/api/printers", status_code=201)
async def create_printer(payload: PrinterWriteRequest) -> dict[str, Any]:
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not data.get("name"):
        raise AppError("请先给这台打印机起个名字。", code="INTERNAL", hint="例如「我的 Ender-3」。")
    profile = registry().create(data)
    return _profile_payload(profile)


@router.put("/api/printers/{printer_id}")
async def update_printer(printer_id: str, payload: PrinterWriteRequest) -> dict[str, Any]:
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    try:
        profile = registry().update(printer_id, data)
    except KeyError:
        raise not_found("这台打印机配置") from None
    return _profile_payload(profile)


@router.delete("/api/printers/{printer_id}")
async def delete_printer(printer_id: str) -> dict[str, Any]:
    reg = registry()
    profile = reg.get(printer_id)
    if profile is None:
        raise not_found("这台打印机配置")
    was_overridden = bool(profile.builtin and profile.updated_at)
    was_selected = reg.selected_id() == printer_id
    if not reg.delete(printer_id):
        raise AppError(
            "内置机型不能删除。",
            code="INTERNAL",
            hint="你可以直接修改它的参数（会保存为你的自定义版本，之后可以「恢复默认」），"
            "或新建一台自己的打印机。",
        )
    # 两种语义要分清，否则用户以为「把内置机型删掉了」
    if was_overridden:
        message = "已恢复这台内置机型的出厂参数。"
    else:
        message = "已删除这台自定义打印机。"
    if was_selected:
        message += "它原本是当前使用的机型，已切回通用机型。"
    return {"ok": True, "message": message, "reverted": was_overridden, "switched": was_selected}


class PreviewRequest(BaseModel):
    """在保存之前先算一遍：这台机器上，某个尺寸的模型需要什么约束。"""

    # nozzle_mm 允许为 0：光固化机型没有喷嘴，用 0 表示（配合 extruder="resin"）。
    nozzle_mm: float = Field(0.4, ge=0, le=2.0)
    build_x: float = Field(220, gt=0, le=2000)
    build_y: float = Field(220, gt=0, le=2000)
    build_z: float = Field(250, gt=0, le=2000)
    material: str | None = None
    enclosure: bool = False
    max_bed_temp_c: int = 100
    max_hotend_temp_c: int = 260
    layer_min_mm: float = 0.08
    layer_max_mm: float = 0.32
    recommended_max_overhang_deg: int = 55
    recommended_max_bridge_mm: float = 30
    extruder: str = "direct"


@router.post("/api/printers/preview")
async def preview_constraints(payload: PreviewRequest) -> dict[str, Any]:
    """编辑机型时实时预览「这些参数意味着什么」——让用户看得懂每一项的影响。"""
    profile = PrinterProfile.from_dict(
        {
            "id": "preview",
            "name": "预览机型",
            **payload.model_dump(),
        },
        builtin=False,
    )
    constraints = resolve_process(profile, payload.material)
    return {"constraints": constraints.to_dict(), "checklist": constraints.summary_lines(), "notes": constraints.notes}


@router.post("/api/printers/reset")
async def reset_printers() -> dict[str, Any]:
    """清空所有自定义改动，恢复内置知识库原样。"""
    registry().reset_custom()
    store().update("printer", {"printer_id": DEFAULT_PRINTER_ID, "material": None})
    return {"ok": True, "message": f"已恢复内置机型库（共 {len(BUILTIN_PRINTERS)} 台）。"}
