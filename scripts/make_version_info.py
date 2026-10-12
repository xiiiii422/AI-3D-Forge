"""生成 Windows 可执行文件的版本资源（PyInstaller --version-file）。

正式交付时用户「右键 → 属性」能看到中文产品名与版本号，而不是空白。
由 scripts\\build.ps1 调用：
    python scripts/make_version_info.py
"""

from __future__ import annotations

import console_utf8  # noqa: F401,E402 —— 必须最先导入：GBK 控制台下 print 中文/符号会弄崩脚本（见 src/aiforge/stdio.py）
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiforge.version import APP_NAME, APP_PUBLISHER, APP_VERSION  # noqa: E402

TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({v0}, {v1}, {v2}, 0),
    prodvers=({v0}, {v1}, {v2}, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404b0',
        [StringStruct('CompanyName', '{company}'),
         StringStruct('FileDescription', '{description}'),
         StringStruct('FileVersion', '{version}'),
         StringStruct('InternalName', 'AI-3D-Forge'),
         StringStruct('LegalCopyright', '{company}'),
         StringStruct('OriginalFilename', 'AI-3D-Forge.exe'),
         StringStruct('ProductName', '{product}'),
         StringStruct('ProductVersion', '{version}')])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def main() -> int:
    parts = (APP_VERSION.split(".") + ["0", "0", "0"])[:3]
    numbers = []
    for part in parts:
        digits = "".join(ch for ch in part if ch.isdigit())
        numbers.append(int(digits) if digits else 0)

    payload = TEMPLATE.format(
        v0=numbers[0],
        v1=numbers[1],
        v2=numbers[2],
        company=APP_PUBLISHER,
        product=APP_NAME,
        description=f"{APP_NAME} —— AI 3D 打印设计工具",
        version=APP_VERSION,
    )

    target = ROOT / "build" / "version_info.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")
    print(f"已生成 {target}")
    if os.environ.get("AIFORGE_VERBOSE") == "1":
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
