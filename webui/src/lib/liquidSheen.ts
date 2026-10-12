/**
 * 液态玻璃的「跟随指针」高光：把指针位置写成元素自己的两个 CSS 变量。
 *
 * 为什么在 JS 里只写变量、不写 background：
 * 渐变的位置在 CSS 里不可动画，把它做成变量 + 一层 opacity 过渡，
 * 移动时只改两个 custom property，鼠标进出时由 CSS 负责淡入淡出。
 *
 * 为什么不做成 React 状态：pointermove 是每帧都可能来的事件，
 * 走 setState 会把整棵界面 reconcile 一遍；这里一次 rAF 最多处理一个点。
 */

/** 与 app.css 里那组玻璃面板一一对应；不在这个列表上的元素不会有高光 */
const SHEEN_TARGETS = [
  '.topbar',
  '.side',
  '.panel',
  '.params-sheet',
  '.viewer-toolbar',
  '.app-footer',
  '.card',
  '.modal',
  '.menu',
  '.toast',
  '.viewer-overlay',
  '.viewer-dim-hint',
].join(', ');

export function attachLiquidSheen(): () => void {
  let queued: PointerEvent | null = null;
  let frame = 0;

  const flush = () => {
    frame = 0;
    const event = queued;
    queued = null;
    if (!event) return;
    if (document.documentElement.dataset.appearance !== 'glass') return;
    const source = event.target instanceof Element ? event.target : null;
    // closest<HTMLElement>：要写自定义属性，光 Element 上没有 style
    const target = source ? source.closest<HTMLElement>(SHEEN_TARGETS) : null;
    if (!target) return;
    const box = target.getBoundingClientRect();
    target.style.setProperty('--sheen-x', `${Math.round(event.clientX - box.left)}px`);
    target.style.setProperty('--sheen-y', `${Math.round(event.clientY - box.top)}px`);
  };

  const onMove = (event: PointerEvent) => {
    queued = event;
    if (!frame) frame = requestAnimationFrame(flush);
  };

  document.addEventListener('pointermove', onMove, { passive: true });
  return () => {
    document.removeEventListener('pointermove', onMove);
    if (frame) cancelAnimationFrame(frame);
  };
}
