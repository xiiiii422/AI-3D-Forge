# AI 3D Forge —— 前端 / 后端接口契约（v1）

> 本文件是前端与后端之间**唯一**的接口依据。任何一方改动接口，必须同步修改本文件。
> 后端实现：`src/aiforge/api/*.py`；前端实现：`webui/src/**`。

## 0. 基本约定

| 项目 | 约定 |
|---|---|
| 服务地址 | `http://127.0.0.1:<port>`，端口由后端动态选择（默认从 8760 起找空闲端口） |
| 前端资源 | 生产构建产物 `webui/dist/` 由后端在 `/` 静态托管；开发期 Vite 用 proxy 指向后端 |
| 数据格式 | 除下载接口外，全部为 JSON，字段名统一 `snake_case` |
| 字符编码 | UTF-8（后端返回 `application/json; charset=utf-8`） |
| 错误格式 | 非 2xx 一律返回 `{"detail": "<中文可读信息>", "code": "<机器码>", "hint": "<可选：用户该怎么做>"}` |
| 身份认证 | 无。仅监听 127.0.0.1 回环地址；后端校验 `Host` 与 `Origin` 必须是本机 |

### 0.1 通用状态对象

```ts
/** 单项检查的统一结构 */
interface CheckItem {
  /** 稳定的机器码，前端用它决定「自动修复」按钮的行为 */
  id: CheckId;
  /** OpenAI/Apple 风格的极简中文标题，如 "OpenSCAD" */
  title: string;
  /** 状态：ok=通过 / warn=可用但需注意 / fail=不可用 / checking=检测中 */
  status: "ok" | "warn" | "fail" | "checking";
  /** 一句话人话结论，如 "OpenSCAD 2021.01 已就绪" */
  detail: string;
  /** 一句话告诉用户「需要我做什么」，通过时为 null */
  action: string | null;
  /** 是否可由软件自动修复（决定是否显示 [自动修复] 按钮） */
  fixable: boolean;
  /** 补充信息，仅用于「系统诊断」展开面板，可为 null */
  meta: Record<string, unknown> | null;
}

type CheckId =
  | "windows"        // Windows 版本
  | "runtime"        // 软件运行环境（打包后的 Python 运行时）
  | "python"         // Python 运行环境
  | "python_version" // Python 版本是否满足要求
  | "openscad"       // OpenSCAD 是否安装
  | "openscad_cli"   // OpenSCAD CLI 能否实际跑通
  | "openscad_version" // OpenSCAD 版本
  | "deps"           // 必要 Python 依赖
  | "project_dir"    // 项目目录权限
  | "network"        // 网络连接（DeepSeek / OpenSCAD 官方源）
  | "api"            // DeepSeek API 配置状态
  | "api_live"       // DeepSeek API 实际可用性（联网测试）
  | "temp"           // 本地临时目录可读写
  | "stl_preview"    // Three.js / WebGL 预览能力
  | "mesh_engine";   // 3D 模型引擎（STL 解析/分析）
```

### 0.2 环境检测报告

```ts
interface EnvReport {
  /** 报告生成时间（ISO8601，本地时区） */
  checked_at: string;
  /** 总体结论 */
  overall: "ok" | "warn" | "fail";
  /** ok 的数量 / 总数，用于「环境就绪 9/10」 */
  ok_count: number;
  total: number;
  /** 是否已完成首次向导（决定是否显示欢迎向导） */
  first_run: boolean;
  items: CheckItem[];
}

interface VersionInfo {
  app: string;           // "1.0.0"
  build: string;         // 构建号，如 "2025.01.15-a1b2c3d"
  app_full: string;      // "AI 3D Forge 1.0.0"
  python: string;        // 运行环境版本，如 "3.13.14 (bundled)"
  python_bundled: boolean; // true=用户机器无需安装 Python
  openscad: string | null; // 如 "2021.01"，未安装为 null
  os: string;            // "Windows 11 专业工作站版 (build 26200)"
}
```

---

## 1. 环境与诊断

### `GET /api/env/report`
返回 `EnvReport`。首屏与向导使用；`?live=1` 时额外做联网的 DeepSeek 可用性测试（较慢，默认不测）。

### `GET /api/env/version`
返回 `VersionInfo`。

### `GET /api/diagnostics`
「系统诊断中心」数据，按分组返回：

```ts
interface DiagnosticsGroup {
  /** 分组标题："AI 服务" / "3D 引擎" / "模型生成" / "STL 预览" / "文件系统" / "网络" */
  group: string;
  items: CheckItem[];
}
interface DiagnosticsResponse {
  checked_at: string;
  overall: "ok" | "warn" | "fail";
  groups: DiagnosticsGroup[];
}
```

### `POST /api/diagnostics/fix`
请求：`{ "id": CheckId }`。执行自动修复（例如：写默认配置、重建目录、重测 CLI、重下 OpenSCAD）。
返回：`{ "ok": boolean, "message": string, "report": EnvReport }`（修复后立即重测并回传最新报告）。

### `POST /api/env/detect`
强制重新检测（忽略缓存）。返回 `EnvReport`。

---

## 2. OpenSCAD

### `GET /api/openscad/detect`
```ts
interface OpenScadInfo {
  found: boolean;
  path: string | null;        // openscad.exe 绝对路径
  version: string | null;     // "2021.01"
  source: "config" | "path" | "common" | "registry" | "user" | null;
  candidates: { path: string; version: string | null }[]; // 扫描到的所有可用副本
  cli_ok: boolean;            // 实际执行 --version 是否成功
  detail: string;
}
```

### `POST /api/openscad/locate`
请求：`{ "path": "D:\\OpenSCAD\\openscad.exe" }`（用户手动选择）。
后端校验后写入用户配置，返回 `OpenScadInfo`。校验失败返回 400 + `{detail, hint}`。

### `POST /api/openscad/browse`
弹出 Windows 文件选择对话框（需桌面会话；WebView 模式下可用）。
返回：`{ "path": string | null }`。若无桌面会话返回 501。

