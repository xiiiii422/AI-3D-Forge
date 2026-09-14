# AI 3D Forge

**用一句话描述你想要的东西，AI 帮你设计出可以直接 3D 打印的模型。**

一个面向普通 Windows 用户的桌面 AI 3D 打印设计工具。
不需要懂编程，不需要安装 Python、Node.js，不需要打开命令行。

```
输入「设计一个桌面手机支架，高度100mm。」
        ↓
先请你确认「我理解成了什么」：把尺寸摆成一张可改的规格卡片
        ↓
DeepSeek 按确认后的规格生成参数化 OpenSCAD 代码
        ↓
OpenSCAD 编译出 STL 模型
        ↓
网页里直接预览、旋转查看
        ↓
AI 检查能不能打印、哪里需要改
        ↓
「高度改成150mm。」→ 只改高度，改完逐条核对「到底动了什么」
        ↓
下载 STL → 切片 → 打印
```

---

## 我该看哪份文档？

| 你是谁 | 看这里 |
|---|---|
| **我想用这个软件** | [README-用户.md](README-用户.md) —— 安装、配置、使用、常见问题 |
| **我要改代码 / 打包发布** | [README-开发者.md](README-开发者.md) —— 架构、构建、测试、发版清单 |
| **我要对接前后端接口** | [docs/API-CONTRACT.md](docs/API-CONTRACT.md) —— 接口契约（唯一依据） |
| **我想了解 OpenSCAD 从哪来** | [docs/OPENSCAD-SOURCES.md](docs/OPENSCAD-SOURCES.md) —— 官方来源与校验值 |
| **许可与第三方组件** | [docs/LICENSE.txt](docs/LICENSE.txt) |

---

## 版本形态

本项目交付三个层次，请确认你需要哪一个：

| 版本 | 文件 | 需要什么 | 适合谁 |
|---|---|---|---|
| **开发版** | 源码目录 | Python 3.10+、Node.js 18+ | 开发者 |
| **测试版** | `build\dist\AI-3D-Forge\AI-3D-Forge.exe` | 无（免安装） | 内测 |
| **正式发布版** | `installer\Output\AI-3D-Forge-Setup.exe` | 无（一键安装） | 普通用户 |

