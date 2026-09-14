/**
 * 欢迎向导第 1 步「开始配置」点击不生效的定位脚本。
 * 直接检查 DOM：按钮是否存在、是否被遮挡、点击后 React 状态有没有变化。
 */
import { launchBrowser, connectPage, sleep } from './cdp.mjs';

const BASE = 'http://127.0.0.1:5273';
const PORT = 9344;

const browser = await launchBrowser({ port: PORT, width: 1280, height: 800 });
const page = await connectPage(browser.port, 'about:blank');
await page.setViewport(1280, 800);
await page.goto(`${BASE}/#/welcome`);
await page.waitForFunction(`!!document.querySelector('.hero')`, { label: 'hero' });

const before = await page.evaluate(`(() => {
  const btns = [...document.querySelectorAll('button')];
  return btns.map(b => {
    const r = b.getBoundingClientRect();
    const cx = Math.round(r.x + r.width / 2), cy = Math.round(r.y + r.height / 2);
    const top = document.elementFromPoint(cx, cy);
    return {
      text: (b.textContent || '').trim(),
      cls: b.className,
      disabled: b.disabled,
      rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      center: [cx, cy],
      topElement: top ? top.tagName + '.' + (typeof top.className === 'string' ? top.className : '') : null,
      hitIsSelfOrChild: top ? (top === b || b.contains(top)) : false,
    };
  });
})()`);
console.log('按钮清单：', JSON.stringify(before, null, 1));

// 用 JS 直接 click() 一次，看状态是否推进
const afterJsClick = await page.evaluate(`(() => {
  const b = [...document.querySelectorAll('button')].find(x => x.textContent.includes('开始配置'));
  if (!b) return 'no-button';
  b.click();
  return 'clicked';
})()`);
await sleep(600);
const afterJsState = await page.evaluate(
  `({ hasReadiness: !!document.querySelector('.readiness-num'), stepText: document.querySelector('.wizard-step.is-active')?.textContent?.trim() ?? null })`,
);
console.log('JS click 结果：', afterJsClick, JSON.stringify(afterJsState));

await browser.kill();
