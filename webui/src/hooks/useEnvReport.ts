import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import type { EnvReport } from '../api/types';
import { toFriendlyError, type FriendlyError } from './errors';

export interface UseEnvReportResult {
  report: EnvReport | null;
  /** 首次加载（还没有任何数据）时为 true —— 用于渲染骨架屏 */
  loading: boolean;
  /** 正在重新检测（已有旧数据仍在展示） */
  detecting: boolean;
  error: FriendlyError | null;
  /** 重新检测；live=true 时后端会额外做联网的 AI 服务可用性测试 */
  refresh: (options?: { live?: boolean }) => Promise<EnvReport | null>;
}

/**
 * 环境检测报告。契约 `GET /api/env/report`。
 * 重新检测时保留旧数据并把 detecting 置位，界面因此不会闪白。
 */
export function useEnvReport(options: { auto?: boolean } = {}): UseEnvReportResult {
  const { auto = true } = options;
  const [report, setReport] = useState<EnvReport | null>(null);
  const [loading, setLoading] = useState(auto);
  const [detecting, setDetecting] = useState(false);
  const [error, setError] = useState<FriendlyError | null>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const refresh = useCallback(async (opts: { live?: boolean } = {}) => {
    setDetecting(true);
    setError(null);
    try {
      const data = await api.envReport({ live: opts.live });
      if (!alive.current) return data;
      setReport(data);
      return data;
    } catch (err) {
      if (!alive.current) return null;
      setError(toFriendlyError(err));
      return null;
    } finally {
      if (alive.current) {
        setDetecting(false);
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    if (!auto) return;
    void refresh();
  }, [auto, refresh]);

  return { report, loading, detecting, error, refresh };
}
