import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { IconAlert, IconCheck, IconInfo, IconX } from './Icons';

export type ToastKind = 'ok' | 'warn' | 'error' | 'info';

interface ToastItem {
  id: number;
  kind: ToastKind;
  text: string;
}

interface ToastApi {
  show: (text: string, kind?: ToastKind) => void;
  ok: (text: string) => void;
  warn: (text: string) => void;
  error: (text: string) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

const ICON: Record<ToastKind, ReactNode> = {
  ok: <IconCheck size={16} />,
  warn: <IconAlert size={16} />,
  error: <IconAlert size={16} />,
  info: <IconInfo size={16} />,
};

const ICON_COLOR: Record<ToastKind, string> = {
  ok: 'var(--ok)',
  warn: 'var(--warn)',
  error: 'var(--danger)',
  info: 'var(--text-3)',
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(1);
  const timers = useRef(new Map<number, number>());

  const dismiss = useCallback((id: number) => {
    setItems((prev) => prev.filter((t) => t.id !== id));
    const timer = timers.current.get(id);
    if (timer !== undefined) {
      window.clearTimeout(timer);
      timers.current.delete(id);
    }
  }, []);

  const show = useCallback(
    (text: string, kind: ToastKind = 'info') => {
      const id = nextId.current++;
      setItems((prev) => [...prev.slice(-3), { id, kind, text }]);
      const timer = window.setTimeout(() => dismiss(id), kind === 'error' ? 7000 : 4200);
      timers.current.set(id, timer);
    },
    [dismiss],
  );

  const api = useMemo<ToastApi>(
    () => ({
      show,
      ok: (text: string) => show(text, 'ok'),
      warn: (text: string) => show(text, 'warn'),
      error: (text: string) => show(text, 'error'),
    }),
    [show],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toast-layer" role="status" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast toast-${t.kind}`}>
            <span style={{ color: ICON_COLOR[t.kind], flex: 'none', marginTop: 1 }}>
              {ICON[t.kind]}
            </span>
            <span className="grow">{t.text}</span>
            <button
              type="button"
              className="toast-close"
              aria-label="关闭提示"
              onClick={() => dismiss(t.id)}
            >
              <IconX size={14} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error('useToast 必须在 <ToastProvider> 内使用');
  return ctx;
}
