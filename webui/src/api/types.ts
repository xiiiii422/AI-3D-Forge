/**
 * AI 3D Forge —— 前端 / 后端接口类型定义
 *
 * 本文件逐字转写自 `docs/API-CONTRACT.md`（v1）。接口契约是唯一依据：
 * 字段名一律 snake_case，禁止在此处「顺手改名」。
 * 若后端改动接口，必须先改契约，再改本文件。
 */

/* ------------------------------------------------------------------ *
 * 0.1 通用状态对象
 * ------------------------------------------------------------------ */

/** 单项检查的统一结构 */
export interface CheckItem {
  /** 稳定的机器码，前端用它决定「自动修复」按钮的行为 */
  id: CheckId;
  /** OpenAI/Apple 风格的极简中文标题，如 "OpenSCAD" */
  title: string;
  /** 状态：ok=通过 / warn=可用但需注意 / fail=不可用 / checking=检测中 */
  status: 'ok' | 'warn' | 'fail' | 'checking';
  /** 一句话人话结论，如 "OpenSCAD 2021.01 已就绪" */
  detail: string;
  /** 一句话告诉用户「需要我做什么」，通过时为 null */
  action: string | null;
  /** 是否可由软件自动修复（决定是否显示 [自动修复] 按钮） */
  fixable: boolean;
  /** 补充信息，仅用于「系统诊断」展开面板，可为 null */
  meta: Record<string, unknown> | null;
}

export type CheckId =
  | 'windows' // Windows 版本
  | 'runtime' // 软件运行环境（打包后的 Python 运行时）
  | 'python' // Python 运行环境
  | 'python_version' // Python 版本是否满足要求
  | 'openscad' // OpenSCAD 是否安装
  | 'openscad_cli' // OpenSCAD CLI 能否实际跑通
  | 'openscad_version' // OpenSCAD 版本
  | 'deps' // 必要 Python 依赖
  | 'project_dir' // 项目目录权限
  | 'network' // 网络连接（DeepSeek / OpenSCAD 官方源）
  | 'api' // DeepSeek API 配置状态
  | 'api_live' // DeepSeek API 实际可用性（联网测试）
  | 'temp' // 本地临时目录可读写
  | 'stl_preview' // Three.js / WebGL 预览能力
  | 'mesh_engine'; // 3D 模型引擎（STL 解析/分析）

/* ------------------------------------------------------------------ *
 * 0.2 环境检测报告
 * ------------------------------------------------------------------ */

export interface EnvReport {
  /** 报告生成时间（ISO8601，本地时区） */
  checked_at: string;
  /** 总体结论 */
  overall: 'ok' | 'warn' | 'fail';
  /** ok 的数量 / 总数，用于「环境就绪 9/10」 */
  ok_count: number;
  total: number;
  /** 是否已完成首次向导（决定是否显示欢迎向导） */
  first_run: boolean;
  items: CheckItem[];
}

export interface VersionInfo {
  app: string; // "1.0.0"
  build: string; // 构建号，如 "2025.01.15-a1b2c3d"
  app_full: string; // "AI 3D Forge 1.0.0"
  python: string; // 运行环境版本，如 "3.13.14 (bundled)"
  python_bundled: boolean; // true=用户机器无需安装 Python
  openscad: string | null; // 如 "2021.01"，未安装为 null
  os: string; // "Windows 11 专业工作站版 (build 26200)"
}

/* ------------------------------------------------------------------ *
 * 1. 环境与诊断
 * ------------------------------------------------------------------ */

export interface DiagnosticsGroup {
  /** 分组标题："AI 服务" / "3D 引擎" / "模型生成" / "STL 预览" / "文件系统" / "网络" */
  group: string;
  items: CheckItem[];
}

export interface DiagnosticsResponse {
  checked_at: string;
  overall: 'ok' | 'warn' | 'fail';
  groups: DiagnosticsGroup[];
}

export interface DiagnosticsFixResponse {
  ok: boolean;
  message: string;
  report: EnvReport;
}

/* ------------------------------------------------------------------ *
 * 2. OpenSCAD（用户可见文案统一称「3D 引擎」）
 * ------------------------------------------------------------------ */

