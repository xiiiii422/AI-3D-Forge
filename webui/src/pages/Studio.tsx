import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { api } from '../api/client';
import type {
  DesignSpec,
  Job,
  JobParam,
  JobResult,
  PrinterContext,
  ProjectSummary,
  UsageInfo,
} from '../api/types';
import { Button } from '../components/Button';
import { ChatPanel, type ChatEntry } from '../components/ChatPanel';
import { ConfirmModal, Modal } from '../components/Modal';
import { ParamsChips } from '../components/ParamsChips';
import { ProjectSidebar } from '../components/ProjectSidebar';
import { useToast } from '../components/Toast';
import {
  IconCube,
  IconFolder,
  IconMaximize,
  IconMinimize,
  IconNozzle,
  IconRotate,
  IconRotate3D,
  IconSliders,
  IconSparkle,
  IconWireframe,
  IconX,
} from '../components/Icons';
import { toFriendlyError } from '../hooks/errors';
import { useJobStream } from '../hooks/useJobStream';
import { usePrinters, trimNum, type PrinterSwitchEvent } from '../hooks/usePrinters';
import { useProjects } from '../hooks/useProjects';
import Viewer3D, { type ModelStats } from '../viewer/Viewer3D';

/** 作业阶段 → 用户看得懂的中文（不出现 SCAD/CLI 之类的技术词） */
const STAGE_LABEL: Record<Job['stage'], string> = {
  queued: '排队中',
  thinking: '正在理解你的需求',
  understanding: '正在整理需求规格',
  awaiting_confirmation: '等你确认需求',
  generating_scad: '正在设计结构',
  validating_scad: '正在检查设计',
  compiling: '正在生成模型',
  analyzing: '正在测量尺寸',
  reviewing: '正在评估可打印性',
  verifying: '正在核对这次改动',
  exporting: '正在导出文件',
  done: '已完成',
  error: '出错了',
  cancelled: '已取消',
};

function newId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function nowIso(): string {
  return new Date().toISOString();
}

/**
 * 卡死看门狗的时间窗（毫秒）。
 *
 * 生产环境固定 2 分钟；**只给测试**留了一个注入点
 * （`window.__AIFORGE_STALL_MS`）：否则要验证「卡死必须能自己解锁、
 * 并且真的把服务端作业停掉」就得在测试里干等 2 分钟 ——
 * 这种「等不起的路径」正是最容易被漏测、也最容易变成砖头的那一类。
 * 后端也有同类的显式开关（`AIFORGE_UI_FIXTURE`）。
 */
function stallWindowMs(): number {
  const injected = (window as unknown as { __AIFORGE_STALL_MS?: unknown }).__AIFORGE_STALL_MS;
  return typeof injected === 'number' && injected > 0 ? injected : 120_000;
}

