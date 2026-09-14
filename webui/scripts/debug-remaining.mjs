/**
 * 三个残留失败的定点诊断：
 *  A. 参数 chips「应用参数」点了没反应？
 *  B. 设置页「深色」按钮不可见（是哪个元素被标记了）？
 *  C. 诊断页「技术详情」折叠项点不开？
 *  D. 关于页 FAQ 点不开？
 */
import { launchBrowser, connectPage, sleep } from './cdp.mjs';

const BASE = 'http://127.0.0.1:5273';
const PORT = 9348;

const browser = await launchBrowser({ port: PORT, width: 1280, height: 800 });
const page = await connectPage(browser.port, 'about:blank');
const problems = page.collectProblems();
await page.setViewport(1280, 800);

/* ---------- A. 参数 chips ---------- */
await page.goto(`${BASE}/#/studio`);
await page.waitForFunction(`document.querySelectorAll('.proj').length >= 1`, { label: '项目' });
await page.click('.proj', { index: 0 });
await page.waitForFunction(`document.querySelectorAll('.chip').length >= 3`, { label: 'chips', timeoutMs: 20000 });

const chipState0 = await page.evaluate(`(() => {
  const chips = [...document.querySelectorAll('.chip')];
  const target = chips.find(c => (c.querySelector('.chip-label').textContent || '').includes('高度'));
  const apply = [...document.querySelectorAll('.params-head button')].find(b => b.textContent.includes('应用参数'));
  return {
    labels: chips.map(c => c.querySelector('.chip-label').textContent + '=' + c.querySelector('input').value),
    foundHeight: !!target,
    heightValue: target ? target.querySelector('input').value : null,
    applyExists: !!apply,
    applyDisabled: apply ? apply.disabled : null,
    applyText: apply ? apply.textContent.trim() : null,
  };
})()`);
console.log('A1 chips 初始状态:', JSON.stringify(chipState0, null, 1));

const setResult = await page.evaluate(`(() => {
  const chips = [...document.querySelectorAll('.chip')];
  const target = chips.find(c => (c.querySelector('.chip-label').textContent || '').includes('高度'));
  const input = target.querySelector('input');
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  setter.call(input, '150');
  input.dispatchEvent(new Event('input', { bubbles: true }));
  return input.value;
})()`);
await sleep(400);
const chipState1 = await page.evaluate(`(() => {
  const apply = [...document.querySelectorAll('.params-head button')].find(b => b.textContent.includes('应用参数'));
  return {
    setTo: ${JSON.stringify(setResult)},
    dirtyClass: !!document.querySelector('.chip-input.is-dirty'),
    restoreBtn: !!([...document.querySelectorAll('.params-head button')].find(b => b.textContent.includes('还原'))),
    applyDisabled: apply.disabled,
    applyClass: apply.className,
  };
})()`);
console.log('A2 改成 150 之后:', JSON.stringify(chipState1, null, 1));

// 点击「应用参数」
const applyMark = await page.markByText('应用参数');
console.log('A3 标记到:', await page.evaluate(`(() => { const el = document.querySelector(${JSON.stringify(applyMark.selector)}); return el ? el.tagName + '.' + el.className + ' disabled=' + el.disabled : null; })()`));
await page.click(applyMark.selector, { finder: applyMark.finder });
await sleep(1500);
const afterApply = await page.evaluate(`({
  runbar: !!document.querySelector('.runbar'),
  runText: (document.querySelector('.runbar-status-text') || {}).textContent || null,
  results: document.querySelectorAll('.result').length,
  userMsgs: [...document.querySelectorAll('.msg-user .bubble')].map(b => b.textContent.trim()),
  panelError: (document.querySelector('.err') || {}).textContent || null,
})`);
console.log('A4 点击应用参数后:', JSON.stringify(afterApply, null, 1));
await page.screenshot('webui/scripts/shots/debug-params.png');

/* ---------- B. 设置页「深色」 ---------- */
await page.goto(`${BASE}/#/settings`);
await page.waitForFunction(`document.querySelectorAll('.seg button').length >= 3`, { label: '主题分段' });
const segInfo = await page.evaluate(`(() => {
  return [...document.querySelectorAll('.seg button')].map(b => {
    const r = b.getBoundingClientRect();
    return { text: b.textContent.trim(), pressed: b.getAttribute('aria-pressed'), rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)], visible: r.width > 0 && r.height > 0 };
  });
})()`);
console.log('B1 主题分段控件:', JSON.stringify(segInfo, null, 1));
const darkMark = await page.markByText('深色');
console.log('B2 「深色」标记到:', await page.evaluate(`(() => { const el = document.querySelector(${JSON.stringify(darkMark.selector)}); if (!el) return null; const r = el.getBoundingClientRect(); return { tag: el.tagName, cls: String(el.className), text: el.textContent.trim(), rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)], parentDisplay: getComputedStyle(el.parentElement).display, offsetParent: el.offsetParent ? el.offsetParent.className : null }; })()`));

/* ---------- C. 诊断页技术详情 ---------- */
await page.goto(`${BASE}/#/diagnostics`);
await page.waitForFunction(`document.querySelectorAll('.acc-head').length >= 1`, { label: '技术详情', timeoutMs: 20000 });
const accInfo = await page.evaluate(`(() => {
  const heads = [...document.querySelectorAll('.acc-head')];
  return heads.slice(0, 4).map(h => {
    const r = h.getBoundingClientRect();
    return { text: h.textContent.replace(/\\s+/g,' ').trim().slice(0, 20), expanded: h.getAttribute('aria-expanded'), rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)] };
  });
})()`);
console.log('C1 折叠项:', JSON.stringify(accInfo, null, 1));
await page.clickText('技术详情');
await sleep(600);
const c2 = await page.evaluate(`({ bodies: document.querySelectorAll('.acc-body').length, dls: document.querySelectorAll('.acc-body .dl').length })`);
console.log('C2 展开后:', JSON.stringify(c2));

/* ---------- D. 关于页 FAQ ---------- */
await page.goto(`${BASE}/#/about`);
await page.waitForFunction(`document.querySelectorAll('.acc-head').length >= 5`, { label: 'FAQ', timeoutMs: 20000 });
const faqInfo = await page.evaluate(`(() => [...document.querySelectorAll('.acc-head')].map(h => h.textContent.replace(/\\s+/g,' ').trim().slice(0,24)))()`);
console.log('D1 FAQ 标题:', JSON.stringify(faqInfo));
await page.clickText('OpenSCAD 安装失败怎么办');
await sleep(600);
console.log('D2 展开后:', JSON.stringify(await page.evaluate(`({ bodies: document.querySelectorAll('.acc-body').length, paras: document.querySelectorAll('.acc-body p').length })`)));

console.log('控制台问题:', JSON.stringify(problems.console), JSON.stringify(problems.pageErrors));
await browser.kill();
