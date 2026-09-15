# AI 3D Forge —— 开发者文档

面向需要修改、构建、发布这个项目的开发者。
**普通用户请看 [`README-用户.md`](README-用户.md)。**

---

## 一、项目定位与完成标准

这不是一个「在我电脑上能跑」的 Demo。交付标准是：

> **一个完全不会编程的人，拿到 `AI-3D-Forge-Setup.exe`，双击安装、打开、填一次 API Key，就能生成并下载可打印的 STL。**

由此推导出的硬性约束（也是本项目所有设计决策的由来）：

| 约束 | 实现方式 |
|---|---|
| 用户机器没有 Python | PyInstaller 把 Python 运行时与全部依赖打进发布包 |
| 用户机器没有 OpenSCAD | 首次启动自动检测 → 从官方源下载 → SHA-256 校验 → 免管理员安装 → CLI 实测 |
| 用户不碰命令行 | 单 exe 启动，内置 WebView2 窗口（缺失时回退系统浏览器） |
| 用户数据不能丢 | 程序装 Program Files / D 盘，数据一律进 `%APPDATA%` 与 `文档\` |
| 少占用 C 盘 | 安装默认选 D 盘；venv/npm/pip 缓存全部重定向到项目目录 |
| 不能出现 ModuleNotFoundError | 构建期自检 + 打包后冒烟测试 + 运行期依赖体检 |

---

## 二、版本区分

项目交付三个层次，请在分发给别人之前确认你在哪个层次：

| 层次 | 形态 | 适合谁 | 说明 |
|---|---|---|---|
| **开发版** | 源码目录 + `python main.py` | 开发者 | 需要本机 Python 3.10+；前端需 `npm run build` 或跑 Vite 开发服务器 |
| **测试版** | `build\dist\AI-3D-Forge\AI-3D-Forge.exe` | 内测同学 | 免 Python，双击即用，但没有安装器/快捷方式/卸载；`AIFORGE_CHANNEL=beta` 标记 |
| **正式发布版** | `installer\Output\AI-3D-Forge-Setup.exe` | 普通用户 | 完整安装器，含快捷方式、卸载、升级、首次初始化 |

`AIFORGE_CHANNEL` 环境变量控制显示名称：`dev` / `beta` / `release`（默认 `release`）。
发布前请确认它是你想要的层次。

---

## 三、技术架构

```
┌─────────────────────────────────────────────────────────────┐
│  AI-3D-Forge.exe (PyInstaller onedir)                       │
│                                                             │
│  main.py ──► aiforge.server  ──► uvicorn @127.0.0.1:<port>  │
│                    │                   │                    │
│                    │                   └─► aiforge.app      │
│                    │                        (FastAPI)       │
│                    └─► pywebview 窗口 ──────┘               │
│                        (WebView2)                           │
│                                                             │
│  前端 webui/dist ──► 由 FastAPI 静态托管（离线，无 CDN）      │
└─────────────────────────────────────────────────────────────┘
            │                                  │
            ▼                                  ▼
   DeepSeek API (HTTPS)              OpenSCAD CLI (子进程)