### `POST /api/openscad/install`
启动自动安装。请求：
```ts
{ "confirm": true, "method": "auto" | "installer" | "portable" }
```
- `auto`：优先静默安装；需要管理员权限时自动提权（弹 UAC）；失败则回退免安装绿色版。
- `installer`：运行官方安装程序（会弹 UAC）。
- `portable`：下载官方 zip 解压到 `%LOCALAPPDATA%\AI-3D-Forge\runtime\openscad\`（**无需管理员**，最省 C 盘的做法是解压到用户目录，但若检测到 D 盘可用会优先用 D 盘缓存目录）。

返回 `202` + `{ "job_id": string }`，随后用 SSE 订阅进度（见 2.1）。
若已在安装中，返回 `409`。

### 2.1 安装/检测进度流（SSE）
### `GET /api/openscad/install/{job_id}/stream`
`text/event-stream`，事件序列：

```
event: progress
data: {"stage":"download","percent":37.5,"message":"正在从官方源下载 OpenSCAD…","received":8214528,"total":21876824}

event: log
data: {"message":"已校验文件完整性（SHA-256 匹配官方发布值）"}

event: done
data: {"ok":true,"info":{...OpenScadInfo},"message":"OpenSCAD 已安装并配置完成。"}

event: error
data: {"ok":false,"message":"需要管理员权限才能安装到 Program Files。","need_admin":true,"manual_url":"https://openscad.org/downloads.html","manual_steps":["..."]}
```

`stage` 取值：`prepare` | `download` | `verify` | `install` | `locate` | `cli_test` | `config` | `done`。
前端必须同时支持 `done` 与 `error`，并在 `error` 时展示 `manual_url` + `manual_steps`（第四节「安装说明」）。

### `POST /api/openscad/test`
运行一次真实 CLI 冒烟测试（生成一个 10mm 立方体并转 STL）。
返回：`{ "ok": boolean, "message": string, "duration_ms": number, "diagnostics": string | null }`。

---

## 3. 生成 / 修改（核心流程）

### 3.1 创建作业
### `POST /api/jobs`

```ts
interface CreateJobRequest {
  /** 自然语言需求："设计一个桌面手机支架，高度100mm。" */
  prompt: string;
  /** 续改时传入已有项目 id；新建时为 null */
  project_id?: string | null;
  /** 续改时传入修改指令，例如："高度改成150mm。" */
  instruction?: string | null;
  /** 模式：create=从零设计 / modify=在现有 SCAD 上修改 */
  mode?: "create" | "modify";
  /** 可选：模型名，缺省由 AI 生成 */
  name?: string | null;
}

interface JobRef {
  job_id: string;
  project_id: string;
  /** 该项目的版本号（每次成功生成 +1） */
  revision: number;
}
```
返回 `202` + `JobRef`。

**同一项目同时只能有一个作业（`409` + `code=BUSY`）**：两个作业会并发地对同一个
项目目录「归档上一版 → 写 model.scad → 存 meta」，而它们读到的 `revision` 相同 ——
互相覆盖、静默丢模型。`/api/jobs`、`/api/projects/{id}/params`、`/api/compile`
三个入口共用这条守卫。

唯一的例外是**无人监听的「等待确认」作业**：作业停在确认卡片上、而且没有任何界面
订阅它的 SSE（用户刷新页面 / 切走项目 / 关了窗口），说明它已经被丢下，会一直挂到
进程结束。这种作业会被**就地取消**（`status` 变 `cancelled`）并放行新任务，
否则用户会撞上「卡片没了、停止按钮也没有」的死锁。
判据是「有没有人监听」（`Job.has_watcher`），**不是**等了多久 ——
用户去接杯水回来还要点确认，不该把他的作业取消掉。

### 3.2 作业状态（轮询兜底）
### `GET /api/jobs` → `{ "jobs": Job[] }`
最近 20 个作业快照（新的在前），可选 `?project_id=` 只看一个项目。
界面刷新 / 切走再回来时用它找回还在跑的作业 —— 没有这个列表，进度条会凭空消失，
用户以为软件把任务丢了（所以它不是“没人调的后台接口”，`webui/src/api/client.ts` 真的在调）。

### `GET /api/jobs/{job_id}`
```ts
type JobStage =
  | "queued" | "thinking" | "understanding" | "awaiting_confirmation"
  | "generating_scad" | "validating_scad"
  | "compiling" | "analyzing" | "reviewing" | "verifying"
  | "exporting" | "done" | "error" | "cancelled";

/** awaiting_confirmation 是**非终态**：作业在服务端等用户确认规格 */
type JobStatus = "running" | "awaiting_confirmation" | "done" | "error" | "cancelled";

interface JobMessage {
  seq: number;
  /** 面向用户的一句话（中文、无技术黑话） */
  text: string;
  /** 可选：该条消息对应的技术明细，仅诊断面板展示 */
  detail?: string | null;
}

interface Job {
  job_id: string;
  project_id: string;
  revision: number;
  status: JobStatus;
  stage: JobStage;
  /** 0-100，用于顶部细进度条 */
  percent: number;
  messages: JobMessage[];
  error: { message: string; hint: string | null; code: string } | null;
  result: JobResult | null;
  /**
   * 最近一次公布的规格。等待确认期间必须带上 ——
   * 用户在这个状态刷新页面时，界面只能靠首帧快照重建确认卡片。
   */
  spec?: SpecEventPayload | null;
  created_at: string;
  finished_at: string | null;
}

