import { useEffect, useId, useState, type ReactNode } from 'react';
import { IconChevronRight } from './Icons';

interface AccordionProps {
  title: ReactNode;
  children: ReactNode;
  /** 默认展开 */
  defaultOpen?: boolean;
}

/** 折叠面板：常见问题、技术详情、打印性问题明细都用它 */
export function Accordion({ title, children, defaultOpen = false }: AccordionProps) {
  const [open, setOpen] = useState(defaultOpen);
  const id = useId();

  return (
    <div className="acc">
      <button
        type="button"
        className="acc-head"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((v) => !v)}
      >
        <span>{title}</span>
        <IconChevronRight size={16} className="acc-chevron" />
      </button>
      {open ? (
        <div className="acc-body" id={id}>
          {children}
        </div>
      ) : null}
    </div>
  );
}

/** Esc 关闭浮层的通用工具（下拉菜单等） */
export function useEscapeToClose(active: boolean, onClose: () => void) {
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [active, onClose]);
}
