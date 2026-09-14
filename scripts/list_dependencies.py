"""列出「实际随程序分发」的第三方依赖、版本与许可证。

用途：`docs/LICENSE.txt` 里的第三方清单必须以这个脚本的输出为准，
不要手抄 —— 手工维护的清单一定会随着依赖升级而悄悄过期，
而许可证漏标是实打实的合规问题（例如 certifi 是 MPL-2.0，
必须给出归属，不能因为它只是传递依赖就省略）。

用法：
    build\\venv\\Scripts\\python.exe scripts\\list_dependencies.py
    build\\venv\\Scripts\\python.exe scripts\\list_dependencies.py --markdown

做法：从 requirements.txt 的直接依赖出发，沿 importlib.metadata 里的
`Requires-Dist` 递归展开**传递依赖**，得到真正被打进 exe 的完整集合。
环境标记（`; platform_system == ...`）会被跳过，避免把只在别的平台安装的
包算进来。
"""

from __future__ import annotations

import argparse
import re
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 直接运行时依赖（与 requirements.txt 的「--- 运行时 ---」段一致）。
# 构建期依赖（pyinstaller）单独说明：它的引导程序会嵌进分发出去的 exe。
RUNTIME_PACKAGES = (
    "fastapi",
    "uvicorn",
    "python-multipart",
    "pydantic",
    "httpx",
    "keyring",
    "pywebview",
)

# 发行包名 → 项目主页（许可证清单里给出来源，便于使用者核对）
HOMEPAGES = {
    "fastapi": "https://fastapi.tiangolo.com/",
    "starlette": "https://www.starlette.io/",
    "uvicorn": "https://www.uvicorn.org/",
    "pydantic": "https://docs.pydantic.dev/",
    "pydantic-core": "https://docs.pydantic.dev/",
    "python-multipart": "https://github.com/Kludex/python-multipart",
    "httpx": "https://www.python-httpx.org/",
    "httpcore": "https://www.encode.io/httpcore/",
    "keyring": "https://github.com/jaraco/keyring",
    "pywebview": "https://pywebview.flowrl.com/",
    "certifi": "https://github.com/certifi/python-certifi",
    "anyio": "https://anyio.readthedocs.io/",
    "click": "https://click.palletsprojects.com/",
    "h11": "https://github.com/python-hyper/h11",
    "idna": "https://github.com/kjd/idna",
    "bottle": "https://bottlepy.org/",
    "proxy-tools": "https://github.com/jtushman/proxy_tools",
    "annotated-types": "https://github.com/annotated-types/annotated-types",
    "typing-extensions": "https://github.com/python/typing_extensions",
    "more-itertools": "https://github.com/more-itertools/more-itertools",
    "jaraco-classes": "https://github.com/jaraco/jaraco.classes",
    "jaraco-context": "https://github.com/jaraco/jaraco.context",
    "jaraco-functools": "https://github.com/jaraco/jaraco.functools",
}

# 少数包在元数据里没有 License 字段，只有 Classifier，甚至两样都缺。
# 这里按包内 LICENSE 文件人工确认一次，并注明来源，避免输出「未知」而失去意义。
KNOWN_LICENSES = {
    "anyio": "MIT",
    "certifi": "MPL-2.0",
    "click": "BSD-3-Clause",
    "idna": "BSD-3-Clause",
    "jaraco-context": "MIT",
    "jaraco-functools": "MIT",
    "more-itertools": "MIT",
    "typing-extensions": "PSF-2.0",
    "bottle": "MIT",
}


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def license_of(dist: metadata.Distribution) -> str:
    raw = (dist.metadata.get("License") or "").strip()
    if not raw or len(raw) > 60 or "\n" in raw:
        raw = ""
    if not raw:
        for classifier in dist.metadata.get_all("Classifier") or []:
            if classifier.startswith("License ::"):
                raw = classifier.split("::")[-1].strip()
                break
    if not raw:
        raw = KNOWN_LICENSES.get(normalize(dist.metadata["Name"]), "见包内 LICENSE")
    return raw


def collect() -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}

    def walk(name: str, depth: int) -> None:
        key = normalize(name)
        if key in found:
            return
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            found[key] = {
                "name": name,
                "version": "未安装",
                "license": "-",
                "depth": str(depth),
                "homepage": "",
            }
            return
        found[key] = {
            "name": dist.metadata["Name"],
            "version": dist.version,
            "license": license_of(dist),
            "depth": str(depth),
            "homepage": HOMEPAGES.get(key, ""),
        }
        for requirement in dist.requires or []:
            if ";" in requirement:  # 带环境标记：只在别的平台装，跳过
                continue
            child = re.split(r"[<>=!\[; ]", requirement.strip())[0]
            if not child:
                continue
            # 只保留真正会被打进去的：源码分发包（无 wheel）也照算
            walk(child, depth + 1)

    for package in RUNTIME_PACKAGES:
        walk(package, 0)

    return sorted(found.values(), key=lambda item: normalize(item["name"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markdown", action="store_true", help="输出 Markdown 表格")
    args = parser.parse_args()

    rows = collect()
    missing = [r for r in rows if r["version"] == "未安装"]

    if args.markdown:
        print("| 组件 | 版本 | 许可证 | 来源 |")
        print("| --- | --- | --- | --- |")
        for row in rows:
            link = f"[{row['homepage']}]({row['homepage']})" if row["homepage"] else "-"
            print(f"| {row['name']} | {row['version']} | {row['license']} | {link} |")
    else:
        print(f"{'组件':24} {'版本':12} 许可证")
        print("-" * 76)
        for row in rows:
            print(f"{row['name']:24} {row['version']:12} {row['license']}")
        print("-" * 76)

    print(f"\n共 {len(rows)} 个发行包（含传递依赖）")
    licenses = sorted({r["license"] for r in rows})
    print(f"涉及许可证：{'、'.join(licenses)}")
    if missing:
        print(f"\n[警告] 有 {len(missing)} 个包未安装，清单不完整："
              f"{'、'.join(r['name'] for r in missing)}")
        print("       请先 pip install -r requirements.txt 再重新生成。")
        return 1
    print("\n提示：docs/LICENSE.txt 里的第三方清单应与本输出一致；"
          "改依赖后请重新生成并更新文档。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
