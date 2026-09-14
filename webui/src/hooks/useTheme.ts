import { useCallback, useEffect, useState } from 'react';

export type ThemeChoice = 'light' | 'dark' | 'system';
export type ResolvedTheme = 'light' | 'dark';

const STORAGE_KEY = 'aiforge.theme';

function systemTheme(): ResolvedTheme {
  if (typeof window === 'undefined') return 'light';
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function readStored(): ThemeChoice {
  if (typeof window === 'undefined') return 'light';
  const raw = window.localStorage.getItem(STORAGE_KEY);
  return raw === 'dark' || raw === 'system' || raw === 'light' ? raw : 'light';
}

/**
 * 主题：契约里 theme 有 light/dark/system 三态。
 * 本地 localStorage 先落地（首屏不闪），后端设置到达后由 setTheme 覆盖。
 */
export function useTheme() {
  const [choice, setChoiceState] = useState<ThemeChoice>(readStored);
  const [resolved, setResolved] = useState<ResolvedTheme>(() =>
    readStored() === 'system' ? systemTheme() : (readStored() as ResolvedTheme),
  );

  const apply = useCallback((next: ThemeChoice) => {
    const value: ResolvedTheme = next === 'system' ? systemTheme() : next;
    // 主题切换时**临时关掉所有过渡**：一次换肤会同时改掉几乎所有元素的
    // background / border-color / color，二十多处 transition 一起触发，
    // 界面是"抹"过去而不是"啪"地切过去（better-ui：Suppress transitions on
    // theme switch）。做法是注入一条 !important 规则、强制一次重排、
    // 下一帧移除 —— 不往任何组件上写 transition: none，所以不影响其它交互。
    const style = document.createElement('style');
    style.appendChild(
      document.createTextNode('*,*::before,*::after{transition:none !important}'),
    );
    document.head.appendChild(style);
    document.documentElement.setAttribute('data-theme', value);
    document.documentElement.style.colorScheme = value;
    // 强制重排，让浏览器按"无过渡"把这一帧算完
    void document.body.offsetHeight;
    window.requestAnimationFrame(() => style.remove());
    setResolved(value);
  }, []);

  const setTheme = useCallback(
    (next: ThemeChoice, options: { persist?: boolean } = {}) => {
      setChoiceState(next);
      apply(next);
      if (options.persist !== false) {
        try {
          window.localStorage.setItem(STORAGE_KEY, next);
        } catch {
          // 隐私模式下 localStorage 可能不可写，忽略即可
        }
      }
    },
    [apply],
  );

  useEffect(() => {
    apply(choice);
    if (choice !== 'system') return;
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = () => apply('system');
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
  }, [choice, apply]);

  return { theme: choice, resolvedTheme: resolved, setTheme };
}