export interface OpenScadInfo {
  found: boolean;
  path: string | null; // openscad.exe 绝对路径
  version: string | null; // "2021.01"
  source: 'config' | 'path' | 'common' | 'registry' | 'user' | null;
  candidates: { path: string; version: string | null }[]; // 扫描到的所有可用副本
  cli_ok: boolean; // 实际执行 --version 是否成功
  detail: string;
}

export interface OpenScadInstallRequest {
  confirm: true;
  method: 'auto' | 'installer' | 'portable';
}

/** POST /api/openscad/install → 202 + { job_id } */
export interface JobIdRef {
  job_id: string;
}

/** SSE `progress` 事件 */
export interface InstallProgressEvent {
  stage:
    | 'prepare'
    | 'download'
    | 'verify'
    | 'install'
    | 'locate'
    | 'cli_test'
    | 'config'
    | 'done';
  percent: number;
  message: string;
  received?: number;
  total?: number;
}

/** SSE `log` 事件 */
export interface InstallLogEvent {
  message: string;
}

/** SSE `done` 事件 */
export interface InstallDoneEvent {
  ok: true;
  info: OpenScadInfo;
  message: string;
}

/** SSE `error` 事件 */
export interface InstallErrorEvent {
  ok: false;
  message: string;
  need_admin?: boolean;
  manual_url?: string;
  manual_steps?: string[];
}

export interface OpenScadTestResponse {
  ok: boolean;
  message: string;
  duration_ms: number;
  diagnostics: string | null;
}

/* ------------------------------------------------------------------ *
 * 3. 生成 / 修改（核心流程）
 * ------------------------------------------------------------------ */

export interface CreateJobRequest {
  /** 自然语言需求："设计一个桌面手机支架，高度100mm。" */
  prompt: string;
  /** 续改时传入已有项目 id；新建时为 null */
  project_id?: string | null;
  /** 续改时传入修改指令，例如："高度改成150mm。" */
  instruction?: string | null;
  /** 模式：create=从零设计 / modify=在现有 SCAD 上修改 */
  mode?: 'create' | 'modify';
  /** 可选：模型名，缺省由 AI 生成 */
  name?: string | null;
}

export interface JobRef {
  job_id: string;
  project_id: string;
  /** 该项目的版本号（每次成功生成 +1） */
  revision: number;
}

export type JobStage =
  | 'queued'
  | 'thinking'
  | 'understanding'
  | 'awaiting_confirmation'
  | 'generating_scad'
  | 'validating_scad'
  | 'compiling'
  | 'analyzing'
  | 'reviewing'
  | 'verifying'
  | 'exporting'
  | 'done'
  | 'error'
  | 'cancelled';

/** 作业状态：awaiting_confirmation 是「等你确认需求理解」的非终止状态 */
export type JobStatus = 'running' | 'awaiting_confirmation' | 'done' | 'error' | 'cancelled';

export interface JobMessage {
  seq: number;
  /** 面向用户的一句话（中文、无技术黑话） */
  text: string;
  /** 可选：该条消息对应的技术明细，仅诊断面板展示 */
  detail?: string | null;
}

export interface Job {
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
  /** 等待用户确认时的设计规格（含展示用文案）；其它阶段为 null */
  spec?: SpecEventPayload | null;
  created_at: string;
  finished_at: string | null;
}

/* ------------------------------------------------------------------ *
 * 3B. 设计契约（Design Spec）与改动核对
 *
 * 机制：先把你的一句话需求解析成一份「带数值的规格」，你确认后才开始设计；
 * 之后每次修改都先立一份「改动契约」（只准动你点名的参数），改完再逐条核对。
 * 界面据此说明「我按什么做的」「这次到底改了什么」。
 * ------------------------------------------------------------------ */

/** 规格里的一条硬指标 */
export interface SpecItem {
  name: string;
  label: string;
  value: number | null;
  unit: string;
  min: number | null;
  max: number | null;
  /** 是不是你亲口指定的（你指定的值，后续修改不会被 AI 擅自改掉） */
  user_specified: boolean;
  /** 后端拼好的一句话，直接展示即可 */
  display: string;
}

