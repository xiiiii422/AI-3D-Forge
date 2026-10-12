"""AI 3D Forge —— 高分辨率（DPI）适配。

为什么需要这个模块（实测现象）：
  * PyInstaller 生成的 exe 默认**不声明** DPI 感知，Windows 于是按「DPI 不敏感
    进程」处理：整个窗口先按 96dpi 渲染，再被系统**位图拉伸**到用户缩放比例。
    150%/200% 屏上文字和 3D 线条全部发虚 —— 用户反馈的「高分辨率下界面模糊」。
  * pywebview 5.4 自己不做任何 DPI 声明（在其包里搜不到 SetProcessDpiAwareness），
    所以只能由我们在**创建任何窗口之前**自己声明。

必须在导入 webview / 创建窗口之前调用 `ensure_dpi_awareness()`：
Windows 的 DPI 感知一旦有窗口被创建就锁定，之后调用无效。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import os
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

# DPI_AWARENESS_CONTEXT values（user32，Win10 1703+）
_PER_MONITOR_AWARE_V2 = -4
_PER_MONITOR_AWARE = -3
_SYSTEM_AWARE = -1

# SM_CXSCREEN / SM_CYSCREEN（整屏，单位随 DPI 感知状态变化）。
# 这里**没有**工作区索引可用：winuser.h 里根本不存在 SM_CXWORKAREA / SM_CYWORKAREA，
# 工作区只能问 SystemParametersInfoW(SPI_GETWORKAREA)，见下面取值处的实测说明。
_SM_CXSCREEN = 0
_SM_CYSCREEN = 1

#: SystemParametersInfoW 的「主显示器工作区（像素矩形）」
_SPI_GETWORKAREA = 0x0030

_state: DpiState | None = None


@dataclass
class DpiState:
    """当前进程的 DPI 状况（诊断页与验收脚本都会读它）。"""

    aware: bool = False
    mode: str = "unknown"  # per_monitor_v2 | per_monitor | system | unaware
    scale: float = 1.0
    dpi: int = 96
    screen_px: tuple[int, int] = (0, 0)
    work_px: tuple[int, int] = (0, 0)

    @property
    def work_dip(self) -> tuple[int, int]:
        """工作区大小换算成设备无关像素（DIP）—— 窗口尺寸要用这个单位。"""
        scale = self.scale if self.scale > 0.01 else 1.0
        return (int(self.work_px[0] / scale), int(self.work_px[1] / scale))

    def to_dict(self) -> dict[str, object]:
        work = self.work_dip
        return {
            "aware": self.aware,
            "mode": self.mode,
            "dpi": self.dpi,
            "scale": round(self.scale, 2),
            "screen_px": list(self.screen_px),
            "work_px": list(self.work_px),
            "work_dip": list(work),
        }


def _user32():
    if os.name != "nt":
        return None
    try:
        return ctypes.windll.user32  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None


def _shcore_set_per_monitor() -> int | None:
    """调 `shcore.SetProcessDpiAwareness(2)`，把 **HRESULT 原样交回**；没有 shcore 返回 None。

    返回值必须看：这个 API 失败时**不抛异常**，只返回非 0 的 HRESULT。本机实测
    （build/work/dpi-hresult.txt）：进程已经有 DPI 感知时再调一次，返回
    `-2147024891`（0x80070005 = E_ACCESSDENIED），Python 这边一条异常都捕不到。
    旧写法是 `windll.shcore.SetProcessDpiAwareness(2)` 后直接 `aware = True`、
    `mode = "per_monitor"` —— 于是「调用没炸」被当成「设成功了」，写进日志、写进
    诊断页，还顺手**跳过**下面那条真可能成功的 `SetProcessDPIAware()` 兜底。
    """
    from ctypes import windll

    try:
        setter = windll.shcore.SetProcessDpiAwareness
    except (OSError, AttributeError):  # Win7 / 精简系统没有 shcore.dll
        return None
    setter.argtypes = [ctypes.c_int]
    setter.restype = ctypes.c_long
    return int(setter(2))


def _query_awareness(user32) -> str | None:
    """问系统「本进程现在的感知状态是什么」，而不是「我刚才那次设成功没有」。

    三条 setter 全失败时，最常见的原因不是「系统不让我感知」，而是**感知早就被别人
    定好了**（exe 清单、PyInstaller 引导程序、或已经建过窗口）—— 那时它们一律返回
    失败。这时候写 `mode="unaware"` 是反方向的谎报：用户会照着「未适配」去折腾，
    而真问题是别的。所以拿只读 API 对一次账。

    返回 `per_monitor_v2 | per_monitor | system | None`（None = 查不出来或确实不感知）。
    """
    try:
        if user32 is not None and hasattr(user32, "AreDpiAwarenessContextsEqual"):
            getter = user32.GetThreadDpiAwarenessContext
            getter.restype = ctypes.c_void_p
            handle = getter()
            equal = user32.AreDpiAwarenessContextsEqual
            equal.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            equal.restype = ctypes.c_bool
            for value, label in (
                (_PER_MONITOR_AWARE_V2, "per_monitor_v2"),
                (_PER_MONITOR_AWARE, "per_monitor"),
                (_SYSTEM_AWARE, "system"),
            ):
                if handle and equal(ctypes.c_void_p(handle), ctypes.c_void_p(value)):
                    return label
    except Exception:  # noqa: BLE001 - Win10 1703 以前没有这两个导出
        pass
    try:
        from ctypes import windll

        getter = windll.shcore.GetProcessDpiAwareness
        getter.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        getter.restype = ctypes.c_long
        out = ctypes.c_int(-1)
        if int(getter(None, ctypes.byref(out))) == 0:  # DPI_AWARENESS: 0/1/2
            return {1: "system", 2: "per_monitor"}.get(int(out.value))
    except Exception:  # noqa: BLE001
        pass
    return None


def ensure_dpi_awareness() -> DpiState:
    """声明进程 DPI 感知（幂等，可重复调用）。

    依次尝试：Per-Monitor V2（最佳，多显示器不同缩放也不会糊）→ Per-Monitor →
    System aware → 老的 shcore 接口 → user32.SetProcessDPIAware。
    全部失败也不报错：那说明系统太旧或被组策略固定，界面仍可用，只是可能仍会被拉伸。
    """
    global _state
    if _state is not None:
        return _state

    state = DpiState()
    if os.name != "nt" or sys.platform != "win32":
        state.mode = "not-windows"
        _state = state
        return state

    user32 = _user32()
    if user32 is not None:
        for ctx, label in (
            (_PER_MONITOR_AWARE_V2, "per_monitor_v2"),
            (_PER_MONITOR_AWARE, "per_monitor"),
            (_SYSTEM_AWARE, "system"),
        ):
            try:
                # SetProcessDpiAwarenessContext 需要 BOOL 返回值；参数是指针型常量
                func = user32.SetProcessDpiAwarenessContext
                func.argtypes = [ctypes.c_void_p]
                func.restype = ctypes.c_bool
                if func(ctypes.c_void_p(ctx)):
                    state.aware = True
                    state.mode = label
                    break
            except Exception:  # noqa: BLE001 - 老系统没有这个导出
                continue

    if not state.aware:
        hr = _shcore_set_per_monitor()  # Win8.1 的 shcore 接口
        if hr == 0:
            # **只有 HRESULT 为 0** 才算真的设成了 per-monitor
            state.aware = True
            state.mode = "per_monitor"
        else:
            if hr is not None:
                log.debug("shcore.SetProcessDpiAwareness 未成功：hr=0x%08X", hr & 0xFFFFFFFF)
            try:
                if user32 is not None and user32.SetProcessDPIAware():
                    state.aware = True
                    state.mode = "system"
            except Exception:  # noqa: BLE001
                state.mode = "unaware"
            if not state.aware:
                real = _query_awareness(user32)
                if real:
                    state.aware = True
                    state.mode = real
                    log.info("DPI 感知已由别处设定，对账结果：%s", real)
                else:
                    state.mode = "unaware"

    # 感知声明之后再取 DPI 才是真值（不感知时 GetDpiForSystem 恒为 96）
    dpi = 96
    try:
        if user32 is not None:
            thread = user32.GetDpiForSystem if hasattr(user32, "GetDpiForSystem") else None
            if thread is not None:
                value = user32.GetDpiForSystem()
                if value:
                    dpi = int(value)
    except Exception:  # noqa: BLE001
        dpi = 96
    if dpi < 96:
        try:
            dc = user32.GetDC(0) if user32 is not None else None
            if dc:
                LOGPIXELSX = 88
                gdi32 = ctypes.windll.gdi32  # type: ignore[attr-defined]
                dpi = int(gdi32.GetDeviceCaps(dc, LOGPIXELSX)) or 96
                user32.ReleaseDC(0, dc)
        except Exception:  # noqa: BLE001
            dpi = 96

    state.dpi = dpi
    state.scale = dpi / 96.0
    if user32 is not None:
        try:
            state.screen_px = (int(user32.GetSystemMetrics(_SM_CXSCREEN)), int(user32.GetSystemMetrics(_SM_CYSCREEN)))
        except Exception:  # noqa: BLE001
            pass
        # 工作区必须用 SystemParametersInfoW(SPI_GETWORKAREA)。
        # 这里以前读的是 GetSystemMetrics(48) / (49)，上面注释把它写成「SM_CXWORKAREA /
        # SM_CYWORKAREA」—— 而 winuser.h 里**没有这两个索引**：48 是 SM_CYMINSPACING
        # （小图标纵向间距）、49 是 SM_CXMINTRACK（最小窗口跟踪宽度），本机实测分别
        # 读到 34 和 16（屏幕 2880×1620）。后果是连串的：work_dip=(27,16) →
        # window_geometry 把 min 抬到 floor、又把 preferred 夹到 work-8，于是
        # **任何机器上都恒定返回 (720,520) 且 min_size == size**：用户永远拿不到
        # 设计尺寸 1360×880，窗口还被钉死不能放大；而这条夹紧本来要保护的
        # 「高缩放小屏上窗口比屏幕还大」一次也没被保护过。又因为读回来的数不是 0，
        # 下面那道「拿不到就退回屏幕尺寸」的兜底形同虚设。
        # 取值必须在 DPI 感知声明**之后**：未感知进程读到的是系统虚拟化后的 DIP 值。
        try:
            rect = ctypes.wintypes.RECT()
            user32.SystemParametersInfoW.argtypes = [
                ctypes.wintypes.UINT,
                ctypes.wintypes.UINT,
                ctypes.wintypes.LPVOID,
                ctypes.wintypes.UINT,
            ]
            user32.SystemParametersInfoW.restype = ctypes.wintypes.BOOL
            if user32.SystemParametersInfoW(
                _SPI_GETWORKAREA, 0, ctypes.byref(rect), 0
            ):
                state.work_px = (int(rect.right - rect.left), int(rect.bottom - rect.top))
        except Exception:  # noqa: BLE001
            pass
    if state.work_px == (0, 0):
        state.work_px = state.screen_px

    _state = state
    log.info(
        "DPI 适配：mode=%s dpi=%d scale=%.2f 屏幕=%dx%d 工作区=%dx%d（DIP %dx%d）",
        state.mode,
        state.dpi,
        state.scale,
        state.screen_px[0],
        state.screen_px[1],
        state.work_px[0],
        state.work_px[1],
        state.work_dip[0],
        state.work_dip[1],
    )
    return state


def current_state() -> DpiState:
    return _state or DpiState()


def window_geometry(
    preferred: tuple[int, int] = (1360, 880),
    minimum: tuple[int, int] = (1080, 700),
    floor: tuple[int, int] = (720, 520),
    state: DpiState | None = None,
) -> tuple[tuple[int, int], tuple[int, int]]:
    """把「理想窗口尺寸」按真实屏幕工作区夹紧，返回 ((宽,高),(最小宽,最小高))，单位 DIP。

    高缩放的痛点：200% 的 1920×1080 屏只有 960×540 DIP 可用，而窗口最小尺寸写死
    1080×700 —— 窗口比屏幕还大，标题栏/按钮跑到屏幕外，用户既关不掉也看不见。
    界面本身在 900px（甚至 540px 缩放）下实测无溢出，所以允许把最小尺寸降到 floor。
    """
    st = state or current_state()
    work_w, work_h = st.work_dip
    if work_w <= 0 or work_h <= 0:  # 拿不到屏幕信息时保持原样，绝不返回 0
        return preferred, minimum

    min_w = min(minimum[0], max(floor[0], work_w - 24))
    min_h = min(minimum[1], max(floor[1], work_h - 24))
    width = max(min_w, min(preferred[0], work_w - 8))
    height = max(min_h, min(preferred[1], work_h - 8))
    return (int(width), int(height)), (int(min_w), int(min_h))
