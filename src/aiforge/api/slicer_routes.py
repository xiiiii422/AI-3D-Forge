"""切片软件接口：检测列表 + 手动指定主程序。

界面只需要两个调用：进入「送到切片软件」前拿一次列表；检测不到时点一次「手动指定」。
所以这里刻意只有两个端点 —— 默认项与手动路径列表都走 `PUT /api/settings`，
不另开接口（少一个入口就少一处会坏的地方）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter

from .. import slicers as slicers_mod
from ..config import store
from .errors import AppError

log = logging.getLogger(__name__)

router = APIRouter(tags=["slicers"])


@router.get("/api/slicers")
async def list_slicers(rescan: int = 0) -> dict[str, Any]:
    """已检测到的切片软件。`rescan=1` 强制重扫（用户刚装完软件时要用）。

    扫描要读注册表并列若干目录（实测本机约几十毫秒），放线程里跑，避免卡住事件循环。
    """
    if rescan:
        slicers_mod.invalidate_cache()
    return await asyncio.to_thread(slicers_mod.describe)


def _open_exe_dialog() -> tuple[str, str | None]:
    """弹原生「打开」对话框选切片软件主程序。返回 ("ok", path) / ("canceled", None) / ("nodialog", None)。

    与项目保存框同样先 pywebview、退 tkinter：原生窗口里没有系统的下载/选择面板，
    浏览器模式下再退回"让用户自己填路径"不现实，所以只如实返回 nodialog。
    """
    try:
        import webview

        window = webview.active_window()
        if window is not None:
            picked = window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("可执行文件 (*.exe)",),
            )
            if picked:
                return ("ok", picked[0] if isinstance(picked, (list, tuple)) else str(picked))
            return ("canceled", None)
    except Exception:  # noqa: BLE001 - 无 pywebview / 无桌面会话时降级
        pass

    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(title="选择切片软件主程序", filetypes=[("可执行文件", "*.exe")])
        root.destroy()
        return ("ok", str(path)) if path else ("canceled", None)
    except Exception:  # noqa: BLE001
        return ("nodialog", None)


@router.post("/api/slicers/pick")
async def pick_custom_path() -> dict[str, Any]:
    """自动检测漏网时，让用户自己指出主程序 exe，然后立刻重扫。"""
    status, path = await asyncio.to_thread(_open_exe_dialog)
    cfg = store()
    if status != "ok" or not path:
        return {"status": status, **await asyncio.to_thread(slicers_mod.describe)}
    from pathlib import Path

    picked = Path(path)
    if not picked.is_file():
        raise AppError("选中的文件不存在。", code="INTERNAL", hint="请重新选择切片软件的主程序（.exe）。")
    current = cfg.config.slicers.custom_paths
    if str(picked) not in current:
        cfg.update("slicers", {"custom_paths": [*current, str(picked)]})
        cfg.save()
    slicers_mod.invalidate_cache()
    log.info("用户手动指定切片软件主程序：%s", picked)
    return {"status": "added", "path": str(picked), **await asyncio.to_thread(slicers_mod.describe)}
