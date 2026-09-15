/**
 * AI 3D Forge —— 规格确认卡片 / 改动核对结论 / **停止生成** 的布局与行为体检。
 *
 * 为什么单独做这一支：`layout-audit.mjs` 只体检静态路由，
 * 而这几处界面只在「一次真实作业进行到某个阶段」时才存在。
 * 它们都是「坏了用户就点不动」的关键界面（确认卡片没了，作业就在服务端死等），
 * 所以必须真的渲染出来检查：文字有没有被裁、有没有横向溢出、
 * 按钮有没有可访问名称、卡片在窄窗口下会不会撑破。
 *
 * 其中「停止后再也发不出消息」是实测踩过的真实故障：
 * 后端推了 `cancelled` 事件，前端 switch 漏了分支 → 帧被静默丢掉 →
 * 作业状态永远停在 running → 发送键**永久禁用**，用户除了刷新没别的出路。
 * 所以这里必须真的点一次停止，并确认发送键恢复可点、内容没被清掉、下一条能发出去。
 *
 * 依赖一个假的 API（webui/mock-server.mjs），所以要先把假后端与前端 dev server 起起来：
 *   node webui/mock-server.mjs                      # 假后端，默认 8760
 *   cd webui && npm run dev                         # 前端 dev server，5273（proxy → 8760）
 *   node webui/scripts/spec-card-audit.mjs http://127.0.0.1:5273
 */

import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, '..', '..');

const baseUrl = process.argv[2] || 'http://127.0.0.1:5273';
const shotDir = resolve(root, 'build', 'work', 'spec-cards');