interface JobResult {
  project_id: string;
  revision: number;
  name: string;
  /** 生成得到的参数化 SCAD 源码 */
  scad: string;
  /**
   * AI 归纳的参数表，用于「参数微调」UI。
   * `group` 是**可选**的类别标签：后端目前不产出，缺省时由前端按变量名/说明归类
   * （唯一一份规则：`webui/src/lib/paramGroups.ts`）。
   */
  params: {
    name: string; label: string; value: number; unit: string;
    min: number | null; max: number | null; group?: string | null;
  }[];
  /** 一句话设计说明 */
  summary: string;
  stl: {
    /** 相对下载地址，直接用于 <a download> 或 fetch */
    url: string;
    filename: string;
    size_bytes: number;
    triangles: number;
    volume_mm3: number;
    bbox_mm: { x: number; y: number; z: number };
    watertight: boolean;
    watertight_ratio: number; // 0-1
    /**
     * **实体等效**重量（按 100% 填充算），也就是用量上限。
     * 不要直接展示成「实际用量」：实测的桌面垃圾桶实体等效 191g，
     * 按「2 层墙 + 15% 填充」只有约 111g。要展示给用户的是 `usage`。
     */
    mass_g: number | null;
    /** 耗材用量估算（模型 + 支撑，含区间、线材长度与假设说明） */
    usage: UsageInfo | null;
    /**
     * 模型由几个互不相连的实体组成。正常应当为 1。
     * 封闭内腔不算零件（判据：内腔面有向体积为负且被外壳包围盒完全包住）。
     */
    solid_parts: number;
    /** 除主体外还有几个独立零件（>0 表示「一个成品被做成了几件」） */
    stray_parts: number;
    /**
     * 腔体孤岛：水平截面上「被空腔完全包围的材料块」出现的高度。
     * 非空表示容器内腔里长出了不贴内壁的材料 —— 典型成因是外壁加强筋
     * 没贴合外形轮廓、从外壁穿进了腔体。模型仍是 1 个连通实体，
     * 所以 solid_parts 检查看不到它，必须靠这项。
     */
    cavity_islands: { z: number; count: number }[];
    /** 射线法实测的壁厚分布（min / p05 / 中位数 + 最薄处坐标） */
    wall: {
      min_mm: number; p05_mm: number; median_mm: number; samples: number;
      thinnest: { location: number[]; thickness_mm: number }[];
    } | null;
    /** 支撑量估算（偏大的上界：只用于方向之间的比较，不是切片精确值） */
    support: {
      angle_limit_deg: number; support_volume_mm3: number; support_area_mm2: number;
      ratio_pct: number; sampled: boolean; method: string;
    } | null;
    /** 支撑占比偏高时给出的「更省支撑的摆放方向」（六个轴向中最优的一个） */
    orientation_hint: {
      orientation: string; label: string;
      support_volume_mm3: number; support_ratio_pct: number; build_height_mm: number;
    } | null;
    /** 耗材用量估算（模型 + 支撑） */
    usage: {
      material: string; density_g_cm3: number;
      model_solid_cm3: number;      // 实体等效（100% 填充）
      model_printed_cm3: number;    // 按外壳 + 填充估算
      model_grams: number; support_cm3: number; support_grams: number;
      total_grams: number;          // 下限（按填充估算）
      total_grams_upper: number;    // 上限（实体等效），备料用这个
      filament_length_m: number; support_ratio_pct: number;
      filament_diameter_mm: number;
      assumptions: { wall_lines: number; line_width_mm: number; infill_pct: number };
      notes: string[];              // 估算依据与不确定性，必须展示
    } | null;
  } | null;
  /** AI 打印性检查结论 */
  printability: {
    score: number;              // 0-100
    verdict: "pass" | "warn" | "fail";
    summary: string;
    issues: { severity: "info" | "warn" | "error"; title: string; detail: string; suggestion: string }[];
    metrics_note: string;
    /**
     * 外观复核结论（多模态模型看渲染图后的原文）。
     * 第一行是机器可读的「结论：有问题」或「结论：无明显问题」；
     * 只有前者才算发现问题，空串/缺字段表示「没有结论」——**不得当成没问题**。
     * 渲染或复核失败时该字段缺省，不影响模型本身。
     */
    visual_review?: string;
  } | null;
  /** 编译/校验等技术信息，折叠展示 */
  diagnostics: string | null;
  /** 本次生成依据的设计契约 */
  spec?: DesignSpec | null;
  /** 修改类作业的改动核对结论；新建时为 null */
  change_verdict?: ChangeVerdict | null;
  /** 改动核对结论的人话版本 */
  change_summary?: string | null;
}
```

### 3.3 作业实时流（推荐，用于打字机效果）
### `GET /api/jobs/{job_id}/stream`
`text/event-stream`：
```
event: message
data: {"seq":3,"text":"正在设计结构…","detail":null,"kind":"info"}

event: stage
data: {"stage":"compiling","percent":62,"text":"正在生成 3D 模型…"}

event: spec
data: {"spec":{...DesignSpec 展示结构...},"awaiting_confirmation":true}

event: done
data: { ...完整 Job 快照，status="done"，result 为 JobResult }

event: error
data: {"message":"…","hint":"…","code":"OPENSCAD_FAILED"}

event: cancelled
data: { ...完整 Job 快照，status="cancelled" }
```

**事件载荷约定（后端实现以此为准）**
- 首帧：`event: snapshot` + 完整 Job 快照，立刻推送当前已有状态（用于断线重连补齐）。
- `message` / `stage`：增量推送。
- `done` / `cancelled`：**推完整 Job 快照**（含 `status`、`result`、`error`、`messages`），
  而不是只推 `JobResult`。前端据此判定结束并读取 `result`。
- `spec`：推送设计规格（`{spec, awaiting_confirmation}`）。用户确认后若规格被改动，
  会再推一次同样的 `spec` 事件（此时 `awaiting_confirmation=false`），界面据此把卡片收成只读版。
- `error`：推 `{message, hint, code}` 三件套。
- 快照里的 `created_at` / `finished_at` 是 **ISO8601 字符串**（可为 null），与 `Job` 类型一致。

> **终止事件只有三个：`done` / `error` / `cancelled`，且前端必须逐一处理。**
> 少处理一个的后果不是「少一条提示」，而是**界面永久卡在「生成中」**：
> 作业状态到不了终态 → 发送键一直禁用 → 用户除了刷新页面没有别的出路。
> 实测踩过：`cancelled` 漏了分支，用户点「停止生成」后再也发不出消息。
> `scripts/selfcheck.py::t_sse_contract` 会断言这三个事件在前端都有处理分支；
> `webui/scripts/spec-card-audit.mjs` 会真的点一次停止并确认发送键恢复可点。

### `POST /api/jobs/{job_id}/cancel` → `{ "ok": true }`
`running` 与 `awaiting_confirmation` 都可取消；后者会先唤醒挂起的规格等待，避免任务泄漏。

### 3.3.1 确认规格
### `POST /api/jobs/{job_id}/confirm-spec` → `{ "ok": true }`
请求：
```ts
{
  /** 传回（可修改过的）规格；null / 省略表示「就按你理解的做」 */
  spec?: DesignSpec | null;
  /** 补充说明，并入契约的澄清记录，之后每轮都不会忘 */
  note?: string | null;
}
```
作业不处于 `awaiting_confirmation` 时返回 **409**（不能重复确认）。
确认后作业重新进入 `running`，按确认过的规格开始设计。

### 3B. 设计规格（DesignSpec）与改动核对（ChangeVerdict）
```ts
interface SpecItem {
  name: string;
  label: string;
  value: number | null;
  unit: string;
  min: number | null;
  max: number | null;
  /** 用户亲口指定的值：后续修改不得被擅自改掉 */
  user_specified: boolean;
  /** 后端拼好的一句话，直接展示 */
  display: string;
}

