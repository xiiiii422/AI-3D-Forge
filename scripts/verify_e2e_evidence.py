"""端到端日志的证据核对：确认「贴合意图 + 只改该改的」这套机制真的在跑。

为什么单独一支：`scripts/e2e_test.py` 自己会断言行为，但它打印的过程日志
（规格卡片推送、等待确认、变更契约、AI 复核）才是「机制**被真实触发过**」
的直接证据。仅看「全部通过」无法区分「机制生效」和「机制被跳过」。

用法（先跑过 scripts/e2e_test.py）：
    & build\\venv\\Scripts\\python.exe scripts\\verify_e2e_evidence.py

重要坑：`Tee-Object` 写出的日志是 **UTF-16LE**（带 BOM），不是 UTF-8。
按 UTF-8 读会得到一堆 NUL 字符、任何中文关键词都匹配不到 ——
排查时先看文件前两个字节，别怀疑自己的关键词写错了。
"""

from __future__ import annotations

import console_utf8  # noqa: F401,E402 —— 必须最先导入：GBK 控制台下 print 中文/符号会弄崩脚本（见 src/aiforge/stdio.py）
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = ROOT / "build" / "e2e-latest.log"

# (目标条款, 说明, 日志里必须出现的证据)
REQUIRED: list[tuple[str, str, str]] = [
    ("①", "把需求解析成规格并推给界面", "[规格]"),
    ("①", "生成前进入「等待确认」状态", "等待你确认我对需求的理解"),
    ("①", "界面侧断言：生成前先确认理解", "生成前先确认「我对需求的理解」"),
    ("①", "界面侧断言：等待确认时快照自带规格", "等待确认时快照自带规格"),
    ("②", "规格指纹回写到源码注释", "// @spec"),
    ("③", "修改前产出变更契约", "本次只允许修改"),
    ("④", "AI 复核（第二道校验）实际执行", "AI 复核"),
    ("④", "程序化核对给出结论", "改动通过契约校验"),
    ("⑤", "规格随项目持久化", "项目里存下了设计规格"),
    ("⑦", "只改点名项（源码级）", "源码里除点名项外"),
    ("⑦", "越界模型被确定性兜底装进机器", "程序自动把越界模型"),
    ("⑦", "规格持久化断言", "设计规格已保留并随项目持久化"),
]

# 最终汇总行的格式；用它确认这次跑到了结尾
SUMMARY_RE = re.compile(r"端到端验收：通过 (\d+) 项，失败 (\d+) 项")


def read_log(path: Path) -> tuple[str, str]:
    """按 BOM 自动判断编码读日志，返回 (文本, 编码名)。"""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace"), "utf-16"
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig", errors="replace"), "utf-8-sig"
    return raw.decode("utf-8", errors="replace"), "utf-8"


def main() -> int:
    log_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LOG
    if not log_path.is_file():
        print(f"[FAIL] 找不到端到端日志：{log_path}")
        print("       请先运行： python scripts\\e2e_test.py")
        return 2

    text, encoding = read_log(log_path)
    print("=" * 74)
    print("端到端机制证据核对（确认机制真的被触发，而不只是「测试通过」）")
    print("=" * 74)
    print(f"日志：{log_path}")
    print(f"编码：{encoding}   大小：{log_path.stat().st_size} 字节")
    print("-" * 74)

    failures: list[str] = []
    for clause, label, needle in REQUIRED:
        count = text.count(needle)
        flag = "OK  " if count > 0 else "FAIL"
        if count == 0:
            failures.append(f"{clause} {label}（缺少证据：{needle!r}）")
        print(f"  [{flag}] {clause} {label:32} 命中 {count} 次")

    print("-" * 74)
    summary = SUMMARY_RE.search(text)
    if summary is None:
        failures.append("日志里没有端到端汇总行（这次运行可能没跑完）")
        print("  [FAIL] 没找到汇总行 —— 这次端到端可能没跑完")
    else:
        passed, failed = int(summary.group(1)), int(summary.group(2))
        print(f"  端到端汇总：通过 {passed} 项，失败 {failed} 项")
        if failed:
            failures.append(f"端到端自身有 {failed} 项失败")

    print("=" * 74)
    if failures:
        print("证据核对未通过：")
        for item in failures:
            print(f"  ✗ {item}")
        print("=" * 74)
        return 1
    print(f"机制证据齐备：{len(REQUIRED)} 条全部命中，端到端全绿。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