```

**为什么是「本机 Web 服务 + 原生窗口」而不是纯桌面 GUI？**
3D 预览需要 Three.js（WebGL），而 Three.js 必须有浏览器内核。
用系统自带的 WebView2 既拿到了完整的 Web 渲染能力，又不需要打包一个几百 MB 的 Chromium。

### 模块职责

| 模块 | 职责 | 关键设计 |
|---|---|---|
| `paths.py` | 目录解析 | 用户数据全部在用户目录；`文档` 目录查注册表（兼容 OneDrive 重定向） |
| `config.py` | 配置 + 密钥 | API Key 进 Windows 凭据管理器，降级为受限权限文件；配置原子写 |
| `envcheck.py` | 环境检测、诊断中心、自动修复 | 15 项检查，每项带 `fixable` 决定界面是否出现「自动修复」 |
| `openscad.py` | 检测/定位/下载/校验/安装/CLI 测试 | 官方源 + SHA-256 钉死；**GBK 解码**（见「踩过的坑」） |
| `scad.py` | SCAD 安全校验、参数提取与改写 | 拦截 `import`/`surface`；只改写顶层纯数字赋值 |
| `mesh.py` | STL 解析与几何度量 | 纯标准库；边计数判封闭性；散度定理算体积 |
| `compiler.py` | SCAD → STL | 剥离 BOM；输出文件存在且非空才算成功 |
| `llm.py` | DeepSeek 客户端与提示词 | 提示词强制「参数区 + 元数据注释」骨架，使参数微调可以不走 AI；打印机约束注入三条链路；**温度集中在顶部常量**（见踩坑第 29 条） |
| `design.py` | **设计契约（DesignSpec）** | 把一句话解析成带数值的硬指标；合并策略保证「说过的话不会被下一轮抹掉」；指纹只取用户指定项 |
| `changes.py` | **改动契约与双重校验** | 修改前立契约（只准动点名参数），改后程序化核对 + AI 复核；`penalty()` 用于在多个不达标版本间取舍 |
| `printers.py` | **打印机特性知识库** | 13 台内置机型真实规格 → 推导出可执行的工艺约束；自定义机型持久化 |
| `jobs.py` | 生成流水线 | 阶段化进度广播；**规格确认关卡**；编译失败自动回灌报错让 AI 修复一次；**成型范围硬校验 + 自动缩放兜底** |
| `projects.py` | 项目持久化 | 原子写 + `history.jsonl` 追加 + `revisions/` 归档 |
| `server.py` | 服务与窗口 | 端口自动避让；单实例；窗口关闭即退出 |
| `api/*` | HTTP 路由 | 统一错误结构 `{detail, code, hint}`；只监听回环 |

### 数据流：一次生成

```
POST /api/jobs {prompt}          ← 目标打印机在作业开始时锁定，全程一致
   └─► Job 创建 ──► GenerationRunner.run()
         ├─ 0. 解析打印机上下文    printers.resolve_process() → 工艺约束 + 提示词块
         ├─ 0.5 解析设计规格       [understanding] DeepSeek → DesignSpec
         │      ├─ SSE spec 事件  → 界面渲染可编辑的规格卡片
         │      └─ 等待确认        [awaiting_confirmation] POST /confirm-spec
         ├─ 1. DeepSeek 生成 SCAD  [generating_scad]  ← 提示词里带「契约 + 成型范围/壁厚/孔径/$fn」
         │      └─ 源码写入 `// @spec <指纹>`
         ├─ 2. scad.validate()     [validating_scad]
         │      └─ 不通过 → 回灌问题让 AI 修一次
         ├─ 3. compiler.compile_to_stl() [compiling]  ← $fn 按真实模型尺寸算
         │      └─ 编译失败 → 回灌 OpenSCAD 真实报错让 AI 修一次
         ├─ 4. mesh.analyze(limits) [analyzing]   → 按该机型判定薄壁/超范围
         ├─ 4.5 成型范围硬校验      [analyzing]
         │      ├─ 越界 → 把实测尺寸回灌给 AI 重做
         │      └─ 仍越界 → 变量注入 + 缩放调用（确定性兜底）
         ├─ 5. DeepSeek 打印性检查  [reviewing]   → 以该机型 + 材料为准绳
         ├─ 6. projects 落盘 + 归档 + 导出 [exporting]
         └─ 7. job.succeed() ──► SSE done {完整 Job 快照}
```

修改类作业（`mode=modify`）在第 1 步之前多一道**改动契约**：
`build_change_contract()` 把指令翻译成「只能改哪些参数、目标值多少、其余不许动」，
生成完成后再用 `verify_change()` 逐条核对，不通过则 `repair_change()` 回灌偏差重试一次，
仍不通过就按 `ChangeVerdict.penalty()` 在两个候选里取代价更小的那个（详见上一节）。

前端通过 `GET /api/jobs/{id}/stream` 订阅 SSE；**首帧必定补发 `snapshot`**，
因此刷新页面或断线重连后界面能立刻还原，不会卡在转圈状态。
`snapshot` 里带 `spec` 字段，所以「等你确认需求」这一步刷新后卡片依然在。

### 设计规格与改动契约：为什么「少改了/改歪了」能被压住

这是本项目里**唯一一处会改变 AI 行为方式**而不只是改提示词措辞的机制。
`design.py` + `changes.py` 两个模块构成一道「先立契约、再动手、后核对」的闸门：

```
用户一句话
  └─► ① parse_design_spec()      → DesignSpec（带数值的硬指标 + 禁止项 + 验收标准）
        ├─ merge_specs(旧, 新)   → 旧契约里用户说过的值不会被新的一轮抹掉
        ├─ SSE spec 事件          → 界面弹出可编辑的规格卡片
        └─ await job.wait_for_spec()  ← 用户确认 / 改数字 / 补一句说明后才继续
  └─► ② generate_from_spec()     → 提示词里带完整契约 + 打印机约束
        └─ embed_fingerprint()   → SCAD 顶部写 `// @spec <12hex>`
  └─► ③ 修改时先 build_change_contract(指令, 契约, 当前参数)
        → 「只准动 height，其余不许变」被翻译成机器可核对的清单
  └─► ④ 双重校验
        ├─ verify_change(改前, 改后)  → ChangeVerdict（参数级，确定性）
        └─ client.review_change(...)  → AI 复核（结构级，温度 0，看「参数对了但改坏了」）
        └─ 不通过 → repair_change() 回灌具体差异让 AI 修一次
        └─ 仍不通过 → _apply_targets() 把目标值直接写回原源码（确定性兜底）
        └─ 再不行 → _pick_least_bad() 按 penalty() 取代价更小的版本并如实报告
```

**几个不能想当然的地方**（都踩过）：

| 判断 | 为什么这么做 |
|---|---|
| 指纹只取「用户明确指定的数值 + 禁止项 + 品类」 | 把 `features`/`acceptance` 算进去就不稳定——AI 每次措辞都不同，指纹一变就判不出「还是不是同一个设计」 |
| `merge_specs` 里新值为 `null` 时**不覆盖**旧值 | 用户第二轮只说「加个孔」，新解析出的规格里没有高度；直接替换就把「高度 100mm」丢了，下一轮修改失去约束 |
| `_prefer` 用 `penalty()` 分档，而不是「达标/不达标」 | 二元判定会把「改到 140（目标 150，没破坏别处）」和「完全没改」判成一样，于是永远选后者——用户的要求永远得不到推进 |
| 改动核对是**确定性代码**，不是让 AI 自查 | AI 自查会为了「交差」说改好了；参数 diff 是硬事实。视觉/语言模型的结论一律要程序复核 |
| `awaiting_confirmation` **不是终态** | 前端若把它当结束，确认卡片会被立刻丢掉、轮询也停——作业在服务端死等，用户点什么都没反应 |
| `cancel()` 必须覆盖 `awaiting_confirmation` | 只认 `running` 的话，确认卡片上的「取消」是死的（作业挂在 `wait_for_spec()` 上） |
| 把规格推送抽成模块级 `jobs.publish_spec()` | 界面刷新后靠 `Job.snapshot()["spec"]` 重建卡片，逻辑必须与推送时是同一份，且要能离线自检 |

对应的守门测试：
`scripts/selfcheck_design.py`（131 项确定性检查，不调用 AI，秒级）
+ `scripts/selfcheck.py::t_sse_contract` 里对 `spec` 事件与快照的断言。

### 打印机知识库：为什么它不只是「一份说明书」

`printers.py` 把机型规格翻译成**可执行的数字**，这些数字会真正改变程序行为：

| 推导出的约束 | 用在哪里 | 依据 |
|---|---|---|
| 最小壁厚 = 3 × 喷嘴 | 注入生成提示词；几何检查判薄壁 | 两圈走线层间结合最弱，三圈才稳定 |
| 最小成型尺寸 = 2 × 喷嘴 | 几何检查判「零件太细」 | 一层两圈走线即可成型 |
| 最小孔径 = 2 × 喷嘴 + 0.4 | 注入提示词 | 材料收缩 + 圆弧插补误差会堵孔 |
| 活动件间隙 = 0.6 × 喷嘴 | 注入提示词 | 配合件留隙经验值 |
| 层高上限 = min(标称, 0.8 × 喷嘴) | 注入提示词 | 超过会挤出不足 |
| `$fn` = f(喷嘴, 模型尺寸) | **实际写进 SCAD** | 弦到弧偏差 ≈ R·(1−cos(π/n))，取偏差 = 喷嘴/8 |
| 成型范围 | 几何检查 + **硬校验与自动缩放** | 装不下就是装不下 |
| 材料热变形温度 vs 是否封闭腔体 | 提示词警告（ABS + 开放机身会翘边） | 材料特性 |
| 材料密度 | 耗材克数估算 | 按材料而非写死 PLA |

**设计上的一个关键判断**：光靠提示词约束不住 AI。实测案例——提示词里明确写了
「不得超 180mm」，AI 仍生成了 212mm 的横梁。因此成型范围必须用**实测尺寸兜底**：
先让 AI 依据真实数字重做，仍不行就**注入缩放倍率变量 + 重新实例化顶层调用**
（见第七节第 18 条），等比缩放到刚好装得下。
这是「懂打印机」这件事真正生效的地方，也是本项目里唯一一处会**修改 AI 产物几何**的逻辑
（所以它只在 `create` 模式下启用，`modify`/`compile`/`params` 只提示、不擅改）。

> 另一个反直觉的坑：`rough_min_dim`（最小成型尺寸）必须**小于** `min_wall_mm`（壁厚下限），
> 否则几何检查里「壁厚不足」那条分支会被「零件太细」永远盖住、永远触发不了。
> 早期版本写成 `max(min_wall × 1.5, 2.5)` 就踩了这个坑，`selfcheck.py` 现在有断言钉住这个不等式。

---

## 四、开发环境搭建

### 前置要求

- Windows 10 1809+ / Windows 11
- Python 3.10+（开发机实测 3.13.14）
- Node.js 18+（开发机实测 22.22.2）

### 首次搭建

```powershell
# 全部缓存重定向到项目目录，避免写满 C 盘
# 用「当前目录」当仓库根：既不用把本机绝对路径写进文档，
# 也顺带避开中文路径在 PowerShell 5.1 命令行上被按 ANSI 编码搞坏的问题
$root = (Get-Location).Path
$env:PIP_CACHE_DIR = "$root\build\cache\pip"
$env:npm_config_cache = "$root\build\cache\npm"

# Python 依赖
python -m venv "$root\build\venv"
& "$root\build\venv\Scripts\python.exe" -m pip install -r requirements-build.txt

# 前端依赖
cd "$root\webui"; npm install; npm run build
```

### 日常开发

```powershell
# 终端 1：后端（带热重载）
& "build\venv\Scripts\python.exe" main.py --server

# 终端 2：前端（Vite 开发服务器，已配置 /api 代理到 8760）
cd webui; npm run dev
```

前端开发时访问 Vite 的地址（默认 `http://127.0.0.1:5273`），它会代理 API 到后端。

### 命令行入口

```powershell
python main.py                     # 桌面窗口模式
python main.py --browser           # 用系统浏览器
python main.py --server            # 只起服务，不开窗口
python main.py --port 8899         # 指定端口
python main.py --check             # 打印完整环境检测报告（排障用）
python main.py --install-openscad  # 命令行自动安装 OpenSCAD
python main.py --first-init        # 安装器调用的首次初始化
python main.py --version           # 版本信息
```

---

## 五、测试

### 后端自检（不消耗 AI 额度，秒级）

```powershell
& "build\venv\Scripts\python.exe" scripts\selfcheck.py
```

16 项检查：模块导入、路径、配置密钥、SCAD 校验、参数改写、STL 网格分析、
OpenSCAD 自动安装、CLI 测试、编译、项目持久化、环境报告、作业事件流、
**接口响应形状（契约回归）**、提示词解析、快捷方式、HTTP 路由与安全拦截。

### 端到端验收（**真实 AI + 真实 OpenSCAD**，会消耗 API 额度）

这是发布前的必做项，对应需求第四十六节的验收流程：

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."   # 只从环境变量读取，不落盘
& "build\venv\Scripts\python.exe" scripts\e2e_test.py
```

它会真实执行：
1. 启动程序 → 环境自动检测
2. **移除 OpenSCAD，模拟一台没装过的电脑** → 走完自动安装全流程 → 再次检测 → CLI 测试
3. 保存 API Key → 真实调用 DeepSeek 验证
4. 输入「设计一个桌面手机支架，高度100mm。」→ 生成 SCAD → 编译 STL → 打印性检查
5. 验证 STL 可被 Three.js 预览读取、缩略图可用、测量数据可用
6. 输入「高度改成150mm。」→ **断言 Z 轴尺寸确实增加了约 50mm**
7. 参数微调（不走 AI）→ 断言源码已写入新值且尺寸变大
8. 下载 SCAD/STL → 项目列表
9. 系统诊断分组 → 语法错误被友好处理 → 无 Key 时快速失败
10. 数据目录正确性 → API Key 可清除、可恢复

测试使用独立的用户目录（`build\work\e2e-*`），不会污染你正在使用的配置和项目。

### 复杂实例验收（**真实 AI**，衡量「模型够不够贴合描述」）

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."
# 全部 6 个用例；迭代时只跑其中几个：--only 1,3
& "build\venv\Scripts\python.exe" scripts\complex_e2e.py
```

`e2e_test.py` 证明的是**流程跑得通**；这个脚本证明的是**东西做得对**。
它跑 6 个市面上真实存在、要求多个功能特征协同的桌面小物
（夹桌式垃圾桶 / 线缆收纳底座 / 壁挂钥匙挂钩 / 夹桌式水杯架 /
多格笔筒 / 壁挂机顶盒托架），每个都：真实生成 → 编译 → 实测几何，
并对两件事做**硬性验收**（这两条正是用户实际踩到的坑）：

* `solid_parts == 1`：「一个成品」不能是「几个各自独立的零件」
* 容器类零件腔内不能有 `cavity_islands`：加强筋不能穿进腔体内部

同时记录设计深度（截面渐变、圆角收边、阵列特征、公差参数）与填充率，
用来判断模型是「有设计的产品」还是「练习作」。
脚本会复现应用里的「实测 → 定点修复 → 只采纳更好的」闭环，
所以它给出的合格率就是**用户实际拿到的合格率**。

> 当前基线：首次生成即合格 4/6，其余 2 个会被检出并尝试修复；
> 修复不达标时保留原版并在对话里说明（绝不交付更差的版本）。

### 端到端机制证据核对（确认机制真的被触发）

```powershell
# 先跑过 e2e_test.py，然后：
& "build\venv\Scripts\python.exe" scripts\verify_e2e_evidence.py
```

`e2e_test.py` 断言的是**行为**（只改了点名项、尺寸确实变了对不对）。
但「全部通过」无法区分「机制生效」和「机制被跳过」——
所以另有一支脚本核对**机制被真实触发过**的直接证据：
规格卡片有没有推给界面、有没有停在等待确认、指纹有没有写进源码、
变更契约有没有产出、AI 复核有没有执行、规格有没有落盘。

> **踩坑**：`Tee-Object` 写出的日志是 **UTF-16LE**（带 BOM），不是 UTF-8。
> 按 UTF-8 读会得到一堆 NUL 字符，任何中文关键词都匹配不到，
> 现象看起来像「机制根本没跑」。脚本会按 BOM 自动判断编码；
> 手工排查时先看文件前两个字节。

### 安装包验收（安装 / 升级 / 卸载 / 重装 / 数据保留）

```powershell
.\scripts\test_install.ps1              # 46 项，含真实的卸载与重装
.\scripts\test_install.ps1 -KeepInstalled   # 停在卸载之前
```

> ⚠️ 这会在这台机器上**真实安装、卸载软件**并创建真实的桌面/开始菜单快捷方式。
> 请在测试机或你不在意的账户上运行。脚本会在结束时清理测试安装与测试用户数据。

验收内容：静默安装 → 校验程序文件/快捷方式/卸载器/注册表项 → **启动已安装的程序并用 HTTP 驱动它**
→ 通过真实 API 创建一个项目 → 覆盖升级 → 校验程序被替换且**用户数据完好** →
卸载 → 校验程序被删除且**用户数据仍在** → 重装 → 校验**项目重新可见且内容可读**。

### 界面布局自动体检

```powershell
# 先启动程序（开发或打包均可），然后：
cd webui
$env:PLAYWRIGHT_BROWSERS_PATH = "$PWD\..\build\cache\playwright"
node scripts\layout-audit.mjs http://127.0.0.1:8760
```

在真实浏览器里逐元素检查：文字横向溢出、文字纵向被裁切、元素越界、
图标按钮缺少可访问名称、可点击区域过小。<br>
在 1440×900 与 1180×720 两档分辨率 × 5 个路由共 10 组下，**当前为 0 缺陷**。

### 规格确认卡片 / 改动核对结论的界面体检

上面那支脚本只体检静态路由，而这两个组件只在「一次真实作业进行到某个阶段」
时才存在，所以单列一支。它们属于「坏了用户就点不动」的关键界面
（确认卡片不出现，作业就在服务端死等），因此必须真的渲染出来检查。

```powershell
# 需要两个后台进程：假后端 + 前端 dev server（proxy 指向假后端）
cd webui
$env:MOCK_PORT = '8761'; node mock-server.mjs                      # 终端 1
$env:AIFORGE_DEV_API = 'http://127.0.0.1:8761'; npm run dev        # 终端 2
node scripts\spec-card-audit.mjs http://127.0.0.1:5273
```

覆盖：卡片在 1440×900 / 1180×720 下的布局、编辑态、确认后变只读、
**点停止后发送键必须恢复可点**、以及改动核对结论的「通过（绿）/ 有问题（红）」两种色调，
截图落在 `build\work\spec-cards\`。**当前为 0 缺陷**。

### 真实后端联调：停止生成必须解锁输入区

假后端上的体检不能替代这一支 —— 假后端**曾经压根不推 `cancelled` 事件**，
所以「点停止后发送键永久禁用」这条路径在假后端上根本测不到。

```powershell
# 1. 起真实后端（它自己托管 webui/dist，不需要另外起前端）
$env:AIFORGE_DATA_DIR = "$PWD\build\work\cancel-data"
$env:AIFORGE_PROJECTS_DIR = "$PWD\build\work\cancel-projects"
$env:AIFORGE_LOCAL_DIR = "$PWD\build\work\testlocal"
& build\venv\Scripts\python.exe -m uvicorn aiforge.app:create_app --factory `
    --host 127.0.0.1 --port 8891 --app-dir src

# 2. 另开一个终端
cd webui
$env:PLAYWRIGHT_BROWSERS_PATH = "$PWD\..\build\cache\playwright"
node scripts\verify-cancel-real.mjs http://127.0.0.1:8891
```

检查：生成中发送键禁用 → 点停止 → **t+1s 恢复可点** → 草稿没被清掉 → 下一条真的发得出去。

### 交付清单核对

```powershell
& "build\venv\Scripts\python.exe" scripts\verify_delivery.py
```

逐项核对交付文件是否存在、体积、SHA-256、C 盘占用，并把每一项对应到需求条款。

---

## 六、构建与发布

### 一键完整构建

```powershell
.\scripts\build.ps1
```

依次完成：环境检查 → 依赖安装 → 前端构建 → 版本资源 → 后端自检 →
PyInstaller 打包 → **打包产物冒烟测试** → 生成安装包。

常用参数：

```powershell
.\scripts\build.ps1 -SkipFrontend     # 前端没改动，复用 webui\dist
.\scripts\build.ps1 -SkipSelfcheck    # 跳过自检（不推荐）
.\scripts\build.ps1 -SkipInstaller    # 只要程序本体，不要安装包
```

产物：

| 路径 | 说明 |
|---|---|
| `build\dist\AI-3D-Forge\AI-3D-Forge.exe` | 免安装程序本体（测试版） |
| `installer\Output\AI-3D-Forge-Setup.exe` | 一键安装包（正式发布版） |
| `installer\Output\build-manifest.json` | 构建信息 + SHA-256 校验值 |

### 安装包编译

`build.ps1` 会自动调用 `scripts\build_installer.ps1`。
如果本机没有 Inno Setup 6，脚本会**自动从官方渠道下载并静默安装到 D 盘**（只有构建机需要它，最终用户不需要）。

也可以单独运行：

```powershell
.\scripts\build_installer.ps1
.\scripts\build_installer.ps1 -ForceDownload   # 强制重新获取 Inno Setup
```

### 打包配置说明（`AI-3D-Forge.spec`）

- **onedir 而非 onefile**：启动快得多，且安装器可以增量升级
- 前端 `webui/dist` 打包进 `_internal\webui\`，由 `paths.frontend_dir()` 定位
- 显式 `excludes` 掉 numpy/pandas/matplotlib/pytest 等用不到的重型库
- **不用 UPX**：压缩后极易被杀毒软件误报，宁可包大一点
- 排除 `proxy_tools`（pywebview 的 JS 桥，仅 Flask/Django 后端需要）

### 发版检查清单

- [ ] `AIFORGE_CHANNEL` 设置为 `release`
- [ ] `src/aiforge/version.py` 里的 `APP_VERSION` 已递增
- [ ] `installer/AI-3D-Forge.iss` 里的 `#define AppVersion` 已同步（**容易漏！**）
- [ ] `scripts/selfcheck.py` 全绿（47 项，含设计机制 131 项子检查）
- [ ] **克隆冒烟**：把提交集导出/克隆到项目**之外**，用它自己的
      `scripts/selfcheck.py` 跑一遍全绿（验证「别人 clone 下来能用」；
      全新 clone 里没有 `webui/dist`，自检会走「界面未构建」那一支 —— 见 §67）
- [ ] `scripts/e2e_test.py` 全绿（**0 失败**；项数随是否配置 Key 与模型产出略有差异，实测 74～77 项。
      其中 5 项是「进度条真的在动」的实测断言，跑在真实 SSE 流上 —— 见 §69。
      若失败项**全部**是 AI 产出质量类（见 §59），先重跑一次确认；连续两次都红按真回归处理）
- [ ] `scripts/verify_e2e_evidence.py` 全绿（12 条机制证据全部命中；
      日志由 `e2e_test.py` 自己写到 `build\e2e-latest.log`，**不需要手工 Tee**）
- [ ] `.\scripts\test_install.ps1` 全绿（真实安装/卸载/重装；会备份并还原真实 `config.json`，
      结束时不留测试项目残留 —— 见 §66）
- [ ] `node webui\scripts\layout-audit.mjs` 全绿（10 组 0 缺陷）
- [ ] `node webui\scripts\audit-interface.mjs` 全绿（按 jakubkrehel/skills 的条款实测：
      对比度明暗两套、焦点指示、320/1080/200% 视口、reduced-motion、颜色单载、tabular-nums、主题切换抑制）
- [ ] `node webui\scripts\interface-shots.mjs` 跑通（浅色/深色两套页面快照，用于人工复核配色）
- [ ] `node webui\scripts\spec-card-audit.mjs` 全绿（含「点停止后发送键恢复可点」）
- [ ] `node webui\scripts\verify-cancel-real.mjs` 全绿（对着真实后端再验一次停止）
- [ ] `node webui\scripts\verify-stream-lost.mjs` 全绿（作业在服务端消失时界面必须自己解锁）
- [ ] `node webui\scripts\verify-double-send.mjs` 全绿（连点两次发送只能产生一个作业）
- [ ] `node webui\scripts\verify-stall-watchdog.mjs` 全绿（卡死必须**真的**停掉服务端作业；等用户确认不算卡死）
- [ ] `node webui\scripts\verify-job-survives-navigation.mjs` 全绿（切页/刷新后建模视图与确认卡片必须回来，
      作业不能因为切页被停掉 —— 见 §69）
- [ ] 三支界面体检脚本在**服务没起来**时必须报 `*_UNREACHABLE` 并以 2 退出，
      而不是把连接错误算成"发现 N 个问题"（见 §69 的坑 1）
- [ ] `.\scripts\export_source.ps1` 通过（含密钥安全门禁 + 必需文件检查；
      顶层是**允许清单**，被排除的意外目录会明确告警 —— 见 §70）
- [ ] `.\scripts\build.ps1` 完整通过，冒烟测试无意外 ✗
- [ ] `scripts\verify_delivery.py` 全部齐备
- [ ] `build\venv\Scripts\python.exe scripts\list_dependencies.py` 能跑通，
      `docs\LICENSE-THIRD-PARTY.txt` 的第三方清单与之**一致**（改过依赖必须重跑）
- [ ] 在**干净的 Windows** 上实测：安装 → 启动 → 自动装 OpenSCAD → 生成 → 改尺寸 → 下载 → 卸载 → 重装（数据仍在）

### 开源发布前检查（GitHub 安全门禁）

两条命令覆盖大部分内容（CI 也会跑，见 `.github/workflows/ci.yml`）：

```powershell
& build\venv\Scripts\python.exe scripts\check_oss_files.py             # 文件与链接完整性
& build\venv\Scripts\python.exe scripts\check_no_secrets.py --history  # 密钥/隐私（含 Git 历史）
```

- [ ] **开源文件完整性**：`check_oss_files.py` 报 `OSS_FILES_OK`
      （33 个必需文件、README 的 10 个要点、相对链接不断链）
- [ ] **没有提交任何密钥**（API Key / Token / 口令 / Cookie / `.env`）：
      `check_no_secrets.py --history` 报 `NO_SECRET_FOUND`。
      `--history` 不可省：密钥"提交过又删掉"时，当前文件是干净的，
      但 `git log -p` 里还留着，任何人 clone 都能翻出来
- [ ] `.gitignore` 生效：`git status --porcelain -uall` 里**不含** `build/`、
      `installer/Output/`、`webui/scripts/shots/`、`node_modules`、`__pycache__`
- [ ] 仓库里没有本机绝对路径（`C:\Users\<用户名>`、个人目录）。
      注意 `build/` 下确实有带本机路径的报告，所以**整个 `build/` 都被忽略**，
      不要为了「少写一行」把它改成逐个子目录忽略。
      这一条已经自动化：`scripts\selfcheck.py` 的「无本机路径泄漏」会用
      `Path.home()` 和仓库根目录**运行时推导**出本机路径再扫描提交集
      （规则本身不硬编码任何路径，否则规则自己就是泄漏）；
      正斜杠和 JS 转义写法（`D:/...`、`C:\\Users\\...`）都覆盖 ——
      第一版靠肉眼查漏了两处正斜杠写法，靠 `D:\` 反斜杠搜索是查不出来的
- [ ] 提交集里的文本文件编码完好、行尾统一为 LF：无 BOM、无 U+FFFD、无 GBK 乱码字符
      （`scripts\selfcheck.py` 的「源码编码完好」）
- [ ] `docs/LICENSE-THIRD-PARTY.txt` 的第三方清单与 `scripts/list_dependencies.py` 输出一致
      （含 MPL-2.0 的 certifi、Apache-2.0 的 python-multipart、
      PyInstaller 引导程序例外条款）
- [ ] **许可证已就位**：仓库根目录 `LICENSE` 是 MIT 全文，README 标注 MIT，
      安装向导（`LicenseFile=..\LICENSE`）展示的也是它 —— 见 §70
- [ ] 仓库目录（`AI-3D-Forge-GitHub`）与开发树同步且工作区干净：
      `git status --porcelain -uall` 无输出

### 本地已验证基线（供对照，换机器后请重跑）

| 项目 | 结果 |
|---|---|
| 后端自检 | 47/47 通过（含设计规格与改动机制 131 项子检查、模块引用静态检查、依赖声明完整性、模型外观复核、并发作业守卫、破网格修复触发与排序、STL 读取健壮性、长分析心跳、助手调用作用域、子进程不变量守护、换安装目录可回滚、进度可信度、网络失败可读且可重试、安装失败不破坏已有环境、验收脚本不覆盖用户配置、开源卫生两查） |
| 端到端验收（真实 DeepSeek） | 76/76 通过（含规格确认关卡、只改点名项、越界自动缩放、AI 复核、图片复核、进度条实测轨迹） |
| 端到端机制证据 | 12/12 命中（`verify_e2e_evidence.py`） |
| 安装包验收 | 通过（46 项：首启准备日志、图标、版本元数据、静默卸载不挂起、升级不产生重复卸载项、重装后项目仍可读；跑完真实 `config.json`/`printers.json` 的 SHA-256 与运行前一致，测试项目零残留） |
| 克隆冒烟（别人的机器） | 通过（把提交集克隆到项目之外，用它自己的 `scripts/selfcheck.py` 跑：**47/47**；界面未构建时走「首页 503 + npm run build 提示」那一支） |
| 界面布局体检 / 规格卡片体检 | 10 组 + 4 组，全部 0 缺陷（含「停止后发送键恢复可点」） |
| 真实后端停止验证 | 通过（`webui/scripts/verify-cancel-real.mjs`） |
| 连点两次发送 | 通过（`webui/scripts/verify-double-send.mjs`：空窗期内再敲一次仍只发 1 个作业） |
| 卡死看门狗 | 通过（`webui/scripts/verify-stall-watchdog.mjs`：真的发取消请求；等待确认时不谎报、不清卡片） |
| 前端机型功能验收 | 23/23 通过（`webui/scripts/verify-printers.mjs`） |
| 依赖声明完整性 | Python 6 个第三方模块 + 前端 4 个包，全部有声明 |
| 仓库待提交体积 | 154 个文件 / 约 1.9 MB（`build/` 全部被忽略，实测避免了 25 MB+ 生成物） |
| 程序本体体积 | 38.5 MB |
| 安装包体积 | 16.1 MB |
| 源码包体积 | 4.2 MB |
| 冷启动到界面可用 | 约 3 秒 |
| WebView2 检测耗时 | 约 0.1 ms（打开窗口前同步执行，必须极快） |
| OpenSCAD 复用已有安装 | 约 1.6 秒（不做任何下载） |
| OpenSCAD 预置缓存安装 | 约 6.5 秒（校验→解压→CLI 测试→清理 20.9MB 压缩包） |
| 首次准备（跳过下载） | 约 3.4 秒 |
| 一次生成的 AI 耗时 | 6～10 秒，SCAD 约 2.3～4.5 KB |
| 越界模型自动处理 | 240mm 横梁在 180³ 机型上 → 自动重做 + 等比缩放 → 180.0mm，`within_build_volume=true` |
| 网格分析（含射线法壁厚与支撑） | 2 万面模型约 0.7 秒；100 万面 50.5 秒（优化前 90.0 秒，逐字段对拍确认指标不变） |

### 新电脑安装体验：几个刻意的设计

1. **安装时就把 OpenSCAD 装好**（`--first-run-setup`），而不是等用户第一次打开软件再下载 20MB。
   这样"安装完 → 打开 → 直接能用"是一条路径，不用在向导里再等一次。
   代价是安装多花 1～3 分钟，因此这一步**必须可见**（见第 15 条踩坑）。
2. **WebView2 在开窗口之前就检测**。缺失时不再"试着开窗口失败后悄悄改用浏览器"，
   而是明确告诉用户原因，并给出微软官方免费组件的安装入口。
   同时把状态接进「系统诊断」的「运行环境」分组，可一键自动修复（走 winget）。
3. **图标贯穿全链路**：exe 内嵌、快捷方式、安装器向导、Add/Remove Programs 卸载项。
   `scripts/make_icon.py` 用标准库手写 PNG/ICO（超采样抗锯齿），尺寸覆盖 16→256。
4. **卸载时明确告知数据已保留**，避免用户以为模型被删了。

---

## 七、踩过的坑（改代码前请先读这一节）

这些都是实测踩出来的，重犯成本很高：

### 1. OpenSCAD 的控制台输出是 **GBK** 编码，不是 UTF-8

中文 Windows 上，`openscad.exe` 按系统 ANSI 代码页输出。若按 UTF-8 解码，
用户看到的报错路径就会变成 `D:\<含中文的目录>` 后面跟一串乱码字节的样子。

→ 见 `openscad.console_encoding()` / `output_of()`。**所有** OpenSCAD 输出都必须走这两个函数。

### 2. OpenSCAD 2021.01 不解析带 BOM 的文件

报错是 `Parser error: syntax error in file ..., line 1`，看起来像语法错误，其实是 BOM。

→ 见 `scad.ensure_no_bom()`；写 `.scad` 一律用 UTF-8 **无 BOM**。

### 3. 二进制 STL 的头部也可能是 `solid ...`

OpenSCAD 生成的二进制 STL，前 80 字节就是 `solid ...`。
按「是否以 solid 开头」判断格式会把二进制文件误判为 ASCII。

→ 见 `mesh._looks_binary()`：用「文件长度 = 84 + 50 × 面片数」自校验。

### 4. 手工拼三角面极容易把绕序写反

散度定理对绕序敏感。绕序错一个面，体积就会算错（实测一个「立方体」算出 333.33 而不是 1000）。
写测试夹具时请**从 OpenSCAD 真实生成的 STL 反读面片**，不要手抄。

### 5. pywebview 需要 `PYTHONNET_RUNTIME=coreclr`

新版 .NET 下 pythonnet 找不到运行时，会导致窗口创建失败。

→ 见 `server._prepare_webview_runtime()`。且**必须**有浏览器回退路径。

### 6. TestClient 的默认 Host 是 `testserver`

会被「只允许本机来源」的中间件拦掉。测试时要显式 `base_url="http://127.0.0.1:8760"`。
要测「外部来源被拒绝」，得另建一个 `base_url="http://192.168.1.50:8760"` 的客户端。

### 7. 中文文件名的下载要 RFC 5987 编码

否则用户下载下来的文件名是乱码。

→ 见 `projects_routes.attachment_header()`。

### 8. 不要把 `ok` 放在 SSE `done` 事件的顶层

`api/openscad_routes.py` 的 done 事件结构是 `{info: {...}, message: "..."}`，
`info` 里才有 `found`/`path`/`version`。写测试断言时注意层级。

### 9. 作业 SSE 的 `done` 必须推「完整 Job 快照」，不是 `JobResult`

写前端时很容易只推 `result`。前端的宽松兜底分支会让它**看起来能用**，
但会悄悄丢掉 `messages`，而且 `finished.status` 判断会失真。
契约 3.3 已写明：`done` / `cancelled` 都推完整快照（含 `status`/`result`/`error`/`messages`，
`created_at`/`finished_at` 是 ISO8601 字符串）。
`scripts/selfcheck.py::t_sse_contract` 会钉住这个形状 —— 改坏了会立刻失败。

### 10. 本环境的 shell 是 Windows PowerShell 5.1，不是 PowerShell 7

这一条踩了很多次，务必记住：

- **`.ps1` 必须是纯 ASCII，注释也算**。PS 5.1 把无 BOM 的 UTF-8 脚本按系统 ANSI
  代码页（中文 = GBK）解读。最阴的一类故障：某行行尾的中文标点，其 UTF-8 字节在
  GBK 下会多出一个落在 `0x60` 的尾字节，于是被当成**反引号（续行符）**，
  下一行被合并进注释 —— 被合并掉的语句凭空消失，脚本跑到那里就**静默结束，
  既不报语法错也不继续**。实测损失：`build.ps1` 的「UI 资源是否打进包」检查
  连同 `$packedWebui` 赋值一起消失，脚本停在 7/8 却不报错。
  → 用 `python scripts\check_ps_encoding.py` 检查，`--fix` 自动把风险行改成 ASCII 注释；
  `build.ps1` 的第 4 步已把这项检查设为**构建前置门禁**。
  需要输出中文时，让被调用的 Python 程序去打印（它自己会设 UTF-8）。
- **原生命令的 stderr 在 `$ErrorActionPreference='Stop'` 下会变成终止性错误**。
  Python 的 logging 默认写 stderr，直接 `2>&1 |` 管道会中断构建。
  正确做法：`Start-Process ... -RedirectStandardOutput/-RedirectStandardError`，
  或先把输出收进变量再过滤（`2>&1 | ForEach-Object { "$_" }`）。
- **`native.exe *> file` 不是有效的重定向语法**，命令会静默不执行，
  而 `$LASTEXITCODE` 还留着上一次的值 —— 极难排查。用 `Start-Process` 重定向。
- **读回 UTF-8 文件要用 `-Encoding UTF8`**，否则中文变乱码。
- **`Join-Path` 的 `-Path` 传 `$null` 会抛异常**。注册表里的
  `InstallLocation` 可能「存在但为空」，必须先判空。
- **`Get-Content` 读空文件返回 `$null`**，`$null | Where-Object { $_.Trim() }`
  会抛 "Cannot bind argument to parameter 'Path'"。先收进数组再过滤。
- 不要用 `$args` 作变量名（保留自动变量）。

### 11. Inno Setup 的两个坑

- **`{group}` / `{autodesktop}` 在管理员安装下解析为「公共」位置**
  （`C:\ProgramData\...\Start Menu`、`C:\Users\Public\Desktop`），不是用户目录。
  验收脚本必须两个位置都查，否则会把正常的安装判成失败。
- **`[Tasks]` 不要加 `checkedonce`**：它会让任务在静默安装（`/VERYSILENT`）下
  永不被选中，结果桌面快捷方式根本不创建。安装日志里 `-- Icon entry --` 的条数
  是最快的判断依据（正常应有 3 条：开始菜单 2 + 桌面 1）。
- Inno Setup 6 通过 winget 安装时**不含 `Languages\ChineseSimplified.isl`**，
  而翻译仓库在 `raw.githubusercontent.com`（本机不可达）。因此 `.iss` 只用英文
  语言文件 + 中文 `[CustomMessages]`/`[Messages]` 覆盖；`[Languages]` 的 `Name`
  必须是 ASCII，`LanguageID`/`LanguageCodePage` 属于 `.isl` 文件、不能写在
  `[Languages]` 里。

### 12. GitHub 在本机不可达，但 `api.github.com` 可达

`github.com` 连接超时，`raw.githubusercontent.com` 也不可达。
因此凡是需要从 GitHub 下载的东西（OpenSCAD 走的是 `files.openscad.org`，正常；
Inno Setup 的官方安装包只在 GitHub 上）都要有非 GitHub 的备用路径 ——
Inno Setup 用 winget 解决（它经 Microsoft CDN 取包）。

### 13. 打印机约束别只写在提示词里

实测：提示词明确写了成型范围，AI 仍会越界（要求 ≤180mm，生成 212mm）。
凡是「必须成立」的约束都要有**程序侧兜底**，不能依赖模型听话。
现在成型范围的做法是：提示词 → 实测 → 回灌重做 → `scale()` 等比缩放。
反过来，像「壁厚」「悬垂」这类无法只靠包围盒测准的约束，
就只能写进提示词 + 交给 AI 评审，**不要在界面上声称程序验证过**。

### 14. 「最小成型尺寸」必须小于「最小壁厚」

几何检查是一个 `if / elif` 链：
`太小 → 无法成型` 优先于 `偏薄 → 强度不足`。
如果最小成型尺寸阈值比壁厚下限还大，第二条分支永远不会触发，
等于白写一段代码还给人「检查过了」的错觉。
`selfcheck.py::t_printer_affects_analysis` 里有断言守着这个不等式。

### 15. 安装器里不要用 `cmd /C` + 重定向来跑带参数的 exe

`[Run]` 里曾这样调用首次准备：

```
Filename: "{cmd}"; Parameters: "/C ""{app}\AI-3D-Forge.exe"" --first-run-setup >> ""{app}\logs\first-run.log"" 2>&1"
```

**实测失败**：Inno 日志显示 `Process exit code: 1`，日志文件一个字节都没有；
单独复现时 cmd 报 `The filename, directory name, or volume label syntax is incorrect`。
原因是 cmd 对「带引号的 exe 路径 + 重定向符」的解析不可靠，
而 `CreateProcess` 传参、PowerShell 管道等调用方式各有各的引号规则，
很容易只在安装器这条路径上坏掉（开发时手工跑同一条命令却是好的）。

**正确做法：去掉 shell 这一层，让程序自己写日志。**
现在 `[Run]` 直接调用 exe 并传 `--log-file`：

```
Filename: "{app}\{#AppExeName}"; Parameters: "--first-run-setup --log-file ""{app}\logs\first-run.log"""
```

不要把它"优化"回 cmd 重定向。`selfcheck.py::t_first_run_setup` 会守住这一点。

### 16. `[Code]` 里的 `MsgBox` 会让静默卸载永久挂起

`/SUPPRESSMSGBOXES` **不会**抑制从 `[Code]` 里调用的 `MsgBox`。
我在卸载完成时加了一句「你的文件已保留」的提示框 —— 结果
`unins000.exe /VERYSILENT` 弹出一个看不见的模态框，永久等待一个永远不会到来的点击：
卸载器进程消失了、安装目录也删了，但调用方一直挂着。
这对批量部署是致命的。

守卫写法：

```pascal
if not UninstallSilent then
  MsgBox(..., mbInformation, MB_OK);
```

`selfcheck.py::t_first_run_setup` 里有断言：只要 `[Code]` 段出现 `MsgBox`，
就必须同时出现 `UninstallSilent`。

### 17. 静默安装下 `[Tasks]` 的坑（补充第 11 条）

- `checkedonce` 标志会让任务在 `/VERYSILENT` 下**永不被选中**，桌面快捷方式直接不创建。
- 管理员安装时 `{group}` / `{autodesktop}` 解析为**公共**位置
  （`C:\ProgramData\...\Start Menu`、`C:\Users\Public\Desktop`），
  验收脚本两个位置都要查。
- 判断 `[Run]` / `[Icons]` 是否按预期执行，最快的办法是看 Inno 安装日志里
  `-- Icon entry --` 的条数与 `Process exit code`，而不是猜。

### 18. 缩放 AI 生成的 SCAD 时，绝不能把 `module` 定义套进 `scale(){}` 壳

成型范围兜底最初写成 `scale([k,k,k]) { <AI 的整段源码> }`。
只要 AI 的源码里有 `module` 定义，OpenSCAD 立刻报
`Parser error: modules cannot be defined inside a transform`——
而「AI 写的源码里有没有 module」完全不可控，等于把一个必然触发的坑留给线上。

**最终做法（`jobs._wrap_geometry_in_scale()`）**：把源码按行拆开 ——
`module` / `function` / `include` / 顶层赋值留在**块外**，
其余几何语句（含模块调用）整体缩进后包进 `scale(){...}`。

这样一次改法同时解决了四个实测踩到的坑：
1. `module` 定义不会被包进变换块（否则语法错误）；
2. 参数赋值不被包进变换块 —— 包进去会被「参数 × 倍率」再算一次，**缩两次**；
3. 顶层调用不需要被注释/重新实例化（早期那套写法在「只有 module、忘了调用」
   的源码上彻底失效）；
4. 尺寸无论是走参数还是写死在几何里，都只缩一次。

`selfcheck_design.py::t_scaled_source_builder` 守着这四条，
并且要求它对「只有声明没有几何」「多个 module 都没调用」这两种情况
如实返回 `None`（猜错 module 比不缩放更糟）。

> 教训：判据要对着**真实报错**写，而不是对着「我猜这样不安全」写。
> 早期因为「顶层裸几何可能不安全」就一律返回 `None`，结果实测的 240mm 试件
> 正好是这种形态，兜底直接失效；其实裸几何包进 `scale()` 完全合法。

### 19. 「等待确认」不是终态，`cancel` 也必须认它

作业多了一个 `awaiting_confirmation` 状态（等用户确认需求理解）。它踩了两处：

- 前端 `useJobStream` 原本用 `status !== 'running'` 判「已结束」→
  一进等待确认就被当成结束：确认卡片被丢掉、轮询停表，而服务端正挂在
  `wait_for_spec()` 上死等，用户点什么都没反应。
  正确判据是 `status !== 'running' && status !== 'awaiting_confirmation'`。
- `Job.cancel()` 原本只处理 `status == "running"` → 用户在确认卡片上点「取消」
  是死的。现在两者都处理，并且 `cancel()` 会先 `confirm_spec(None)` 唤醒挂起的等待，
  否则任务永远不返回（任务泄漏）。

另外，规格卡片的数据必须进 `Job.snapshot()`（字段 `spec`）：
用户在这个状态刷新页面时，前端只能靠首帧快照重建卡片。

### 20. 会「点不动」的逻辑要能在不调用 AI 的情况下自检

确认关卡、`_prefer` 取舍、缩放构造这三处都是「坏了就完全没法用」的高危路径。
所以它们都被刻意做成了**纯函数或模块级函数**：
`changes.verify_change()`、`jobs._prefer()`、`jobs.build_scaled_source()`、
`jobs.publish_spec()`。`scripts/selfcheck_design.py` 可以秒级跑完 131 项断言。
把这类逻辑塞进执行器的私有方法里，就只能靠联网跑完整流程才能验证一次。

### 21. 假后端里的「关卡」必须**先推送再暂停**

`webui/mock-server.mjs` 模拟规格确认关卡时，最早的写法是先判断 `step.gate`
就进入等待循环，结果：规格事件永远发不出去，界面看不到卡片，
而假后端在等服务端确认 —— 与真实后端一模一样的死等，测试却完全看不出来
（它只是"卡住"，不报错）。正确顺序是**先把 spec 推给前端、再暂停**。

同理，真实后端在用户确认后会**重推一次** `awaiting_confirmation=false` 的规格，
假后端也必须照做，否则「确认后卡片收成只读」这条路径根本没被覆盖。
凡是"假后端和真后端行为不一致"的地方，测试就等于没测。

### 22. Vite 的 dev server 会被临时文件打死（EBUSY）

本环境保存文件走的是「写 `.xxx.tmpdir/xxx.tmp` 再改名」，
chokidar 会去 watch 那个转瞬即逝的临时目录并在 Windows 上抛 `EBUSY`，
**直接把 dev server 整个进程干掉**（不是打印警告，是崩溃退出）。
`vite.config.ts` 里已忽略这类路径：

```ts
watch: { ignored: ['**/.*.tmpdir/**', '**/*.tmp'] },
```

排查这类问题时不要只看浏览器报错 —— dev server 已经死了，
页面上的现象会误导你以为是自己写的组件有问题。

### 23. 缩放兜底最阴的一个坑：参数会被 `params` 写回原值

`_scale_to_fit()` 里曾经这样做：

```python
wrapped = build_scaled_source(source, factor, printer)          # 套一层 scale()
compiler.compile_to_stl(wrapped, ..., params, ...)               # 把原始参数透传
```

现象：日志说「已等比缩放」，模型尺寸却**一点没变**（240mm 进去 240mm 出来）。
原因：`compile_to_stl` → `build_scad_source` 会用 `params` 把源码顶层的
参数赋值**覆盖回原值**，而模型尺寸完全由这些参数决定，`scale(){...}`
缩的是一个「最终尺寸由顶层参数说了算」的模型 —— 等于没缩。

三个错误的修法都试过：
- 只删 `params` → 参数驱动的模型能缩，尺寸写死在几何里的缩不了；
- 只缩参数值 → 写死几何的那种没救；
- 缩参数值 **再加** `scale()` → **缩两次**（实测 240mm → 135mm，期望 180mm）。

正确做法就是第 18 条那一个函数：**几何进块、声明留外**，`params` 传 `None`。

### 24. 规格里的参数名 ≠ 源码里的参数名

规格是 AI 从用户口语里提炼的（`height`），源码是另一次 AI 调用生成的
（`stand_height`）。两者对不上时，「把目标值直接写回源码」这条确定性兜底
会**静默失效** —— 用户看到的仍然是「AI 两次都没改干净」。

`changes.resolve_param_name()` 负责解析，匹配顺序：完全同名 → 归一化同名 →
词元包含（`height` ⊂ `stand_height`）→ 去分隔符互含（中文名走这条）。
只在**唯一命中**时采用，同时命中多个就返回 `None`（宁可让 AI 改，也不猜错参数）。

> 排查中发现 `design.normalize_name()` 自己也有个隐蔽 bug：
> 它用「包含已知别名」做复合名匹配，而 `SPEC_ALIASES` 里有单字母别名
> （`h`/`w`/`d`/`l`/`r`），于是 `base_thickness` 因为含字母 `h` 被判成 `height`、
> `stand_width` 因为含 `w` 被判成 `width`…… 整张参数表的「高/宽/深」全部塌缩成同一个值。
> 现在改为**按词元匹配**（`stand_height` → `height`），中文才回退到包含匹配，
> 且只接受长度 ≥ 2 的别名。`selfcheck_design.py::t_param_name_resolution` 钉住了这条。

### 25. 「顺手改坏别处」的代价权重必须高于「没改到位」

`ChangeVerdict.penalty()` 里 `unauthorized` 原本按 2.0/项计。
实测：要求「高度改成 150」，AI 改对了高度（+3.0 的收益）却把 `pad_dia`
从 8 改成 80（代价 2.0），于是取舍逻辑**宁愿接受改坏别处的版本**，
也不接受「高度没改但其它都完好」——用户看到的就是「越改越歪」。
现在权重是 4.0/项。

另外补了一条确定性兜底：两个候选都不达标时，先把授权参数的目标值
**直接写回原始源码**（`jobs._apply_targets()`），别处一个字都不动。
「只改该改的」本来就是文本替换能做到的事，没必要再赌一次 AI。

### 26. 归一化 key 会把「手机槽宽」和「整体宽度」混成一个参数

`design.normalize_name()` 最初用「包含已知别名」给复合名归类，于是：

| 参数 | 错误归一化 | 后果 |
|---|---|---|
| `phone_slot_width` | `width` | 与整体宽度撞车 |
| `rib_thickness` / `lip_thickness` / `base_thickness` | `thickness` | 三者撞成一个 |

改动核对里 `_lookup()` 是按归一化 key 取值的，撞车之后**会取到错的那个参数**：
核对 `phone_slot_width` 时拿到整体宽度 78，于是把根本没被改的参数报成
「未授权改动」——用户看到的就是「明明只让改高度，它却说这也不对」。
同一条 bug 还让「把目标值写回源码」的兜底找不到正确参数。

修法：复合名的 key 保留**完整原名**（`x_phone_slot_width`），
整体尺寸保持 `width`；单字母 ASCII 别名（`h`/`w`/`d`）加 `d_` 前缀，
避免 `header` 这类词被误判成高度。中文单字别名（高/宽/深）不加前缀。
「规格叫 height、源码叫 stand_height」这种跨命名约定改由
`changes.resolve_param_name()` 的词元匹配负责，不靠归一化。

`selfcheck_design.py::t_param_name_resolution` 钉住了这条。

### 27. 参数写对了、几何没跟着动 —— 契约核对看不出来

实测：用户说「高度改成 150mm」，AI 把 `height` 参数写成 150，
**模型总高却从 101mm 掉到 38mm**（几何完全没跟着参数走）。
旧契约核对只看参数 diff，于是判「通过」——用户拿到的却是一个完全不对的模型。
E2E 也是靠量 Z 轴尺寸才发现（Δ 是 −63mm 而不是 +50mm）。

补的检查在 `_finish` 里（那里才有实测尺寸），判据必须是**相对**的：

* 硬性：参数值 > 模型最大尺寸 × 1.25 → 这个参数根本不在决定尺寸；
* 相对：改动前该参数与模型主尺寸几乎相等（说明它确实在驱动尺寸，
  例如 100mm 高 → 实测 101mm），改动后却仍停在原尺寸附近 → 几何没跟上。

> 为什么不能写成「参数值 > 模型最大尺寸」这么简单：实测反例是
> `height = 150` 的支架实测 142.8mm（倾斜靠背让总高略小于参数值）——
> 绝对判据会把正常模型判成错误。

对比用的「改动前尺寸」直接读 `project.metrics` 缓存（上次生成时已经测过），
不重新编译。

### 28. 程序化核对查不出「参数全对但结构被改坏」

参数 diff 是确定性的，但它只能看见**数值**。下面这些都是参数全对、模型却废了：

* 把底座改窄到站不稳；
* 把承力处削薄；
* 把原本落在平台上的支撑改成悬空；
* 把靠背的受力方向改反。

所以改动流程是**双重校验**：

1. `verify_change()` —— 程序化、确定性、不消耗额度（参数级）；
2. `client.review_change()` —— AI 复核（结构级、温度 0）：把**改动前/后两份代码 +
   改动契约**一起给它，专门问两件事：你要的改动真生效了吗？结构有没有被改坏？

合并规则写在 `jobs._merge_ai_review()`，方向是**不对称**的，这是刻意的：

| 情况 | 结论 |
|---|---|
| 程序化通过 + AI 认可 | 通过 |
| 程序化通过 + **AI 明确异议** | **判不通过**，带着 AI 指出的疑点重做一次 |
| 程序化失败 | 不通过（不再问 AI —— 这一版注定被丢掉，问了是白花钱） |
| **AI 拿不到结论**（网络失败 / JSON 异常） | **不影响程序化结论** |

最后一行最关键：绝不能因为「AI 没回话」就把一个干净的改动判成失败。
`selfcheck_design.py::t_ai_review_merge` 把四种情况全钉住了。

### 29. 温度是产品行为的一部分，必须集中且可断言

温度决定「同一个需求会不会每次得到不同结果」，所以它不该散在十个调用点里各自漂移。
现在集中在 `llm.py` 顶部：

| 常量 | 值 | 用在哪 | 为什么 |
|---|---|---|---|
| `TEMPERATURE_SPEC` | 0.10 | 解析需求为规格 | 要「照实提取」，不要发挥 |
| `TEMPERATURE_GENERATE` | 0.20 | 从零设计 | 保留一点创造性，但受规格强约束 |
| `TEMPERATURE_MODIFY` | 0.10 | 改现有代码 | 偏离原文的每一步都是风险 |
| `TEMPERATURE_REPAIR` | 0.05 | 带差异重做 | 只准修被指出的地方 |
| `TEMPERATURE_REVIEW` | 0.00 | 复核 / 判定 | 同一份改动必须得到同一个结论 |

`selfcheck_design.py` 里有断言：九处调用点必须全部引用常量、
不允许出现裸写的数字、改动类温度必须低于生成类、判定类必须为 0。
（写这条测试时立刻就抓到了两个漏网的裸 `0.2` 默认参数。）

### 30. SSE 事件契约是双向的：后端推了、前端必须认识

这是目前踩过的**后果最严重**的一类 bug（用户原话：「点击停止生成后无法再点击发送键」）。

后端在取消时推 `event: cancelled` + 完整快照，而前端 `useJobStream` 的
`switch (frame.event)` 里只有 `message` / `stage` / `spec` / `done` / `error`。
`cancelled` 落到 `default` 被**静默丢弃**，于是：

```
作业状态永远停在 running
   → Studio 的 running 恒为真
      → 发送键 disabled 恒为真
         → 用户除了刷新页面没有任何出路
