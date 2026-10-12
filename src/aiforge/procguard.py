"""子进程守护：应用退出时，自己起的子进程**不能**变成孤儿继续跑。

为什么必须做（实测反馈）：编译模型会起 `openscad.exe` 子进程。用户在编译期间
关掉窗口、或从任务管理器结束进程时，Python 进程死了，**子进程不会跟着死**：
软件关了，OpenSCAD 还在后台把一个复杂模型算完，占满一个核，有时还在项目目录里
留下一个半截 STL —— 下次打开软件，用户会看到一个"凭空出现"的模型。

更糟的是本项目的退出路径：`runtime_state.force_exit()` 走的是 `os._exit()`，
它**故意跳过** atexit / finally（否则 pywebview/uvicorn 的线程会拖住退出）。
所以任何"在退出时清理"的写法都靠不住。

Windows 上的正解是**作业对象（Job Object）**：
建一个设置了 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的作业，把**我们派生的工人进程**放进去，
作业的最后一个句柄一关（正常退出、崩溃、被强杀都算），系统会连带杀掉其中所有进程。
这是内核保证，不依赖我们有没有机会跑清理代码。

策略（三层，逐层兜底）：
  1. **作业对象**：只把工人（OpenSCAD、安装器…）逐个纳入作业；工人自己派生的子进程
     自动继承这个作业，所以我们没经手过的孙进程也在内；
  2. **登记表 + 退出时回收**：能优雅退出时先杀子进程再退出，避免半截文件；
  3. **启动清扫**：把派生的 PID 记到磁盘，下次启动时只杀"确实是我们装的 OpenSCAD"
     的残留进程（按可执行文件路径判断），不碰用户自己开的 OpenSCAD。

非 Windows 上退化为第 2、3 层。

**为什么不把当前进程自己放进作业**（旧做法，已被实测推翻）：放进去之后我们启动的一切
子进程都继承它 —— 包括用户自己的**切片软件**。想靠 `CREATE_BREAKAWAY_FROM_JOB` 逃出去
行不通：本机实测（开发工具链已经把我们关在一个祖先作业里）带上了这个标志子进程仍然
“在作业里”，并且我们一硬退出它就跟着没了 —— 而接口当时报的是 `detached: true`。
把用户的程序和文档当成我们的工人带走是错的，所以改成“作业只装工人”，
并且 `spawn_detached()` 的回报改成拿内核问一句，而不是自己假定。
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
# 以前这里还有一对 _JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK / _CREATE_BREAKAWAY_FROM_JOB，
# 用来让切片软件从作业里“逃逸”。实测它并不成立（见模块开头），现在改成
# 根本不让用户的程序进入作业，所以不再需要脱离能力；保留在这里反而是一个错东西。
_JobObjectExtendedLimitInformation = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TH32CS_SNAPPROCESS = 0x00000002
_STILL_ACTIVE = 259  # GetExitCodeProcess 还没退出时会返回这个值


class _ProcessEntry32(ctypes.Structure):
    """PROCESSENTRY32W（Toolhelp 进程快照）。字段顺序与 Windows SDK 一致，不能改。"""

    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]

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
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    kernel32.Process32FirstW.restype = ctypes.c_int
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry32)]
    kernel32.Process32NextW.restype = ctypes.c_int
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry32)]
    return kernel32


def install(record_path: Path | None = None) -> bool:
    """建一个"最后一个句柄关掉就杀光里面进程"的作业，返回是否成功。

    注意这里**不**把当前进程放进去（旧做法放过，后来被实测推翻）：放了之后
    我们启动的子进程会自动继承，连用户的切片软件都被算成工人。现在改由
    `register()` / `Popen()` 把工人逐个纳入，作业只装它该装的东西。
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
            _job_handle = handle  # 句柄必须保留到进程结束，关掉就等于杀掉作业里全部工人
            _job_ready = True
            log.info("子进程守护已启用：应用退出时系统会连带结束作业里的工人进程")
            return True
        except Exception as exc:  # noqa: BLE001 - 守护失败不该影响启动
            log.warning("子进程守护初始化失败：%s", exc)
            return False