/** 一份设计契约（后端 `DesignSpec.to_display()` 的结构） */
export interface DesignSpec {
  kind: string;
  summary: string;
  specs: SpecItem[];
  features: string[];
  prohibitions: string[];
  acceptance: string[];
  open_questions: string[];
  clarifications: string[];
  fingerprint: string;
}

/** SSE `spec` 事件 / Job.spec */
export interface SpecEventPayload {
  spec: DesignSpec;
  awaiting_confirmation: boolean;
}

/** 一次改动的核对结论 */
export interface ChangeVerdict {
  ok: boolean;
  /** 你要的改动是否真的生效了 */
  performed: boolean;
  /** 你没授权、却被改动的参数名 */
  unauthorized: string[];
  /** 你此前指定的指标是否仍然满足 */
  spec_intact: boolean;
  /** 设计指纹是否保持不变（还是同一个设计） */
  fingerprint_kept: boolean;
  reasons: string[];
  /** 目标值未达成时的相对偏离（参数名 → 偏离比例） */
  target_misses: Record<string, number>;
}

/** AI 归纳的参数，用于「参数微调」UI */
export interface JobParam {
  name: string;
  label: string;
  value: number;
  unit: string;
  min: number | null;
  max: number | null;
  /**
   * 参数分类（用于「参数微调」分栏）。后端目前不产出这个字段，
   * 缺省时前端按变量名/说明归类，见 lib/paramGroups.classifyParam。
   */
  group?: string | null;
}

export interface StlInfo {
  /** 相对下载地址，直接用于 <a download> 或 fetch */
  url: string;
  filename: string;
  size_bytes: number;
  triangles: number;
  volume_mm3: number;
  bbox_mm: { x: number; y: number; z: number };
  watertight: boolean;
  watertight_ratio: number; // 0-1
  /* ---- 契约 §3A：JobResult.stl 额外包含的打印机相关字段 ---- */
  /** 本次生成使用的材料，如 "PETG" */
  material: string;
  /** 材料密度（g/cm³），用于估算耗材克数 */
  material_density: number;
  /** 模型是否放得进这台机器的成型范围 */
  within_build_volume: boolean;
  /** 三轴中最小的那一维（mm），无法测量时为 null */
  min_dimension_mm: number | null;
  /** 最小维度是否达到该机型建议壁厚；缺少几何数据时为 null */
  meets_min_wall: boolean | null;
  /**
   * 耗材用量估算（模型本体 + 支撑）。
   * 注意这是**估算区间**：total_grams 按「N 层墙 + 填充率」算，是下限；
   * total_grams_upper 按实体等效算，是上限 —— 薄壁件实际会靠近上限。
   * 备料请用上限。缺少模型数据时为 null。
   */
  usage?: UsageInfo | null;
}

/** 耗材用量估算（后端 mesh.estimate_usage 的产物） */
export interface UsageInfo {
  material: string;
  density_g_cm3: number;
  /** 实体等效体积（cm³），即 100% 填充 */
  model_solid_cm3: number;
  /** 按外壳 + 填充估算的实际体积（cm³） */
  model_printed_cm3: number;
  model_grams: number;
  support_cm3: number;
  support_grams: number;
  /** 用量下限（克）：模型按填充率估算 + 支撑 */
  total_grams: number;
  /** 用量上限（克）：模型按实体等效 + 支撑。备料用这个 */
  total_grams_upper: number;
  filament_length_m: number;
  support_ratio_pct: number;
  filament_diameter_mm: number;
  assumptions: { wall_lines: number; line_width_mm: number; infill_pct: number };
  /** 估算依据与不确定性说明，必须展示给用户 */
  notes: string[];
}

/** 支撑量估算（棱柱法，偏大的上界） */
export interface SupportInfo {
  angle_limit_deg: number;
  support_volume_mm3: number;
  support_area_mm2: number;
  ratio_pct: number;
  method: string;
}

export interface PrintabilityIssue {
  severity: 'info' | 'warn' | 'error';
  title: string;
  detail: string;
  suggestion: string;
}

