"""支持 `python -m aiforge` 启动（与 python main.py 等价）。"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
