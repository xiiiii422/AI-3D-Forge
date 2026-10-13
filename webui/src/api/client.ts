/**
 * AI 3D Forge —— 类型化 API 客户端
 *
 * 约定（见 docs/API-CONTRACT.md §0）：
 *  - 除下载接口外全部 JSON，字段 snake_case
 *  - 非 2xx 一律是 { detail, code, hint? }，统一抛出 ApiError
 *  - 认证：无。仅 127.0.0.1 回环
 */

import type {
  ApiErrorBody,
  ApiKeyTestResponse,
  CheckId,
  CompileRequest,
  ConfirmSpecRequest,
  CreateJobRequest,
  CreateProjectRequest,
  DiagnosticsFixResponse,
  DiagnosticsResponse,
  EnvReport,
  HealthResponse,
  Job,
  JobIdRef,
  JobRef,
  OkResponse,
  OpenExternalRequest,
  OpenFolderRequest,
  OpenScadInfo,
  OpenScadInstallRequest,
  OpenScadTestResponse,
  PrinterActionResponse,
  PrinterKnowledgeSummary,
  PrinterPreviewRequest,
  PrinterPreviewResponse,
  PrinterProfile,
  PrinterWriteRequest,
  PrintersResponse,
  ProjectDetail,
  ProjectSummary,
  RenameProjectRequest,
  SelectedPrinterResponse,
  SendToSlicerResult,
  SelectPrinterRequest,
  SelectPrinterResponse,
  Settings,
  SettingsUpdate,
  SlicersResponse,
  TrashListResponse,
  TrashRestoreResponse,
  UpdateParamsRequest,
  VersionInfo,
  WindowRuntimeInstallResponse,
  WindowRuntimeStatus,
} from './types';

/** 所有请求都走这个前缀，dev 期由 Vite proxy 转发到后端 */
export const API_BASE = '/api';

/** 后端返回非 2xx 时抛出的错误。字段名与契约的错误体一致。 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly hint: string | null;

  constructor(status: number, code: string, message: string, hint: string | null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.hint = hint;
  }

  /** 用户能看到的一句话（中文），永远不暴露堆栈或英文技术词 */
  get friendly(): string {
    return this.message || '出了点问题，请稍后重试。';
  }
}

function isApiErrorBody(value: unknown): value is ApiErrorBody {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as ApiErrorBody).detail === 'string'
  );
}

async function toApiError(res: Response): Promise<ApiError> {
  let code = 'INTERNAL';
  let detail = `请求失败（${res.status}）`;
  let hint: string | null = null;
  try {
    const body: unknown = await res.json();
    if (isApiErrorBody(body)) {
      detail = body.detail;
      code = body.code || code;
      hint = body.hint ?? null;
    }
  } catch {
    // 非 JSON 响应（例如网关错误页）——保留默认文案
  }
  return new ApiError(res.status, code, detail, hint);
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  body?: unknown;
  signal?: AbortSignal;
  /** 查询串，值为 undefined/null 时自动跳过 */
  query?: Record<string, string | number | boolean | undefined | null>;
}

function buildUrl(path: string, query?: RequestOptions['query']): string {
  const url = `${API_BASE}${path}`;
  if (!query) return url;
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue;
    search.append(key, String(value));
  }
  const qs = search.toString();
  return qs ? `${url}?${qs}` : url;
}

