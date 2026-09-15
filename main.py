"""AI 3D Forge —— 程序入口（开发与发布版共用）。

普通用户永远不需要看到这个文件，也不需要执行 `python main.py`：
发布版是 AI-3D-Forge.exe，双击即用（见 README-用户.md）。

用法（仅开发者）：
    python main.py                 启动桌面窗口
    python main.py --browser       用系统浏览器打开
    python main.py --port 8899     指定端口
    python main.py --check         只做环境检测并打印结果（排障用）
    python main.py --server        只启动服务，不开窗口（服务器/调试）
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 允许以源码方式直接运行：把 src 加入模块搜索路径
_ROOT = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_ROOT, "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="AI-3D-Forge",
        description="AI 3D Forge —— 用一句话生成可 3D 打印的模型",
    )
    parser.add_argument("--browser", action="store_true", help="使用系统浏览器而不是内置窗口")
    parser.add_argument("--port", type=int, default=None, help="指定本机服务端口（默认自动选择 8760 起）")
    parser.add_argument("--server", action="store_true", help="只启动本机服务，不打开界面")
    parser.add_argument("--check", action="store_true", help="执行环境检测并打印结果")
    parser.add_argument(
        "--diagnose-native-window",
        action="store_true",
        help="诊断原生窗口组件（排障用：逐层 import 并打印真实错误）",
    )
    parser.add_argument("--install-openscad", action="store_true", help="自动安装 OpenSCAD 后退出")
    parser.add_argument("--first-init", action="store_true", help="安装器调用的首次初始化（建目录 + 环境自检，静默）")
    parser.add_argument(
        "--first-run-setup",
        action="store_true",
        help="安装器调用的首次准备（可见进度：建目录、检测并自动安装 OpenSCAD、自检）",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="把 --first-run-setup 的输出同时写入该文件（安装器用它留日志，不经过 shell 重定向）",
    )
    parser.add_argument("--version", action="store_true", help="显示版本信息")
    return parser


def _ensure_utf8_output() -> None:
    """让命令行输出使用 UTF-8，并让子进程继承同样的约定。

    中文 Windows 的控制台默认是 GBK/936。Python 3.13 虽然默认 UTF-8 模式，
    但 --version / --check 这类命令行输出仍可能被控制台按 GBK 解读而显示成乱码。
    这里显式把标准流切成 UTF-8，并设置 PYTHONIOENCODING 供子进程继承
    （对 Windows 这种「子进程输出按 ANSI 代码页编码」的环境尤其重要）。
    """
    import io

    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            if getattr(stream, "encoding", "").lower().replace("-", "") not in ("utf8", "utf8mb4"):
                buffer = getattr(stream, "buffer", None)
                if buffer is not None:
                    setattr(sys, name, io.TextIOWrapper(buffer, encoding="utf-8", errors="replace", line_buffering=True))
        except (AttributeError, ValueError, OSError):
            # 无控制台（windowed 模式）时 std 流可能为 None，忽略即可
            pass


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_output()
    args = _build_parser().parse_args(argv)

    from aiforge import logging_setup
    from aiforge.version import APP_NAME, APP_VERSION, CHANNEL_LABEL, os_label, python_version

    logging_setup.setup(console=args.check or args.server or args.install_openscad)

    # 子进程守护要在**所有入口**上生效，而不是只在"启动服务"那条路上。
    # 实测踩到：`--check` 不经过 start_server，于是「系统诊断」里那条
    # 「后台进程守护」显示成"当前环境不支持" —— 明明是我们自己没装。
    # 一条会误报的诊断比没有诊断更糟：用户会去查一个不存在的问题。
    # install() 是幂等的（已经建好作业对象就直接返回），放在这里没有副作用。
    from aiforge import server as _server

    _server.install_child_guard()

    if args.version:
        print(f"{APP_NAME} {APP_VERSION}（{CHANNEL_LABEL}）")
        print(f"运行环境：Python {python_version()}")
        print(f"操作系统：{os_label()}")
        return 0

    if args.check:
        return _run_check()

    if args.diagnose_native_window:
        return _diagnose_native_window()

    if args.first_init:
        return _first_init()

    if args.first_run_setup:
        return _first_run_setup(args.log_file)

    if args.install_openscad:
        return _install_openscad()

    from aiforge import single_instance, server

    if not single_instance.acquire():
        pid = single_instance.running_pid()
        print(f"{APP_NAME} 已经在运行中（进程号 {pid}）。请切换到已打开的窗口。")
        return 0

    try:
        return server.run(
            port=args.port,
            browser_only=args.browser or args.server,
            open_browser=True if args.server else None,
        )
    finally:
        single_instance.release()


def _run_check() -> int:
    """命令行环境自检：给技术支持用的可复制文本输出。"""
    import asyncio

    from aiforge import envcheck, openscad as scad_cli
    from aiforge.paths import paths
    from aiforge.version import APP_NAME, APP_VERSION, CHANNEL_LABEL

    print("=" * 66)
    print(f"{APP_NAME} {APP_VERSION}（{CHANNEL_LABEL}）环境检测")
    print("=" * 66)

    files = paths()
    print(f"用户配置目录：{files.data_dir}")
    print(f"项目保存目录：{files.projects_dir}")
    print(f"运行组件目录：{files.runtime_dir}")
    print("-" * 66)

    report = asyncio.run(envcheck.build_report(use_cache=False, deep=True))
    icons = {"ok": "✓", "warn": "⚠", "fail": "✗", "checking": "…"}
    for item in report["items"]:
        print(f"{icons.get(item['status'], '?')} {item['title']}: {item['detail']}")
        if item.get("action"):
            print(f"    → {item['action']}")

    print("-" * 66)
    print(f"结论：{report['ok_count']}/{report['total']} 项通过（overall={report['overall']}）")

    exe = scad_cli.resolve_executable()
    print(f"OpenSCAD 可执行文件：{exe or '未找到'}")
    if exe is None:
        print("手动安装指引：")
        for step in scad_cli.manual_instructions()["steps"]:
            print(f"  - {step}")
    return 0


def _first_init() -> int:
    """安装器在安装完成后调用：创建用户目录、记录安装位置、跑一次环境自检。

    刻意做得「绝不失败」：任何异常都只记录日志并返回 0，
    以免安装流程因为一次自检报错而让用户以为安装失败。
    """
    import logging

    from aiforge import envcheck
    from aiforge.config import store
    from aiforge.paths import install_root, paths

    log = logging.getLogger("first_init")
    try:
        files = paths().ensure()
        log.info("用户数据目录：%s", files.data_dir)
        log.info("项目保存目录：%s", files.projects_dir)
        log.info("程序安装目录：%s", install_root())

        # 记录安装位置，便于「系统诊断」里展示与排查
        cfg = store()
        cfg.config.ui.theme = cfg.config.ui.theme or "light"
        cfg.save()

        import asyncio

        report = asyncio.run(envcheck.build_report(use_cache=False, live=False, deep=False))
        log.info("首次环境自检：%s/%s 项通过", report["ok_count"], report["total"])
        for item in report["items"]:
            log.info("  [%s] %s：%s", item["status"], item["title"], item["detail"])
    except Exception:  # noqa: BLE001 - 首次初始化永不让安装失败
        log.exception("首次初始化过程中出现问题（不影响安装）")
    return 0


def _first_run_setup(log_path: str | None = None) -> int:
    """安装器在复制完文件后调用：把「首次准备」做完，并让用户看得见。

    与旧的 --first-init 的区别：
      * 旧版是 runhidden 静默执行，用户看到安装条卡在那里，不知道在干什么；
      * 这一步会**自动把 OpenSCAD 装好**（下载 20MB），因此必须显示进度，
        否则用户会以为安装卡死。装完之后第一次打开软件就能直接出模型，
        不需要在欢迎向导里再等一次下载。

    日志写法（重要）：由程序自己写文件，**不依赖 shell 重定向**。
    安装器原先用 `cmd /C "...exe" --first-run-setup >> "..." 2>&1`，
    实测 cmd 在这种引用+重定向下会直接报
    "The filename, directory name, or volume label syntax is incorrect" 并返回 1，
    日志一个字节都没有。去掉 shell 这一层之后行为完全确定。

    任何时候都返回 0：准备工作失败不该让用户以为「安装失败」——
    软件本身是可用的，缺 OpenSCAD 时首次启动的向导会再引导一次。
    """
    import logging

    log = logging.getLogger("first_run_setup")

    # 安装器传来的日志文件：把所有面向用户的行同时写进这里，
    # 这样即使安装器窗口一闪而过，用户与技术支持仍能看到发生了什么。
    sink = None
    if log_path:
        try:
            target = Path(log_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            sink = target.open("w", encoding="utf-8", newline="\n")
        except OSError as exc:
            logging.getLogger(__name__).warning("无法写入首次准备日志 %s：%s", log_path, exc)

    def emit(text: str) -> None:
        # 这行会被 Inno Setup 的进度页实时读到，是用户可见反馈
        print(text, flush=True)
        if sink is not None:
            try:
                sink.write(text + "\n")
                sink.flush()
            except OSError:
                pass

    emit("AI 3D Forge 首次准备")
    emit("=" * 46)

    from aiforge import envcheck
    from aiforge.config import store
    from aiforge.paths import install_root, paths

    try:
        emit("正在创建用户数据目录…")
        files = paths().ensure()
        log.info("用户数据目录：%s", files.data_dir)
        log.info("项目保存目录：%s", files.projects_dir)
        cfg = store()
        cfg.save()
    except Exception:  # noqa: BLE001
        log.exception("创建用户目录失败（不影响安装）")
        emit("  未能创建用户数据目录，首次启动时会重试。")
        _close_quietly(sink)
        return 0

    # --- OpenSCAD：能自动装就直接装好，让用户开箱即用 ---------------------
    from aiforge import openscad as scad_cli

    # 自动化/受管环境可以跳过下载：这台机器上 21MB 可能要几分钟，
    # 批量部署或验收测试不希望卡在这里。用户侧无需关心这个开关。
    if os.environ.get("AIFORGE_SKIP_OPENSCAD_DOWNLOAD") == "1":
        emit("已按环境变量跳过 OpenSCAD 下载（AIFORGE_SKIP_OPENSCAD_DOWNLOAD=1）。")
        emit("首次打开软件时可以在向导里安装它。")
        _finish_first_run(emit, log, skip_openscad=True)
        _close_quietly(sink)
        return 0

    try:
        info = scad_cli.detect(deep=True)
    except Exception:  # noqa: BLE001
        log.exception("检测 OpenSCAD 失败")
        info = None

    if info is not None and info.found and info.cli_ok:
        emit(f"已检测到 OpenSCAD {info.version}，无需安装。")
    else:
        auto = store().config.openscad.auto_install
        if not auto:
            emit("已跳过 OpenSCAD 安装（设置中已关闭自动安装）。")
        else:
            emit("未检测到 OpenSCAD（3D 建模引擎），正在自动安装…")
            emit("  这一步需要从官方源下载约 21MB，请保持网络连接。")
            last_line = {"text": ""}

            def progress(stage: str, percent: float | None, message: str, extra: dict) -> None:
                if message == last_line["text"]:
                    return
                last_line["text"] = message
                suffix = f"  {percent:.0f}%" if percent is not None else ""
                emit(f"  {message}{suffix}")

            try:
                installed = scad_cli.install("auto", progress)
                emit(f"OpenSCAD {installed.version or ''} 已安装并配置完成。")
            except scad_cli.OpenScadError as exc:
                # 装不上不是安装失败：软件可用，向导会再引导一次
                log.warning("自动安装 OpenSCAD 未成功：%s", exc.message)
                emit("  自动安装未成功，这不影响软件安装。")
                emit(f"  原因：{exc.message}")
                emit("  首次打开软件时可以在向导里再试一次，或按提示手动安装。")
            except Exception:  # noqa: BLE001
                log.exception("自动安装 OpenSCAD 时出现异常")
                emit("  自动安装未成功，这不影响软件安装。")

    _finish_first_run(emit, log)
    _close_quietly(sink)
    return 0


def _close_quietly(handle) -> None:
    if handle is None:
        return
    try:
        handle.close()
    except OSError:
        pass


def _finish_first_run(say, log, *, skip_openscad: bool = False) -> None:
    """首次准备的收尾：环境自检 + 明确的完成提示（写进日志）。"""
    from aiforge import envcheck
    from aiforge.paths import install_root

    try:
        say("正在检查运行环境…")
        import asyncio

        report = asyncio.run(envcheck.build_report(use_cache=False, live=False, deep=False))
        log.info("首次环境自检：%s/%s 项通过", report["ok_count"], report["total"])
        for item in report["items"]:
            log.info("  [%s] %s：%s", item["status"], item["title"], item["detail"])
        problems = [i for i in report["items"] if i["status"] == "fail"]
        if skip_openscad:
            say("  已跳过 3D 引擎安装，其余检查照常完成。")
        elif problems:
            say(f"  有 {len(problems)} 项需要注意，首次启动时会引导你处理。")
        else:
            say(f"  环境检查通过（{report['ok_count']}/{report['total']}）。")
    except Exception:  # noqa: BLE001
        log.exception("首次环境自检失败（不影响安装）")

    say("=" * 46)
    say("准备完成，可以开始使用了。")
    # 机器可读的完成标记：安装验收脚本与技术支持用它判断这一步真的跑完了。
    # 之所以需要它：验收脚本是纯 ASCII 的（PS 5.1 编码所限），无法可靠匹配中文行。
    say("FIRST_RUN_SETUP_OK")
    log.info("首次准备结束，程序安装目录：%s", install_root())


def _diagnose_native_window() -> int:
    """诊断原生窗口（WebView2）能否真正使用。

    排障用：把「打包版里到底缺哪个模块/哪一步失败」打印出来。
    这类问题只在发布版上复现（构建机 site-packages 里有东西），
    所以必须能在打包版自身环境里跑诊断，而不是靠猜。
    """
    import platform
    import traceback

    from aiforge import webview_runtime

    print("AI 3D Forge 原生窗口诊断")
    print("=" * 52)
    print(f"frozen        : {getattr(sys, 'frozen', False)}")
    print(f"executable    : {sys.executable}")
    print(f"python        : {platform.python_version()}")
    print(f"meipass       : {getattr(sys, '_MEIPASS', '(none)')}")

    status = webview_runtime.cached_status(refresh=True)
    print()
    print("WebView2 运行时：")
    print(f"  installed   : {status.installed}")
    print(f"  version     : {status.version}")
    print(f"  source      : {status.source}")
    print(f"  detail      : {status.detail}")

    # 逐层 import，才能定位到底是哪一层缺东西
    print()
    print("逐层 import：")
    for module in ("proxy_tools", "clr_loader", "pythonnet", "webview"):
        try:
            imported = __import__(module)
            version = getattr(imported, "__version__", "?")
            print(f"  [OK]   {module} {version}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {module}: {type(exc).__name__}: {exc}")

    print()
    try:
        import webview

        print("webview.start 可行性：可以导入 webview")
        # 真正会失败的一步是 start()（它才去加载 GUI 后端）
        gui = os.environ.get("PYWEBVIEW_GUI", "(auto)")
        print(f"GUI 后端（环境变量 PYWEBVIEW_GUI）：{gui}")
        print("提示：设 PYWEBVIEW_GUI=edgechromium 可强制使用 WebView2 后端。")
    except Exception:
        print("webview 无法导入，完整回溯：")
        traceback.print_exc()
    return 0


def _install_openscad() -> int:
    from aiforge import openscad as scad_cli

    def progress(stage: str, percent: float | None, message: str, extra: dict) -> None:
        suffix = f"（{percent:.0f}%）" if percent is not None else ""
        print(f"[{stage}]{suffix} {message}")

    try:
        info = scad_cli.install("auto", progress)
    except scad_cli.OpenScadError as exc:
        print(f"自动安装失败：{exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"建议：{exc.hint}", file=sys.stderr)
        print("\n手动安装步骤：")
        for step in scad_cli.manual_instructions()["steps"]:
            print(f"  - {step}")
        return 5
    print(f"\nOpenSCAD {info.version or ''} 已安装并配置完成。")
    print(f"位置：{info.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
