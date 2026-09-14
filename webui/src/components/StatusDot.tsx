import type { CheckItem } from '../api/types';

type Status = CheckItem['status'];

/** 状态文案：给屏幕阅读器与 tooltip 用，不出现英文 */
const STATUS_LABEL: Record<Status, string> = {
  ok: '通过',
  warn: '需注意',
  fail: '未通过',
  checking: '检测中',
};

export function StatusDot({ status }: { status: Status }) {
  return (
    <span
      className={`dot dot-${status}`}
      role="img"
      aria-label={STATUS_LABEL[status]}
      title={STATUS_LABEL[status]}
    />
  );
}

/** 把 ok/warn/fail/checking 映射成 pill 的语义 class */
export function statusPillClass(status: Status | 'neutral'): string {
  switch (status) {
    case 'ok':
      return 'pill pill-ok';
    case 'warn':
      return 'pill pill-warn';
    case 'fail':
      return 'pill pill-fail';
    case 'checking':
      return 'pill pill-brand';
    default:
      return 'pill pill-neutral';
  }
}
