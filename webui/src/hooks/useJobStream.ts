import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, api, parseSseChunk, parseSseData, type SseFrame } from '../api/client';
import type {
  Job,
  JobMessage,
  JobResult,
  JobStage,
  SpecEventPayload,
} from '../api/types';

/** 流状态：streaming=SSE 在线 / polling=已降级为轮询 / idle=未订阅 / settled=已结束 */
export type JobStreamMode = 'idle' | 'streaming' | 'polling' | 'settled';

/** 轮询兜底间隔（契约要求 1.5s） */
const POLL_INTERVAL_MS = 1500;
/** SSE 最多重连 1 次，两次失败即降级轮询 */
const MAX_SSE_RETRIES = 1;
/** 单次读取的兜底超时（防止连接被静默挂起） */
const READ_TIMEOUT_MS = 45000;
/**
 * 轮询连续失败到多少次就放弃（约 12 秒）。
 *
 * 为什么必须有上限：轮询是「SSE 连不上」之后的兜底，而连不上的原因
 * 很可能是**本地服务已经不在了**（进程被杀、端口被占、系统休眠后没恢复）。
 * 这种情况下继续轮询永远不会成功，界面却会一直是「生成中」+ 发送键禁用 ——
 * 用户看到的是「卡住了」，真实原因是「失联了」，两者该给的提示完全不同。
 */
const MAX_POLL_FAILURES = 8;

export interface UseJobStreamOptions {
  /** 订阅的作业 id；null 表示不订阅 */
  jobId: string | null;
  /** 作业结束时回调（流或轮询任一路径都会触发，只会触发一次） */
  onDone?: (job: Job) => void;
  /** 订阅过程中出错时回调 */
  onError?: (message: string) => void;
}

export interface UseJobStreamResult {
  job: Job | null;
  mode: JobStreamMode;
  /** 最新一条面向用户的进度文案（用于单行打字机状态） */
  runMessage: string;
  stage: JobStage | null;
  percent: number;
  /** 是否已结束（done/error/cancelled；等待确认不算结束） */
  finished: boolean;
  result: JobResult | null;
  /** 等待用户确认的设计规格（确认后仍在，awaiting_confirmation 变为 false） */
  spec: SpecEventPayload | null;
  /** 重新建立连接（手动重试） */
  reconnect: () => void;
}

/** 按 seq 合并消息，去重且保证顺序 —— SSE 与轮询兜底可能返回重叠数据 */
function mergeMessages(prev: JobMessage[], next: JobMessage[]): JobMessage[] {
  if (next.length === 0) return prev;
  const bySeq = new Map<number, JobMessage>();
  for (const m of prev) bySeq.set(m.seq, m);
  for (const m of next) bySeq.set(m.seq, m);
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}

/**
 * 作业实时流。
 *
 * 设计要点（对应任务书 Cross-cutting 要求）：
 *  1. 先 `GET /api/jobs/{id}` 拉一次完整快照，页面重连/刷新后能立刻补齐状态；
 *  2. 用 fetch + ReadableStream 手动解析 SSE（不用 EventSource：需要自定义头与更细的错误控制）；
 *  3. SSE 意外断开只重连一次，连续两次失败降级为每 1.5s 轮询 `GET /api/jobs/{id}`，直到作业结束。
 */
