/**
 * 精确定位：verify-flow 里 markByText + click 这条路为什么没有推进向导。
 */
import { launchBrowser, connectPage, sleep } from './cdp.mjs';

const BASE = 'http://127.0.0.1:5273';
const PORT = 9346;

const browser = await launchBrowser({ port: PORT, width: 1280, height: 800 });
const page = await connectPage(browser.port, 'about:blank');
const problems = page.collectProblems();
await page.setViewport(1280, 800);
await page.goto(`${BASE}/#/welcome`);
await page.waitForFunction(`!!document.querySelector('.hero')`, { label: 'hero' });

// 走 verify-flow 的同一条路径
const selector = await page.markByText('开始配置');
console.log('标记选择器：', selector);
const found = await page.evaluate(
  `(() => { const el = document.querySelector(${JSON.stringify(selector)}); return el ? { tag: el.tagName, text: el.textContent.trim(), cls: el.className } : null; })()`,
);
console.log('标记到的元素：', JSON.stringify(found));

await page.click(selector);
await sleep(1200);
const after = await page.evaluate(
  `({
     active: document.querySelector('.wizard-step.is-active')?.textContent?.trim() ?? null,
     hasReadiness: !!document.querySelector('.readiness-num'),
     hasSkeleton: !!document.querySelector('.skeleton'),
     checkRows: document.querySelectorAll('.check-row').length,
     bodyLen: (document.querySelector('.wizard-body')?.innerHTML || '').length,
     bodyHead: (document.querySelector('.wizard-body')?.innerHTML || '').slice(0, 200),
   })`,
);
console.log('点击后状态：', JSON.stringify(after, null, 1));
console.log('控制台问题：', JSON.stringify(problems.console), JSON.stringify(problems.pageErrors));

await page.screenshot('webui/scripts/shots/debug-after-click.png');
await browser.kill();
