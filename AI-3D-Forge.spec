# -*- mode: python ; coding: utf-8 -*-
"""AI 3D Forge —— PyInstaller 打包配置。

目标（需求第三十五、三十六节）：
    把 Python 运行时与全部依赖一起打进 AI-3D-Forge.exe，
    最终用户机器上「不需要」安装 Python、不需要 pip、不需要命令行。

关键点：
  * 前端 webui/dist 一并打包并在运行时从 bundle 目录提供（离线可用，不依赖 CDN）。
  * 体积控制：显式排除本软件用不到的重量级库（tkinter 除外——手动选择路径时要弹文件对话框）。
  * 采用 onedir 模式：启动比 onefile 快得多，且便于安装器增量升级。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve()
SRC = ROOT / "src"
WEBUI_DIST = ROOT / "webui" / "dist"
VERSION_FILE = ROOT / "build" / "version_info.txt"
ICON_FILE = ROOT / "build" / "AI-3D-Forge.ico"

# --------------------------------------------------------------------------
# 数据文件
# --------------------------------------------------------------------------
datas: list[tuple[str, str]] = []

# 前端构建产物 → 打包到 <bundle>/webui，运行时由 aiforge.paths.frontend_dir() 定位
if not (WEBUI_DIST / "index.html").is_file():
    raise SystemExit(
        "未找到前端构建产物 webui/dist/index.html。\n"
        "请先执行：cd webui; npm run build\n"
        "（或直接运行 scripts\\build.ps1，它会自动完成前端构建）"
    )
datas.append((str(WEBUI_DIST), "webui"))

# 用户说明文档随程序附带，方便用户在安装目录里直接查看
for doc in ("README-用户.md", "README-开发者.md", "README.md", "LICENSE", "CHANGELOG.md"):
    candidate = ROOT / doc
    if candidate.is_file():
        datas.append((str(candidate), "docs"))

# 完整用户指南与开发者上手文档也在 docs/ 里，随程序一起带上，
# 这样 README-用户.md 里那句「完整说明见 docs/USER_GUIDE.md」在安装目录里也点得开。
for doc in ("USER_GUIDE.md", "DEVELOPMENT.md", "LICENSE-THIRD-PARTY.txt"):
    candidate = ROOT / "docs" / doc
    if candidate.is_file():
        datas.append((str(candidate), "docs"))

# 固定零件库（经过实测验证的参数化零件）：必须随程序分发 ——
# 生成的模型会写 `use <aiforge_parts.scad>`，编译时靠 OPENSCADPATH 找到它。
# 放到包内的 parts/ 目录，运行期由 aiforge.parts.catalog.library_path() 定位。
PARTS_DIR = ROOT / "src" / "aiforge" / "parts"
if not (PARTS_DIR / "aiforge_parts.scad").is_file():
    raise SystemExit("未找到固定零件库 src\\aiforge\\parts\\aiforge_parts.scad")
datas.append((str(PARTS_DIR / "aiforge_parts.scad"), "parts"))

# --------------------------------------------------------------------------
# 隐式导入
# --------------------------------------------------------------------------
hiddenimports: list[str] = [
    # keyring 的 Windows 凭据管理器后端是按名字动态加载的
    "keyring.backends.Windows",
    "keyring.backends.SecretService",
    "keyring.backends.kwallet",
    "keyring.backends.chainer",
    "keyring.backends.fail",
    # pywebview 的 Windows 后端
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    # proxy_tools 被 webview/__init__.py 顶层 import（from proxy_tools import
    # module_property），但它是个单模块包、只提供装饰器，PyInstaller 的静态分析
    # 曾经漏掉它，导致打包版启动即报 "No module named 'proxy_tools'"。
    # 显式列出，不再依赖分析器的推断。
    "proxy_tools",
    # 服务运行时的动态部分
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
]
hiddenimports += collect_submodules("keyring.backends")

# 明确排除：这些库本软件完全不用（有些会因为环境里的其他包被间接拉进来）
# 重要：不要排除 distutils / setuptools / wheel / pip。
#   PyInstaller 自带的 hook-distutils 与 utils\hooks\setuptools.py 会去 alias
#   这几个模块，一旦被排除就会在打包时报
#   'Target module "X" already imported as "ExcludedModule(...)"'。
#   它们本来也不会被拉进来（除非有依赖真的 import），所以不排除几乎没有代价。
# 也不要排除 tkinter —— 手动选择 openscad.exe 时要弹文件对话框（兜底路径）。
#
# 绝对不要排除 pywebview 的运行时依赖：bottle 与 proxy_tools。
#   webview/http.py 第 31 行顶层 `import bottle`；
#   webview/__init__.py 第 23 行顶层 `from proxy_tools import module_property`。
# 曾经以为「只有 Flask/Django 后端才需要」就把两个都排除了，结果打包版启动即
# ModuleNotFoundError，原生窗口永远起不来、静默退回系统浏览器；而构建机上
# site-packages 里有它们，一切正常，只在发布版复现。
# selfcheck 的 t_frozen_webview_deps 有断言守着这件事。
#
# 可以排除的得拿证据说：`yaml`（实测在 _internal 里占 0.24 MB）全仓库只有两个
# 导入方，而且都是跑不到的分支：
#   * uvicorn/config.py:374 —— 只在 `--config xxx.yml` 时才 import，本项目用代码直传配置；
#   * starlette/schemas.py:12 —— 在 try/except ImportError 里，只服务 starlette 的
#     `Route(schema=...)` 文档字符串解析，FastAPI 的 /api/openapi.json 不走它。
# 为防“打包版才复现”那一类坑，test_install.ps1 现在会在装好的程序里请求
# /api/openapi.json 并数路由数：少了依赖会在这里炸成 500，而不是默默上线。
excludes: list[str] = [
    "pytest",
    "numpy",
    "pandas",
    "matplotlib",
    "scipy",
    "PIL",
    "pydoc_data",
    "lib2to3",
    "sqlite3",
    "yaml",  # 见上方证据：只有两个跑不到的条件导入
    "_yaml",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "main.py")],
    pathex=[str(SRC), str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

# 应用图标：任务栏、资源管理器、快捷方式都会用它。
# 没有图标的软件在用户眼里就是"半成品"，在一堆快捷方式里也找不到。
if not ICON_FILE.is_file():
    raise SystemExit(
        "未找到应用图标 build/AI-3D-Forge.ico。\n"
        "请先执行：python scripts\\make_icon.py\n"
        "（scripts\\build.ps1 会自动生成）"
    )

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AI-3D-Forge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # 不用 UPX：压缩后极易被杀毒软件误报
    console=False,  # 双击不弹黑框（GUI 程序）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(VERSION_FILE) if VERSION_FILE.is_file() else None,
    icon=str(ICON_FILE),
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AI-3D-Forge",
)
