"""项目接口：列表、详情、重命名、删除、SCAD/STL 下载、缩略图。"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Response
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from .. import compiler
from .. import envcheck
from .. import projects as projects_mod
from .errors import AppError, not_found

router = APIRouter(tags=["projects"])


def attachment_header(filename: str) -> str:
    """构造 Content-Disposition。中文文件名用 RFC 5987 编码，避免下载后名字变乱码。

    两处必须小心：

    1. **ASCII 兜底名不能简单「删掉非 ASCII 字符」**。
       中文名的非 ASCII 部分删完之后，剩下的**不是空串而是扩展名**：
       `桌面手机支架.stl` → `.stl`，真值判断通过，于是
       `... or "model"` 这个兜底永远不会触发 —— 只认 `filename=` 的老客户端
       会存下一个**名叫 `.stl` 的文件**（Windows/macOS 上是隐藏文件，
       用户会以为下载失败）。所以主名与扩展名要分开处理，主名为空时才换成 model。
       实测：修复前 5 个用例里 4 个的兜底名没有主名（还有 `" v2.scad"`、`"3D .stl"`
       这种带前导空格/悬空空格的）。

    2. **函数产物直接进 HTTP 头**，所以 ASCII 兜底只保留安全字符集。
       文件名里若带引号或换行会破坏头结构（当前调用方都用 `safe_name` 清过，
       这里再兜一层，避免以后有人直接拿项目名来拼）。
    """
    stem, dot, suffix = filename.rpartition(".")
    if not dot:
        stem, suffix = filename, ""
    ascii_stem = re.sub(
        r"[^A-Za-z0-9._ -]", "", stem.encode("ascii", "ignore").decode("ascii")
    ).strip(" .")
    ascii_suffix = re.sub(r"[^A-Za-z0-9]", "", suffix.encode("ascii", "ignore").decode("ascii"))
    fallback = f"{ascii_stem or 'model'}{'.' + ascii_suffix if ascii_suffix else ''}"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


@router.get("/api/projects")
async def list_projects() -> list[dict[str, Any]]:
    return [project.to_summary() for project in projects_mod.store().list()]


class CreateProjectRequest(BaseModel):
    name: str = "未命名模型"


@router.post("/api/projects")
async def create_project(payload: CreateProjectRequest) -> dict[str, Any]:
    project = projects_mod.store().create(payload.name)
    return project.to_summary()


@router.get("/api/projects/{project_id}")
async def get_project(project_id: str) -> dict[str, Any]:
    project = projects_mod.store().get(project_id)
    if project is None:
        raise not_found("这个项目")
    return project.to_detail()


class RenameRequest(BaseModel):
    name: str


@router.patch("/api/projects/{project_id}")
async def rename_project(project_id: str, payload: RenameRequest) -> dict[str, Any]:
    store = projects_mod.store()
    project = store.get(project_id)
    if project is None:
        raise not_found("这个项目")
    if not payload.name.strip():
        raise AppError("名称不能为空。", code="INTERNAL", hint="请输入一个名字。")
    return store.rename(project, payload.name).to_summary()


@router.delete("/api/projects/{project_id}")
async def delete_project(project_id: str) -> dict[str, Any]:
    store = projects_mod.store()
    if not store.delete(project_id):
        raise not_found("这个项目")
    return {"ok": True, "message": "已移入项目文件夹中的 .deleted 目录，可手动恢复。"}


@router.get("/api/projects/{project_id}/stl")
async def download_stl(project_id: str) -> FileResponse:
    store = projects_mod.store()
    project = store.get(project_id)
    if project is None:
        raise not_found("这个项目")
    if not project.stl_path.is_file():
        raise AppError(
            "这个项目还没有生成 STL 文件。",
            code="OPENSCAD_FAILED",
            hint="请先在对话中描述模型并等待生成完成。",
            status_code=409,
        )
    filename = store.download_name(project, "stl")
    return FileResponse(
        project.stl_path,
        media_type="model/stl",
        filename=filename,
        headers={"Cache-Control": "no-store", "Content-Disposition": attachment_header(filename)},
    )


@router.get("/api/projects/{project_id}/stl/raw")
async def raw_stl(project_id: str) -> FileResponse:
    """供 Three.js 直接预览使用（inline，不触发下载）。"""
    store = projects_mod.store()
    project = store.get(project_id)
    if project is None:
        raise not_found("这个项目")
    if not project.stl_path.is_file():
        raise AppError("这个项目还没有生成 STL 文件。", code="OPENSCAD_FAILED", status_code=409)
    return FileResponse(
        project.stl_path,
        media_type="model/stl",
        headers={"Cache-Control": "no-store", "Content-Disposition": "inline"},
    )


@router.get("/api/projects/{project_id}/scad")
async def download_scad(project_id: str) -> PlainTextResponse:
    store = projects_mod.store()
    project = store.get(project_id)
    if project is None:
        raise not_found("这个项目")
    source = project.read_scad()
    if not source:
        raise AppError("这个项目还没有模型代码。", code="INTERNAL", status_code=409)
    filename = store.download_name(project, "scad")
    return PlainTextResponse(
        source,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": attachment_header(filename), "Cache-Control": "no-store"},
    )


@router.get("/api/projects/{project_id}/thumbnail")
async def thumbnail(project_id: str) -> Response:
    store = projects_mod.store()
    project = store.get(project_id)
    if project is None:
        raise not_found("这个项目")

    if project.thumbnail_path.is_file():
        try:
            svg = project.thumbnail_path.read_text(encoding="utf-8")
            return Response(content=svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})
        except OSError:
            pass

    # 没有缓存缩略图时即时生成一份（依据已有测量数据，不再调用 OpenSCAD）
    from ..mesh import MeshMetrics

    metrics: MeshMetrics | None = None
    if project.metrics:
        raw = project.metrics
        bbox = raw.get("bbox") or {}
        metrics = MeshMetrics(
            triangles=int(raw.get("triangles") or 0),
            bbox=(float(bbox.get("x") or 0), float(bbox.get("y") or 0), float(bbox.get("z") or 0)),
        )
    svg = compiler.make_thumbnail_svg(metrics, project.name)
    return Response(content=svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@router.get("/api/projects/{project_id}/metrics")
async def metrics(project_id: str) -> dict[str, Any]:
    store = projects_mod.store()
    project = store.get(project_id)
    if project is None:
        raise not_found("这个项目")
    if project.stl_path.is_file():
        # 文件已存在时以真实文件为准，避免展示过期数据
        try:
            fresh = compiler.analyze_stl(project.stl_path)
            return fresh.to_dict()
        except Exception:  # noqa: BLE001 - 分析失败时退回缓存值
            pass
    return project.metrics or {}


@router.post("/api/projects/{project_id}/open-folder")
async def open_project_folder(project_id: str) -> dict[str, Any]:
    import os

    project = projects_mod.store().get(project_id)
    if project is None:
        raise not_found("这个项目")
    try:
        os.startfile(str(project.dir))  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        raise AppError(
            f"无法打开文件夹：{exc}",
            code="INTERNAL",
            hint=f"你可以手动打开：{project.dir}",
        ) from exc
    return {"ok": True}


@router.post("/api/projects/{project_id}/rescan")
async def rescan(project_id: str) -> dict[str, Any]:
    """重新分析磁盘上的 STL（用于用户手动替换了模型文件的情况）。"""
    store = projects_mod.store()
    project = store.get(project_id)
    if project is None:
        raise not_found("这个项目")
    if not project.stl_path.is_file():
        raise AppError("这个项目还没有 STL 文件。", code="OPENSCAD_FAILED", status_code=409)
    try:
        fresh = compiler.analyze_stl(project.stl_path)
    except Exception as exc:  # noqa: BLE001
        raise AppError(f"模型分析失败：{exc}", code="OPENSCAD_FAILED") from exc
    project.metrics = fresh.to_dict()
    store.save(project)
    envcheck.invalidate_cache()
    return fresh.to_dict()
