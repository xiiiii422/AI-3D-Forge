"""把开发树的源码导出集镜像到独立的 GitHub 仓库目录（默认只做 dry-run）。

为什么不用 PowerShell 复制：仓库里有中文文件名，PowerShell 5.1 的管道/编码
在这类路径上反复出过问题（见 README-开发者.md 的踩坑记录）。Python 显式指定
编码之后行为可预期，而且能在复制前先打印差异。

为什么这个脚本必须在 scripts/ 而不是某个临时目录：docs/PUBLISH-TO-GITHUB.md
的「改完之后怎么同步回仓库」直接引用它。以前它写在 build/work/ 里 —— 那是
.gitignore 排除、也不进源码包的目录，于是别人 clone 之后照着文档跑，
第一条命令就指向一个不存在的文件。发布工具自己不在发布物里，是文档骗人。

用法：
    python scripts/sync_github_repo.py                  # 只看差异
    python scripts/sync_github_repo.py --apply          # 真的同步
    python scripts/sync_github_repo.py --repo <目录>     # 仓库目录不在项目同级时

仓库目录默认取**项目同级**的 `AI-3D-Forge-GitHub`（不写死绝对路径：写死了
既是隐私泄漏，也会在别人的机器上指到错的地方，见自检的「无本机路径泄漏」）。
"""

from __future__ import annotations

import console_utf8  # noqa: F401,E402 —— 必须最先导入：GBK 控制台下 print 中文/符号会弄崩脚本（见 src/aiforge/stdio.py）
import argparse
import filecmp
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "build" / "source" / "AI-3D-Forge-Source"
DEFAULT_REPO_NAME = "AI-3D-Forge-GitHub"
# 导出集里有、但仓库里**不该有**的东西：
#   * 前端构建产物 —— 属于 Releases，不进源码仓库；
#   * 界面截图 —— .gitignore 里本来就把 webui/scripts/shots/ 排除掉了，
#     但 export_source.ps1 会把它打进"源码包"（给人看的交付物），
#     所以镜像时必须自己跳过，否则仓库工作区会多出一堆被忽略的二进制文件。
SKIP_PREFIXES = ("webui/dist/", "webui/scripts/shots/")


def repo_tracked(repo: Path) -> set[str]:
    # core.quotepath=false：否则非 ASCII 文件名会被 git 转义成
    # "README-\345\274\200..." 这种八进制串，跟导出集里的真实名字对不上，
    # 会被误判成"删除 + 新增"。
    out = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        # errors 必须写死：不写就跟着系统的 locale 走，同一份文件名清单在英文
        # Windows 上会把解码异常抛出来，脚本挂在这里 —— 而它挂掉的时机正好是
        # 「已经决定要删哪些文件」的那一刻（见自检「子进程输出读取要安全」）。
        errors="replace",
        check=True,
    )
    return {line.strip().replace("\\", "/") for line in out.stdout.splitlines() if line.strip()}


def export_files(export: Path) -> set[str]:
    result = set()
    for path in export.rglob("*"):
        if path.is_file():
            rel = path.relative_to(export).as_posix()
            if any(rel.startswith(prefix) for prefix in SKIP_PREFIXES):
                continue
            result.add(rel)
    return result


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真的执行复制/删除")
    parser.add_argument("--repo", default="", help="仓库目录（默认与项目同级的 %s）" % DEFAULT_REPO_NAME)
    parser.add_argument("--export", default="", help="导出集目录（默认 build/source/AI-3D-Forge-Source）")
    args = parser.parse_args(argv)

    export = Path(args.export).resolve() if args.export else EXPORT
    repo = Path(args.repo).resolve() if args.repo else (ROOT.parent / DEFAULT_REPO_NAME)

    if not export.is_dir():
        print(f"导出集不存在：{export}（先跑 scripts/export_source.ps1）")
        return 2
    if repo == ROOT or ROOT in repo.parents:
        # 指到项目自己（或项目里面）就等于把镜像写进真源，两边从此分叉。
        print(f"目标目录 {repo} 落在开发树 {ROOT} 里面；仓库必须是**独立**的目录。")
        return 2
    if not (repo / ".git").is_dir():
        print(f"目标不是 git 仓库：{repo}（先 git init，或用 --repo 指对目录）")
        return 2

    tracked = repo_tracked(repo)
    wanted = export_files(export)

    added = sorted(wanted - tracked)
    removed = sorted(tracked - wanted)
    changed: list[str] = []
    for rel in sorted(wanted & tracked):
        src = export / rel
        dst = repo / rel
        if not dst.is_file() or not filecmp.cmp(src, dst, shallow=False):
            changed.append(rel)

    print(f"开发树  {ROOT}")
    print(f"导出集  {export}（{len(wanted)} 个文件）")
    print(f"仓库    {repo}（已跟踪 {len(tracked)} 个）")
    print(f"新增 {len(added)}：{added[:12]}{' …' if len(added) > 12 else ''}")
    print(f"内容变化 {len(changed)}：{changed[:12]}{' …' if len(changed) > 12 else ''}")
    # 删除单独全量打印：这是这个脚本唯一会**销毁文件**的方向，截断成 12 条
    # 就等于让人对着一份看不全的清单说"好，应用吧"。
    if removed:
        print(f"删除 {len(removed)}（请逐条确认那是真的不该再存在，而不是导出脚本漏了）：")
        for rel in removed:
            print(f"  - {rel}")

    if not args.apply:
        print("\n这是 dry-run。加 --apply 才会真的写。")
        return 0

    for rel in changed + added:
        src = export / rel
        dst = repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    for rel in removed:
        target = repo / rel
        if target.is_file():
            target.unlink()
    # 清掉空目录（git 不跟踪目录，但留着会让人困惑）
    for path in sorted(repo.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and path.name != ".git" and not any(path.iterdir()):
            path.rmdir()
    print(f"\n已同步：新增/更新 {len(changed) + len(added)}，删除 {len(removed)}")
    print("下一步在仓库目录里 `git add -A; git commit`（不要 push，先跑发布前自查）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
