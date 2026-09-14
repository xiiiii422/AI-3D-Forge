"""AI 3D Forge —— 原生窗口问题一键诊断。

什么时候用：如果软件打开的是**浏览器**而不是它自己的窗口，把安装目录下的
「诊断（原生窗口）.ps1」双击运行，它会调用本模块并把结果写到桌面上，
把那个文本文件发给技术支持即可。

为什么用 Python 而不是 PowerShell 写：本项目的构建脚本必须保持纯 ASCII
（Windows PowerShell 5.1 会把无 BOM 的 .ps1 按系统代码页解码，中文会乱码），
而这份诊断报告需要大量中文说明。Python 处理 UTF-8 是可靠的。

用法：
    python scripts/diagnose_native_window.py                # 自动找安装目录
    python scripts/diagnose_native_window.py <安装目录或 exe>
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _resolve_exe(hint: str | None) -> Path | None:
    """找到 AI-3D-Forge.exe：优先用参数，其次找常见安装位置。"""
    candidates: list[Path] = []
    if hint:
        given = Path(hint)
        candidates.append(given if given.suffix.lower() == ".exe" else given / "AI-3D-Forge.exe")
    # 本脚本若随发布包一起分发，exe 就在同级或固定位置
    for base in (ROOT, ROOT / "build" / "release", ROOT / "build" / "dist" / "AI-3D-Forge"):
        candidates.append(base / "AI-3D-Forge.exe")
    for drive in ("D:", "C:", "E:"):
        candidates.append(Path(f"{drive}\\Program Files\\AI 3D Forge\\AI-3D-Forge.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _run(cmd: list[str], timeout: int = 180) -> str:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = proc.stdout or b""
        err = proc.stderr or b""
        text = ""
        for raw in (out, err):
            if not raw:
                continue
            try:
                text += raw.decode("utf-8")
            except UnicodeDecodeError:
                text += raw.decode("gbk", errors="replace")
        return text.strip() or f"(无输出，退出码 {proc.returncode})"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"(执行失败：{exc})"


def _reg_query(subkey: str, value: str) -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for prefix in ("", r"SOFTWARE\WOW6432Node"):
            try:
                with winreg.OpenKey(hive, f"{prefix}\\{subkey}" if prefix else subkey) as key:
                    data, _ = winreg.QueryValueEx(key, value)
                    if data:
                        return str(data)
            except OSError:
                continue
    return None


def collect(hint: str | None = None) -> str:
    """收集诊断信息，返回可直接保存的文本。"""
    lines: list[str] = []
    add = lines.append

    add("AI 3D Forge —— 原生窗口诊断报告")
    add("=" * 52)
    add(f"生成时间 : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    add(f"操作系统 : {platform.platform()}")
    add(f"Python   : {platform.python_version()}（本诊断脚本自身所用）")

    exe = _resolve_exe(hint)
    add(f"程序路径 : {exe or '(未找到 AI-3D-Forge.exe)'}")
    if exe is not None:
        add(f"程序版本 : {_version_of(exe)}")
    add("")

    # --- .NET Framework 4.x：原生窗口靠它提供 WinForms ---
    add("一、.NET Framework 4.x（原生窗口必需）")
    add("-" * 52)
    release = _reg_query(r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full", "Release")
    version = _reg_query(r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full", "Version")
    if release:
        add(f"Release  : {release}")
        add(f"Version  : {version}")
        try:
            add("判定     : 可用" if int(release) >= 528040 else "判定     : 版本偏低，可能缺少组件")
        except ValueError:
            add("判定     : 无法判断")
    else:
        add("未找到 .NET Framework 4.x —— 这就是原生窗口打不开的常见原因。")
        add("修复：安装 Windows 更新，或从微软官网下载 .NET Framework 4.8 运行时。")
    forms = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "System.Windows.Forms.dll"
    add(f"System.Windows.Forms.dll : {'存在' if forms.is_file() else '缺失'}")
    add("")

    # --- WebView2 运行时 ---
    # 不自己查注册表：程序本身的检测同时覆盖注册表与文件系统，更准。
    # 第三节会打印它的结论，这里只留一句说明，避免两处结论打架。
    add("二、WebView2 运行时（显示界面用）")
    add("-" * 52)
    add("见下方第三节「程序自身诊断」中的 WebView2 运行时一节。")
    add("若显示未安装：软件会自动改用系统浏览器（功能完全一致），")
    add("也可以安装微软官方的 WebView2 运行时（免费，约 2MB）。")
    add("")

    # --- 程序自身的逐层 import 诊断 ---
    add("三、程序自身诊断（逐层 import，能精确定位缺哪一步）")
    add("-" * 52)
    if exe is not None:
        # 子进程输出会出现「每行后跟一个空行」的现象，折叠掉以便阅读
        raw = _run([str(exe), "--diagnose-native-window"])
        add("\n".join(line for line in raw.splitlines() if line.strip()))
    else:
        add("(找不到程序，跳过)")
    add("")

    # --- 最近的运行日志 ---
    add("四、最近的运行日志")
    add("-" * 52)
    log = Path(os.environ.get("LOCALAPPDATA", "")) / "AI-3D-Forge" / "logs" / "aiforge.log"
    if log.is_file():
        add(f"日志位置 : {log}")
        try:
            tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
            add("\n".join(tail))
        except OSError as exc:
            add(f"(读取失败：{exc})")
    else:
        add(f"(没有找到日志文件：{log})")

    return "\n".join(lines)


def _version_of(exe: Path) -> str:
    if os.name != "nt":
        return "(非 Windows)"
    try:
        import winreg  # noqa: F401

        # 用 PowerShell 读版本资源太重，这里用 WMIC 的替代：直接看文件时间
        return datetime.fromtimestamp(exe.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return "(未知)"


def main() -> int:
    hint = sys.argv[1] if len(sys.argv) > 1 else None
    report = collect(hint)

    desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
    target = desktop / "AI-3D-Forge-诊断报告.txt"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(report, encoding="utf-8", newline="\r\n")
    except OSError:
        target = ROOT / "AI-3D-Forge-诊断报告.txt"
        target.write_text(report, encoding="utf-8", newline="\r\n")

    print(report)
    print()
    print("=" * 52)
    print(f"报告已保存到：{target}")
    print("请把该文件发给技术支持。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