interface DesignSpec {
  kind: string;
  summary: string;
  specs: SpecItem[];
  features: string[];
  prohibitions: string[];
  acceptance: string[];
  /** 用户没说清、AI 按常见做法先定的点 */
  open_questions: string[];
  /** 用户历轮补充的说明（累积，只增不减） */
  clarifications: string[];
  /** 只由「用户指定的数值 + 禁止项 + 品类」算出的稳定指纹（12 位 hex） */
  fingerprint: string;
}

interface SpecEventPayload {
  spec: DesignSpec;
  awaiting_confirmation: boolean;
}

interface ChangeVerdict {
  ok: boolean;
  /** 用户要求的改动是否真的生效 */
  performed: boolean;
  /** 未授权却被改动的参数名 */
  unauthorized: string[];
  /** 用户此前指定的指标是否仍被满足 */
  spec_intact: boolean;
  /** 设计指纹是否保持不变 */
  fingerprint_kept: boolean;
  reasons: string[];
  /** 目标值未达成的相对偏离（参数名 → 偏离比例） */
  target_misses: Record<string, number>;
  /** 参数值站不住（例如 height=150 但模型最大尺寸只有 38mm） */
  implausible: string[];
  /** AI 复核的一句话结论；未复核时为 null */
  ai_summary: string | null;
  /** AI 复核提出的疑点 */
  ai_concerns: string[];
}
```

> 契约目的：把「我理解成了什么」和「这次到底改了什么」变成可核对的事实，
> 而不是让 AI 自称改好了。
>
> **双重校验**（改动类作业）：
> 1. **程序化核对**（确定性、不消耗额度）：参数级 diff —— 请求的改动是否生效、
>    未授权改动、规格是否仍被满足、指纹是否保留、参数与几何是否自洽；
> 2. **AI 复核**（一次调用，温度 0）：把改动前后的两份代码一起给它，
>    专门看「参数全对但结构被改坏」这类程序看不出的问题
>    （底座改窄到站不稳、承力处被削薄、支撑改成悬空）。
>
> 合并规则是**不对称**的：两道都通过才算通过；AI 提出明确异议即判不通过并重做一次；
> **AI 拿不到结论（网络失败 / JSON 异常）时不影响程序化结论** ——
> 绝不因为「AI 没回话」就把一个干净的改动判成失败。
>
> 实现要点（改动这两块前务必先读）：
> - 规格里的参数名与源码里的参数名**经常不一致**（`height` vs `stand_height`），
>   目标值必须经 `changes.resolve_param_name()` 解析成源码真实存在的参数名，
>   否则「把目标值写回源码」的确定性兜底会静默失效。
> - 两个候选都不达标时，先试「只把授权参数的目标值写回原源码」，再谈取舍；
>   取舍按 `ChangeVerdict.penalty()`，其中 `unauthorized` 的权重必须高于
>   「没改到位」（否则会选中「改坏别处」的版本）。

### 3.4 参数微调（可选快捷路径，不调用 AI）
### `POST /api/projects/{project_id}/params`
请求：`{ "params": { "height": 150 } }`
后端把值注入 SCAD 顶部 `PARAMS` 变量区并重新编译（不调用 AI，秒级返回）。
返回 `202` + `JobRef`（复用同一套进度流）。

> 参数表在 UI 里分栏展示：整体尺寸 / 孔与螺纹 / 壁厚与强度 / 圆角与配合 / 数量与阵列 / 其它参数
> （参数 ≥5 个且确实跨类才分栏，少的时候保持平铺）。**归类是纯展示逻辑，只在前端一份**
> （`webui/src/lib/paramGroups.ts`），后端不给类别，因此历史项目立刻受益。
> 如果哪天要让后端直接给，填 `group`（取上面那几个 id）即可覆盖关键词判断。

### 3.5 直接编译 SCAD（高级 / 调试）
### `POST /api/compile`
请求：`{ "scad": "<源码>", "project_id": "<可选>", "name": "<可选>" }` → `202` + `JobRef`。

---

## 3A. 打印机配置与知识库

> 打印机的选择会**真正影响** AI 生成（尺寸/壁厚/最小特征/`$fn`）、
> 几何检查（薄壁、成型范围）与 AI 打印性评审。切换机型后重新生成即可得到针对该机的模型。

### `GET /api/printers`
```ts
interface PrinterConstraints {
  min_wall_mm: number;              // 竖直外墙可靠壁厚（3× 喷嘴）
  min_hole_dia_mm: number;          // 最小孔径
  min_hole_dia_vertical_mm: number; // 竖直孔最小直径
  min_protrusion_mm: number;        // 最小凸起/浮雕
  clearance_mm: number;             // 活动件间隙
  max_layer_mm: number;
  default_layer_mm: number;
  max_horizontal_span_mm: number;   // 最大桥接
  max_overhang_deg: number;
  max_build_x: number; max_build_y: number; max_build_z: number;
  max_build_diagonal_mm: number;
  fn: number;                       // 参考 $fn
  rough_min_dim_mm: number;         // 最小可成型尺寸
  notes: string[];                  // 这台机器/材料需要提醒的事
}