/** AI 打印性检查结论 */
export interface Printability {
  score: number; // 0-100
  verdict: 'pass' | 'warn' | 'fail';
  summary: string;
  issues: PrintabilityIssue[];
  metrics_note: string;
  /** 契约 §3A：评审用的同一份打印机上下文（AI 评审未完成时可能没有） */
  printer?: PrinterContext | null;
}

/** 一条可执行需求测试的结果（后端 aiforge/reqtests.py，口径对齐 CADTests） */
export interface ReqTest {
  group: string;
  requirement: string;
  kind: 'dimension' | 'hole' | 'thread' | 'integrity' | 'print' | 'semantic' | string;
  /** pass 达成 / fail 没达成 / invalid 该测却没测出来 / manual 得靠人或外观复核确认 */
  status: 'pass' | 'fail' | 'invalid' | 'manual';
  expected: string;
  actual: string;
  deviation_pct: number | null;
  tolerance_mm: number | null;
  fix_hint: string;
  /** 咨询类（工艺建议）：只进清单，不算需求未达成 */
  advisory: boolean;
}

/** 一次生成/修改的需求核对表 */
export interface ReqSuite {
  pass_rate: number;
  requirement_score: number;
  invalid_ratio: number;
  counts: { pass: number; fail: number; invalid: number; manual: number };
  groups: number;
  manual: number;
  tests: ReqTest[];
}

export interface JobResult {
  project_id: string;
  revision: number;
  name: string;
  /** 生成得到的参数化 SCAD 源码 */
  scad: string;
  /** AI 归纳的参数表，用于「参数微调」UI */
  params: JobParam[];
  /** 一句话设计说明 */
  summary: string;
  stl: StlInfo | null;
  /** AI 打印性检查结论 */
  printability: Printability | null;
  /** 编译/校验等技术信息，折叠展示 */
  diagnostics: string | null;
  /** 契约 §3A：本次生成针对哪台打印机、哪种材料（界面必须展示） */
  printer: PrinterContext | null;
  /** 本次生成依据的设计契约 */
  spec?: DesignSpec | null;
  /** 逐条需求判定与 PR/RS/IR（对标成熟产品的“可验证交付”） */
  requirement_tests?: ReqSuite | null;
  /** 修改类作业的改动核对结论；新建时为 null */
  change_verdict?: ChangeVerdict | null;
  /** 改动核对结论的人话版本 */
  change_summary?: string | null;
}

/** POST /api/jobs/{job_id}/confirm-spec */
export interface ConfirmSpecRequest {
  /** 传回（可修改过的）规格；null 表示「就按你理解的做」 */
  spec?: DesignSpec | null;
  /** 补充说明，会并入契约的澄清记录 */
  note?: string | null;
}

/** POST /api/jobs/{job_id}/cancel → { ok: true } */
export interface OkResponse {
  ok: true;
}

/** POST /api/projects/{project_id}/params */
export interface UpdateParamsRequest {
  params: Record<string, number>;
}

/** POST /api/compile */
export interface CompileRequest {
  scad: string;
  project_id?: string;
  name?: string;
}

/* ------------------------------------------------------------------ *
 * 3A. 打印机配置与知识库
 *
 * 打印机的选择会真正影响 AI 生成（尺寸/壁厚/最小特征/$fn）、
 * 几何检查（薄壁、成型范围）与 AI 打印性评审。切换机型后重新生成即可适配。
 * ------------------------------------------------------------------ */

/** 把机型规格翻译成「模型该怎么设计」的具体数字 */
export interface PrinterConstraints {
  min_wall_mm: number; // 竖直外墙可靠壁厚（3× 喷嘴）
  min_hole_dia_mm: number; // 最小孔径
  min_hole_dia_vertical_mm: number; // 竖直孔最小直径
  min_protrusion_mm: number; // 最小凸起/浮雕
  clearance_mm: number; // 活动件间隙
  max_layer_mm: number;
  default_layer_mm: number;
  max_horizontal_span_mm: number; // 最大桥接
  max_overhang_deg: number;
  max_build_x: number;
  max_build_y: number;
  max_build_z: number;
  max_build_diagonal_mm: number;
  fn: number; // 参考 $fn
  rough_min_dim_mm: number; // 最小可成型尺寸
  notes: string[]; // 这台机器/材料需要提醒的事
}

