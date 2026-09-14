import { statusPillClass, StatusDot } from './StatusDot';

interface EnvPillProps {
  okCount: number;
  total: number;
  loading: boolean;
  /** 未全绿时点击去哪（默认诊断页） */
  onClick: () => void;
}

/**
 * 环境就绪胶囊：全绿显示「环境就绪」，否则显示 x/y 并可点击进入诊断页。
 */
export function EnvPill({ okCount, total, loading, onClick }: EnvPillProps) {
  if (loading) {
    return (
      <span className="pill" aria-live="polite">
        <StatusDot status="checking" />
        正在检测环境…
      </span>
    );
  }

  const allOk = total > 0 && okCount >= total;
  const status = allOk ? 'ok' : okCount === 0 ? 'fail' : 'warn';
  const label = allOk ? '环境就绪' : `环境就绪 ${okCount}/${total}`;

  return (
    <button
      type="button"
      className={statusPillClass(status)}
      onClick={onClick}
      title={allOk ? '一切正常' : '有项目需要处理，点击查看系统诊断'}
      aria-label={allOk ? '环境就绪，查看系统诊断' : `${label}，查看系统诊断`}
    >
      <StatusDot status={status} />
      {label}
    </button>
  );
}