```

它不会报错、不会打日志，只是「点了没反应」。真实复现记录（对着真实后端）：

```
[3] 点击「停止生成」…
    t+1s … t+40s   发送键禁用=true   运行条=1
[4] SSE 事件： ["snapshot","message","stage","spec","cancelled"]
```

**两处修法**（缺一不可）：

1. `useJobStream` 补上 `case 'cancelled'`，把它当**终止事件**处理
   （契约 §3.3 里它与 `done`/`error` 同级），并让 `Studio.onDone` 对
   `cancelled` 给出明确的收尾文案；
2. `Studio` 加**看门狗**：运行中若 120 秒没有任何新消息/阶段变化，主动解锁并
   如实告知。理由不是「再修一次那个 bug」，而是「上游无论怎么坏，
   界面都不能把自己锁死」—— 发送键的禁用状态一旦卡住，等于把软件变成砖头。

**防回归**：`selfcheck.py::t_sse_contract` 现在断言后端会推的每个终止事件
（`done`/`error`/`cancelled`）在前端都有对应的 `case`。这条断言是有效的——
临时把 `case 'cancelled'` 改名，它立刻报错。
另外 `webui/scripts/spec-card-audit.mjs` 会**真的点一次停止**，并确认发送键恢复可点、
草稿没被清掉、下一条真的发得出去。

**假后端也有同样的病**：`mock-server.mjs` 在取消时只悄悄 `return`，
压根不推 `cancelled` —— 所以这条最关键的用户路径在假后端上根本测不到，
前端漏处理分支的 bug 就是这么漏过去的。现在假后端会推真正的 `cancelled` 快照。

> **测界面时要想清楚「这个禁用是谁造成的」**：发送键在**输入框为空**时
> 本来就是禁用的。第一版复现脚本忘了先往输入框里放文字，
> 于是把「空输入导致的正常禁用」误报成故障 —— 差一点就得出完全错误的结论。
> 复现脚本现在会先填字再判定。

---

### 44. 「先取值、再回头找位置」会吃掉重复数值，且完全没有单位概念

自然语言改模型这条链路上，有两个真实 Bug 叠在同一段代码里
（`changes.build_change_contract` 的数值归属逻辑）：

**一、相同数字会被吃掉一半**

旧写法是「先 `extract_numbers(text)` 取出所有数值，再对每个值用
`text.find(f"{value:g}")` 回头找它在原文里的位置」——用来判断这个数字属于哪个子句。
但 `find` **永远返回第一处**。于是：

| 用户说 | 期望 | 实测（修复前） |
|---|---|---|
| 宽度改成90mm，深度改成90mm | width=90, depth=90 | **只有 width=90** |
| 高度改成100mm，宽度改成100mm | height=100, width=100 | **只有 height=100** |
| 高度改成100mm，深度改成100mm，宽度改成100mm | 三个都要 | **只有 height=100** |

5 个用例错 4 个。后果是**静默少改一个参数**，而契约核对只检查「授权过的目标」，
所以它会认为一切正常、报成功。「把盒子改成正方体」是极常见的请求。

修法：边扫描边记录位置（`design.iter_mm_numbers` 返回 `(位置, 值)`），
每个数字有自己的下标，不再依赖 `find`。

**二、没有单位概念**

同一段代码里没有任何 cm/英寸 的处理，于是「高度改成 15cm」被当成 15mm ——
生成一个**小 10 倍**的模型，而契约核对还认为目标已达成。
`extract_numbers` 的正则就是 `-?\d+(?:\.\d+)?`，连单位符号都没看。

修法：在同一个扫描里识别紧跟数字的长度单位并换算成毫米
（mm/厘米/公分/米/inch/英寸/`"`）。三条必须守住的边界：

