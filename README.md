# AI 3D Forge

**用一句话描述你想要的东西，AI 帮你设计出可以直接 3D 打印不烧心的模型。**

[![CI](https://github.com/xiiiii422/AI-3D-Forge/actions/workflows/ci.yml/badge.svg)](https://github.com/xiiiii422/AI-3D-Forge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D4.svg)](#系统要求)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![Release](https://img.shields.io/badge/release-v1.0.3-success.svg)](CHANGELOG.md)

[English](README.en.md) | **简体中文**

一个面向普通用户的 Windows 桌面工具：**自然语言 → 参数化 OpenSCAD → STL → 直接去打印**。
不需要懂编程，不需要安装 Python 或 Node.js，不需要打开命令行。

![欢迎页](docs/images/welcome.png)

---

## 目录

- [这是什么](#这是什么)
- [核心功能](#核心功能)
- [工作流程](#工作流程)
- [系统要求](#系统要求)
- [安装](#安装)
- [配置 DeepSeek API](#配置-deepseek-api)
- [配置 OpenSCAD（可自动安装）](#配置-openscad可自动安装)
- [如何运行](#如何运行)
- [3D 打印相关能力](#3d-打印相关能力)
- [截图](#截图)
- [你的数据与隐私](#你的数据与隐私)
- [技术栈](#技术栈)
- [常见问题](#常见问题)
- [参与贡献](#参与贡献)
- [许可证](#许可证)

---

## 这是什么

你在输入框里说一句人话，比如：

> 设计一个桌面手机支架，高度100mm。

软件会先把你这句话整理成**一份可修改的规格**（尺寸、必需结构、不要什么）让你确认，
然后用 DeepSeek 生成**参数化 OpenSCAD 源码**，调用本机 OpenSCAD 编译成 **STL**，
在网页里用 Three.js 直接预览，并对模型做**真实几何测量**（尺寸、壁厚、是否一体、支撑量）
和 AI 打印性检查；不满意就继续说人话改，最后下载 STL 去切片打印。

它不是一个"AI 画图"玩具：软件会量、会核对、会拒绝把明显不可打印的模型交给你。

## 核心功能

| 功能 | 说明 |
|---|---|
| **一句话生成模型** | 中文自然语言 → 参数化 SCAD → STL |
| **需求理解确认** | 生成前先把你的要求变成一张可改的规格卡片，确认后才开工；说过的话会被记住 |
| **真实几何测量** | 自己解析 STL：包围盒、体积、表面积、壁厚（射线法）、连通件数、内腔异物、支撑量、耗材估算 |
| **打印性检查** | 以**你选的那台打印机**为准绳给出评分、问题和改法（喷嘴/成型范围/材料/悬垂角都参与判定） |
| **AI 看图复核** | 把模型渲染成两个视角交给多模态模型看，找"数字正常但一眼就不对"的问题 |
| **自然语言修改** | 「高度改成150mm」——只改你点名的参数，改完逐条核对到底动了什么 |
| **参数微调** | 直接改参数秒级重编译，不消耗 AI 额度 |
| **成型范围兜底** | 尺寸超出你的机器时，先让 AI 重做，仍超出则确定性等比缩放到刚好装得下 |
| **预览里直接标尺寸** | 默认画出宽/深/高三条工程图式尺寸线（标签跟着视角转）；点中任意一条棱线只看那条棱的长度 |
| **3MF 交付与一键送切片** | 与 STL 同几何但**明写毫米单位**并带机型/材料元数据（避免“导入后大 10 倍”）；识别到本机切片软件时，一个按钮直接送进去 |
| **版本历史** | 每个项目自动留档，随时回退 |
| **单文件交付** | 安装包一键装好，含 3D 引擎自动安装；卸载/升级不删你的项目和 Key |

![生成中](docs/images/studio-generating.png)

## 工作流程

```
① 你说一句话
        │
        ▼
② 需求理解确认 ── 软件把要求整理成规格卡片（尺寸/结构/禁止项），你确认或直接改数字
        │
        ▼
③ DeepSeek 生成参数化 OpenSCAD 源码（提示词里带你的打印机约束）
        │
        ▼
④ 源码安全校验（拦截 import/use/surface 等危险指令，只允许纯几何建模）
        │
        ▼
⑤ OpenSCAD 编译出 STL
        │
        ▼
⑥ 程序化测量：尺寸/壁厚/连通件数/内腔异物/支撑量 → 不达标就把实测数据交回 AI 修一次
        │
        ▼
⑦ 渲染两视角 → AI 看图复核 + 打印性评审（以你的机型为准绳）
        │
        ▼
⑧ 预览（含尺寸标注）、继续提修改、参数微调、下载 3MF/STL/SCAD，或直接送到切片软件
```

## 系统要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10 1809+ / Windows 11（64 位） |
| 磁盘空间 | 约 400 MB（程序约 40 MB + OpenSCAD 约 21 MB + 项目文件） |
| 网络 | 生成模型时需要（调用你自己的 DeepSeek API）；预览与已有项目完全离线可用 |
| DeepSeek API Key | 必需（[platform.deepseek.com](https://platform.deepseek.com/api_keys)，按用量计费） |
| OpenSCAD | 必需，**软件可以自动帮你装**（官方源 + SHA-256 校验） |
| 源码运行（可选） | Python 3.10+、Node.js 18+ |

## 安装

### 方式一：安装包（推荐给普通用户）

1. 到 [Releases](../../releases) 下载 `AI-3D-Forge-Setup.exe`
2. 双击 → 一路「下一步」（默认装到 D 盘，省 C 盘空间）
3. 安装程序会顺手把 OpenSCAD 也装好；打开软件跟着欢迎向导走即可

> 详细步骤与截图级说明见 [用户快速上手](README-用户.md) 与
> [完整用户指南](docs/USER_GUIDE.md)。

### 方式二：从源码运行（适合开发者）

```powershell
git clone <仓库地址> AI-3D-Forge
cd AI-3D-Forge

$env:PIP_CACHE_DIR   = "$(Get-Location)\build\cache\pip"
$env:npm_config_cache = "$(Get-Location)\build\cache\npm"

python -m venv build\venv
& build\venv\Scripts\python.exe -m pip install -r requirements-build.txt
cd webui; npm ci; npm run build; cd ..

& build\venv\Scripts\python.exe main.py
```

完整说明（含测试、打包、CI）见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。

## 配置 DeepSeek API

1. 打开 [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) 创建 API Key
2. 软件里 **设置 → AI 服务**（或首次打开的向导）粘贴 Key
3. 点 **[测试连接]** 确认可用

- Key 保存在 **Windows 凭据管理器**里，界面只回显掩码（`sk-1234****cdef`），
  日志里也会自动打码；
- Key **不会**被写进源码、安装包或仓库，也不会随项目文件一起被分享出去；
- 想用环境变量（CI/脚本场景）：设置 `DEEPSEEK_API_KEY` 即可，读取优先级为
  凭据管理器 → 环境变量 → 本地配置文件。

## 配置 OpenSCAD（可自动安装）

软件**不打包** OpenSCAD（它是独立的 GPLv2+ 程序），而是：

1. 自动检测：常见安装位置、注册表、`PATH` 都会找，找不到才提示安装；
2. 一键自动安装：从 [openscad.org](https://openscad.org/) 官方源下载 → **校验官方 SHA-256**
   → 免管理员安装到用户目录 → 跑一次 CLI 实测确认真的能出图；
3. 你也可以自己装：软件会自动识别；识别不到时在 **设置 → 3D 引擎** 里手动指定
   `openscad.exe` 即可。

来源、版本与校验值记录在 [docs/OPENSCAD-SOURCES.md](docs/OPENSCAD-SOURCES.md)。

## 如何运行

安装包版：双击桌面图标即可（独立窗口；系统缺少 WebView2 时自动回退到浏览器，功能一致）。

源码版常用命令：

```powershell
& build\venv\Scripts\python.exe main.py            # 桌面窗口
& build\venv\Scripts\python.exe main.py --browser  # 用系统浏览器
& build\venv\Scripts\python.exe main.py --server   # 只起本地服务（默认 127.0.0.1:8760）
& build\venv\Scripts\python.exe main.py --check    # 打印环境检测报告（排障用）
```

服务只监听 `127.0.0.1`，局域网内其它电脑访问不到。

## 3D 打印相关能力

| 能力 | 具体做了什么 |
|---|---|
| 机型知识库 | 内置 13 台常见机型真实规格，推导出可执行约束（最小壁厚 = 3×喷嘴、最小孔径、最大悬垂角、桥接跨度…），也支持自建机型 |
| 尺寸适配 | 生成后按你的成型范围实测校验；超范围先让 AI 重做，仍超则等比缩放并告知比例 |
| 壁厚实测 | 对 STL 打射线量真实壁厚（最小值 / 5% 分位 / 中位数），而不是从包围盒猜 |
| 一体性检查 | 连通分量分析：模型是不是"一整件"；不是就报出散件数量、位置和体积 |
| 内腔异物 | 水平剖切检查容器内腔里有没有穿进去的材料（加强筋长错位置的典型症状） |
| 支撑与耗材 | 按悬垂面估算支撑体积，给出耗材区间（下限=2 层墙 15% 填充，上限=实体等效）与线材长度 |
| 打印性评审 | AI 结合上面的**实测数据**给出评分、风险项与具体改法，结论以你的机型为准绳 |

![结果](docs/images/studio-result.png)

## 截图

截图都在 [`docs/images/`](docs/images/)（浅色主题，样例数据来自假后端，不含任何真实账号信息）。
界面还支持深色主题与可选的**液态玻璃**材质（面板半透 + 背景模糊 + 跟随指针的高光，
透明度可调；弹窗与菜单始终是厚玻璃，不会跟着变透到读不动），
这些都在 **设置 → 外观**。

## 你的数据与隐私

- 本地服务只监听 `127.0.0.1`，并校验来源，子网或其它网页无法调用；
- API Key 存 Windows 凭据管理器，**不进源码、不进日志、不进安装包**；
- 唯一的对外请求：调用你自己配置的 DeepSeek API；以及在你确认后从 OpenSCAD 官网下载安装包；
- 你的模型、项目、STL **不会被上传**；卸载或升级都不会删除你的数据；
- 仓库里没有任何密钥、令牌、Cookie 或本机路径 —— 有自动化守卫
  （`scripts/check_no_secrets.py`，含 Git 历史扫描）在 CI 里把关。

## 技术栈

| 层 | 技术 |
|---|---|
| 界面 | React 18 + TypeScript + Vite + 手写 CSS 设计系统（无组件库） |
| 3D 预览 | Three.js（WebGL 直接渲染 STL） |
| 后端 | Python 3 + FastAPI + uvicorn（仅回环） |
| AI | DeepSeek（生成参数化 SCAD / 复核 / 打印性评审；看图用多模态模型） |
| 3D 引擎 | OpenSCAD CLI |
| 窗口 | WebView2（pywebview），缺失时回退系统浏览器 |
| 打包 | PyInstaller（onedir）+ Inno Setup 6 |

刻意的取舍：不引入 numpy/trimesh（STL 用标准库手写解析）、不用 Electron/Tauri
（复用系统 WebView2）、不用前端组件库 —— 发布包因此保持在几十 MB 量级。

## 常见问题

**Q：需要会编程吗？** 不需要。全程中文对话，不碰命令行。

**Q：要花钱吗？** 软件本身免费（MIT）。生成模型会调用你自己的 DeepSeek API，
一次设计通常远低于 1 分钱；用「应用参数」调尺寸不消耗额度。

**Q：模型能直接打印吗？** 软件会做尺寸、壁厚、一体性、支撑等检查并给出结论；
但请在实际打印前自行确认强度与安全性（尤其承重件）。

**Q：能装到 C 盘以外吗？** 可以，安装程序默认就优先选 D 盘。

**Q：卸载会删掉我的模型吗？** 不会，数据和程序分开存放。

更多问题见 [完整用户指南第七节](docs/USER_GUIDE.md#七常见问题)。

## 参与贡献

欢迎提交 Issue 与 Pull Request：

- 贡献流程、代码规范与验收标准见 [CONTRIBUTING.md](CONTRIBUTING.md)
- 开发环境、测试与构建见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)
- 安全问题请按 [SECURITY.md](SECURITY.md) 私下反馈，**不要**开公开 Issue
- 版本变更记录见 [CHANGELOG.md](CHANGELOG.md)

## 许可证

本项目采用 **MIT License**，全文见 [LICENSE](LICENSE)。

- 随包分发的第三方组件及各自许可证：见 [docs/LICENSE-THIRD-PARTY.txt](docs/LICENSE-THIRD-PARTY.txt)
- **本软件不包含 OpenSCAD**：OpenSCAD 是独立的开源程序（GPLv2+），
  由本软件在你确认后从[官方网站](https://openscad.org/)下载、校验后安装；
  其许可证全文见 [docs/LICENSE-OpenSCAD-GPLv2.txt](docs/LICENSE-OpenSCAD-GPLv2.txt)
