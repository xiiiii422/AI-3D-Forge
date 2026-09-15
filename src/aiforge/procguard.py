"""子进程守护：应用退出时，自己起的子进程**不能**变成孤儿继续跑。

为什么必须做（实测反馈）：编译模型会起 `openscad.exe` 子进程。用户在编译期间
关掉窗口、或从任务管理器结束进程时，Python 进程死了，**子进程不会跟着死**：
软件关了，OpenSCAD 还在后台把一个复杂模型算完，占满一个核，有时还在项目目录里
留下一个半截 STL —— 下次打开软件，用户会看到一个"凭空出现"的模型。

更糟的是本项目的退出路径：`runtime_state.force_exit()` 走的是 `os._exit()`，
它**故意跳过** atexit / finally（否则 pywebview/uvicorn 的线程会拖住退出）。
所以任何"在退出时清理"的写法都靠不住。

Windows 上的正解是**作业对象（Job Object）**：
把进程放进一个设置了 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的作业里，
作业的最后一个句柄一关（正常退出、崩溃、被强杀都算），系统会连带杀掉其中所有进程。
这是内核保证，不依赖我们有没有机会跑清理代码。

策略（三层，逐层兜底）：
  1. **作业对象**：把当前进程加入作业，之后派生的子进程自动继承 —— 覆盖一切子进程，
     包括我们没经手过的（安装器、winget…）；
  2. **登记表 + 退出时回收**：能优雅退出时先杀子进程再退出，避免半截文件；
  3. **启动清扫**：把派生的 PID 记到磁盘，下次启动时只杀"确实是我们装的 OpenSCAD"
     的残留进程（按可执行文件路径判断），不碰用户自己开的 OpenSCAD。

非 Windows 上退化为第 2、3 层。
"""

from __future__ import annotations

import atexit
import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

# --- Windows 常量 -----------------------------------------------------------
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_job_handle: int | None = None
_job_ready = False
_lock = threading.Lock()
_live: dict[int, subprocess.Popen] = {}
_record_path: Path | None = None


# --------------------------------------------------------------------------
# Windows 作业对象
# --------------------------------------------------------------------------
class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    return kernel32


def install(record_path: Path | None = None) -> bool:
    """创建作业对象并把**当前进程**纳入其中，返回是否成功。

    把当前进程加入作业后，我们派生的一切子进程自动继承该作业 ——
    于是"应用一死，子进程全灭"由内核保证。
    """
    global _job_handle, _job_ready, _record_path
    _record_path = record_path
    atexit.register(terminate_all)

    if os.name != "nt":
        return False

    with _lock:
        if _job_handle is not None:
            return _job_ready
        try:
            kernel32 = _kernel32()
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                log.warning("创建作业对象失败（错误码 %s）", ctypes.get_last_error())
                return False
            info = _ExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = kernel32.SetInformationJobObject(
                handle,
                _JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                log.warning("设置 KillOnJobClose 失败（错误码 %s）", ctypes.get_last_error())
                kernel32.CloseHandle(handle)
                return False
            if not kernel32.AssignProcessToJobObject(handle, kernel32.GetCurrentProcess()):
                # 常见于：当前进程已在别的作业里且不允许嵌套（旧系统）。
                # 不是致命问题 —— 还有登记表与启动清扫两层兜底。
                log.info("把当前进程加入作业对象失败（错误码 %s），改用登记表兜底", ctypes.get_last_error())
                kernel32.CloseHandle(handle)
                return False
            _job_handle = handle  # 句柄必须保留到进程结束，关掉就等于杀掉自己与子进程
            _job_ready = True
            log.info("子进程守护已启用：应用退出时系统会连带结束所有子进程")
            return True
        except Exception as exc:  # noqa: BLE001 - 守护失败不该影响启动
            log.warning("子进程守护初始化失败：%s", exc)
            return False


def job_object_active() -> bool:
    """作业对象是否已经生效（测试与诊断用）。"""
    return _job_ready


def register(proc: subprocess.Popen) -> None:
    """登记一个子进程：退出时先杀它，避免留下半截产物。"""
    with _lock:
        _live[proc.pid] = proc
        pids = _read_records()
        pids.add(proc.pid)
        _write_records(pids)


def unregister(proc: subprocess.Popen) -> None:
    with _lock:
        _live.pop(proc.pid, None)


def run(cmd: list[str], *, timeout: float | None = None, **kwargs) -> subprocess.CompletedProcess:
    """`subprocess.run` 的替代品：登记子进程，超时按老规矩杀掉它。"""
    proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603 - 调用方自己给完整命令
    register(proc)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill(proc)
        out, _ = proc.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout, output=out) from exc
    finally:
        unregister(proc)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, None)


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.kill()
    except OSError as exc:
        log.info("结束子进程 %s 失败：%s", proc.pid, exc)


