import type { ReactNode } from 'react';
import type { CheckItem } from '../api/types';
import { IconAlert, IconCheck, IconLoader, IconWrench, IconX } from './Icons';

const ICON: Record<CheckItem['status'], ReactNode> = {
  ok: <IconCheck size={13} />,
  warn: <IconAlert size={13} />,
  fail: <IconX size={13} />,
  checking: <IconLoader size={13} className="spin" />,
};

interface CheckListProps {
  items: CheckItem[];
  /** 点击「自动修复」；不传则不渲染修复按钮 */
  onFix?: (item: CheckItem) => void;
  /** 正在修复中的 check id，用于禁用按钮 */
  fixingId?: string | null;
  /** 是否显示 action 行（诊断页在 status!=='ok' 时显示） */
  showAction?: boolean;
  /** 是否显示 meta 折叠技术详情（诊断页用） */
  renderMeta?: (item: CheckItem) => ReactNode;
}

/**
 * 通用检查项列表：✓ / ⚠ / ✗ + 标题 + 一句话结论 + （可选）需要用户做什么 + 自动修复。
 * 契约 §0.1 的 CheckItem 直接驱动渲染，不做任何字段改名。
 */
export function CheckList({
  items,
  onFix,
  fixingId,
  showAction = true,
  renderMeta,
}: CheckListProps) {
  return (
    <div className="checklist">
      {items.map((item) => {
        const needsAttention = item.status !== 'ok';
        return (
          <div className="check-row" key={item.id}>
            <span className={`check-icon check-icon-${item.status}`}>{ICON[item.status]}</span>
            <div className="check-row-main">
              <div className="check-row-title">
                <span>{item.title}</span>
              </div>
              <div className="check-row-detail">{item.detail}</div>
              {showAction && needsAttention && item.action ? (
                <div
                  className={`check-row-action${item.fixable ? ' is-brand' : ''}`}
                >
                  {item.action}
                </div>
              ) : null}
              {renderMeta ? renderMeta(item) : null}
            </div>
            {onFix && needsAttention && item.fixable ? (
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => onFix(item)}
                disabled={fixingId === item.id}
                aria-label={`自动修复：${item.title}`}
              >
                <IconWrench size={14} />
                {fixingId === item.id ? '修复中…' : '自动修复'}
              </button>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

/** 检测中的平静骨架屏：3 行占位，不做抖动 spinner */
export function CheckListSkeleton({ rows = 6 }: { rows?: number }) {
  return (
    <div className="checklist" aria-hidden="true">
      {Array.from({ length: rows }, (_, i) => (
        <div className="skeleton-row" key={i}>
          <div className="skeleton skeleton-circle" />
          <div style={{ flex: 1 }}>
            <div
              className="skeleton"
              style={{ height: 12, width: `${34 + ((i * 7) % 26)}%`, marginBottom: 8 }}
            />
            <div className="skeleton" style={{ height: 10, width: `${58 + ((i * 11) % 30)}%` }} />
          </div>
        </div>
      ))}
    </div>
  );
}
