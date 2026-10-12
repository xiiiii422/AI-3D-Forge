"""AI 3D Forge —— 版本与构建信息（单一事实来源）。"""

from __future__ import annotations

import os
import platform
import sys
from datetime import datetime

from .paths import is_frozen

# 语义化版本：正式发布版
# 1.0.1：修「说改 A 结果什么都没改」的三处根因（规格不跟随、规格名↔源码参数名
#        无桥、契约把「加个孔」听成「外径加 10mm」）——见 README-开发者.md §72~§74
# 1.0.2：滤镜转接环可用性与几何兜底、三档使用模式（免费/付费/混合性价比）、
#        下载改弹保存对话框、对话串台与历史项目无下载按钮修复 —— 见 CHANGELOG.md
# 1.0.3：小而美一轮：删 38 个死名字与 8 条 /api；修单实例锁两个结构缺陷（「进程号
#        None」与锁位飘移）、subprocess 解码假通过；被拒时先唤回已有窗口 —— 见 CHANGELOG.md
APP_VERSION = "1.0.3"
# 版本阶段：dev=开发版 / beta=测试版 / release=正式发布版
APP_CHANNEL = os.environ.get("AIFORGE_CHANNEL", "release")
# 构建号：打包时由 scripts\\build.ps1 写入环境变量，源码运行时回退到时间戳
BUILD_ID = os.environ.get("AIFORGE_BUILD_ID", "dev-" + datetime.now().strftime("%Y%m%d"))

APP_NAME = "AI 3D Forge"
APP_PUBLISHER = "AI 3D Forge"

# 需求第三十三节：Python 版本要求
REQUIRED_PYTHON = (3, 10)
_RUNTIME_PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

CHANNEL_LABEL = {
    "dev": "开发版",
    "beta": "测试版",
    "release": "正式发布版",
}.get(APP_CHANNEL, "开发版")


def python_version() -> str:
    return _RUNTIME_PYTHON


def python_bundled() -> bool:
    """发布版把 Python 运行时一起打包，用户机器无需安装 Python。"""
    return is_frozen()


def python_ok() -> bool:
    return sys.version_info[:2] >= REQUIRED_PYTHON


def os_label() -> str:
    if os.name != "nt":
        return f"{platform.system()} {platform.release()}"
    try:
        release = platform.release()
        version = platform.version()
        build = version.split(".")[-1] if version else ""
        # Windows 11 的 build 号 >= 22000
        family = "Windows 11" if build.isdigit() and int(build) >= 22000 else "Windows 10"
        if release and release not in ("10", "11"):
            family = f"Windows {release}"
        edition = _windows_edition()
        suffix = f" {edition}" if edition else ""
        return f"{family}{suffix} (build {build})" if build else f"{family}{suffix}"
    except Exception:  # noqa: BLE001 - 环境信息永不致命
        return platform.platform()


def _windows_edition() -> str:
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        )
        try:
            value, _ = winreg.QueryValueEx(key, "ProductName")
            return str(value).replace("Microsoft ", "").strip()
        finally:
            winreg.CloseKey(key)
    except OSError:
        return ""


def version_info(openscad_version: str | None = None) -> dict[str, object]:
    return {
        "app": APP_VERSION,
        "build": BUILD_ID,
        "app_full": f"{APP_NAME} {APP_VERSION}",
        "channel": APP_CHANNEL,
        "channel_label": CHANNEL_LABEL,
        "python": python_version() + ("（随程序内置）" if python_bundled() else "（开发环境）"),
        "python_bundled": python_bundled(),
        "python_ok": python_ok(),
        "openscad": openscad_version,
        "os": os_label(),
    }
