interface ProgressBarProps {
  /** 0-100；为 null 时渲染不确定态（细条匀速滑动，不抖动） */
  percent: number | null;
  /** 进度条上方的左侧文案 */
  label?: string;
  /** 进度条上方的右侧文案（通常是百分比） */
  value?: string;
}

export function ProgressBar({ percent, label, value }: ProgressBarProps) {
  const indeterminate = percent === null;
  const clamped = indeterminate ? 0 : Math.max(0, Math.min(100, percent));
  return (
    <div>
      {label || value ? (
        <div className="progress-meta">
          <span>{label ? <strong>{label}</strong> : null}</span>
          {value ? <span>{value}</span> : null}
        </div>
      ) : null}
      <div
        className={`progress${indeterminate ? ' progress-indeterminate' : ''}`}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={indeterminate ? undefined : Math.round(clamped)}
        aria-label={label ?? '进度'}
      >
        <div className="progress-bar" style={indeterminate ? undefined : { width: `${clamped}%` }} />
      </div>
    </div>
  );
}
