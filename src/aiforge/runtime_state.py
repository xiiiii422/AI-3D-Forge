"""AI 3D Forge —— 进程级运行状态（供退出、单实例等跨模块协作使用）。

刻意做成极小的模块，避免 server / api / launcher 之间互相 import 造成循环依赖。
"""

from __future__ import annotations

import os
import threading

_shutdown_requested = threading.Event()
_window = None
_server = None


def request_shutdown() -> None:
    _shutdown_requested.set()


def shutdown_requested() -> bool:
    return _shutdown_requested.is_set()


def force_exit(code: int = 0) -> None:
    """确保进程真的退出：pywebview / uvicorn 的线程都可能阻止正常退出。"""
    try:
        _close_window()
    finally:
        os._exit(code)  # noqa: SLF001 - 退出流程必须无条件生效


def register_window(window) -> None:
    global _window
    _window = window


def window():
    return _window


def _close_window() -> None:
    window = _window
    if window is None:
        return
    try:
        window.destroy()
    except Exception:  # noqa: BLE001 - 关闭失败也必须继续退出
        pass


def register_server(server) -> None:
    global _server
    _server = server


def server():
    return _server


def stop_server() -> None:
    if _server is not None:
        try:
            _server.should_exit = True
        except Exception:  # noqa: BLE001
            pass
