"""AI 3D Forge —— 快捷方式创建（安装器之外的运行期兜底）。

安装器（Inno Setup）会在安装时创建桌面与开始菜单快捷方式。这里提供同样的能力，
用于：便携版用户、快捷方式被误删后的「修复快捷方式」。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
APP_NAME = "AI 3D Forge"


def launcher_command() -> tuple[str, str]:
    """返回 (可执行文件, 参数) —— 优先指向发布版的 exe。"""
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve()), ""

    # 开发模式：指向源码目录的启动脚本，方便开发者验证快捷方式逻辑
    root = Path(__file__).resolve().parents[2]
    main_py = root / "main.py"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    executable = pythonw if pythonw.is_file() else Path(sys.executable)
    return str(executable), f'"{main_py}"'


def _powershell(script: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()[:300]
    return True, ""


def _create_one(link_path: Path, executable: str, arguments: str, working_dir: str, description: str) -> tuple[bool, str]:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    escaped_link = str(link_path).replace("'", "''")
    escaped_target = executable.replace("'", "''")
    escaped_workdir = working_dir.replace("'", "''")
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut('{escaped_link}'); "
        f"$sc.TargetPath = '{escaped_target}'; "
        f"$sc.Arguments = '{arguments}'; "
        f"$sc.WorkingDirectory = '{escaped_workdir}'; "
        f"$sc.Description = '{description}'; "
        f"$sc.IconLocation = '{escaped_target},0'; "
        "$sc.Save()"
    )
    return _powershell(script)


def _desktop_dir() -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"


def _start_menu_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def create_shortcuts() -> dict[str, object]:
    """创建桌面与开始菜单快捷方式。返回每项的结果，失败不抛异常。"""
    executable, arguments = launcher_command()
    working_dir = str(Path(executable).parent)
    results: dict[str, object] = {"executable": executable, "arguments": arguments, "created": [], "failed": []}

    targets = [
        (_desktop_dir() / f"{APP_NAME}.lnk", "桌面"),
        (_start_menu_dir() / f"{APP_NAME}.lnk", "开始菜单"),
    ]
    for link, label in targets:
        ok, error = _create_one(link, executable, arguments, working_dir, "AI 3D 打印设计工具")
        if ok:
            results["created"].append({"label": label, "path": str(link)})
        else:
            log.info("创建%s快捷方式失败：%s", label, error)
            results["failed"].append({"label": label, "path": str(link), "error": error})

    results["ok"] = not results["failed"]
    return results


def describe_shortcuts() -> dict[str, object]:
    executable, arguments = launcher_command()
    desktop = _desktop_dir() / f"{APP_NAME}.lnk"
    start_menu = _start_menu_dir() / f"{APP_NAME}.lnk"
    return {
        "executable": executable,
        "arguments": arguments,
        "desktop": {"path": str(desktop), "exists": desktop.is_file()},
        "start_menu": {"path": str(start_menu), "exists": start_menu.is_file()},
    }