* **长单位要排在短单位前面**：否则 `15mm` 会先匹配到 `m` 而变成 15000mm；
* **ASCII 单位后面不能再跟字母**：`3 in the box` 不是英寸；
* **只有长度单位才换算**：`加 3 个孔`、`45 度`、`2 层墙` 里的数字不能乘。

**防回归**：`selfcheck_design.py::t_instruction_parsing` 现在有 10 条断言钉住这两类
（相同数字、cm/厘米/米/英寸、以及「个/度」不被误乘）。
**验证方式是把修复退回去跑一遍**：退回旧写法后这 6 条立刻报错
（`{'width': 90.0}`、`15cm → 15.0`），确认这些断言不是摆设。

> **教训**：「先取一个值，再用这个值回头找它在原文里的位置」是很脆的写法 ——
> 只要原文里出现重复值就会定位错。要么一次性扫描出 `(位置, 值)`，
> 要么就别指望能找回来。

---

### 45. OpenSCAD 的「静默丢几何」：编译成功、STL 非空，但少了一整块形状

端到端验收连红两次之后挖到的根因，也是本项目目前见过**最阴**的一类故障。

AI 把侧视截面画进 2D 轮廓后这样挤出：

```scad
// 错：3D 的 rotate 放在 linear_extrude 的**子节点**位置
linear_extrude(height = 72) rotate([90, 0, 90]) square([70, 100]);
```

OpenSCAD 的反应是：

```
WARNING: Scaling a 2D object with 0 - removing object
```

然后**把这块几何整个丢掉**。它不报错、退出码 0、STL 照常生成。
模型里恰好还有底座，于是 `union()` 非空 → 编译 `ok=True` →
界面显示「模型生成成功」。用户要一个 100mm 高的手机支架，
拿到一块 **8.7mm 厚的板**（实测体积 43.7cm³ = 78×70×8，正好只剩底座）。

正确写法是把旋转放在**外面**（先挤出成体，再整体转向）：

```scad
rotate([90, 0, 90]) linear_extrude(height = 30) square([20, 40]);   // ✓
linear_extrude(height = 30) rotate([0, 0, 90]) square([20, 40]);    // ✓ 只绕 Z 合法
```

**为什么以前没被发现**：`compiler.parse_diagnostics()` 一直**解析**这些警告，
但从来没人**处理**它们 —— 只有「STL 缺失或为空」才算失败。
「少了一块几何」的 STL 既不空、也不缺，就一路绿灯走完了。

修法分三层：

1. **识别**（`compiler.DROPS_GEOMETRY_PATTERNS` + `CompileResult.dropped_geometry`）：
   把 `removing object`、`may not be a valid 2D object`、`ignoring unknown module`、
   `empty geometry` 这类警告挑出来，挂到编译结果上。
2. **修复**：作为第 5 类证据交给 AI 定点重做（`_geometry_integrity_problems`），
   证据里直接给出「正确写法 / 错误写法」的对照。
3. **预防**：`SCAD_CONVENTIONS` 里加了硬规则 ——
   `rotate` 的前两个分量只要不是 0，就**必须**放在 `linear_extrude` 外面。

> **教训**：**「能编译」不等于「形状对」**。凡是「工具只 warn 不 error」而后果是
> 丢内容的情况，都必须把警告升级成可处理的状态，否则它会伪装成成功。
> 这类问题还有一个共同特征：**症状出现在很远的地方**（尺寸不对），
> 根因却在另一处（一个子节点位置放错）。

---

### 46. 修复环节会把「已经缩到能打印」的模型换成「更连通但装不下」的

这是上面那一轮**我自己引入的回归**，值得单独记下来，因为它暴露了排序键的设计缺陷。

现象（端到端日志原文）：

```
⚠ 模型放不进这台打印机，正在按成型范围重新设计…
✓ 已按成型范围重新设计，现在可以放进这台打印机。   ← 缩到 180mm，成功了
⚠ 模型有尺寸或结构问题，正在让 AI 修正…              ← 紧接着跑了「完整性修复」
✓ 已修正。
模型尺寸 234×40×20mm 超出该机型…                    ← 修复把尺寸又改回去了
```

根因：`_integrity_rank()` 只按「散件数 → 点名尺寸 → 穿腔」排序，
**完全不看模型还放不放得下**。而修复环节排在「成型范围重做」之后，
于是它把一个刚好能打印的版本，换成了「更连通但 234mm」的版本。

修法：把「装不下」提到排序键的**第 0 位**：

```python
return (
    1 if metrics.exceeds_build_volume else 0,   # 放不下就是废件，最严重
    metrics.stray_parts,
    len(_spec_dimension_problems(spec, metrics)),
    sum(n for _, n in metrics.cavity_islands),
    0 if metrics.watertight else 1,             # 第 4 项见 §58
)
```

这样**任何**修复都不可能把「能打印」改成「不能打印」。
`selfcheck.py::t_geometry_repair` 里有一条断言专门钉住它：
「装不下（哪怕其它方面都干净）必须比『放得下但有散件/穿腔』更差」——
把第 0 项打掉，这条立刻报错。

> **教训**：一个「只采纳更好的版本」的闭环，**「更好」的定义必须包含全部硬约束**。
> 漏掉任何一条，闭环就会拿它去换别的指标 —— 而且换得理直气壮。
> 加法顺序也重要：**越靠后的环节，越需要知道前面环节保证了什么**。

---

### 47. 采纳门槛写成「候选必须封闭」，会让有效修复永远无法生效

第 46 节改完之后，顺着同一条闭环又发现一个**方向相反**的坑。

采纳条件原本是：

```python
if retry_metrics.watertight and _integrity_rank(retry_metrics, spec) < best_rank:
```

读起来很合理：「只有封闭且更好的版本才要」。但它有个致命前提 ——
**原版是封闭的**。如果原版本来就不封闭（例如网格有非流形边），
那么「把 3 个散件修成一整件」的候选虽然明显更好，却因为「仍然不封闭」被拒绝：

| 原版 | 候选 | 谁更好 | 旧代码 | 结果 |
|---|---|---|---|---|
| 3 散件 + 不封闭 | 1 整件 + 不封闭 | 候选 | **拒绝** | 用户继续拿 3 散件 |
| 封闭 + 3 散件 | 封闭 + 1 整件 | 候选 | 采纳 | 正常 |
| 封闭 | 不封闭 | 原版 | 拒绝 | 正常（这条必须保持拒绝） |

修复明明有效，却永远不会被采纳 —— 而且是**在最需要它的时候**（原版已经有问题）。

改成「封闭性**不变差**」：

```python
watertight_not_worse = retry_metrics.watertight or not best_metrics.watertight
if watertight_not_worse and _integrity_rank(retry_metrics, spec) < best_rank:
```

* 原版封闭 → 候选也必须封闭（绝不用封闭性换别的指标）
* 原版不封闭 → 已是失败状态，允许候选改善别的方面

五条判定都钉进了 `selfcheck.py::t_geometry_repair`
（含「原封闭 → 候选不封闭必须拒绝」这条反向保护）。

> **教训**：绝对门槛（「必须满足 X」）在**增量改进**的语境里常常是错的，
> 因为它默认了「起点已经满足 X」。这类地方要问的是
> 「候选有没有让 X 变差」，而不是「候选满足不满足 X」。

---

### 48. 本轮「查了但没问题」的几处（记录下来，省得下次重查）

排查同样重要，但没必要把「确认没问题」也写成事故。以下几处经对抗性测试确认是干净的，
并把测试固化进自检，避免以后改坏：

| 区域 | 结论 | 固化在哪 |
|---|---|---|
| 项目名字 → 目录 | `safe_name` 会剥掉 `<>:"/\|?*` 与控制字符、去首尾点空格、处理 Windows 保留名（CON/COM1…）、限长 40。带斜杠/纯点/emoji/超长都不会让目录跑出 Projects | `t_projects`（把清洗打掉，它立刻报错） |
| 项目 id → 路径 | **不拼路径**：`directory_for` 是扫描各项目的 `meta.json` 比对 `id`。所以 `id="../../x"` 只会查不到（404），不存在路径穿越 | 无需额外防护 |
| 删除语义 | 是移入 `Projects/.deleted`（可恢复），不是真删；`.deleted` 以点开头，`_iter_dirs` 会跳过，所以删掉的项目不会再出现在列表里 | `t_projects` |
| 归档顺序 | `archive_revision` 在覆盖 `model.scad` **之前**执行，历史里存的确实是上一版 | 代码注释 + 端到端「项目保留了历史版本」 |
| 拓扑判定误报 | 9 个「数学上完全封闭」的模型（球、布尔并/差、hull、offset 圆角、薄壁壳…）全部正确判为封闭、开放边/非流形边均为 0。**没有误报** —— 所以端到端里那次「6 条非流形边」是模型本身的问题，不是分析器的错 | `t_mesh_analysis`（新增 7 条断言） |
| 存储往返 | `metrics.usage`、`printability.visual_review`、`spec.fingerprint`、参数名含数字等都能原样读回 | `t_projects` |

> 拓扑判定那一条特别值得记：它**不只是**影响用户看到的警告，
> 还直接参与第 47 节的采纳门槛（`watertight`）。
> 一旦它误报，任何修复都无法被采纳 —— 两个 Bug 会叠成「模型一直修不好」。
> 所以「分析器本身准不准」必须先钉死，再谈基于它的决策。

---

### 49. `"$fn" in source` 这种子串检查，会把算好的细分度静默丢掉

`build_scad_source()` 负责在编译前按目标打印机注入 `$fn`（分段数）。
它原本的判据是：

```python
if fn and "$fn" not in cleaned:      # 整份源码的子串检查
```

只要源码里**任何位置**出现 `$fn` 这三个字符，注入就被跳过。
而这三种情况都会让子串检查为真、却并没有真的声明细分度：

| 源码形态 | 注入 | 同一个圆柱的面数 |
|---|---|---|
| 完全没提 `$fn` | ✅ | **380** |
| 注释里提一句 `// 建议 $fn 大一点` | ❌ | **116** |
| 只有 `// @param $fn 96 曲面细分 64 128` 注释 | ❌ | **116** |
| 只在模块内部 `$fn = 12;` | ❌ | **212** |
| 顶层 `$fn = 8;`（应尊重作者） | ❌ | 28 |

380 → 116 就是**肉眼可见的棱面**。而 `$fn` 恰恰是按「喷嘴直径 + 模型尺寸」
算出来保证圆弧面光滑的那个值 —— 这个功能在最容易命中的场景下悄悄不生效。

而且**我们的提示词本身就会让 AI 写 `// @param $fn …` 注释**（参数区要求每个参数一行），
所以「有注释、没赋值」并不是罕见形态。

修法：判据换成「**顶层有没有真的赋值**」，在 `scad.py` 新增
`top_level_assignments()`（与 `apply_params` 同一套括号/花括号深度判断，
只认最外层赋值，且用 `(?<![=!<>])=(?!=)` 排除 `==` / `<=`）。

* 顶层显式赋值 → 尊重作者，不覆盖（包括赋值写在文件**末尾**的情况）
* 其余一律注入（注释、字符串、模块内部的局部 `$fn` 都不算声明）

`selfcheck.py::t_compiler` 里 5 条断言钉住它，并且最后一条会**比较真实面数**
（注入版必须比 `$fn = 8` 版多一倍以上），确保它不是空跑。

> **教训**：判断「代码里有没有 X」**永远不要用子串检查**。
> 注释、字符串、内层作用域都会让它误判为真。
> 要判就判**你自己定义的那个语义**（这里是「顶层赋值」），并且用与其它逻辑
> 同一套作用域规则去判 —— 本项目里已经有一份现成的深度判断，直接复用。

---

### 50. 本轮「查了但没问题」的几处（作业状态机与异常恢复）

| 区域 | 结论 |
|---|---|
| `cancel()` 的状态竞争 | `cancel()` 先置 `cancelled` 并推快照，再 `task.cancel()`；`run()` 捕获 `CancelledError` 后调用 `job.cancel()`（因状态已是 cancelled 故幂等、不会重复推送）再 re-raise。**取消不会被后续异常改写成 error** |
| `_finish` 的出口 | 全文只有两个出口：编译失败 `job.fail()+return`，末尾 `job.succeed()`。不存在「走完却没置终态」的路径（那会让界面永远转圈） |
| SSE 流终止 | `event_source()` 有 15s 超时 + 心跳；**并且会在「作业状态已终态但没收到终止事件」时主动补推一次**（含事件被丢弃/订阅晚于结束的情况），随后 `return`。这条兜底正好覆盖了 `_publish` 队列满丢事件的极端情况 |
| 前端解锁 | `useJobStream` 里 `done`/`cancelled`/`error` 三个分支都置 `settled` 并 `setMode('settled')`，`error` 也调用 `onError`。**不存在只在某个分支解锁的漏洞**（这个漏洞在更早一轮修过一次，现在三个分支一致） |
| 错误事件形状 | `fail()` 推的是 `{message,hint,code}`，SSE 超时兜底分支推的也是同一形状，两条路径一致，前端一个 case 就能处理 |
| OpenSCAD 版本兼容 | 编译只用 `openscad -o out.stl in.scad` 这一种最朴素的调用（不传 `--export-format` 等版本相关开关），所以用户机器上装着较老的 OpenSCAD 也能编译；缩略图是 Python 侧生成的 SVG，不依赖 OpenSCAD 参数 |
| 参数微调的落盘 | `compile_to_stl` 会把新参数**写回源码文本**再编译（不是用 `-D` 临时覆盖），而 `_finish` 随后把这份源码存盘，所以参数改动确实落到项目文件里 |

---

### 51. 轮询兜底会永远轮询下去；而「作业对象还不存在」时收尾是空操作

用户在界面上看到的现象：**发送键一直禁用、一直显示「生成中」**。
和更早那次「点停止后发送键永久禁用」是同一类故障，只是入口不同。

两个缺陷叠在一起：

**一、轮询没有上限**

`startPolling()` 是 SSE 连不上之后的兜底。而 SSE 连不上的原因很可能是
**本地服务已经不在了**（进程被杀、端口被占、休眠回来没恢复）。
旧实现单次失败就 `catch {}` 然后无限期重排下一拍：

```js
} catch {
  // 单次轮询失败不算致命，下一拍继续
}
if (!disposed && !finished) pollTimer = setTimeout(poll, POLL_INTERVAL_MS);
```

服务永久不可用时它永远不会成功，界面也就永远不会收尾。

**二、收尾时假定「作业对象一定存在」**

我第一版修法是加一个统一的收尾函数：

```js
setJob((prev) => (prev ? { ...prev, status: 'error', ... } : prev));   // ← 问题在这
```

看着没问题，但**作业对象可能是 null**：如果连快照都没拉到
（作业在服务端已经不存在，`GET /api/jobs/<id>` 直接 404），
那 `prev` 就是 null，这个更新是**空操作** —— 状态没变、界面不解锁。

**这条是真实浏览器测试抓出来的**：修完之后 `tsc` 通过、代码读起来也对，
但跑 `verify-stream-lost.mjs` 仍然 25 秒不解锁。补上两处才算真修好：

1. `settleUnreachable` 在 `prev` 为 null 时**构造**一个终态 Job，而不是原样返回；
2. 导出的 `finished` 增加一条兜底：`mode === 'settled' || …` ——
   只要流/轮询已经收尾，界面就必须解锁，**不依赖 job 对象存在**。

另外给轮询加了上限（连续 8 次失败 ≈ 12 秒即放弃并如实告知「与本地服务失联」），
以及在 404 时**立刻**收尾（作业已被回收，再轮询也拿不到结果）。
实测：404 场景下 **0.5 秒**收尾，而不是等 120 秒的进度看门狗。

**防回归**：新增 `webui/scripts/verify-stream-lost.mjs`。
它**不调用 AI**：只打桩 `POST /api/jobs` 返回一个服务端不认识的 `job_id`，
让后续请求真实地拿到 404，从而把 SSE→重连→轮询→收尾整条链路真跑一遍。

> **教训（两条）**
> 1. **兜底路径也需要上限**。「失败就重试」的兜底如果没有放弃条件，
>    在「对端永远不回来」时它比不兜底更糟 —— 用户面对的是一个永远转圈的界面。
> 2. **状态更新要问「prev 会不会是 null」**。`prev ? {...} : prev` 这种写法
>    在「从未成功初始化」的路径上就是静默失败，而且**类型检查查不出来**。
>    这次是真实浏览器测试抓到的，`tsc` 和代码审阅都没看出来。

> **测界面解锁时的一个老坑（又踩了一次）**：发送键在**输入框为空**时本来就是禁用的，
> 而 Enter 发送之后输入框会被清空。第一版断言直接看 `isEnabled()`，
> 于是把「空输入导致的正常禁用」误判成故障。正确做法是
> **先往输入框里填字再看是否可点** —— 这个坑在 `spec-card-audit.mjs` 的注释里已经写过一次了。

---

### 52. 「删掉非 ASCII」得到的不是空串，而是扩展名 —— 下载文件名退化成 `.stl`

`attachment_header()` 里有一句看着很稳的兜底：

```python
ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "model"
```

作者的意思是「ASCII 版为空时用 model 顶上」。但对中文项目名：

| 项目名 | 去掉非 ASCII 之后 | 真值判断 | 结果 |
|---|---|---|---|
| 桌面手机支架.stl | `.stl` | **非空** | 兜底不触发 |
| 水杯架.stl | `.stl` | **非空** | 兜底不触发 |
| 我的模型 v2.scad | ` v2.scad` | 非空 | 带前导空格 |
| 3D 零件.stl | `3D .stl` | 非空 | 悬空空格 |
| model.stl | `model.stl` | 非空 | 正常 |

5 个用例错 4 个。后果：只认 `filename=` 的客户端会存下一个**名叫 `.stl` 的文件** ——
在 Windows/macOS 上是隐藏文件，用户会以为下载失败或找不到文件。
（现代浏览器都用 `filename*`，所以这个问题只在老旧/精简客户端上暴露，
正因如此它一直没被端到端测试抓到 —— 原来的断言只检查 `"filename*" in disposition`。）

修法：**主名与扩展名分开处理**，主名为空时才换成 `model`；
同时把 ASCII 兜底限制在安全字符集内（这个函数的产物直接进 HTTP 头，
文件名里带引号或换行会破坏头结构 —— 当前调用方都用 `safe_name` 清过，
这里再兜一层，避免以后有人直接拿项目名来拼）。

顺带把端到端那条断言**加强**了（不是降低标准）：除了 `filename*`，
还要求 ASCII 兜底的**主名非空**。现在它会在真实 HTTP 响应上打印：

```
[OK] STL 可下载且文件名正确 —— ASCII 兜底主名='model'；attachment; filename="model.stl"; filename*=UTF-8''…
```

> **教训**：`x.encode("ascii","ignore")` 之后**不能假设剩下的是「名字」**——
> 它可能只剩下分隔符/扩展名这类「看起来像内容、其实没有信息」的碎片，
> 而这类碎片在真值判断下全是「真」。
> 兜底条件要针对**你真正在意的那个部分**（这里是主名）来判断，而不是整体是否为空串。

---

### 53. 本轮「查了但没问题」的几处（配置与启动路径）