export function useJobStream({
  jobId,
  onDone,
  onError,
}: UseJobStreamOptions): UseJobStreamResult {
  const [job, setJob] = useState<Job | null>(null);
  const [mode, setMode] = useState<JobStreamMode>('idle');

  // 回调放进 ref，避免因为父组件每次渲染新建函数而重开连接
  const onDoneRef = useRef(onDone);
  const onErrorRef = useRef(onError);
  useEffect(() => {
    onDoneRef.current = onDone;
    onErrorRef.current = onError;
  }, [onDone, onError]);

  const [generation, setGeneration] = useState(0);
  const reconnect = useCallback(() => setGeneration((g) => g + 1), []);

  useEffect(() => {
    if (!jobId) {
      setJob(null);
      setMode('idle');
      return;
    }

    let disposed = false;
    let timer: number | undefined;
    let pollTimer: number | undefined;
    let abort: AbortController | null = null;
    let doneFired = false;

    const clearTimers = () => {
      if (timer !== undefined) window.clearTimeout(timer);
      if (pollTimer !== undefined) window.clearTimeout(pollTimer);
      timer = undefined;
      pollTimer = undefined;
    };

    /** 应用一次作业快照；作业结束则回调 onDone 并停表 */
    const applyJob = (next: Job): boolean => {
      if (disposed) return true;
      // awaiting_confirmation 不是终态：作业在等服务端等用户确认规格，
      // 若当成结束处理，确认卡片会立刻被丢掉、轮询也会停，用户就卡死了。
      const finished = next.status !== 'running' && next.status !== 'awaiting_confirmation';
      setJob((prev) => {
        const sameJob = prev && prev.job_id === next.job_id;
        return {
          ...next,
          // 消息按 seq 合并，避免流与轮询之间的重复/乱序
          messages: mergeMessages(sameJob ? prev.messages : [], next.messages ?? []),
          // 后端在某帧里省略 result 时保留上一次已拿到的结果
          result: next.result ?? (sameJob ? prev.result : null),
          // 同上：快照没带 spec 时保留已收到的规格，避免卡片闪没
          spec: next.spec ?? (sameJob ? prev.spec : null),
        };
      });
      if (finished) {
        clearTimers();
        setMode('settled');
        if (!doneFired) {
          doneFired = true;
          onDoneRef.current?.(next);
        }
        return true;
      }
      return false;
    };

    /** 轮询兜底：每 1.5s 拉一次，直到结束或组件卸载 */
    const startPolling = () => {
      if (disposed) return;
      setMode('polling');
      let failures = 0;
      const poll = async () => {
        if (disposed) return;
        let finished = false;
        try {
          const next = await api.getJob(jobId);
          failures = 0;
          finished = applyJob(next);
        } catch (err) {
          // 作业已经不在服务端了（被回收/过期）：再轮询也不会拿到结果，
          // 必须立刻收尾，否则界面会一直转圈、发送键一直禁用。
          if (err instanceof ApiError && err.status === 404) {
            settleUnreachable('这次生成任务在服务端已经不存在了（可能已过期），请重新生成。');
            return;
          }
          failures += 1;
          // 连续失败到上限 → 判定为「与本地服务失联」，如实告知并解锁
          if (failures >= MAX_POLL_FAILURES) {
            settleUnreachable('与本地服务的连接中断了，已经停止跟踪这次生成。请刷新页面后重试。');
            return;
          }
        }
        if (!disposed && !finished) {
          pollTimer = window.setTimeout(poll, POLL_INTERVAL_MS);
        }
      };
      void poll();
    };

    /**
     * 无法继续跟踪这次作业时的统一收尾。
     * 必须同时做三件事：把 job 置成终态（界面据此解锁输入区）、
     * 把流状态置成 settled（避免再重连）、并如实告知原因 ——
     * 少了任何一件，用户看到的就是一个永远转圈、点不动发送键的界面。
     *
     * 注意 job 可能是 **null**：如果连快照都没拉到（例如作业在服务端已经不存在，
     * GET 直接 404），那么从来没成功 applyJob 过。此时必须**构造**一个终态 Job，
     * 而不是 `prev ? {...} : prev` —— 后者在 prev 为 null 时是空操作，
     * 界面会一直停在「生成中」、发送键一直禁用（实测就是这个表现）。
     */
    const settleUnreachable = (message: string) => {
      if (disposed) return;
      setJob((prev) => {
        const now = new Date().toISOString();
        return {
          job_id: prev?.job_id ?? jobId,
          project_id: prev?.project_id ?? '',
          revision: prev?.revision ?? 0,
          status: 'error',
          stage: 'error',
          percent: 0,
          messages: prev?.messages ?? [],
          error: { message, hint: null, code: 'STREAM_LOST' },
          result: prev?.result ?? null,
          spec: prev?.spec ?? null,
          created_at: prev?.created_at ?? now,
          finished_at: now,
        };
      });
      setMode('settled');
      onErrorRef.current?.(message);
    };

    const abortTimer = () => {
      if (timer !== undefined) {
        window.clearTimeout(timer);
        timer = undefined;
      }
    };

    /** 打开 SSE 流；返回 'ended'（正常收尾）/ 'retry'（可重连）/ 'giveup'（降级轮询） */
    const openStream = async (): Promise<'ended' | 'retry' | 'giveup'> => {
      abort = new AbortController();
      abortTimer();
      try {
        const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/stream`, {
          headers: { Accept: 'text/event-stream' },
          signal: abort.signal,
          cache: 'no-store',
        });
        if (!res.ok || !res.body) {
          const text = await res.text().catch(() => '');
          throw new Error(text || `HTTP ${res.status}`);
        }

        setMode('streaming');
        const reader = res.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';
        let settled = false;

        const handleFrame = (frame: SseFrame): void => {
          const payload = parseSseData<Record<string, unknown>>(frame.data);
          if (!payload) return;
          switch (frame.event) {
            case 'message': {
              const msg = payload as unknown as JobMessage;
              if (typeof msg.seq !== 'number' || typeof msg.text !== 'string') return;
              setJob((prev) =>
                prev
                  ? { ...prev, messages: mergeMessages(prev.messages, [msg]) }
                  : prev,
              );
              break;
            }
            case 'stage': {
              const stage = payload.stage as JobStage | undefined;
              const percent = payload.percent as number | undefined;
              setJob((prev) =>
                prev
                  ? {
                      ...prev,
                      stage: stage ?? prev.stage,
                      percent: typeof percent === 'number' ? percent : prev.percent,
                    }
                  : prev,
              );
              break;
            }
            case 'spec': {
              // 规格卡片：用户确认前必须看得见、可纠正
              const incoming = payload.spec as SpecEventPayload['spec'] | undefined;
              if (!incoming) return;
              setJob((prev) =>
                prev
                  ? {
                      ...prev,
                      spec: {
                        spec: incoming,
                        awaiting_confirmation: payload.awaiting_confirmation !== false,
                      },
                    }
                  : prev,
              );
              break;
            }
            case 'done': {
              settled = true;
              // 契约：done 的 data 是完整 Job（含 status/result/error）
              const next = payload as unknown as Job;
              if (typeof next.status === 'string' && Array.isArray(next.messages)) {
                applyJob(next);
              } else {
                // 宽松兜底：后端若只推了 JobResult，也当作完成处理
                const result = payload as unknown as JobResult;
                setJob((prev) => {
                  if (!prev) return prev;
                  const merged: Job = {
                    ...prev,
                    status: 'done',
                    stage: 'done',
                    percent: 100,
                    result,
                  };
                  if (!doneFired) {
                    doneFired = true;
                    onDoneRef.current?.(merged);
                  }
                  return merged;
                });
                clearTimers();
                setMode('settled');
              }
              break;
            }
            case 'cancelled': {
              // 「停止生成」的收尾事件。契约 §3.3 里 cancelled 与 done/error 一样
              // 是**终止事件**，但这里最初漏了这个分支 —— 后果非常严重：
              // 帧被 default 静默丢掉 → job.status 永远停在 running →
              // `running` 恒为真 → 发送键永久禁用，用户除了刷新页面没别的出路。
              // （已用真实后端复现：SSE 收到 snapshot/message/stage/spec/cancelled，
              //  而界面 45 秒后发送键依旧点不动。）
              settled = true;
              const next = payload as unknown as Job;
              if (typeof next.status === 'string' && Array.isArray(next.messages)) {
                applyJob(next);
              } else {
                // 宽松兜底：后端只推了 {message} 时也要能收尾
                setJob((prev) => {
                  if (!prev) return prev;
                  const merged: Job = { ...prev, status: 'cancelled', stage: 'cancelled' };
                  if (!doneFired) {
                    doneFired = true;
                    onDoneRef.current?.(merged);
                  }
                  return merged;
                });
                clearTimers();
                setMode('settled');
              }
              break;
            }
            case 'error': {
              settled = true;
              const message = (payload.message as string) || '生成失败，请重试。';              setJob((prev) =>
                prev
                  ? {
                      ...prev,
                      status: 'error',
                      stage: 'error',
                      error: {
                        message,
                        hint: (payload.hint as string | null) ?? null,
                        code: (payload.code as string) || 'INTERNAL',
                      },
                    }
                  : prev,
              );
              clearTimers();
              setMode('settled');
              if (!doneFired) {
                doneFired = true;
                onErrorRef.current?.(message);
              }
              break;
            }
            default:
              break;
          }
        };

        // ---- 读取循环 ----
        for (;;) {
          if (disposed) {
            void reader.cancel().catch(() => undefined);
            return 'ended';
          }
          const chunk = await Promise.race([
            reader.read(),
            new Promise<never>((_, reject) => {
              timer = window.setTimeout(
                () => reject(new Error('stream-idle-timeout')),
                READ_TIMEOUT_MS,
              );
            }),
          ]);
          if (timer !== undefined) {
            window.clearTimeout(timer);
            timer = undefined;
          }
          if (chunk.done) break;
          buffer += decoder.decode(chunk.value, { stream: true });
          const { frames, rest } = parseSseChunk(buffer);
          buffer = rest;
          for (const frame of frames) handleFrame(frame);
          if (settled) {
            void reader.cancel().catch(() => undefined);
            return 'ended';
          }
        }
        return 'ended';
      } catch (err) {
        if (disposed) return 'ended';
        if (err instanceof DOMException && err.name === 'AbortError') return 'ended';
        if (err instanceof Error && err.message === 'stream-idle-timeout') return 'retry';
        return 'retry';
      } finally {
        abortTimer();
      }
    };

    void (async () => {
      // 1) 先拉快照：断线重连/刷新页面后，这里能重建全部已有消息、阶段与结果
      let snapshot: Job | null = null;
      try {
        snapshot = await api.getJob(jobId);
        if (applyJob(snapshot)) return; // 已经结束，不必开流
      } catch (err) {
        if (disposed) return;
        onErrorRef.current?.(err instanceof Error ? err.message : '无法获取作业状态。');
        // 快照失败仍尝试开流（流可能可用），由重试/降级逻辑兜底
      }

      // 2) 打开 SSE
      let attempt = 0;
      for (;;) {
        const outcome = await openStream();
        if (disposed || outcome === 'ended') return;
        if (outcome === 'retry' && attempt < MAX_SSE_RETRIES) {
          attempt += 1;
          continue;
        }
        // 3) 连续失败 → 降级轮询
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

  const messages = job?.messages ?? [];
  const runMessage = messages.length > 0 ? messages[messages.length - 1].text : '';

  return {
    job,
    mode,
    runMessage,
    stage: job?.stage ?? null,
    percent: job?.percent ?? 0,
    /**
     * 是否已结束（done/error/cancelled；等待确认不算结束）。
     *
     * `mode === 'settled'` 这一条是**兜底**：只要流/轮询已经收尾，
     * 界面就必须解锁 —— 不能依赖 job 对象一定存在。
     * 实测踩到过：作业在服务端不存在时连快照都拿不到（job 始终为 null），
     * 只按 job 判断的话 finished 永远是 false，发送键就一直禁用。
     */
    finished:
      mode === 'settled' ||
      (!!job && job.status !== 'running' && job.status !== 'awaiting_confirmation'),
    result: job?.result ?? null,
    spec: job?.spec ?? null,
    reconnect,
  };
}
