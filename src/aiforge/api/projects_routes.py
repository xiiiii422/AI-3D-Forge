"""项目接口：列表、详情、重命名、删除、SCAD/STL 下载、缩略图、回退历史版本。"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Response
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .. import compiler
from .. import envcheck
from .. import jobs as jobs_mod
from .. import projects as projects_mod
from .. import scad as scad_src
from ..projects import HistoryEntry, now_iso
from .errors import AppError, not_found

log = logging.getLogger(__name__)

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


# --------------------------------------------------------------------------
# 回收站：删掉的项目住在 `Projects/.deleted` 里，在这里列出 / 恢复 / 彻底删除
#
# 这三条**必须排在 `/api/projects/{project_id}` 前面**：FastAPI 按注册顺序匹配，
# 放到后面就会被 `{project_id}` 吞掉（变成「查一个叫 trash 的项目」然后 404），
# 而 404 在界面上看起来完全就是「回收站是空的」。自检里有一条专门盯这个顺序。
# --------------------------------------------------------------------------
@router.get("/api/projects/trash")
async def list_trash() -> dict[str, Any]:
    store = projects_mod.store()
    items = store.trash_list()
    return {"items": items, "count": len(items), "dir": str(store.trash_dir)}


class TrashPurgeRequest(BaseModel):
    confirm: bool = False


@router.post("/api/projects/trash/{entry}/restore")
async def restore_from_trash(entry: str) -> dict[str, Any]:
    project = projects_mod.store().trash_restore(entry)
    if project is None:
        raise not_found("回收站里的这个条目")
    return {"ok": True, "project": project.to_summary(), "message": f"已恢复「{project.name}」。"}


@router.post("/api/projects/trash/{entry}/purge")
async def purge_trash_entry(entry: str, payload: TrashPurgeRequest) -> dict[str, Any]:
    """彻底删除回收站里的一条。**不可恢复**，所以必须带 `confirm: true`。

    不拿「接口被调了」当「用户真想删」：没有显式确认就拒，
    这样即使以后误调这个端点也不会造成数据丢失。
    """
    if not payload.confirm:
        raise AppError(
            "彻底删除后无法恢复，需要明确确认。",
            code="INTERNAL",
            hint="界面上需要再点一次「确认彻底删除」。",
            status_code=400,
        )
    if not projects_mod.store().trash_purge(entry):
        raise not_found("回收站里的这个条目")
    return {"ok": True, "message": "已彻底删除。"}


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
    return {"ok": True, "message": "已移入回收站（项目文件夹的 .deleted 目录），可在「设置 → 回收站」恢复。"}


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


class SaveRequest(BaseModel):
    kind: str = "stl"


def _save_file_dialog(default_name: str, file_types: list[tuple[str, str]]) -> tuple[str, str | None]:
    """弹出原生「另存为」对话框，让用户选输出目录与文件名。

    为什么需要：WebView2 默认没有下载处理程序，前端 `<a download>` 点下去会
    「无响应」；而原生窗口里弹一个系统保存框最符合用户对「下载」的预期。
    优先 pywebview，不可用退回 tkinter；两者都不可用（无桌面会话）返回 nodialog，
    由前端退回浏览器下载。返回 ("ok", path) / ("canceled", None) / ("nodialog", None)。
    """
    try:
        import webview

        window = webview.active_window()
        if window is not None:
            wt_types = tuple(f"{label} ({glob})" for label, glob in file_types)
            picked = window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=default_name, file_types=wt_types
            )
            if picked:
                path = picked[0] if isinstance(picked, (list, tuple)) else str(picked)
                return ("ok", path)
            return ("canceled", None)
    except Exception:  # noqa: BLE001 - 无 pywebview / 无桌面会话时降级
        pass

    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.asksaveasfilename(
            title="选择保存位置",
            initialfile=default_name,
            filetypes=[(label, glob) for label, glob in file_types],
            defaultextension="",
        )
        root.destroy()
        return ("ok", str(path)) if path else ("canceled", None)
    except Exception:  # noqa: BLE001 - 无桌面会话（如纯浏览器模式）
        return ("nodialog", None)


@router.post("/api/projects/{project_id}/save")
async def save_to_disk(project_id: str, payload: SaveRequest) -> dict[str, Any]:
    """把当前项目的 STL / SCAD / 3MF 写到用户选定的路径（弹原生保存框）。

    返回：{status: saved|canceled|no-dialog, path?}。no-dialog 时前端自行退回浏览器下载。
    """
    store = projects_mod.store()
    project = store.get(project_id)
    if project is None:
        raise not_found("这个项目")
    kind = payload.kind if payload.kind in ("stl", "scad", "3mf") else "stl"
    if kind == "stl":
        if not project.stl_path.is_file():
            raise AppError("这个项目还没有生成 STL 文件。", code="OPENSCAD_FAILED", status_code=409)
        data = project.stl_path.read_bytes()
        default_name = store.download_name(project, "stl")
        file_types = [("STL 模型", "*.stl"), ("所有文件", "*.*")]
    elif kind == "3mf":
        # 直接复用下载路由（包含“旧项目没有 3MF 就从 STL 现场生成”的逻辑）
        response = await download_3mf(project_id)
        data = Path(str(response.path)).read_bytes()
        default_name = store.download_name(project, "3mf")
        file_types = [("3MF 打印文件", "*.3mf"), ("所有文件", "*.*")]
    else:
        source = project.read_scad()
        if not source:
            raise AppError("这个项目还没有模型代码。", code="INTERNAL", status_code=409)
        data = source.encode("utf-8")
        default_name = store.download_name(project, "scad")
        file_types = [("OpenSCAD", "*.scad"), ("所有文件", "*.*")]

    status, path = await asyncio.to_thread(_save_file_dialog, default_name, file_types)
    if status == "nodialog":
        return {"status": "no-dialog"}
    if status != "ok" or not path:
        return {"status": "canceled"}
    try:
        Path(path).write_bytes(data)
    except OSError as exc:
        raise AppError(
            f"写入文件失败：{exc.strerror or exc}",
            code="DISK_FULL" if getattr(exc, "errno", None) == 28 else "INTERNAL",
            hint="请换一个有写入权限、磁盘空间充足的目录。",
        ) from exc
    return {"status": "saved", "path": path}


def ensure_three_mf(project) -> Path:
    """保证项目有一份 3MF，返回它的路径（旧的只有 STL 时现场从 STL 转一个）。

    下载 3MF 与「送到切片软件」都必须先走这一步：两条路径对元数据的要求一样，
    抄两份迟早会不一样（一份带机型、一份不带）。
    """
    if project.three_mf_path.is_file():
        return project.three_mf_path
    if not project.stl_path.is_file():
        raise AppError(
            "这个项目还没有生成 3D 模型文件。",
            code="OPENSCAD_FAILED",
            hint="请先在对话中描述模型并等待生成完成。",
            status_code=409,
        )
    from ..threemf import write_3mf_from_stl

    metrics = project.metrics or {}
    bbox = metrics.get("bbox") or {}
    printer = project.printer or {}
    try:
        write_3mf_from_stl(
            project.stl_path,
            project.three_mf_path,
            title=project.name,
            metadata={
                "Title": project.name,
                "Application": "AI 3D Forge",
                "Description": project.summary or "",
                "Printer": str(printer.get("name") or ""),
                "Material": str(printer.get("material") or ""),
                "DesignSizeMM": (
                    f"{bbox.get('x', 0):.1f}x{bbox.get('y', 0):.1f}x{bbox.get('z', 0):.1f}"
                    if isinstance(bbox, dict)
                    else ""
                ),
                "Revision": str(project.revision),
            },
        )
    except (OSError, ValueError) as exc:
        raise AppError(
            f"生成 3MF 失败：{exc}",
            hint="可以改用 STL（切片软件也支持），或重新生成一次模型。",
            code="INTERNAL",
        ) from exc
    return project.three_mf_path


@router.get("/api/projects/{project_id}/3mf")
async def download_3mf(project_id: str) -> FileResponse:
    """下载 3MF（切片软件用：声明 mm 单位，并带机型/材料元数据）。

    旧项目只有 STL，`ensure_three_mf` 会按需从已交付的 STL 现场转一个
    （几何与 STL 完全一致），结果存下来，下次直接取。
    """
    store = projects_mod.store()
    project = store.get(project_id)
    if project is None:
        raise not_found("这个项目")
    await asyncio.to_thread(ensure_three_mf, project)
    filename = store.download_name(project, "3mf")
    return FileResponse(
        project.three_mf_path,
        media_type="model/3mf",
        filename=filename,
        headers={"Cache-Control": "no-store", "Content-Disposition": attachment_header(filename)},
    )


class SendToSlicerRequest(BaseModel):
    slicer: str | None = Field(None, description="切片软件 id；不传就用设置里的默认或检测到的第一个")


@router.post("/api/projects/{project_id}/send-to-slicer")
async def send_to_slicer(project_id: str, payload: SendToSlicerRequest) -> dict[str, Any]:
    """把当前模型交给切片软件打开（对标 Meshy 的"一键送切片器"）。

    只认 id 不认路径：能启动的程序必须是本机检测出来的那几个 exe，
    请求里塞任何路径都无效（避免变成一个本机任意程序执行接口）。

    一律送 3MF（带毫米单位与机型元数据，切片软件不需要再猜尺寸）；
    只有 3MF 确实写不出来时才退回 STL。
    """
    from .. import slicers as slicers_mod

    store = projects_mod.store()
    project = store.get(project_id)
    if project is None:
        raise not_found("这个项目")

    kind = "3mf"
    if not (project.three_mf_path.is_file() or project.stl_path.is_file()):
        raise AppError(
            "这个项目还没有生成 3D 模型文件。",
            code="OPENSCAD_FAILED",
            hint="请先在对话中描述模型并等待生成完成。",
            status_code=409,
        )
    try:
        await asyncio.to_thread(ensure_three_mf, project)
    except AppError:
        kind = "stl"  # 3MF 转不出来：送 STL（切片软件也吃），上面已保证 STL 存在

    try:
        result = await asyncio.to_thread(slicers_mod.send, project, payload.slicer, kind)
    except LookupError as exc:
        raise AppError(
            str(exc),
            code="SLICER_NOT_FOUND",
            hint="在「设置 → 切片软件」里点「手动指定主程序」也能接上。",
            status_code=409,
        ) from exc
    except FileNotFoundError as exc:
        raise AppError(
            str(exc),
            code="SLICER_NOT_FOUND",
            hint="请重新检测一次，或先自己打开切片软件再把文件拖进去。",
            status_code=409,
        ) from exc
    log.info("送到切片软件：%s -> %s（%s）", project.name, result.get("slicer_name"), result.get("status"))
    return result


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
            data = fresh.to_dict()
            # 结果卡要显示文件大小/材料；MeshMetrics 不含这两项，从文件与缓存补，
            # 否则重开项目时「文件大小」显示 0 B（用户实测反馈）。
            data["size_bytes"] = project.stl_path.stat().st_size
            cached = project.metrics or {}
            if isinstance(cached, dict):
                data.setdefault("material", cached.get("material", ""))
                data.setdefault("material_density", cached.get("material_density", 0))
                # 需求核对清单是作业完成时按当时的规格与机型算出来的，重新分析 STL 不会产出它。
                # 不从缓存带回来，重开项目就没有核对表（结果卡缺一整块）。
                if cached.get("requirement_tests"):
                    data["requirement_tests"] = cached["requirement_tests"]
            return data
        except Exception:  # noqa: BLE001 - 分析失败时退回缓存值
            pass
    return project.metrics or {}


class RestoreRequest(BaseModel):
    revision: int


@router.post("/api/projects/{project_id}/restore")
async def restore_revision(project_id: str, payload: RestoreRequest) -> dict[str, Any]:
    """把项目回退到某个历史版本（用户文档承诺过「随时可以回到之前的版本」）。

    为什么这次才补上（实测发现的功能缺口）：项目一直在**归档**历史版本
    （`revisions/rNNN/` + `history.jsonl`），用户说明书也写着"随时可以回到之前的版本"，
    但**没有任何恢复入口** —— 归档了却回不去，属于"看起来完成、实际没工作"。

    语义与安全约束：
      * 回退本身会产生**新的一版**（revision+1），历史只追加不重写 ——
        用户回退错了还能再回退回来；
      * 回退前先把当前版本归档（复用作业启动时的同一路径），当前成果绝不丢；
      * 归档里必须**同时**有 .scad 与 .stl，缺一不可（否则回退出一个空项目）；
      * 回退后重新实测指标，避免界面继续显示旧数字。
    """
    store = projects_mod.store()
    project = store.get(project_id)
    if project is None:
        raise not_found("这个项目")

    wanted = int(payload.revision)
    source_dir = project.revision_dir(wanted)
    archived_scad = source_dir / "model.scad"
    if not archived_scad.is_file():
        raise AppError(
            f"找不到第 {wanted} 版的历史文件。",
            code="NOT_FOUND",
            hint="可能这一版没有保留 STL/源码（设置里可以打开「保留历史 STL」）。",
            status_code=404,
        )
    archived_stl = source_dir / "model.stl"
    if not archived_stl.is_file():
        raise AppError(
            f"第 {wanted} 版没有保留 STL，无法回退。",
            code="NOT_FOUND",
            hint="在「设置 → 文件与存储」里打开「保留历史 STL」后，之后的版本都会带上 STL。",
            status_code=409,
        )

    # 先保住当前这一版：回退不该让用户丢掉手上的模型
    if project.revision > 0 and not project.revision_dir(project.revision).exists():
        try:
            store.archive_revision(project, project.revision, keep_stl=True)
        except OSError as exc:
            log.warning("回退前归档当前版本失败：%s", exc)

    try:
        scad_text = archived_scad.read_text(encoding="utf-8")
        stl_bytes = archived_stl.read_bytes()
    except OSError as exc:
        raise AppError(f"读取历史版本失败：{exc}", code="INTERNAL") from exc

    new_revision = project.revision + 1
    projects_mod.atomic_write_text(project.scad_path, scad_text)
    project.stl_path.write_bytes(stl_bytes)

    try:
        metrics = compiler.analyze_stl(project.stl_path)
        project.metrics = metrics.to_dict()
    except Exception as exc:  # noqa: BLE001 - 指标测不出来也要让回退生效
        log.warning("回退后重新测量失败：%s", exc)

    project.revision = new_revision
    project.updated_at = now_iso()
    project.last_status = "ok"
    project.params = scad_src.extract_top_level_numbers(scad_text)
    project.param_meta = jobs_mod.build_param_meta(scad_text, project.params)
    store.save(project)
    store.append_history(
        project,
        HistoryEntry(
            revision=new_revision,
            created_at=now_iso(),
            instruction=f"回退到第 {wanted} 版",
            summary=project.summary or "",
            name=project.name,
            stl=True,
            metrics=project.metrics,
            params=project.params,
        ),
    )
    try:
        store.export_copy(project)
    except OSError as exc:
        log.debug("回退后导出副本失败：%s", exc)
    try:
        svg = compiler.make_thumbnail_svg(
            compiler.analyze_stl(project.stl_path), project.name
        )
        projects_mod.atomic_write_text(project.thumbnail_path, svg)
    except Exception as exc:  # noqa: BLE001 - 缩略图失败不影响回退
        log.debug("回退后缩略图失败：%s", exc)
    envcheck.invalidate_cache()
    log.info("项目 %s 回退到第 %d 版（新版本号 %d）", project.id, wanted, new_revision)
    return {
        "project_id": project.id,
        "restored_from": wanted,
        "revision": new_revision,
        "metrics": project.metrics,
    }
