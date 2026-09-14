"""AI 3D Forge —— WebView2 运行时检测。

软件用系统自带的 WebView2 打开原生窗口（不打包 Chromium，省下几百 MB）。
Windows 11 与较新的 Windows 10 都自带，但**企业长期服务版、精简版、或某些
被 IT 策略清理过的机器上可能缺失**。

缺失时的表现很反直觉：程序不会崩，只是悄悄改用系统浏览器打开界面 ——
用户会以为"这软件怎么开了个网页"，而不知道真正原因，也无从修复。
所以必须在**打开窗口之前**就检测出来，并给出明确的一句话解释 + 一键安装入口。

检测位置（与微软官方推荐一致，覆盖机器级与用户级安装）：
  1. 注册表 HKLM/HKCU 的 WOW6432Node\\...\\EdgeUpdate\\Clients（Edge 通道）
  2. 注册表 ...\\EdgeUpdate\\Clients\\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}（WebView2 专属 GUID）
  3. 文件系统回退（注册表被策略清理时仍能命中）
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# WebView2 Runtime 在注册表中的固定 GUID（微软文档给出的 Evergreen Runtime ID）
WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

# 官方下载页：Evergreen Bootstrapper / 常青版运行时
WEBVIEW2_DOWNLOAD_PAGE = "https://developer.microsoft.com/microsoft-edge/webview2/"
WEBVIEW2_BOOTSTRAPPER = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"


@dataclass
class WebView2Status:
    installed: bool
    version: str | None = None
    source: str | None = None  # registry | filesystem | none
    detail: str = ""
    install_hint: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "installed": self.installed,
            "version": self.version,
            "source": self.source,
            "detail": self.detail,
            "install_hint": self.install_hint,
            "download_url": WEBVIEW2_DOWNLOAD_PAGE,
            "bootstrapper_url": WEBVIEW2_BOOTSTRAPPER,
        }


def _from_registry() -> tuple[str | None, str | None]:
    if os.name != "nt":
        return None, None
    try:
        import winreg
    except ImportError:
        return None, None

    # 依次查：WebView2 专属 GUID、EdgeUpdate 客户端列表（含 Edge Stable/Beta/Dev）
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
        (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
    ]
    for hive, subkey in keys:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
                if version:
                    return str(version), "registry"
        except OSError:
            continue

    # 机器上装了 Edge（Chromium）时 WebView2 通常也在，但版本号要单独取；
    # 这里只作为"疑似存在"的判断，最终仍以文件系统为准。
    return None, None


def _from_filesystem() -> tuple[str | None, str | None]:
    """在常见安装目录里找 msedgewebview2.exe。

    注册表在受管机器上可能被清理，文件系统回退能救回这种情况。
    """
    roots: list[Path] = []
    for env_name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(env_name)
        if not base:
            continue
        roots.append(Path(base) / "Microsoft" / "EdgeWebView" / "Application")
        roots.append(Path(base) / "Microsoft" / "Edge" / "Application")

    for root in roots:
        if not root.is_dir():
            continue
        try:
            candidates = sorted(
                (item for item in root.iterdir() if item.is_dir()),
                key=lambda p: p.name,
                reverse=True,
            )
        except OSError:
            continue
        for candidate in candidates:
            exe = candidate / "msedgewebview2.exe"
            if exe.is_file():
                return candidate.name, "filesystem"
    return None, None


def detect() -> WebView2Status:
    """检测 WebView2 运行时。结果可安全缓存 —— 它不会在进程存活期间变化。"""
    if os.name != "nt":
        return WebView2Status(
            installed=False,
            detail="当前操作系统不是 Windows，原生窗口不可用。",
            install_hint="将使用系统浏览器打开界面。",
        )

    version, source = _from_registry()
    if not version:
        version, source = _from_filesystem()

    if version:
        return WebView2Status(
            installed=True,
            version=version,
            source=source,
            detail=f"WebView2 运行时已就绪（{version}）。",
        )

    return WebView2Status(
        installed=False,
        detail="系统缺少 WebView2 运行时，无法打开原生窗口。",
        install_hint=(
            "软件已自动改用系统浏览器打开界面，功能完全一致。"
            "如果想要独立窗口，可安装微软官方的 WebView2 运行时（免费、约 2MB、无需重启）。"
        ),
    )


_cached: WebView2Status | None = None


def cached_status(refresh: bool = False) -> WebView2Status:
    global _cached
    if _cached is None or refresh:
        _cached = detect()
    return _cached


def check_native_window_ready() -> tuple[bool, str]:
    """检查「原生窗口」这条链路是否真的可用（不只是运行时在不在）。

    为什么要查到这么深：WebView2 运行时装着、pywebview 也打包了，仍然可能起不来，
    实测踩过两个坑，且都只在发布版复现：
      1. 打包时漏掉 pywebview 的顶层依赖（bottle / proxy_tools）→ import webview 失败；
      2. CLR 运行时选错：pythonnet 自带的 coreclr 里**没有 System.Windows.Forms**，
         WinForms 后端建窗口时报 "Could not load file or assembly 'System.Windows.Forms'"。
         Windows 自带的 .NET Framework 4.x 才有，所以必须用 netfx。

    因此这里要做到「真的把 WinForms 后端模块导入进来」，而不是只 import webview ——
    只 import webview 两个坑都发现不了（第一个能发现，第二个发现不了）。

    返回 (是否可用, 说明)。
    """
    if os.name != "nt":
        return False, "当前系统不支持 Windows 原生窗口，将使用系统浏览器。"

    status = cached_status()
    if not status.installed:
        return False, status.detail + " " + (status.install_hint or "")

    # 与真实启动路径用同一套运行时配置，否则这项检查没有意义
    os.environ["PYTHONNET_RUNTIME"] = "netfx"

    try:
        import webview  # noqa: F401
    except ImportError as exc:
        return False, (
            f"原生窗口组件不完整（{exc}）。这通常意味着安装包打包不完整，"
            "软件已改用系统浏览器，功能不受影响；重新安装可修复。"
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"原生窗口组件加载失败（{type(exc).__name__}: {exc}），将使用系统浏览器。"

    # 关键一步：真正加载 WinForms 后端（这一步才会暴露缺少 System.Windows.Forms）
    try:
        from webview.platforms import winforms  # noqa: F401
    except ImportError as exc:
        return False, (
            f"原生窗口后端（WinForms）不可用：{exc}。"
            "软件已改用系统浏览器，功能完全一致；重新安装软件可修复独立窗口。"
        )
    except Exception as exc:  # noqa: BLE001
        return False, (
            f"原生窗口后端加载失败（{type(exc).__name__}: {exc}）。"
            "软件已改用系统浏览器，功能完全一致。"
        )

    return True, f"原生窗口可用（WebView2 {status.version}）。"


def try_install() -> tuple[bool, str]:
    """尝试自动安装 WebView2 运行时。

    优先用 winget（微软官方包管理器，走 Microsoft 自己的 CDN）；
    失败则回退到官方 Bootstrapper 下载并静默执行。
    返回 (是否成功, 给用户看的说明)。
    """
    if os.name != "nt":
        return False, "仅支持 Windows。"

    winget = shutil.which("winget")
    if winget:
        try:
            proc = subprocess.run(
                [
                    winget, "install", "--id", "Microsoft.EdgeWebView2Runtime", "--exact",
                    "--silent", "--accept-package-agreements", "--accept-source-agreements",
                    "--disable-interactivity",
                ],
                capture_output=True,
                timeout=900,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0 and cached_status(refresh=True).installed:
                return True, "WebView2 运行时已安装完成。"
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.info("通过 winget 安装 WebView2 失败：%s", exc)

    # 回退：官方 Bootstrapper
    try:
        import httpx

        from .paths import paths

        target = paths().downloads_dir / "MicrosoftEdgeWebview2Setup.exe"
        target.parent.mkdir(parents=True, exist_ok=True)
        with httpx.stream(
            "GET", WEBVIEW2_BOOTSTRAPPER, follow_redirects=True,
            timeout=httpx.Timeout(30.0, read=180.0),
        ) as response:
            response.raise_for_status()
            with target.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=256 * 1024):
                    handle.write(chunk)
        proc = subprocess.run(
            [str(target), "/silent", "/install"],
            capture_output=True,
            timeout=900,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if cached_status(refresh=True).installed:
            return True, "WebView2 运行时已安装完成。"
        return False, f"安装程序已运行（退出码 {proc.returncode}），但未检测到 WebView2。"
    except Exception as exc:  # noqa: BLE001
        log.warning("下载安装 WebView2 失败：%s", exc)
        return False, f"自动安装失败：{exc}。可访问 {WEBVIEW2_DOWNLOAD_PAGE} 手动安装。"