export interface PrinterProfile {
  id: string;
  name: string;
  manufacturer: string;
  nozzle_mm: number; // 0 = 光固化（无喷嘴）
  build_x: number;
  build_y: number;
  build_z: number;
  build_volume_mm: { x: number; y: number; z: number; volume_cm3: number };
  nozzle_options: number[];
  layer_min_mm: number;
  layer_max_mm: number;
  max_speed_mm_s: number;
  max_accel_mm_s2: number;
  max_flow_mm3_s: number;
  has_heated_bed: boolean;
  max_bed_temp_c: number;
  max_hotend_temp_c: number;
  /** 封闭腔体（决定能否用 ABS/ASA/PA） */
  enclosure: boolean;
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
  /** true = 内置机型（不可删，可被用户覆盖修改） */
  builtin: boolean;
  /** 数据来源说明（官方规格 / 经验值） */
  sources: string;
  updated_at: string;
  /** 由机型 + 材料推导得到 */
  constraints: PrinterConstraints;
  /** 上面这些约束的人话版本，直接展示 */
  checklist: string[];
  recommended_fn_examples: { '50mm': number; '150mm': number };
}

export interface PrintersResponse {
  selected_id: string;
  selected_material: string;
  default_id: string;
  /** 所有机型可用材料的并集 */
  materials: string[];
  printers: PrinterProfile[];
}

/** GET /api/printers/selected */
export interface SelectedPrinterResponse {
  printer: PrinterProfile;
  material: string;
  material_options: string[];
  note: string;
}

/** POST /api/printers/select */
export interface SelectPrinterRequest {
  printer_id: string;
  material?: string;
}

export interface SelectPrinterResponse {
  printer: PrinterProfile;
  material: string;
}

/** POST /api/printers · PUT /api/printers/{printer_id} —— 字段均可选 */
export interface PrinterWriteRequest {
  name?: string;
  manufacturer?: string;
  nozzle_mm?: number;
  build_x?: number;
  build_y?: number;
  build_z?: number;
  nozzle_options?: number[];
  layer_min_mm?: number;
  layer_max_mm?: number;
  max_speed_mm_s?: number;
  max_accel_mm_s2?: number;
  max_flow_mm3_s?: number;
  has_heated_bed?: boolean;
  max_bed_temp_c?: number;
  max_hotend_temp_c?: number;
  enclosure?: boolean;
  materials?: string[];
  filament_diameter_mm?: number;
  extruder?: PrinterProfile['extruder'];
  bed_leveling?: PrinterProfile['bed_leveling'];
  bed_surface?: string;
  kinematics?: PrinterProfile['kinematics'];
  recommended_max_overhang_deg?: number;
  recommended_max_bridge_mm?: number;
  dimensional_tolerance_mm?: number;
  notes?: string;
  sources?: string;
}

/** POST /api/printers/preview —— 编辑机型时的实时预览（不保存） */
export interface PrinterPreviewRequest {
  nozzle_mm?: number;
  build_x?: number;
  build_y?: number;
  build_z?: number;
  material?: string | null;
  enclosure?: boolean;
  max_bed_temp_c?: number;
  max_hotend_temp_c?: number;
  layer_min_mm?: number;
  layer_max_mm?: number;
  recommended_max_overhang_deg?: number;
  recommended_max_bridge_mm?: number;
  extruder?: PrinterProfile['extruder'];
}

export interface PrinterPreviewResponse {
  constraints: PrinterConstraints;
  checklist: string[];
  notes: string[];
}

/** DELETE /api/printers/{id} · POST /api/printers/reset */
export interface PrinterActionResponse {
  ok: true;
  message: string;
}

/** GET /api/printers/knowledge/summary */
export interface PrinterKnowledgeFeature {
  key: string;
  label: string;
  affects: string;
}

export interface PrinterKnowledgeSummary {
  builtin_count: number;
  features: PrinterKnowledgeFeature[];
  material_density_g_cm3: Record<string, number>;
  material_heat_deflection_c: Record<string, number>;
}