详细区别见 [README-开发者.md 第二节](README-开发者.md#二版本区分)。

---

## 快速开始

### 普通用户

1. 双击 `AI-3D-Forge-Setup.exe`
2. 一路「下一步」（安装程序默认装到 D 盘，帮你省 C 盘空间）
3. 打开软件，跟着欢迎向导走
   - 如果没装 OpenSCAD，点一下 **[自动安装 OpenSCAD]** 就行
   - 粘贴你的 DeepSeek API Key
4. 开始设计

详见 [README-用户.md](README-用户.md)。

### 开发者

```powershell
# 搭建（全部缓存重定向到项目目录，避免占用 C 盘）
# 把下面的 <项目目录> 换成你自己的克隆路径，例如 D:\code\AI-3D-Forge
$env:PIP_CACHE_DIR = "<项目目录>\build\cache\pip"
$env:npm_config_cache = "<项目目录>\build\cache\npm"
python -m venv build\venv
& build\venv\Scripts\python.exe -m pip install -r requirements-build.txt
cd webui; npm install; npm run build; cd ..

# 运行
& build\venv\Scripts\python.exe main.py

# 后端自检（41 项，秒级，不消耗 AI 额度）
& build\venv\Scripts\python.exe scripts\selfcheck.py

# 完整构建（含打包与安装包）
.\scripts\build.ps1
```

---

## 交付清单

对照项目需求，最终交付包含：

**程序与安装**
- [x] 完整源代码
- [x] Windows 免安装发布版（`build\dist\AI-3D-Forge\`）
- [x] 一键安装包 `AI-3D-Forge-Setup.exe`
- [x] 自动启动脚本（快捷方式 + 单实例保护）
- [x] 卸载支持（Inno Setup 卸载器）
- [x] 升级安装且保留用户数据

**环境适配**
- [x] 启动时自动环境检测（Windows / 运行环境 / Python / OpenSCAD / 依赖 / 目录权限 / 网络 / API / 临时目录 等 15 项）
- [x] OpenSCAD 自动检测（搜索常见位置 + 注册表 + PATH，不假设安装位置，支持手动指定）
- [x] OpenSCAD 自动安装（官方源 + SHA-256 校验 + 免管理员安装 + 安装后 CLI 实测）
- [x] 自动安装失败时给出官方入口与最少操作步骤，绝不静默失败
- [x] Python 依赖全部打包进 exe，用户机器无需 Python
- [x] 首次启动向导
- [x] 系统诊断中心（分组自检 + 一键自动修复）

**核心功能**
- [x] DeepSeek API 配置（存 Windows 凭据管理器）
- [x] **打印机配置与特性知识库**（13 台内置机型真实规格；选中的机型真正影响 AI 生成、几何检查与打印性评审）
- [x] **自定义打印机**（喷嘴、成型尺寸、材料、腔体、悬垂角等可自建/修改）
- [x] **成型范围硬校验**（越界自动回灌重做，仍不行则等比缩放到刚好装得下）
- [x] 3D 模型生成（自然语言 → 参数化 SCAD）
- [x] OpenSCAD 执行
- [x] STL 生成
- [x] Three.js 预览（旋转 / 平移 / 缩放 / 线框 / 自动旋转）
- [x] AI 打印性检查（基于对 STL 的真实几何测量，且以选中机型为准绳）
- [x] **需求理解确认**（先把你要的尺寸整理成可修改的规格卡片，确认后才开工；规格随项目持久化，说过的话不会再忘）
- [x] 自然语言修改（「高度改成150mm」）
- [x] **改动契约与核对**（只允许改你点名的参数；改完逐条核对「该改的改了没、别处动了没」）
- [x] 参数微调（不消耗 AI 额度）
- [x] SCAD 下载
- [x] STL 下载
- [x] 项目保存（含历史版本归档）

**文档**
- [x] [README-用户.md](README-用户.md) —— 用户说明
- [x] [README-开发者.md](README-开发者.md) —— 开发者说明
- [x] 常见问题（在用户说明第六节 + 软件内「关于」页面）

---

## 技术栈

| 层 | 技术 | 为什么 |
|---|---|---|
| 界面 | React 18 + TypeScript + Vite + 手写 CSS 设计系统 | 无组件库，界面风格可控（OpenAI / Apple / 小米 气质） |
| 3D 预览 | Three.js | WebGL 直接渲染 STL |
| 后端 | Python 3 + FastAPI + uvicorn @127.0.0.1 | 轻量、启动快 |
| AI | DeepSeek（`deepseek-chat`） | 生成参数化 SCAD |
| 3D 引擎 | OpenSCAD CLI | SCAD → STL |
| 窗口 | WebView2（pywebview），缺失时回退系统浏览器 | 不打包 Chromium，省几百 MB |
| 打包 | PyInstaller（onedir）+ Inno Setup 6 | 免 Python 运行 + 标准 Windows 安装体验 |

**刻意的取舍**：没有 numpy / trimesh（STL 解析用标准库手写）、没有 Electron / Tauri（用系统 WebView2）、
没有前端组件库（CSS 变量手写）。发布包因此保持在几十 MB 量级。

---

## 数据与隐私

- 服务只监听 `127.0.0.1`，局域网内其他电脑无法访问
- API Key 存 Windows 凭据管理器，**永不出现在源码、日志、安装包或界面上**（界面只显示掩码）
- 唯一的对外请求：调用你自己配置的 DeepSeek API；以及在你确认后从 OpenSCAD 官网下载安装包
- 你的模型、项目、STL **不会**被上传
- 用户数据放在用户目录，卸载或升级软件都不会删除

---

## 许可

**本项目自身的开源许可证还没有选定** —— 这是一个**需要作者本人决定**的事项，
不是可以随手补上的文件。当前状态：

* 仓库里**没有** `LICENSE` 文件，GitHub 会显示 "no license"；
* 按默认版权规则，**他人无权使用、修改或再分发**本项目的代码；
* `docs/LICENSE.txt` 里写的是**第三方组件清单与免责声明**，不是本项目的授权条款。

发布到 GitHub 之前请补一个 `LICENSE`（常见选择：MIT / Apache-2.0 这类宽松许可，
或 GPLv2+ —— 与本项目调用的 OpenSCAD 保持一致）。选定后 GitHub 会自动识别。
发版清单里的这一条在 [README-开发者.md](README-开发者.md) 的「开源发布前检查」中。

**重要**：本软件**不包含** OpenSCAD 的可执行文件。
OpenSCAD 是一个独立的开源程序（GPLv2+），由本软件在你需要时从
[官方网站](https://openscad.org/) 下载并校验完整性后安装。
你也可以自行安装，软件会自动识别。
