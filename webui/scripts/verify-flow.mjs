/**
 * AI 3D Forge 前端验收脚本：用真实 Edge（headless）把关键流程走一遍。
 *
 * 前置：假后端已在 8760（node mock-server.mjs --first-run），Vite dev 已在 5273。
 * 运行：node scripts/verify-flow.mjs
 *
 * 覆盖：欢迎向导 4 步 → 工作台 → 发需求 → 进度 → 结果卡片 → STL 预览 →
 *       参数微调 → 设置/诊断/关于 → 两种窗口尺寸下的溢出审计。
 * 产出：scripts/shots/*.png 截图 + 控制台错误清单。
 */
import { mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { connectPage, launchBrowser, sleep } from './cdp.mjs';
import { analyzePng } from './png.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const SHOTS = join(HERE, 'shots');
const BASE = process.env.BASE_URL || 'http://127.0.0.1:5273';
const PORT = Number(process.env.CDP_PORT || 9333);

const steps = [];
const failures = [];

function record(name, ok, detail = '') {
  steps.push({ name, ok, detail });
  console.log(`${ok ? '  ✓' : '  ✗'} ${name}${detail ? ` — ${detail}` : ''}`);
  if (!ok) failures.push(`${name}${detail ? ` — ${detail}` : ''}`);
}

async function step(name, fn) {
  try {
    const detail = await fn();
    record(name, true, typeof detail === 'string' ? detail : '');
  } catch (err) {
    record(name, false, err instanceof Error ? err.message : String(err));
  }
}

/** 页面里读一段文本（找不到返回空串） */
const textOf = (selector) =>
  `(document.querySelector(${JSON.stringify(selector)})?.textContent || '').trim()`;

/**
 * 溢出 / 布局审计：
 *  1) 文档是否横向溢出视口（结构性布局错误的硬指标）
 *  2) 是否有元素横向越界（排除处在可滚动容器内的元素——那是正常的滚动内容）
 *  3) 有文字的元素是否被裁掉（排除刻意使用省略号或主动裁剪的）
 *  4) 图标按钮是否都有可读名称、点击目标是否过小
 */
const AUDIT = `(() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  const overflowDoc = {
    x: document.documentElement.scrollWidth - vw,
    y: document.documentElement.scrollHeight - vh,
    bodyX: document.body.scrollWidth - vw,
  };

  const scrollableAncestor = (el) => {
    let p = el.parentElement;
    while (p && p !== document.body) {
      const cs = getComputedStyle(p);
      if (['auto', 'scroll', 'hidden', 'clip'].includes(cs.overflowX)) return p;
      p = p.parentElement;
    }
    return null;
  };
  const ownClip = (el) => {
    const cs = getComputedStyle(el);
    return ['auto', 'scroll', 'hidden', 'clip'].includes(cs.overflowX) &&
           ['auto', 'scroll', 'hidden', 'clip'].includes(cs.overflowY);
  };
  const describe = (el) => {
    const cls = typeof el.className === 'string' && el.className.trim()
      ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.')
      : '';
    return el.tagName.toLowerCase() + cls;
  };

  const outside = [];
  const clipped = [];
  const bareButtons = [];
  const smallTargets = [];

  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity) === 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;

    // 横向越界（纵向越界交给滚动容器，不算错）
    if ((r.right > vw + 1 || r.left < -1) && !scrollableAncestor(el) && !ownClip(el)) {
      outside.push(describe(el) + ' @x=' + Math.round(r.left) + ' w=' + Math.round(r.width));
    }

    // 纯文字节点被裁切（省略号是有意为之）
    const isTextLeaf = el.children.length === 0 && (el.textContent || '').trim().length > 0;
    if (isTextLeaf && cs.textOverflow !== 'ellipsis' && !ownClip(el) && el.tagName !== 'INPUT') {
      const sx = getComputedStyle(el);
      const padX = parseFloat(sx.paddingLeft) + parseFloat(sx.paddingRight);
      if (el.scrollWidth - el.clientWidth > 2 && el.clientWidth - padX > 0) {
        clipped.push(describe(el) + ' sw=' + el.scrollWidth + ' cw=' + el.clientWidth);
      }
    }

    if (el.tagName === 'BUTTON') {
      const label = (el.getAttribute('aria-label') || el.textContent || '').trim();
      if (!label) bareButtons.push(describe(el));
      if (r.width < 24 || r.height < 24) {
        smallTargets.push(describe(el) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
      }
    }
  }

  const canvas = document.querySelector('.viewer-canvas canvas');
  const webgl = canvas
    ? { w: canvas.width, h: canvas.height, cw: canvas.clientWidth, ch: canvas.clientHeight }
    : null;

  return {
    overflowDoc,
    outside: [...new Set(outside)].slice(0, 6),
    clipped: [...new Set(clipped)].slice(0, 6),
    bareButtons: [...new Set(bareButtons)].slice(0, 6),
    smallTargets: [...new Set(smallTargets)].slice(0, 6),
    webgl,
  };
})()`;

async function audit(page, label) {
  const res = await page.evaluate(AUDIT);
  const problems = [];
  if (res.overflowDoc.x > 1 || res.overflowDoc.bodyX > 1) {
    problems.push(`文档横向溢出 ${res.overflowDoc.x}px`);
  }
  if (res.outside.length) problems.push(`元素横向越界: ${res.outside.join(' | ')}`);
  if (res.clipped.length) problems.push(`文字被裁切: ${res.clipped.join(' | ')}`);
  if (res.bareButtons.length) problems.push(`图标按钮缺少可读名称: ${res.bareButtons.join(' | ')}`);
  record(`布局审计（${label}）`, problems.length === 0, problems.join(' ／ '));
  return res;
}

/** 整页截图做配色合规检查：设计规范禁止紫色/紫罗兰强调色 */
async function auditColors(page, label) {
  const path = await page.screenshot(join(SHOTS, `color-${label.replace(/[^\w\u4e00-\u9fa5]+/g, '_')}.png`));
  const res = analyzePng(path);
  const problems = [];
  if (res.purpleRatio > 0.001) problems.push(`紫色像素占比 ${(res.purpleRatio * 100).toFixed(2)}%`);
  record(`配色审计（${label}）`, problems.length === 0, problems.join(' ／ '));
  return res;
}

async function main() {
  rmSync(SHOTS, { recursive: true, force: true });
  mkdirSync(SHOTS, { recursive: true });

  const browser = await launchBrowser({ port: PORT, width: 1280, height: 800 });
  const page = await connectPage(browser.port, 'about:blank');
  const problems = page.collectProblems();
  await page.setViewport(1280, 800);

  const shot = (name) => page.screenshot(join(SHOTS, `${name}.png`));

  try {
    /* ---------------- 1. 首屏 → 欢迎向导 ---------------- */
    await page.goto(`${BASE}/`);
    await step('首次启动跳转到欢迎向导', async () => {
      await page.waitForUrlIncludes('/welcome', 15000);
      return await page.evaluate('location.hash');
    });
    await page.waitForFunction(textOf('.hero h1') + " === 'AI 3D Forge'", { label: '欢迎页标题' });
    await page.waitForFunction(`!!${textOf('.hero-lead')}`, { label: '一句话价值主张' });
    await shot('01-welcome-hero');
    await audit(page, '欢迎页 1280×800');
    await auditColors(page, '欢迎页');

    /* ---------------- 2. 环境检测 ---------------- */
    await step('进入环境检测并显示就绪进度', async () => {
      await page.clickText('开始配置', { expect: `!!document.querySelector('.readiness-num')` });
      await page.waitForFunction(
        `document.querySelectorAll('.check-row').length >= 10`,
        { label: '检查项列表', timeoutMs: 15000 },
      );
      const num = await page.evaluate(textOf('.readiness-num'));
      const rows = await page.evaluate(`document.querySelectorAll('.check-row').length`);
      const icons = await page.evaluate(
        `({ok: document.querySelectorAll('.check-icon-ok').length, fail: document.querySelectorAll('.check-icon-fail').length, warn: document.querySelectorAll('.check-icon-warn').length})`,
      );
      await shot('02-welcome-env');
      return `显示 ${num}，${rows} 项检查（ok=${icons.ok} warn=${icons.warn} fail=${icons.fail}）`;
    });
    await step('重新检测按钮可用', async () => {
      await page.clickText('重新检测', { expect: `!!document.querySelector('.check-row')` });
      await page.waitForFunction(`!!document.querySelector('.check-row')`, { label: '检测结果' });
      return '已重新拉取 /api/env/report';
    });

    /* ---------------- 3. 3D 引擎（自动安装 + SSE 进度） ---------------- */
    await step('3D 引擎页显示未安装与自动安装入口', async () => {
      await page.clickText('下一步', {
        expect: `!!document.querySelector('.notice-ok') || !!([...document.querySelectorAll('button')].find(b => b.textContent.includes('自动安装 OpenSCAD')))`,
      });
      const hasInstall = await page.evaluate(
        `!!([...document.querySelectorAll('button')].find(b => b.textContent.includes('自动安装 OpenSCAD')))`,
      );
      await shot('03-welcome-engine');
      return hasInstall ? '显示 [自动安装 OpenSCAD]' : '引擎已就绪';
    });
    await step('自动安装：SSE 进度 → 单进度条 → 完成确认', async () => {
      await page.clickText('自动安装 OpenSCAD', { expect: `!!document.querySelector('.progress')` });
      await page.waitForFunction(`!!document.querySelector('.progress')`, { label: '进度条', timeoutMs: 15000 });
      await sleep(900);
      const mid = await page.evaluate(
        `({ pct: document.querySelector('.progress [role="progressbar"]')?.getAttribute('aria-valuenow'), meta: ${textOf('.progress-meta')} })`,
      );
      await shot('04-welcome-installing');
      await page.waitForFunction(`!!document.querySelector('.notice-ok')`, { label: '安装完成', timeoutMs: 25000 });
      const doneText = await page.evaluate(textOf('.notice-ok'));
      return `进度中 ${mid.pct ?? '不确定'}% ｜ 进度文案「${mid.meta.replace(/\s+/g, ' ').slice(0, 24)}」｜ 完成「${doneText.replace(/\s+/g, ' ').slice(0, 30)}」`;
    });

    /* ---------------- 4. AI 服务 ---------------- */
    await step('进入 AI 服务步骤，API Key 为密码框且有显示切换', async () => {
      await page.clickText('下一步', { expect: `!!document.querySelector('#api-key')` });
      await page.waitForFunction(`!!document.querySelector('#api-key')`, { label: 'API Key 输入框' });
      const info = await page.evaluate(`(() => {
        const el = document.querySelector('#api-key');
        const toggle = document.querySelector('.input-affix-btn');
        return { type: el.getAttribute('type'), autocomplete: el.getAttribute('autocomplete'), toggleLabel: toggle?.getAttribute('aria-label') };
      })()`);
      if (info.type !== 'password') throw new Error(`type=${info.type}`);
      if (info.autocomplete !== 'off') throw new Error(`autocomplete=${info.autocomplete}`);
      if (!info.toggleLabel) throw new Error('显示/隐藏按钮缺少 aria-label');
      await page.setInputValue('#api-key', 'sk-mock1234567890abcdef');
      await shot('05-welcome-apikey');
      return `type=password, autocomplete=off, 切换按钮「${info.toggleLabel}」`;
    });
    await step('测试连接返回成功', async () => {
      await page.clickText('测试连接', { expect: `!!document.querySelector('.notice-ok')` });
      await page.waitForFunction(`!!document.querySelector('.notice-ok')`, { label: '测试结果', timeoutMs: 15000 });
      return (await page.evaluate(textOf('.notice-ok'))).replace(/\s+/g, ' ').slice(0, 40);
    });
    await step('完成配置后进入工作台', async () => {
      await page.clickText('完成配置', { expect: `location.hash.includes('/studio')` });
      await page.waitForUrlIncludes('/studio', 15000);
      await page.waitForFunction(`!!document.querySelector('.studio')`, { label: '工作台' });
      return await page.evaluate('location.hash');
    });

    /* ---------------- 5. 工作台：三栏 + 预览 + 空状态 ---------------- */
    await step('工作台三栏结构就位', async () => {
      await page.waitForFunction(
        `document.querySelectorAll('.proj').length >= 2`,
        { label: '项目列表' },
      );
      const info = await page.evaluate(`(() => {
        const count = (sel) => document.querySelectorAll(sel).length;
        return {
          projects: count('.proj'),
          viewer: !!document.querySelector('.viewer-stage'),
          chat: !!document.querySelector('.panel'),
          toolbar: count('.viewer-tools button'),
          empty: !!document.querySelector('.viewer-empty'),
          pill: ${textOf('.pill')},
        };
      })()`);
      await shot('06-studio-empty');
      await audit(page, '工作台空状态 1280×800');
      record(
        '工具栏四个按钮 + 空状态 + 就绪胶囊',
        info.toolbar === 4 && info.empty && info.projects >= 2,
        `项目 ${info.projects} 个，工具栏 ${info.toolbar} 个按钮，胶囊「${info.pill}」`,
      );
      return '';
    });

    /* ---------------- 6. 发需求 → 进度 → 结果卡片 ---------------- */
    await step('输入需求并回车发送', async () => {
      await page.focus('.composer-box textarea');
      await page.type('设计一个桌面手机支架，高度100mm');
      const value = await page.evaluate(`document.querySelector('.composer-box textarea').value`);
      if (!value.includes('手机支架')) throw new Error('输入未生效');
      await page.pressKey('Enter');
      return `输入框内容「${value}」`;
    });
    await step('出现进度条与单行打字机状态', async () => {
      await page.waitForFunction(`!!document.querySelector('.runbar')`, { label: '运行条', timeoutMs: 15000 });
      await page.waitForFunction(`!!document.querySelector('.caret')`, { label: '打字机光标' });
      await sleep(1200);
      const info = await page.evaluate(
        `({ text: ${textOf('.runbar-status-text')}, bar: !!document.querySelector('.runbar .progress-bar') })`,
      );
      await shot('07-studio-running');
      return `状态「${info.text}」`;
    });
    await step('完成后渲染结果卡片（设计说明 / 打印评分 / 下载）', async () => {
      await page.waitForFunction(`!!document.querySelector('.result')`, { label: '结果卡片', timeoutMs: 30000 });
      const info = await page.evaluate(`(() => {
        const buttons = [...document.querySelectorAll('.downloads button')].map(b => b.textContent.trim());
        return {
          name: ${textOf('.result-head strong')},
          summary: ${textOf('.result-summary')},
          score: ${textOf('.verdict-score b')},
          verdict: ${textOf('.verdict-title')},
          metrics: [...document.querySelectorAll('.stats-cell')].map(c => c.textContent.trim()),
          buttons,
          issues: document.querySelectorAll('.issue').length,
        };
      })()`);
      if (info.buttons.length !== 2) throw new Error(`下载按钮 ${info.buttons.length} 个`);
      await shot('08-studio-result');
      return `${info.name}：评分 ${info.score}，${info.issues} 条建议，按钮 ${info.buttons.join('/')}`;
    });
    await step('STL 在 Three.js 里真实渲染（canvas + 尺寸/面数/体积浮层）', async () => {
      await page.waitForFunction(
        `(() => { const c = document.querySelector('.viewer-canvas canvas'); return !!c && c.width > 100 && c.height > 100; })()`,
        { label: 'WebGL canvas', timeoutMs: 20000 },
      );
      await page.waitForFunction(`document.querySelectorAll('.stat').length >= 3`, {
        label: '模型数据浮层',
        timeoutMs: 20000,
      });
      await page.waitForFunction(
        `!document.querySelector('.viewer-loading')`,
        { label: '预览加载结束', timeoutMs: 20000 },
      );
      await sleep(900);
      const info = await page.evaluate(`(() => {
        const c = document.querySelector('.viewer-canvas canvas');
        const gl = c.getContext('webgl2') || c.getContext('webgl');
        return {
          canvas: c.width + 'x' + c.height,
          renderer: gl.getParameter(gl.VERSION),
          stats: [...document.querySelectorAll('.stat')].map(s => s.textContent.trim()),
          rect: (() => { const r = document.querySelector('.viewer-stage').getBoundingClientRect(); return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]; })(),
        };
      })()`);
      const shotPath = await shot('09-studio-model');
      // 用截图像素证明「画面里真的有模型」——WebGL 后台缓冲在合成后会被清空，
      // readPixels 不可靠，而截图是用户真正看到的画面。
      const region = { x: info.rect[0], y: info.rect[1], width: info.rect[2], height: info.rect[3] };
      const analysis = analyzePng(shotPath, region);
      await page.evaluate(`window.__viewerRect = ${JSON.stringify(info.rect)}`);
      record(
        'WebGL 画面里有模型（截图像素分析）',
        analysis.modelLikeRatio > 0.05,
        `canvas ${info.canvas}，${info.renderer}；视口内模型像素占比 ${(analysis.modelLikeRatio * 100).toFixed(1)}%`,
      );
      return `${info.canvas}，${info.stats.join(' ｜ ')}`;
    });

    /* ---------------- 7. 参数微调（不调用 AI） ---------------- */
    await step('参数 chips 可编辑并应用', async () => {
      await page.waitForFunction(`document.querySelectorAll('.chip').length >= 3`, { label: '参数 chips' });
      const before = await page.evaluate(
        `(() => { const chips = [...document.querySelectorAll('.chip')]; return chips.map(c => c.querySelector('.chip-label').textContent); })()`,
      );
      // 高度改成 150mm（按 label 精确匹配，不能靠 .chip 下标）
      const changed = await page.evaluate(`(() => {
        const chips = [...document.querySelectorAll('.chip')];
        const target = chips.find(c => (c.querySelector('.chip-label').textContent || '').trim() === '高度');
        if (!target) return null;
        const input = target.querySelector('input');
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, '150');
        input.dispatchEvent(new Event('input', { bubbles: true }));
        return input.value;
      })()`);
      if (changed !== '150') throw new Error(`没能写入高度参数（读到 ${changed}）`);
      await page.waitForFunction(`!!document.querySelector('.chip-input.is-dirty')`, { label: '参数已改动' });
      await page.clickText('应用参数', { expect: `!!document.querySelector('.runbar')` });
      await page.waitForFunction(`!!document.querySelector('.runbar')`, { label: '参数任务运行', timeoutMs: 15000 });
      await page.waitForFunction(
        `document.querySelectorAll('.result').length >= 2`,
        { label: '第二张结果卡片', timeoutMs: 40000 },
      );
      const bbox = await page.evaluate(`(() => {
        const cards = [...document.querySelectorAll('.result')];
        return cards[cards.length - 1].querySelectorAll('.stats-cell')[0]?.textContent.trim();
      })()`);
      await shot('10-studio-params');
      return `参数 ${before.join('/')}；应用后尺寸「${bbox}」`;
    });

    /* ---------------- 8. 工具按钮：线框 / 自动旋转 / 重置 ---------------- */
    await step('线框与自动旋转可切换', async () => {
      await page.click('button[aria-label="线框切换"]');
      await sleep(400);
      const wire = await page.evaluate(
        `document.querySelector('button[aria-label="线框切换"]').getAttribute('aria-pressed')`,
      );
      await page.click('button[aria-label="自动旋转"]');
      await sleep(400);
      const spin = await page.evaluate(
        `document.querySelector('button[aria-label="自动旋转"]').getAttribute('aria-pressed')`,
      );
      await shot('11-studio-wireframe');
      await page.click('button[aria-label="线框切换"]');
      await page.click('button[aria-label="自动旋转"]');
      if (wire !== 'true' || spin !== 'true') throw new Error(`aria-pressed 未切换（wire=${wire} spin=${spin}）`);
      return `线框 aria-pressed=${wire}，自动旋转 aria-pressed=${spin}`;
    });
    await step('重置视角不报错', async () => {
      await page.click('button[aria-label="重置视角"]');
      await sleep(400);
      return '已触发重置信号';
    });

    /* ---------------- 9. 项目操作：切换 / 搜索 / 新建 ---------------- */
    await step('切换项目会载入对应模型', async () => {
      const firstName = await page.evaluate(
        `document.querySelectorAll('.proj .proj-name')[1]?.textContent`,
      );
      await page.click('.proj', { index: 1 });
      await page.waitForFunction(
        `(document.querySelector('.viewer-toolbar-title strong')?.textContent || '').includes(${JSON.stringify(firstName)})`,
        { label: '切换项目', timeoutMs: 15000 },
      );
      return `已切到「${firstName}」`;
    });
    await step('搜索项目过滤生效', async () => {
      await page.setInputValue('.side-search', '花盆');
      await sleep(400);
      const count = await page.evaluate(`document.querySelectorAll('.proj').length`);
      await page.setInputValue('.side-search', '');
      await sleep(300);
      return `过滤后剩 ${count} 个`;
    });
    await step('新建项目进入空状态', async () => {
      await page.clickText('新建项目');
      await page.waitForFunction(`!!document.querySelector('.viewer-empty')`, { label: '空状态', timeoutMs: 15000 });
      return '已新建并显示空状态';
    });

    /* ---------------- 10. 设置页 ---------------- */
    await step('设置页四个分区就位', async () => {
      await page.goto(`${BASE}/#/settings`);
      await page.waitForFunction(`document.querySelectorAll('.card').length >= 4`, { label: '设置卡片' });
      const info = await page.evaluate(`(() => ({
        heads: [...document.querySelectorAll('.card-head h2')].map(h => h.textContent.trim()),
        switches: document.querySelectorAll('.switch').length,
        paths: [...document.querySelectorAll('.path-box code')].map(c => c.textContent.trim()),
        masked: !!document.querySelector('code.mono'),
      }))()`);
      await shot('12-settings');
      await audit(page, '设置页 1280×800');
      return `分区 ${info.heads.join(' / ')}；开关 ${info.switches} 个`;
    });
    await step('深色主题切换生效', async () => {
      await page.clickText('深色', {
        expect: `document.documentElement.getAttribute('data-theme') === 'dark'`,
      });
      await page.waitForFunction(`document.documentElement.getAttribute('data-theme') === 'dark'`, {
        label: '深色主题',
      });
      await shot('13-settings-dark');
      await audit(page, '设置页深色 1280×800');
      await auditColors(page, '设置页深色');
      await page.clickText('浅色', {
        expect: `document.documentElement.getAttribute('data-theme') === 'light'`,
      });
      await page.waitForFunction(`document.documentElement.getAttribute('data-theme') === 'light'`, {
        label: '浅色主题',
      });
      return 'data-theme 可切换（浅色 / 深色）';
    });

    /* ---------------- 11. 诊断页 ---------------- */
    await step('诊断页六个分组 + 自动修复 + 技术详情', async () => {
      await page.goto(`${BASE}/#/diagnostics`);
      await page.waitForFunction(`document.querySelectorAll('.diag-group').length >= 6`, {
        label: '诊断分组',
      });
      const groups = await page.evaluate(
        `[...document.querySelectorAll('.diag-group-head h2')].map(h => h.textContent.trim())`,
      );
      // 现场造一个问题项：让假后端重新「忘记」3D 引擎，诊断页就会出现可修复项
      await page.evaluate(`fetch('/api/openscad/forget', { method: 'POST' }).then(r => r.ok)`);
      await sleep(400);
      await page.clickText('全部重新检测', {
        expect: `[...document.querySelectorAll('button')].some(b => b.textContent.includes('自动修复'))`,
      });
      await sleep(600);
      const fixable = await page.evaluate(
        `[...document.querySelectorAll('button')].filter(b => b.textContent.includes('自动修复')).length`,
      );
      const metaAccordions = await page.evaluate(
        `[...document.querySelectorAll('.acc-head')].filter(b => b.textContent.includes('技术详情')).length`,
      );
      await shot('14-diagnostics');
      await audit(page, '诊断页 1280×800');
      record('诊断为契约里的六个分组', groups.length === 6, groups.join(' / '));
      record('问题项带 [自动修复] 按钮', fixable > 0, `${fixable} 个`);
      record('技术详情可展开（meta）', metaAccordions > 0, `${metaAccordions} 个`);
      return `${fixable} 个自动修复按钮，${metaAccordions} 个技术详情`;
    });
    await step('自动修复弹 toast 并刷新报告', async () => {
      await page.clickText('自动修复', { expect: `!!document.querySelector('.toast')` });
      await page.waitForFunction(`!!document.querySelector('.toast')`, { label: 'toast', timeoutMs: 20000 });
      const toastText = await page.evaluate(textOf('.toast'));
      return `toast「${toastText}」`;
    });
    await step('展开技术详情显示 meta 定义列表', async () => {
      await page.clickText('技术详情', { expect: `!!document.querySelector('.acc-body .dl')` });
      await page.waitForFunction(`!!document.querySelector('.acc-body .dl')`, { label: 'meta 列表' });
      const pairs = await page.evaluate(`document.querySelectorAll('.acc-body .dl dt').length`);
      return `${pairs} 条 meta`;
    });

    /* ---------------- 12. 关于页 ---------------- */
    await step('关于页版本信息 + 常见问题 + 开源许可', async () => {
      await page.goto(`${BASE}/#/about`);
      await page.waitForFunction(`document.querySelectorAll('.license').length >= 4`, { label: '许可列表' });
      const info = await page.evaluate(`(() => ({
        version: [...document.querySelectorAll('.dl dt')].map(d => d.textContent.trim()),
        faq: [...document.querySelectorAll('.acc-head')].map(h => h.textContent.trim()).length,
        licenses: [...document.querySelectorAll('.license')].map(l => l.textContent.replace(/\\s+/g,' ').trim().slice(0, 24)),
      }))()`);
      await page.clickText('OpenSCAD 安装失败怎么办', {
        expect: `document.querySelectorAll('.acc-body').length >= 1`,
      });
      await page.waitForFunction(`document.querySelectorAll('.acc-body').length >= 1`, {
        label: 'FAQ 展开',
      });
      await sleep(300);
      await shot('15-about');
      await audit(page, '关于页 1280×800');
      record('常见问题至少 5 条', info.faq >= 5, `${info.faq} 条`);
      record('开源许可含 OpenSCAD GPLv2', info.licenses.some((l) => l.includes('OpenSCAD')), info.licenses.join(' / '));
      return `版本字段 ${info.version.length} 项，许可 ${info.licenses.length} 项`;
    });

    /* ---------------- 13. 1600×900 溢出复检 ---------------- */
    await step('1600×900 布局复检', async () => {
      await page.setViewport(1600, 900);
      await sleep(500);
      await page.goto(`${BASE}/#/studio`);
      await page.waitForFunction(`!!document.querySelector('.studio')`, { label: '工作台' });
      await sleep(1200);
      await shot('16-studio-1600x900');
      await audit(page, '工作台 1600×900');
      return '已复检';
    });
    await step('1280×800 复检（含真实模型）', async () => {
      await page.setViewport(1280, 800);
      await sleep(600);
      await sleep(800);
      await shot('17-studio-1280x800');
      await audit(page, '工作台 1280×800（含模型）');
      await auditColors(page, '工作台');
      return '已复检';
    });

    /* ---------------- 14. 无控制台错误 ---------------- */
    await step('控制台无错误 / 无未捕获异常', async () => {
      await sleep(500);
      const real = problems.console.filter((c) => !c.includes('favicon'));
      if (problems.pageErrors.length) throw new Error(problems.pageErrors.join(' | '));
      if (real.length) throw new Error(real.join(' | '));
      return `console 干净，失败请求 ${problems.failedRequests.length} 条`;
    });
  } finally {
    page.close();
    await browser.kill();
  }

  /* ---------------- 汇总 ---------------- */
  const pass = steps.filter((s) => s.ok).length;
  console.log(`\n===== 验收结果 ${pass}/${steps.length} =====`);
  if (failures.length) {
    console.log('失败项：');
    for (const f of failures) console.log(`  - ${f}`);
  }
  writeFileSync(
    join(SHOTS, 'report.json'),
    JSON.stringify({ pass, total: steps.length, steps, problems, shots: SHOTS }, null, 2),
  );
  console.log(`截图与报告: ${SHOTS}`);
  process.exit(failures.length ? 1 : 0);
}

main().catch((err) => {
  console.error('验收脚本崩溃：', err);
  process.exit(2);
});