export default function Studio() {
  const { projectId: routeProjectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const toast = useToast();
  const printers = usePrinters();

  const { projects, loading: projectsLoading, reload, create, rename, remove } = useProjects();

  const [activeId, setActiveId] = useState<string | null>(routeProjectId ?? null);
  const [search, setSearch] = useState('');
  const [entries, setEntries] = useState<ChatEntry[]>([]);
  const [params, setParams] = useState<JobParam[]>([]);
  const [detailLoading, setDetailLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [applyingParams, setApplyingParams] = useState(false);
  /**
   * 需求已发出、还没拿到 job_id 的那一小段时间。
   *
   * 为什么必须有：`running` 靠 `jobId` 算出来，而 `api.createJob()` 是一次网络往返。
   * 旧实现里发送键只看 `running`，于是「按下 Enter 到响应回来」这段空窗期里
   * 发送键仍然是可点的 —— 连按两次（或 Enter 连敲）就会发出两个作业：
   * 后端会用 409 BUSY 挡住第二个（不会真的污染项目），但界面上会多出一条
   * 「还有一次生成没有结束」的错误卡片，看起来像用户自己操作错了。
   *
   * 这里做的是**乐观锁**：按下瞬间就锁，请求结束（成功或失败）再解锁，
   * 这样无论后端多快多慢，界面上都不会出现第二次提交。
   */
  const [sending, setSending] = useState(false);
  /** 同步防重入：state 要等下一次渲染才生效，同一个事件循环里连点两次挡不住 */
  const sendLockRef = useRef(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [viewerRevision, setViewerRevision] = useState<number | undefined>(undefined);
  const [modelStats, setModelStats] = useState<ModelStats | null>(null);
  /** 从项目实测数据里读到的耗材估算（重开项目时用） */
  const [projectUsage, setProjectUsage] = useState<UsageInfo | null>(null);
  const [viewerLoading, setViewerLoading] = useState(false);
  const [viewerError, setViewerError] = useState<string | null>(null);
  const [wireframe, setWireframe] = useState(false);
  const [autoRotate, setAutoRotate] = useState(false);
  const [resetSignal, setResetSignal] = useState(0);
  const [fullscreen, setFullscreen] = useState(false);
  const [renameTarget, setRenameTarget] = useState<ProjectSummary | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<ProjectSummary | null>(null);
  const [busy, setBusy] = useState(false);
  /** 切换机型后弹出的「旧模型是按别的机器生成的」提醒 */
  const [printerSwitch, setPrinterSwitch] = useState<PrinterSwitchEvent | null>(null);

  const stageRef = useRef<HTMLDivElement>(null);
  /** 记录最后一次用户输入，供「重试 / 自动修复」复用 */
  const lastInputRef = useRef<{ text: string; mode: 'create' | 'modify' } | null>(null);
  const activeIdRef = useRef<string | null>(activeId);
  activeIdRef.current = activeId;

  const activeProject = useMemo(
    () => projects.find((p) => p.id === activeId) ?? null,
    [projects, activeId],
  );

  /** 把结果卡片推进对话，并同步参数表与预览版本 */
  const commitResult = useCallback((result: JobResult) => {
    setEntries((prev) => [
      ...prev,
      { id: newId(), role: 'assistant', text: '', at: nowIso(), result },
    ]);
    setParams(result.params ?? []);
    setViewerRevision(result.revision);
    setActiveId(result.project_id);
  }, []);

  const job = useJobStream({
    jobId,
    onDone: (finished) => {
      setJobId(null);
      setApplyingParams(false);
      if (finished.status === 'done' && finished.result) {
        commitResult(finished.result);
        void reload();
      } else if (finished.status === 'cancelled') {
        // 用户主动停止：不推错误卡片，但必须让对话框留下一条明确的收尾记录，
        // 否则用户会以为「是不是还在跑」。
        setEntries((prev) => [
          ...prev,
          {
            id: newId(),
            role: 'assistant',
            text: '已停止生成。可以继续描述你的需求。',
            at: nowIso(),
          },
        ]);
        void reload();
      } else if (finished.status === 'error') {
        const err = finished.error;
        setEntries((prev) => [
          ...prev,
          {
            id: newId(),
            role: 'assistant',
            text: '',
            at: nowIso(),
            error: {
              message: err?.message ?? '生成失败，请重试。',
              hint: err?.hint ?? null,
              code: err?.code ?? 'INTERNAL',
            },
          },
        ]);
        void reload();
      }
    },
    onError: (message) => {
      setApplyingParams(false);
      toast.error(message);
    },
  });

  const running = job.job?.status === 'running' || (jobId !== null && !job.finished);
  /** 规格确认卡片：作业在等服务端确认时出现；确认后仍保留为只读回顾 */
  const pendingSpec = job.spec;
  const [specBusy, setSpecBusy] = useState(false);

  /**
   * 卡死看门狗。
   *
   * 为什么需要它：发送键的禁用条件是 `running`，而 `running` 依赖
   * 「作业状态」和「是否收到终止事件」。只要后端因为任何原因（bug、断网、
   * 事件丢失、未来的新事件没被前端认识）没有把作业推进到终态，
   * 用户就会被**永久锁在不能输入的状态**里 —— 这是最糟的一类故障，
   * 因为它把整个软件变成砖头，而且用户除了重启没有别的出路。
   *
   * 实测过一次：`cancelled` 事件漏了分支，用户点停止后发送键再也点不动。
   * 与其只修那一处，这里补一条「上游无论怎么坏，界面都能自己解锁」的底线：
   * 只要运行中且**长时间没有任何新消息/阶段变化**，就主动收尾并如实告知。
   */
  const PROGRESS_STALL_MS = stallWindowMs();
  const progressRef = useRef({ at: Date.now(), marker: '' });
  useEffect(() => {
    if (!running) return;
    // 状态也算「进展」：作业从「等待确认」被确认后转入 running 时，
    // 必须重新开始计时，否则会把一个刚刚恢复的作业立刻当成卡死取消掉。
    const marker = `${job.job?.status ?? ''}|${job.stage ?? ''}|${job.percent}|${job.job?.messages.length ?? 0}`;
    if (progressRef.current.marker !== marker) {
      progressRef.current = { at: Date.now(), marker };
    }
  }, [running, job.stage, job.percent, job.job?.messages.length, job.job?.status]);

  useEffect(() => {
    if (!running || !jobId) return;
    // 「停在确认卡片上等人」不是卡死：作业没坏，界面上有确认/取消可点，
    // 用户可能只是去接杯水。把它当卡死会谎报「生成卡住了」，
    // 更糟的是会顺手取消掉一个完全正常的作业。
    if (job.job?.status === 'awaiting_confirmation') return;
    const timer = window.setInterval(() => {
      if (Date.now() - progressRef.current.at < PROGRESS_STALL_MS) return;
      progressRef.current = { at: Date.now(), marker: '' };
      const stalled = jobId;
      setJobId(null);
      setApplyingParams(false);
      /**
       * 必须**真的**把服务端那个作业停掉，不能只解锁界面。
       *
       * 旧实现只做了 `setJobId(null)`，却对用户说「已经为你停止」——
       * 服务端的作业还挂在 running 上，于是：
       *   * 用户被告知的事情是假的；
       *   * 那个项目被这个幽灵作业一直占着（`reject_if_busy` 会返回 409），
       *     用户接下来每一条需求都发不出去，只能重启软件。
       * 界面解锁绝不能等这个请求（本地服务可能正是卡住的那个），
       * 所以发出去就不管结果；能不能停成功，都不影响解锁。
       */
      void api.cancelJob(stalled).catch(() => undefined);
      setEntries((prev) => [
        ...prev,
        {
          id: newId(),
          role: 'assistant',
          text: '这次生成长时间没有任何进展，已经为你停止，可以直接重试。',
          at: nowIso(),
        },
      ]);
      toast.warn('生成卡住了，已自动停止。请重试。');
    }, 5000);
    return () => window.clearInterval(timer);
  }, [running, jobId, toast, job.job?.status]);

  /**
   * 发送锁的看门狗 —— 和上面那条是同一个道理：锁必须能自己解开。
   *
   * `api.request()` 没有超时设置。如果本地服务「接了连接但不回包」（进程卡死、
   * 代理吞包、休眠唤醒后 socket 半死），`await api.createJob()` 永远不会 settle，
   * 于是 `finally` 永远不执行，发送键就**永久禁用**了 ——
   * 重复提交只是浪费额度，永久锁死是把软件变成砖头，后者严重得多。
   *
   * 所以给锁一个上限：到点就解锁并如实告知。真正的重复提交仍然由后端 409 兜住，
   * 而且这里**不会**取消请求 —— 万一请求其实已经送达，作业照常跑，
   * 它的响应回来时依旧会接管成正常流程。
   */
  const SEND_LOCK_MAX_MS = 30_000;
  useEffect(() => {
    if (!sending) return;
    const timer = window.setTimeout(() => {
      sendLockRef.current = false;
      setSending(false);
      toast.warn('提交请求一直没有回应，已经为你解锁，可以直接重试。');
    }, SEND_LOCK_MAX_MS);
    return () => window.clearTimeout(timer);
  }, [sending, toast]);

  const handleConfirmSpec = useCallback(
    async (spec: DesignSpec | null, note: string) => {
      const id = jobId;
      if (!id) return;
      setSpecBusy(true);
      try {
        await api.confirmSpec(id, { spec, note: note || null });
        if (spec) {
          setEntries((prev) => [
            ...prev,
            {
              id: newId(),
              role: 'user',
              text: `需求确认：已按我改的规格设计${note ? `（补充：${note}）` : ''}`,
              at: nowIso(),
              kind: 'message',
            },
          ]);
        }
      } catch (err) {
        toast.error(toFriendlyError(err).message);
      } finally {
        setSpecBusy(false);
      }
    },
    [jobId, toast],
  );

  /* -------------------- 路由与项目同步 -------------------- */
  useEffect(() => {
    setActiveId(routeProjectId ?? null);
  }, [routeProjectId]);

  /** 打开项目：载入详情 → 重建对话与参数 */
  const openProject = useCallback(
    async (id: string) => {
      setActiveId(id);
      navigate(`/studio/${id}`, { replace: false });
      setEntries([]);
      setParams([]);
      setModelStats(null);
      setDetailLoading(true);
      try {
        const detail = await api.getProject(id);
        const last = detail.history?.[detail.history.length - 1];
        if (last || detail.summary) {
          const parts: string[] = [];
          if (last?.prompt) parts.push(`需求：${last.prompt}`);
          if (last?.instruction) parts.push(`修改：${last.instruction}`);
          const early = parts.join('\n');
          setEntries([
            {
              id: newId(),
              role: 'assistant',
              text: `${early ? `${early}\n\n` : ''}${detail.summary ?? '这个项目已经生成过模型。'}`,
              at: last?.created_at ?? detail.updated_at,
            },
          ]);
        }
        setParams(detail.params ?? []);
        setViewerRevision(detail.revision);
        // 耗材估算挂在项目实测数据上（metrics.usage），重开项目也要能看到 ——
        // 它不能只存在于「本次会话刚跑完的那个作业结果」里，
        // 否则每次重新打开软件，用户就看不到这个数字了。
        setProjectUsage(detail.metrics?.usage ?? null);
      } catch (err) {
        toast.error(toFriendlyError(err).message);
      } finally {
        setDetailLoading(false);
      }
    },
    [navigate, toast],
  );

  // 直接从 URL 进入某个项目时载入一次
  const bootstrappedRef = useRef<string | null>(null);
  useEffect(() => {
    if (!routeProjectId) return;
    if (bootstrappedRef.current === routeProjectId) return;
    bootstrappedRef.current = routeProjectId;
    void openProject(routeProjectId);
  }, [routeProjectId, openProject]);

  /* -------------------- 发送 / 重试 -------------------- */
  const startJob = useCallback(
    async (options: { text: string; mode: 'create' | 'modify' }) => {
      // 乐观锁的第一道闸：同一个事件循环里（或上一次请求还没回来时）再点一次直接丢弃。
      // 放在最前面是刻意的 —— 连点两次不应该在对话里留下两条一样的用户气泡，
      // 也不该覆盖 lastInputRef（那会破坏「重试」用的原始输入）。
      if (sendLockRef.current) return;
      sendLockRef.current = true;
      setSending(true);

      const { text, mode } = options;
      lastInputRef.current = options;
      setEntries((prev) => [
        ...prev,
        { id: newId(), role: 'user', text, at: nowIso(), kind: 'message' },
      ]);
      try {
        const ref = await api.createJob(
          mode === 'modify' && activeId
            ? { prompt: text, project_id: activeId, instruction: text, mode: 'modify' }
            : { prompt: text, project_id: null, mode: 'create' },
        );
        setActiveId(ref.project_id);
        if (ref.project_id !== activeId) {
          bootstrappedRef.current = ref.project_id;
          navigate(`/studio/${ref.project_id}`, { replace: true });
        }
        setJobId(ref.job_id);
      } catch (err) {
        const friendly = toFriendlyError(err);
        setEntries((prev) => [
          ...prev,
          {
            id: newId(),
            role: 'assistant',
            text: '',
            at: nowIso(),
            error: { message: friendly.message, hint: friendly.hint, code: friendly.code },
          },
        ]);
      } finally {
        // 成功时 jobId 已就位（running 接管锁定），失败时这里负责解锁 —— 两种情况都必须清
        sendLockRef.current = false;
        setSending(false);
      }
    },
    [activeId, navigate],
  );

  const handleSend = useCallback(
    (text: string) => {
      // 已有模型 → 修改模式；否则从零设计
      const mode: 'create' | 'modify' = viewerRevision !== undefined ? 'modify' : 'create';
      void startJob({ text, mode });
    },
    [startJob, viewerRevision],
  );

  const handleErrorAction = useCallback(
    (entry: ChatEntry) => {
      const code = entry.error?.code;
      if (code === 'NO_API_KEY' || code === 'API_KEY_INVALID') {
        navigate('/settings');
        return;
      }
      if (code === 'OPENSCAD_MISSING') {
        navigate('/welcome');
        return;
      }
      const last = lastInputRef.current;
      if (!last) {
        toast.show('请重新描述你的需求。');
        return;
      }
      const text =
        code === 'OPENSCAD_FAILED' || code === 'SCAD_INVALID'
          ? `${last.text}\n（上一次生成失败，请修正后重新生成）`
          : last.text;
      void startJob({ text, mode: last.mode });
    },
    [navigate, startJob, toast],
  );

  /* -------------------- 机型切换提醒 -------------------- */
  /** 当前项目里最后一次生成用的机器（可能没有：早期项目或纯参数微调） */
  const lastResultPrinter = useMemo<PrinterContext | null>(() => {
    for (let i = entries.length - 1; i >= 0; i -= 1) {
      const entry = entries[i];
      const printer = entry.result?.printer ?? entry.result?.printability?.printer ?? null;
      if (printer) return printer;
    }
    return null;
  }, [entries]);

  /**
   * 耗材用量估算。
   * 两个来源缺一不可：
   *   1) 刚跑完的作业结果（result.stl.usage）—— 生成后立刻能看到；
   *   2) 项目里持久化的实测数据（detail.metrics.usage）—— 重开软件后还能看到。
   * 只取其中任何一个都会漏场景：只取 1 刷新就没了，只取 2 生成完还要重新拉一次。
   */
  const lastUsage = useMemo(() => {
    for (let i = entries.length - 1; i >= 0; i -= 1) {
      const usage = entries[i].result?.stl?.usage;
      if (usage) return usage;
    }
    return projectUsage;
  }, [entries, projectUsage]);

  const handledSwitchRef = useRef(0);
  useEffect(() => {
    const event = printers.lastSwitch;
    if (!event || handledSwitchRef.current === event.at) return;
    handledSwitchRef.current = event.at;
    if (!activeIdRef.current) return;
    // revision > 0 才说明这个项目里已经有模型（项目列表与预览版本取较大值更稳）
    const revision = Math.max(activeProject?.revision ?? 0, viewerRevision ?? 0);
    if (revision <= 0) return;
    // 本来就是同一台机器（例如只是换了材料）就不用打扰用户
    if (lastResultPrinter?.id === event.printer.id) return;
    setPrinterSwitch(event);
  }, [printers.lastSwitch, activeProject, viewerRevision, lastResultPrinter]);

  /** 按新机型重新生成：复用上一次的需求描述，没有就发一条通用的改机指令 */
  const handleRegenerateForPrinter = useCallback(() => {
    const event = printerSwitch;
    setPrinterSwitch(null);
    if (!event) return;
    const text =
      lastInputRef.current?.text ??
      `按新的打印机（${event.printer.name}）重新设计这个模型，用途和外形保持不变，尺寸与壁厚要符合新机型的要求。`;
    void startJob({ text, mode: activeIdRef.current ? 'modify' : 'create' });
  }, [printerSwitch, startJob]);

  const handleApplyParams = useCallback(
    async (values: Record<string, number>) => {
      const id = activeIdRef.current;
      if (!id) return;
      setApplyingParams(true);
      const labels = params
        .filter((p) => values[p.name] !== undefined)
        .map((p) => `${p.label} → ${values[p.name]}${p.unit ?? ''}`)
        .join('，');
      setEntries((prev) => [
        ...prev,
        {
          id: newId(),
          role: 'user',
          text: `调整参数：${labels}`,
          at: nowIso(),
          kind: 'params',
        },
      ]);
      try {
        const ref = await api.updateParams(id, { params: values });
        setJobId(ref.job_id);
      } catch (err) {
        setApplyingParams(false);
        toast.error(toFriendlyError(err).message);
      }
    },
    [params, toast],
  );

  const handleCancel = useCallback(async () => {
    if (!jobId) return;
    try {
      await api.cancelJob(jobId);
      toast.show('已请求停止生成。');
    } catch (err) {
      toast.error(toFriendlyError(err).message);
    }
  }, [jobId, toast]);

  /* -------------------- 项目操作 -------------------- */
  const handleCreate = useCallback(async () => {
    setCreating(true);
    try {
      const created = await create(`未命名项目 ${new Date().getMonth() + 1}/${new Date().getDate()}`);
      if (created) {
        setEntries([]);
        setParams([]);
        setModelStats(null);
        setViewerRevision(undefined);
        setActiveId(created.id);
        bootstrappedRef.current = created.id;
        navigate(`/studio/${created.id}`);
        toast.ok('已新建项目，在右侧描述你想要的模型。');
      }
    } finally {
      setCreating(false);
    }
  }, [create, navigate, toast]);

  const handleConfirmRename = useCallback(async () => {
    if (!renameTarget) return;
    const name = renameValue.trim();
    if (!name) {
      toast.warn('名称不能为空。');
      return;
    }
    setBusy(true);
    const updated = await rename(renameTarget.id, name);
    setBusy(false);
    setRenameTarget(null);
    if (updated) toast.ok('已重命名。');
  }, [rename, renameTarget, renameValue, toast]);

  const handleConfirmDelete = useCallback(async () => {
    if (!deleteTarget) return;
    setBusy(true);
    const ok = await remove(deleteTarget.id);
    setBusy(false);
    setDeleteTarget(null);
    if (ok) {
      toast.ok('项目已移入回收站。');
      if (deleteTarget.id === activeId) {
        setEntries([]);
        setParams([]);
        setModelStats(null);
        setViewerRevision(undefined);
        setActiveId(null);
        navigate('/studio');
      }
    }
  }, [remove, deleteTarget, activeId, navigate, toast]);

  const handleOpenFolder = useCallback(
    async (project: ProjectSummary | null) => {
      try {
        await api.openFolder(
          project ? { kind: 'project', project_id: project.id } : { kind: 'projects' },
        );
      } catch (err) {
        toast.error(toFriendlyError(err).message);
      }
    },
    [toast],
  );

  /* -------------------- 全屏 -------------------- */
  const toggleFullscreen = useCallback(async () => {
    const el = stageRef.current;
    if (!el) return;
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await el.requestFullscreen();
    } catch {
      toast.warn('当前窗口无法进入全屏。');
    }
  }, [toast]);

  useEffect(() => {
    const onChange = () => setFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener('fullscreenchange', onChange);
    return () => document.removeEventListener('fullscreenchange', onChange);
  }, []);

  const percentForBar = running ? (job.percent > 0 ? job.percent : null) : null;
  const hasModel = viewerRevision !== undefined || modelStats !== null;

  return (
    <div className="studio">
      {/* 工作台没有可见的页面大标题（三栏布局：项目栏 / 预览台 / 对话），
          但每页必须有一个 h1 作为大纲的根 —— 只给读屏软件的隐藏标题。
          better-accessibility：Structure is navigation。 */}
      <h1 className="sr-only">工作台</h1>
      <ProjectSidebar
        projects={projects}
        loading={projectsLoading}
        activeId={activeId}
        search={search}
        onSearch={setSearch}
        onOpen={(p) => void openProject(p.id)}
        onCreate={() => void handleCreate()}
        creating={creating}
        onRename={(p) => {
          setRenameTarget(p);
          setRenameValue(p.name);
        }}
        onDelete={(p) => setDeleteTarget(p)}
        onOpenFolder={(p) => void handleOpenFolder(p)}
      />

      <div className="studio-center">
        <div className="viewer-toolbar">
          <div className="viewer-toolbar-title">
            <strong title={activeProject?.name ?? '新的设计'}>
              {activeProject?.name ?? (detailLoading ? '正在载入…' : '新的设计')}
            </strong>
            <span className="muted-3 xs">
              {activeProject
                ? `第 ${activeProject.revision} 版 · ${projects.length} 个项目`
                : '在右侧描述你想设计的模型'}
            </span>
          </div>
          <div className="viewer-tools">
            <Button
              iconOnly
              variant="ghost"
              aria-label="重置视角"
              title="重置视角"
              onClick={() => setResetSignal((n) => n + 1)}
            >
              <IconRotate size={17} />
            </Button>
            <Button
              iconOnly
              variant="ghost"
              className={wireframe ? 'is-active' : undefined}
              aria-label="线框切换"
              aria-pressed={wireframe}
              title="线框切换"
              onClick={() => setWireframe((v) => !v)}
            >
              <IconWireframe size={17} />
            </Button>
            <Button
              iconOnly
              variant="ghost"
              className={autoRotate ? 'is-active' : undefined}
              aria-label="自动旋转"
              aria-pressed={autoRotate}
              title="自动旋转"
              onClick={() => setAutoRotate((v) => !v)}
            >
              <IconRotate3D size={17} />
            </Button>
            <Button
              iconOnly
              variant="ghost"
              aria-label={fullscreen ? '退出全屏' : '全屏'}
              title={fullscreen ? '退出全屏' : '全屏'}
              onClick={() => void toggleFullscreen()}
            >
              {fullscreen ? <IconMinimize size={17} /> : <IconMaximize size={17} />}
            </Button>
          </div>
        </div>

        <div className="viewer-stage" ref={stageRef}>
          <Viewer3D
            projectId={activeId && hasModel ? activeId : null}
            revision={viewerRevision}
            wireframe={wireframe}
            autoRotate={autoRotate}
            resetSignal={resetSignal}
            onStats={setModelStats}
            onLoadingChange={setViewerLoading}
            onError={setViewerError}
          />

          {/* 模型数据浮层 */}
          {modelStats ? (
            <div className="viewer-overlay viewer-overlay-stats">
              <div className="stat">
                <div className="stat-label">尺寸（毫米）</div>
                <div className="stat-value">
                  {modelStats.size.x.toFixed(1)} × {modelStats.size.y.toFixed(1)} ×{' '}
                  {modelStats.size.z.toFixed(1)}
                </div>
              </div>
              <div className="stat">
                <div className="stat-label">三角面数</div>
                <div className="stat-value">{modelStats.triangles.toLocaleString('zh-CN')}</div>
              </div>
              <div className="stat">
                <div className="stat-label">体积</div>
                <div className="stat-value">
                  {(modelStats.volumeMm3 / 1000).toLocaleString('zh-CN', {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2,
                  })}{' '}
                  cm³
                </div>
              </div>
              {lastUsage ? (
                <div className="stat">
                  <div className="stat-label">预计耗材（{lastUsage.material}）</div>
                  <div className="stat-value">
                    {lastUsage.total_grams.toFixed(0)}–{lastUsage.total_grams_upper.toFixed(0)} g
                  </div>
                  <div className="stat-note">
                    约 {lastUsage.filament_length_m.toFixed(1)} m 线材
                    {lastUsage.support_grams > 0
                      ? `｜含支撑 ${lastUsage.support_grams.toFixed(0)} g`
                      : ''}
                  </div>
                </div>
              ) : null}
            </div>
          ) : null}

          {/* 空状态 */}
          {!hasModel && !viewerLoading ? (
            <div className="viewer-empty">
              <span className="viewer-empty-mark">
                <IconCube size={26} />
              </span>
              <h3>还没有模型</h3>
              <p className="small" style={{ maxWidth: '30em' }}>
                在右侧描述你想设计的模型，生成完成后这里会显示可旋转、可缩放的三维预览。
              </p>
              <span className="tag">
                <IconSparkle size={13} style={{ marginRight: 4 }} />
                试试「设计一个桌面手机支架，高度100mm」
              </span>
            </div>
          ) : null}

          {viewerLoading ? <div className="viewer-loading">正在载入模型预览…</div> : null}

          {viewerError ? (
            <div className="viewer-overlay viewer-overlay-top" style={{ maxWidth: 320 }}>
              <div className="row" style={{ alignItems: 'flex-start' }}>
                <IconX size={15} style={{ color: 'var(--danger)', marginTop: 2 }} />
                <span className="small">{viewerError}</span>
              </div>
            </div>
          ) : null}
        </div>

        {/* 参数微调 */}
        {params.length > 0 ? (
          <ParamsChips
            params={params}
            disabled={running || sending}
            applying={applyingParams}
            onApply={(values) => void handleApplyParams(values)}
          />
        ) : (
          <section className="params-sheet" aria-label="参数微调">
            <div className="params-head" style={{ marginBottom: 0 }}>
              <div className="params-title">
                <IconSliders size={14} style={{ verticalAlign: -2, marginRight: 6 }} />
                参数微调
                <span>生成完成后，常用尺寸会出现在这里，改数字即可重新编译</span>
              </div>
              {hasModel ? (
                <Button
                  size="sm"
                  variant="ghost"
                  icon={<IconFolder size={14} />}
                  onClick={() => void handleOpenFolder(activeProject)}
                >
                  打开文件夹
                </Button>
              ) : null}
            </div>
          </section>
        )}
      </div>

      <ChatPanel
        entries={entries}
        running={running}
        sending={sending}
        statusText={job.runMessage}
        percent={percentForBar}
        stageLabel={job.stage ? STAGE_LABEL[job.stage] : ''}
        hasProject={Boolean(activeId)}
        hasModel={hasModel}
        onSend={handleSend}
        onCancel={() => void handleCancel()}
        onErrorAction={handleErrorAction}
        pendingSpec={pendingSpec}
        specBusy={specBusy}
        onConfirmSpec={(spec, note) => void handleConfirmSpec(spec, note)}
        onCancelSpec={() => void handleCancel()}
      />

      {/* 重命名 */}
      <Modal
        open={renameTarget !== null}
        title="重命名项目"
        onClose={() => setRenameTarget(null)}
        footer={
          <>
            <Button onClick={() => setRenameTarget(null)}>取消</Button>
            <Button variant="primary" loading={busy} onClick={() => void handleConfirmRename()}>
              保存
            </Button>
          </>
        }
      >
        <div className="field">
          <label className="field-label" htmlFor="rename-input">
            项目名称
          </label>
          <input
            id="rename-input"
            className="input"
            value={renameValue}
            maxLength={60}
            onChange={(e) => setRenameValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void handleConfirmRename();
            }}
          />
        </div>
      </Modal>

      {/* 删除确认 */}
      <ConfirmModal
        open={deleteTarget !== null}
        title="删除项目"
        danger
        busy={busy}
        message={
          <>
            将把「{deleteTarget?.name}」移入回收站，之后可以从回收站恢复。确定要删除吗？
          </>
        }
        confirmText="删除"
        onCancel={() => setDeleteTarget(null)}
        onConfirm={() => void handleConfirmDelete()}
      />

      {/* 换了打印机之后的「旧模型要不要重做」提醒 */}
      <Modal
        open={printerSwitch !== null}
        title="已切换到新机型"
        onClose={() => setPrinterSwitch(null)}
        footer={
          <>
            <Button onClick={() => setPrinterSwitch(null)}>稍后再说</Button>
            <Button
              variant="primary"
              icon={<IconSparkle size={15} />}
              onClick={() => void handleRegenerateForPrinter()}
            >
              按新机型重新生成
            </Button>
          </>
        }
      >
        <div className="muted">
          {lastResultPrinter ? (
            <>
              当前项目「{activeProject?.name ?? '未命名项目'}」里的模型是按
              <strong>「{lastResultPrinter.name}」</strong>生成的，和刚选的
              <strong>「{printerSwitch?.printer.name}」</strong>不是同一台机器。
            </>
          ) : (
            <>
              当前项目里的模型是按之前的打印机生成的，和刚选的
              <strong>「{printerSwitch?.printer.name}」</strong>不是同一台机器。
            </>
          )}
          <br />
          换机后喷头直径、成型范围、最小壁厚都可能不一样，旧模型不会自动改变。
          需要适配的话，可以让 AI 按新机型重新生成一次。
        </div>
        {printerSwitch ? (
          <ul className="rule-list rule-list-grid" style={{ marginTop: 'var(--sp-3)' }}>
            <li>
              喷嘴 {trimNum(printerSwitch.printer.nozzle_mm, 2)}mm · 成型范围{' '}
              {Math.round(printerSwitch.printer.build_x)}×{Math.round(printerSwitch.printer.build_y)}
              ×{Math.round(printerSwitch.printer.build_z)}mm
            </li>
            <li>
              最小壁厚 {trimNum(printerSwitch.printer.constraints.min_wall_mm, 2)}mm
            </li>
            <li>
              <span className="row" style={{ gap: 4 }}>
                <IconNozzle size={13} />
                当前材料 {printerSwitch.material}
              </span>
            </li>
          </ul>
        ) : null}
      </Modal>
    </div>
  );
}
