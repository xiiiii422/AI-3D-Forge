"""AI 3D Forge —— 单实例保护。

用户重复双击图标时，不应该启动第二个服务、第二个窗口，也不应该弹出「端口被占用」。
做法：在用户目录下对锁文件加独占锁；拿不到锁说明已有实例在运行，把该说的话送到
用户看得见的地方再退（见 `report_refusal`）。

**锁字节与 pid 分开存，而且锁位必须钉死在第 0 字节**（本轮修的坑，两条都是打包验收实测抓出来的）：

1. `msvcrt.locking` 是**强制**字节范围锁，另一个进程去读被锁的那一段会直接
   `PermissionError: [Errno 13]`。以前我们把 pid 写在第 0 字节、又锁第 0 字节，于是
   `running_pid()`（唯一的用途就是「锁被别人拿着时读它是谁」）**每次读都撞上自己加的锁**
   → 永远 `except` → 永远返回 None，用户看到的是「已经在运行中（进程号 None）」：
   被守卫钉住的修复只有这一条：锁只占第 0 字节，pid 从第 1 字节起写。
   读方还额外带了两个**防御、不是修复**的写法：`buffering=0` 和明确长度 `read(24)`
   —— 两个都实测过：拆掉它们守卫仍绿（因为 `acquire()` 会把文件裁到 `1+len(pid)`，
   刚好没给它们留生效机会）。留着的理由是一个远端条件：不带长度的整读在文件
   **有尾巴**时会按 8192 对齐回头读第 0 字节，实测 `PermissionError`（子进程探了 14 种读法）。
   不把正确性挂在「别人肯记得裁文件」上，比多一道保险便宜。

2. `open(path, "a+b")` 的初始位置是 **EOF，不是 0**（实测：空文件 tell()=0、
   10 字节文件 tell()=10）。`locking` 从当前位置开始锁，所以不显式 `seek(0)` 的话，
   锁在哪一字节取决于当时文件多长 —— 两个实例错开一格就不冲突了，
   **单实例保护变成碰运气**（实测：锁文件里残留 30 字节旧 pid 时，第二个实例照样 acquire 成功）。
   现在锁之前一律先 `seek(0)`。

3. 从锁文件读回来的那个数，**在能证明它是“我们自己”之前不准说给用户，也不准拿它去唤窗**。
   旧版（1.0.2）把 pid 写在第 0 字节，新版从第 1 字节读 —— 跨版本时这就是“少掉首位数字”的
   一个数。本机一份真实残留（`336456`）被读成 `36456`；再拿另一个形状实测一次：错读出的数
   正好落在一个活着的外来进程上（`charmap.exe`），于是提示里写出它的进程号、
   `focus_running_window()` 把它最小化的窗口还原到前台 —— 一个**被拒的第二实例动了用户另一个
   程序的窗口**。判据见 `_owner_is_ours()`。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from .paths import APP_NAME, is_frozen, paths
from .stdio import safe_print

log = logging.getLogger(__name__)

_handle = None
_lock_path: Path | None = None

_PID_OFFSET = 1  # 第 0 字节是锁区，pid 从第 1 字节开始（见模块注释）


def _recorded_pid(path: Path) -> int | None:
    """读锁文件里记着的 pid；读不到/不是数字都返回 None。

    必须从 `_PID_OFFSET` 开始读（第 0 字节被别人锁着，这点是投毒实测过的）。
    下面两个写法是**防御、不是修复**，各自实测过拆掉它们守卫仍绿：
    `buffering=0`、以及给 `read` 一个明确长度 —— 后者挡的是一个远端条件：
    不带长度的整读在文件有尾巴时会按 8192 对齐回头读第 0 字节，实测 `PermissionError`。
    """
    try:
        with open(path, "rb", buffering=0) as fh:
            fh.seek(_PID_OFFSET)
            raw = fh.read(24)
    except OSError:
        return None
    text = raw.decode("ascii", errors="replace").strip()
    return int(text) if text.isdigit() else None


def acquire() -> bool:
    """尝试获取单实例锁。返回 True 表示本进程是唯一实例。

    `False` **只表示「另一个进程正持着这把锁」**。其它失败一律放行：
    本模块早就写过一条约定「锁机制失败不应阻止软件启动」（见下面
    `except Exception` 那条注释），而旧写法把 open / truncate / write / flush 的
    `OSError` 和真正的锁冲突混进了同一个 `except OSError: return False`，于是
    **最常见的失败方式恰好变成了谎报**。实测三种很日常的形态：
    锁文件被置成只读（备份/同步盘/误操作）、`app.lock` 变成一个目录（同步盘占位）、
    杀软或索引器只给读共享地开着它 —— 都会让用户双击后听到
    「AI 3D Forge 已经在运行中，请切换到已打开的窗口」，而**根本没有窗口**，
    退出码还是 0（快捷方式与脚本都以为正常）。用户只能反复双击、重启，
    或者自己找到并删掉 `%APPDATA%` 里那个锁 —— 软件从此打不开。
    """
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
        except Exception as exc:  # noqa: BLE001 - 连文件都开不了，证明不了别人在跑
            log.debug("单实例锁文件打不开，继续启动：%s", exc)
            return True
        try:
            # `a+b` 的当前位置是 EOF，而 `locking` 从当前位置开始锁：不先 seek(0)
            # 就会按“当时文件多长”去锁一个飘移的字节，第二个实例锁到别的字节就照样起得来。
            _handle.seek(0)
            msvcrt.locking(_handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            # **只有这里**失败才说明另一个进程正持着锁
            log.debug("单实例锁已被其它进程持有：%s", exc)
            try:
                _handle.close()
            except OSError:
                pass
            _handle = None
            return False
        except Exception:  # noqa: BLE001 - 意料外的错不能算“别人在跑”
            log.debug("单实例锁不可用，继续启动", exc_info=True)
            try:
                _handle.close()
            except OSError:
                pass
            _handle = None
            return True
        try:
            # 先裁到只剩锁字节，再从偏移 1 写 pid：不裁的话上一个较长 pid 的尾巴会留下来
            _handle.truncate(_PID_OFFSET)
            _handle.seek(_PID_OFFSET)
            _handle.write(str(os.getpid()).encode("ascii"))
            _handle.flush()
        except Exception as exc:  # noqa: BLE001 - 锁已拿到，只是 pid 没记上
            # 影响面只有「被拒的那个实例能不能报出/唤回我们」，不该阻止启动。
            log.debug("单实例锁已持有但 pid 没写进去：%s", exc)
        return True

    # 非 Windows：用 pid 文件做尽力而为的判断（布局与 Windows 一致：第 0 字节是占位）
    try:
        from .procguard import process_alive  # noqa: PLC0415 - 与其余 Windows 原语一样按需引用

        _lock_path = paths().data_dir / "app.lock"
        if _lock_path.exists():
            held = _recorded_pid(_lock_path)
            if held is not None and process_alive(held):
                return False
        _lock_path.parent.mkdir(parents=True, exist_ok=True)
        _lock_path.write_bytes(b"\x00" + str(os.getpid()).encode("ascii"))
        return True
    except OSError:
        return True


def _owner_is_ours(pid: int) -> bool:
    """锁里记的这个 pid 是不是“我们自己的、还活着”的进程。

    为什么必须有这一句：旧版把 pid 写在第 0 字节，本版从第 1 字节读，跨版本时拿到的
    是“少掉首位数字”的数（本机残留 `336456` → 读成 `36456`）。这种数完全可能落在一个
    活着的外来进程上 —— 实测过：拿它去唤窗，另一个程序最小化的窗口被还原到了前台。

    按“我们可能被叫出的名字”**集合**比，不是按单一路径比：实测这台机器的 venv 里同一个
    python.exe 有两个名字（`build\\venv\\Scripts\\python.exe` 与它背后互为硬链接的真身），
    `sys.executable` 报前者、`QueryFullProcessImageNameW` 八次里七次报后者 —— 只认一个
    就会把真兄弟认成外人，那条唤窗逻辑于是永远关着。

    非 Windows 拿不到镜像名，退化成“进程在不在”（那里没有错读布局的问题：写与读同一份代码）。
    """
    from .procguard import _image_path, process_alive  # noqa: PLC0415, SLF001 - 同一包内的私有约定

    if os.name != "nt":  # pragma: no cover - 非 Windows 分支
        return process_alive(pid)
    # 实测：进程已经退出时 `_image_path()` 返回 None（哪怕 OpenProcess 还开得开），
    # 所以这一句同时管住了“还活着”与“是不是我们”。
    image = _image_path(pid)
    if not image:
        return False
    names = {os.path.normcase(sys.executable)}
    mine = _image_path(os.getpid())
    if mine:
        names.add(os.path.normcase(mine))
    return os.path.normcase(image) in names


def release() -> None:
    global _handle
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
    """读锁文件里记录的 PID（用于「已有实例」提示与唤窗）。

    这条只能在「本进程没拿到锁」的时候调，也就是**另一个进程正持着锁**的时候 ——
    所以它绝不能从第 0 字节读，否则就是拿自己加的锁去挡自己想读的信息。
    拿到数以后还要过 `_owner_is_ours()`：读回来的数不一定真是那个持有者（见模块注释第 3 条），
    认不出来就返回 None —— 宁可少说一个进程号，也不能报错一个、更不能去动别人的窗口。
    真机验收：自检的「单实例锁（真两进程）」用例（另起一个真进程持锁再读）。
    """
    try:
        path = paths().data_dir / "app.lock"
        if not path.is_file():
            return None
    except OSError:
        return None
    pid = _recorded_pid(path)
    if pid is None:
        return None
    return pid if _owner_is_ours(pid) else None


def refusal_message(pid: int | None) -> str:
    """第二个实例被拒时给用户看的那句话。

    拿不到 pid 时就不写括号（以前是无条件拼接，所以真机上看到的就是「进程号 None」）。
    """
    base = f"{APP_NAME} 已经在运行中"
    if pid:
        return f"{base}（进程号 {pid}）。请切换到已打开的窗口。"
    return f"{base}。请切换到已打开的窗口。"


def _has_console() -> bool:
    """当前进程有没有一个看得到字的控制台。

    必须在 `stdio.prepare()` 之后问：打包版虽然是 `console=False`，但从终端里起时
    `ensure_console_streams()` 会试着用 `AttachConsole(ATTACH_PARENT_PROCESS)` 挂回那个
    控制台。挂得上挂不上本机都撞上过（连着两次跑装机验收：一次这句话真打到了控制台上
    而没弹框，一次弹了框），所以不能拿“双击图标”这一种启动方式去写死它：
    判据就一个 —— 此刻有没有人看得见字。
    """
    if os.name != "nt":
        return sys.stdout is not None
    import ctypes

    return bool(ctypes.windll.kernel32.GetConsoleWindow())  # type: ignore[attr-defined]


def _should_dialog(*, frozen: bool, has_console: bool, headless: bool, focused: bool) -> bool:
    """只在「打包版 + 没有控制台 + 不是明确要后台跑 + 没能把已有窗口带回前台」时弹框。

    后两个条件各挡一类事故：PowerShell 跑验收脚本（`--server`）弹个模态框
    会把整条流水线卡死；而已经帮用户把窗口带回来了，就更不该再逼他多点一次「确定」。
    """
    return bool(frozen) and not has_console and not headless and not focused


def focus_running_window(pid: int | None = None) -> bool:
    """把已在运行的那个实例的窗口带回前台（只能做到“试过”时返回 True）。

    为什么要做：用户重复双击图标，多数是因为第一次还来不及看到窗口。成熟桌面软件
    的做法是把已有窗口带回来，而不是再开一个。窗口按 pid 找 —— pid 来自锁文件，
    这正是本轮先把 pid 修到真能读到的原因（没修之前它永远是 None，这条也就永远关着）。
    """
    if os.name != "nt":
        return False
    target = running_pid() if pid is None else pid
    if not target:
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    found: list[int] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)  # noqa: N806

    @WNDENUMPROC
    def _each(hwnd, _lparam):  # noqa: ANN001 - 回调签名由 Windows 定
        if not user32.IsWindowVisible(hwnd):
            return True
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value != target:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if not buf.value.strip():
            return True  # 没标题的那多是隐形宿主/消息窗口，不该被带到前面
        found.append(hwnd)
        return True

    user32.EnumWindows(_each, 0)
    if not found:
        return False
    hwnd = found[0]
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE：最小化时先还原
    user32.SetForegroundWindow(hwnd)
    # 不能自己骗自己：Windows 的前台切换可以被系统策略拦住，所以成不成要去问内核现在谁在前沿。
    return bool(user32.GetForegroundWindow() == hwnd)


def report_refusal(headless: bool = False) -> str:
    """把「已有实例在跑」这件事说到用户听得见的地方，返回说的那句话。

    顺序是：能直接把已有窗口带回前台就什么都不问；带不回来才弹框。
    为什么不能只靠 `print`：打包版是 `console=False` 的窗口程序，用户双击图标时根本没有
    控制台（`stdio.ensure_console_streams()` 挂不上父控制台就只挂到 devnull），于是
    print 本身成功但**没人看得见** —— “双击图标没反应”什么证据都不留。这种时候只能弹
    一次系统消息框（条件见 `_should_dialog`；从终端起时控制台看得到字，就不多要一点）。
    """
    pid = running_pid()
    msg = refusal_message(pid)
    safe_print(msg)
    focused = focus_running_window(pid)
    if _should_dialog(frozen=is_frozen(), has_console=_has_console(), headless=headless, focused=focused):
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, msg, APP_NAME, 0x40)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 弹不出来也不能拦住退出
            log.debug("消息框弹不出来", exc_info=True)
    return msg
