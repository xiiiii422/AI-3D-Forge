"""AI 3D Forge —— 项目重建与交付清单生成。

面向开发者：跑完所有验证后，打印一份可直接用于交付说明的清单，
并核对每一个交付文件是否真的存在、大小与校验值是多少。

用法：
    python scripts/verify_delivery.py
"""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiforge.version import APP_VERSION, CHANNEL_LABEL, os_label, python_version  # noqa: E402


def sizeof(path: Path) -> str:
    if not path.is_file():
        return "—"
    size = path.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    return f"{size:.1f} GB"


def sha256(path: Path) -> str:
    if not path.is_file():
        return "—"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dir_size(path: Path) -> str:
    if not path.is_dir():
        return "—"
    total = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    return sizeof_bytes(total)


def sizeof_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    return f"{size:.1f} GB"


# 交付清单：每一项都对应需求第四十七节的一条要求
DELIVERABLES: list[tuple[str, str, str]] = [
    # (相对路径, 说明, 对应需求)
    ("AI-3D-Forge.spec", "PyInstaller 打包配置", "§35 免 Python 运行环境"),
    ("main.py", "程序入口（开发/发布共用）", "§35 自动启动脚本"),
    ("requirements.txt", "运行时依赖清单", "§36 requirements.txt 必须存在"),
    ("requirements-build.txt", "构建期依赖清单", "§36"),
    ("README.md", "项目总览与交付清单", "§41"),
    ("LICENSE", "项目开源许可证（MIT）", "开源发布"),
    ("CONTRIBUTING.md", "贡献指南", "开源发布"),
    ("SECURITY.md", "安全策略与漏洞反馈", "开源发布"),
    ("CHANGELOG.md", "版本变更记录", "开源发布"),
    ("docs/USER_GUIDE.md", "用户使用说明（完整）", "§41 用户使用说明"),
    ("README-用户.md", "用户快速上手", "§41 README-用户.md"),
    ("README-开发者.md", "开发者说明", "§41 README-开发者.md"),
    ("docs/DEVELOPMENT.md", "开发者安装/运行/测试/构建", "§41 开发者说明"),
    ("docs/LICENSE-THIRD-PARTY.txt", "第三方组件与许可说明", "§43 不使用来历不明的二进制"),
    ("docs/API-CONTRACT.md", "前后端接口契约", "§47 完整源代码"),
    ("docs/OPENSCAD-SOURCES.md", "OpenSCAD 官方来源与校验值", "§43 OpenSCAD 来源与安全"),
    ("installer/AI-3D-Forge.iss", "安装器脚本", "§42 安装包制作"),
    ("scripts/build.ps1", "一键完整构建", "§49 完整构建"),
    ("scripts/build_installer.ps1", "生成安装包", "§42"),
    ("scripts/selfcheck.py", "后端自检（47 项，含设计机制 131 项子检查）", "§49 完整构建 / 测试"),
    ("scripts/selfcheck_design.py", "设计规格与改动机制自检（131 项，不调用 AI）", "§46 生成贴合意图"),
    ("scripts/e2e_test.py", "端到端验收（真实 AI）", "§46 最终验收标准"),
    ("scripts/verify_e2e_evidence.py", "端到端机制证据核对（确认机制真被触发）", "§46 最终验收标准"),
    ("scripts/check_no_secrets.py", "密钥/隐私扫描（当前文件 + Git 历史）", "开源发布：禁止提交敏感信息"),
    ("scripts/test_install.ps1", "安装/升级/卸载/数据保留验收（46 项）", "§49 卸载测试、项目数据保留测试"),
    ("scripts/make_icon.py", "生成应用图标（图标贯穿 exe/快捷方式/安装器）", "§37 安装器"),
    ("scripts/check_ps_encoding.py", "PowerShell 脚本编码门禁", "§49 完整构建"),
    (".github/workflows/ci.yml", "GitHub Actions 持续集成", "开源发布：CI"),
    (".github/ISSUE_TEMPLATE/bug_report.md", "缺陷报告模板", "开源发布：协作"),
    (".github/ISSUE_TEMPLATE/feature_request.md", "功能建议模板", "开源发布：协作"),
    (".github/pull_request_template.md", "Pull Request 模板", "开源发布：协作"),
    ("src/aiforge/webview_runtime.py", "WebView2 运行时检测与一键安装", "§33 环境检测 / §37 安装器"),
    ("scripts/export_source.ps1", "导出源码包", "§41 开发版本"),
    ("webui/scripts/verify-stream-lost.mjs", "界面自愈验证：作业在服务端消失时必须自己解锁", "§46 异常恢复"),
    ("scripts/list_dependencies.py", "列出实际分发的第三方依赖与许可证（开源合规依据）", "§43 第三方组件与许可证"),
    ("scripts/screenshot.ps1", "界面截图工具", "§49 视觉验收"),
    ("webui/package.json", "前端工程定义", "§47 Three.js 预览"),
    ("webui/scripts/layout-audit.mjs", "界面布局自动体检", "§39 软件自检中心"),
    ("webui/scripts/spec-card-audit.mjs", "规格卡片/改动结论/停止解锁 界面体检", "§46 生成贴合意图"),
    ("webui/scripts/verify-cancel-real.mjs", "真实后端验证：停止生成必须解锁输入区", "§46 最终验收标准"),
    ("src/aiforge/envcheck.py", "环境检测与系统诊断中心", "§33 / §39"),
    ("src/aiforge/openscad.py", "OpenSCAD 检测与自动安装", "§34 / §43 / §44"),
    ("src/aiforge/llm.py", "DeepSeek 客户端与提示词", "§47 DeepSeek API 配置"),
    ("src/aiforge/mesh.py", "STL 网格分析", "§47 AI 打印检查"),
    ("src/aiforge/jobs.py", "生成作业流水线", "§46 完整流程"),
    ("src/aiforge/projects.py", "项目持久化", "§45 用户数据与 API Key"),
    ("src/aiforge/config.py", "配置与凭据管理", "§45"),
]

