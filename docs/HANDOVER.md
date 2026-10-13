# 交接说明（给接手方 / AI 编码助手）

> 本文的目标：**让一个完全没参与过这个项目的开发者（或 AI 助手）在 30 分钟内能改对代码、跑得起验证、不踩已知的坑。**
> 如果你只读一份文档，就读这一份；每个结论后面都给到了可复核的出处（文件 / 命令 / 日志）。

> 取证只能指到**随仓库一起分发**的东西（代码、脚本、文档），或给一条别人能重跑的命令。
> 指不到的一律不写：本地日志（`build\work\` 是忽略目录）与不入库的记录都不算证据，
> 拿它们当出处就是文档骗人（本轮已把它定性为缺陷并删过一次）。

- 产品：**AI 3D Forge** —— 自然语言 → 参数化 OpenSCAD → STL → 3D 打印模型（Windows 桌面应用）
- 当前版本：**1.0.3**（`src/aiforge/version.py` 是唯一事实来源，安装脚本与 .iss 均从它读取）
- 交接时的产物：安装包 `installer\Output\AI-3D-Forge-Setup.exe`（16.4 MB）、免安装版 `build\dist\AI-3D-Forge\`
- 用户真实数据（**不在仓库里，也不要删**）：`%USERPROFILE%\Documents\AI-3D-Forge\Projects`
- 配置与日志：`%APPDATA%\AI-3D-Forge`（配置）、`%LOCALAPPDATA%\AI-3D-Forge`（运行时、OpenSCAD、日志）

---

## 1. 先读什么（按顺序，40 分钟）

| 顺序 | 文件 | 为什么先读它 |
|---|---|---|
| 1 | 本文 | 知道边界、命令、坑 |
| 2 | `README-开发者.md` §三 技术架构 + §七 踩过的坑 | 架构与**77 个已踩过的坑**，改动前必读 |
| 3 | `CHANGELOG.md` 1.0.3 段 | 最近删了什么、修了什么、**刻意不做什么与为什么** |
| 4 | `src/aiforge/jobs.py`（作业主流程） | 一条作业从需求到落盘的全过程都在这里 |
| 5 | `src/aiforge/changes.py`（改动契约与校验） | "用户说要改 A" 变成"只允许改 A"的核心判据 |
| 6 | `docs/API-CONTRACT.md` | 前后端接口的唯一依据 |

---

## 2. 30 秒上手（可直接复制）

```powershell
# 0) 一次性环境（会建 build\venv，装依赖、拉前端依赖）
cd <项目目录>
.\scripts\build.ps1                  # 前端 + 自检 + 打包 + 安装包，全流程
.\scripts\build.ps1 -SkipInstaller   # 只到免安装版
.\scripts\build.ps1 -SkipFrontend -SkipSelfcheck   # 只重打包后端

# 1) 跑起来（开发模式，最常用）
.\build\venv\Scripts\python.exe main.py            # 开窗口
.\build\venv\Scripts\python.exe main.py --browser  # 用系统浏览器
.\build\venv\Scripts\python.exe main.py --server --port 8899   # 只起服务（自动化测试用）
.\build\venv\Scripts\python.exe main.py --check    # 环境自检（13 项）

