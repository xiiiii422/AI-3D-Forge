"""AI 3D Forge —— 单实例保护。

用户重复双击图标时，不应该启动第二个服务、第二个窗口，也不应该弹出「端口被占用」。
做法：在用户目录下对锁文件加独占锁；拿不到锁说明已有实例在运行，直接退出。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .paths import paths

log = logging.getLogger(__name__)

_handle = None
_lock_path: Path | None = None


def acquire() -> bool:
    """尝试获取单实例锁。返回 True 表示本进程是唯一实例。"""
    global _handle, _lock_path
    if os.name == "nt":
        try:
            import msvcrt
        except ImportError:  # pragma: no cover
            return True
        try:
            _lock_path = paths().data_dir / "app.lock"
            _lock_path.parent.mkdir(parents=True, exist_ok=True)
            _handle = open(_lock_path, "a+b")  # noqa: SIM115 - 需要长期持有句柄
            msvcrt.locking(_handle.fileno(), msvcrt.LK_NBLCK, 1)
            _handle.seek(0)
            _handle.truncate()
            _handle.write(str(os.getpid()).encode("ascii"))
            _handle.flush()
            return True
        except OSError:
            if _handle is not None:
                try:
                    _handle.close()
                except OSError:
                    pass
                _handle = None
            return False
        except Exception:  # noqa: BLE001 - 锁机制失败不应阻止软件启动
            log.debug("单实例锁不可用，继续启动", exc_info=True)
            return True

    # 非 Windows：用 pid 文件做尽力而为的判断
    try:
        _lock_path = paths().data_dir / "app.lock"
        if _lock_path.exists():
            raw = _lock_path.read_text(encoding="ascii").strip()
            if raw.isdigit() and _pid_alive(int(raw)):
                return False
        _lock_path.parent.mkdir(parents=True, exist_ok=True)
        _lock_path.write_text(str(os.getpid()), encoding="ascii")
        return True
    except OSError:
        return True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def release() -> None:
    global _handle, _lock_path
    if _handle is not None:
        try:
            import msvcrt

            _handle.seek(0)
            msvcrt.locking(_handle.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:  # noqa: BLE001
            pass
        try:
            _handle.close()
        except OSError:
            pass
        _handle = None
    if _lock_path is not None and os.name != "nt":
        try:
            _lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def running_pid() -> int | None:
    """读取锁文件里记录的 PID（用于「已有实例」提示）。"""
    try:
        path = paths().data_dir / "app.lock"
        if not path.is_file():
            return None
        raw = path.read_text(encoding="ascii").strip()
        return int(raw) if raw.isdigit() and raw else None
    except (OSError, ValueError):
        return None