interface PrinterProfile {
  id: string;
  name: string;
  manufacturer: string;
  nozzle_mm: number;                // 0 = 光固化（无喷嘴）
  build_x: number; build_y: number; build_z: number;
  build_volume_mm: { x: number; y: number; z: number; volume_cm3: number };
  nozzle_options: number[];
  layer_min_mm: number; layer_max_mm: number;
  max_speed_mm_s: number; max_accel_mm_s2: number; max_flow_mm3_s: number;
  has_heated_bed: boolean; max_bed_temp_c: number; max_hotend_temp_c: number;
  enclosure: boolean;               // 封闭腔体（决定能否用 ABS/ASA/PA）
  materials: string[];
  filament_diameter_mm: number;
  extruder: 'direct' | 'bowden' | 'resin';
  bed_leveling: 'manual' | 'abl' | 'auto';
  bed_surface: string;
  kinematics: 'cartesian' | 'corexy' | 'delta' | 'bedslinger' | 'resin';
  recommended_max_overhang_deg: number;
  recommended_max_bridge_mm: number;
  dimensional_tolerance_mm: number;
  notes: string;
  builtin: boolean;                 // true = 内置机型（不可删，可被用户覆盖修改）
  sources: string;                  // 数据来源说明（官方规格 / 经验值）
  updated_at: string;
  constraints: PrinterConstraints;  // 由机型 + 材料推导得到
  checklist: string[];              // 上面这些约束的人话版本，直接展示
  recommended_fn_examples: { '50mm': number; '150mm': number };
}