# 2) 自检（改动后必跑；离线跑很快）
.\build\venv\Scripts\python.exe scripts\selfcheck.py --offline
.\build\venv\Scripts\python.exe scripts\selfcheck.py            # 完整版：含 OpenSCAD 编译 / STL 分析 / 打包
.\build\venv\Scripts\python.exe scripts\selfcheck_design.py     # 设计规格与改动机制（144 项）
```

> **AI Key**：存在 Windows 凭据管理器里（`config.py`，后端名 `credential_manager`），
> **绝不写进仓库**。`scripts/check_no_secrets.py` 会在自检里扫一遍。
> 真实 AI 调用需要额度；余额不足时链路会明确报"DeepSeek 账户余额不足"，不会假装成功。

---

## 3. 架构地图

### 后端（`src/aiforge/`，Python 3.13）

| 模块 | 职责（一句话） | 改它要小心什么 |
|---|---|---|
| `jobs.py` | **作业主流程**：生成 / 修改 / 参数微调 / 几何修复 / 收尾落盘；阶段与消息推送 | 2540+ 行，是全项目的脊柱；改它必跑完整自检 |
| `changes.py` | **改动契约**：把一句话解析成"只允许改哪些参数"，并核对改动是否越权 | 判据都带"实测踩过"的注释，别凭直觉简化 |
| `design.py` | 设计规格（SpecItem / 别名表 `SPEC_ALIASES` / 归一化 `normalize_name`） | 别名表动一个词，可能影响很多匹配路径 |
| `llm.py` | DeepSeek 调用与提示词（生成 / 修改 / 修复 / 复核 / 打印性评审 / 外观复核） | 提示词里写死的契约必须与后端判据一致 |
| `scad.py` | SCAD 源码安全校验、参数提取与改写、注释元数据（`extract_param_labels`） | `apply_params` 只改顶层纯数值赋值，别扩成"智能改写" |
| `compiler.py` | 调 OpenSCAD 编译 STL、诊断解析、`$fn` 注入、固定零件库搜索路径、**报错→改法翻译** | 超时/静默丢几何的处理都不能去掉 |
| `mesh.py` | STL 实测分析：封闭性、连通件数、腔体孤岛、射线法壁厚、支撑估算、螺纹识别 | 所有"实测"结论都来自这里，是判据的事实来源 |
| `envcheck.py` | 环境体检：`--check` 跑 13 项，界面“系统诊断”另有分组（7 组，含 API 实测与自动修复入口） | 新增检查要同时进 `CHECK_IDS`；两边数字不是一份，文档里不写定数 |
| `threemf.py` | 从**已交付的 STL** 转 3MF（纯标准库） | 不能改成“重跑一次 OpenSCAD”：那会让 3MF 与用户下载的那个 STL 面数不一致 |
| `slicers.py` | 切片软件检测与直送（注册表 → 常见目录 → 手动指定） | `detached` 必须问 `procguard.in_job(pid)`，不能看自己设了什么标志；快照清理必须 `keep=刚交出去那一份` |
| `projects.py` | 项目持久化、历史版本归档、删除（进 `.deleted`） | 归档时机很关键（见 README-开发者.md §72） |
| `parts/` | **固定零件库**（`aiforge_parts.scad` + catalog）：螺纹转接环等已验证零件 | 零件的生成器是实测标定过的（见 README-开发者.md §71） |
| `server.py` / `api/*` | FastAPI 服务、路由、SSE 事件流 | 接口改动必须同步 `docs/API-CONTRACT.md` |
| `procguard.py` | 子进程守护（关软件连带结束 OpenSCAD），**所有入口都要装** | 漏一个入口就会出现"关掉了 OpenSCAD 还在跑" |

### 前端（`webui/src/`，React 18 + TS + Vite + Three.js）

| 位置 | 职责 |
|---|---|
| `pages/Studio.tsx` | 工作台：对话 / 进度 / 结果卡片 / 历史与回退 / 删除确认 |
| `viewer/Viewer3D.tsx` | Three.js 预览（平滑着色、自动适配、线框、自动旋转、尺寸浮层） |
| `components/SpecCard.tsx` | **需求确认卡片**（可编辑数字 + "就是这样，开始设计"）—— 生成前的人工关卡 |
| `components/ProjectSidebar.tsx` | 项目列表 / 搜索 / 重命名 / 删除入口 |
| `hooks/useProjects.ts`、`api/client.ts` | 数据与接口层（接口形状以 `docs/API-CONTRACT.md` 为准） |
| `scripts/*.mjs` | Playwright 界面验收与审计（见 §5） |

---

## 4. 核心链路与**不许破坏**的不变量

```
自然语言 → llm.parse_design_spec → SpecCard 人工确认 → llm.generate_from_spec
        → scad.validate → compiler 编译 → mesh.analyze（实测几何）
        → 几何完整性判据 → AI 打印性评审 → 外观复核 → projects 落盘（含归档）
修改：instruction → build_change_contract（授权范围）→ llm.modify_with_contract
        → verify_change（程序化核对）+ AI 复核 → 不通过则重做/确定性兜底 → 取舍 → 重新编译与检测
```

被反复验证过、**不能为了"简化"而去掉**的判据（每一条都对应真实缺陷）：

1. **改动必须落到实测几何上**：不看 AI 的自我陈述，只看编译后的 STL 实测值。
2. **"该改的没改"和"越权改了别处"都要报**，且取舍时按代价比较（`ChangeVerdict.penalty`），
   "完全没改"也参与比较 —— 不允许它靠"无害"白占便宜。
3. **规格要跟着改动走**：授权过的目标值要写回规格，否则下一轮会拿旧规格把模型改回去。
4. **用户点名的整体尺寸必须核对实测值**（含圆件的外径 = 水平最大尺寸；超出 50% 也要报）。
5. **整体缩放是相对改动**：按比例改尺寸参数，数量/角度类不跟着变；有确定性兜底。
6. **几何修复失败必须保留原模型**，并如实说明；绝不允许"改提示文字假装修好"。
7. **子进程守护在所有入口生效**；**归档发生在作业开始时**（否则 `revisions/rNNN` 会错位）。

---

## 5. 测试与验收：每个脚本证明什么

| 脚本 | 需要什么 | 证明什么 | 期望结果 |
|---|---|---|---|
| `scripts/selfcheck.py --offline` | 无 | 94 项中跳 15 项（需真实环境）后剩下的全部：模块/路径/SCAD 处理/契约语义/改名桥/缩放/回退/零件库/线程/编码/密钥/外观材质/切片检测规则/接口面/预算/构建脚本形状/模块内未定义名字/高 DPI 工作区 | 79 通过 / 15 跳过 / 0 失败 |
| `scripts/selfcheck.py` | OpenSCAD | 加上真实编译、STL 分析、打包检查、路由契约、**无参数 GET 逐条发真请求**等（共 94 项） | 94 通过 / 0 失败 |
| `scripts/selfcheck_design.py` | 无 | 设计规格与改动机制的 144 项细判据 | 全部通过 |
| `webui/scripts/verify-flow.mjs` | mock 后端（`--first-run` **新起**）+ vite | 界面全流程：欢迎向导 → 工作台 → 需求确认 → 结果卡片 → 预览（含尺寸标注）→ 线框开关 → 设置（页面动画、液态玻璃 10 条、送到切片软件）→ 缺 WebView2 的回退横幅 4 条 → 溢出审计 | 83/83，且 console/pageErrors/failedRequests 全空（2026-10-12 复跑） |
| `webui/scripts/verify-glass-contrast.mjs` | 同上 | 玻璃上的字还读不读得清：逐像素量文字对比度 / 底纹泄漏 / 浮层分界度。写进注释与文档的玻璃数字必须能在报告里找到 | `CONTRAST_OK`（不达标就非零退出） |
| `webui/scripts/layout-audit.mjs` / `audit-interface.mjs` | 真实后端托管 `webui/dist`（默认 8770）或 vite | 布局溢出/遮挡；对比度、焦点、命名、颜色单载、tabular-nums | 0 问题 + `INTERFACE_AUDIT_OK`（audit 必须含一个真实项目路由 `#/studio/<id>`；2026-10-13 在真后端上复跑：10 组 0 问题，项目路由 tabular 候选 7 个、仅颜色 59、无焦点 25 全部 0 违规） |
| `scripts/verify_modify_matrix.py` | **真实 AI + OpenSCAD** | 端到端自然语言修改：改高度 / 加通孔 / 改孔径 / 加厚底座 / 整体缩放 / 历史回退，每一步都用 STL 实测判定 | 逐项 OK（见 §7 现状） |
| `scripts/verify_parts.py` | OpenSCAD | 固定零件库每个零件能编译且封闭、尺寸正确 | 3 个零件共 6 个用例全通过（6/6，含升口径与内外螺纹位置） |
| `scripts/e2e_test.py` + `scripts/verify_e2e_evidence.py` | 真实 AI | 全链路端到端与"证据"校验（防伪造） | 通过 |
| `scripts/test_install.ps1` | 无 | 安装 / 跑起来 / **真双开唤窗取证（2b 段）** / 覆盖安装 / 失败回滚 / 不破坏用户配置 | 54 通过 / 0 失败（会真装机，跑完自动卸） |
| `scripts/check_no_secrets.py` / `check_ps_encoding.py` / `check_oss_files.py` | 无 | 敏感信息、PowerShell 纯 ASCII、开源文件完整性 | NO_SECRET_FOUND 等 |
| `scripts/probes/README.md` | 视脚本而定 | 一次性诊断工具（每个对应一个真实缺陷；判据固化成守卫后就该删） | 见该目录说明 |

**惯例（请保持）**：
* **阴性对照**：给某个修复写自检时，同时写一条"把修复拆掉就必须变红"的断言 ——
  否则无法区分"判据真的在起作用"还是"判据被放松了"。`t_name_bridge`、`t_contract_intent` 都是这么写的。
* **不删测试、不放宽阈值、不伪造通过**。断言失败先分清"代码坏了"还是"这一版模型没做好"（README-开发者.md §59）。

---

## 6. 1.0.1 修了什么（改动集中在改动契约链路）

| 症状（用户视角） | 根因 | 修在哪 | 守卫 |
|---|---|---|---|
| 说改高度，模型纹丝不动，界面报成功 | 规格名 `height` 与源码参数名 `total_h` 之间没有桥：正确的改动被同时判"没改到"+"越权"，取舍时输给"什么都没改" | `changes.resolve_param_name` + `scad.extract_param_labels`（注释标签桥 + 结尾缩写） | `t_name_bridge` |
| 改小尺寸会被自己的检查改回去 | 规格永远停在最初那版 | 授权目标值写回规格 + 判据按新目标值判 | `selfcheck_design` |
| "增加一个直径10mm的圆孔"却把外径从 80 改成 90 | 裸别名"直径"命中外径 + "增加"被当相对增量 | 方位词必须一起出现；"新增特征"句里的数字归属新特征 | `t_contract_intent` |
| "底座加厚到6mm"完全没生效 | 句子里没有"厚度"二字，只有动词"加厚" | 动词→维度提示（加厚/加高…）+ 方位必须完全对上 | `t_contract_intent` |
| "整体缩小到80%"静默无操作 | 缩放没有"目标参数值"，确定性兜底对它是空转；且授权名单没落到源码参数名 | `build_user_scaled_source` + 契约缩放分支补参数名 | `t_user_scale` |
| 回退历史版本拿到的不是那一版 | 归档发生在收尾阶段（编号错位）+ 根本没有恢复接口 | 作业开始时归档；新增 `POST /api/projects/{id}/restore` | `t_restore_revision` |
| 圆件直径从不校验 | 直径被当成"内部尺寸"跳过 | 外径按水平最大尺寸核对 + 超 50% 报问题 | `selfcheck` 尺寸判据段 |
| 打包版 `--check` 退出码 1 且无输出 | `--noconsole` 打包后 `stdout` 为 `None`，`print()` 崩 | `main._ensure_console_streams()` + 报告落地文件 | `t_console_streams` |
| CGAL 编译报错自动修复两轮修不动 | 只把原始 assert 丢给 AI | `compiler.explain_failure()` 把报错翻成可执行改法 | `t_compile_advice` |

细节与实测数据：`README-开发者.md` §72 / §73 / §74，`CHANGELOG.md` 1.0.1 段。

1.0.2 与 1.0.3 不在这张表里重复一遍：前者看 `CHANGELOG.md` 对应段，后者（按「小而美」收账、
单实例锁与“这是谁”的判活/身份、反臃肿守卫、为什么不做面数预算、构建脚本被一句 pip 警告打死）
也看 `CHANGELOG.md` 的 1.0.3 段；其中唤窗/判活事故与 `.ps1` 的 stderr 终止坑这两条教训成文在
`README-开发者.md` §76 / §77，对应的可复跑验证是守卫 `t_single_instance` 与 `t_ps1_native_stderr`。

---

## 7. 交接时的验证状态（诚实版）

**已实测通过（最近一次复跑：2026-10-11，本轮改动后）：**
* `selfcheck.py --offline`：**79 通过 / 15 跳过 / 0 失败**
* `selfcheck.py`（完整，含 OpenSCAD 编译 / STL 分析 / 打包检查 / 路由契约 / 真实 HTTP 逐条跑）：
  **94 通过 / 0 失败**
* `selfcheck_design.py`：**全部通过**（144 项设计机制判据）
* `scripts/verify_parts.py`：固定零件库 **3 个零件共 6 个用例**全绿（含升口径与内外螺纹位置、镂空断言）
* `scripts/check_oss_files.py` / `check_no_secrets.py` / `check_ps_encoding.py`：全绿
  （无密钥扫描覆盖 **182 个文本文件**、编码/本机路径扫描 181 个提交文本文件，无 BOM / 乱码；
  扫描口径：中文路径必须用 `git -c core.quotepath=false ls-files` 取，否则两份中文名 README
  会被转义成八进制串而从来没进过扫描）
* `scripts/test_install.ps1`：**54 通过 / 0 失败**，其中 2b 段是**只有真装机才能验**的双开唤窗取证
  （跑在最终发包上；包体 SHA-256 不写在这里 —— `CHANGELOG.md` 会被打包进安装包，把校验号写进
  被校验的那个东西里，写进去的那一刻号就变了；要核对就对眼前的包自己算：
  `Get-FileHash installer\Output\AI-3D-Forge-Setup.exe -Algorithm SHA256`）
* 打包产物：免安装目录 **38.9 MB（216 文件，exe 本体 7,497,972 字节≈ 7.2 MB，onedir）**、
  安装包 **16.4 MB**；
  打包版 `--check` **13/13 通过（overall=ok）**；真启动服务后 `/api/app/health`、`/api/projects`、
  `/api/printers/selected`、`/`、`/api/openapi.json` 全部 200；关掉后无残留进程
* 契约语义矩阵（离线、真实项目源码 + 12 条指令）：**12/12**
* 端到端修改矩阵：`scripts/verify_modify_matrix.py` 在 1.0.1/1.0.2/1.0.3 代码上跑过的子集全绿
  （glm 21/21）；全矩阵受免费额度限制，可用 `AIFORGE_MATRIX_STEPS` 指定步骤

> 本小节故意不再把原始日志写成 `build\work\*.txt`：`build/` 整目录是忽略的构建产物，
> 新 clone 的人根本没有那些文件 —— 拿它当铁证就是文档骗人（本轮已把它定性为缺陷并删过一次）。
> 本机现场可以拿 `scripts/` 里的同名命令重跑出来。

**本轮拿不到的证据（不口头保证）：**
* 弹窗（MessageBox）的正向效果：自检里不能真弹模态框（会卡住流水线），所以它只能验到
  「该不该弹」那个决策函数（四条件真值表），弹出来长什么样未验。
* `SetForegroundWindow` 能不能成功不是产品说了算：Windows 有前台切换策略，同一条装机验收在
  1.0.3 的每次发包上都跑过，得到过**两种结果**（`woke=False` 与 `woke=True`），所以判据里
  **故意不钉它**。（原始跑动日志在 gitignored 的 `build\work\` 里，新 clone 的人根本没有那些
  文件，所以这里既不写次数也不抄现场；能复跑的就是 `scripts/test_install.ps1` 的 2b 段。）
  能钉住的只有：`SW_RESTORE` 确实把窗口还原了（`isIconic before=True after=False`）、
  以及被拒那次没有静默退出。把其中任何一个当成“产品必须保证”的，都会被下一次跑动打脸。

**上一版留给接手方补跑、本轮已经跑完的（保留跑法，下次改动照做）：**
1. `scripts/verify_modify_matrix.py` —— 已在 1.0.1/1.0.2/1.0.3 代码上跑过子集全绿（glm 21/21）；
   全矩阵受免费档额度限制，可用 `AIFORGE_MATRIX_STEPS` 指定步骤。
2. 界面流程验收 `webui/scripts/verify-flow.mjs` —— 2026-10-12 在新起的 mock + vite 上复跑 **83/83 全绿**
   （console / pageErrors / failedRequests 均为 0）。那 83 条里比旧基线多的一条是「预览线框开关」：
   它验的是刚加载的模型也能切线框（上一轮实测到 `Viewer3D.tsx` 的 effect 在异步加载完成前就跑完，
   新模型一律用默认值渲染）。同一天 `verify-glass-contrast.mjs` 也复跑到 `CONTRAST_OK`。
   复现要注意两件事：mock 必须用 `--first-run` **新起**（否则第一条「首次启动跳向导」就红）；
   地址栏也不能直接用 127.0.0.1 —— Vite 默认只听 `::1`，脚本默认的 BASE 已改成 `http://localhost:5273`。
   跑法：`node webui\mock-server.mjs --first-run`（新起才有效）+ `npx vite --port 5273` +
   `$env:BASE_URL='http://localhost:5273'; node scripts\verify-flow.mjs`。
3. `scripts/test_install.ps1` —— 2026-10-13 在**本轮重新打的 1.0.3 安装包**上跑完 **54 通过 / 0 失败**
   （1.0.2 时是 46 项）：2b 段真双开唤窗拿到 `woke=True restored=True`，覆盖安装后用户配置存活，
   卸载后用户项目与配置都还在，跑完自动卸载、不留进程。
   **每次发布后必须补验**：2b 段只有装到本机才验得到，代码改不动它坏没坏。
   两个环境前提（实测）：① 打包链需要先有 `build\dist\AI-3D-Forge`（跑 `.\scripts\build.ps1`）；
   ② 本机若没有 Inno Setup，`build_installer.ps1` 会**联网自动获取**到用户目录（属于往本机装东西，
   动手前要用户同意）。
4. 真后端界面审计（2026-10-13 跑通，跑法记在这里，别再当成「本机做不了」）：先把数据目录
   指到沙箱，**不拿真实用户项目做审计**：
   `$env:AIFORGE_DATA_DIR='build\work\audit-data'`、`$env:AIFORGE_LOCAL_DIR='build\work\audit-local'`、
   `$env:AIFORGE_PROJECTS_DIR='build\work\audit-projects'` → `build\venv\Scripts\python.exe -X utf8 scripts\probes\make_audit_project.py`
   造一个**真编译出来的**项目（否则 `tabular-nums` 永远 0 个候选）→ `… main.py --server --port 8770`
   → `node webui\scripts\layout-audit.mjs http://127.0.0.1:8770` 与 `…audit-interface.mjs …`。
   接口路径是 `/health` 与 `/version`（没有 `/api/` 前缀），探错了会得到 404 而误报成缺陷。
5. 安装包在本机还打不出来：`ISCC.exe` 不在 `C:/D: 的 Inno Setup 6` 任何常见路径，
   `build_installer.ps1` 会**联网自动获取** Inno Setup（属于往本机装东西的动作，需用户同意）。
   只验打包链本身可以跑 `scripts\build.ps1 -SkipInstaller`（PyInstaller + 冻结版冒烟）。

---

## 8. 已知问题与建议的下一步（按优先级）

| 优先级 | 问题 | 建议做法 |
|---|---|---|
| ~~P0~~ 已做（1.0.2） | 特征尺寸（孔径）已独立复测：`mesh.measure_hole_diameter` + `hole_profile` 已并入，作为“孔真的改了”的判据（自检「孔径射线实测」「孔径改动判据」） | —— |
| 部分已做（1.0.2） | 生成质量随机性：已有几何完整性兜底（散件换路径重生成、薄膜检测、逐轴尺寸校正、零件库确定性直出）；CGAL 变体清单仍可继扩 | 继续扩充 `compiler.explain_failure` 改法清单 |
| ~~P1~~ 已做（1.0.2） | 端到端矩阵与界面流程已跑绿（矩阵子集 21/21、verify-flow 47/47） | 后续改动后照例重跑 |
| P1 | "同一维度、方位词不同"的匹配偏保守（如"这个孔的内径"配不上"顶部圆孔直径"） | 现在会退化成"结构改动"由 AI 按原话处理（安全但弱）；可用"维度 + 方位不冲突"分级放宽，并保留阴性对照 |
| P2 | 历史/项目：删除只能进 `.deleted` 手动恢复，界面没有回收站 | 要么加"回收站"页（列/恢复/彻底删除），要么保持现状文案（已改成如实说明） |
| ~~P2~~ 已做（1.0.2） | 多轮对话上下文：`DesignSpec.to_prompt()` 已全量注入 clarifications（“用户此前补充说明过的要求”），生成/修改/评审三条链路共享；自检「修改提示词携带补充说明」含阴性对照 | 长对话压测仍可补 |
| P3 | 预览大模型性能 | 已有 CreasedNormals/线框开关；超大 STL 可加进度与"降采样预览" |

---

## 9. 环境陷阱与硬规矩（踩过才会信）

1. **所有 `.ps1` 必须纯 ASCII**（含注释）：PowerShell 5.1 按 GBK 解析 BOM-less 脚本，
   中文行尾字节可能被读成续行符，语句**静默消失**，脚本半路停住且不报错。改完跑 `scripts\check_ps_encoding.py`。
2. **不要用 `Get-Content | Set-Content` 改文件**：会引入 BOM/乱码。用编辑器或 Python 读写 UTF-8。
3. `Select-Object -Last N` 会让原生命令出现**假 exit 1**（管道提前关闭），别据此判断失败。
4. **`TestClient` 跑不了长作业**（事件循环关闭会取消任务）：测长流程要 `server.start_server()` + httpx。
5. **测试数据隔离**：设 `AIFORGE_DATA_DIR` / `AIFORGE_PROJECTS_DIR` 指到 `build\work\...`；
   **不要**覆盖 `AIFORGE_LOCAL_DIR`（里面是本机已装的 OpenSCAD，覆盖后直接报"没有找到 OpenSCAD"）。
6. **不要动用户真实数据**（`%USERPROFILE%\Documents\AI-3D-Forge`）。诊断只读。
7. **API Key 绝不入库**：`secrets/`、`*.local.env`、`.env` 已在 `.gitignore`；提交前跑 `check_no_secrets.py`。
8. `build/` 全目录都是生成物（含 300MB 级临时数据），已在 `.gitignore`；可以直接删，`build.ps1` 能重建。
9. 关软件后**不应有** OpenSCAD/AI-3D-Forge 残留进程；有残留就是 `procguard` 的某个入口漏装了。
10. 判据只能来自**实测**（STL/编译结果），不能来自"AI 说它改好了"。
11. **`scripts/*.py` 里只要打印中文，文件开头必须 `import console_utf8`，而且比第一个 `print` 先**。
    中文 Windows 的控制台是 GBK/936，Python 到 3.13 都不会默认启用 UTF-8 模式，一个「−」（U+2212）
    就能把自检中途弄死（连累退出码与汇总一起丢）。实现只有 `src/aiforge/stdio.py` 一份，
    **不要在 `main.py` 或脚本里再写一份自己的标准流处理**；自检里有 AST 守卫会查。
12. **验证工具自身的缺陷算产品缺陷**。实测踩到：对比度脚本把裁切框里远处的亮色控件当成
    「字底下的底」，把完全实底的基线量成 3.14:1 —— 照着这个读数改产品就会为了一个假问题把整套
    材质做废。尺子换了口径，旧文档里的“修前”数字就必须在新口径下重取（`NO_FLOAT_FLOOR=1` 对照模式），
    而不是继续拄旧数字。
13. **界面验收全跑在 mock 上，所以它永远看不见后端的 500**。实例：手册承诺的「一键安装」在真后端里
    一点就 500（`NameError: asyncio`），而 `verify-flow.mjs` 82 步全绿。对策已固化：`selfcheck.py` 用
    `TestClient` **真发 HTTP**（无参数 GET 逐条跑 + 安装端点打桩跑一次），不拿 mock 结果当后端证据。
14. **投毒验守卫要改对地方**：把 `router` 上的 endpoint 函数换成抛异常的 **弄不红**「GET 逐条跑」——
    FastAPI 在执行装饰器时就把函数绑走了；得打桩处理函数**运行时才调的那个属性**。验不红的守卫比没守卫更危险。
15. **PowerShell 重定向会把中文弄成乱码**：`cmd *> log.txt` 写出的文件里中文不可搜（GBK 写、UTF-8 读），
    `Select-String -Pattern '中文'` 也匹配不到。要看日志里的数字：用编辑器按行号读，或给 Python 加 `-X utf8`。
16. **Windows 上拿 `os.kill(pid, 0)` “探一下存活”是错的**：`signal.CTRL_C_EVENT == 0`，那是在发 Ctrl+C
    （同控制台时死的是谁说不准：一次打死调用者自己、一次打死目标；而对已退出的 pid 不报错、
    回答“还活着”）。判活只用 `procguard.process_alive()`，判“是不是我们”只用
    `single_instance._owner_is_ours()`；在锁文件 pid 与验收脚本上是同一个教训
    （教训成文见 `README-开发者.md` §76）。
17. **`.ps1` 里不要手写 `2>&1`**：`$ErrorActionPreference` 为 `'Stop'` 时，原生命令只往 stderr 写一行
    就能**终止整个脚本**（命令 `exit 0` 也一样），报出来的是 `NativeCommandError` 而不是工具自己的话；
    终止后读 `$LASTEXITCODE` 也读不到真值（5 次采样：-1 四次、真 0 一次）。要调原生命令就用
    `build.ps1` 的 `Invoke-Native`（或在调用点显式降 `'Continue'`）；守卫 `t_ps1_native_stderr`
    盯着这一条，教训成文见 `README-开发者.md` §77。同一个坑本仓写过两次注释都没拦住第三次。
18. **凡是用 `git ls-files` 枚举「会进仓库的文件」，必须加 `-c core.quotepath=false`**：
    git 默认把非 ASCII 路径转义成 `"\\346\\226\\207..."`，还原出来的 Path 在磁盘上不存在，
    `is_file()` 为假 → **中文名文件被静默跳过**。实测：本仓库的 `README-开发者.md`（188 KB）
    与 `README-用户.md` 从来没进过密钥扫描集合，而报告照样打「已扫描 180 个文本文件」、
    结论照样是 `NO_SECRET_FOUND`。守卫 `t_secret_scanner_not_blind` 现在双向盯着
    （阳性：中文名文件必须在集合内；阴性：八进制转义路径不得被当成文件）。
    同理，`.gitignore` 是白名单式的，靠人维护 —— 媒体类文件与大文件另有一道路径守卫兜着。

---

## 10. 交付 / 发布

```powershell
.\scripts\build.ps1                     # → build\dist\AI-3D-Forge\（免安装）+ installer\Output\AI-3D-Forge-Setup.exe
.\scripts\export_source.ps1             # → build\source\AI-3D-Forge-Source\（源码包，排除 build/、密钥、截图）
.\scripts\make_release.ps1              # → build\release\（发布目录 + 校验和）
.\build\venv\Scripts\python.exe scripts\verify_delivery.py   # 发布物完整性/一致性检查
```
版本号**只有一个来源**：`src/aiforge/version.py` 的 `APP_VERSION`。`scripts/build_installer.ps1` 读它并以
`/DAppVersion=` 传给 Inno Setup；`.iss` 里 `#ifndef AppVersion` 的兜底值是一个故意写错的占位号
`0.0.0-hand-compile`（手编时一眼看出不对），**升版不要动它**。“两边各写一个版本号”已经漂过一次，
现在由自检的「安装器元数据」用例钉住（含三条阴性对照）。

**源码包（`AI-3D-Forge-Source.zip`）**是“纯源码”，2026-09-22 重跑导出实测 **1.64 MB / 188 个条目**：
它**不含** `build/`（venv、OpenSCAD、图标、打包产物都在里面）。因此：

* 解开后的第一次 `.\scripts\build.ps1` 会自己建 venv、装依赖、找/装 OpenSCAD、生成图标；
  在那之前，少数"依赖构建产物"的自检项（图标资源、安装器元数据、依赖 OpenSCAD 的项）**本来就会失败**，
  这不是包坏了；
* 两个卫生检查（源码编码 / 无本机路径）靠 `git ls-files` 取文件清单，
  在**不是 git 仓库**的裸目录里会报"只扫到 0 个文本文件"——先 `git init` 再跑即可；
* 导出脚本自带三道检查：**密钥扫描 / 完整性（35 个必需文件 + 2 份中文指南）/ 卫生
  （除 `docs\images\` 外不得有截图、`*-report.json`、`_tmp_*` 调试产物）**，
  任何一道不过就直接拒绝出包（本次交接就是靠第三道拦下了 `webui\scripts\shots\` 的漏出）。

---

## 11. 接手验收：怎么算"接手成功"

按顺序做完这几条，就算接稳了（预计 1–2 小时，含一次真实 AI 调用）：

1. `.\scripts\build.ps1` 成功产出安装包并打出 `BUILD_COMPLETE`；自检输出就在它自己那一段
   （`5/8 Backend self-check`）里，要留档自己重定向（`*> build\work\build.log`）；
2. `scripts\selfcheck.py --offline` = **79 通过 / 15 跳过 / 0 失败**（完整版：**94 通过 / 0 失败**）；
   `selfcheck_design.py` 全通过（包在「设计规格与改动机制」那一项里，**144 项**）；
3. 改一个**小**东西（例如提示词里一句措辞），跑通"改 → 自检 → 打包"闭环；
4. 用真实 Key 跑一次 `scripts/verify_modify_matrix.py`，按 §7 的说明把它跑到全绿；
5. 用 `webui/scripts/verify-flow.mjs` 跑到全绿；
6. 打开 `README-开发者.md` §75–§77 读一遍最近三个缺陷，能对着代码说出"判据在哪、守卫是哪条自检"。

> **发布前必须先补的一步（本轮实测到的状态，不是猜测）**：`git log -1` 显示 HEAD 还停在
> 2026-09-17 那一颗提交上，1.0.2/1.0.3 两轮的产品修复、守卫与发包口径**全部只在工作区**
> （`git status` 89 处改动 + 19 个未跟踪文件，其中 6 个是产品模块：`stdio` / `dpi` /
> `slicers` / `threemf` / `reqtests` / `api/slicer_routes`）。
> 这些未跟踪文件**不算缺陷**：`docs/PUBLISH-TO-GITHUB.md` 的提交步骤写的就是 `git add -A`，
> 而且 `README-开发者.md` “发布前”清单里本来就有一项「克隆冒烟：把提交集导出/克隆到项目之外，
> 用它自己的 `scripts/selfcheck.py` 跑一遍全绿」 —— 所以这里刻意**不**
> 为“漏 add”再加一条守卫（为一个已经被流程挡住的风险养一条检查，就是本轮一直在删的那类东西）。
> 要提交的时刻请用 `git add -A`，别用 `git commit -am`（后者不会带上未跟踪文件，那样推上去的
> 仓库 `import aiforge.stdio` 直接 ModuleNotFoundError）。

有问题优先看：`%LOCALAPPDATA%\AI-3D-Forge\logs\aiforge.log`（运行日志）、
`%LOCALAPPDATA%\AI-3D-Forge\logs\check-report.txt`（环境自检报告副本）、
`build\work\smoke-check.txt` 与 `smoke-version.txt`（`build.ps1` 冒烟阶段真的会写这两个）。
`build\work\` 里其余东西都是一次性 scratch，**会被整目录清掉** —— 结论不要写在里面
（上一轮就是因为文档指着它，新 clone 的人根本拿不到，已当缺陷处过一次）。

---

## 12. 交接后增量（2026-09-17：智谱 GLM 支持与几何质量兜底）

接手后围绕「glm-4.7-flash 支持 + 持续优化」做的改动，均带自检与真实链路验证：

- **GLM 提供商支持**：`llm.detect_provider` 按 base_url 分流；视觉复核用 `glm-4v-flash`
  且 max_tokens 按智谱硬上限 1024；思考模型默认 `thinking.type=disabled`（实测 glm-4.7-flash
  思考吃满 8192/16384 导致正文为空）；429/5xx 瞬态退避重试；错误文案提供商化；
  设置页「服务商预设」一键切换（模型下拉联动）。浏览器实测：预设切换、测试连接成功。
  跑法：隔离配置里把 `api.base_url` 设为 `https://open.bigmodel.cn/api/paas/v4`、
  `api.model` 设为 `glm-4.7-flash`，Key 用环境变量 `GLM_API_KEY`（SecretStore 已支持）；
  注意凭据管理器优先级最高，自动化测试需设 `PYTHON_KEYRING_BACKEND` 屏蔽或先清 Key。
- **修改矩阵子集模式**：`AIFORGE_MATRIX_STEPS="改高度,整体缩放"`；glm 子集 **21/21**。
- **几何质量兜底**：零厚度薄膜（问题 9，p05<0.05mm）、点名尺寸逐轴校正
  （契约目标优先、偏差≤2% 不修、倍率 0.5~2.0 外不修、采纳以实测偏差更小为门）、
  散件换路径重生成、完整性 rank 扩到 6 元组。
- **修复**：`_ai_second_opinion` 漏绑 job 导致 modify 崩溃；安全校验误拦 `module shell()`
  （本地已定义同名 module/function 放行，报错带行号+片段）；设置页测试连接重复耗时。
- **验证基线更新**：`selfcheck.py` **63 通过 / 0 失败**；`verify-flow.mjs` **47/47**；
  glm 真实链路 create/modify 端到端水密单件、尺寸精确。
