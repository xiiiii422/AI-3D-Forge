/**
 * 程序化验证「文字有没有被裁切 / 重叠」。
 * 视觉模型在 5 张图批量降采样后会误报，这里用几何测量给出确定结论：
 *  - 文本叶子节点 scrollWidth > clientWidth（且没有用省略号）→ 真裁切
 *  - 同一容器内两个文本叶子的矩形相交 → 真重叠
 */
import { launchBrowser, connectPage, sleep } from './cdp.mjs';

const BASE = 'http://127.0.0.1:5273';
const PORT = 9354;

const CHECK = `(() => {
  const clipped = [];
  const overlapped = [];
  const leaves = [];
  const describe = (el) => {
    const cls = typeof el.className === 'string' && el.className.trim()
      ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.')
      : '';
    return el.tagName.toLowerCase() + cls;
  };
  const ownClip = (el) => {
    const cs = getComputedStyle(el);
    return ['auto','scroll','hidden','clip'].includes(cs.overflowX) &&
           ['auto','scroll','hidden','clip'].includes(cs.overflowY);
  };

  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity) === 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (el.children.length === 0 && (el.textContent || '').trim()) {
      leaves.push({ el, r });
      if (cs.textOverflow !== 'ellipsis' && !ownClip(el) && el.tagName !== 'INPUT') {
        const pad = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
        if (el.scrollWidth - el.clientWidth > 2 && el.clientWidth - pad > 0) {
          clipped.push(describe(el) + ' 「' + el.textContent.trim().slice(0, 18) + '」 sw=' + el.scrollWidth + ' cw=' + el.clientWidth);
        }
      }
      if (el.scrollHeight - el.clientHeight > 2 && cs.overflow === 'visible' && el.clientHeight > 0) {
        clipped.push(describe(el) + ' 「' + el.textContent.trim().slice(0, 18) + '」 纵向 sh=' + el.scrollHeight + ' ch=' + el.clientHeight);
      }
    }
  }

  // 重叠检测：同一父容器内的兄弟文本节点两两比较
  const parents = new Map();
  for (const item of leaves) {
    const p = item.el.parentElement;
    if (!p) continue;
    if (!parents.has(p)) parents.set(p, []);
    parents.get(p).push(item);
  }
  for (const [p, items] of parents) {
    for (let i = 0; i < items.length; i += 1) {
      for (let j = i + 1; j < items.length; j += 1) {
        const a = items[i].r, b = items[j].r;
        const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (ox > 3 && oy > 3) {
          overlapped.push(
            describe(items[i].el) + ' 「' + items[i].el.textContent.trim().slice(0, 14) + '」 × ' +
            describe(items[j].el) + ' 「' + items[j].el.textContent.trim().slice(0, 14) + '」 重叠 ' +
            Math.round(ox) + '×' + Math.round(oy) + 'px（父 ' + describe(p) + '）'
          );
        }
      }
    }
  }

  return { clipped, overlapped, leafCount: leaves.length };
})()`;

const browser = await launchBrowser({ port: PORT, width: 1280, height: 800 });
const page = await connectPage(browser.port, 'about:blank');
await page.setViewport(1280, 800);

const pages = [
  ['诊断页', `${BASE}/#/diagnostics`, `document.querySelectorAll('.diag-group').length >= 6`],
  ['设置页', `${BASE}/#/settings`, `document.querySelectorAll('.card').length >= 4`],
  ['关于页', `${BASE}/#/about`, `document.querySelectorAll('.license').length >= 4`],
  ['工作台', `${BASE}/#/studio`, `!!document.querySelector('.studio')`],
  ['欢迎向导', `${BASE}/#/welcome`, `!!document.querySelector('.wizard')`],
  ['配置页-深色', `${BASE}/#/settings`, `document.querySelectorAll('.card').length >= 4`],
];

let problems = 0;
for (const [name, url, ready] of pages) {
  if (name === '配置页-深色') {
    await page.evaluate(`document.documentElement.setAttribute('data-theme','dark')`);
    await sleep(400);
  }
  await page.goto(url);
  await page.waitForFunction(ready, { label: name, timeoutMs: 20000 });
  await sleep(900);
  const res = await page.evaluate(CHECK);
  const bad = res.clipped.length + res.overlapped.length;
  problems += bad;
  console.log(`\n【${name}】文本节点 ${res.leafCount} 个`);
  if (res.clipped.length) console.log('  裁切：\n   - ' + res.clipped.join('\n   - '));
  if (res.overlapped.length) console.log('  重叠：\n   - ' + res.overlapped.join('\n   - '));
  if (!bad) console.log('  ✓ 没有裁切，也没有重叠');
}

console.log(`\n合计问题：${problems}`);
await browser.kill();
process.exit(problems ? 1 : 0);
