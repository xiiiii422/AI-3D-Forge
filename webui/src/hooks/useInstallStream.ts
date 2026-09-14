import { useCallback, useEffect, useRef, useState } from 'react';
import { parseSseChunk, parseSseData, type SseFrame } from '../api/client';
import type {
  InstallDoneEvent,
  InstallErrorEvent,
  InstallLogEvent,
  InstallProgressEvent,
} from '../api/types';
import { toFriendlyError } from './errors';

/** 安装阶段的中文标签（契约 §2.1 的 stage 取值，用户看不到英文原词） */
const STAGE_LABEL: Record<InstallProgressEvent['stage'], string> = {
  prepare: '准备中',
  download: '正在下载',
  verify: '正在校验',
  install: '正在安装',
  locate: '正在定位',
  cli_test: '正在测试',
  config: '正在写入配置',
  done: '已完成',
};

export interface UseInstallStreamResult {
  /** 0-100；null 表示还不确定进度（细进度条走不确定态） */
  percent: number | null;
  /** 当前阶段的中文标签 */
  stageLabel: string;
  /** 后端下发的实时文案 */
  message: string;
  /** 技术日志（折叠展示，不打扰用户） */
  logs: string[];
  /** 成功结果 */
  done: InstallDoneEvent | null;
  /** 失败结果：message / need_admin / manual_url / manual_steps */
  error: InstallErrorEvent | null;
  running: boolean;
  /** 是否已降级为轮询（调试与文案提示用） */
  degraded: boolean;
}

const POLL_INTERVAL_MS = 1500;
const MAX_SSE_RETRIES = 1;

/**
 * OpenSCAD 自动安装进度流（契约 §2.1）。
 * 与 useJobStream 同构：fetch + ReadableStream 解析 SSE，失败重连一次，仍失败则降级轮询
 * `GET /api/openscad/install/{job_id}`（后端若未实现该轮询端点，会安静地保持最后状态）。
 */
export function useInstallStream(jobId: string | null): UseInstallStreamResult {
  const [percent, setPercent] = useState<number | null>(null);
  const [stageLabel, setStageLabel] = useState('准备中');
  const [message, setMessage] = useState('');
  const [logs, setLogs] = useState<string[]>([]);
  const [done, setDone] = useState<InstallDoneEvent | null>(null);
  const [error, setError] = useState<InstallErrorEvent | null>(null);
  const [running, setRunning] = useState(false);
  const [degraded, setDegraded] = useState(false);
  const [generation, setGeneration] = useState(0);

  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const reset = useCallback(() => {
    setPercent(null);
    setStageLabel('准备中');
    setMessage('');
    setLogs([]);
    setDone(null);
    setError(null);
    setDegraded(false);
    setGeneration((g) => g + 1);
  }, []);
  // 供「重试安装」等入口使用；当前页面通过换 job_id 触发新订阅
  void reset;

  useEffect(() => {
    if (!jobId) {
      setRunning(false);
      return;
    }
    let disposed = false;
    let retryTimer: number | undefined;
    let pollTimer: number | undefined;
    let abort: AbortController | null = null;

    const clearTimers = () => {
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
      if (pollTimer !== undefined) window.clearTimeout(pollTimer);
    };

    setRunning(true);
    setError(null);
    setDone(null);

    const handleFrame = (frame: SseFrame): boolean => {
      const payload = parseSseData<Record<string, unknown>>(frame.data);
      if (!payload) return false;
      switch (frame.event) {
        case 'progress': {
          const ev = payload as unknown as InstallProgressEvent;
          if (typeof ev.percent === 'number') setPercent(Math.max(0, Math.min(100, ev.percent)));
          if (ev.stage) setStageLabel(STAGE_LABEL[ev.stage] ?? '处理中');
          if (ev.message) setMessage(ev.message);
          return false;
        }
        case 'log': {
          const ev = payload as unknown as InstallLogEvent;
          if (ev.message) setLogs((prev) => [...prev.slice(-49), ev.message]);
          return false;
        }
        case 'done': {
          const ev = payload as unknown as InstallDoneEvent;
          setDone(ev);
          setPercent(100);
          setStageLabel(STAGE_LABEL.done);
          setMessage(ev.message || '安装完成。');
          setRunning(false);
          return true;
        }
        case 'error': {
          const ev = payload as unknown as InstallErrorEvent;
          setError(ev);
          setRunning(false);
          return true;
        }
        default:
          return false;
      }
    };

    const startPolling = () => {
      if (disposed) return;
      setDegraded(true);
      const poll = async () => {
        if (disposed) return;
        try {
          const res = await fetch(`/api/openscad/install/${encodeURIComponent(jobId)}`, {
            headers: { Accept: 'application/json' },
          });
          if (res.ok) {
            const data = (await res.json()) as Record<string, unknown>;
            if (typeof data.percent === 'number') setPercent(data.percent);
            if (typeof data.message === 'string') setMessage(data.message);
            if (data.status === 'done') {
              setDone({
                ok: true,
                message: (data.message as string) ?? '安装完成。',
                info: data.info as InstallDoneEvent['info'],
              });
              setRunning(false);
              return;
            }
            if (data.status === 'error') {
              setError({
                ok: false,
                message: (data.message as string) ?? '安装失败。',
                need_admin: Boolean(data.need_admin),
                manual_url: (data.manual_url as string) ?? undefined,
                manual_steps: (data.manual_steps as string[]) ?? undefined,
              });
              setRunning(false);
              return;
            }
          }
        } catch {
          // 轮询失败不致命：继续下一拍
        }
        if (!disposed) pollTimer = window.setTimeout(poll, POLL_INTERVAL_MS);
      };
      void poll();
    };

    const openStream = async (): Promise<'ended' | 'retry'> => {
      abort = new AbortController();
      try {
        const res = await fetch(`/api/openscad/install/${encodeURIComponent(jobId)}/stream`, {
          headers: { Accept: 'text/event-stream' },
          signal: abort.signal,
          cache: 'no-store',
        });
        if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
        const reader = res.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';
        for (;;) {
          if (disposed) {
            void reader.cancel().catch(() => undefined);
            return 'ended';
          }
          const chunk = await reader.read();
          if (chunk.done) break;
          buffer += decoder.decode(chunk.value, { stream: true });
          const { frames, rest } = parseSseChunk(buffer);
          buffer = rest;
          let settled = false;
          for (const frame of frames) {
            if (handleFrame(frame)) settled = true;
          }
          if (settled) {
            void reader.cancel().catch(() => undefined);
            return 'ended';
          }
        }
        return 'ended';
      } catch {
        return 'retry';
      }
    };

    void (async () => {
      let attempt = 0;
      for (;;) {
        const outcome = await openStream();
        if (disposed || outcome === 'ended') return;
        if (attempt < MAX_SSE_RETRIES) {
          attempt += 1;
          await new Promise<void>((resolve) => {
            retryTimer = window.setTimeout(resolve, 600);
          });
          continue;
        }
        startPolling();
        return;
      }
    })();

    return () => {
      disposed = true;
      clearTimers();
      abort?.abort();
    };
  }, [jobId, generation]);

  // 给「安装失败但流没给 message」的极端情况兜一句人话
  const fallbackMessage = error && !error.message ? '安装没有成功，可以按下面的步骤手动完成。' : '';

  return {
    percent,
    stageLabel,
    message: message || fallbackMessage,
    logs: logs.slice(-5),
    done,
    error,
    running,
    degraded,
  };
}

/** 把 browse/locate 的错误也变成人话 */
export function describeError(err: unknown): string {
  return toFriendlyError(err).message;
}
