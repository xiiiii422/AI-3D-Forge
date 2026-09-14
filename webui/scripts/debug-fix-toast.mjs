/** 单项复现：[自动修复] → toast */
import { launchBrowser, connectPage, sleep } from './cdp.mjs';

const BASE = 'http://127.0.0.1:5273';
const PORT = 9352;

const browser = await launchBrowser({ port: PORT, width: 1280, height: 800 });
const page = await connectPage(browser.port, 'about:blank');
const problems = page.collectProblems();
await page.setViewport(1280, 800);

// 监听真实网络请求
const requests = [];
page.on('Network.requestWillBeSent', (p) => requests.push(`${p.request.method} ${p.request.url}`));

await page.goto(`${BASE}/#/diagnostics`);
await page.waitForFunction(`document.querySelectorAll('.diag-group').length >= 6`, { label: '诊断' });
await page.evaluate(`fetch('/api/openscad/forget', { method: 'POST' })`);
await sleep(400);
await page.clickText('全部重新检测', {
  expect: `[...document.querySelectorAll('button')].some(b => b.textContent.includes('自动修复'))`,
});
await sleep(600);
requests.length = 0;

const before = await page.evaluate(`(() => {
  const btns = [...document.querySelectorAll('button')].filter(b => b.textContent.includes('自动修复'));
  return btns.map((b, i) => {
    const r = b.getBoundingClientRect();
    return { i, text: b.textContent.trim(), disabled: b.disabled, rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)] };
  });
})()`);
console.log('自动修复按钮:', JSON.stringify(before));

const mark = await page.markByText('自动修复');
console.log('标记选择器:', mark.selector);
console.log(
  '标记到的元素:',
  await page.evaluate(`(() => {
    const el = document.querySelector(${JSON.stringify(mark.selector)});
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const cx = Math.round(r.x + r.width / 2), cy = Math.round(r.y + r.height / 2);
    const top = document.elementFromPoint(cx, cy);
    return { tag: el.tagName, text: el.textContent.trim(), rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)], center: [cx, cy], top: top ? top.tagName + '.' + String(top.className) : null, isSelf: top === el };
  })()`),
);
const box = await page.click(mark.selector, { finder: mark.finder });
console.log('点击返回:', JSON.stringify(box));
console.log('点击后立刻:', await page.evaluate(`[...document.querySelectorAll('button')].filter(b => b.textContent.includes('修复')).map(b => b.textContent.trim() + (b.disabled ? '(disabled)' : ''))`));

for (const delay of [100, 300, 600, 1000, 2000, 3000]) {
  await sleep(delay === 100 ? 100 : delay - 0);
  const state = await page.evaluate(`({
    toasts: [...document.querySelectorAll('.toast')].map(t => t.textContent.replace(/\\s+/g,' ').trim()),
    toastLayerHtml: (document.querySelector('.toast-layer')?.innerHTML || '').slice(0, 120),
    fixButtons: [...document.querySelectorAll('button')].filter(b => b.textContent.includes('修复')).map(b => b.textContent.trim() + (b.disabled ? '(disabled)' : '')),
  })`);
  console.log(`  +${delay}ms:`, JSON.stringify(state));
}

console.log('网络请求:', JSON.stringify(requests.filter((r) => r.includes('/api/')), null, 1));
console.log('控制台问题:', JSON.stringify(problems.console), JSON.stringify(problems.pageErrors));
await browser.kill();
