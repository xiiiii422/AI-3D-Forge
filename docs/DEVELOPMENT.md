# 开发者上手（安装 / 运行 / 测试 / 构建）

面向要**改代码、跑测试、打包发布**的人。
深度资料（架构、数据流、踩坑记录、发版清单）在
[`README-开发者.md`](../README-开发者.md)；这份文档只讲最短路径。

> 只想知道怎么用软件？看 [用户快速上手](../README-用户.md) 或
> [完整用户指南](USER_GUIDE.md)。

---

## 目录

- [一、前置要求](#一前置要求)
- [二、克隆与搭建](#二克隆与搭建)
- [三、运行](#三运行)
- [四、测试](#四测试)
- [五、构建与打包](#五构建与打包)
- [六、持续集成（CI）](#六持续集成ci)
- [七、文档地图](#七文档地图)
- [八、改代码前必须知道的几条规矩](#八改代码前必须知道的几条规矩)

---

## 一、前置要求

| 工具 | 版本 | 用途 |
|---|---|---|
| Windows | 10 1809+ / 11（64 位） | 本项目只面向 Windows（原生窗口、作业对象、安装器） |
| Python | 3.10+（开发机实测 3.13.14） | 后端与脚本 |
| Node.js | 18+（开发机实测 22.22.2） | 前端构建与浏览器端检查脚本 |
| OpenSCAD | 任意较新版本（自动安装会装官方 2021.01） | 真正把 SCAD 编译成 STL；**测试需要它** |
| DeepSeek API Key | 可选 | 仅端到端验收需要；CI 中**绝不**使用 |

OpenSCAD 可以不管：`scripts/selfcheck.py` 会在需要时从官方源下载校验后自动安装
（免管理员，装在用户目录里）。

---

## 二、克隆与搭建

```powershell
git clone <仓库地址> AI-3D-Forge
cd AI-3D-Forge

# 缓存重定向到项目目录，避免写满 C 盘
$env:PIP_CACHE_DIR  = "$(Get-Location)\build\cache\pip"
$env:npm_config_cache = "$(Get-Location)\build\cache\npm"

# Python 依赖（requirements-build.txt = 运行时 + 打包/测试工具）
python -m venv build\venv
& build\venv\Scripts\python.exe -m pip install -r requirements-build.txt

# 前端依赖与构建产物（后端会静态托管 webui/dist）
cd webui; npm ci; npm run build; cd ..
```

只想跑后端、不碰界面：跳过 `npm` 那一步也能启动，此时首页会返回 503 并提示
`npm run build`（这是刻意设计：宁可明确报错，也不要给一个空白页面）。

---

## 三、运行

```powershell
& build\venv\Scripts\python.exe main.py                # 桌面窗口（WebView2）
& build\venv\Scripts\python.exe main.py --browser      # 用系统浏览器打开
& build\venv\Scripts\python.exe main.py --server       # 只起服务，不开窗口
& build\venv\Scripts\python.exe main.py --port 8899    # 指定端口
& build\venv\Scripts\python.exe main.py --check        # 打印环境检测报告（排障）
& build\venv\Scripts\python.exe main.py --version
```

前后端分开开发（推荐）：

```powershell
# 终端 1：后端（热重载），API 在 127.0.0.1:8760
& build\venv\Scripts\python.exe main.py --server

# 终端 2：前端 Vite 开发服务器，已把 /api 代理到后端
cd webui; npm run dev
```

前端地址默认 `http://localhost:5273`（注意：Vite 只监听 IPv6 的 `::1`，
用 `127.0.0.1` 访问会连不上 —— 用 `localhost`）。
没有 API Key 时也可以用 `webui/mock-server.mjs` 起一个假后端，
界面类检查脚本（§四）默认就走它。

---

## 四、测试

### 1. 后端自检（不消耗 AI 额度，约 1～2 分钟）

```powershell
& build\venv\Scripts\python.exe scripts\selfcheck.py            # 完整 94 项
& build\venv\Scripts\python.exe scripts\selfcheck.py --offline  # 跳过依赖 OpenSCAD/图形界面/网络的项（CI 用）
& build\venv\Scripts\python.exe scripts\selfcheck_design.py     # 设计规格与改动机制（147 项子检查）
```

`--offline` 会把跳过的项**明确打印成 `[SKIP]` 并单独计数**，绝不把没跑的检查算作通过。
发布前必须跑完整版（不加 `--offline`）。

### 2. 端到端验收（**真实 DeepSeek + 真实 OpenSCAD**，会消耗 API 额度）

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."     # 只从环境变量读，不落盘
& build\venv\Scripts\python.exe scripts\e2e_test.py            # 76 项
& build\venv\Scripts\python.exe scripts\verify_e2e_evidence.py build\work\...   # 机制证据核对 12 条
```

它会真的移除 OpenSCAD 再走一遍自动安装、真的调用 DeepSeek 生成与修改模型、
真的把 SSE 流里的进度轨迹记下来做断言。**没有 Key 时不要跑**，也不要在 CI 里跑。

### 3. 安装/卸载/重装验收（会真实安装软件）

```powershell
.\scripts\test_install.ps1        # 46 项；跑完会备份并还原你的 config.json
```

### 4. 界面类检查（需要界面服务在跑）

```powershell
node webui\scripts\layout-audit.mjs      http://localhost:5273   # 溢出/裁切/点击区
node webui\scripts\spec-card-audit.mjs   http://localhost:5273   # 规格卡片与改动结论
node webui\scripts\audit-interface.mjs   http://localhost:5273   # 对比度/焦点/动效/排版（按 jakubkrehel/skills 条款）
node webui\scripts\interface-shots.mjs   http://localhost:5273   # 生成浅色/深色截图
node webui\scripts\verify-job-survives-navigation.mjs            # 切页/刷新后建模视图必须回来
```

这些脚本在**连不上服务**时会明确报 `*_UNREACHABLE` 并以退出码 2 结束，
不会把「没测到」伪装成「0 个问题」。

### 5. 开源合规检查（不联网）

```powershell
& build\venv\Scripts\python.exe scripts\check_no_secrets.py           # 密钥/隐私扫描（当前文件）
& build\venv\Scripts\python.exe scripts\check_no_secrets.py --history # 再扫一遍 Git 历史
& build\venv\Scripts\python.exe scripts\list_dependencies.py          # 第三方依赖与许可证清单
```

### 6. 交付物核对（需要先构建）

```powershell
& build\venv\Scripts\python.exe scripts\verify_delivery.py
```

---

## 五、构建与打包

```powershell
.\scripts\build.ps1                    # 前端构建 → 自检 → PyInstaller → 安装包（一条命令走完）
.\scripts\build.ps1 -SkipInstaller     # 只出免安装版
.\scripts\export_source.ps1            # 导出源码包（含密钥门禁）
.\scripts\make_release.ps1 -SkipBuild  # 组装发布目录 + 校验和
& build\venv\Scripts\python.exe scripts\verify_delivery.py
```

产物：

| 产物 | 路径 | 说明 |
|---|---|---|
| 免安装版 | `build\dist\AI-3D-Forge\AI-3D-Forge.exe` | 双击即用，适合内测 |
| 安装包 | `installer\Output\AI-3D-Forge-Setup.exe` | 给普通用户的一键安装包 |
| 源码包 | `build\source\AI-3D-Forge-Source.zip` | 含 `.github/`、文档、脚本 |
| 发布目录 | `build\release\` | 安装包 + 校验和 + 文档（可直接发出去） |

发版前请照着 [`README-开发者.md`](../README-开发者.md) 的「发版检查清单」逐条打勾。

---

## 六、持续集成（CI）

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) 在每次 push / PR 时跑三件事：

| 作业 | 运行环境 | 内容 |
|---|---|---|
| `audit` | ubuntu | 开源文件完整性、密钥/隐私扫描（含 Git 历史）、Python 语法、`.gitignore` 生效性 |
| `backend-offline` | windows | 安装依赖、导入冒烟、`selfcheck.py --offline`（跳过的项会列出来） |
| `frontend` | ubuntu | `npm ci` + `tsc --noEmit` + `vite build` |

### 推之前先在本地排演一遍 CI（实测做法）

为什么单独记这一节：开发机上「全绿」只说明**这台机器**绿。实测过一次直接把三个坑留给 CI：
文档指着不入库的本地产物（别人 clone 后指针死链）、`--offline` 里有两项其实需要 OpenSCAD、
预算守卫的下限把「未构建」当成了失败。三个 job 都能不依赖 GitHub 先跑一遍：

```powershell
# 先拿一份干净的仓库副本（用镜像仓或导出集克隆），下面三步全部在它里面跑
$m = "<克隆目录>"
# 0) 一个只装了 requirements.txt 的新 venv（验证依赖声明真的完整）+ 一个裸 venv（验证 audit job 不依赖任何包）
python -m venv "$env:TEMP\ci2\venv";  python -m venv --without-pip "$env:TEMP\ci1\venv"
& "$env:TEMP\ci2\venv\Scripts\python.exe" -m pip install -r "$m\requirements.txt"

# 1) audit job（不装任何依赖的那个解释器）
& "$env:TEMP\ci1\venv\Scripts\python.exe" "$m\scripts\check_oss_files.py"
& "$env:TEMP\ci1\venv\Scripts\python.exe" "$m\scripts\check_no_secrets.py" --history
& "$env:TEMP\ci1\venv\Scripts\python.exe" -m compileall -q "$m\src" "$m\scripts" "$m\main.py"
& "$env:TEMP\ci1\venv\Scripts\python.exe" "$m\scripts\check_no_secrets.py" --paths-only

# 2) backend-offline job（先清掉本机 AIFORGE_* 变量，否则测的是开发机目录）
$env:AIFORGE_DATA_DIR = $null; $env:AIFORGE_LOCAL_DIR = $null; $env:AIFORGE_PROJECTS_DIR = $null
Push-Location $m; & "$env:TEMP\ci2\venv\Scripts\python.exe" scripts\selfcheck.py --offline; Pop-Location

# 3) frontend job：不拆掉现有 node_modules 也能验出 lock 与 package.json 是否同步
Push-Location "$m\webui"; npm ci --dry-run; Pop-Location
```

预期与口径：第 2 步**失败项为 0**，跳过项比开发机多一条（需要真编译的那条会自己报原因）；
跳过的条数不写进文档，以免与开发机读数混淆。第 3 步只算「lock 同步」证据；
ubuntu 平台二进制能否装到，看 `webui/package-lock.json` 里有没有
`@rollup/rollup-linux-x64-gnu` 与 `@esbuild/linux-x64`（两者都带 `os: ["linux"], cpu: ["x64"]`）——
这是静态核对，**不能当成「在 ubuntu 上真装成功」的结论**，后者只能等首跑。

CI **不使用真实 DeepSeek API**（避免产生费用），也**不跑**端到端验收与安装包验收 ——
那两项需要 OpenSCAD、真实 Key 和一台可安装软件的 Windows，请在本地按 §四 跑。

---

## 七、文档地图

| 文档 | 读者 | 内容 |
|---|---|---|
| [`README.md`](../README.md) | 所有人 | 是什么、功能、工作流、安装、配置、许可 |
| [`README-用户.md`](../README-用户.md) | 普通用户 | 快速上手（装 → 配 → 用 → 打） |
| [`docs/USER_GUIDE.md`](USER_GUIDE.md) | 普通用户 | 完整使用说明与常见问题 |
| **本文** | 贡献者 | 安装、运行、测试、构建 |
| [`CONTRIBUTING.md`](../CONTRIBUTING.md) | 贡献者 | 分支、提交、PR 流程与验收标准 |
| [`README-开发者.md`](../README-开发者.md) | 维护者 | 架构、数据流、踩坑记录、发版清单 |
| [`docs/API-CONTRACT.md`](API-CONTRACT.md) | 前后端开发者 | 接口契约（**唯一依据**） |
| [`docs/OPENSCAD-SOURCES.md`](OPENSCAD-SOURCES.md) | 维护者 | OpenSCAD 官方来源与校验值 |
| [`SECURITY.md`](../SECURITY.md) | 所有人 | 漏洞反馈方式与安全边界 |
| [`CHANGELOG.md`](../CHANGELOG.md) | 所有人 | 版本变更记录 |

---

## 八、改代码前必须知道的几条规矩

1. **密钥永远不进仓库**。API Key 只从 Windows 凭据管理器 / 环境变量读，
   界面只回显掩码。提交前跑 `scripts/check_no_secrets.py`。
2. **`.ps1` 必须是纯 ASCII**。Windows PowerShell 5.1 会按系统 ANSI 代码页解码
   无 BOM 的 `.ps1`，中文字符串会被改坏；需要中文输出时交给 Python 脚本。
   `scripts/check_ps_encoding.py` 与自检都会拦。
3. **不要用 `Get-Content | Set-Content` 往返源码文件**：它按 ANSI 解码再写回，
   中文会变乱码、行会被吃掉，还会加 BOM。用编辑工具做精确替换。
   自检的「源码编码完好」会扫出 BOM / 替换字符 / 乱码特征字。
4. **改接口必须同步改 `docs/API-CONTRACT.md`**：那份文档是前后端契约的唯一依据，
   自检里有针对响应形状的回归断言。
5. **测试红了先分清「代码坏了」还是「这一版模型没做好」**：端到端失败会分成
   「AI 产出质量类」与「代码契约类」，后者必须为 0，不允许靠重跑掩盖。
6. **不要为了让测试通过而放宽断言**。要么修代码，要么把判据改得更准确
   （并说明为什么旧判据是错的）。
7. **不要跑着测试改源码**：自检里有几处按行号读取源码的断言，
   运行期间改文件会让它们误报。改完再跑。