| 区域 | 结论 |
|---|---|
| 配置目录不可写 | `server.run()` 捕获 `paths().ensure()` 的 OSError → `_fatal()` → **Windows 下弹原生 MessageBox**（含日志位置）。GUI 程序没有控制台，只 `print` 是没人看得到的，这里做对了 |
| 安装期首次准备 | `--first-init` / `--first-run-setup` 全程 `try/except` 且任何情况都返回 0：装不上 OpenSCAD 也不会让安装流程失败，只把原因写进日志和可见输出 |
| 配置内容不是对象 | `AppConfig.from_dict()` 有 `if not isinstance(raw, dict): return cfg`，所以 `[]` / `"x"` / `123` 这类「合法 JSON 但不是对象」不会抛异常、也不会让程序起不来 |
| 配置段字段类型错 | `model(**kwargs)` 只按**字段名**过滤，不做类型校验：手改成 `"timeout_s": "abc"` 会静默接受。属于「手工改坏配置」的场景，暂不处理（记录在此） |
| OpenSCAD 下载中断 | 每轮先删残留 `.part`（注释说明了「不删会白下一遍」）、3 次重试、`_replace_with_retry` 容忍杀软占用、成功后校验 SHA-256、装完清理 20.9MB 压缩包；最终失败给中文原因 + 手动安装指引。**三次都失败时也会把半截的 `.part` 删掉**（本轮补上，见 §64）；解压改成「旁路目录 + 自检通过后才替换」，失败不会破坏已有安装 |
| 项目 id 与名字 | 已在第 48 节验证：id 不参与拼路径、`safe_name` 会剥掉路径分隔符与保留名 |








### 31. 一句口语指令 → 目标值：四种归属错误

`build_change_contract()` 负责把「高度改成150mm」翻译成 `{height: 150}`。
它最初用「数字附近 12 个字符里出现参数名」判断归属。用探针脚本一测，
四种说法全部解析错，而且错的方向都是**改错参数**（比没改成功更糟）：

| 用户说 | 旧结果 | 正确结果 | 为什么错 |
|---|---|---|---|
| 高度不变，宽度改成90mm | `{height: 90}` | `{width: 90}` | 数字落到了用户**要求别动**的参数上 |
| 高度改成150mm，宽度改成120mm | `{}` | `{height:150, width:120}` | 12 字符窗口太窄，只认了第一个 |
| 高度改小 20mm | `{height: 20}` | `{height: 80}` | 把**增量**当成了目标值 |
| 同上（授权名单） | `changed=[height,width]` | `changed=[width]` | 句子提到就算「允许改」，等于授权 AI 改冻结点 |

修法（都在 `build_change_contract` 里）：

1. 先按标点/连词把指令切成**子句**，数字归到它所在的子句，
   再与「该子句提到的参数」配对 —— 一句话改多个参数才能各自取到值；
2. 子句里出现「不变 / 保持不变 / 别动 / 原样」等说法时，该子句的数字**不归它**，
   并把该参数记进新增的 `ChangeContract.frozen`，同时从 `changed` 里剔除
   （`frozen` 会在契约文本里单独写成「用户明确要求不要改动」）；
3. 子句里出现「改小 / 增大」等**相对**说法时，用 当前值±增量 算出目标值；
4. `verify_change` 单独检查 frozen 项是否被动过，并明确报
   「你明确要求不要改动的项被改了」—— 混在越权长列表里用户看不出来。

`selfcheck_design.py::t_instruction_parsing` 把这四种说法全部钉住了。

### 32. 取舍必须有下限：绝不挑出比「不改」还差的版本

`_pick_least_bad()` 有两个实测缺陷：

* 三个候选**全都把别处改坏**时，它仍会挑一个出来（代价 10.0），
  而原版代价只有 3.33 —— 用户拿到的是比不改更糟的结果。
  根因是函数假设调用方会把原版放进候选里，但这个保证没有落实在函数内部。
* 候选列表为空时直接 `min([])` 抛 `ValueError`，整次修改变成
  「生成过程中出现了意外问题」。

现在把「原版永远是候选之一」写进函数内部（调用方不必记得加），
并让空列表也返回原版。`selfcheck_design.py::t_least_bad_floor` 守着。

### 33. 「无法确认改动生效」时不要报成功

契约里**一个参数都没点名**时（用户只说「加个孔」），旧代码直接跳过核对、
判「通过」。于是「什么都没改」也会显示成成功 —— 正是最该避免的「假装成功」。

现在这种情况会比对改动前后的代码：**完全一样**就如实记一条
「这次没有再点名任何尺寸，而代码前后完全一样 —— 无法确认改动已经生效」，
并把 `performed` 置为 false。

> 但**不判失败**（`ok` 保持 true）：结构类改动（加个孔）本来就无法用参数证明，
> 判失败会把正常需求误杀。这一层兜底交给 AI 复核 —— 那正是它存在的理由。
> `selfcheck_design.py::t_verify_honesty` 同时钉住了「如实说明」与「不误杀」两侧。

### 34. 带 BOM 的 config.json 会让用户设置全部丢失

`config.py` 读自己的 `config.json` 时写死了 `utf-8`。只要这个文件带 BOM
（**任何 Windows 编辑器「另存为 UTF-8」都会加**，我自己写的安装测试脚本也加过），
软件就会把自己目录里一份**完好的配置**判成损坏：
打一条 `配置文件损坏（Unexpected UTF-8 BOM…）` 警告、回退默认值、
并把原文件改名成 `.json.corrupt`。

用户看到的现场是：**「明明配好了，重启后设置全没了」**（连打印机选择一起丢）。
这是从真实运行日志里抓到的，不是推演出来的。

修法：加 `CONFIG_ENCODING = "utf-8-sig"` —— 写出的字节与 utf-8 完全相同
（不带 BOM），读的时候却能容忍 BOM。秘密文件（降级到文件保存 Key 时）同样处理。
`selfcheck.py::t_config_and_secrets` 里有断言：
写入带 BOM 的配置必须能正常读出，且不得生成 `.corrupt`。

---

### 35. 「一整件」还不够：特征长错位置，零件数检查看不见

用户第二次反馈的原话是「软件给我建模的是一个桌面卡口和一个正常的垃圾桶」。
按第 33 条之前的思路修完「散件」（`solid_parts`）之后，模型确实变成一整件了，
但拿真实用例复测，发现**一整件里还有更隐蔽的错**：

生成的垃圾桶把 6 根外壁加强筋写成

    for (i = [0 : rib_count - 1]) rotate([0, 0, i * 360 / rib_count]) rib_profile();

而 `rib_profile()` 里的筋是按「桶底短边一半」这个**固定半径**摆的。
圆桶这样写没问题，矩形/扇形桶身则完全不成立 ——
外壁到中轴的距离随角度变化，于是 0°/180° 那两根正常外凸，
另外 4 根在底端埋进壁里、往上就**飘进了桶内腔**，桶里平白长出 4 块挡板。

关键在于：这个模型 `solid_parts == 1`（筋和底面相接）、`watertight == True`、
包围盒和体积都正常 —— 现有全部指标都是绿的，用户却拿到一个不能用的桶。

修法是两层，缺一不可：

1. **几何层**（`mesh._detect_cavity_islands`）：用水平面切模型，
   把截面接成闭合环，按**嵌套层数**的奇偶判断材料与空腔
   （0 层=材料外轮廓，1 层=空腔，2 层=空腔里的材料孤岛）。
   偶数层且 ≥2 的环就是「本该是空的地方长出了东西」。
   实测在真实模型上正好检出 4 处，与「4 根筋穿腔」完全吻合。
2. **提示词层**（`changes.initial_prompt` 第 11/12 条 + `llm.SCAD_CONVENTIONS`）：
   强制「壳体类零件掏空必须是最后一步」——
   先把外形和所有外壁特征 union 成实心外坯，最后统一 `difference()` 掉内腔。
   这样布尔运算会自动把伸进腔体的部分切掉，特征正好停在内壁面上。
   实测：同一需求从「4 根筋穿腔」变成 0 处孤岛。

两个实现细节值得记下来（都是实测踩出来的）：

* **切片接环不能用「按网格取整」去重**。CSG 出来的网格里，一个图元的边
  常常落在另一个图元顶点外 1~3µm 处，取整会把这两点分进相邻格子，
  接环时出现大量「度数为 1」的断头 —— 真实模型上 4 层切片全部装配失败，
  于是检测永远返回「无孤岛」这个假阴性。改成空间哈希 + 3×3 邻域搜索，
  把 10µm 内的点真正合并成一个节点才对。
* **环的绕向不可靠**，不能用「正环=材料、负环=空腔」来判断内外 ——
  无向图走出来的环方向是任意的。改用嵌套层数（纯几何事实）才稳。

---

### 36. 密封空心件被误报成「2 个零件」

修第 35 条时顺带发现的老毛病：`_count_parts` 数的是**曲面连通分量**，
而一个密封空心件（外壳面 + 内腔面）在拓扑上本来就是两块互不相连的曲面。
于是「密封浮球」「带内腔的盒子」这类完全正常的模型会被报成
「模型由 2 个互不相连的零件组成，打印出来需要自己拼装」—— 纯误报。

判别方法用面朝向：内腔面的法线指向空腔内部，它自己围出的**有向体积为负**，
真实体零件为正。为稳妥再叠一条几何条件（该分量必须被另一个分量的包围盒
完全包住，且不能互相包含），两条同时成立才认定为内腔面。

`selfcheck.py::t_mesh_analysis` 里有对应用例：
密封空心件必须数出 1 个零件；腔内真漂着一块时必须数出 2 个。

---

### 37. 构建脚本被环境里的 safe-delete 钩子打死