/** 契约 §3A：生成结果里的打印机上下文（JobResult.printer） */
export interface PrinterContext {
  id: string;
  name: string;
  manufacturer: string;
  nozzle_mm: number;
  material: string;
  build_volume_mm: { x: number; y: number; z: number };
  enclosure: boolean;
  is_resin: boolean;
  constraints: PrinterConstraints;
  checklist: string[];
}

/* ------------------------------------------------------------------ *
 * 4. 项目
 * ------------------------------------------------------------------ */

export interface ProjectSummary {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
  revision: number;
  thumbnail_url: string | null; // 后端生成的 PNG 预览
  stl_url: string | null;
  summary: string | null;
  /** 最近一次是否编译成功 */
  last_status: 'ok' | 'error' | 'unknown';
}

export interface ProjectDetail extends ProjectSummary {
  scad: string;
  params: JobResult['params'];
  history: {
    revision: number;
    prompt: string | null;
    instruction: string | null;
    created_at: string;
    summary: string;
  }[];
  files: { scad_path: string; stl_path: string | null; export_dir: string | null };
  /**
   * 上一次生成时持久化的实测数据（含壁厚、支撑、耗材估算）。
   * 后端一直在返回它，类型里以前漏了 —— 重开项目要靠它才能显示耗材估算。
   * 只声明界面用得到的字段，其余字段忽略。
   */
  metrics?: ProjectMetrics | null;
}

/** 射线法实测的壁厚分布 */
export interface WallInfo {
  min_mm: number;
  p05_mm: number;
  median_mm: number;
  samples: number;
  thinnest: { location: number[]; thickness_mm: number }[];
}

/** 后端持久化的模型实测数据（只列界面用得到的部分） */
export interface ProjectMetrics {
  volume_mm3?: number;
  wall?: WallInfo | null;
  support?: SupportInfo | null;
  usage?: UsageInfo | null;
}

export interface CreateProjectRequest {
  name: string;
}

export interface RenameProjectRequest {
  name: string;
}

/* ------------------------------------------------------------------ *
 * 5. 设置
 * ------------------------------------------------------------------ */

export interface Settings {
  api_key_set: boolean;
  /** 仅返回掩码，如 "sk-6862****3164"；后端永不回传完整 Key */
  api_key_masked: string | null;
  api_key_store: 'credential_manager' | 'config_file' | 'env';
  base_url: string; // 默认 "https://api.deepseek.com"
  model: string; // 默认 "deepseek-chat"
  /** 混合模式：规格解析/评审/复核用的免费档模型；空串 = 不启用 */
  economy_model: string;
  /** 经济档服务商地址；空串 = 与主提供商同地址 */
  economy_base_url: string;
  openscad_path: string | null;
  data_dir: string; // %APPDATA%\AI-3D-Forge
  projects_dir: string; // %USERPROFILE%\Documents\AI-3D-Forge\Projects
  /** 日志目录（%LOCALAPPDATA%\AI-3D-Forge\logs）：不在 data_dir 里，界面不能猜 */
  log_dir: string;
  keep_stl_history: boolean;
  auto_install_openscad: boolean;
  first_run_done: boolean;
  theme: 'light' | 'dark' | 'system';
  /** 界面材质：solid = 实面（默认），glass = 液态玻璃 */
  appearance: 'solid' | 'glass';
  /** 玻璃透明度百分比 0–60；后端会夹到合法区间（上限受对比度约束） */
  glass_transparency: number;
  /** 契约 §3A：POST /api/printers/select 会同步写入这三个字段 */
  printer_id?: string;
  printer_name?: string;
  printer_material?: string;
  /** 每次生成前是否把目标机型约束说一遍 */
  announce_in_chat: boolean;
  /** 生成前是否先让用户确认「我对需求的理解」（规格卡片） */
  confirm_spec: boolean;
  /** 送到切片软件的默认目标 id（只存 id，路径每次实扫） */
  default_slicer?: string | null;
  /** 自动检测漏网时用户手动指定的主程序 */
  slicer_custom_paths?: string[];
}

