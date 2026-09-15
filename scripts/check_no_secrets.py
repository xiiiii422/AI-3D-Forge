"""开源前的敏感信息扫描：**当前文件** 与 **Git 历史** 都要查。

为什么必须两支都查（这是开源翻车的典型姿势）：
  只扫工作区文件是不够的 —— 密钥可能在第 3 次提交里被写进去、第 5 次提交删掉，
  当前文件看起来干干净净，但 `git log -p` 里它还在，任何人 clone 都能翻出来。

检查项（对应需求「开源前安全检查」的清单）：
  * DeepSeek / OpenAI 形状的 API Key（`sk-` + 32 位十六进制）
  * 通用长令牌、`Authorization: Bearer xxx`、Cookie、password/secret 赋值
  * 私钥块（`-----BEGIN ... PRIVATE KEY-----`）
  * 带口令的连接串（`scheme://<user>:<password>@<host>`）
  * `.env` 类文件被提交
  * 本机绝对路径（`C:\\Users\\<用户名>`、仓库自身绝对路径）
  * 不该进仓库的生成物（build/、dist/、node_modules/、日志、安装包）

规则本身**不能硬编码本机路径或用户名**（那样规则自己就是泄漏），
所以本机特征在运行时推导：当前用户目录 + 当前仓库根目录。

用法：
    python scripts/check_no_secrets.py                  # 只扫当前文件（默认，快）
    python scripts/check_no_secrets.py --history        # 再扫一遍 Git 历史（发布前必做）
    python scripts/check_no_secrets.py --paths-only      # 只查"不该提交的路径"
    python scripts/check_no_secrets.py --quiet           # 只输出结论行
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 单个文件超过这个大小就不读（二进制/大文件不是密钥的藏身处，读了只会拖慢）
MAX_TEXT_BYTES = 4 * 1024 * 1024

# 允许出现的「像密钥但其实是示例」的写法。宁可漏报也不要天天误报 ——
# 一条老是误报的守卫最后一定会被人关掉。
ALLOW_MARKERS = (
    "<your-key>",
    "<password>",
    "<token>",
    "YOUR_API_KEY",
    "sk-xxxx",
    "sk-1234****cdef",  # 文档里的掩码示例
    "sk-mock1234567890abcdef",  # 前端假后端用的固定假 Key
    "sk-selftest",  # 自检里构造的假 Key
    "EXAMPLE",
)

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("完整 API Key（sk- + 32 位十六进制）", re.compile(r"sk-[0-9a-fA-F]{32}")),
    ("疑似长令牌（sk-/ghp_/xox/bearer 形状）", re.compile(
        r"(?:sk-[A-Za-z0-9]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{10,})"
    )),
    ("Authorization / Bearer 头里带字面量", re.compile(
        r"(?i)authorization\s*[:=]\s*['\"]?(?:bearer|basic)\s+[A-Za-z0-9._\-]{16,}"
    )),
    ("Cookie 请求头里带字面量", re.compile(r"(?i)cookie\s*[:=]\s*['\"][^'\"]{16,}['\"]")),
    ("password / secret / token 赋了字面量", re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token)\b\s*[:=]\s*"
        r"['\"][^'\"\s]{8,}['\"]"
    )),
    ("私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("带口令的连接串", re.compile(r"(?i)\b[a-z][a-z0-9+.\-]{2,}://[^\s:/@]+:[^\s:/@]{3,}@[^\s/]+")),
]

# 那个「带口令的连接串」的示例写法（本文档第 11 行）用一个明显是占位符的形式，
# 见 ALLOW_MARKERS 里的 <password>。

# 不该被提交的路径（要么是生成物，要么可能含密钥）
FORBIDDEN_PATH_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # `.env` / `.env.local` / `prod.env` —— 这类文件按惯例就是放密钥的
    (".env 类文件", re.compile(r"(?i)(^|/)\.env($|\.|/)|\.env$")),
    ("密钥/凭据文件", re.compile(r"(?i)(^|/)(secrets?|credentials?)(\.|/|$)")),
    ("生成物 build/dist", re.compile(r"(?i)(^|/)(build|dist|node_modules)/")),
    ("日志/临时文件", re.compile(r"(?i)\.(log|tmp|bak|pyc|pyo)$")),
    ("安装包 / 压缩产物", re.compile(r"(?i)\.(exe|msi|zip|7z|stl|3mf)$")),
    ("私钥文件", re.compile(r"(?i)\.(pem|p12|pfx|key)$")),
]

# 这些「看似违规」的文件是项目自身的一部分
PATH_ALLOWLIST = (
    "scripts/check_no_secrets.py",  # 本文件自己写着这些规则
    ".gitignore",
    ".gitattributes",
)


def shipped_files() -> list[Path]:
    """要扫描的文件：= 「会进仓库的文件」。

    优先问 git（`--cached --others --exclude-standard` 同时覆盖已跟踪与
    未跟踪但**没有被 .gitignore 排除**的文件）—— 这样在还没有提交的开发目录里
    也能得到正确的集合，而且自动尊重 .gitignore（截图产物、安装包、
    构建目录都不会被误报成"提交了生成物"）。

    没有 git 时退回目录遍历，并显式跳过同一批目录。
    """
    tracked = _git(["ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT)
    if tracked:
        files = []
        for line in tracked.splitlines():
            rel = line.strip()
            if not rel:
                continue
            path = ROOT / rel
            if path.is_file():
                files.append(path)
        if files:
            return files

    skip_dirs = {
        ".git", "build", "node_modules", "__pycache__", ".venv", "venv",
        ".vite", "dist", ".pytest_cache", ".ruff_cache", ".mypy_cache",
        "shots",  # webui/scripts/shots：界面截图产物
        "Output",  # installer/Output：安装包产物
    }
    found: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.relative_to(ROOT).parts):
            continue
        found.append(path)
    return found


def _git(args: list[str], cwd: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout or ""


def _personal_needles() -> list[tuple[str, str]]:
    """在运行时推导本机特征，避免把本机路径/用户名写进规则本身。"""
    needles: list[tuple[str, str]] = [(str(ROOT), "本仓库绝对路径")]
    home = Path.home()
    if home.name and home.name.lower() not in {"public", "default", "all users"}:
        needles.append((str(home), "本机用户目录"))
    return needles


def _skip_line(line: str) -> bool:
    return any(marker in line for marker in ALLOW_MARKERS)


def scan_text(path: Path, text: str) -> list[str]:
    findings: list[str] = []
    rel = path.relative_to(ROOT).as_posix()
    for label, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            snippet = match.group(0)
            if _skip_line(snippet):
                continue
            line_no = text[: match.start()].count("\n") + 1
            findings.append(f"{rel}:{line_no}: {label} → {snippet[:24]}…")
            break  # 同一文件同一类问题报一条就够
    for needle, label in _personal_needles():
        for variant in (needle, needle.replace("\\", "/")):
            if variant and variant in text.replace("\\\\", "\\"):
                findings.append(f"{rel}: 含{label}（{variant}）")
                break
    return findings


def scan_files(quiet: bool) -> tuple[list[str], int]:
    problems: list[str] = []
    files = shipped_files()
    scanned = 0
    for path in files:
        try:
            if path.stat().st_size > MAX_TEXT_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "\x00" in text[:1024]:  # 二进制
            continue
        scanned += 1
        problems.extend(scan_text(path, text))
    if not quiet:
        print(f"  已扫描 {scanned} 个文本文件")
    return problems, scanned


def scan_paths(quiet: bool) -> list[str]:
    problems: list[str] = []
    files = shipped_files()
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        if rel in PATH_ALLOWLIST:
            continue
        for label, pattern in FORBIDDEN_PATH_PATTERNS:
            if pattern.search(rel):
                # webui/scripts/shots 之类由 .gitignore 排除，不属于"被提交"的集合
                problems.append(f"{rel}: {label} —— 这类文件不该进仓库")
                break
    if not quiet:
        print(f"  已检查 {len(files)} 个文件路径")
    return problems


def scan_history(quiet: bool) -> list[str]:
    """扫描 Git 历史：新增行里出现过的密钥/隐私特征。

    只看 `+` 开头的行：删除行上的命中说明它已经被删掉了，不构成泄漏
    （但仍会在报告里以「历史中出现过」提示，因为它还留在历史里可被翻出）。
    """
    if not (ROOT / ".git").exists() and not _git(["rev-parse", "--git-dir"], cwd=ROOT):
        if not quiet:
            print("  当前目录不是 git 仓库，跳过历史扫描")
        return []

    log = _git(["log", "--all", "-p", "--no-color", "--pretty=format:__COMMIT__%h %s"], cwd=ROOT)
    if not log.strip():
        if not quiet:
            print("  没有可扫描的提交（仓库还没有历史）")
        return []

    problems: list[str] = []
    commit = "?"
    commits_seen = 0
    for line in log.splitlines():
        if line.startswith("__COMMIT__"):
            commit = line[len("__COMMIT__"):].strip()
            commits_seen += 1
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:]
        for label, pattern in SECRET_PATTERNS:
            match = pattern.search(body)
            if match and not _skip_line(match.group(0)):
                problems.append(f"提交 {commit}: {label} → {match.group(0)[:24]}…")
        for needle, label in _personal_needles():
            if needle and (needle in body or needle.replace("\\", "/") in body):
                problems.append(f"提交 {commit}: 历史里含{label}")
    if not quiet:
        print(f"  已扫描 {commits_seen} 个提交的完整补丁")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="开源前敏感信息扫描")
    parser.add_argument("--history", action="store_true", help="同时扫描 Git 历史（发布前必做）")
    parser.add_argument("--paths-only", action="store_true", help="只检查不该提交的路径")
    parser.add_argument("--quiet", action="store_true", help="只输出结论行")
    args = parser.parse_args()

    if not args.quiet:
        print("=" * 66)
        print("  开源前敏感信息扫描（API Key / 令牌 / 口令 / Cookie / 本机路径）")
        print("=" * 66)

    problems: list[str] = []
    scanned = 0
    if args.paths_only:
        problems.extend(scan_paths(args.quiet))
    else:
        file_problems, scanned = scan_files(args.quiet)
        problems.extend(file_problems)
        problems.extend(scan_paths(args.quiet))
        if args.history:
            problems.extend(scan_history(args.quiet))

    if problems:
        print("")
        for item in problems:
            print(f"  [FAIL] {item}")
        print("")
        print(f"发现 {len(problems)} 处问题：开源前必须处理（或把确属示例的写法加入白名单并说明原因）。")
        return 1

    # 「一个文件都没扫到」必须是失败：一份什么都没查的报告比没有报告更危险
    if not args.paths_only and scanned < 20:
        print(f"  [FAIL] 只扫到 {scanned} 个文本文件，明显不对（仓库结构异常？）")
        return 1
    print("NO_SECRET_FOUND" + ("（含 Git 历史）" if args.history else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
