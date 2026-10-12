"""负向对照：证明「开源合规」这几支守卫在真出问题时会变红。

每种违规都临时造出来，跑守卫，确认它**报错**，然后还原并确认守卫恢复绿色。
不这样做的话，"守卫通过"和"守卫根本没在工作"长得一模一样。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable


def run(script: str, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        [PY, str(ROOT / "scripts" / script), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def expect_red(label: str, script: str, args: tuple[str, ...], prepare, cleanup) -> bool:
    undo = prepare()
    try:
        code, output = run(script, *args)
    finally:
        cleanup(undo)
    ok = code != 0
    first = next((ln.strip() for ln in output.splitlines() if "[FAIL]" in ln), "")
    print(f"  [{'OK' if ok else 'FAIL'}]   {label} → {'如实报错' if ok else '居然通过了（守卫是假的）'}"
          + (f"：{first[len('[FAIL]'):].strip()[:80]}" if ok and first else ""))
    return ok


def write_text(path: Path, text: str) -> str | None:
    original = path.read_text(encoding="utf-8") if path.is_file() else None
    path.write_text(text, encoding="utf-8")
    return original


def restore(path: Path, original: str | None) -> None:
    if original is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(original, encoding="utf-8")


def main() -> int:
    print("负向对照：开源合规守卫")
    results: list[bool] = []

    # 1) 塞一个完整 API Key 形状的字符串 → 敏感信息扫描必须报警
    #
    # 这个假 Key 只能在运行时拼，不得写成字面量：本仓自己的扫描器会报它（应当报，
    # 它就是照着真 Key 的形状做的），而 GitHub 的 secret scanning 也会扫公开仓库里的
    # `sk-` + 32 位十六进制 —— 守卫脚本自己往将公开的仓库里埋一颗看着像泄密弹，
    # 接到的就是真的安全邮件了。拆开写仍然能造出形似的整串（下面两半拼起来是 32 位）。
    fake_key = "sk-" + "0123456789abcdef" * 2
    target = ROOT / "docs" / "USER_GUIDE.md"
    results.append(
        expect_red(
            "文档里出现完整 API Key",
            "check_no_secrets.py",
            (),
            lambda: write_text(target, target.read_text(encoding="utf-8") + f"\n{fake_key}\n"),
            lambda original: restore(target, original),
        )
    )

    # 2) 塞一个本机绝对路径 → 同一支守卫必须报警
    results.append(
        expect_red(
            "文档里出现本机绝对路径",
            "check_no_secrets.py",
            (),
            lambda: write_text(target, target.read_text(encoding="utf-8") + f"\n{ROOT}\\build\\x\n"),
            lambda original: restore(target, original),
        )
    )

    # 3) 删掉必需文件 → 开源文件完整性必须报警
    license_file = ROOT / "LICENSE"
    backup = ROOT / "build" / "work" / "LICENSE.negctl"
    backup.parent.mkdir(parents=True, exist_ok=True)
    results.append(
        expect_red(
            "缺少 LICENSE",
            "check_oss_files.py",
            (),
            lambda: (shutil.copyfile(license_file, backup), license_file.unlink())[0],
            lambda _: shutil.copyfile(backup, license_file),
        )
    )

    # 4) README 链接指向不存在的文件 → 交叉链接检查必须报警
    readme = ROOT / "README.md"
    results.append(
        expect_red(
            "README 链接断链",
            "check_oss_files.py",
            (),
            lambda: write_text(readme, readme.read_text(encoding="utf-8") + "\n[断链](docs/DOES-NOT-EXIST.md)\n"),
            lambda original: restore(readme, original),
        )
    )

    # 5) 造一个"没被 .gitignore 挡住、但本身不该提交"的文件 → 路径检查必须报警
    #    （webui/dist 那类有 .gitignore 兜底，属于"忽略即安全"，不适合当反例）
    leaked = ROOT / "docs" / "leaked.env"
    results.append(
        expect_red(
            "把 .env 放进提交集",
            "check_no_secrets.py",
            ("--paths-only",),
            lambda: write_text(leaked, "DEEPSEEK_API_KEY=<your-key>\n"),
            lambda original: restore(leaked, original),
        )
    )

    # 6) 反向确认：.gitignore 真的在起作用 —— 生成物存在，但**不在**扫描集里。
    #    这一条如果变红，说明忽略规则失效（dist/截图会被当成"提交了生成物"）。
    dist_probe = ROOT / "webui" / "dist" / "index.html"
    existed = dist_probe.is_file()
    if existed:
        code, output = run("check_no_secrets.py", "--quiet")
        listed = "webui/dist" in output
        print(f"  [{'OK' if not listed else 'FAIL'}]   .gitignore 生效：已存在的 webui/dist 不在扫描集里")
        results.append(not listed)
    else:
        print("  [SKIP] 未构建前端，跳过 .gitignore 生效性反证")

    print("")
    code, output = run("check_oss_files.py", "--quiet")
    secrets_code, secrets_output = run("check_no_secrets.py", "--quiet")
    restored = code == 0 and secrets_code == 0
    print(f"  [{'OK' if restored else 'FAIL'}]   还原后重新通过"
          f"（OSS={code}，secrets={secrets_code}）")
    ok = all(results) and restored
    print("NEGCTL_OK" if ok else "NEGCTL_FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
