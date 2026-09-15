/**
 * AI 3D Forge —— 界面布局自动体检。
 *
 * 在真实浏览器里跑一段 JS，逐元素检查是否存在真实的布局缺陷：
 *   - 文字横向溢出容器（scrollWidth > clientWidth，且没有 ellipsis/滚动条兜底）
 *   - 文字纵向被裁切（scrollHeight > clientHeight 且 overflow 为 hidden）
 *   - 元素超出视口右边界 / 下边界
 *   - 图标按钮没有可访问名称（aria-label / title / 文本都为空的空按钮）
 *   - 可点击元素尺寸过小（< 24px，触控与可访问性问题）
 *
 * 输出 JSON，供构建流程判断是否通过。
 *
 * 用法：node webui/scripts/layout-audit.mjs [baseUrl]
 */

import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, '..', '..');

const baseUrl = process.argv[2] || 'http://127.0.0.1:8770';
const routes = ['#/welcome', '#/studio', '#/settings', '#/diagnostics', '#/about'];
const viewports = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'small', width: 1180, height: 720 },
];

const AUDIT_SCRIPT = `
(() => {
  const problems = [];
  const seen = new Set();

  function describe(el) {
    const tag = el.tagName.toLowerCase();
    const cls = (el.className && typeof el.className === 'string')
      ? '.' + el.className.trim().split(/\\s+/).slice(0, 3).join('.')
      : '';
    const text = (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 48);
    return tag + cls + (text ? ' :: "' + text + '"' : '');
  }

  function push(kind, el, detail) {
    const key = kind + '|' + describe(el);
    if (seen.has(key)) return;
    seen.add(key);
    problems.push({ kind, element: describe(el), detail });
  }

  // 元素是否处在「可纵向滚动的容器」里。
  // 设置 / 诊断 / 关于本来就是长页面，内容超出视口是正常设计，不是缺陷；
  // 只有「无法通过滚动看到」的溢出才是真问题。
  function inScrollableAncestor(el) {
    let node = el.parentElement;
    while (node && node !== document.body) {
      const style = getComputedStyle(node);
      const oy = style.overflowY;
      if ((oy === 'auto' || oy === 'scroll') && node.scrollHeight > node.clientHeight + 1) return true;
      node = node.parentElement;
    }
    return false;
  }

  const bodyScrolls = document.documentElement.scrollHeight > window.innerHeight + 1;
  const all = Array.from(document.querySelectorAll('body *'));

  // 「只给读屏软件」的文本（.sr-only / [data-visually-hidden]）是**故意**裁切的：
  // 1×1 像素 + overflow:hidden 就是这个标准写法本身，内容和可访问名都还在。
  // 这些规则要抓的是「可见文字被意外切掉」，所以把这类元素排除掉
  // —— 这不是放宽标准：给无可见标题的页面补 h1 时，只有这条路可走。
  function intendedForScreenReadersOnly(el) {
    return el.classList.contains('sr-only') || el.hasAttribute('data-visually-hidden');
  }

  for (const el of all) {
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
    const srOnly = intendedForScreenReadersOnly(el);

    const hasText = Array.from(el.childNodes).some(
      (n) => n.nodeType === 3 && n.textContent.trim().length > 0
    );
    const rect = el.getBoundingClientRect();
    const scrollable = inScrollableAncestor(el) || bodyScrolls;

    // 1) 文字横向溢出且没有省略号兜底 —— 会压到相邻元素
    if (!srOnly && hasText && el.scrollWidth > el.clientWidth + 1) {
      const overflowsVisible = style.overflowX === 'visible';
      const hasEllipsis = style.textOverflow === 'ellipsis';
      if (overflowsVisible || !hasEllipsis) {
        push('text-overflow-x', el, 'scrollWidth=' + el.scrollWidth + ' > clientWidth=' + el.clientWidth);
      }
    }

    // 2) 文字纵向被裁切且不滚动 —— 用户永远看不到被切掉的部分
    if (!srOnly && hasText && el.scrollHeight > el.clientHeight + 1) {
      if (style.overflowY === 'hidden') {
        push('text-clipped-y', el, 'scrollHeight=' + el.scrollHeight + ' > clientHeight=' + el.clientHeight);
      }
    }

    // 3) 横向超出视口（横向溢出不正常：应该自适应或内部滚动）
    if (rect.width > 0 && rect.height > 0 && rect.right > window.innerWidth + 2) {
      if (!inScrollableAncestor(el)) {
        push('beyond-viewport-x', el, 'right=' + Math.round(rect.right) + ' > viewport=' + window.innerWidth);
      }
    }

    // 4) 纵向超出视口且整页/容器都不能滚动 —— 内容被永久藏起来
    if (rect.width > 0 && rect.height > 0 && rect.bottom > window.innerHeight + 2) {
      if (style.position !== 'fixed' && !scrollable) {
        push('unreachable-y', el, 'bottom=' + Math.round(rect.bottom) + ' > viewport=' + window.innerHeight);
      }
    }

    // 5) 空白图标按钮（无障碍缺陷）
    if ((el.tagName === 'BUTTON' || el.getAttribute('role') === 'button') && rect.width > 0) {
      const name = el.getAttribute('aria-label') || el.getAttribute('title') || (el.textContent || '').trim();
      if (!name) push('button-without-name', el, 'no aria-label / title / text');
    }

    // 6) 可点击元素过小
    if ((el.tagName === 'BUTTON' || el.tagName === 'A') && rect.width > 0 && rect.height > 0) {
      if (rect.height < 24 || rect.width < 24) {
        push('target-too-small', el, Math.round(rect.width) + 'x' + Math.round(rect.height));
      }
    }
  }

  return {
    route: location.hash || '(root)',
    title: document.title,
    viewport: window.innerWidth + 'x' + window.innerHeight,
    pageScrollable: bodyScrolls,
    nodeCount: all.length,
    problems,
  };
})()
`;