def terminate_all() -> int:
    """结束所有登记在册的子进程，返回结束的个数。"""
    with _lock:
        procs = list(_live.values())
    killed = 0
    for proc in procs:
        if proc.poll() is None:
            _kill(proc)
            killed += 1
    if killed:
        log.info("退出前结束了 %d 个子进程", killed)
    with _lock:
        _write_records(set())
    return killed


# --------------------------------------------------------------------------
# 崩溃后的残留清扫（PID 记录在磁盘上）
# --------------------------------------------------------------------------
def _read_records() -> set[int]:
    if _record_path is None or not _record_path.is_file():
        return set()
    try:
        data = json.loads(_record_path.read_text(encoding="utf-8"))
        return {int(value) for value in data.get("pids", [])}
    except (OSError, ValueError, TypeError):
        return set()


def _write_records(pids: set[int]) -> None:
    if _record_path is None:
        return
    try:
        _record_path.parent.mkdir(parents=True, exist_ok=True)
        _record_path.write_text(
            json.dumps({"pids": sorted(pids), "updated_at": time.time()}),
            encoding="utf-8",
        )
    except OSError as exc:
        log.debug("写入子进程记录失败：%s", exc)


def _image_path(pid: int) -> str | None:
    """取某个 PID 的可执行文件全路径（用于判断"这是我们装的 OpenSCAD 吗"）。"""
    if os.name != "nt":
        return None
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = ctypes.c_uint32(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(handle)


def sweep_orphans(expected_dir: Path | None = None) -> list[int]:
    """清理上次运行残留的、由本程序安装的 OpenSCAD 进程。

    只杀**可执行文件位于我们管理的安装目录下**的进程 ——
    用户自己装的 OpenSCAD（或用户正在用的其它 OpenSCAD 窗口）一律不碰。
    """
    pids = _read_records()
    if not pids:
        return []
    killed: list[int] = []
    for pid in pids:
        image = _image_path(pid)
        if not image:
            continue
        if expected_dir is not None:
            try:
                inside = str(Path(image).resolve()).lower().startswith(
                    str(expected_dir.resolve()).lower()
                )
            except OSError:
                inside = False
            if not inside:
                continue
        elif "openscad" not in Path(image).name.lower():
            continue
        if _terminate_pid(pid):
            killed.append(pid)
    _write_records(set())
    if killed:
        log.info("清理了上次运行残留的 %d 个 OpenSCAD 进程：%s", len(killed), killed)
    return killed


def _terminate_pid(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 15)  # noqa: S606 - 非 Windows 退化为 SIGTERM
            return True
        except OSError:
            return False
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def reset_for_tests() -> None:
    """仅供测试：丢弃登记表（不影响作业对象）。"""
    with _lock:
        _live.clear()


__all__ = [
    "install",
    "job_object_active",
    "register",
    "unregister",
    "run",
    "sweep_orphans",
    "terminate_all",
    "reset_for_tests",
]


if __name__ == "__main__":  # pragma: no cover - 手工排查用
    print("job object active:", install())
    print("python:", sys.executable)