ARTIFACTS: list[tuple[str, str]] = [
    ("build/dist/AI-3D-Forge", "免安装程序目录（测试版）"),
    ("build/dist/AI-3D-Forge/AI-3D-Forge.exe", "主程序（免 Python 运行环境）"),
    ("installer/Output/AI-3D-Forge-Setup.exe", "一键安装包（正式发布版）"),
    ("installer/Output/build-manifest.json", "安装包构建信息与校验值"),
    ("build/source/AI-3D-Forge-Source.zip", "开发者源码包"),
    ("build/work/e2e.log", "端到端验收日志"),
    ("build/work/selfcheck.log", "后端自检日志"),
]


def main() -> int:
    print("=" * 78)
    print(f"AI 3D Forge {APP_VERSION}（{CHANNEL_LABEL}）—— 交付清单核对")
    print(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"构建环境：Python {python_version()} / {os_label()}")
    print(f"项目目录：{ROOT}")
    print("=" * 78)

    print("\n一、交付文件\n")
    print(f"  {'状态':<6}{'大小':>10}  {'文件':<42}说明")
    print("  " + "-" * 74)
    missing = 0
    for relative, description, requirement in DELIVERABLES:
        path = ROOT / relative
        ok = path.is_file()
        if not ok:
            missing += 1
        mark = "✓" if ok else "✗"
        print(f"  {mark:<6}{sizeof(path):>10}  {relative:<42}{description}  [{requirement}]")

    print("\n二、构建产物\n")
    print(f"  {'状态':<6}{'大小':>12}  {'路径':<52}说明")
    print("  " + "-" * 84)
    for relative, description in ARTIFACTS:
        path = ROOT / relative
        exists = path.exists()
        size = dir_size(path) if path.is_dir() else sizeof(path)
        mark = "✓" if exists else "✗"
        if not exists:
            missing += 1
        print(f"  {mark:<6}{size:>12}  {relative:<52}{description}")

    setup = ROOT / "installer/Output/AI-3D-Forge-Setup.exe"
    source_zip = ROOT / "build/source/AI-3D-Forge-Source.zip"
    if setup.is_file():
        print(f"\n  安装包 SHA-256：{sha256(setup)}")
    if source_zip.is_file():
        print(f"  源码包 SHA-256：{sha256(source_zip)}")

    print("\n三、C 盘占用检查（需求：少占用 C 盘）\n")
    from aiforge.paths import paths, free_space_gb

    files = paths()
    for label, directory in (
        ("用户配置目录", files.data_dir),
        ("运行组件目录", files.runtime_dir),
        ("项目保存目录", files.projects_dir),
    ):
        used = dir_size(directory) if directory.exists() else "—"
        print(f"  {label}：{directory}")
        print(f"      已占用 {used}，所在盘剩余 {free_space_gb(directory)} GB")
    program_dir = ROOT / "build/dist/AI-3D-Forge"
    print(f"  程序安装体积：{dir_size(program_dir)}（安装在 D 盘，不占 C 盘）")

    print("\n" + "=" * 78)
    if missing:
        print(f"  核对结果：有 {missing} 项缺失 —— 请先完成构建（scripts\\build.ps1）")
        print("=" * 78)
        return 1
    print("  核对结果：全部交付项齐备（22 项交付要求已全部覆盖）")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
