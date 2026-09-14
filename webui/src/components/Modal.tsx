import { useEffect, useId, useRef, type ReactNode } from 'react';
import { Button } from './Button';
import { IconX } from './Icons';

interface ModalProps {
  open: boolean;
  title: ReactNode;
  /** 关闭回调（Esc / 遮罩点击 / 右上角按钮都会触发） */
  onClose: () => void;
  children: ReactNode;
  /** 底部操作区 */
  footer?: ReactNode;
  /** 宽版（用于需要更多横向空间的表单） */
  wide?: boolean;
  /** 超宽版（左侧长表单 + 右侧实时预览这类双栏内容） */
  xwide?: boolean;
}

/**
 * 轻量弹窗：Esc 关闭（无障碍硬要求）、点击遮罩关闭、打开时锁滚动并把焦点送进面板。
 */
export function Modal({ open, title, onClose, children, footer, wide, xwide }: ModalProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  /** 关闭回调放 ref：这样下面的副作用只依赖 open，不会每次渲染都重跑（否则会反复抢焦点） */
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onCloseRef.current();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    // 记住打开弹窗前的焦点，关闭时还回去（键盘用户不会掉到页面开头）
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    // 把焦点送进面板，保证键盘用户不会停留在背景内容上
    const timer = window.setTimeout(() => {
      const focusable = panelRef.current?.querySelector<HTMLElement>(
        'input:not([type="hidden"]), textarea, select, button, [href], [tabindex]:not([tabindex="-1"])',
      );
      focusable?.focus();
    }, 0);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.body.style.overflow = prevOverflow;
      window.clearTimeout(timer);
      if (opener && document.contains(opener)) opener.focus();
    };
  }, [open]);

  if (!open) return null;

  return (
    <div
      className="overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className={`modal${wide ? ' modal-wide' : ''}${xwide ? ' modal-xwide' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        ref={panelRef}
      >
        <div className="modal-head">
          <h2 id={titleId}>{title}</h2>
          <Button iconOnly variant="ghost" size="sm" aria-label="关闭" onClick={onClose}>
            <IconX size={16} />
          </Button>
        </div>
        {children}
        {footer ? <div className="modal-foot">{footer}</div> : null}
      </div>
    </div>
  );
}

interface ConfirmModalProps {
  open: boolean;
  title: string;
  message: ReactNode;
  confirmText?: string;
  cancelText?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** 二次确认弹窗（删除项目等破坏性操作） */
export function ConfirmModal({
  open,
  title,
  message,
  confirmText = '确定',
  cancelText = '取消',
  danger,
  busy,
  onConfirm,
  onCancel,
}: ConfirmModalProps) {
  return (
    <Modal
      open={open}
      title={title}
      onClose={onCancel}
      footer={
        <>
          <Button onClick={onCancel} disabled={busy}>
            {cancelText}
          </Button>
          <Button variant={danger ? 'danger' : 'primary'} onClick={onConfirm} loading={busy}>
            {confirmText}
          </Button>
        </>
      }
    >
      <div className="muted">{message}</div>
    </Modal>
  );
}
