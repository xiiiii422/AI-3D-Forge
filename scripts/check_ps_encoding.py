"""诊断 .ps1 的「GBK 误读吞掉换行」问题（Windows PowerShell 5.1 专用陷阱）。

机制（已用真实报错验证）：
  Windows PowerShell 5.1 把无 BOM 的 .ps1 按系统 ANSI 代码页解码。本机为 936/GBK。
  某个中文标点的 UTF-8 字节如果在 GBK 下「先组成一个双字节汉字、再多出一个尾字节」，
  而这个尾字节恰好在 ASCII 可打印范围内、且被解析成反引号(0x60)时，
  该反引号就成了续行符 —— 当前行与下一行被合并。

  实例：「）」的 UTF-8 是 EF BC 89，若其后紧跟换行 0A：
        GBK 把 EF BC 读成一个汉字，剩下 89 0A；
        0x89 不是合法的 GBK 首字节，于是 0A 被当作独立字节解码成 0x0A -> 反引号。
        结果：以中文标点结尾的注释会吃掉下一行代码，被吃掉的语句凭空消失。

  这就是「脚本跑到某处就静默结束、既不报语法错也不继续」的根因，极难排查。

关键：本检查必须**按字节手工解码并保留反引号**，不能用 errors="replace" ——
      替换模式会把这个 0x0A 变成 U+FFFD，从而漏报（这正是第一版扫描器失效的原因）。

用法：
    python scripts/check_ps_encoding.py          # 只检查
    python scripts/check_ps_encoding.py --fix    # 把会吞行的行改成 ASCII 注释

约定：本项目的 .ps1 一律**全 ASCII（含注释）**，中文只出现在被调用的 Python 程序输出里。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
BACKTICK = 0x60


def gbk_decode_like_powershell(line: bytes) -> str:
    """按 GBK 规则逐字节解码，**保留**非法的尾字节为对应 ASCII 字符。

    这是还原 PowerShell 5.1 实际所见的关键：遇到「合法的双字节汉字」就吃掉两字节，
    否则单字节按 Latin-1 输出（因此 0x0A 会变成反引号而不是替换字符）。
    """
    out: list[str] = []
    index = 0
    length = len(line)
    while index < length:
        byte = line[index]
        if byte < 0x80:
            out.append(chr(byte))
            index += 1
            continue
        if index + 1 < length:
            pair = line[index : index + 2]
            try:
                out.append(pair.decode("gbk"))
                index += 2
                continue
            except UnicodeDecodeError:
                pass
        # 非法序列：按单字节原样保留，这样隐含的控制字符不会被抹掉
        out.append(chr(byte))
        index += 1
    return "".join(out)


def find_hazards(path: Path) -> list[tuple[int, bytes, str, str]]:
    """找出「UTF-8 行按 GBK 解码后，末尾多出一个反引号」的行。

    这是 Windows PowerShell 5.1 的实际行为：它把文件按 GBK 解码，行尾那个
    「GBK 无法组成完整双字节汉字的多余字节」如果落在 0x60，就被当成续行符，
    于是下一行被合并进来 —— 被合并掉的往往是紧接着的一条语句，凭空消失。

    Returns: (行号, 原始字节, UTF-8 原文, GBK 解码结果)
    """
    raw = path.read_bytes()
    findings: list[tuple[int, bytes, str, str]] = []
    for index, line in enumerate(raw.split(b"\n"), start=1):
        if all(byte < 0x80 for byte in line):
            continue
        decoded = gbk_decode_like_powershell(line)
        if decoded.endswith(chr(BACKTICK)):
            findings.append(
                (index, line, line.decode("utf-8", errors="replace"), decoded)
            )
    return findings


def ascii_comment(line: bytes) -> bytes:
    """把一行含中文的注释替换为 ASCII 注释，保留缩进。"""
    text = line.decode("utf-8", errors="replace").rstrip("\r").rstrip()
    stripped = text.lstrip()
    if not stripped.startswith("#"):
        raise SystemExit(
            "这行含代码且非 ASCII，无法自动改写，请人工处理：\n  " + text
        )
    indent = text[: len(text) - len(stripped)]
    return (indent + "# NOTE: keep .ps1 files ASCII-only (see README-开发者.md section 10)").encode("ascii")


def make_ascii(line: bytes) -> bytes:
    """把一个含中文的行转成等价的纯 ASCII 行。

    注释行：整体替换为一句 ASCII 说明（保留信息量，不丢上下文）。
    含代码的行：只把行内非 ASCII 部分删掉 —— 但本项目约定不允许这种行存在，
    因此遇到就报错让人工处理，避免自动改写把逻辑弄坏。
    """
    text = line.decode("utf-8", errors="replace").rstrip(b"\r".decode())
    stripped = text.lstrip()
    if stripped.startswith("#"):
        indent = text[: len(text) - len(stripped)]
        return (indent + "# (see README-开发者.md: keep .ps1 files ASCII-only)").encode("ascii")
    raise SystemExit(
        "拒绝自动修改含代码的非 ASCII 行，请人工处理：\n  " + text
    )


def main() -> int:
    apply_fix = "--fix" in sys.argv
    targets = sorted(SCRIPTS_DIR.glob("*.ps1")) + sorted(ROOT.glob("*.ps1"))

    total = 0
    for path in targets:
        hazards = find_hazards(path)
        if not hazards:
            continue
        print(f"\n=== {path.name}: {len(hazards)} 行会在 PS 5.1 下吞掉下一行 ===")
        for line_no, _line, text, decoded in hazards:
            print(f"  L{line_no}")
            print(f"    UTF-8   : {text.strip()}")
            print(f"    PS5.1   : {decoded.strip()}  <-- 行尾反引号 = 续行符")
        total += len(hazards)

        if apply_fix:
            raw = path.read_bytes()
            lines = raw.split(b"\n")
            for line_no, line, _text, _decoded in hazards:
                lines[line_no - 1] = ascii_comment(line)
            path.write_bytes(b"\n".join(lines))
            print(f"    -> 已把 {len(hazards)} 行改成 ASCII 注释")

    if total == 0:
        print("OK: 所有 .ps1 都不含会吞掉下一行的行。")
        return 0

    if apply_fix:
        print(f"\n已修复 {total} 行。请重新运行构建确认。")
        return 0

    print(f"\n共 {total} 行有风险。运行 `python scripts/check_ps_encoding.py --fix` 修复。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
