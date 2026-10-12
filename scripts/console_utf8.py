"""命令行脚本的启动兜底：`import console_utf8` 一行，把控制台输出修好。

为什么需要（实测踩到）：中文 Windows 的控制台默认是 GBK/936，脚本里一个
「−」（U+2212）就能让 print 抛 UnicodeEncodeError —— 自检跑到一半直接崩掉，
连「哪几项失败、退出码是什么」都看不到。发布门禁不该被一个和被测代码无关的
编码问题打断。

实现只有 `src/aiforge/stdio.py` 一份（应用与脚本共用，避免两处漂移）。
这里刻意**不**走 `import aiforge.stdio`：本仓库有些脚本（check_no_secrets、
check_ps_encoding 等）要在什么依赖都没装的裸 Python 里跑，走包导入会连带
拉起 `aiforge/__init__.py`。stdio.py 只用标准库，所以按文件路径单独加载。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_IMPL = Path(__file__).resolve().parents[1] / "src" / "aiforge" / "stdio.py"
BOOT_ERROR: BaseException | None = None


def _load():
    spec = importlib.util.spec_from_file_location("_aiforge_stdio", _IMPL)
    if spec is None or spec.loader is None:  # pragma: no cover - 文件在就一定有 loader
        raise ImportError(f"加载不了控制台兜底模块：{_IMPL}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


try:
    _stdio = _load()
except BaseException as exc:  # noqa: BLE001 - 兜底逻辑本身坏了也不能带倒脚本
    # 不打印：这个模块存在的理由就是“打印可能正好是会崩的那一步”。
    # 真正的守卫在 scripts/selfcheck.py「控制台编码不会打断自检」那条用例里。
    _stdio, BOOT_ERROR = None, exc

if _stdio is not None:
    # 导入即生效：这些脚本的共同入口，不需要每个脚本再写一遍调用
    _stdio.prepare()

    safe_print = _stdio.safe_print
    ensure_console_streams = _stdio.ensure_console_streams
    ensure_utf8_output = _stdio.ensure_utf8_output
    __all__ = ["safe_print", "ensure_console_streams", "ensure_utf8_output", "BOOT_ERROR"]