def in_job(pid: int, job_handle: int | None = None) -> bool | None:
    """这个进程在不在作业里；给了 `job_handle` 就只问“是不是这个作业”。

    问不出来（进程已退出、非 Windows、没有这个 API）返回 None —— 调用方得把
    “不知道”当不知道，不要当成“没问题”。
    """
    if os.name != "nt":  # pragma: no cover - 非 Windows 上没有作业对象这个概念
        return None
    kernel32 = _kernel32()
    # 头文件里 IsProcessInJob 是个宏，实际导出的符号名因 Windows 版本而异
    query = getattr(kernel32, "IsProcessInJob", None) or getattr(kernel32, "K32IsProcessInJob", None)
    if query is None:
        return None
    query.restype = ctypes.c_int
    query.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    handle = _open_process(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
    if not handle:
        return None
    try:
        result = ctypes.c_int(-1)
        if not query(handle, job_handle, ctypes.byref(result)):
            return None
        return bool(result.value)
    finally:
        kernel32.CloseHandle(handle)


def _open_process(pid: int, access: int) -> int | None:
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(access, False, pid)
    return handle or None


def process_alive(pid: int) -> bool:
    """这个 pid 现在还在跑吗 —— **只问，绝不动它**。

    Windows 上不能用 `os.kill(pid, 0)` 代劳。那里的 signal 0 就是 `signal.CTRL_C_EVENT`
    （本机实测 `signal.CTRL_C_EVENT == 0`），所以它不是“探测”，而是**往目标所在的
    整个控制台组投一个 Ctrl+C」。本机三种后果都撞见过：目标在另一个控制台时谁也没死
    （它照常活着）；同一个控制台下**两种都出现过** —— 一次是调用者自己被带走
    （退出码 0xC000013A，目标照常活着），一次是目标 1 秒内退了（退出码 0xC0000142）——
    同一次调用死的是谁我们说不准，而这正是不能拿它当探测的理由。更糟的是它对“已经退出的 pid”
    根本不报错 —— 也就是会答“还活着”（只有“从未存在过的 pid”会被 winerror 87 拦下）。
    一个探测函数不能既答错又动手，所以这里只走只读的内核接口。POSIX 下 signal 0 才是
    “只查存在”的惯例。

    也不能只问“`OpenProcess` 开不开得开”：实测一个已经退出（退出码 7）、但还有句柄
    指着它的 pid，`OpenProcess` 照样成功 —— 必须把退出码取回来跟 `_STILL_ACTIVE` 比。
    """
    if os.name != "nt":  # pragma: no cover - 非 Windows 上 signal 0 就是存在性检查
        try:
            os.kill(pid, 0)  # noqa: S606 - POSIX 惯例：signal 0 只探测，不投递
            return True
        except OSError:
            return False
    kernel32 = _kernel32()
    handle = _open_process(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
    if not handle:
        return False
    try:
        code = ctypes.c_uint32(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _assign(pid: int) -> bool:
    """把一个工人进程纳入“我们一死就杀光”的作业。"""
    if os.name != "nt" or not _job_ready or _job_handle is None:
        return False
    handle = _open_process(pid, _PROCESS_SET_QUOTA | _PROCESS_TERMINATE)
    if not handle:
        return False
    try:
        return bool(_kernel32().AssignProcessToJobObject(_job_handle, handle))
    finally:
        _kernel32().CloseHandle(handle)


def job_object_active() -> bool:
    """作业对象是否已经生效（测试与诊断用）。"""
    return _job_ready


def register(proc: subprocess.Popen) -> None:
    """登记一个**工人**子进程：纳入作业（我们死了内核连带杀它），
    能优雅退出时再先杀一道，避免留下半截产物。"""
    with _lock:
        _live[proc.pid] = proc
        if not _assign(proc.pid):
            # 纳不进去（非 Windows / 作业没建成 / 权限不够）：还有退出回收与启动清扫两道
            log.debug("工人 pid=%s 未能纳入作业对象，只靠退出时回收", proc.pid)
        pids = _read_records()
        pids.add(proc.pid)
        _write_records(pids)


def unregister(proc: subprocess.Popen) -> None:
    with _lock:
        _live.pop(proc.pid, None)


def run(cmd: list[str], *, timeout: float | None = None, **kwargs) -> subprocess.CompletedProcess:
    """`subprocess.run` 的替代品：把工人登记并纳入作业，超时按老规矩杀掉它。

    stdout 与 stderr 都要带回去（`CompletedProcess(cmd, rc, out, err)`）：调用方拿
    stderr 拼给人看的错误说明，丢了它就等于把错因抹平。
    """
    proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603 - 调用方自己给完整命令
    register(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill(proc)
        out, err = proc.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err) from exc
    finally:
        unregister(proc)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.kill()
    except OSError as exc:
        log.info("结束子进程 %s 失败：%s", proc.pid, exc)


def Popen(cmd: list[str], **kwargs) -> subprocess.Popen:  # noqa: N802 - 名字故意跟 subprocess 一致
    """`subprocess.Popen` 的替代品：创建即登记。

    给“起一个工人但不等它结束”的路径用（交互式安装程序）：这类子进程
    自己会派生孙进程，孙进程从父进程继承作业，所以我们没经手过的也在内。
    """
    proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603 - 调用方自己给完整命令
    register(proc)
    return proc


def spawn_detached(cmd: list[str]) -> tuple[subprocess.Popen, bool]:
    """启动一个**不该随本应用一起死**的子进程（切片软件这类用户程序）。

    不登记、不纳入作业，因此本应用死掉时内核不会把它带走。第二个返回值
    （能不能独立于我们）是**拿内核问出来的**，不是自己假定的：旧写法带着
    `CREATE_BREAKAWAY_FROM_JOB` 就一律报 True，而实测在“我们已经被关在某个祖先作业里”
    的环境（开发工具链、沙箱、部分杀软）它根本逃不出去，用户看到的是
    “已启动 Bambu Studio”然后软件一关它就没了。

    标准输入输出全部指向 DEVNULL：不继承我们的控制台/日志句柄，否则我们一死
    它写日志会被打断，用户的软件可能因此自己退出。
    """
    proc = subprocess.Popen(  # noqa: S603 - 调用方自己给完整命令
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if os.name != "nt":
        # POSIX 上子进程在父进程死后会被 init 接走，本来就能继续跑
        return proc, True
    return proc, in_job(proc.pid, _job_handle) is False


def image_path(pid: int) -> str | None:
    """某个进程当前运行的可执行文件全路径（启动后回查用）。"""
    return _image_path(pid)


def list_running_process_names() -> set[str]:
    """当前在跑的进程名（小写 basename，如 `{'bambu-studio.exe'}`）。

    只用 Toolhelp 快照里的名字，**不**逐个去 OpenProcess 取完整路径：
    后者在一台普通机器上要开几百次句柄，为了“切片软件是不是已经开着”付不起这个代价。
    权限不足、快照期间已退出的进程会拿不到名字，直接跳过，不抛错。
    """
    if os.name != "nt":  # pragma: no cover - 非 Windows 上不展示运行状态
        return set()
    kernel32 = _kernel32()
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return set()
    entry = _ProcessEntry32()
    entry.dwSize = ctypes.sizeof(_ProcessEntry32)
    names: set[str] = set()
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile:
                names.add(Path(entry.szExeFile).name.casefold())
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    except OSError as exc:  # pragma: no cover - 快照中途失败不影响调用方
        log.debug("枚举进程失败：%s", exc)
    finally:
        kernel32.CloseHandle(snapshot)
    return names


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
    "Popen",
    "install",
    "job_object_active",
    "in_job",
    "register",
    "unregister",
    "run",
    "spawn_detached",
    "image_path",
    "list_running_process_names",
    "sweep_orphans",
    "terminate_all",
    "reset_for_tests",
]


if __name__ == "__main__":  # pragma: no cover - 手工排查用
    print("job object active:", install())
    print("python:", sys.executable)