/** 逐元素体检（与 layout-audit.mjs 同一套判据，保持口径一致） */
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

  // 与 layout-audit.mjs 保持同一条口径：「只给读屏软件」的文本（.sr-only）是
  // **故意**裁切的（1×1 像素 + overflow:hidden 就是这个标准写法本身），
  // 不属于「可见文字被意外切掉」。
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

    if (!srOnly && hasText && el.scrollWidth > el.clientWidth + 1) {
      if (style.overflowX === 'visible' || style.textOverflow !== 'ellipsis') {
        push('text-overflow-x', el, 'scrollWidth=' + el.scrollWidth + ' > clientWidth=' + el.clientWidth);
      }
    }
    if (!srOnly && hasText && el.scrollHeight > el.clientHeight + 1 && style.overflowY === 'hidden') {
      push('text-clipped-y', el, 'scrollHeight=' + el.scrollHeight + ' > clientHeight=' + el.clientHeight);
    }
    if (rect.width > 0 && rect.height > 0 && rect.right > window.innerWidth + 2) {
      if (!inScrollableAncestor(el)) {
        push('beyond-viewport-x', el, 'right=' + Math.round(rect.right) + ' > viewport=' + window.innerWidth);
      }
    }
    if (rect.width > 0 && rect.height > 0 && rect.bottom > window.innerHeight + 2) {
      if (style.position !== 'fixed' && !scrollable) {
        push('unreachable-y', el, 'bottom=' + Math.round(rect.bottom) + ' > viewport=' + window.innerHeight);
      }
    }
    if ((el.tagName === 'BUTTON' || el.getAttribute('role') === 'button') && rect.width > 0) {
      const name = el.getAttribute('aria-label') || el.getAttribute('title') || (el.textContent || '').trim();
      if (!name) push('button-without-name', el, 'no aria-label / title / text');
    }
    if ((el.tagName === 'BUTTON' || el.tagName === 'A') && rect.width > 0 && rect.height > 0) {
      if (rect.height < 24 || rect.width < 24) {
        push('target-too-small', el, Math.round(rect.width) + 'x' + Math.round(rect.height));
      }
    }
  }
  return { viewport: window.innerWidth + 'x' + window.innerHeight, nodeCount: all.length, problems };
})()
`;

const results = [];
let failures = 0;

function step(name, ok, detail = '') {
  if (!ok) failures += 1;
  console.log(`  ${ok ? '[OK]  ' : '[FAIL]'} ${name}` + (detail ? ` —— ${detail}` : ''));
}

/**
 * 停止生成后，发送键必须恢复可点。
 *
 * 关键细节：发送键在**输入框为空**时本来就是禁用的，所以必须先往输入框里
 * 放文字，测的才是「是不是被生成状态锁住了」。第一版体检脚本漏了这一步，
 * 结果把「空输入框导致的正常禁用」误报成故障 —— 测界面时必须想清楚
 * 「这个禁用到底是谁造成的」，否则会得出完全错误的结论。
 */
async function checkCancelUnlocks(page) {
  const composer = page.locator('textarea').first();
  const sendBtn = page.locator('.composer-send');

  await composer.fill('设计一个桌面手机支架，高度100mm，底座宽度80mm');
  await composer.press('Enter');
  await page.waitForSelector('.runbar', { timeout: 20000 }).catch(() => undefined);
  step('再次生成时运行条出现', (await page.locator('.runbar').count()) > 0);

  const stopBtn = page.locator('.panel-head button').first();
  if ((await stopBtn.count()) === 0) {
    step('运行中存在「停止」按钮', false, '没找到停止按钮');
    return;
  }
  await stopBtn.click();

  // 先把文字放进输入框，否则发送键会因为「空输入」而禁用，测不出真问题
  await composer.fill('停止后我还能不能发消息');
  await page.waitForTimeout(400);

  let recoveredAt = null;
  for (let second = 1; second <= 30; second += 1) {
    if (!(await sendBtn.isDisabled())) {
      recoveredAt = second;
      break;
    }
    await page.waitForTimeout(1000);
  }
  step(
    '点「停止生成」后发送键恢复可点',
    recoveredAt !== null,
    recoveredAt === null ? '30 秒后仍然禁用（用户被锁死，只能刷新）' : `t+${recoveredAt}s 恢复`,
  );
  if (recoveredAt === null) return;

  const draft = await composer.inputValue();
  step('停止不会清空已经打了一半的字', draft === '停止后我还能不能发消息', JSON.stringify(draft));

  await sendBtn.click();
  await page.waitForTimeout(1500);
  const bubbles = await page.locator('.msg-user .bubble').allInnerTexts();
  step(
    '恢复后确实能发出下一条消息',
    bubbles.some((t) => t.includes('停止后我还能不能发消息')),
    `最后一条用户消息=${JSON.stringify(bubbles.slice(-1))}`,
  );
}

async function main() {
  // 服务没起来 ≠ 规格卡片有问题。先探一下，别把连接错误当体检结论。
  try {
    const probe = await fetch(baseUrl + '/', { signal: AbortSignal.timeout(8000) });
    if (!probe.ok) throw new Error('HTTP ' + probe.status);
  } catch (err) {
    console.error('[ERROR] 连不上被测界面 ' + baseUrl + '：' + String(err).slice(0, 160));
    console.error('  本次没有测到任何东西；请先启动界面服务（见 README-开发者.md）。');
    console.error('SPEC_CARD_AUDIT_UNREACHABLE');
    process.exit(2);
  }

  const { chromium } = await import('playwright');
  let browser;
  try {
    browser = await chromium.launch({ channel: 'msedge', headless: true });
  } catch (err) {
    console.error('无法启动浏览器：' + err.message);
    process.exit(2);
  }
  if (!existsSync(shotDir)) mkdirSync(shotDir, { recursive: true });

  for (const viewport of [
    { name: 'desktop', width: 1440, height: 900 },
    { name: 'narrow', width: 1180, height: 720 },
  ]) {
    const context = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
      deviceScaleFactor: 1,
    });
    const page = await context.newPage();
    page.on('pageerror', (err) => step('页面无脚本异常', false, err.message));

    await page.goto(`${baseUrl}/#/studio`, { waitUntil: 'networkidle', timeout: 40000 });
    await page.waitForTimeout(800);

    // 发一条带具体数字的需求，触发「规格确认」关卡
    const composer = page.locator('textarea').first();
    await composer.fill('设计一个桌面手机支架，高度100mm，底座宽度80mm');
    await composer.press('Enter');

    // 等规格卡片出现
    let appeared = false;
    try {
      await page.waitForSelector('.spec-card', { timeout: 20000 });
      appeared = true;
    } catch {
      appeared = false;
    }
    step('规格确认卡片已渲染', appeared);
    if (!appeared) {
      await page.screenshot({ path: resolve(shotDir, `${viewport.name}-no-spec.png`) });
      results.push({ viewport: viewport.name, problems: [{ kind: 'spec-card-missing' }] });
      await page.close();
      await context.close();
      continue;
    }

    step(
      '卡片带出了「你指定」的尺寸',
      (await page.locator('.spec-cell.is-user').count()) > 0,
      `${await page.locator('.spec-cell.is-user').count()} 项`,
    );
    step(
      '未说清的项被如实列出',
      (await page.locator('.spec-questions li').count()) > 0,
      `${await page.locator('.spec-questions li').count()} 条`,
    );
    step(
      '确认按钮可用',
      await page.locator('.spec-actions button').first().isEnabled(),
    );

    const before = await page.evaluate(AUDIT_SCRIPT);
    before.viewportName = viewport.name;
    before.state = 'spec-card';
    before.count = before.problems.length;
    results.push(before);
    await page.screenshot({ path: resolve(shotDir, `${viewport.name}-spec-card.png`), fullPage: false });
    step(
      `布局体检（${viewport.name} / 确认卡片）`,
      before.count === 0,
      `${before.count} 个问题（${before.nodeCount} 个元素）`,
    );
    for (const p of before.problems.slice(0, 10)) {
      console.log(`         - ${p.kind}: ${p.element}  ${p.detail}`);
    }

    // 展开编辑态：输入框必须撑得住，而且改了数字能被带回去
    await page.locator('.spec-edit-toggle').first().click();
    await page.waitForTimeout(200);
    const inputs = page.locator('.spec-input input');
    step('点「改数字」后出现可编辑输入框', (await inputs.count()) > 0, `${await inputs.count()} 个`);
    if ((await inputs.count()) > 0) {
      await inputs.first().fill('135');
      await page.waitForTimeout(120);
      step('改动被标记出来', (await page.locator('.spec-cell.is-dirty').count()) > 0);
    }

    const editing = await page.evaluate(AUDIT_SCRIPT);
    editing.viewportName = viewport.name;
    editing.state = 'spec-editing';
    editing.count = editing.problems.length;
    results.push(editing);
    await page.screenshot({ path: resolve(shotDir, `${viewport.name}-spec-editing.png`) });
    step(
      `布局体检（${viewport.name} / 编辑态）`,
      editing.count === 0,
      `${editing.count} 个问题`,
    );
    for (const p of editing.problems.slice(0, 10)) {
      console.log(`         - ${p.kind}: ${p.element}  ${p.detail}`);
    }

    // 确认 → 卡片应收成只读版，作业继续跑完
    await page.locator('.spec-actions button').first().click();
    await page.waitForTimeout(1500);
    const locked = await page.locator('.spec-card .tag-ok').count();
    if (locked === 0) {
      console.log('      [调试] 确认后的对话内容：');
      console.log(
        (await page.locator('.panel').last().innerText())
          .split('\n')
          .map((l) => `        | ${l}`)
          .slice(-18)
          .join('\n'),
      );
    }
    step('确认后卡片变为只读（已确认）', locked > 0, `${locked} 个标记`);
    await page.screenshot({ path: resolve(shotDir, `${viewport.name}-spec-locked.png`) });

    // ★ 停止生成必须能解锁输入区（实测踩过的真实故障，见文件头说明）
    await checkCancelUnlocks(page);

    await page.close();
    await context.close();
  }

  /* ---------------- 改动核对结论（修改类作业才有） ---------------- */
  const base = baseUrl.replace(/\/$/, '');
  for (const kind of ['ok', 'bad']) {
    // 假后端提供了切换开关，浏览器里点不出来（它是后端判定结果）
    await fetch(`${base}/api/mock/verdict`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind }),
    }).catch(() => undefined);
    const context = await browser.newContext({
      viewport: { width: 1440, height: 900 },
      deviceScaleFactor: 1,
    });
    const page = await context.newPage();
    page.on('pageerror', (err) => step('页面无脚本异常', false, err.message));

    await page.goto(`${baseUrl}/#/studio`, { waitUntil: 'networkidle', timeout: 40000 });
    await page.waitForTimeout(800);
    // 先做一个模型出来（走完整生成，确认关卡由脚本自动过）
    const composer = page.locator('textarea').first();
    await composer.fill('设计一个桌面手机支架，高度100mm，底座宽度80mm');
    await composer.press('Enter');
    await page.waitForSelector('.spec-card', { timeout: 20000 });
    await page.locator('.spec-actions button').first().click();
    await page.waitForSelector('.result', { timeout: 30000 });
    step(`[${kind}] 第一个模型已生成`, (await page.locator('.result').count()) > 0);

    // 再发一条修改指令 → 修改类作业会带回改动核对结论
    const composer2 = page.locator('textarea').first();
    await composer2.fill('高度改成150mm');
    await composer2.press('Enter');
    await page.waitForSelector('.change-verdict', { timeout: 40000 }).catch(() => undefined);
    const cards = await page.locator('.change-verdict').count();
    step(`[${kind}] 改动核对结论已渲染`, cards > 0, `${cards} 个`);
    if (cards > 0) {
      const cls = await page.locator('.change-verdict').first().getAttribute('class');
      step(
        `[${kind}] 结论色调正确（${kind === 'ok' ? '通过=绿' : '有问题=红/黄'}）`,
        kind === 'ok' ? cls.includes('change-verdict-ok') : !cls.includes('change-verdict-ok'),
        cls,
      );
    }
    const report = await page.evaluate(AUDIT_SCRIPT);
    report.viewportName = 'desktop';
    report.state = `change-verdict-${kind}`;
    report.count = report.problems.length;
    results.push(report);
    await page.screenshot({ path: resolve(shotDir, `change-verdict-${kind}.png`), fullPage: true });
    step(
      `布局体检（改动核对结论 / ${kind}）`,
      report.count === 0,
      `${report.count} 个问题（${report.nodeCount} 个元素）`,
    );
    for (const p of report.problems.slice(0, 10)) {
      console.log(`         - ${p.kind}: ${p.element}  ${p.detail}`);
    }
    await page.close();
    await context.close();
  }

  await browser.close();

  const out = resolve(root, 'build', 'work', 'spec-card-audit.json');
  writeFileSync(out, JSON.stringify(results, null, 2), 'utf8');
  console.log('');
  console.log(`规格卡片体检：${failures} 个问题（截图与明细：build/work/spec-cards/）`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