interface PrintersResponse {
  selected_id: string;
  selected_material: string;
  default_id: string;
  materials: string[];              // 所有机型可用材料的并集
  printers: PrinterProfile[];
}
```

### `GET /api/printers/selected`
```ts
{
  printer: PrinterProfile;
  material: string;
  material_options: string[];
  note: string;
}
```

### `POST /api/printers/select`
请求 `{ printer_id: string, material?: string }` → `{ printer: PrinterProfile, material: string }`
同时会写入 `GET /api/settings` 的 `printer_id` / `printer_name` / `printer_material`。

### `GET /api/printers/{printer_id}` → `PrinterProfile`（不存在返回 404）

### `POST /api/printers`（新建自定义机型）→ `201` + `PrinterProfile`
### `PUT /api/printers/{printer_id}`（部分更新）→ `PrinterProfile`
### `DELETE /api/printers/{printer_id}` → `{ ok: true, message }`
内置机型返回 `400`（提示可「覆盖修改」或「新建」）。

请求体字段均可选：`name` `manufacturer` `nozzle_mm` `build_x/y/z` `nozzle_options`
`layer_min_mm` `layer_max_mm` `max_speed_mm_s` `max_accel_mm_s2` `max_flow_mm3_s`
`has_heated_bed` `max_bed_temp_c` `max_hotend_temp_c` `enclosure` `materials`
`filament_diameter_mm` `extruder` `bed_leveling` `bed_surface` `kinematics`
`recommended_max_overhang_deg` `recommended_max_bridge_mm` `dimensional_tolerance_mm`
`notes` `sources`

### `POST /api/printers/preview`
编辑机型时**实时预览**这套参数意味着什么（不保存）。
请求只需 `nozzle_mm` `build_x/y/z` `material` `enclosure` 等子集。
→ `{ constraints: PrinterConstraints, checklist: string[], notes: string[] }`

### `POST /api/printers/reset` → 清空用户自定义，恢复内置库

### `GET /api/printers/knowledge/summary`
知识库自述：`{ builtin_count, features: [{key,label,affects}], material_density_g_cm3, material_heat_deflection_c }`
前端「打印机」设置页可用它解释「每项参数到底影响什么」。

### 生成结果里的打印机上下文（`JobResult.printer`）
```ts
printer: {
  id: string; name: string; manufacturer: string;
  nozzle_mm: number; material: string;
  build_volume_mm: { x: number; y: number; z: number };
  enclosure: boolean;
  is_resin: boolean;
  constraints: PrinterConstraints;
  checklist: string[];
}
```
`JobResult.stl` 额外包含：`material` `material_density` `within_build_volume`
`min_dimension_mm` `meets_min_wall`。
`JobResult.printability` 里也会有同一份 `printer` 对象。

---

## 4. 项目

### `GET /api/projects` → `ProjectSummary[]`
```ts
interface ProjectSummary {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
  revision: number;
  thumbnail_url: string | null;   // 后端生成的 PNG 预览
  stl_url: string | null;
  summary: string | null;
  /** 最近一次是否编译成功 */
  last_status: "ok" | "error" | "unknown";
}
```

### `POST /api/projects` → 请求 `{ "name": string }` → `ProjectSummary`
### `GET /api/projects/{id}` → `ProjectDetail`
```ts
interface ProjectDetail extends ProjectSummary {
  scad: string;
  params: JobResult["params"];
  history: { revision: number; prompt: string | null; instruction: string | null; created_at: string; summary: string }[];
  files: { scad_path: string; stl_path: string | null; export_dir: string | null };
}
```
### `PATCH /api/projects/{id}` → `{ "name": string }` → `ProjectSummary`
### `DELETE /api/projects/{id}` → `{ "ok": true }`（移入回收站而非彻底删除）

### `POST /api/projects/{id}/save` → 请求 `{ "kind"?: "stl" | "scad" | "3mf" }`（默认 `stl`）
弹系统「另存为」框，把当前文件写到用户选的位置。返回三态，**不靠“没报错”当“已保存”**：
- `{ "status": "saved", "path": string }`；
- `{ "status": "canceled" }`（用户点了取消，界面不提示成功也不报错）；
- `{ "status": "no-dialog" }`（无桌面会话，前端自己退回浏览器下载）。
还没有对应文件时返回 `409`。

### `POST /api/projects/{id}/restore` → 请求 `{ "revision": number }`
回退到历史版本。**回退本身产生新的一版**（`revision+1`），历史只追加不重写；
回退前先把当前版归档。归档里必须**同时**有 `.scad` 与 `.stl`：
只有源码没 STL → `409` + `NOT_FOUND`（hint 指向「保留历史 STL」开关）；整个归档不存在 → `404`。
返回 `{ "project_id", "restored_from", "revision", "metrics" }`（metrics 是回退后重新实测的，不沿用旧数字）。

### 下载
- `GET /api/projects/{id}/stl` → `application/sla`，`Content-Disposition: attachment; filename="<name>.stl"`
- `GET /api/projects/{id}/scad` → `text/plain; charset=utf-8`，附件下载
- `GET /api/projects/{id}/stl/raw` → 同样的 STL 但 `inline`（供 Three.js `STLLoader` 直接 fetch）
- `GET /api/projects/{id}/thumbnail` → `image/png`

---

## 4A. 送到切片软件

对标同类产品的“一键送切片器”。刻意只有两个接口：
默认目标与手动路径表都写进 `PUT /api/settings`，不另开入口 —— 少一个接口就少一处会坏的地方。
真机往返（进程表里确有 pid、命令行里带着我们的 3MF、`detached` 问得出来）不靠文档口头保证，
由 `scripts/probes/probe_slicer_roundtrip.py` 跑（这个目录的 README 列了它需要的外部条件）。

### `GET /api/slicers`
已检测到的切片软件。`?rescan=1` 强制重扫（用户刚装完切片软件时要用）。
```ts
interface SlicerEntry {
  id: string;               // "bambu_studio" | "orcaslicer" | …（识别表里的稳定 id）
  name: string;             // "Bambu Studio"
  exe: string;              // 主程序绝对路径
  version: string | null;   // "02.08.02.61"，取不到为 null
  source_label: string;     // 人话来源：“已安装程序” / “常见安装目录” / “手动指定”
  running: boolean;         // 进程表里已经开着
}
interface SlicersResponse {
  slicers: SlicerEntry[];      // 排序 = 识别表顺序（实测跑过的排前面）
  default_id: string | null;   // 默认目标；软件被卸载后回到 null，不会留下幽灵 id
  count: number;
  recognized_names: string[];  // 我们认得哪些：没检测到时界面要能告诉用户“装这些里任意一个就行”
}
```

**只存 id、不存路径**：路径每次从注册表 / 安装目录实扫 —— 切片软件升级换了目录也不会指着旧路启动。

### `POST /api/slicers/pick`
自动检测漏网时让用户自己指出主程序（弹原生选择框，只收 `.exe`），存好后立即重扫。
返回 `{ "status": "added" | "canceled" | "nodialog", "path"?: string, ...SlicersResponse }`。

### `GET /api/projects/{id}/3mf` → `model/3mf` 附件
3MF 里声明毫米单位并带上机型 / 材料 / 尺寸 / 版本元数据，切片软件不必再猜尺寸。
旧项目只有 STL 时现场从 STL 转一份（几何完全一致）并留下来，下次直接取。

### `POST /api/projects/{id}/send-to-slicer` → 请求 `{ "slicer"?: string }`
把当前模型交给切片软件打开。不传 `slicer` 就用设置里的默认，没有默认就用检测到的第一个。
**只认 id 不认路径**：能启动的程序必须是本机检测出来的那几个 exe，请求里塞任何路径都无效
（否则这个接口就变成了“本机任意程序执行器”）。
```ts
interface SendToSlicerResult {
  status: "started" | "forwarded" | "failed";
  detail: string;            // 一句话给人看的结果
  pid: number;
  exit_code: number | null;  // started 时为 null
  detached: boolean;         // true = 不在本软件“一死就杀光”的作业里，关软件不会连带杀掉它
  slicer: string;
  slicer_name: string;
  file: string;              // 交出去的那份快照路径
  exe: string;
  kind: "3mf" | "stl";       // 一律优先 3MF，只有 3MF 确实写不出来才退回 STL
  source_label: string;
  note?: string;             // 只在 detached=false 时出现：退出本软件可能连带关掉它
}
```
- `detached` **不是“设了个标志就算”**：由 `procguard` 问内核（该 pid 还在不在我们那个作业里）得出，
  问不出来按 `false` 处理并附 `note`。曾经的事故：代码里句句“我们设了 `CREATE_BREAKAWAY_FROM_JOB`”，
  接口也报 `detached: true`，但用户关掉本软件时 Bambu Studio 跟着没了（祖先作业会让该标志失效）。
- `forwarded`：切片软件本来就开着，新进程把文件交给它的现有窗口后自己退掉（退出码 0）；
  这条无法再确认导入，界面按“已交给现有窗口”如实说。
- 快照文件名 `<项目名>-r<版本>.3mf`，落在数据目录的 `slicer/` 下，每个项目只留最近 5 份。
  **刚交出去那一份绝不能被清理删掉**（真的发生过：`shutil.copy2` 把源文件的旧 mtime 带给每个快照，
  快照时间全同 → “留最新 5 份”退化成“按目录序留前 5 份” → 第 6 次之后切片软件报“文件不存在”）。
  现在写出后立刻 `os.utime` 打上当下时间，并由 `_prune(keep=…)` 显式保护那一份。
- 错误：`404` 项目不存在；`409 OPENSCAD_FAILED` 还没生成模型；`409 SLICER_NOT_FOUND`
  点名或默认的目标当前没检测到（hint 指向「设置 → 切片软件」里的手动指定）。

---

## 5. 设置

### `GET /api/settings`
```ts
interface Settings {
  api_key_set: boolean;
  /** 仅返回掩码，如 "sk-6862****3164"；后端永不回传完整 Key */
  api_key_masked: string | null;
  api_key_store: "credential_manager" | "config_file" | "env";
  base_url: string;          // 默认 "https://api.deepseek.com"
  model: string;             // 默认 "deepseek-chat"
  openscad_path: string | null;
  data_dir: string;          // %APPDATA%\AI-3D-Forge
  projects_dir: string;      // %USERPROFILE%\Documents\AI-3D-Forge\Projects
  /** 日志目录（默认 %LOCALAPPDATA%\AI-3D-Forge\logs，可被 AIFORGE_LOCAL_DIR 改）。
   *  它**不在** data_dir 里：设置页要告诉用户日志在哪，就只能发真实路径。 */
  log_dir: string;
  keep_stl_history: boolean;
  auto_install_openscad: boolean;
  first_run_done: boolean;
  theme: "light" | "dark" | "system";
  /** 界面材质：实底（普通）/ 液态玻璃。默认 "solid" */
  appearance: "solid" | "glass";
  /** 玻璃透明度百分比，0 = 完全不透明。默认 25，硬上限 60（见下）。
   *  只作用于「页面内」的面板；弹窗/菜单/提示条这类浮层始终是厚玻璃，不随它变透 */
  glass_transparency: number;
  /** 契约 §3A：当前目标机型 */
  printer_id?: string;
  printer_name?: string;
  printer_material?: string;
  /** 生成前是否在对话里说明目标机型 */
  announce_in_chat: boolean;
  /** 生成前是否先让用户确认「我对需求的理解」（规格卡片） */
  confirm_spec: boolean;
  /** 送到切片软件的默认目标 id（只存 id，见 §4A）；没设过为 null */
  default_slicer: string | null;
  /** 用户手动指出的切片软件主程序路径（自动检测漏网时的逃生口） */
  slicer_custom_paths: string[];
}
```

### `PUT /api/settings`（部分更新，字段均可选）
- 传 `api_key` 时写入凭据管理器；传 `api_key: ""` 表示清除。
- `confirm_spec` / `announce_in_chat` 对应界面「设置 → 行为」里的两个开关。
- `default_slicer`：**必须是当下真能检测到的 id**，否则 `409` + `SLICER_NOT_FOUND`。
  存一个“将来才会出现的 id”没意义，而且软件被卸载后它会变成幽灵默认值（点“送到切片软件”永远失败）。
  传 `""` 表示取消默认（回到“用检测到的第一个”）。
- `slicer_custom_paths` 是**整表替换**（不是追加），后端去掉空串与重复项后写入，并立即作废检测缓存。
- `appearance` 只接受 `solid` / `glass`，**其它值一律忽略**（保持原值，不报错也不写坏配置）。
- `glass_transparency` 由后端**硬夹取到 0–60**，非数字输入回到默认 25；界面滑块的上限就是这两个数，
  前后端各存一份（`src/aiforge/config.py` 与 `webui/src/hooks/useAppearance.ts`），改一处必须同步
  （`scripts/selfcheck.py` 的「外观材质与玻璃透明度」用例会把两边常量、夹取语义、非法值行为全部对一遍）。
- 上限的实测依据（口径：`webui/scripts/verify-glass-contrast.mjs`，逐像素量「字 vs 字底下的底」，
  底只取**字形外圈 3–11px**（剔 2px 抗锯齿晕圈），而不是整个裁切框；数据在它跑出的
  `webui/scripts/shots/contrast-report.json` 里 —— 那是生成物、不入库，要拿数字得自己跑一遍）。
  注意这三句话彼此不同，不要混用：
  1. **不是**「超过 60% 文字就不达标」：普通面板（卡片/侧栏/工作台）上的文字到 90% 仍 ≥ 4.5:1（最低 5.07:1）。
  2. 随透明度单调恶化的是**底纹泄漏**（面板底亮度散布）：0（实底）→ 0.072（60%）→ 0.094（90%）。
     再往右面板不再是一块干净的底 —— 这是观感判断，不是无障碍判断。
  3. 会真的掉到 4.5:1 以下的是**浮层**：背后压着整页文字时，拿掉下限复测（`NO_FLOAT_FLOOR=1`）
     透明度 25% → 4.14:1，60% → 1.84:1（并且分界度塌到 1.03，与背后页面基本糊在一起）。
     所以浮层在 CSS 里另有一层不透明度下限（`tokens.css` 的 `--glass-solid-float: max(94%, var(--glass-solid))`），
     加下限后实测 4.78~4.86:1（浅）/ 7.16~7.23:1（深）且**不随滑块变化**；这条下限由 `scripts/selfcheck.py` 做源码守卫，
     由 `verify-glass-contrast.mjs` 做像素验收（弹窗浅色/深色、菜单、预览浮层）。
- 返回更新后的 `Settings`。

### `POST /api/settings/api-key/test`
请求 `{ "api_key"?: string }`（缺省用已存 Key）。真实调用 DeepSeek 验证 Key 是否可用。

- **未配置任何 Key 时**：返回 `400` + `{detail, code: "NO_API_KEY", hint}`。
  这是「前置条件不满足」，不是「测试失败」，前端据此显示「去配置 AI 服务」。
- **已配置但不可用时**：返回 `200` + `{ "ok": false, "message": "...", "hint": "..." }`。
- **可用**：返回 `200` + `{ "ok": true, "message": "...", "model": "...", "latency_ms": 123 }`。

### `POST /api/settings/first-run/complete` → `{ "ok": true }`

---

## 5A. 需求核对表（requirement_tests）

生成/修改作业完成后，后端把设计契约编译成**逐条可执行判定**（`src/aiforge/reqtests.py`），
随结果与项目指标一起返回；口径对齐 CADTests（arXiv 2605.07807）：

```jsonc
"requirement_tests": {
  "pass_rate": 1.0,          // PR：整份需求是否全部达成（有未达成/没测出来即 0）
  "requirement_score": 1.0,  // RS：达成需求组 / 全部需求组
  "invalid_ratio": 0.0,      // IR：没能判定的组占比（invalid 计失败，不算通过）
  "counts": { "pass": 10, "fail": 0, "invalid": 0, "manual": 3 },
  "groups": 10, "manual": 3,
  "tests": [{
    "group": "dim:height",           // 需求组：同组内任一断言不过，该条需求算未达成
    "requirement": "总高（Z 方向/高度）",
    "kind": "dimension",             // dimension|hole|thread|integrity|print|semantic
    "status": "pass",                // pass|fail|invalid|manual
    "expected": "100mm", "actual": "99.8mm",
    "deviation_pct": -0.2, "tolerance_mm": 2.0,   // 公差 = max(规格公差, 机型配合公差, 2%)
    "fix_hint": "…", "advisory": false            // advisory=工艺建议：进清单但不参与 PR/RS
  }]
}
```

- 出现位置：`GET /api/jobs/{id}` 的 `result.requirement_tests`、
  `GET /api/projects/{id}/metrics` 的 `requirement_tests`（项目重开时从缓存带回）。
- `manual` 是**语义需求**（外观/结构是否合理），程序不打分，界面标成「需确认」。
- 未达成项会连同实测数字回喂给几何修复链路（`【需求核对表】` 段）。

## 6. 应用控制

- `POST /api/app/open-folder` 请求 `{ "kind": "data" | "projects" | "exports" | "project", "project_id"?: string }` → `{ "ok": true }`（在资源管理器中打开）
- `POST /api/app/open-external` 请求 `{ "url": string }` → `{ "ok": true }`（仅允许 https + 域名白名单：
  DeepSeek/OpenSCAD 官网、GitHub，以及底部作者入口的 douyin.com / bilibili.com；
  原生窗口会拦截新窗口，所以界面里的外链一律走本接口，失败时前端才退到 `window.open`）
- `GET /api/app/health` → `{ "ok": true, "version": "1.0.0", "uptime_s": 12.3 }`
- `POST /api/app/shutdown` → `{ "ok": true }`（退出程序，前端「退出」按钮使用）
- `GET /api/app/window-runtime` → 原生窗口组件（WebView2）状态：
  ```ts
  interface WindowRuntimeStatus {
    installed: boolean;
    version: string | null;      // "153.0.4234.48"
    source: string | null;       // registry | filesystem | none
    detail: string;
    install_hint: string;
    download_url: string;        // 微软官方下载页
    bootstrapper_url: string;    // 常青版一键安装包直达链接
  }
  ```
- `POST /api/app/window-runtime/install` → `{ "ok": boolean, "message": string }`
  一键安装（winget 优先，失败回退官方 Bootstrapper）。
  上面两个接口只在「被浏览器打开」时用：后端缺 WebView2 时会改开系统浏览器，并在地址上带
  `?browser_fallback=1`（`src/aiforge/server.py`），前端据此在页面顶部说明原因并给一键安装入口
  （用户手册 docs/USER_GUIDE.md §2.2 承诺的行为；原生窗口里这条横幅一行也不渲染、一次请求也不多发）。

> 另外 `GET /api/docs` 与 `GET /api/openapi.json` 是 FastAPI 自带的自省页，只给开发排查用，
> 界面不调、也不属于本契约；除了这两个，本文件必须覆盖每一条 `/api` 路由
> （`scripts/selfcheck.py` 的「接口面不臃肿」守卫会三向核对：路由 ↔ 本文件 ↔ 前端调用）。

---

## 7. 前端页面与路由

| 路由 | 页面 | 说明 |
|---|---|---|
| `/` | 重定向 | `first_run=true` → `/welcome`，否则 → `/studio` |
| `/welcome` | 首次启动向导 | 4 步：欢迎 → 环境检测 → OpenSCAD（缺失则自动安装）→ DeepSeek API Key |
| `/studio` | 主工作台 | 左：项目列表；中：Three.js 预览；右：对话/修改；顶部：环境就绪条 |
| `/studio/:projectId` | 主工作台（已打开项目） | 同上 |
| `/settings` | 设置 | API、模型、路径、数据目录 |
| `/diagnostics` | 系统诊断 | 分组自检 + [自动修复] |
| `/about` | 关于 | 版本信息、开源许可、常见问题入口 |

### 7.1 设计风格强制要求（OpenAI / Apple / Xiaomi 气质）

- **底色**：`--bg: #FFFFFF`（浅色）/ 深色 `#0B0B0F`；**绝不用**纯黑或高饱和渐变当背景。
- **文字**：`--text: #0D0D0D`、`--text-2: #6E6E73`（Apple 灰）、`--text-3: #8E8E93`。
- **描边**：`--line: #ECECEC`，1px；卡片圆角 `--r-card: 16px`，按钮 `--r-btn: 10px`，胶囊 `999px`。
- **主色**：`--accent: #111111`（近黑按钮，OpenAI 风）；强调色 `--brand: #FF6A00`（小米橙，仅用于进度/焦点/选中描边，点缀不铺面积）。
- **字体**：`-apple-system, "SF Pro Text", "Segoe UI Variable Text", "Segoe UI", "PingFang SC", "Microsoft YaHei UI", system-ui, sans-serif`。
- **动效**：`160ms cubic-bezier(.2,.8,.2,1)`；禁止弹跳、禁止大于 400ms 的转场。
- **阴影**：极轻，`0 1px 2px rgba(0,0,0,.04), 0 8px 24px rgba(0,0,0,.06)`；不要重投影。
- **留白**：大留白，模块间距 24/32px，行高 1.6。
- **禁止**：拟物化、大面积彩色块、emoji 当图标（用内联 SVG 线性图标，1.5px 描边）、多于 3 种强调色。
- **中文文案**：短句、人话、不用英文技术词（如显示「3D 引擎」而不是「OpenSCAD CLI subprocess」）。

