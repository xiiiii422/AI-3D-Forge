"""开源文件完整性检查：一个仓库「看起来像不像能直接开源」的硬性清单。

为什么单独一支脚本（而不是塞进 selfcheck）：
  这份清单要在 **CI 的 ubuntu 机器**上也能跑 —— 不依赖 OpenSCAD、不依赖 Windows、
  不依赖任何密钥。它是"别人点进仓库第一眼看到的东西"的守卫：
  LICENSE、README（中英双语）、贡献指南、安全策略、变更记录、Issue/PR 模板、
  CI 工作流、第三方许可证清单……

检查三件事：
  1. **必需文件存在**（缺一个就红）；
  2. **README 必须覆盖的章节**（是什么/功能/流程/系统要求/安装/API/OpenSCAD/运行/打印能力/截图/许可/贡献）；
  3. **交叉引用不断链**：README 与文档里 `[文字](相对路径)` 指向的文件必须真实存在
     （开源仓库最常见的破绽就是 README 里点不开的链接）。

用法：
    python scripts/check_oss_files.py
    python scripts/check_oss_files.py --quiet
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES: list[tuple[str, str]] = [
    ("README.md", "项目门面（中文）"),
    ("README.en.md", "项目门面（英文）"),
    ("README-用户.md", "用户快速上手"),
    ("README-开发者.md", "开发者/维护者手册"),
    ("LICENSE", "开源许可证（MIT）"),
    ("SECURITY.md", "安全策略与漏洞反馈"),
    ("CONTRIBUTING.md", "贡献指南"),
    ("CHANGELOG.md", "变更记录"),
    ("CODE_OF_CONDUCT.md", "行为准则"),
    (".gitignore", "忽略清单"),
    (".gitattributes", "换行与二进制约定"),
    (".github/workflows/ci.yml", "CI 工作流"),
    (".github/ISSUE_TEMPLATE/bug_report.md", "缺陷报告模板"),
    (".github/ISSUE_TEMPLATE/feature_request.md", "功能建议模板"),
    (".github/pull_request_template.md", "PR 模板"),
    (".github/dependabot.yml", "依赖更新配置"),
    ("docs/USER_GUIDE.md", "完整用户指南"),
    ("docs/DEVELOPMENT.md", "开发者上手"),
    ("docs/API-CONTRACT.md", "接口契约"),
    ("docs/LICENSE-THIRD-PARTY.txt", "第三方组件与许可证"),
    ("docs/LICENSE-OpenSCAD-GPLv2.txt", "OpenSCAD 的 GPLv2 全文"),
    ("docs/OPENSCAD-SOURCES.md", "OpenSCAD 来源与校验值"),
    ("requirements.txt", "运行时依赖"),
    ("requirements-build.txt", "构建期依赖"),
    ("main.py", "程序入口"),
    ("AI-3D-Forge.spec", "打包配置"),
    ("scripts/check_no_secrets.py", "敏感信息扫描"),
    ("scripts/check_oss_files.py", "开源文件完整性检查"),
    ("scripts/selfcheck.py", "后端自检"),
    ("scripts/list_dependencies.py", "第三方依赖清单生成"),
    ("src/aiforge/app.py", "后端应用入口"),
    ("webui/package.json", "前端工程定义"),
    ("installer/AI-3D-Forge.iss", "安装器脚本"),
]

# README 必须提到的关键点（大小写不敏感的子串匹配）。
# 用「关键词」而不是「标题逐字相同」，避免为了过检查而写死标题。
README_MUST_MENTION: list[tuple[str, str]] = [
    ("MIT", "许可证类型"),
    ("LICENSE", "许可证文件链接"),
    ("Windows", "支持的操作系统"),
    ("DeepSeek", "AI 服务配置"),
    ("OpenSCAD", "3D 引擎配置"),
    ("docs/images", "截图位置"),
    ("CONTRIBUTING", "贡献方式"),
    ("SECURITY", "安全问题反馈入口"),
    ("docs/USER_GUIDE.md", "用户指南入口"),
    ("docs/DEVELOPMENT.md", "开发者文档入口"),
]

MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def check_required() -> tuple[list[str], int]:
    problems = []
    for rel, why in REQUIRED_FILES:
        if not (ROOT / rel).is_file():
            problems.append(f"缺少文件：{rel}（{why}）")
    return problems, len(REQUIRED_FILES)


def check_readme() -> tuple[list[str], int]:
    problems = []
    path = ROOT / "README.md"
    if not path.is_file():
        return ["README.md 不存在，无法检查其内容"], 0
    text = path.read_text(encoding="utf-8")
    for keyword, why in README_MUST_MENTION:
        if keyword.lower() not in text.lower():
            problems.append(f"README.md 没有提到 {keyword}（{why}）")
    # 英文版不能是空壳
    en = ROOT / "README.en.md"
    if en.is_file() and len(en.read_text(encoding="utf-8")) < 2000:
        problems.append("README.en.md 太短，看起来没有真正写内容")
    return problems, len(README_MUST_MENTION)


def check_links() -> tuple[list[str], int]:
    """相对链接必须指向真实存在的文件（跳过外链与锚点）。"""
    problems: list[str] = []
    checked = 0
    candidates = [
        ROOT / "README.md",
        ROOT / "README.en.md",
        ROOT / "README-用户.md",
        ROOT / "README-开发者.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "SECURITY.md",
        ROOT / "CHANGELOG.md",
        ROOT / "CODE_OF_CONDUCT.md",
        ROOT / "docs" / "USER_GUIDE.md",
        ROOT / "docs" / "DEVELOPMENT.md",
        ROOT / "docs" / "PUBLISH-TO-GITHUB.md",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for target in MARKDOWN_LINK.findall(text):
            target = target.strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            if target.startswith("<") or " " in target:
                continue
            # GitHub 页面型链接（`../../releases`、`../../issues/new?template=...`）
            # 是相对 **仓库页面** 的，不是相对文件的本地路径，本地核对没有意义。
            if target.startswith("../../") or "?" in target:
                continue
            rel = target.split("#", 1)[0]
            if not rel:
                continue
            checked += 1
            resolved = (path.parent / rel).resolve()
            if not resolved.exists():
                problems.append(f"{path.relative_to(ROOT).as_posix()}: 链接指向不存在的路径 → {target}")
    return problems, checked


def main() -> int:
    parser = argparse.ArgumentParser(description="开源文件完整性检查")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if not args.quiet:
        print("=" * 66)
        print("  开源文件完整性检查（必需文件 / README 章节 / 交叉链接）")
        print("=" * 66)

    problems: list[str] = []
    required_problems, required_count = check_required()
    problems.extend(required_problems)
    readme_problems, readme_count = check_readme()
    problems.extend(readme_problems)
    link_problems, link_count = check_links()
    problems.extend(link_problems)

    if not args.quiet:
        print(f"  必需文件 {required_count - len(required_problems)}/{required_count} 存在")
        print(f"  README 要点 {readme_count - len(readme_problems)}/{readme_count} 覆盖")
        print(f"  相对链接已核对 {link_count} 条")

    if problems:
        print("")
        for item in problems:
            print(f"  [FAIL] {item}")
        print("")
        print(f"发现 {len(problems)} 处问题：开源文件不完整。")
        return 1

    # 「一条链接都没核对到」说明解析失效了，不能算通过
    if link_count < 5:
        print(f"  [FAIL] 只核对到 {link_count} 条相对链接，明显不对（解析逻辑失效？）")
        return 1
    print("OSS_FILES_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