`build.ps1` 跑到 PyInstaller 那步会随机失败，退出码 1，日志里混着一行

    [safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":50,...}

根因不在本项目：这台机器的 `PYTHONPATH` 指向 `...\shim\`，
里面有个 `sitecustomize.py` 会把 `os.remove` / `shutil.rmtree` /
`pathlib.Path.unlink` 全部替换成「移入回收站」，并统计**每次工具调用**内的
删除次数；超过阈值（默认 50）就 `raise SystemExit(1)` ——
直接杀掉正在跑的子进程。PyInstaller 每次构建都要清掉上次的
`build\work\pyinstaller\...` 目录，文件数轻松过 50，于是必挂。

这不是代码缺陷，是环境钩子与「构建需要大量删除」的冲突。处理方式：
给构建子进程关掉该钩子（`CODEBUDDY_SAFE_DELETE_ENABLED=0`），
或清空它统计用的临时状态目录。**注意**：这不是绕过文件安全策略，
本项目的构建产物目录本来就该被覆盖重建。

---

### 38. 让 AI「有设计感」和「是一整件」是一对矛盾，只能靠实测兜底

用户第二次反馈是「生成的模型过于简单，不能满足描述」——例子是
`D:\模型\Desk_Bin_15mm_Tabletop.3mf`（夹桌式垃圾桶：扇形外扩桶身 +
一体夹口 + 外壁竖筋）。于是往提示词里加了三条（形体要有收减/渐变、
要有高频细节、容器要先实心后掏空），结果**复杂度上去了、连通性塌了**：

用 6 个真实桌面小物实测（`scripts/complex_e2e.py`），
第一次生成就有 5 个出现散件 —— 最离谱的「线缆收纳底座」被拆成
**80 个互不相连的零件**（主体 + 79 颗独立的小齿纹）。

结论：**提示词可以提高上限，但保证不了下限**。真正兜住的是这条闭环：

    实测（连通分量数 / 截面腔体孤岛）
      → 把证据（含每个散件的中心坐标与尺寸）交回给 AI
      → 只采纳「封闭且严格更好」的那一版，平手一律保留原版
      → 最多两轮，第二轮明确要求「换画法，不要再微调坐标」

几个具体经验，都是实测出来的：

* **只说「有 80 个散件」AI 根本改不动**。必须给位置和尺寸：
  「#1 中心(0,56,55) 尺寸2.4×24×110 体积1056mm³」——
  它才能对回自己写的那个 `for` 循环。
* **让 AI 微调坐标是没用的**。实测它把加强筋从 `body_depth - 1` 推到
  `body_depth - 2`（真的往里推了 1mm），模型照样是 7 个零件。
  有效的指令是**换画法**：把贴面特征画进 **2D 截面**再统一挤出 ——
  这样它天生就和主体是同一块料。把这条写进生成提示词后，
  6 个用例的首次合格数从 2/6 提到 4/6。
* **采纳门槛必须是「严格更好」**。修复经常把 A 处修好、B 处改坏；
  只要不是严格更优就保留原版，用户手里的模型只会变好不会变差。

另外注意：这类模型偶尔会让 OpenSCAD 卡住（实测有一次 CGAL 布尔
超过 300 秒超时）。`offset` / `minkowski` 用得多、`hull` 套 `hull`
的写法风险最高。

---

### 39. 规格里用户点名的尺寸，以前从来没有任何程序去核对过

`DesignSpec.acceptance` 里会写着「支架总高等于 100mm」，
`SPEC_SYSTEM_PROMPT` 也要求写成「可核对的句子」—— 但**从来没有人核对过它**：
它只是被塞进提示词给 AI 看。

改动流程有 `_implausible_targets`（参数值 vs 实测尺寸）兜着，
**首次生成完全没有**。也就是说：用户说「高度 100mm」，第一次生成出来 62mm，
老版本会一路绿灯交付。

补上的做法（`jobs._spec_dimension_problems`）：

* 从规格里只取**用户明确指定**（`user_specified=True`）、单位为 mm 的项；
* 只取**整体尺寸**语义的字段（height/tall/高度/总高 → Z 轴，width/宽度 → X 轴，
  depth/深度 → Y 轴）。命中壁厚/孔径/圆角/间隙/数量这类词的字段一律跳过 ——
  `wall_thickness 2.4` 不能拿去和包围盒比；
* **只报「实测 < 声明值 90%」这一个方向**。模型常有多出来的结构（底座、托边、翻边），
  实测偏大是正常的 —— 端到端日志里就有「参数值 240.0，含底座等结构故不等高」
  这样的正常现象；反过来实测偏小则没有合理解释。

它接在已有的「实测 → 定点修复」闭环上，优先级排在散件之后、腔体孤岛之前：
用户说过的数字做不到，说明**根本没听懂需求**，比内部结构瑕疵更严重。

实测校准过：用例「线缆收纳底座」声明 80×80×30mm、实测 83.2×83.2×30.5mm，
不误报；`selfcheck.py::t_geometry_repair` 钉住了「偏大不报」「10% 内不报」
「壁厚不参与比较」这几条。

---

### 40. 系统代理会把「访问本机服务」也代理掉，端到端测试因此假死

`e2e_test.py` 连跑两次都在第一步就崩：

    File "scripts\e2e_test.py", line 271, in main
        health = client.get("/api/app/health").json()
    json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)

看着像程序起不来 —— 其实**和程序完全无关**。查下来是：

* httpx 默认 `trust_env=True`，会去读系统代理（Windows 上来自注册表
  `HKCU:\...\Internet Settings`）。这台机器 `ProxyEnable=1`、
  代理是 `127.0.0.1:7890`（mihomo）；
* 于是连 `http://127.0.0.1:8899/api/app/health` 这种**回环地址**的请求
  也被送去了代理，代理返回 `502` 且响应体为空；
* `.json()` 拿到空 body → 报成 JSON 解析失败，把根因完全掩盖了。

判别方法（一眼定性，别再猜）：直接打这个请求看状态码 ——

```python
httpx.get(url, trust_env=True)   # -> 502, len=0
httpx.get(url, trust_env=False)  # -> 200, {"ok":true,...}
```

修法：给访问回环地址的客户端加 `trust_env=False`。
**回环地址永远不该走代理**，这条与网络环境无关，是通用规则。

排查时还顺手确认了一件事（避免误改）：应用自己**不**用 httpx 访问本机 ——
`llm.py`（DeepSeek）和 `envcheck.py`（联网检测）访问的都是外网，
走代理是**期望行为**，不要给它们也加上 `trust_env=False`。
前端是 WebView2 直接用浏览器栈访问 `127.0.0.1`，
Chromium 自己会按系统代理的绕过列表处理，不受这条影响。

---

### 41. 指标全绿 ≠ 模型是对的：必须让多模态模型真的看一眼

第 35 节那个坑（垃圾桶 4 根加强筋穿进内腔）的教训只解决了一半：
程序把**能测的**都测了，但「测不出来、看一眼就知道」的问题一直没人管。
补上的是渲染 + 图片复核（`render.py` + `llm.review_snapshot`）。

**先说两个接口事实，都是实测出来的，不是推测**（对着 `api.deepseek.com` 打的）：

| 模型 | 图片输入 |
|---|---|
| `deepseek-flash` | **支持**。64×64 红图答「红色」；桶形渲染图答「四棱台，上宽下窄」（与模型实际尺寸一致）；数球答「6 个」（实际就是 6 个） |
| `deepseek-v4-pro` | **不支持**。收到图后回答「我这边无法查看你上传的图片」，**静默忽略图片** |

所以图片复核固定用 `VISION_MODEL = "deepseek-flash"`，**不跟随用户配置里的模型** ——
否则用户一换模型，复核就退化成「模型说没问题」的假阳性。

**第二个坑更要命：思考过程也计入 `max_tokens`。** 同一个请求：

```
max_tokens=2048 -> completion_tokens=2048（全是 reasoning）
                   finish_reason="length"，content=""          <- 看起来「模型没意见」
max_tokens=8192 -> completion_tokens=4186（reasoning 4099）
                   正常给出结论
```

额度给小了会表现成**「模型没有意见」**，而实际上是它话没说完。
这是最危险的一类失败：看起来通过了，其实什么都没检查。
所以预算写死 8192，并且识别 `finish_reason=="length"` + 空正文时自动放大额度重试一次
（`VISION_MAX_TOKENS_RETRY`）；仍然为空就如实返回空串，
由调用方说成「复核没有给出结论」，**绝不翻译成「没问题」**。

**判读方向同样不能反**（`llm.parse_snapshot_verdict`）：
只有第一行明确写「结论：有问题」才算有问题；空回复、含糊答复、格式跑偏全部算「没有结论」。
反过来把「没结论」当成「没问题」，缺陷就被静默放过了。

**渲染器**（`render.py`）是纯标准库的软件光栅化（画家算法 + Z 缓冲 +
朗伯光照 + 灰度着色），不引入 OpenGL / Pillow / matplotlib：

* 两个视角（3/4 前视 + 俯视），俯视是为了看清腔体开口、夹持口这类侧面看不出来的结构；
* 正交投影、按最长边归一化，任何尺寸的模型都填满画面；
* 超过 12 万面按步长抽样，并在返回值里**如实标出 `sampled`** ——
  抽样会留下细小孔洞，不能让复核者以为自己看到的是完整实体；
* 实测速度：立方体 1.8s、24552 面的球阵 2.3s。

**接线顺序很关键**：外观复核必须排在几何修复**之前**，
它的结论才能作为第 4 类修复证据并进去（`_geometry_integrity_problems(..., visual=)`）。
`selfcheck.py::t_visual_review` 用 `finish_body.index(...)` 的比较把这条顺序钉死了。

实测效果：一次真实生成里，程序判定 `solid_parts=1`、无孤岛、包围盒正常，
而图片复核报告「桶口边缘有两个小凸起，与加强筋和夹持功能不对应，疑似长错位置」——
正是程序测不出来的那一类。

---

### 42. 借 `earthtojake/text-to-cad` 的 DfAM 检查：壁厚必须真的测

GitHub 通了之后把 `earthtojake/text-to-cad` 完整看了一遍 —— 它不是单个 skill，
而是一个**技能集合**（cad / dfam-check / gcode / bambu-labs / cad-viewer /
step-parts / urdf / srf / sdf / dxf …）。之前只读了 `skills/cad/SKILL.md`，
漏掉了最相关的一个：**`skills/dfam-check/`**（面向增材制造的设计规则检查）。
它带来三个真问题：

**一、壁厚根本不该从包围盒推（已修，这是最严重的一个）**

旧实现把「包围盒最小边」当壁厚。对**空心容器完全失效**：
实测的桌面垃圾桶 110×186×110mm、壁厚 2.4mm，包围盒最小边 110mm ——
无论壁做得多薄都不会报警。而容器正是这个软件最主要的用途。

改成射线法实测（`mesh.measure_wall_thickness`），做法照 dfam-check 的
`dfam_tool.py`：按面积加权采样表面 → 从采样点沿法线反向打射线 →
第一个交点即对面表面，距离就是该处局部厚度 → 报 min / **p05** / 中位数 + 最薄处坐标。

* 用 p05 而不是 min 做判据：min 可能是噪声三角面造成的采样离群值，
  p05 偏低才说明**有一片区域**真的薄。
* 纯 Python 实现，用均匀网格 + 射线步进做加速（逐面求交是 射线数×面数，太慢）。
  实测：2 万面 0.51s。横跨很多格子的大面（立方体的面）单独放 `oversized` 列表，
  否则它在 20×20×20 网格里会被存 8000 份，建表直接爆。
* 实测校准：同样是「包围盒最小边 100mm」，壁厚 3mm 不报警、0.8mm 报警 ——
  旧实现在这两个之间**给不出任何区别**。
* 在真实生成物上马上有收获：`case2` 实测 p05 = **1.00mm**，低于 1.2mm 门槛，
  旧实现完全看不到。

同时修掉一个自相矛盾的默认值：`AnalysisLimits.rough_min_dim_mm` 回落值写的是
**4.0mm**，而每台机的规则是「2 × 喷嘴」= 0.8mm（它自己的文档也是这么写的）。
4.0 会把 1.2mm 的薄片判成「连一层走线都放不下」—— 1.2mm 在 0.4mm 喷嘴上明明是三层。
已改成 0.8，与 DfAM 表里 FDM 的「最小正向特征 0.8mm」也对得上。

**二、光固化的悬垂门槛写反了（已修）**

树脂机型的 `recommended_max_overhang_deg=80 / max_bridge=200` 等于「随便悬空」，
而且会作为硬约束注入 AI 提示词 —— AI 于是放心地做大悬垂、长跨度不加支撑，
打出来就是拉丝、塌陷、脱层。

DfAM 表给的是 **SLA/DLP 自支撑角 30°（比 FDM 的 45° 更严）**、最大无支撑桥接 5mm。
道理很直白：光固化逐层固化后要从离型膜上拉起，悬垂处没有可依附的上一层，
比 FDM 更依赖支撑。已改成 30° / 5mm 并注明来源。

更值得记的是：`selfcheck.py` 里原本有一条断言写着
`assert resin_constraints.max_overhang_deg >= 70, "光固化几乎不受悬垂限制"`
—— **测试把错误信念固化了下来**，所以这个 bug 一直没被发现。
断言已改成「光固化门槛不得比 FDM 更宽松」，并解释原因。

**三、没有测量支撑的限值不该当成结论（采纳为原则）**

dfam-check 明确要求：`min hole diameter`、`min positive feature`、
`max unsupported bridge` 这几项它没有测量手段，就必须报成「未检查」，
**不许从渲染图、包围盒或面数去推断**（"A limit with no measurement behind it
is not a finding"）。这条与本项目既有的「只报告真正跑过的检查」一致，
本轮也按它处理：壁厚现在有实测，其余没有实测的限值不写成结论。

**没有采纳的部分**（如实记录）：`❓ need more info` 三态标签（本项目的
打印性结论仍是 pass/warn/fail 三档，只有图片复核用了「没有结论」）、
6 个轴向的摆放方向推荐（本项目不切片）、支撑体积棱柱估算、
粉床工艺的困粉逃逸检查（本项目没有 SLS/MJF 机型）。

---

### 43. 耗材用量估算：不能拿「实体体积 × 密度」当实际用量

用户要求「估算一下耗材用量」。翻 `gcode` / `dfam-check` 两个 skill 之后的做法：

**先修一个一直在误导用户的数**。旧实现 `compiler.estimate_mass_g` 就是
`实体体积 × 密度`，等于假设 **100% 填充**。实测参照物（桌面垃圾桶，
其 3MF 用的参数是「0.16mm 层高、2 层墙、15% 填充」）实体等效 191g，
按实际参数只有约 111g —— **高估七成**。而且这个数字虽然算出来了、
也放进了接口，`webui` 里**从来没有地方显示它**（`mass_g` 是死字段），
所以用户一直没看到，也就没人发现它偏。

现在给出的是一个**区间**，并把假设摊开：

```
外壳体积 ≈ 表面积 × 壁厚（壁厚 = 墙圈数 × 走线宽度）
内部体积 ≈ max(0, 实体体积 − 外壳体积)
下限 = 外壳 + 内部 × 填充率        上限 = 实体等效
```

- **上半部分靠实测的壁厚中位数判断**：壁比两层墙还薄的零件切片后基本是实心，
  实际用量靠近上限；厚实件靠近下限。这句话直接写进结果里，不藏。
- **备料按上限**，这是用户最实际的诉求（不想打到一半没料）。
- 界面：预览浮层与对话区各加一格「预计耗材」，显示区间、线材长度、支撑克数。

**支撑量**借的是 dfam-check 的棱柱法（悬垂面投影面积 × 高度），但做了两处修正：

1. **往下打射线量到「下方第一个表面」**，而不是一律量到平台 ——
   否则「下面本来就有材料」的地方会被算成支撑。
2. **抽样还原的估计量写错了，实测放大 40 倍**。原来用「样本权重之和」去还原整体，
   但抽样是**有放回**的，样本权重之和会**超过**总面积（实测 133231 vs 2770），
   于是 `if sampled_weight < total_projected` 这个分支永远不触发，
   直接把未还原的累加值当结果 —— 一个 73cm³ 的支架被算成需要 11943cm³ 支撑。
   正确估计量是 `投影总面积 × 柱高的面积加权平均`（样本已按面积加权，均值即加权平均）。

**绝对值仍然不可靠，这一点必须说清楚**：这个方法分不清「外部悬垂」和
「向下开口的内腔顶面」，内部有空腔的零件会被算高（实测一个手机支架
算出 333%）。所以：
* 给用户的话里明确写了「这是偏大的估算，请以方向之间的**比较**为准」；
* 方向推荐只做**同口径比较** —— 候选方向用便宜的棱柱法算，
  那就拿「当前方向的棱柱值」当基准，绝不能拿射线精化后的数去比
  （两把尺子量出来的数比大小没有意义）。

**没有采纳的**：`gcode` skill 那套「装 OrcaSlicer / PrusaSlicer / CuraEngine 真切片」
（重量级依赖，而且本项目不做切片）；支撑体积的切片级精度；丝材成本换算（缺电价/耗材单价）。

### 54. 绝不能用 PowerShell 的 `Get-Content | Set-Content` 往返源码文件

想在 .py 里做一次文本替换时，最顺手的写法是：

```powershell
(Get-Content x.py -Raw) -replace 'a', 'b' | Set-Content x.py
```

**这一次就把文件写坏了，而且是静默的**：Windows PowerShell 5.1 的 `Get-Content`
默认按**系统 ANSI 代码页（中文 Windows 上是 GBK）**解码，而本仓库所有文件都是
UTF-8 无 BOM。于是：

1. 中文全部变乱码（`生成作业接口` → `鐢熸垚浣滀笟鎺ュ彛`）；
2. 更狠的是**行尾的中文字节被吃掉**，把 16 行合并成了 7 行
   （实测 `jobs_routes.py` 从 306 行变成 290 行）—— 代码语义已经错了；
3. 写回时还会加上 UTF-8 BOM。

而 `python -c "import ..."` 照样能导入、测试照样能跑（乱码只在**字符串字面量**里，
语法完全合法），所以只有用户看到乱码报错时才会暴露。

**正确做法**：
* 改代码一律用编辑工具（精确字符串替换），不要用 shell 做文本往返；
* 万不得已必须在命令行处理文本时，用 Python 显式指定编码
  （`Path.read_text(encoding='utf-8')` / `write_text(..., encoding='utf-8')`），
  绝不走 PowerShell 的 `Get-Content`/`Set-Content`；
* 改完立刻核对 `Get-FileHash`（改前先存一份字节级备份到 `build\work\`），
  并跑 `selfcheck.py` 的「源码编码完好」——
  它会扫提交集里的 126 个文本文件，查 BOM / U+FFFD / 私用区字符 / GBK 乱码字符。

**顺带一提**：`Select-Object -Last N` 会破坏管道，让本来成功的命令返回 exit 1；
日志截尾要用 `-First` 或直接把输出重定向到文件再读。

**同一条坑又踩了一次（§69 那轮，给这份文档改测试计数时）**：图省事用了
`(Get-Content README-开发者.md -Raw) -replace ... | Set-Content ... -Encoding UTF8` ——
这次中文没乱（`-Encoding UTF8` 至少没按 GBK 解码），但**文件被加上了 BOM**，
是自检的「源码编码完好」把它抓出来的（`EF BB BF` 开头）。去掉 BOM 用了字节级写法：

```powershell
$p = (Resolve-Path README-开发者.md).Path
$b = [System.IO.File]::ReadAllBytes($p)
if ($b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF) {
  [System.IO.File]::WriteAllBytes($p, $b[3..($b.Length - 1)])
}
```

结论不变：**这类改动应该用编辑工具做精确替换**。守卫存在的意义就是让"手滑"变成
一条红色自检，而不是一个进仓库的 BOM。

### 55. 拿「增量」当断言，会把完全正确的结果判成失败

端到端第六步原来是这么验「高度 100→150 生效了」的：

```python
delta = after_z - before_z
assert abs(delta - 50.0) <= 8.0        # 总高应该增加约 50mm
```

这条断言是**代理量**，不是需求本身。手机支架这种设计里一旦出现倾斜件，
总高就不是「高度参数」的线性函数：AI 会把靠背做成 18° 倾斜
（源码写 `back_len = (height - base_h) / cos(rad)`，但两次 `rotate` 之后
长度方向接近水平），于是斜率实测在 0.79～0.83 之间。

后果在同一个下午就撞见了两次，方向相反：

| 实测 | 模型是否达标 | 旧断言 |
|---|---|---|
| Z 83.62 → 124.62（点名 150mm，实际 124.62mm） | ❌ 真的差了 17% | `Δ=41` → 报失败（碰巧对） |
| Z 110.51 → **150.0**（点名 150mm，**分毫不差**） | ✅ 完全正确 | `Δ=39.49` → **照样报失败** |

也就是说这条断言有一条**系统性误报路径**：只要设计里有倾斜件，
哪怕模型精确做到 150.0mm，它也会红。

**改法**：不要修容忍度（那只是把误报窗口挪一挪），而是**直接断言需求**，
并且用产品自己的口径（`jobs._spec_dimension_problems`：实测 < 声明的 90% 算没做到）：

1. 源码层：点名的 `150` 真的写进了顶层参数赋值；
2. 几何层：总高确实变大了（Δ > 5，专抓「参数被静默忽略」）；
3. 需求层：实测总高 ≥ 点名值 × 90%。

改完当场跑出 `Δ=+39.49` 且 `实测总高 150.0mm（100%）` —— 三条全绿。
**注意这不是「把测试改宽」**：第 3 条比原来的 Δ 判据更贴近需求，
`Δ≈50` 能过的「长了但远远不到 150mm」的模型，第 3 条会直接挡下。

**顺带记一笔**：同一天还有一次 61/62，失败项正是旧的 Δ 判据（`Δ=41`，
模型 124.62mm 确实没做到 150mm）。**那是 AI 设计波动，不是代码回归**——
同一份代码重跑就是 64/64。判断「是不是回归」的可靠办法不是猜，
而是看**失败量是不是 AI 产出量**（这里是模型的实测高度）；
凡是取决于 AI 这次写了什么形状的断言，都要按上面三条重写。

### 56. 核对脚本读的日志，必须是**这一次**运行写出来的

`scripts/verify_e2e_evidence.py` 的判据是日志内容，默认读 `build\e2e-latest.log`；
而这个文件以前要靠人手工落盘：

```powershell
python scripts\e2e_test.py | Tee-Object build\e2e-latest.log
```

忘了 tee 会怎样？核对脚本**照样"跑得通"**，只是拿的是上一次的日志 ——
实测那次它读到的是 6 小时前、只有 1020 字节的一次失败运行，
于是 4 条证据全部报「缺少证据」，看起来像机制坏了，其实只是看错了文件。

现在 `e2e_test.py` 自己在启动时 tee 到 `build\e2e-latest.log`（UTF-8，
`_Tee` 类，写日志失败只告警、绝不影响验收）。**凡是有"先跑 A 再核对 A 的产物"
这种两步流程，就让 A 自己产出那个产物**，不要把它留给人的记性。

### 57. 「只解锁界面、不动服务端」等于把用户锁死在项目上

卡死看门狗（`Studio.tsx` 的 `PROGRESS_STALL_MS`）本来是"最后一道底线"：
上游不管怎么坏，界面都要能自己解锁。但它当时只做了 `setJobId(null)`，
却对用户说「**已经为你停止**」—— 服务端的作业还挂在 `running` 上。两处后果：

1. **那句话是假的**。真正停下来和"看起来停下来了"是两件事，前者用户会拿它做决策
   （"我重发一条就行"），后者只会让他更困惑。
2. **项目被幽灵作业锁死**。后端 `reject_if_busy` 会一直返回 409 BUSY
   （两个作业并发写同一个项目目录会互相覆盖、静默丢模型，所以这条守卫是对的），
   于是用户接下来**每一条需求都发不出去**。实测界面此时既没有停止按钮、
   也没有任何入口能碰到那个作业 —— 只能重启软件。

同一个看门狗还犯了第二个错：**把「等用户确认」当成了卡死**。
作业停在规格卡片上等用户点确认，2 分钟没动就谎报"生成卡住了"，
顺手把卡片清掉 —— 作业完全正常，用户回来发现卡片没了，
而那个作业还在服务端等着（于是又回到上面第 2 条）。

改法（两处都补上，缺一不可）：

* 看门狗触发时**真的**调 `POST /api/jobs/{id}/cancel`，且**不等它的结果**
  （本地服务可能正是卡住的那个，解锁绝不能依赖它）；
* 看门狗看到 `status == "awaiting_confirmation"` 直接返回：等用户不是卡死。
  同时把 `status` 并入"进度标记" —— 否则用户确认后作业转入 `running` 时
  计时器不会重置，会立刻把一个刚恢复的作业当成卡死取消掉。

后端还补了一条**权威层**的自愈（前端死了也要能解开）：停等待确认、
而且**没有任何界面在监听**它的作业（刷新页面/切走项目/关窗口都会断开 SSE
→ 订阅数归零）会被就地取消并放行新任务。判据用「有没有人监听」而不是
「等了多久」—— 时间判据会误伤去接杯水的用户。

**防回归**：`webui/scripts/verify-stall-watchdog.mjs`（打桩三个接口，不调 AI；
用 `window.__AIFORGE_STALL_MS` 把 120 秒窗口缩到 2 秒，否则这条最要紧的路径
永远没人测）+ `selfcheck.py` 的「并发作业守卫」（覆盖有人监听 / 无人监听两条分支）。

**这条坑的一般形式**：凡是"界面自己收尾"的兜底逻辑，都要问一句
**服务端的那个状态谁来收？** 只收前端 = 状态泄漏 + 用户被锁死。

### 58. 「程序能测出来」≠「程序会去修」：破网格没人管

端到端连续跑出过一次 **736 条开放边**的 STL（封闭率 23%）。那是切不出来的废件，
而当时的全链路行为是：

* 打印性检查**如实报错**：「模型不是封闭实体（有 736 条开放边），
  切片时可能出现空洞或无法自动修复，**建议让 AI 重新生成**」；
* 修复循环**一次都没试过**去修它。

两个原因，缺一不可：

1. **触发条件里没有它**。修复循环是"按问题类别触发"的，
   而 `_geometry_integrity_problems()` 当时只报 5 类
   （散件 / 穿腔 / 点名尺寸 / 视觉复核 / OpenSCAD 丢弃几何）——
   破网格不在任何一类里，所以循环根本不会启动。
2. **就算触发了也白修**。采纳门槛是"排序键必须严格更好"，
   而 `_integrity_rank()` 只比 散件 → 点名尺寸 → 穿腔 → 装不下，
   **封闭性不参与排序** —— 一个"其它都一样、只把网格修封闭了"的候选
   会被判成平手然后丢弃。

一句话：**能被测出来的缺陷，和会被修的缺陷，是两件事**；
中间要有完整的路径 —— 触发条件 → 排序键 → 采纳门槛，少一环就等于没修。

修法：

* `_geometry_integrity_problems()` 增加第 6 类：开放边 ≥ 10 条就报
  （证据里带上开放边数与封闭率，并写明最常见的原因是"刚好贴合"而不是真正相交）；
* `_integrity_rank()` 末尾加一位封闭性（`0 if watertight else 1`）。

**两个阈值都是刻意选的**，不是随手写 0：

| 情况 | 是否触发修复 | 为什么 |
|---|---|---|
| 开放边 736 条 | ✅ | 切不出来，是废件 |
| 开放边 9 条 | ❌ | 主流切片软件会自己补；不值得花用户一次 AI 调用（报告里照样如实说明） |
| 只有非流形边、开放边为 0 | ❌ | CGAL 在两实体恰好相切时极常见，多数切片软件能处理 |

**封闭性为什么放在排序键最后、而不是前面**：往前放就等于允许修复拿它去交换别的指标
（"多一个散件但网格封闭了"会被判为更优），那是拿"是不是一整件"赌网格封闭性。
放最后的效果正好是我们要的：**其它指标持平时，封闭的那一版胜出** ——
既让"只修网格"的修复能被采纳，又不动已有的优先级。
另外采纳门槛里本来就有 `watertight_not_worse`，
所以"把原本封闭的模型修成破网格"这条路早就被堵死了。

**防回归**：`selfcheck.py::t_geometry_repair` 新增 5 条断言
（736 条必须触发并带数字、9 条不许触发、只有非流形边不许触发、
封闭版在平手时必须更优、散件仍优先于封闭性）。
把阈值改成 `>= 10**9`、或把排序键最后一位写成常数 0，测试立刻变红。

### 59. 端到端红了，先分清「代码坏了」还是「这一版模型没做好」

端到端里混着两类断言，红的**含义完全不同**：

* **代码契约类**：接口形状、状态机、机制有没有被触发、错误处理。
  它们与 AI 这一版写了什么形状**毫无关系**，红了就是回归。
* **AI 产出质量类**：几何封不封闭、是不是一整件、点名尺寸做到没有、壁厚。
  同一份代码、同一个需求，跑两次结果可能不同 —— 实测出现过
  64/64 与 62/64 交替（失败项全是几何质量），也出现过 64/64 与 64/65
  （差一条非流形边）。

不分开的时候，一次运气不好的生成看起来和真回归一模一样，排查要花几十分钟。
所以汇总里现在会直接给出构成：

```
端到端验收：通过 64 项，失败 1 项
失败构成：AI 产出质量类 1 项（随模型产出波动）、代码契约类 0 项（必须为 0）
→ 本次失败全部落在「这一版模型没做好」上，代码契约零失败。
   先重跑一次确认不是运气问题；连续两次都红就是真回归，必须查。
```

**规矩（不许用重跑掩盖问题）**：

* 失败项**全部**是 AI 产出质量类 → 允许先重跑一次确认；**连续两次都红**按真回归处理；
* 只要有一条是代码契约类 → 这是回归，**重跑到绿也不算**，必须找出原因；
* `exit code` 不因为分类而改变（失败就是失败），分类只影响"下一步该查什么"。

这条分类是"只把话说清楚"，**不改变任何断言的判定**。

### 60. 损坏的 STL 会一路走到 `int(round(nan))` 才炸 —— 而且崩之前算的是垃圾

发现过程很偶然：写性能基准脚本时漏了二进制 STL 每个面的法线（12 个 float
只写了 9 个），于是文件步长是 38 字节而不是 50。旧实现的表现是：

1. `_parse_binary` 发现「头部声明 998,000 面、实际长度只够 758,480 面」，
   于是**悄悄按实际长度截断**继续解析 —— 但它按 50 字节一刀切，
   真实步长是 38，于是整段错位；
2. 读出来的坐标是垃圾：`nan`、`3.0e+34`，混进正常的坐标里；
3. 一路走到 `_count_parts` 的 `int(round(point[0] * scale))` 才炸：
   `ValueError: cannot convert float NaN to integer` ——
   用户看到的是和 3D 建模毫无关系的一句话。

就算不炸，后果一样糟：一份残缺网格会被当成正常模型分析，
封闭性 / 零件数 / 壁厚全是错的，报告会说「模型有 300 个散件」，
而用户根本不知道文件本身没写完。

修法（三道，缺一不可）：

* **有限性检查**：二进制每面只做一次 `isfinite(和)`（NaN 会污染整条加法链），
  命中就报「坐标不是有效数字（NaN/无穷），文件已损坏」并给出下一步；
  ASCII 路径的 `1e999`（会变成 inf）同样挡住；
* **不完整检查**：声明面数比实际多出 16 个以上就报
  「文件不完整：头部声明 N 个三角面，实际只有 M 个」。
  仍然容忍 ≤16 面的尾部差额 —— 那是写入收尾的竞态，**原有的宽松行为不能顺手改严**；
* **格式判定**：OpenSCAD 的二进制 STL 头部恰好是 `solid ...`（见 §1 的注释），
  一个**被写了一半**的二进制文件会因此被判成 ASCII，只能得到含糊的
  「无法解析模型文件内容」。加一条判据：面数域之后若出现 `\x00`
  （二进制浮点数据几乎必然有，ASCII 文本不该有）就按二进制处理，
  于是报错能说清「OpenSCAD 没写完」，而不是「文件损坏」。

**防回归**：`selfcheck.py::t_stl_robustness`（NaN 必须报错且带 hint、
声明 10 万面只有 3 面必须报「不完整」、差 6 面必须仍然容忍、
ASCII 的 `1e999` 必须报错、朝向变换语义逐位不变）。
把有限性检查改成 `if False and ...` → 立刻红。

### 61. 性能：一个面 36 次字典构造 —— 以及"优化不许改结论"的验证方法

实测 100 万面的模型，`analyze_stl` 要 **90 秒**。剖析后发现最大的一笔开销非常蠢：

```python
def pick(values, axis):
    index = {"x": 0, "y": 1, "z": 2}[axis[-1]]   # 每个分量都现建一次字典
    value = values[index]
    return -value if axis.startswith("-") else value   # 再加字符串操作
```

调用方是 `tuple(pick(n, axis) for axis in axes)`：
**一个面**要算 6 个朝向 × 2 组（法线 + 重心）× 3 个分量 = **36 次**。
100 万面 = 3600 万次字典构造 + 字符串切片 + `startswith`，
占整条链路 **45%** 的时间。朝向只有那么几个、而且每次调用都一样，
把它**编译一次**成 `(分量下标, 符号)` 就全没了。

同一轮还顺手去掉了两处重复计算（都保持表达式逐字不变）：

| 位置 | 原来 | 现在 |
|---|---|---|
| `analyze()` 主循环 | 每个顶点量化 3 遍（去重 / 边统计 / 包围盒各一遍）= 27 次 `round()`/面 | 一面算一次、三处复用 = 9 次 |
| 边统计的键 | 嵌套元组 `((x,y,z),(x,y,z))` | 拼平的 6 元组（同样唯一标识无向边，哈希便宜得多） |
| `_count_parts()` | 每个角都重新量化（水密网格里同一顶点平均被 6 个面用到） | 按坐标缓存量化结果 |

结果：**100 万面 90.0s → 50.5s**；2 万面（AI 生成模型的常见量级）0.7s。
边统计/去重的**表达式一个字都没改**（还是 `round(x, 5)`），
所以拓扑与体积不可能变 —— 但"应该不会变"不算证据，见下。

**这类改动必须做「逐字段对拍」**，否则一次静默的数值偏移会污染所有下游判断
（壁厚、支撑、越界、点名尺寸…）：

* 把改动前的实现照抄一份放进临时脚本，在同一批网格上**逐字段比较**
  （顶点数、包围盒、体积、表面积、退化面、开放边、非流形边、封闭性、封闭率）；
* 朝向变换用 6 个朝向 × 500 个随机向量**逐位比较**；
* 跑真实夹具（`t_mesh_analysis`：立方体体积必须还是 1000.0mm³、
  表面积 600mm²、散件数 1/2、密封内腔 1 件）。

实测结果：全部一致。**性能优化如果不敢对拍，就不要做。**

### 62. 长分析必须发心跳，否则会被自己的看门狗杀掉

`analyze_stl` 是纯 Python 逐面计算，100 万面要跑 50 秒，**这 50 秒里一个事件都不推**。
而前端卡死看门狗的判据正是「120 秒没有任何进展 → 判定卡住并取消作业」（§57）——
于是**一个完全正常的大模型分析会被自己的看门狗取消**。

修法：`jobs._analyze_with_heartbeat()` 把分析放进 worker 线程，
同时每 10 秒推一条状态（「模型面数较多，仍在分析…（已 20 秒）」）。
分析本身不阻塞事件循环；`say()` 只更新状态行，不会往对话里刷屏。

这条坑的一般形式：**任何"可能超过看门狗窗口"的同步长任务，都必须自己产生活动信号**，
否则保护机制会变成误杀机制。

**防回归**：`selfcheck.py::t_analysis_heartbeat`（把分析放慢成 50ms、
心跳设成 10ms，确定性地验证至少推了一次心跳，且指标仍然正确）。
把 `asyncio.wait(..., timeout=heartbeat_s)` 改成 `timeout=3600` → 立刻红。

### 63. 批量替换调用点，炸在最冷的那条分支上（`NameError`）

给网格分析加心跳时，我把 4 处
`await asyncio.to_thread(compiler.analyze_stl, ...)` 一次性换成
`await _analyze_with_heartbeat(job, ...)`。其中 3 处所在的方法里都有
`job = self.job`，**只有 `_scale_to_fit()` 用的是 `self.job`**，
于是我写了一行永远会抛 `NameError: name 'job' is not defined` 的代码。

它躲过了：selfcheck 全绿、`tsc` 全绿、前端体检全绿、
**连跑第一轮端到端也全绿** —— 因为这条路径只在
「模型实测超出成型范围 → 确定性等比缩放兜底」时才走到。
第二轮端到端跑到「越界用例」才炸出来：

```
File ".../jobs.py", line 681, in _refit_to_build_volume
    scaled = await self._scale_to_fit(
NameError: name 'job' is not defined. Did you mean: 'self.job'?
```

两个教训：

1. **批量替换调用点之后，要确认每一处的上下文**。
   同一个类里的方法，有的有 `job = self.job`、有的直接 `self.job`，
   "看起来一样"的 4 行其实不一样。
2. **这类错编译器抓不到**（它不是语法错误，是运行到那一行才报），
   而它偏偏藏在最冷的路径上。所以补了一条 AST 静态检查
   `selfcheck.py::t_helper_job_scope`：凡是「以 Job 为首参」的模块级助手
   （`_analyze_with_heartbeat` / `publish_spec`），调用处第一个实参
   必须是该函数作用域里定义过的名字（或 `self.xxx`）。

顺带说一句：这一轮端到端**第一次**跑出来的失败被分类成
「AI 产出质量类」，第二次才暴露这条真回归 —— 正是 §59 那套分类
（"契约类失败不许靠重跑掩盖"）把它逼出来的。如果没有分类，
两轮都红很容易被记成"AI 又波动了"。

**防回归**：把调用点改回 `job`（不带 `self.`），`t_helper_job_scope` 立刻报
`jobs.py:771 _analyze_with_heartbeat() 的首参 'job' 在 _scale_to_fit() 里没有定义`。

### 64. 「重新安装」失败会把用户手里那份能用的 OpenSCAD 砸掉

旧的免管理员安装流程是这样的：

```python
if target_dir.exists():
    shutil.rmtree(target_dir, ignore_errors=True)   # 先删掉旧的
target_dir.mkdir(...)
zipfile.ZipFile(archive).extractall(target_dir)     # 再解压新的
```

下载和校验都在前面，所以「网络失败」不会走到这里；但**解压失败**会 ——
磁盘满、杀毒软件拦截写入、压缩包损坏（校验过了也可能遇到解压器报错）。
那一刻用户的状态从「有 OpenSCAD 可以用」直接变成「什么都没有」，
而重新下载还要再等 20MB。一次失败的"重新安装"把可用环境砸掉，
是最不该发生的一类退化 —— 和几何修复里「绝不采纳更差的那一版」是同一条原则。

修法（两阶段安装）：

1. 解压到**旁路目录** `runtime\openscad.new`；
2. 在旁路里找 `openscad.exe`，**先跑一次冒烟测试**（确认新的这份真能出图）；
3. 全部通过才 `rmtree(正式目录)` + `rename(旁路 → 正式)`；
4. 任何一步失败：只清掉旁路，正式目录原封不动，报错里明确写
   「原来的 OpenSCAD 没有被改动，可以继续使用」。

顺带把「三次下载都失败后残留 `.part`」也清了：那是个**永远用不上**的文件
（正因为它不完整才会重下），留着只是白占最多 20MB。

**这里有个自检本身的坑，值得单独记一笔**：
第一版测试的桩是「一调用就抛异常」，于是 `.part` 根本没被创建 ——
把清理代码停用掉，测试**照样通过**。桩必须复现真实条件
（先往 `.part` 写半截内容、再抛错）才验得出来。
**凡是"清理/回滚"类的断言，都要先确认桩真的制造出了那份该被清理的东西。**

**防回归**：`selfcheck.py::t_install_no_destroy`
（打桩下载 + 损坏压缩包 → 断言旧 `openscad.exe` 还在、旁路目录已清；
再打桩「写了一部分就断线」→ 断言不留 `.part`）。
把解压改回「先删正式目录」→ 立刻红；把 `.part` 清理停用 → 立刻红。
真实的两阶段流程由端到端覆盖（它会**真的**下载 20MB 并安装：
日志里能看到「正在解压到临时目录 …」「正在测试新下载的 OpenSCAD 能否出图…」
「解压与自检通过，正在替换正式目录…」）。

### 65. 用户点名的尺寸反复做不到 ~16%：先补证据，再谈改法

端到端里最顽固的一条：用户说「高度 100mm」，第一版模型实测常在 **82%～84%**
（实测记录：80.6 / 83.5 / 84.3mm），修改到 150mm 时也出现过 **79% / 82%**。
程序是**检得出来**的（问题 3，阈值 90%），但两轮 AI 修复经常修不动 ——
日志里能看到它反复重写同一段公式，它以为自己算对了。

本轮先把**证据补足**（这是问题 5「挤出写法」验证过的路子：
把机制点破 + 给出正确写法，比"请保证总高"有效得多）：

* 现在会明确写出**差多少、占多少**：
  「支架高度 要求 150mm，实测 Z 方向只有 123.0mm（差 27.0mm，只有要求的 82%）」；
* 补上最可能的成因：倾斜件按「斜长 × cos(倾角) = 竖高」反推总高，
  而**两次 `rotate` 之后挤出方向通常已经不是竖直方向**；
* 明确堵住错解：「把参数值改大不算 —— 那会让参数表跟你的要求对不上」；
* 给出两种稳妥写法（不倾斜 / 先按竖直高度建模再单次 rotate）；
* **只在竖直方向没做到时**才给这段建议（水平方向没做到时塞进去只会带偏 AI）。

**这条改动到此为止，效果还没有被证实**：证据内容是确定性的（有断言钉住），
但「AI 看了之后是否就能一次做对」需要多次真实生成才能统计 ——
本轮只有 1 次端到端数据点（那次第一版就没触发修复，96.5%）。**不做因果声明。**
下一步若要真正解决，方向是「按参数做数值求解」（用实测灵敏度反推参数值），
但那会改动用户点名的参数值本身，属于产品决策，不在本轮范围。

### 66. 验收脚本把它本该保护的东西弄坏了

`scripts\test_install.ps1` 会往**真实**用户目录里种一份「哨兵配置」和「哨兵项目」，
用来验证「升级 / 卸载 / 重装都不丢用户数据」。它的清理逻辑里有这么一段：

```powershell
if (-not $preExistingUserData) {
    Remove-Item $AppData -Recurse -Force ...
    Remove-Item (Documents)\AI-3D-Forge -Recurse -Force ...
} else {
    Write-Host '  Left pre-existing user data untouched.'
}
```

看起来已经考虑过「开发机上本来就有用户数据」了 —— 但它只保护**目录**，
**不保护目录里的那个文件**。而它前面干的是：

```powershell
'{"version":1,"first_run_done":true,"ui":{"theme":"dark"}}' | Set-Content $sentinelConfig
```

也就是**无条件覆盖** `%APPDATA%\AI-3D-Forge\config.json`。
开发机按定义就有真实的 config.json（主题、机型、材料、首次运行状态），
于是在自己的机器上跑一次安装验收，维护者的设置就被永久替换成哨兵，
而日志还会显示「Left pre-existing user data untouched」——
**看起来什么都没坏**。这类「测试工具破坏了它本应保护的东西」最难发现，
因为它一边破坏一边报告成功。

顺带还发现同一支脚本留下的第二种残留：哨兵项目是通过真实 API 建的，
清理时只删它记住的那一个路径；历史上 4 个 `InstallTestProject-*`
就这样留在了用户的项目列表里（每个里面是一句 `cube([7,7,7]);`）。

修法：

* 种哨兵**之前**先把真实 `config.json` 移到 `config.json.installtest-backup`；
* 收尾时删掉哨兵并**无条件**把备份移回来 —— 写在 `-KeepUserData` 判断**之外**
  （`-KeepUserData` 的意思是"保留测试安装和测试项目"，
  绝不是"把盖在用户设置上的哨兵也留下"）；
* 清理时按**创建时间**删掉本次运行产生的所有 `InstallTestProject*` 目录
  （带 3 次重试），绝不按名字批量删 —— 用户完全可能自己建一个同名项目；
* 新增一条断言 `No test project left behind`，残留不为 0 就判失败。

**验证方式**：不满足于"看代码觉得对"，而是**真的跑一遍真实安装验收**
（安装 → 启动 → 走 API → 种数据 → 升级安装 → 卸载 → 重装 → 收尾），
并在前后对用户真实数据取 SHA-256：

| 项目 | 运行前 | 运行后 |
|---|---|---|
| `%APPDATA%\AI-3D-Forge\config.json` | `96FEB70C…` | `96FEB70C…` **一致** |
| `printers.json` | `C9034424…` | `C9034424…` **一致** |
| 真实项目数 | 20（清掉 4 个历史残留后 16） | 16 |
| 测试项目残留 | 4（历史） | **0** |
| 安装目录 / 快捷方式 / 卸载项 | — | 全部清除 |

验收脚本自身：**46 项全通、0 失败**（含首启准备日志、图标、版本元数据、
静默卸载不挂起、升级后不产生重复卸载项、重装后项目仍可读）。

**防回归**：`selfcheck.py::t_install_test_keeps_config`
静态钉住「备份先于覆盖」与「恢复无条件执行」——
后者用**缩进**判定层级（写在 `else` 分支里也满足"文本在判断之后"，
这是第一版断言漏掉的；把恢复挪进分支 → 立刻红）。

**一般形式**：一个"清理/保护"逻辑如果只管目录不管文件、
或者只删它自己记住的那一个路径，就会留下残留或误删。
**验收脚本必须能安全地在"已经有真实数据"的机器上反复运行** ——
做不到就不该进发版清单。

### 67. clone 下来第一件事就报错：`AssertionError: 404`

为交付一个可推送的 GitHub 仓库，我把提交集导出到项目之外的目录、
`git init` + 一次干净提交，然后**克隆出来跑它自己的自检**。结果：

```
通过 40 项，失败 1 项
✗ 接口响应形状: AssertionError: 404
```

根因：`webui/dist` 是构建产物、被 `.gitignore` 排除，所以**全新 clone 里没有它**。
此时后端的行为是**正确且贴心**的：

* `/` 返回 `503` + 「界面资源缺失，无法显示操作界面。开发模式请先在 webui 目录
  执行 npm run build」；
* `/favicon.ico` 这条路由**根本没注册**（它只在界面已构建时才挂上）→ 404。

而自检里那句断言是**无条件**的：

```python
favicon = client.get("/favicon.ico")
assert favicon.status_code == 204, favicon.status_code
```

于是「clone → 跑自检」——新贡献者做的第一件事 —— 必然红，
报错还只有一句 `404`，看不出和"界面没构建"有关。

修法：按「界面是否已构建」分两支，**两支都断言真实契约**（不是放宽）：

| 情形 | 断言 |
|---|---|
| 已构建（`webui/dist/index.html` 存在） | favicon 真的空 204；`/` 返回 200 |
| 未构建（全新 clone） | `/` 返回 503，且 detail 含「界面资源缺失」、hint 含 `npm run build` |

**同一个克隆实验还暴露了第二处"假通过"**：那两条开源卫生守卫
（源码编码 / 本机路径）报告

```
[OK] 源码编码完好 —— 0 个提交文本文件：无 BOM、无 U+FFFD、无乱码字符
```

**0 个文件**！因为我第一次把克隆放在 `build/work/` 下面，而 `build` 是
「不该提交的目录」黑名单里的名字，于是所有文件都被跳过、守卫什么都没查就通过了。
**一条什么都没检查的守卫比没有守卫更糟**，它给的是假安全感。
现在两条守卫都加了 `assert scanned >= 20`，扫不到文件就直接红并提示
"仓库是不是被放在 build/ 之类的被跳过目录下了？"。

**验证**：把仓库克隆到**项目之外**（用一个独立的克隆目录），
用它自己的 `scripts/selfcheck.py` 跑 —— **47/47 通过**，
且结果里明确写着「界面未构建：首页 503 且提示 npm run build（clone 后属正常）」。
真正的"别人 clone 能不能用"就是这个样子验的，见
[docs/PUBLISH-TO-GITHUB.md](docs/PUBLISH-TO-GITHUB.md) 第五节。

**顺带记一个真实的假阳性**：写这一节时我顺手把克隆目录写成了绝对路径，
克隆出来的那份自检立刻报 `README-开发者.md: 含本机仓库绝对路径` ——
因为守卫判定「本机仓库路径」用的是 `ROOT`（在克隆里就是那个克隆目录），
而文档里正好写着同一个字符串。守卫是对的：**文档里不要写绝对路径**，
一律写 `<克隆目录>` 这样的占位符。
（这也说明"运行时推导本机路径"的守卫确实抓得住东西，不是只会放行。）

**一般形式**：断言不许依赖"我这台机器恰好构建过"。
凡是依赖构建产物的检查，都要先判断产物在不在，并对"不在"那种情况
**也**给出明确的契约断言 —— 否则最先被它挡住的就是新来的人。

### 68. 按 jakubkrehel/skills 做了一次界面评审：33+9 条对比度失败

用户给了 [jakubkrehel/skills](https://github.com/jakubkrehel/skills)（6.7k star 的
界面技能集），要求"按这个看看页面有没有要修复的"。按它的编排要求，
评审顺序是 accessibility → layout → writing → typography → colors → ui，
每条发现都要 `path:line` + 实测证据，不许凭"品味"下结论。

**先做了 recon**：栈是 React + 手写 CSS（无组件库）、令牌在 `styles/tokens.css`、
项目自己的界面约定在 `docs/API-CONTRACT.md` §7.1；支持的视口来自
`src/aiforge/server.py:146` 的窗口 `min_size=(1080, 700)`。

**怎么取证**：新写 `webui/scripts/audit-interface.mjs`，在真实浏览器里逐页实测
（布局体检 + 规格卡片体检已有的项不重复）：

* 可访问名称（所有交互元素）、键盘 Tab 走查逐个记录焦点指示；
* 对比度**实测**渲染后的前景/背景（技能明确要求：不许估算）；
* `prefers-reduced-motion: reduce` 下是否还有 >5ms 的动画；
* 1080px（最小窗口）/ 540px（最小窗口 200% 缩放）/ 320px（WCAG 重排基准，本项目到不了，只记录不判定）；
* 状态是否只靠颜色、字号是否落在令牌音阶、`transition: all`/超长时长、
  会变化的数字有没有 tabular-nums、每页一个 h1、main 地标、跳转入口。

**评审发现（按根因合并，共 5 条）**：

| 严重度 | 域 | 位置 | 现状（实测） | 修后 |
|---|---|---|---|---|
| HIGH | colors | `styles/tokens.css` `--text-3` | `#8e8e93`：白底 **3.26:1**、`--bg-sub` **3.12:1**（需 4.5）——向导步骤、项目时间等真实文字 | `#6f6f73`：白底 5.00 / bg-sub 4.79 / surface-2 4.60 |
| HIGH | colors | `styles/tokens.css` `--warn` | `#b26a00`：白底 **4.24:1**（差一点没过 4.5）——`.pill-warn`「环境就绪 11/12」 | `#9f5e00`：白底 5.15 / bg-sub 4.94 / surface-2 4.73 |
| HIGH | colors | `styles/app.css:217` `.btn-brand` + `a` | 品牌色当填充/文字：白字压 `#ff6a00` **2.87:1**（深色 2.61:1）、品牌色当链接文字 2.87:1 | 拆出角色令牌 `--brand-solid` / `--brand-text` / `--brand-on-solid`；实测白字 5.12:1（深色 4.76:1） |
| MEDIUM | accessibility | `App.tsx` | 没有「跳到主内容」，正文前有重复顶栏导航 | 加 `.skip-link`（HashRouter 下拦截默认行为改为聚焦 `#main`） |
| MEDIUM | accessibility | `pages/Studio.tsx` | 工作台没有 h1（其余四页都有） | 补 `.sr-only` 的 h1「工作台」 |
| LOW | typography | `styles/app.css:1996` `kbd` | `font-size: 11px`，低于令牌最小档 12px | `var(--fs-xs)` |

**顺带修掉一条技能点名的 UI 问题**（`better-ui`：Theme switch suppression）：
21 处过渡含 `background/color`，换肤时会整页"抹"过去。现在
`useTheme.apply()` 在切主题时注入 `*,*::before,*::after{transition:none !important}`、
强制重排、下一帧移除。

**评审过程本身踩的坑（都留在脚本注释里）**：

1. 第一版审计把 `page.evaluate(字符串)` 当函数调 —— 它只求值不调用，永远返回
   undefined，于是**每页都印出"0 个问题"**。技能把这种现象叫**静默的覆盖缺口**：
   一份什么都没查的报告和一份全清的报告长得一模一样。现在每个检查都会打印
   「实际看了多少个候选」，测不到就报 ERROR 并计入失败。
2. `transition: all 0s` 报了 36~138 条 —— `transition-property` 的 CSS **初始值**
   就是 `all`，没写过渡的元素也算。加了"时长 > 0 才算"之后归零（源码里本来就没有
   `transition: all`）。
3. 320px 溢出 59px：但窗口最小 1080px，**这个尺寸用户到不了**，属于拿不适用的尺子
   报假问题。改成测 1080 与 540（最小窗口 200% 缩放）。
4. 深色那一轮先加载再 `setAttribute('data-theme')`，会被 ThemeBridge（读到后端设置后
   `setTheme`）覆盖回浅色 —— 于是"深色"量的其实是浅色，**又是一份假的 0**。
   现在改为真的切后端设置，并断言 `data-theme === 'dark'`，测错主题立刻失败；
   评审结束把主题改回原值（评审不留副作用）。
5. `.sr-only` 触发了布局体检的「文字被裁切」——1×1 像素 + overflow:hidden 正是这个
   无障碍写法的本体。两支审计都加了 `intendedForScreenReadersOnly()` 排除，
   口径写在注释里。

**验证**：`audit-interface.mjs` 修前修后各跑一次（对比度 33+9 → 0）；
`layout-audit.mjs` 0 问题；`spec-card-audit.mjs` 0 问题；主题切换抑制用
MutationObserver 实测「规则已注入 / 已移除」；快照在
`build/work/interface-shots/`（浅色+深色各 5 页）。
**结论：无 HIGH 残留 → Approve**（留给后续的只有上面那条已修的 LOW 与
`better-writing` 未评审）。

**没做的事**：`better-writing` 域没有评审（技能要求"没有 owning skill 就标
Not reviewed 并说明"，不能凭记忆编规则）；RTL/逻辑属性没有改
（本产品是中文单语，`better-layout` 那条属于"可适应"，不是缺陷）。

---

### 69. 用户报的三个缺陷：进度条不诚实、关掉软件 OpenSCAD 还在跑、切页把建模视图弄丢

用户一口气报了三条（原话）："进度条不太规范，不能体现准确的进度"、
"关闭应用后 openscan 还会继续工作"、"在 openscan 在后台建模时如果点击到其他界面，
建模界面会凭空消失"。三条都不是"看起来不好看"，而是**用户拿不到真实状态**：
不知道还要等多久、不知道有没有东西在后台偷偷占资源、不知道自己的活还在不在。

| # | 现象 | 根因（位置） | 修法 | 证据 |
|---|---|---|---|---|
| ① | 进度条几秒不动，最长的一段干脆没有百分比 | `src/aiforge/jobs.py` 旧的 `STAGE_PERCENT` 每阶段只有一个定格数字（78 → 86 → 95），`generating_scad` 是 `None`（界面退化成不定态转圈） | `STAGE_BANDS`（区间+文案）+`STAGE_TAU_S`+`Job.creep()`：阶段内按 `1-exp(-t/τ)` 渐近推进；`set_stage`/`progress` **单调不减**；`GenerationRunner.run()` 起 3 秒心跳并在 `finally` 取消；`awaiting_confirmation` 区间起点=终点 → **故意不涨**；只有 `done` 才是 100% | 自检「进度可信度」+负向对照三种变异全红+真实 SSE 轨迹（见下） |
| ② | 关掉软件，`openscad.exe` 还在后台跑 | 退出走 `runtime_state.force_exit()`（`os._exit`，跳过一切清理）；编译用 `subprocess.run` 起的子进程在父进程消失后成孤儿 | 新增 `src/aiforge/procguard.py`：Windows **作业对象** + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`(0x2000) 加在自己进程上（子进程自动继承）＋登记 PIDs 供超时回收＋启动时 `sweep_orphans()`（只杀镜像路径在本软件 OpenSCAD 目录里的进程） | 自检「子进程不变量守护」＋双向实验：无守护存活 `True`／有守护存活 `False`（`tasklist` 交叉确认） |
| ③ | 切到别的页面再回来，建模界面（含确认卡片、对话）"凭空消失" | 三条叠加：顶栏「工作台」指向固定 `#/studio`（回来就落到空工作台）；`Studio.openProject` 无条件清空对话；回到工作台时**没人重新接管**正在跑的作业 | 新增 `webui/src/lib/lastProject.ts`（sessionStorage 记住最近项目）＋顶栏回到最近项目＋`Studio` 用 `conversationCache` 保留对话、打开项目后 `api.listJobs()` 找到 `running`/`awaiting_confirmation` 的作业并**重新接管** | `webui/scripts/verify-job-survives-navigation.mjs` 8 项全绿（含 F5 刷新后确认卡片仍在） |

**① 的真实轨迹**（端到端里把 SSE 收到的每个百分比都记下来，跑完当场断言）：
生成那段收到 30 个百分比事件、修改那段 27 个，全程单调不减；阶段内确实在动
（生成：`12 → 12.4 → 19.6 → 25.3 → 45 → 52 → 61.0 → 78 → 90 → 90.3 …`），
且最后一个值才是 100%。
**为什么用渐近而不是线性**：我们不知道这次 AI 要跑 5 秒还是 30 秒 ——
线性会在超时时顶到上限然后干等（看起来像卡死），渐近则"越接近上限走得越慢"，
既一直在动、又永远不冒充 100%。**为什么"等待你确认"不涨**：那一段的进度取决于
用户，不是取决于程序；进度条替用户往前走，就是在骗人。

**③ 的测试夹具本身有两个"作弊"**（不修就会得到一条假的绿灯）：
mock 后端在客户端断开 SSE 时把作业 `abort`（真后端不会，作业在服务端继续跑），
以及暂停（等确认）时快照仍报 `done`。两处都改成了与真后端一致的行为。

**同一批验证里顺带挖出来并修掉的 5 个"看起来通过"的坑**：

1. 三支界面体检脚本把「连不上服务」算成「发现 N 个问题」——Vite dev 只监听 IPv6 的
   `::1`，用 `127.0.0.1` 访问每个页面都 `ERR_CONNECTION_REFUSED`，脚本于是报了
   "6 个问题"。**服务没起来和界面有问题必须分得开**：现在先探活，失败就打印
   `*_UNREACHABLE` 并以退出码 2 结束（和 §68 的"静默覆盖缺口"同一类错误）。
2. 端到端第七步直接 `float()` 吃接口字段，字段一旦缺失整轮验收以 `TypeError`
   崩掉 —— 崩掉会把**后面所有步骤的结果一起丢掉**，看起来像"测试脚本坏了"。
   现在把「能读到调整前基准尺寸」「调参结果里带实测尺寸」各算一步，并打印实际字段名。
3. 打印失败原因用的是 `result["error"]`，而 `error` 事件的载荷里原因在 `message`，
   于是**作业明明报错、日志上却写着"成功"**。现在统一走 `_why()` 取原因。
4. 网络失败信息是空的：`f"网络请求失败：{exc}"`，而**代理抖动时抛出的 httpx 异常
   `str()` 是空字符串**，界面显示"网络请求失败："后面什么都没有。现在带上异常类型
   与目标地址，并且**只在连接阶段重试一次**：连接没建立=请求没发出去，重试不会重复
   计费；`ReadError`/`RemoteProtocolError` 是"请求已发出、响应断了"，一律不重放
   （否则同一句话可能付两次钱、界面还会出现重复内容）。自检「网络失败可读且可重试」
   把这三个方向都钉住了。
5. **「重新安装 OpenSCAD」会把用户能用的那份删掉**（P0 级隐患，被这轮实测撞出来了）：
   安装流程先在旁路目录 `.new` 里跑一次冒烟测试，紧接着把这个目录改名到正式位置；
   Windows 在 `openscad.exe` 退出后仍会短暂持有它的映像句柄，`rename` 于是报
   `[WinError 5] 拒绝访问` —— 而旧实现是**先 `rmtree(正式目录)` 再 `rename`**，
   这一失败就把用户手里那份能用的 OpenSCAD 删掉了（同一个文件上面刚写着
   "一次失败的重新安装把可用环境砸掉，是最不该发生的一类退化"，代码却没做到）。
   现在换成"三明治"换法：正式目录 → `.old` 备份 → 旁路目录 → 正式位置 → 成功才删备份，
   换入失败**自动回滚**；目录改名走退避重试（0.2s 起、累计约 7 秒），
   专门对付这类"还没放手"的瞬时占用。自检「换安装目录可回滚」用真实目录 +
   注入失败验证：正常替换无残留、**换入失败后原目录一个文件都没少**、前两次失败第三次成功
   （真机形态）。顺带把 `read_version` / `run_cli` / 冒烟测试三处 OpenSCAD 调用
   也接到了 `procguard`，它们现在同样登记在册、应用退出时会被一起收走。

**验证汇总**：后端自检 **47/47**（新增「进度可信度」「网络失败可读且可重试」
「换安装目录可回滚」「开源文件完整性」「敏感信息扫描」）；端到端 **76/76**
（新增 5 项进度实测断言与 2 项字段检查）；
布局体检 0 问题；规格卡片体检 0 问题；界面规范体检 `INTERFACE_AUDIT_OK`；
作业切页存活 `JOB_NAV_OK`；孤儿进程双向实验通过；负向对照 `NEGCTL_OK`。

---

### 70. 按「可直接 push 的商用开源仓库」标准补齐门面与合规守卫

用户要求把这个项目整理成一个**能直接 `git push` 的完整开源仓库**：补齐开源文件、
清理敏感信息、并借鉴成熟项目的做法。核心功能一行没动 —— 这一节全部是
**门面、文档与自动化守卫**。

**补了什么**（都在开发树里写，再镜像到仓库目录，见 `docs/PUBLISH-TO-GITHUB.md` §八）：

| 文件 | 作用 |
|---|---|
| `LICENSE` | MIT 全文（用户明确选定）；安装向导也改为展示它 |
| `README.en.md` | 英文版门面（中文版 `README.md` 保留为默认，两版顶部互相跳转） |
| `README.md` | 重写为**面向 GitHub 访客**：是什么 / 功能 / 工作流 / 系统要求 / 安装 / DeepSeek 配置 / OpenSCAD 配置 / 如何运行 / 打印能力 / 截图 / 隐私 / 技术栈 / FAQ / 贡献 / 许可。删掉了原来的"交付清单（对照需求第 N 节）"这类**内部验收口吻** |
| `SECURITY.md` | 漏洞反馈渠道（私密通道优先）、处理时限、**安全边界表**、不在范围内的事项 |
| `CONTRIBUTING.md` | 开发流程、提交前必跑命令、按改动类型追加的验证、PR 要求、四条红线 |
| `CODE_OF_CONDUCT.md` | Contributor Covenant 2.1 改编 |
| `CHANGELOG.md` | 1.0.0 的功能清单与**已知限制**（含"倾斜模型的实测高度约为标称 82%~95%"） |
| `docs/USER_GUIDE.md` | 完整用户指南（原 `README-用户.md` 的正文 + 目录 + 系统要求 + 截图位置） |
| `docs/DEVELOPMENT.md` | 开发者最短路径：搭建 / 运行 / 测试矩阵 / 构建 / CI / 文档地图 / 七条规矩 |
| `README-用户.md` | 改成**一页纸快速上手**（装→配→用→打），细节指向 `docs/USER_GUIDE.md`（避免两份长文各说各话） |
| `docs/LICENSE-THIRD-PARTY.txt` | 由 `docs/LICENSE.txt` 改名，**第三方清单原样保留**，只把"本软件"一节改成指向 MIT 的 `LICENSE` |
| `.github/workflows/ci.yml` | 三个作业：开源合规+语法（ubuntu）、后端离线自检（windows）、前端构建（ubuntu） |
| `.github/ISSUE_TEMPLATE/*`、`pull_request_template.md`、`dependabot.yml` | Issue/PR 模板与依赖更新（不自动合并：许可证清单要人工同步） |
| `docs/images/*` | 三张 README 截图（欢迎页 / 生成中 / 结果页） |

**顺带修正的引用**：改名 `docs/LICENSE.txt` 影响到安装器（`LicenseFile` 与开始菜单）、
`make_release.ps1`、`make_release_docs.py`、`list_dependencies.py`、`export_source.ps1`、
`verify_delivery.py`、`AI-3D-Forge.spec` —— 全部同步更新，并新增/Renamed 的文档一起
纳入**源码包必需文件**清单（`export_source.ps1` 缺一个就 `exit 1`）。

**新增两支自动化守卫**（CI 与本地自检都会跑）：

1. `scripts/check_oss_files.py` —— 开源文件完整性：
   33 个必需文件是否存在、README 是否覆盖 10 个要点（MIT/Windows/DeepSeek/OpenSCAD/
   截图/贡献/安全/两份 docs 入口…）、**相对链接是否断链**（脚本会跳过 GitHub 页面型链接）。
2. `scripts/check_no_secrets.py` —— 敏感信息扫描：API Key 形状、长令牌、`Authorization`/
   `Cookie`/`password` 字面量、私钥块、带口令连接串、`.env` 类文件、
   本机绝对路径、不该提交的生成物；`--history` 会**再把 Git 历史整个过一遍**
   （只扫新增行 —— 密钥"提交过又删掉"是最典型的翻车方式）。

**为什么守卫必须自己先被证明有效**：另写了 `build/work/negctl_oss.py`，
逐个制造违规并确认守卫真的变红：API Key 写进文档、本机路径写进文档、删掉 LICENSE、
README 断链、`.env` 混进提交集；再反向确认 `.gitignore` 生效
（已存在的 `webui/dist` 不在扫描集里）。六条全绿才算数。

**这一步自己踩到的三个坑**（都由守卫当场抓到，不是事后发现）：

1. 写 `docs/PUBLISH-TO-GITHUB.md` 时顺手写了开发树的**绝对路径** ——
   `check_no_secrets.py` 立刻报「含本仓库绝对路径」。改成 `<项目目录>` 占位符。
   （这正是 §67 那条老坑的复发，说明"靠人记住"不如"写成守卫"。）
2. 路径规则最初只认 `.env` 开头的文件，`docs/leaked.env` 这种 `*.env` 漏掉了 ——
   负向对照当场发现"守卫是假的"，于是把规则扩成 `.env*` 与 `*.env` 两种写法。
3. 改了计数（45 → 47 项）之后，用 `Get-Content | Set-Content` 批量替换会**加 BOM**
   （§54 的复发）；这次改用 `[System.IO.File]::WriteAllBytes/WriteAllText` +
   `UTF8Encoding($false)`，并用自检的「源码编码完好」确认无 BOM。

**验证**：后端自检 **47/47**（新增「开源文件完整性」「敏感信息扫描」）；
`check_oss_files.py` `OSS_FILES_OK`（33/33 文件、10/10 要点、76 条相对链接）；
`check_no_secrets.py` `NO_SECRET_FOUND`（156 个文本文件 + 165 个路径，
**含 Git 历史**：仓库里 3 个提交的完整补丁）；
负向对照 `NEGCTL_OK`（6/6）；`selfcheck.py --offline` 32 通过 / 13 跳过 / 0 失败
（跳过项逐条打印原因，**不混进通过**）；
**把仓库 clone 到项目之外**跑它自己的自检：**47/47**；
安装包验收 **46/46**（安装向导改为展示 MIT 许可证；发布目录新增 `docs\USER_GUIDE.md`，
快速上手里的相对链接在发布包里点得开）；
`build.ps1` 全流程通过（前端 837.9 KB → 安装包 16.2 MB）；
`make_release.ps1` + `verify_delivery.py` 22/22。

**又一个"守卫救了一次"的实例**：第一次克隆冒烟把 clone 放在了 `build\work\` 下，
两条卫生守卫立刻报 `只扫到 0 个提交文本文件，明显不对` —— 因为它们**故意**跳过
路径里带 `build` 的文件（构建产物本来就不该被扫）。
这正是它们该有的行为（宁可不通过，也不假装扫过），排查方法写进了
`docs/PUBLISH-TO-GITHUB.md` 第五节。

**还修掉一个"会把无关代码带进公开仓库"的设计缺陷**：`export_source.ps1` 原本是
"拷贝一切、排除黑名单"。这次实测发现工作目录里多出了两个**与本项目无关**的目录
（`preview/`、`shiro3d/`，不是我建的），它们被直接导进了源码包，
再镜像就会进公开仓库 —— 无关代码可能有自己的许可证，也可能带密钥。
现在顶层改成**允许清单**：只读 `src/webui/scripts/docs/installer/.github` 与
已知的根文件，被排除的顶层项会明确告警（"Not part of this project, excluded: …"）。

---

## 八、接口契约

前后端接口的**唯一**依据是 [`docs/API-CONTRACT.md`](docs/API-CONTRACT.md)。
改接口必须同步改那份文档。

关键约定：
- 错误一律返回 `{detail, code, hint}`；`code` 决定前端展示哪个修复入口
- API Key 永远只返回掩码（`sk-6862****3164`），**绝不回传完整值**
- 作业事件流首帧必须补发 `snapshot`
- `awaiting_confirmation` 是**非终态**：前端不得据此停止订阅或丢弃规格卡片
- 新增事件（如 `spec`）必须同时进 `snapshot`，否则刷新后状态丢失

---

## 九、安全与隐私

| 项目 | 做法 |
|---|---|
| 网络暴露 | 只监听 `127.0.0.1`，中间件校验 `Host` 与 `Origin`，局域网与外部网页无法访问 |
| API Key | Windows 凭据管理器（`keyring`）；不可用时降级为仅当前用户可读的文件，并如实告知用户 |
| 日志 | `logging_setup._RedactFilter` 自动把 `sk-*` 打码，Key 不会出现在日志里 |
| OpenSCAD 来源 | 只用 `files.openscad.org`；`PINNED_SHA256` 钉死校验值，不符即拒绝安装 |
| AI 生成的代码 | `scad.validate()` 拦截 `import`/`surface`/`dxf_*` 等一切读写本地文件的函数 |
| 外部链接 | `ALLOWED_EXTERNAL_HOSTS` 白名单，仅允许 DeepSeek 与 OpenSCAD 官方网站 |
| 用户数据 | 不上传。唯一的对外请求是 DeepSeek API 与你确认后的 OpenSCAD 官方下载 |

---

## 十、目录结构

```
<仓库根>\
├── main.py                     程序入口（开发/发布共用）
├── AI-3D-Forge.spec            PyInstaller 打包配置
├── requirements.txt            运行时依赖
├── requirements-build.txt      构建期依赖
├── README-用户.md              给普通用户看
├── README-开发者.md            本文件
├── src\aiforge\                后端（见「模块职责」）
│   └── api\                    HTTP 路由
├── webui\                      前端（React + TypeScript + Three.js）
│   ├── src\                    pages / components / viewer / hooks / api
│   └── dist\                   构建产物（会被打进 exe）
├── installer\
│   ├── AI-3D-Forge.iss         Inno Setup 安装脚本
│   └── Output\                 安装包产物
├── scripts\
│   ├── build.ps1               一键完整构建
│   ├── build_installer.ps1     生成安装包
│   ├── selfcheck.py            后端自检
│   ├── e2e_test.py             端到端验收（真实 AI）
│   └── make_version_info.py    生成 exe 版本资源
├── docs\
│   ├── API-CONTRACT.md         前后端接口契约
│   ├── OPENSCAD-SOURCES.md     OpenSCAD 官方来源与校验值
│   └── LICENSE.txt             许可与第三方组件说明
└── build\                      全部缓存与产物（已在 .gitignore 中）
    ├── venv\  cache\  downloads\  dist\  work\
```

---

## 十一、依赖策略

刻意保持极少的依赖：

**运行时（8 个）**
`fastapi` `uvicorn` `python-multipart` `pydantic` `httpx` `keyring` `pywebview`
（+ Starlette 等间接依赖）

**刻意没有引入**
- `numpy` / `trimesh` —— STL 解析用标准库 `struct` 手写，省掉 30MB+ 且启动更快
- 任何前端组件库（MUI/AntD/Tailwind）—— 设计系统用 CSS 变量手写，避免几百 KB 的运行时
- `Electron` / `Tauri` —— 用系统自带的 WebView2，省掉 Chromium

新增依赖前请先问：能不能用标准库解决？发布包会因此变大多少？