---

## 8. 错误码表

| code | 含义 | 前端表现 |
|---|---|---|
| `NO_API_KEY` | 未配置 DeepSeek API Key | 打开 API 配置弹窗 |
| `API_KEY_INVALID` | Key 无效/余额不足 | 提示「API Key 无效或额度不足」+ [重新配置] |
| `API_UNREACHABLE` | 网络不可达 | 提示检查网络 + [重试] |
| `OPENSCAD_MISSING` | 未检测到 OpenSCAD | 展示 [自动安装 OpenSCAD] |
| `OPENSCAD_FAILED` | SCAD 编译失败 | 展示原始错误 + [让 AI 修复] |
| `SCAD_INVALID` | AI 生成的 SCAD 未通过安全校验 | 自动重试一次，仍失败则展示原因 |
| `NEED_ADMIN` | 需要管理员权限 | 说明原因 + [重试（将以管理员身份运行）] |
| `DISK_FULL` | 磁盘空间不足 | 提示清理位置与所需空间 |
| `NOT_FOUND` | 要改的东西已经不在了（项目 / 打印机 / 历史版本） | 提示已失效并刷新列表 |
| `BUSY` | 同一个项目上已有一次生成在跑 | 不报错刷屏，引导到进行中的作业或[停止] |
| `SLICER_NOT_FOUND` | 指定的切片软件当前没检测到 | 引导「设置 → 切片软件」[重新检测]或[手动指定主程序] |
| `CANCELLED` | 用户取消 | 安静结束，不报错 |
| `INTERNAL` | 未预期错误 | 「出了点问题」+ 可展开技术详情 |
