import { useCallback, useEffect, useState } from 'react';

/**
 * 界面材质：实面（solid）/ 液态玻璃（glass）。
 *
 * 为什么和 useTheme 一样是「本地先落地 + 后端到达后覆盖」：
 * 材质是纯观感，首屏就该是对的样子，不能等一个网络往返再换。
 * 后端 PUT /api/settings 才是权威存储（换机器/重装都还在），localStorage
 * 只负责首帧不闪。
 *
 * 真正的样式全在 tokens.css / app.css 的 `[data-appearance='glass']` 分支里，
 * 这里只写两个东西：属性 + 透明度百分比。滑块拖动时不触发 React 重渲染，
 * 只改一个 CSS 变量 —— 半透明层重算比整树 reconcile 便宜得多。
 */

export type AppearanceChoice = 'solid' | 'glass';

/** 与后端 src/aiforge/config.py 同一组数，改一处必须同步改另一处 */
export const TRANSPARENCY_MIN = 0;
export const TRANSPARENCY_MAX = 60;
export const TRANSPARENCY_DEFAULT = 25;

const APPEARANCE_KEY = 'aiforge.appearance';
const TRANSPARENCY_KEY = 'aiforge.glass-transparency';
/** 系统级「降低透明度」（Windows：设置 → 个性化 → 颜色 → 透明效果） */
const REDUCED_TRANSPARENCY_QUERY = '(prefers-reduced-transparency: reduce)';

function prefersReducedTransparency(): boolean {
  return typeof matchMedia === 'function' && matchMedia(REDUCED_TRANSPARENCY_QUERY).matches;
}

export function clampTransparency(value: unknown): number {
  const n = Number(value);
  if (!Number.isFinite(n)) return TRANSPARENCY_DEFAULT;
  return Math.max(TRANSPARENCY_MIN, Math.min(TRANSPARENCY_MAX, Math.round(n)));
}

function readStored(): { appearance: AppearanceChoice; transparency: number } {
  if (typeof window === 'undefined') return { appearance: 'solid', transparency: TRANSPARENCY_DEFAULT };
  let appearance: AppearanceChoice = 'solid';
  try {
    if (window.localStorage.getItem(APPEARANCE_KEY) === 'glass') appearance = 'glass';
  } catch {
    // 隐私模式下 localStorage 可能不可读，按默认值走
  }
  return { appearance, transparency: clampTransparency(safeGet(TRANSPARENCY_KEY)) };
}

function safeGet(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

/**
 * 把选择写进 DOM：CSS 只认这两个信号。
 *
 * 为什么系统要求「降低透明度」时要删掉内联变量，而不是照写：
 * tokens.css 里那条 @media (prefers-reduced-transparency: reduce) 保险只能改
 * 样式表里的 :root 值，而内联 style 的优先级在它之上 —— 照样写就会顶掉那条保险，
 * 得到「半透 + 不模糊」这个最读不动的组合（--glass-blur 仍然被媒体查询归零）。
 * 删掉内联值才是让那条规则生效；同时监听媒体查询变化，用户中途改系统设置也能回来。
 */
export function applyAppearance(appearance: AppearanceChoice, transparency: number): void {
  const root = document.documentElement;
  root.dataset.appearance = appearance;
  const value = clampTransparency(transparency);
  // 滑块的填充比例：填充渐变的位置不可动画，只能由这里给出百分比
  root.style.setProperty(
    '--range-fill',
    String(Math.round((value / TRANSPARENCY_MAX) * 100)),
  );
  if (prefersReducedTransparency()) {
    root.style.removeProperty('--glass-solid');
    return;
  }
  // 百分比 = 不透明度：滑块往右是「更透明」，这里存的是留给 color-mix 的那一份实底
  root.style.setProperty('--glass-solid', `${100 - value}%`);
}

export interface UseAppearanceResult {
  appearance: AppearanceChoice;
  transparency: number;
  /** 只改本地并立刻上屏；持久化由调用方走 settings 接口 */
  preview: (next: { appearance?: AppearanceChoice; transparency?: number }) => void;
}

export function useAppearance(): UseAppearanceResult {
  const [{ appearance, transparency }, setState] = useState(readStored);

  useEffect(() => {
    applyAppearance(appearance, transparency);
    const query = typeof matchMedia === 'function' ? matchMedia(REDUCED_TRANSPARENCY_QUERY) : null;
    if (!query) return;
    // 系统设置中途被改（比如用户开了「透明效果」开关）：重跑一次才能补回/拿掉内联值
    const onChange = () => applyAppearance(appearance, transparency);
    query.addEventListener?.('change', onChange);
    return () => query.removeEventListener?.('change', onChange);
  }, [appearance, transparency]);

  const preview = useCallback(
    (next: { appearance?: AppearanceChoice; transparency?: number }) => {
      setState((current) => {
        const merged = {
          appearance: next.appearance ?? current.appearance,
          transparency:
            next.transparency === undefined
              ? current.transparency
              : clampTransparency(next.transparency),
        };
        applyAppearance(merged.appearance, merged.transparency);
        try {
          window.localStorage.setItem(APPEARANCE_KEY, merged.appearance);
          window.localStorage.setItem(TRANSPARENCY_KEY, String(merged.transparency));
        } catch {
          // 存不下就算了，后端那份才是权威的
        }
        return merged;
      });
    },
    [],
  );

  return { appearance, transparency, preview };
}