async function launch() {
  const { chromium } = await import('playwright');
  return chromium.launch({ channel: 'msedge', headless: true });
}

async function main() {
  // 服务没起来 ≠ 界面有问题。先探一下，避免把 ERR_CONNECTION_REFUSED 当成体检结论。
  try {
    const probe = await fetch(baseUrl + '/', { signal: AbortSignal.timeout(8000) });
    if (!probe.ok) throw new Error('HTTP ' + probe.status);
  } catch (err) {
    console.error('[ERROR] 连不上被测界面 ' + baseUrl + '：' + String(err).slice(0, 160));
    console.error('  布局体检需要一个正在运行的界面服务（见 README-开发者.md）；本次没有测到任何东西。');
    console.error('LAYOUT_AUDIT_UNREACHABLE');
    process.exit(2);
  }

  let browser;
  try {
    browser = await launch();
  } catch (err) {
    console.error('无法启动浏览器进行布局体检：' + err.message);
    console.error('请先安装：cd webui && npm i -D playwright');
    process.exit(2);
  }

  const results = [];
  let total = 0;

  for (const viewport of viewports) {
    const context = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
      deviceScaleFactor: 1,
    });
    for (const route of routes) {
      const page = await context.newPage();
      try {
        await page.goto(baseUrl + '/' + route, { waitUntil: 'networkidle', timeout: 30000 });
      } catch {
        await page.goto(baseUrl + '/' + route, { waitUntil: 'domcontentloaded', timeout: 30000 });
      }
      await page.waitForTimeout(1200);
      const report = await page.evaluate(AUDIT_SCRIPT);
      report.viewportName = viewport.name;
      report.count = report.problems.length;
      total += report.count;
      results.push(report);

      const flag = report.count === 0 ? 'OK  ' : 'WARN';
      console.log(
        `  [${flag}] ${viewport.name.padEnd(8)} ${route.padEnd(14)} ${report.count} 个问题  (${report.nodeCount} 个元素)`
      );
      for (const problem of report.problems.slice(0, 12)) {
        console.log(`         - ${problem.kind}: ${problem.element}  ${problem.detail}`);
      }
      await page.close();
    }
    await context.close();
  }

  await browser.close();

  const outDir = resolve(root, 'build', 'work');
  if (!existsSync(outDir)) mkdirSync(outDir, { recursive: true });
  writeFileSync(resolve(outDir, 'layout-audit.json'), JSON.stringify(results, null, 2), 'utf8');

  console.log('');
  console.log(`布局体检合计：${total} 个问题（明细：build/work/layout-audit.json）`);
  process.exit(total === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
