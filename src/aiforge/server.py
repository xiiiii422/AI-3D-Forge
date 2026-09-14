"""AI 3D Forge —— 本机服务与桌面窗口。

对最终用户而言，这里发生的一切都应该是不可见的：
    双击图标 → 出现「AI 3D Forge」窗口 → 直接开始设计。

实现要点：
  * 只监听 127.0.0.1，绝不对外网开放。
  * 端口从 8760 起自动寻找空闲端口（避免与其他软件冲突），前端通过相对路径访问，
    因此换端口不需要改任何前端代码。
  * 优先使用系统自带的 WebView2 打开原生窗口（不额外占用几百 MB 磁盘、不下载浏览器）；
    若系统缺少 WebView2 运行时，则退回到默认浏览器打开 —— 只提示一次，不反复打扰。
  * 单实例保护：重复启动时只把已有窗口带到前台，不会启动第二个服务。
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
from typing import Any

from . import runtime_state, webview_runtime
from .app import create_app, frontend_available
from .paths import is_frozen, paths
from .version import APP_NAME, APP_VERSION, CHANNEL_LABEL

log = logging.getLogger(__name__)

DEFAULT_PORT = 8760
PORT_SCAN_RANGE = 40
WINDOW_TITLE = f"{APP_NAME} {APP_VERSION}"


def find_free_port(preferred: int = DEFAULT_PORT, span: int = PORT_SCAN_RANGE) -> int:
    for candidate in range(preferred, preferred + span):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return candidate
    # 极端情况：让系统分配
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_until_ready(port: int, timeout_s: float = 30.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if runtime_state.shutdown_requested():
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.6):
                return True
        except OSError:
            time.sleep(0.15)
    return False


class ServerThread(threading.Thread):
    def __init__(self, port: int) -> None:
        super().__init__(name="aiforge-server", daemon=True)
        self.port = port
        self._server: Any = None
        self.ready = threading.Event()

    def run(self) -> None:
        import uvicorn

        config = uvicorn.Config(
            create_app(),
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            log_config=None,
            access_log=False,
            loop="asyncio",
            lifespan="on",
        )
        server = uvicorn.Server(config)
        self._server = server
        runtime_state.register_server(server)
        self.ready.set()
        try:
            server.run()
        except Exception:  # noqa: BLE001 - 服务线程崩溃必须留痕，但不影响主线程收尾
            log.exception("本机服务异常退出")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True


def start_server(preferred_port: int = DEFAULT_PORT) -> tuple[ServerThread, int]:
    port = find_free_port(preferred_port)
    thread = ServerThread(port)
    thread.start()
    thread.ready.wait(timeout=10)
    if not wait_until_ready(port, timeout_s=30):
        raise RuntimeError(
            "本机服务启动失败。可能是安全软件拦截了本机网络访问，或端口被占用。"
        )
    return thread, port


def _prepare_webview_runtime() -> None:
    """为 pywebview 配置 CLR 运行时与 GUI 后端。

    必须用 **netfx**（Windows 自带的 .NET Framework 4.x），不能用 coreclr：
      pywebview 的 Windows 后端是 WinForms，而 pythonnet 自带的 coreclr 运行时里
      **没有 System.Windows.Forms**（实测 97 个程序集里只有 System.Drawing.Primitives）。
      用 coreclr 时窗口一定起不来：
        "Could not load file or assembly 'System.Windows.Forms'"
      于是静默退回浏览器 —— 用户看到的就是"这软件怎么开了个网页"。

      .NET Framework 4.x 是 Windows 10/11 的组成部分，WinForms 直接在 GAC 里，
      因此走 netfx 不需要随包携带任何 .NET 运行时（coreclr 桌面版要 60MB+）。

    强制指定 GUI 后端为 winforms，避免 pywebview 在其他平台上自动挑选。
    """
    os.environ["PYTHONNET_RUNTIME"] = "netfx"
    os.environ.setdefault("PYWEBVIEW_GUI", "winforms")
    os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")


def open_window(url: str) -> bool:
    """尝试用 WebView2 打开原生窗口。返回是否成功（失败则调用方退回浏览器）。"""
    _prepare_webview_runtime()
    try:
        import webview
    except Exception as exc:  # noqa: BLE001
        log.info("WebView 组件不可用（%s），将使用系统浏览器打开界面", exc)
        return False

    try:
        window = webview.create_window(
            WINDOW_TITLE,
            url,
            width=1360,
            height=880,
            min_size=(1080, 700),
            background_color="#FFFFFF",
            text_select=True,
        )
        runtime_state.register_window(window)

        def on_closed() -> None:
            runtime_state.request_shutdown()
            runtime_state.stop_server()
            # 关闭窗口即退出程序：给 uvicorn 一点时间优雅收尾
            threading.Timer(1.2, runtime_state.force_exit).start()

        try:
            window.events.closed += on_closed
        except Exception:  # noqa: BLE001
            log.debug("无法注册窗口关闭事件", exc_info=True)

        webview.start(debug=os.environ.get("AIFORGE_WEBVIEW_DEBUG") == "1", private_mode=False)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "打开原生窗口失败（%s），将使用系统浏览器。"
            "若提示 System.Windows.Forms 相关错误，检查 _prepare_webview_runtime 是否仍使用 netfx。",
            exc,
        )
        return False


def open_in_browser(url: str) -> bool:
    import webbrowser

    try:
        return bool(webbrowser.open(url, new=1))
    except Exception as exc:  # noqa: BLE001
        log.error("打开浏览器失败：%s", exc)
        return False


def run(port: int | None = None, *, browser_only: bool = False, open_browser: bool | None = None) -> int:
    """程序主入口。返回进程退出码。"""
    files = paths()
    try:
        files.ensure()
    except OSError as exc:
        _fatal(str(exc))
        return 3

    if not frontend_available():
        log.warning("未找到前端构建产物（webui/dist），界面将提示资源缺失")

    try:
        thread, actual_port = start_server(port or DEFAULT_PORT)
    except RuntimeError as exc:
        _fatal(str(exc))
        return 4

    url = f"http://127.0.0.1:{actual_port}/"
    print(f"{APP_NAME} {APP_VERSION}（{CHANNEL_LABEL}）")
    print(f"本机服务已启动：{url}")
    print("关闭窗口或按 Ctrl+C 退出。")

    from .config import store

    prefer_browser = open_browser if open_browser is not None else store().config.behavior.open_in_browser
    used_window = False

    # 事前检测 WebView2：缺失时不要假装能开窗口，直接走浏览器并在界面上说明原因。
    # 之前的做法是"先试着开窗口，失败了再退回浏览器"——用户只会看到凭空冒出一个
    # 浏览器标签页，完全不知道发生了什么，也不知道怎么修。
    webview_status = webview_runtime.cached_status()
    browser_reason: str | None = None

    if browser_only or prefer_browser:
        if prefer_browser and not browser_only:
            browser_reason = "你之前选择过使用系统浏览器打开界面。"
    elif os.name != "nt":
        browser_reason = "当前系统不支持 Windows 原生窗口。"
    elif not webview_status.installed:
        browser_reason = webview_status.install_hint or webview_status.detail
        log.info("WebView2 不可用（%s），改用系统浏览器并提示用户", webview_status.detail)
    else:
        try:
            # 主线程运行窗口；服务在自己的线程里
            used_window = open_window(url)
        except KeyboardInterrupt:
            used_window = False
        if not used_window:
            browser_reason = (
                "原生窗口启动失败，已改用系统浏览器。"
                "可以点击界面上的「安装组件」按钮修复，或查看「系统诊断」了解详情。"
            )

    if not used_window:
        target = url
        if browser_reason:
            from urllib.parse import quote

            target = f"{url}?browser_fallback=1&reason={quote(browser_reason)}"
        opened = open_in_browser(target)
        if not opened:
            print("无法自动打开界面，请手动在浏览器中访问上面的地址。")

        # 退回浏览器模式时，必须让用户知道「服务还在后台运行、怎么关掉它」。
        # 打包版是 GUI 子系统、没有控制台，用户看不到任何提示，
        # 也就不知道关掉浏览器标签页之后程序还在；反过来也容易以为
        # 「软件自己退出了」。页面里也有退出按钮，这里再明确一次。
        log.info("已进入浏览器模式；服务将持续运行，直到用户在界面里点「退出」或结束本进程。")
        if is_frozen() and os.name == "nt":
            _notify_browser_mode(url, browser_reason)

        try:
            while not runtime_state.shutdown_requested():
                time.sleep(0.4)
        except KeyboardInterrupt:
            pass

    runtime_state.stop_server()
    thread.stop()
    thread.join(timeout=6)
    return 0


def _notify_browser_mode(url: str, reason: str | None) -> None:
    """在浏览器模式下弹一次说明对话框（非阻塞）。

    只在打包版 + Windows 下调用；开发时看控制台就够了，不必被打断。
    提示里必须包含「怎么办」——用户看到浏览器时最需要的是原因和修复入口，
    而不是一句「已改用浏览器」。
    """
    message = (
        f"{APP_NAME} 已在你的浏览器中打开：\n{url}\n\n"
        "界面和服务会持续运行，不会自动关闭。\n"
        "使用完毕后，请点击界面右上角的「退出」，或直接关闭本进程。\n"
    )
    if reason:
        message += f"\n为什么用浏览器而不是独立窗口：\n{reason}\n"
    message += (
        "\n想看详细原因：在安装目录运行\n"
        f"    {sys.executable} --diagnose-native-window\n"
        "并把输出发给技术支持。也可以在软件「设置 → 系统诊断」里查看「原生窗口组件」一项。"
    )

    def show() -> None:
        try:
            import ctypes

            # MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND，不阻塞主逻辑
            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x40 | 0x10000)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            log.debug("无法弹出浏览器模式提示框", exc_info=True)

    threading.Thread(target=show, name="browser-mode-notice", daemon=True).start()


def _fatal(message: str) -> None:
    log.error("启动失败：%s", message)
    print(f"启动失败：{message}", file=sys.stderr)
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                None,
                f"{APP_NAME} 启动失败。\n\n{message}\n\n"
                f"日志位置：{paths().log_dir}",
                f"{APP_NAME}",
                0x10,
            )
        except Exception:  # noqa: BLE001
            pass