/** PUT /api/settings —— 部分更新，字段均可选 */
export interface SettingsUpdate {
  api_key?: string;
  base_url?: string;
  model?: string;
  economy_model?: string;
  economy_base_url?: string;
  openscad_path?: string | null;
  keep_stl_history?: boolean;
  auto_install_openscad?: boolean;
  theme?: 'light' | 'dark' | 'system';
  appearance?: 'solid' | 'glass';
  glass_transparency?: number;
  announce_in_chat?: boolean;
  confirm_spec?: boolean;
  /** 送到切片软件的默认目标；空串 = 取消（回到"用检测到的第一个"） */
  default_slicer?: string | null;
  /** 整表替换手动指定的主程序列表 */
  slicer_custom_paths?: string[];
}

/* ---------------- 切片软件（送到切片软件）---------------- */

/** 检测到的一个切片软件。字段是刻意少的：界面只需要知道哪个软件、版本、从哪找到的、开没开。 */
export interface Slicer {
  id: string;
  name: string;
  exe: string;
  version: string | null;
  /** 发现途径：文件关联 / 已安装程序 / 系统注册 / 常见安装目录 / 手动指定 */
  source_label: string;
  running: boolean;
}

/** GET /api/slicers */
export interface SlicersResponse {
  slicers: Slicer[];
  default_id: string | null;
  count: number;
  /** 我们认得哪些切片软件：没检测到时提示用户装其中任意一个 */
  recognized_names: string[];
}

/**
 * POST /api/projects/{id}/send-to-slicer
 *
 * 三种结果都是"看到了什么就说什么"，不拿"没报错"当成功：
 *  started = 进程还活着；forwarded = 它立刻退出了（单实例把文件交给了已开着的窗口）；
 *  failed = 带着退出码退出。
 */
export interface SendToSlicerResult {
  status: 'started' | 'forwarded' | 'failed';
  detail: string;
  pid: number;
  exit_code: number | null;
  /** 是否成功脱离本软件的进程守护（false 时退出本软件会连带关掉切片器） */
  detached: boolean;
  slicer: string;
  slicer_name: string;
  file: string;
  exe: string;
  kind: '3mf' | 'stl';
  source_label: string;
  note?: string;
}

export interface ApiKeyTestResponse {
  ok: boolean;
  message: string;
  model: string | null;
  latency_ms: number | null;
}

/* ------------------------------------------------------------------ *
 * 6. 应用控制
 * ------------------------------------------------------------------ */

export interface OpenFolderRequest {
  kind: 'data' | 'projects' | 'exports' | 'project';
  project_id?: string;
}

export interface OpenExternalRequest {
  url: string;
}

export interface HealthResponse {
  ok: true;
  version: string;
  uptime_s: number;
}

/** GET /api/app/window-runtime —— 原生窗口组件（WebView2）状态。
 *  缺它时软件会在浏览器里打开，用户手册 §2.2 承诺「页面顶部说明原因 + 一键安装」，
 *  那条横幅用的就是这份形状。 */
export interface WindowRuntimeStatus {
  installed: boolean;
  version: string | null;
  source: string | null;
  detail: string;
  install_hint: string;
  download_url: string;
  bootstrapper_url: string;
}

/** POST /api/app/window-runtime/install —— 一键装（winget 优先，失败回退官方 Bootstrapper 说明） */
export interface WindowRuntimeInstallResponse {
  ok: boolean;
  message: string;
}

/* ------------------------------------------------------------------ *
 * 8. 错误码表
 * ------------------------------------------------------------------ */

export type ApiErrorCode =
  | 'NO_API_KEY'
  | 'API_KEY_INVALID'
  | 'API_UNREACHABLE'
  | 'OPENSCAD_MISSING'
  | 'OPENSCAD_FAILED'
  | 'SCAD_INVALID'
  | 'NEED_ADMIN'
  | 'DISK_FULL'
  | 'CANCELLED'
  | 'NOT_FOUND'
  | 'BUSY'
  | 'SLICER_NOT_FOUND'
  | 'INTERNAL';

/** 非 2xx 一律返回 {"detail": "...", "code": "...", "hint": "..."} */
export interface ApiErrorBody {
  detail: string;
  code: string;
  hint?: string | null;
}
