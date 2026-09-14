import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import type { CheckItem, DiagnosticsResponse, EnvReport } from '../api/types';
import { Accordion } from '../components/Accordion';
import { Button } from '../components/Button';
import { Card } from '../components/Card';
import { CheckList, CheckListSkeleton } from '../components/CheckList';
import { IconCheck, IconRefresh, IconShield } from '../components/Icons';
import { StatusDot, statusPillClass } from '../components/StatusDot';
import { useToast } from '../components/Toast';
import { toFriendlyError, type FriendlyError } from '../hooks/errors';

const OVERALL_LABEL: Record<DiagnosticsResponse['overall'], string> = {
  ok: '全部正常',
  warn: '基本可用',
  fail: '需要处理',
};

function formatCheckedAt(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleString('zh-CN', {
    month: 'long',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** meta 里的值可能是任意 JSON；统一渲染成可读字符串 */
function renderMetaValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

export default function Diagnostics() {
  const toast = useToast();
  const [data, setData] = useState<DiagnosticsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<FriendlyError | null>(null);
  const [fixingId, setFixingId] = useState<string | null>(null);
  const [liveTesting, setLiveTesting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.diagnostics();
      setData(res);
      setError(null);
    } catch (err) {
      setError(toFriendlyError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleFix = useCallback(
    async (item: CheckItem) => {
      setFixingId(item.id);
      try {
        const res = await api.diagnosticsFix(item.id);
        if (res.ok) toast.ok(res.message || `${item.title} 已修复。`);
        else toast.warn(res.message || `${item.title} 仍然需要手动处理。`);
        await load();
      } catch (err) {
        toast.error(toFriendlyError(err).message);
      } finally {
        setFixingId(null);
      }
    },
    [load, toast],
  );

  /** 联网复检：契约 GET /api/env/report?live=1（较慢，需要用户主动触发） */
  const handleLiveTest = useCallback(async () => {
    setLiveTesting(true);
    try {
      const report: EnvReport = await api.envReport({ live: true });
      const apiItem = report.items.find((i) => i.id === 'api_live');
      if (apiItem) {
        if (apiItem.status === 'ok') toast.ok(apiItem.detail);
        else toast.warn(apiItem.detail);
      } else {
        toast.show('联网检测完成。');
      }
      await load();
    } catch (err) {
      toast.error(toFriendlyError(err).message);
    } finally {
      setLiveTesting(false);
    }
  }, [load, toast]);

  const problemCount =
    data?.groups.reduce(
      (sum, group) => sum + group.items.filter((i) => i.status !== 'ok').length,
      0,
    ) ?? 0;

  return (
    <div className="page">
      <div className="page-head">
        <div className="row-between" style={{ alignItems: 'flex-start' }}>
          <div>
            <h1>系统诊断</h1>
            <p>
              检查这台电脑上影响生成模型的所有环节。
              {data ? `上次检测：${formatCheckedAt(data.checked_at)}` : ''}
            </p>
          </div>
          <div className="row" style={{ gap: 6 }}>
            <Button
              icon={<IconShield size={15} />}
              loading={liveTesting}
              onClick={() => void handleLiveTest()}
            >
              联网复检
            </Button>
            <Button
              variant="primary"
              icon={<IconRefresh size={15} />}
              loading={loading}
              onClick={() => void load()}
            >
              全部重新检测
            </Button>
          </div>
        </div>
      </div>

      {data ? (
        <div className="row" style={{ gap: 'var(--sp-3)', marginBottom: 'var(--sp-5)' }}>
          <span className={statusPillClass(data.overall)}>
            <StatusDot status={data.overall} />
            {OVERALL_LABEL[data.overall]}
          </span>
          <span className="muted small">
            {problemCount === 0 ? '没有发现需要处理的问题。' : `${problemCount} 项需要关注`}
          </span>
        </div>
      ) : null}

      {loading && !data ? (
        <Card tight>
          <CheckListSkeleton rows={8} />
        </Card>
      ) : null}

      {error && !data ? (
        <Card>
          <div className="err">
            <div className="err-title">
              <StatusDot status="fail" />
              无法完成检测
            </div>
            <div>{error.message}</div>
            {error.hint ? <div className="err-hint">{error.hint}</div> : null}
            <div className="row" style={{ marginTop: 4 }}>
              <Button size="sm" variant="primary" onClick={() => void load()}>
                重试
              </Button>
            </div>
          </div>
        </Card>
      ) : null}

      {data?.groups.map((group) => (
        <section className="diag-group" key={group.group}>
          <div className="diag-group-head">
            <h2>{group.group}</h2>
            <span className="muted-3 small">
              {group.items.filter((i) => i.status === 'ok').length}/{group.items.length} 正常
            </span>
          </div>
          <Card tight>
            <CheckList
              items={group.items}
              onFix={(item) => void handleFix(item)}
              fixingId={fixingId}
              renderMeta={(item) =>
                item.meta && Object.keys(item.meta).length > 0 ? (
                  <Accordion title="技术详情">
                    <dl className="dl">
                      {Object.entries(item.meta).map(([key, value]) => (
                        <div key={key} style={{ display: 'contents' }}>
                          <dt>{key}</dt>
                          <dd>{renderMetaValue(value)}</dd>
                        </div>
                      ))}
                    </dl>
                  </Accordion>
                ) : null
              }
            />
          </Card>
        </section>
      ))}

      {data ? (
        <div className="notice" style={{ marginTop: 'var(--sp-5)' }}>
          <IconCheck size={16} />
          <span>
            所有检测都在本机完成。「自动修复」只会重建配置或目录、重新测试引擎，不会上传任何数据。
          </span>
        </div>
      ) : null}
    </div>
  );
}
