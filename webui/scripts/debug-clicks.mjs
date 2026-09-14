/**
 * 逐个验证「点不动」的四类控件：
 *  1) 工作台工具栏的图标按钮（线框切换）
 *  2) 诊断页 [自动修复]
 *  3) 诊断页「技术详情」折叠头
 *  4) 关于页 FAQ 折叠头
 * 每种都打印：标记到的元素、点击前的 aria 状态、坐标、命中元素、点击后的 aria 状态。
 */
import { launchBrowser, connectPage, sleep } from './cdp.mjs';

const BASE = 'http://127.0.0.1:5273';
const PORT = 9350;

const browser = await launchBrowser({ port: PORT, width: 1280, height: 800 });
const page = await connectPage(browser.port, 'about:blank');
const problems = page.collectProblems();
await page.setViewport(1280, 800);

/** 用真实鼠标事件点击（不附加 JS click），返回点击前后状态 */
async function probe(label, selector) {
  const before = await page.evaluate(`(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName, cls: String(el.className), disabled: el.disabled ?? null,
      pressed: el.getAttribute('aria-pressed'), expanded: el.getAttribute('aria-expanded'),
      rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      inViewport: r.y >= 0 && r.bottom <= window.innerHeight,
    };
  })()`);
  if (!before) {
    console.log(`${label}: 找不到元素 ${selector}`);
    return;
  }
  console.log(`\n${label} 点击前:`, JSON.stringify(before));

  // 滚到视口内
  if (!before.inViewport) {
    await page.evaluate(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({ block: 'center', behavior: 'instant' })`);
    await sleep(300);
  }

  const box = await page.evaluate(`(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    const r = el.getBoundingClientRect();
    const cx = Math.round(r.x + r.width / 2), cy = Math.round(r.y + r.height / 2);
    const top = document.elementFromPoint(cx, cy);
    return { cx, cy, topTag: top ? top.tagName : null, topCls: top ? String(top.className) : null, isSelfOrChild: top ? (top === el || el.contains(top) || top.contains(el)) : false };
  })()`);
  console.log(`${label} 命中测试:`, JSON.stringify(box));

  if (!box.isSelfOrChild) {
    console.log(`${label}: 被遮挡，跳过真实点击`);
    return;
  }

  await page.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: box.cx, y: box.cy, button: 'none' });
  await page.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: box.cx, y: box.cy, button: 'left', buttons: 1, clickCount: 1 });
  await page.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: box.cx, y: box.cy, button: 'left', buttons: 0, clickCount: 1 });
  await sleep(500);

  const afterMouse = await page.evaluate(`(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) return 'element-gone';
    return { pressed: el.getAttribute('aria-pressed'), expanded: el.getAttribute('aria-expanded'), cls: String(el.className) };
  })()`);
  console.log(`${label} 真实鼠标点击后:`, JSON.stringify(afterMouse));

  // 再补一次原生 click（仅在真实鼠标点击没生效时才需要，这里手动看效果）
  const stillThere = await page.evaluate(`!!document.querySelector(${JSON.stringify(selector)})`);
  if (stillThere) {
    await page.evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
    await sleep(500);
    const afterJs = await page.evaluate(`(() => {
      const el = document.querySelector(${JSON.stringify(selector)});
      if (!el) return 'element-gone';
      return { pressed: el.getAttribute('aria-pressed'), expanded: el.getAttribute('aria-expanded'), cls: String(el.className) };
    })()`);
    console.log(`${label} 原生 click 后:`, JSON.stringify(afterJs));
  }
}

/* 1) 工作台工具栏 */
await page.goto(`${BASE}/#/studio`);
await page.waitForFunction(`document.querySelectorAll('.proj').length >= 1`, { label: '项目' });
await page.click('.proj', { index: 0 });
await page.waitForFunction(`!!document.querySelector('button[aria-label="线框切换"]')`, { label: '工具栏' });
await probe('1-线框切换', 'button[aria-label="线框切换"]');
await probe('1b-自动旋转', 'button[aria-label="自动旋转"]');

/* 2) 诊断页 */
await page.goto(`${BASE}/#/diagnostics`);
await page.waitForFunction(`document.querySelectorAll('.diag-group').length >= 6`, { label: '诊断' });
await page.evaluate(`fetch('/api/openscad/forget', { method: 'POST' })`);
await sleep(300);
await page.clickText('全部重新检测', { expect: `[...document.querySelectorAll('button')].some(b => b.textContent.includes('自动修复'))` });
await sleep(500);
const fixSel = await page.evaluate(`(() => {
  const b = [...document.querySelectorAll('button')].find(x => x.textContent.includes('自动修复'));
  if (!b) return null;
  b.setAttribute('data-probe', 'fix');
  return '[data-probe="fix"]';
})()`);
await probe('2-自动修复', fixSel);

const accSel = await page.evaluate(`(() => {
  const h = [...document.querySelectorAll('.acc-head')].find(x => x.textContent.includes('技术详情'));
  if (!h) return null;
  h.setAttribute('data-probe', 'acc');
  return '[data-probe="acc"]';
})()`);
await probe('3-技术详情', accSel);

/* 4) 关于页 */
await page.goto(`${BASE}/#/about`);
await page.waitForFunction(`document.querySelectorAll('.acc-head').length >= 5`, { label: 'FAQ' });
const faqSel = await page.evaluate(`(() => {
  const h = [...document.querySelectorAll('.acc-head')].find(x => x.textContent.includes('OpenSCAD 安装失败怎么办'));
  if (!h) return null;
  h.setAttribute('data-probe', 'faq');
  return '[data-probe="faq"]';
})()`);
await probe('4-FAQ', faqSel);

console.log('\n控制台问题:', JSON.stringify(problems.console), JSON.stringify(problems.pageErrors));
await browser.kill();