/** 核心请求函数：统一 JSON、统一错误、统一 abort */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, signal, query } = options;
  let res: Response;
  try {
    res = await fetch(buildUrl(path, query), {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
      credentials: 'same-origin',
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new ApiError(
      0,
      'API_UNREACHABLE',
      '无法连接到本地服务，请确认程序仍在运行。',
      '重新启动 AI 3D Forge 后再试一次。',
    );
  }
  if (!res.ok) throw await toApiError(res);

  if (res.status === 204) return undefined as T;
  const text = await res.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}

/* ------------------------------------------------------------------ *
 * 具体端点（路径与契约 §1–§6 一一对应，不多不少）
 * ------------------------------------------------------------------ */

export const api = {
  /* 1. 环境与诊断 */
  envReport: (opts: { live?: boolean; signal?: AbortSignal } = {}) =>
    request<EnvReport>('/env/report', {
      query: { live: opts.live ? 1 : undefined },
      signal: opts.signal,
    }),
  envVersion: (signal?: AbortSignal) => request<VersionInfo>('/env/version', { signal }),
  envDetect: () => request<EnvReport>('/env/detect', { method: 'POST' }),
  diagnostics: (signal?: AbortSignal) => request<DiagnosticsResponse>('/diagnostics', { signal }),
  diagnosticsFix: (id: CheckId) =>
    request<DiagnosticsFixResponse>('/diagnostics/fix', { method: 'POST', body: { id } }),

  /* 2. OpenSCAD */
  openscadDetect: (signal?: AbortSignal) => request<OpenScadInfo>('/openscad/detect', { signal }),
  openscadLocate: (path: string) =>
    request<OpenScadInfo>('/openscad/locate', { method: 'POST', body: { path } }),
  openscadBrowse: () => request<{ path: string | null }>('/openscad/browse', { method: 'POST' }),
  openscadInstall: (method: OpenScadInstallRequest['method'] = 'auto') =>
    request<JobIdRef>('/openscad/install', {
      method: 'POST',
      body: { confirm: true, method } satisfies OpenScadInstallRequest,
    }),
  openscadTest: () => request<OpenScadTestResponse>('/openscad/test', { method: 'POST' }),

  /* 3. 生成 / 修改 */
  createJob: (body: CreateJobRequest) => request<JobRef>('/jobs', { method: 'POST', body }),
  getJob: (jobId: string, signal?: AbortSignal) =>
    request<Job>(`/jobs/${encodeURIComponent(jobId)}`, { signal }),
  cancelJob: (jobId: string) =>
    request<OkResponse>(`/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' }),
  /** 确认（或纠正）AI 对需求的理解；确认后作业才真正开始设计 */
  confirmSpec: (jobId: string, body: ConfirmSpecRequest = {}) =>
    request<OkResponse>(`/jobs/${encodeURIComponent(jobId)}/confirm-spec`, {
      method: 'POST',
      body,
    }),
  /**
   * 列出某个项目的作业（后端只回最近 20 条）。
   *
   * 用途：**切页/刷新后重新接管正在跑的作业**。作业状态在后端，
   * 前端组件一卸载就全忘了 —— 于是"在后台建模时点去别的界面再回来，
   * 建模界面凭空消失"，而作业其实还在跑。
   */
  listJobs: (projectId: string, signal?: AbortSignal) =>
    request<{ jobs: Job[] }>('/jobs', { query: { project_id: projectId }, signal }),
  updateParams: (projectId: string, body: UpdateParamsRequest) =>
    request<JobRef>(`/projects/${encodeURIComponent(projectId)}/params`, {
      method: 'POST',
      body,
    }),
  compile: (body: CompileRequest) => request<JobRef>('/compile', { method: 'POST', body }),

  /* 3A. 打印机配置与知识库 */
  listPrinters: (signal?: AbortSignal) => request<PrintersResponse>('/printers', { signal }),
  selectedPrinter: (signal?: AbortSignal) =>
    request<SelectedPrinterResponse>('/printers/selected', { signal }),
  selectPrinter: (body: SelectPrinterRequest) =>
    request<SelectPrinterResponse>('/printers/select', { method: 'POST', body }),
  getPrinter: (printerId: string, signal?: AbortSignal) =>
    request<PrinterProfile>(`/printers/${encodeURIComponent(printerId)}`, { signal }),
  createPrinter: (body: PrinterWriteRequest) =>
    request<PrinterProfile>('/printers', { method: 'POST', body }),
  updatePrinter: (printerId: string, body: PrinterWriteRequest) =>
    request<PrinterProfile>(`/printers/${encodeURIComponent(printerId)}`, {
      method: 'PUT',
      body,
    }),
  deletePrinter: (printerId: string) =>
    request<PrinterActionResponse>(`/printers/${encodeURIComponent(printerId)}`, {
      method: 'DELETE',
    }),
  previewPrinter: (body: PrinterPreviewRequest) =>
    request<PrinterPreviewResponse>('/printers/preview', { method: 'POST', body }),
  resetPrinters: () => request<PrinterActionResponse>('/printers/reset', { method: 'POST' }),
  printerKnowledge: (signal?: AbortSignal) =>
    request<PrinterKnowledgeSummary>('/printers/knowledge/summary', { signal }),

  /* 4. 项目 */
  listProjects: (signal?: AbortSignal) => request<ProjectSummary[]>('/projects', { signal }),
  createProject: (body: CreateProjectRequest) =>
    request<ProjectSummary>('/projects', { method: 'POST', body }),
  getProject: (id: string, signal?: AbortSignal) =>
    request<ProjectDetail>(`/projects/${encodeURIComponent(id)}`, { signal }),
  renameProject: (id: string, body: RenameProjectRequest) =>
    request<ProjectSummary>(`/projects/${encodeURIComponent(id)}`, { method: 'PATCH', body }),
  deleteProject: (id: string) =>
    request<OkResponse>(`/projects/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  /**
   * 回收站（`Projects/.deleted/`）。
   *
   * 后端里 `/api/projects/trash` 必须比 `/api/projects/{project_id}` 先注册，
   * 否则这个请求会被当成「查一个叫 trash 的项目」而 404（自检盯住这一条）。
   */
  listTrash: (signal?: AbortSignal) => request<TrashListResponse>('/projects/trash', { signal }),
  restoreFromTrash: (entry: string) =>
    request<TrashRestoreResponse>(`/projects/trash/${encodeURIComponent(entry)}/restore`, {
      method: 'POST',
    }),
  /** 不可恢复：后端要求 `confirm: true`，少了它直接 400 */
  purgeTrashEntry: (entry: string) =>
    request<OkResponse>(`/projects/trash/${encodeURIComponent(entry)}/purge`, {
      method: 'POST',
      body: { confirm: true },
    }),
  /** POST /api/projects/{id}/restore —— 回退到某个历史版本（会生成新的一版） */
  restoreRevision: (id: string, revision: number) =>
    request<{ project_id: string; restored_from: number; revision: number }>(
      `/projects/${encodeURIComponent(id)}/restore`,
      { method: 'POST', body: { revision } },
    ),
  /** POST /api/projects/{id}/save —— 弹原生保存框，写到用户选定目录 */
  saveProjectFile: (id: string, kind: 'stl' | 'scad' | '3mf') =>
    request<{ status: 'saved' | 'canceled' | 'no-dialog'; path?: string }>(
      `/projects/${encodeURIComponent(id)}/save`,
      { method: 'POST', body: { kind } },
    ),

  /* 4A. 切片软件（只三个调用：列表、手动指定、送出）*/
  /** GET /api/slicers —— rescan=true 强制重扫（刚装完切片软件时用） */
  listSlicers: (opts: { rescan?: boolean; signal?: AbortSignal } = {}) =>
    request<SlicersResponse>('/slicers', {
      query: { rescan: opts.rescan ? 1 : undefined },
      signal: opts.signal,
    }),
  /** POST /api/slicers/pick —— 弹原生选择框指定主程序（自动检测漏了才需要） */
  pickSlicer: () =>
    request<SlicersResponse & { status: 'added' | 'canceled' | 'nodialog'; path?: string }>(
      '/slicers/pick',
      { method: 'POST' },
    ),
  /** POST /api/projects/{id}/send-to-slicer —— 不传 slicer 就用默认/检测到的第一个 */
  sendToSlicer: (id: string, slicer?: string) =>
    request<SendToSlicerResult>(`/projects/${encodeURIComponent(id)}/send-to-slicer`, {
      method: 'POST',
      body: slicer ? { slicer } : {},
    }),

  /* 5. 设置 */
  getSettings: (signal?: AbortSignal) => request<Settings>('/settings', { signal }),
  updateSettings: (body: SettingsUpdate) =>
    request<Settings>('/settings', { method: 'PUT', body }),
  testApiKey: (apiKey?: string) =>
    request<ApiKeyTestResponse>('/settings/api-key/test', {
      method: 'POST',
      body: apiKey === undefined ? {} : { api_key: apiKey },
    }),
  completeFirstRun: () =>
    request<OkResponse>('/settings/first-run/complete', { method: 'POST' }),

  /* 6. 应用控制 */
  openFolder: (body: OpenFolderRequest) =>
    request<OkResponse>('/app/open-folder', { method: 'POST', body }),
  openExternal: (url: string) =>
    request<OkResponse>('/app/open-external', {
      method: 'POST',
      body: { url } satisfies OpenExternalRequest,
    }),
  health: (signal?: AbortSignal) => request<HealthResponse>('/app/health', { signal }),
  shutdown: () => request<OkResponse>('/app/shutdown', { method: 'POST' }),
  /** 原生窗口组件状态：只在「被浏览器打开」时拉一次（那条横幅需要它） */
  windowRuntime: (signal?: AbortSignal) =>
    request<WindowRuntimeStatus>('/app/window-runtime', { signal }),
  installWindowRuntime: () =>
    request<WindowRuntimeInstallResponse>('/app/window-runtime/install', { method: 'POST' }),
};

/* ------------------------------------------------------------------ *
 * 下载地址工具
 * ------------------------------------------------------------------ */

/** GET /api/projects/{id}/stl —— 附件下载（application/sla） */
export const stlDownloadUrl = (projectId: string) =>
  `${API_BASE}/projects/${encodeURIComponent(projectId)}/stl`;

/** GET /api/projects/{id}/scad —— 附件下载（text/plain） */
export const scadDownloadUrl = (projectId: string) =>
  `${API_BASE}/projects/${encodeURIComponent(projectId)}/scad`;

/** GET /api/projects/{id}/3mf —— 切片软件用的 3MF（声明 mm 单位 + 机型/材料元数据） */
export const threeMfDownloadUrl = (projectId: string) =>
  `${API_BASE}/projects/${encodeURIComponent(projectId)}/3mf`;

/** GET /api/projects/{id}/stl/raw —— inline，供 STLLoader 直接 fetch */
export const stlRawUrl = (projectId: string, revision?: number) =>
  `${API_BASE}/projects/${encodeURIComponent(projectId)}/stl/raw${
    revision === undefined ? '' : `?rev=${revision}`
  }`;

/** GET /api/projects/{id}/thumbnail —— image/png */
export const thumbnailUrl = (projectId: string) =>
  `${API_BASE}/projects/${encodeURIComponent(projectId)}/thumbnail`;

/** GET /api/projects/{id}/metrics —— 持久化的实测数据（重开项目时合成结果卡用） */
export const projectMetrics = (id: string, signal?: AbortSignal) =>
  request<Record<string, unknown>>(`/projects/${encodeURIComponent(id)}/metrics`, { signal });

/** 各提供商的免费模型（供顶栏“切换免费模型”下拉） */
export const DEEPSEEK_BASE_URL = 'https://api.deepseek.com';
export const ZHIPU_BASE_URL = 'https://open.bigmodel.cn/api/paas/v4';

/** 三种使用模式：免费 / 付费均衡 / 混合性价比（生成付费 + 解析评审免费） */
export interface ModePreset {
  id: 'free' | 'deepseek' | 'hybrid';
  label: string;
  base_url: string;
  model: string;
  economy_base_url: string;
  economy_model: string;
}
export const MODE_PRESETS: ModePreset[] = [
  {
    id: 'free',
    label: '免费·GLM Flash',
    base_url: ZHIPU_BASE_URL,
    model: 'glm-4.7-flash',
    economy_base_url: '',
    economy_model: '',
  },
  {
    id: 'deepseek',
    label: 'DeepSeek·付费均衡',
    base_url: DEEPSEEK_BASE_URL,
    model: 'deepseek-chat',
    economy_base_url: '',
    economy_model: '',
  },
  {
    id: 'hybrid',
    label: '混合·性价比',
    base_url: DEEPSEEK_BASE_URL,
    model: 'deepseek-chat',
    economy_base_url: ZHIPU_BASE_URL,
    economy_model: 'glm-4.7-flash',
  },
];

/** 触发浏览器另存为，不新开标签页 */
export function triggerDownload(url: string, filename?: string): void {
  const a = document.createElement('a');
  a.href = url;
  if (filename) a.download = filename;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/* ------------------------------------------------------------------ *
 * SSE 帧解析
 * ------------------------------------------------------------------ */

export interface SseFrame {
  /** 事件名，缺省为 "message" */
  event: string;
  /** data 行的拼接结果（多行 data 用 \n 连接） */
  data: string;
  /** 服务端可选下发的事件 id */
  id: string | null;
}

/**
 * 把 SSE 文本块解析成帧。刻意写成纯函数，便于 useJobStream 复用与测试。
 * 返回值中的 `rest` 是尚未闭合的残余文本，需与下一块拼接。
 */
export function parseSseChunk(buffer: string): { frames: SseFrame[]; rest: string } {
  const frames: SseFrame[] = [];
  // 兼容 \r\n 与 \n
  const normalized = buffer.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  const parts = normalized.split('\n\n');
  const rest = parts.pop() ?? '';

  for (const block of parts) {
    let event = 'message';
    let id: string | null = null;
    const dataLines: string[] = [];
    for (const rawLine of block.split('\n')) {
      const line = rawLine.trimEnd();
      if (!line || line.startsWith(':')) continue; // 注释/心跳
      const colon = line.indexOf(':');
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? '' : line.slice(colon + 1);
      if (value.startsWith(' ')) value = value.slice(1);
      if (field === 'event') event = value;
      else if (field === 'data') dataLines.push(value);
      else if (field === 'id') id = value;
    }
    if (dataLines.length === 0 && event === 'message') continue;
    frames.push({ event, data: dataLines.join('\n'), id });
  }
  return { frames, rest };
}

/** 安全地把 SSE data 解析成对象；解析失败返回 null（不抛） */
export function parseSseData<T>(data: string): T | null {
  if (!data) return null;
  try {
    return JSON.parse(data) as T;
  } catch {
    return null;
  }
}
