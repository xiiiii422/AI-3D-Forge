"""标准输入输出流的编码与控制台兜底 —— 应用与命令行脚本共用一份实现。

为什么要有这个模块（三件事都只在 Windows 上出问题，而且都是「崩在报告结果的路上」）：

1. **GBK 控制台**：中文 Windows 的控制台默认代码页是 936（GBK）。Python 到 3.13
   为止**并没有**默认启用 UTF-8 模式（那是 3.15 起才有的事），所以 print 一个
   U+2212（数学减号「−」，自检明细里就有）会直接抛 UnicodeEncodeError。
   实际后果不是「少打一行字」：`scripts/selfcheck.py` 会在「设计规格与改动机制」
   那一项整个崩掉，连「哪些项失败、退出码是多少」都出不来 —— 发布门禁被
   一个和被测代码毫无关系的编码问题打断。
2. **没有控制台**：`--noconsole` 打包出来的 exe 在双击快捷方式 / `Start-Process`
   / 计划任务这种「没有继承控制台句柄」的启动方式下，`sys.stdout` 与 `sys.stderr`
   都是 `None`，任何 print 都会抛 `AttributeError`，进程以退出码 1 结束，
   而报错本身也没地方显示 —— 用户看到的是「跑完了，什么都没有，还失败了」。
3. **子进程继承**：OpenSCAD、pip、以及被自检拉起来的 python 子进程会按 ANSI 代码页
   编码自己的输出，所以这里同时把 `PYTHONIOENCODING` 设上，让子进程继承同一约定。

这里所有函数都**保证不抛异常**：这是输出兜底逻辑，不该成为新的失败源。
"""

from __future__ import annotations

import io
import os
import sys

_UTF8_ALIASES = ("utf8", "utf8mb4")


def _encoding_of(stream) -> str:
    return str(getattr(stream, "encoding", "") or "").lower().replace("-", "")


def ensure_console_streams() -> bool:
    """没有控制台标准流时，先挂回父进程控制台，再退化成可写的空流。

    返回是否做过修补。真没控制台时 `print` 依然静默丢弃，但不会再抛。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return False
    patched = False
    if os.name == "nt":
        try:
            import ctypes

            # ATTACH_PARENT_PROCESS：从 cmd / PowerShell 里跑脚本时能挂回那个控制台
            if ctypes.windll.kernel32.AttachConsole(-1):  # type: ignore[attr-defined]
                for name in ("stdout", "stderr"):
                    if getattr(sys, name, None) is None:
                        setattr(
                            sys,
                            name,
                            open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1),
                        )
                        patched = True
        except Exception:  # noqa: BLE001 - 挂不上就算了，下面还有兜底
            pass
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
                patched = True
            except OSError:
                pass
    return patched


def ensure_utf8_output() -> bool:
    """把 stdout / stderr 切成 UTF-8（写不出的字符用替代符，而不是抛异常）。

    返回是否改过流。`errors="replace"` 是关键：即使某个流被第三方测试框架换成
    了别的编码，也不该让一次打印决定整个进程的成败。
    """
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    changed = False
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            reconfigure = getattr(stream, "reconfigure", None)
            if _encoding_of(stream) in _UTF8_ALIASES:
                # 已经是 UTF-8：只要确认它不会因写不出字符而抛异常
                if reconfigure is not None and getattr(stream, "errors", "") != "replace":
                    reconfigure(errors="replace")
                    changed = True
                continue
            if reconfigure is not None:
                reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
                changed = True
                continue
            buffer = getattr(stream, "buffer", None)
            if buffer is not None:
                setattr(
                    sys,
                    name,
                    io.TextIOWrapper(buffer, encoding="utf-8", errors="replace", line_buffering=True),
                )
                changed = True
        except (AttributeError, ValueError, OSError):
            # 无控制台（windowed 模式）或流被测试框架换掉：忽略即可
            pass
    return changed


def prepare() -> None:
    """脚本/应用入口处调一次：先补流，再改编码。"""
    ensure_console_streams()
    ensure_utf8_output()


def safe_print(*objects: object, sep: str = " ", end: str = "\n", file=None) -> bool:
    """打印一行，且**绝不允许打印本身把流程打断**。

    返回是否真的写出去了。为什么需要：自检脚本的 `check()` 在失败分支上才组装出
    带异常消息的那行字，而异常消息里最可能带非 ASCII 字符 —— 也就是「只有出错的
    时候才会崩」，崩了以后就再也没有错误清单了。

    兜底顺序：正常打印 → 退化成 ASCII（非 ASCII 转成 ``\\uXXXX``）→ 静默放弃。
    第二档虽然读起来难看，但「哪一项失败」这类信息一定还在。
    """
    text = sep.join(str(obj) for obj in objects) + end
    try:
        print(text, end="", file=file)
        return True
    except Exception:  # noqa: BLE001 - 打印失败不该决定退出码
        pass
    try:
        stream = file if file is not None else (sys.stderr or sys.__stderr__)
        encoding = _encoding_of(stream)
        if encoding in _UTF8_ALIASES:
            # 流本身是 UTF-8 却仍失败（管道被关等），换个流没意义
            return False
        buffer = getattr(stream, "buffer", None) or getattr(sys.__stderr__, "buffer", None)
        if buffer is None:
            return False
        buffer.write(text.encode("ascii", "backslashreplace"))
        return True
    except Exception:  # noqa: BLE001
        return False
