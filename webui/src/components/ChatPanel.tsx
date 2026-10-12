import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { api, scadDownloadUrl, stlDownloadUrl, threeMfDownloadUrl, triggerDownload } from '../api/client';
import type { DesignSpec, JobResult, Printability, SpecEventPayload } from '../api/types';
import { trimNum } from '../hooks/usePrinters';
import { useSlicers } from '../hooks/useSlicers';
import { toFriendlyError } from '../hooks/errors';
import { Button } from './Button';
import { Accordion } from './Accordion';
import { ChangeVerdictCard, SpecCard } from './SpecCard';
import {
  IconAlert,
  IconArrowUp,
  IconChat,
  IconCheck,
  IconDownload,
  IconExternal,
  IconFileCode,
  IconFolder,
  IconLayers,
  IconNozzle,
  IconRefresh,
  IconSparkle,
  IconStop,
  IconWrench,
  IconX,
} from './Icons';
import { ProgressBar } from './ProgressBar';
import { RequirementChecklist } from './RequirementChecklist';
import { StatusDot } from './StatusDot';
import { useToast } from './Toast';

export interface ChatEntry {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  at: string;
  /** 生成完成后的结果卡片 */
  result?: JobResult;
  /** 失败信息 */
  error?: { message: string; hint: string | null; code: string };
  kind?: 'message' | 'params';
}

interface ChatPanelProps {
  entries: ChatEntry[];
  /** 运行中的单行状态文案（打字机效果） */
  running: boolean;
  statusText: string;
  percent: number | null;
  stageLabel: string;
  hasProject: boolean;
  /** 当前是否已经产出过模型（决定输入框提示语） */
  hasModel: boolean;
  onSend: (text: string) => void;
  onCancel: () => void;
  /** 错误卡片上的行为按钮 */
  onErrorAction: (entry: ChatEntry) => void;
  /** 等待用户确认的设计规格；null 表示当前没有待确认项 */
  pendingSpec?: SpecEventPayload | null;
  /**
   * 需求已经发出、但还没拿到 job_id 的那几百毫秒。
   *
   * 为什么必须有这个状态：`running` 是由「job_id 已到手」推出来的，
   * 而创建作业是一次网络往返。旧实现里发送键只看 `running`，
   * 所以「按 Enter 之后、响应回来之前」这段时间发送键仍是可点的 ——
   * 用户连按两次就会发出两个作业（第二个会被后端 409 挡掉，
   * 但界面上会凭空多出一条错误卡片，用户以为是自己操作错了）。
   */
  sending?: boolean;
  /** 正在提交规格确认 */
  specBusy?: boolean;
  onConfirmSpec?: (spec: DesignSpec | null, note: string) => void;
  onCancelSpec?: () => void;
  sendDisabled?: boolean;
}

const EXAMPLES = [
  '设计一个桌面手机支架，高度100mm',
  '做一个 80mm 的花盆，壁厚3mm',
  '画一个 60×40×20mm 的收纳盒，带盖子',
];

const VERDICT_LABEL: Record<Printability['verdict'], string> = {
  pass: '可以打印',
  warn: '可以打印，建议调整',
  fail: '不建议直接打印',
};

const VERDICT_STATUS: Record<Printability['verdict'], 'ok' | 'warn' | 'fail'> = {
  pass: 'ok',
  warn: 'warn',
  fail: 'fail',
};

const SEVERITY_LABEL: Record<'info' | 'warn' | 'error', string> = {
  info: '提示',
  warn: '注意',
  error: '问题',
};

const SEVERITY_CLASS: Record<'info' | 'warn' | 'error', string> = {
  info: 'tag',
  warn: 'tag tag-warn',
  error: 'tag tag-danger',
};

/** 错误码 → 行为按钮文案（契约 §8 的「前端表现」列） */
function errorAction(code: string, hasModel: boolean): { label: string; icon: 'wrench' | 'refresh' | 'settings' } | null {
  switch (code) {
    case 'OPENSCAD_FAILED':
    case 'SCAD_INVALID':
      return { label: '自动修复', icon: 'wrench' };
    case 'OPENSCAD_MISSING':
      return { label: '去安装 3D 引擎', icon: 'settings' };
    case 'NO_API_KEY':
    case 'API_KEY_INVALID':
      return { label: '去配置 AI 服务', icon: 'settings' };
    case 'API_UNREACHABLE':
    case 'INTERNAL':
      return { label: '重试', icon: 'refresh' };
    case 'NEED_ADMIN':
      return { label: '重试（以管理员身份运行）', icon: 'refresh' };
    case 'CANCELLED':
      return null;
    default:
      return hasModel ? { label: '重试', icon: 'refresh' } : null;
  }
}

function formatDateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getMonth() + 1}月${d.getDate()}日 ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatNumber(value: number, digits = 1): string {
  return value.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/** 体积：契约给的是 mm³，界面按 cm³ 展示 */
function formatVolumeCm3(mm3: number): string {
  return `${formatNumber(mm3 / 1000, 2)} cm³`;
}

function IssueRow({ issue }: { issue: Printability['issues'][number] }) {
  const [open, setOpen] = useState(issue.severity === 'error');
  return (
    <div className="issue">
      <button
        type="button"
        className="issue-head"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className={SEVERITY_CLASS[issue.severity]}>{SEVERITY_LABEL[issue.severity]}</span>
        <span className="title">{issue.title}</span>
        <span className="muted-3 small">{open ? '收起' : '详情'}</span>
      </button>
      {open ? (
        <div className="issue-body">
          <div>{issue.detail}</div>
          {issue.suggestion ? <div className="suggestion">建议：{issue.suggestion}</div> : null}
        </div>
      ) : null}
    </div>
  );
}

function ResultCard({ result }: { result: JobResult }) {
  const stl = result.stl;
  const printability = result.printability;
  /** 契约 §3A：结果里带着本次生成所依据的打印机（老数据可能没有） */
  const printer = result.printer ?? printability?.printer ?? null;
  const toast = useToast();
  const [saving, setSaving] = useState(false);
  const slicers = useSlicers();
  const [sending, setSending] = useState(false);

  // 下载：优先弹原生保存框（WebView2 里 `<a download>` 点了无响应）；
  // 无桌面会话（纯浏览器）或后端不支持时，退回浏览器下载。
  const save = async (kind: 'stl' | 'scad' | '3mf') => {
    const urlFor = kind === 'stl' ? stlDownloadUrl : kind === '3mf' ? threeMfDownloadUrl : scadDownloadUrl;
    const nameFor = kind === 'stl' ? (stl?.filename ?? `${result.name}.stl`) : kind === '3mf' ? `${result.name}.3mf` : `${result.name}.scad`;
    const fallbackUrl = urlFor(result.project_id);
    const fallbackName = nameFor;
    setSaving(true);
    try {
      const r = await api.saveProjectFile(result.project_id, kind);
      if (r.status === 'saved' && r.path) toast.ok(`已保存到 ${r.path}`);
      else if (r.status === 'no-dialog') triggerDownload(fallbackUrl, fallbackName);
      // canceled：用户自己关了保存框，不提示
    } catch {
      // 后端不支持 /save（如浏览器模式或旧版）→ 退回浏览器下载
      triggerDownload(fallbackUrl, fallbackName);
    } finally {
      setSaving(false);
    }
  };

  /**
   * 送到切片软件：不弹选择框，直接交给默认目标（切换在「设置 → 切片软件」里做）。
   * 后端把看到的三种情况都说了实话，这里就照原样转达，不拿「没报错」当成功。
   */
  const sendToSlicer = async () => {
    setSending(true);
    try {
      const r = await api.sendToSlicer(result.project_id);
      if (r.status === 'failed') toast.error(r.detail);
      // forwarded：它立刻退出了 = 把文件交给了已经开着的窗口，属于正常但不是「我启动了它」
      else if (r.status === 'forwarded') toast.show(r.detail);
      else toast.ok(r.detail);
      if (r.note) toast.warn(r.note);
    } catch (err) {
      const friendly = toFriendlyError(err);
      toast.error(friendly.hint ? `${friendly.message} ${friendly.hint}` : friendly.message);
      // 列表可能过期了（刚卸载或刚装）：让下一次的按钮状态跟着真实情况走
      if (friendly.code === 'SLICER_NOT_FOUND') void slicers.reload(true);
    } finally {
      setSending(false);
    }
  };

  const build = printer?.build_volume_mm ?? null;
  const minWall = printer?.constraints.min_wall_mm ?? null;
  const oversizedAxes = stl && build
    ? (['x', 'y', 'z'] as const).filter((axis) => stl.bbox_mm[axis] > build[axis] + 0.01)
    : [];
  const axisName = { x: '宽', y: '深', z: '高' } as const;

  return (
    <div className="result">
      <div className="result-head">
        <span className="row" style={{ gap: 6, minWidth: 0 }}>
          <IconCheck size={15} style={{ color: 'var(--ok)', flex: 'none' }} />
          <strong className="nowrap" style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {result.name}
          </strong>
          <span className="tag">第 {result.revision} 版</span>
        </span>
      </div>

      <div className="result-body">
        {/* 这次生成是按哪台机器、哪种材料做的 */}
        {printer ? (
          <div className="result-printer">
            <IconNozzle size={14} />
            <span>
              按 <strong>{printer.name}</strong>
              {printer.material ? ` · ${printer.material}` : ''} 检查
            </span>
          </div>
        ) : null}

        {/* 超出成型范围 */}
        {stl && stl.within_build_volume === false && build ? (
          <div className="warn-row" role="alert">
            <IconAlert size={16} />
            <div className="warn-row-main">
              <strong>超出该机型成型范围</strong>
              <div>
                模型尺寸 {formatNumber(stl.bbox_mm.x)}×{formatNumber(stl.bbox_mm.y)}×
                {formatNumber(stl.bbox_mm.z)}mm，超过该机型的{' '}
                {formatNumber(build.x)}×{formatNumber(build.y)}×{formatNumber(build.z)}mm
                {oversizedAxes.length > 0 ? (
                  <>
                    （超限：{oversizedAxes
                      .map(
                        (axis) =>
                          `${axisName[axis]} ${formatNumber(stl.bbox_mm[axis])} > ${formatNumber(
                            build[axis],
                          )}mm`,
                      )
                      .join('，')}）
                  </>
                ) : null}
                。
              </div>
              <div className="warn-row-hint">
                可以整体缩小到成型范围内、换个方向摆放，或者拆成几件分别打印后再拼装。
              </div>
            </div>
          </div>
        ) : null}

        {/* 低于建议壁厚 */}
        {stl && stl.meets_min_wall === false && minWall !== null ? (
          <div className="warn-row" role="alert">
            <IconAlert size={16} />
            <div className="warn-row-main">
              <strong>低于该机型建议壁厚</strong>
              <div>
                模型最小外形尺寸约 {formatNumber(stl.min_dimension_mm ?? 0)}mm，该机型建议不小于{' '}
                {trimNum(minWall, 2)}mm。
              </div>
              <div className="warn-row-hint">
                太细的结构打印时容易断裂或糊在一起，建议加厚，或换更细的喷嘴（如 0.2mm）。
              </div>
            </div>
          </div>
        ) : null}

        {/* 设计说明 */}
        <div>
          <div className="field-label" style={{ marginBottom: 4 }}>
            设计说明
          </div>
          <div className="result-summary">{result.summary}</div>
        </div>

        {/* 这次改动到底动了什么 —— 用户最关心的那句核对结论 */}
        {result.change_verdict ? (
          <ChangeVerdictCard verdict={result.change_verdict} summary={result.change_summary} />
        ) : null}

        {/* 模型数据 */}
        {stl ? (
          <div className="stats-grid">
            <div className="stats-cell">
              <span>尺寸（毫米）</span>
              <b>
                {formatNumber(stl.bbox_mm.x)} × {formatNumber(stl.bbox_mm.y)} ×{' '}
                {formatNumber(stl.bbox_mm.z)}
              </b>
            </div>
            <div className="stats-cell">
              <span>体积</span>
              <b>{formatVolumeCm3(stl.volume_mm3)}</b>
            </div>
            <div className="stats-cell">
              <span>三角面数</span>
              <b>{stl.triangles.toLocaleString('zh-CN')}</b>
            </div>
            <div className="stats-cell">
              <span>文件大小</span>
              <b>{formatBytes(stl.size_bytes)}</b>
            </div>
            {/* 耗材用量：用户最实际的那个数字（够不够打、要不要买料） */}
            {stl.usage ? (
              <div className="stats-cell">
                <span>预计耗材</span>
                <b>
                  {formatNumber(stl.usage.total_grams)}–{formatNumber(stl.usage.total_grams_upper)} g
                  <em className="stats-hint">
                    {stl.usage.material}，约 {formatNumber(stl.usage.filament_length_m)} m 线材
                    {stl.usage.support_grams > 0
                      ? `（含支撑 ${formatNumber(stl.usage.support_grams)} g）`
                      : ''}
                  </em>
                </b>
              </div>
            ) : null}
          </div>
        ) : null}

        {/* 打印性结论 */}
        {printability ? (
          <div className="verdict">
            <div className="verdict-score">
              <b>{Math.round(printability.score)}</b>
              <span>打印评分</span>
            </div>
            <div className="verdict-main">
              <div className="verdict-title">
                <StatusDot status={VERDICT_STATUS[printability.verdict]} />
                {VERDICT_LABEL[printability.verdict]}
              </div>
              <div className="muted small">{printability.summary}</div>
            </div>
          </div>
        ) : null}

        {printability && printability.issues.length > 0 ? (
          <div>
            <div className="field-label" style={{ marginBottom: 6 }}>
              检查到 {printability.issues.length} 条建议
            </div>
            {printability.issues.map((issue, index) => (
              <IssueRow issue={issue} key={`${issue.title}-${index}`} />
            ))}
            {printability.metrics_note ? (
              <p className="field-hint" style={{ marginTop: 8 }}>
                {printability.metrics_note}
              </p>
            ) : null}
          </div>
        ) : null}

        {/* 逐条需求核对（要求 vs 实测）——用户判断“这版能不能直接用”的依据 */}
        <RequirementChecklist suite={result.requirement_tests} />

        {/* 本次依据的设计契约（只读） */}
        {result.spec && result.spec.specs.length > 0 ? (
          <Accordion title="本次依据的规格">
            <SpecCard
              payload={{ spec: result.spec, awaiting_confirmation: false }}
              locked
              onConfirm={() => undefined}
              onCancel={() => undefined}
            />
          </Accordion>
        ) : null}

        {/* 下载 */}
        <div className="downloads">
          <Button
            variant="primary"
            size="sm"
            icon={<IconDownload size={15} />}
            onClick={() => void save('stl')}
            disabled={!stl || saving}
          >
            下载 STL
          </Button>
          <Button
            size="sm"
            icon={<IconFileCode size={15} />}
            onClick={() => void save('scad')}
            disabled={saving}
          >
            下载 SCAD
          </Button>
          {/* 3MF 声明 mm 单位并带机型/材料元数据，进切片软件不会再“尺寸差 100 倍” */}
          <Button
            size="sm"
            icon={<IconLayers size={15} />}
            onClick={() => void save('3mf')}
            disabled={saving || !stl}
            title="带毫米单位与打印机/材料信息，适合直接拖进 Bambu Studio / OrcaSlicer / Cura"
          >
            下载 3MF
          </Button>
          {/* 一键送进切片软件：只在真检测到软件时才出现 —— 不放一个点了只会报错的死按钮 */}
          {slicers.count > 0 && stl ? (
            <Button
              size="sm"
              icon={<IconExternal size={15} />}
              onClick={() => void sendToSlicer()}
              loading={sending}
              disabled={saving}
              title={
                slicers.target
                  ? `用 ${slicers.target.name} 打开这个模型，不用再自己找文件`
                  : undefined
              }
            >
              送到切片软件
            </Button>
          ) : null}
          {/* 模型文件已经存在项目文件夹里，一键打开比让用户去找路径实用 */}
          <Button
            size="sm"
            variant="ghost"
            icon={<IconFolder size={15} />}
            onClick={() => {
              void api.openFolder({ kind: 'project', project_id: result.project_id }).catch(() => undefined);
            }}
          >
            打开文件夹
          </Button>
        </div>

        {/* 技术详情（折叠） */}
        {result.diagnostics ? (
          <Accordion title="技术详情">
            <pre
              className="mono"
              style={{ margin: 0, whiteSpace: 'pre-wrap', maxHeight: 220, overflow: 'auto' }}
            >
              {result.diagnostics}
            </pre>
          </Accordion>
        ) : null}
      </div>
    </div>
  );
}

export function ChatPanel({
  entries,
  running,
  statusText,
  percent,
  stageLabel,
  hasProject,
  hasModel,
  onSend,
  onCancel,
  onErrorAction,
  sending,
  pendingSpec,
  specBusy,
  onConfirmSpec,
  onCancelSpec,
  sendDisabled,
}: ChatPanelProps) {
  const [draft, setDraft] = useState('');
  const listRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // 新消息 / 进度变化 / 规格卡片出现时保持滚动到底部
  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [entries.length, statusText, running, pendingSpec]);

  const submit = () => {
    const text = draft.trim();
    if (!text || running || sending || sendDisabled) return;
    onSend(text);
    setDraft('');
    if (textareaRef.current) textareaRef.current.style.height = 'auto';
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <section className="panel" aria-label="对话">
      <div className="panel-head">
        <h2 className="row" style={{ gap: 6 }}>
          <IconChat size={16} style={{ color: 'var(--text-3)' }} />
          设计对话
        </h2>
        {running ? (
          <Button size="sm" variant="ghost" icon={<IconStop size={14} />} onClick={onCancel}>
            停止
          </Button>
        ) : null}
      </div>

      {/* 运行中：顶部细进度条 + 单行打字机状态，不刷屏 */}
      {running || sending ? (
        <div className="runbar">
          <ProgressBar
            percent={percent}
            label={undefined}
            value={percent === null ? undefined : `${Math.round(percent)}%`}
          />
          <div className="runbar-status" style={{ marginTop: 8 }}>
            <StatusDot status="checking" />
            <span className="runbar-status-text" aria-live="polite">
              {sending && !running ? '正在提交…' : statusText || stageLabel || '正在处理…'}
              <span className="caret" />
            </span>
          </div>
        </div>
      ) : null}

      <div className="chat" ref={listRef}>
        {/* 首次使用的引导：第一条消息就是描述输入 */}
        {entries.length === 0 ? (
          <div className="msg msg-ai">
            <div className="msg-text">
              {hasProject
                ? '接着描述要改的地方，例如「高度改成150mm」。'
                : '用一句话描述你想设计的模型，我来生成可以直接打印的文件。'}
            </div>
            {!hasProject ? (
              <div className="examples">
                {EXAMPLES.map((example) => (
                  <button
                    type="button"
                    className="chip-example"
                    key={example}
                    onClick={() => setDraft(example)}
                  >
                    {example}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}

        {entries.map((entry) =>
          entry.role === 'user' ? (
            <div className="msg msg-user" key={entry.id}>
              <div className="bubble">{entry.text}</div>
              <div className="msg-time">{formatDateTime(entry.at)}</div>
            </div>
          ) : (
            <div className="msg msg-ai" key={entry.id}>
              <div className="msg-role">
                {entry.kind === 'params' ? '参数微调' : 'AI 3D Forge'} · {formatDateTime(entry.at)}
              </div>
              {entry.text ? <div className="msg-text">{entry.text}</div> : null}

              {entry.error ? (
                <div className="err" style={{ marginTop: 8 }}>
                  <div className="err-title">
                    <IconX size={14} />
                    生成失败
                  </div>
                  <div>{entry.error.message}</div>
                  {entry.error.hint ? <div className="err-hint">{entry.error.hint}</div> : null}
                  {(() => {
                    const action = errorAction(entry.error.code, hasModel);
                    if (!action) return null;
                    return (
                      <div className="row" style={{ marginTop: 4 }}>
                        <Button
                          size="sm"
                          variant="primary"
                          icon={
                            action.icon === 'wrench' ? (
                              <IconWrench size={14} />
                            ) : action.icon === 'refresh' ? (
                              <IconRefresh size={14} />
                            ) : (
                              <IconSparkle size={14} />
                            )
                          }
                          onClick={() => onErrorAction(entry)}
                        >
                          {action.label}
                        </Button>
                      </div>
                    );
                  })()}
                </div>
              ) : null}

              {entry.result ? (
                <div style={{ marginTop: 8, width: '100%' }}>
                  <ResultCard result={entry.result} />
                </div>
              ) : null}
            </div>
          ),
        )}
        {pendingSpec ? (
          <div className="msg msg-ai">
            <div className="msg-role">AI 3D Forge · 需求确认</div>
            <div style={{ marginTop: 8, width: '100%' }}>
              <SpecCard
                payload={pendingSpec}
                busy={specBusy}
                locked={!pendingSpec.awaiting_confirmation}
                onConfirm={(spec, note) => onConfirmSpec?.(spec, note)}
                onCancel={() => onCancelSpec?.()}
              />
            </div>
          </div>
        ) : null}
      </div>

      <div className="composer">
        <div className="composer-box">
          <textarea
            ref={textareaRef}
            rows={1}
            placeholder={hasModel ? '描述要修改的地方…' : '描述你想设计的模型…'}
            aria-label={hasModel ? '描述要修改的地方' : '描述你想设计的模型'}
            value={draft}
            disabled={sendDisabled}
            onChange={(e) => {
              setDraft(e.target.value);
              e.currentTarget.style.height = 'auto';
              e.currentTarget.style.height = `${Math.min(e.currentTarget.scrollHeight, 132)}px`;
            }}
            onKeyDown={onKeyDown}
          />
          <button
            type="button"
            className="composer-send"
            aria-label="发送"
            title="发送（Enter）"
            disabled={!draft.trim() || running || sending || sendDisabled}
            onClick={submit}
          >
            <IconArrowUp size={16} />
          </button>
        </div>
        <div className="composer-foot">
          <span>
            <kbd>Enter</kbd> 发送 · <kbd>Shift</kbd>+<kbd>Enter</kbd> 换行
          </span>
          {running || sending ? (
            <span className="row" style={{ gap: 4 }}>
              <IconLayers size={13} />
              {running ? '正在生成' : '正在提交'}
            </span>
          ) : null}
        </div>
      </div>
    </section>
  );
}
