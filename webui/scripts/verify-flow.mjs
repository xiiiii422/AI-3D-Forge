/**
 * AI 3D Forge 前端验收脚本：用真实 Edge（headless）把关键流程走一遍。
 *
 * 前置：假后端已在 8760（node mock-server.mjs --first-run，**必须是刚新起的**，
 * 否则上一轮跑完留下的 first_run_done 会让第 1 步跳不过欢迎向导），Vite dev 已在 5273。
 * 运行：node scripts/verify-flow.mjs
 *
 * 覆盖：欢迎向导 4 步 → 工作台 → 发需求 → 进度 → 结果卡片 → STL 预览 →
 *       实时尺寸标注（整体 / 单条棱线 / 开关）→ 参数微调 → 重开历史项目的实测数据 →
 *       设置/诊断/关于 → 页面切换动画 → 液态玻璃外观（材质切换 / 透明度 / 降级）→
 *       缺 WebView2 时的页面顶部说明与一键安装 → 文件位置里的真实日志路径 →
 *       回收站（删 → 列 → 恢复 → 彻底删，不可逆操作必须二次确认）→
 *       两种窗口尺寸下的溢出审计。
 * 产出：scripts/shots/<两位编号>-*.png 截图 + scripts/shots/report.json（只清自己写的那批）。
 */
import { mkdirSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { connectPage, launchBrowser, sleep } from './cdp.mjs';
import { analyzePng } from './png.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const SHOTS = join(HERE, 'shots');
// 默认用 localhost 而不是 127.0.0.1：Vite 默认只监听 IPv6 的 ::1，
// 写死 127.0.0.1 会让这个脚本按自己头里的跑法直接死在第 1 步（实测踩过）。
const BASE = process.env.BASE_URL || 'http://localhost:5273';
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

/** 两个矩形是否相交（用来检查尺寸标签有没有被实底浮层压住） */
const overlaps = (a, b) =>
  a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

/** 毫米读数：与 src/viewer/dimensions.ts 的 formatMm 保持同一套取舍 */
const mmText = (v) => (Math.abs(v) < 1 ? v.toFixed(2) : v.toFixed(1));

/* ---------------- 送到切片软件：页面上的按钮与假后端场景开关 ---------------- */

const SEND_BTN =
  "[...document.querySelectorAll('.downloads button')].find((b) => (b.textContent || '').includes('送到切片软件'))";

/** 设置页那张「切片软件」卡（按标题找，不按下标 —— 卡片顺序会变） */
const SLICER_CARD = `(() => {
  const h2 = [...document.querySelectorAll('.card-head h2')].find((el) => el.textContent.trim() === '切片软件');
  return (h2 && h2.closest('.card')) || null;
})()`;

/** 切换假后端的送出场景（ok / forwarded / failed / none）：错误分支只能让后端真的报错才验得出 */
async function setSlicerScenario(page, scenario) {
  const res = await page.evaluate(`(async () => {
    const r = await fetch('/api/mock/slicer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scenario: ${JSON.stringify(scenario)} }),
    });
    return await r.json();
  })()`);
  if (!res || res.ok !== true || res.scenario !== scenario) {
    throw new Error(`切换假后端送出场景失败：${JSON.stringify(res)}`);
  }
}

/** 关掉当前所有 toast：不看干净就读下一条，会把上一条的文案当成这一条的结果 */
async function clearToasts(page) {
  await page.evaluate(`(() => {
    document.querySelectorAll('.toast-close').forEach((b) => b.click());
  })()`);
  await page.waitForFunction(`!document.querySelector('.toast')`, {
    label: '等旧提示消失',
    timeoutMs: 10000,
  });
}

/** 选一个下拉项：写 value 后派 change 事件（React 的 onChange 就挂在这上面） */
async function selectOne(page, selector, value) {
  const options = await page.evaluate(`(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) throw new Error('找不到下拉：' + ${JSON.stringify(selector)});
    const list = [...el.options].map((o) => o.value);
    if (!list.includes(${JSON.stringify(value)})) {
      throw new Error('下拉里没有这一项：' + ${JSON.stringify(value)} + '，实际 ' + list.join('/'));
    }
    el.value = ${JSON.stringify(value)};
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return list;
  })()`);
  await sleep(250);
  return options;
}

/**
 * 尺寸标注叠层的一次快照：标签文字 + 位置 + 浮层里的尺寸读数 + 提示文字。
 * 标签是命令式维护的 HTML（每帧只改 transform），所以只能从 DOM 取证。
 */
const DIM_TAGS_EXPR = `(() => {
  const rectOf = (el) => {
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, w: r.width, h: r.height };
  };
  return {
    tags: [...document.querySelectorAll('.dim-tag')].map((t) => ({
      dim: t.dataset.dim,
      text: (t.textContent || '').trim(),
      rect: rectOf(t),
    })),
    blocks: [...document.querySelectorAll('.viewer-overlay-stats, .viewer-dim-hint')].map((p) => ({
      name: p.className.split(' ')[0],
      rect: rectOf(p),
    })),
    sizeStat: {
      label: [...document.querySelectorAll('.viewer-overlay-stats .stat .stat-label')]
        .map((l) => (l.textContent || '').trim())
        .find((l) => l.includes('尺寸')) || '',
      value: (() => {
        const stat = [...document.querySelectorAll('.viewer-overlay-stats .stat')]
          .find((s) => (s.querySelector('.stat-label')?.textContent || '').includes('尺寸'));
        return (stat?.querySelector('.stat-value')?.textContent || '').replace(/\\s+/g, ' ').trim();
      })(),
    },
    hint: (document.querySelector('.viewer-dim-hint')?.textContent || '')
      .replace(/\\s+/g, ' ').trim(),
    pressed: document.querySelector('button[aria-label="尺寸标注切换"]')?.getAttribute('aria-pressed') ?? '',
  };
})()`;

/**
 * 在页面里解析预览实际拉取的那份 STL（二进制），算出三个方向的跨度。
 * 为什么绕这一圈：尺寸标注的轴向语义只有对着 STL 原始坐标才验得出来 ——
 * OpenSCAD 导出是 Z 向上，所以「高」必须等于 z 跨度。早前预览把 STL 的 Y
 * 当成了高（模型躺倒、标注也标在水平轴上），这段就是那道回归的哨兵。
 * 地址取自 performance 里的 /stl/raw 请求记录，即 viewer 真正用的那一份。
 * 写法约束：这段是模板字符串里的页面代码 —— 不能再套反引号，也不能用
 * 美元符加大括号的插值（会被 Node 这一层先插值），要拼字符串就用加号。
 */
const STL_SPANS_EXPR = `(async () => {
  const url = [...performance.getEntriesByType('resource')]
    .map((e) => e.name).filter((n) => n.includes('/stl/raw')).pop();
  if (!url) throw new Error('页面里没有 /stl/raw 的资源记录');
  const buf = await (await fetch(url)).arrayBuffer();
  if ((buf.byteLength - 84) % 50 !== 0) throw new Error('预览用的 STL 不是二进制，解析器需要扩展');
  const dv = new DataView(buf);
  const count = dv.getUint32(80, true); // 二进制 STL：80 字节头 + 4 字节面数，数据从 84 开始
  if (buf.byteLength !== 84 + count * 50) {
    throw new Error('STL 长度与面数对不上：' + buf.byteLength + ' 字节 / ' + count + ' 面');
  }
  const min = [Infinity, Infinity, Infinity];
  const max = [-Infinity, -Infinity, -Infinity];
  for (let t = 0; t < count; t += 1) {
    const base = 84 + t * 50 + 12; // 12 = 每条三角形开头 3 个 float 的法向量
    for (let v = 0; v < 3; v += 1) {
      for (let i = 0; i < 3; i += 1) {
        const p = dv.getFloat32(base + v * 12 + i * 4, true);
        if (p < min[i]) min[i] = p;
        if (p > max[i]) max[i] = p;
      }
    }
  }
  return { count, spans: [0, 1, 2].map((i) => Number((max[i] - min[i]).toFixed(3))) };
})()`;

/**
 * CDP 的 returnByValue 把 Infinity / NaN 序列化成 null，所以页面里算出的
 * 跨度回到 Node 这边可能是 null（实测坑过一次）：先在这里卡住，
 * 不让它变成后面那句「Cannot read properties of null」。
 */
function assertSpans(stl) {
  if (!stl || !Array.isArray(stl.spans) || stl.spans.some((v) => typeof v !== 'number')) {
    throw new Error(`STL 跨度解析异常：${JSON.stringify(stl)}（包围盒没被任何三角形更新？）`);
  }
  return stl;
}

/**
 * 在预览画布上撒网格点，直到某一下点击真的命中棱线（出现 .dim-tag.is-edge）。
 * 棱线在 WebGL 里恒为 1px，能不能点中完全取决于 raycaster 的屏幕容差，
 * 事先算不出坐标，只能扫 —— 实测十几步之内必中。
 */
async function findEdgeHit(page, box) {
  for (let iy = 2; iy <= 8; iy += 1) {
    for (let ix = 2; ix <= 8; ix += 1) {
      const x = Math.round(box.x + (box.width * ix) / 10);
      const y = Math.round(box.y + (box.height * iy) / 10);
      await page.send('Input.dispatchMouseEvent', {
        type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1,
      });
      await page.send('Input.dispatchMouseEvent', {
        type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: 1,
      });
      await sleep(60);
      const text = await page.evaluate(
        `(document.querySelector('.dim-tag.is-edge')?.textContent || '').trim()`,
      );
      if (text) return { x, y, text };
    }
  }
  return null;
}

/**
 * 玻璃模式的一次快照：材质属性、CSS 变量、几个面是否真的半透、滑块状态。
 *
 * alphaOf 同时吃两种序列化格式：`rgba(255, 255, 255, 0.75)` 与
 * `rgb(255 255 255 / 75%)` —— 新版 Chromium 把计算值改成后者了，
 * 只按逗号格式解析会把 alpha 读成 1，于是「有没有半透」这项永远验不出来。
 */
const GLASS_STATE_EXPR = `(() => {
  const alphaOf = (color) => {
    if (!color || color === 'none') return null;
    const nums = (color.match(/-?\\d*\\.?\\d+/g) || []).map(Number);
    if (!nums.length) return null;
    if (color.includes('/')) {
      const last = nums[nums.length - 1];
      return last > 1 ? last / 100 : last;
    }
    return nums.length >= 4 ? nums[3] : 1;
  };
  const probe = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const cs = getComputedStyle(el);
    return {
      alpha: alphaOf(cs.backgroundColor),
      blur: cs.backdropFilter || cs.webkitBackdropFilter || 'none',
    };
  };
  const root = getComputedStyle(document.documentElement);
  const range = document.querySelector('.glass-range');
  const tune = document.querySelector('.glass-tune-value');
  return {
    appearance: document.documentElement.dataset.appearance || '',
    solid: (root.getPropertyValue('--glass-solid') || '').trim(),
    fill: (root.getPropertyValue('--range-fill') || '').trim(),
    rangeValue: range ? range.value : '',
    valueText: ((tune && tune.textContent) || '').trim(),
    surfaces: {
      card: probe('.card'),
      topbar: probe('.topbar'),
      side: probe('.side'),
      panel: probe('.panel'),
      toolbar: probe('.viewer-toolbar'),
    },
    tokens: {
      spec: (root.getPropertyValue('--glass-spec') || '').trim(),
      sheen: (root.getPropertyValue('--glass-sheen') || '').trim(),
      edge: (root.getPropertyValue('--glass-edge') || '').trim(),
    },
  };
})()`;

/**
 * Node 这边解析颜色的 alpha。
 * 为什么不只依赖页面里那份：自定义属性的计算值形态不固定（可能是
 * `rgb(255 255 255 / .14)`，也可能是 `color(srgb 1 1 1 / 0.6)`、`75%`），
 * 解析失败时必须能区分「读不到」和「读到了但不达标」，所以返回 null。
 */
function parseAlpha(color) {
  if (typeof color !== 'string' || !color.trim() || color === 'none') return null;
  const nums = (color.match(/-?\d*\.?\d+/g) || []).map(Number);
  if (!nums.length) return null;
  if (color.includes('/')) {
    const last = nums[nums.length - 1];
    return last > 1 ? last / 100 : last;
  }
  if (/\btransparent\b/.test(color)) return 0;
  return nums.length >= 4 ? nums[3] : 1;
}

/** 某几个面是否符合「上玻璃 / 退玻璃」；返回问题列表（空 = 通过） */
function checkSurfaces(state, wantGlass, names) {
  const bad = [];
  for (const name of names) {
    const surface = state.surfaces[name];
    if (!surface) {
      // 这个面在当前页面上根本不存在（比如 .side 只在工作台有）：
      // 要求它是玻璃时算错，只要求它「没上玻璃」时不算错
      if (wantGlass) bad.push(`${name} 不在页面上`);
      continue;
    }
    const semi = surface.alpha !== null && surface.alpha < 0.999;
    const blurred = surface.blur.includes('blur(') && !surface.blur.includes('blur(0px)');
    if (wantGlass && !(semi && blurred)) {
      bad.push(`${name} 没成玻璃：alpha=${surface.alpha} backdrop=${surface.blur}`);
    }
    if (!wantGlass && (semi || blurred)) {
      bad.push(`${name} 没退掉玻璃：alpha=${surface.alpha} backdrop=${surface.blur}`);
    }
  }
  return bad;
}

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

    // 横向越界（纵向越界交给滚动容器，不算错）。
    // 刻意挪到视口外的元素不算越界：.skip-link（跳到主内容）用
    // position:absolute + left:-9999px 隐藏，聚焦时才滑回来 —— 那是无障碍的
    // 标准做法，不是布局错误。判据用「整个元素都在视口外」而不是 left<0：
    // 真正的溢出是"露出来一半"（部分可见），那才会真的出现横向滚动条。
    const whollyOutside = r.right < 0 || r.left > vw;
    if ((r.right > vw + 1 || r.left < -1) && !whollyOutside
        && !scrollableAncestor(el) && !ownClip(el)) {
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
  // 只删自己写的产物。以前这里是 `rmSync(SHOTS, {recursive:true})` 整目录清掉，
  // 于是把**别的脚本写到同一个目录**的东西一起删了：跑一次本脚本，
  // `verify-glass-contrast.mjs` 的 `contrast-report.json` 就消失，而 README-开发者
  // 与 docs/API-CONTRACT.md 都拿它当「玻璃数字能在报告里找到」的出处 ——
  // 文档指针当场变断链（本轮就是被自检的「随包文档指针」那条抓出来的）。
  mkdirSync(SHOTS, { recursive: true });
  for (const name of readdirSync(SHOTS)) {
    if (/^\d{2}[a-z]?-.*\.png$/.test(name) || name === 'report.json') {
      rmSync(join(SHOTS, name), { force: true });
    }
  }

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
        '工具栏五个按钮 + 空状态 + 就绪胶囊',
        info.toolbar === 5 && info.empty && info.projects >= 2,
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
    await step('需求确认卡片：可编辑数字 + 确认后开始设计', async () => {
      // 需求里给了具体尺寸时，程序先摆出「我理解成了什么」让用户确认 ——
      // 这一步必须真的走通，否则后面的结果卡片永远不会出现。
      await page.waitForFunction(`!!document.querySelector('.spec-card')`, {
        label: '需求确认卡片',
        timeoutMs: 30000,
      });
      const info = await page.evaluate(`(() => {
        const card = document.querySelector('.spec-card');
        const inputs = card.querySelectorAll('input');
        const buttons = [...card.querySelectorAll('button')].map(b => b.textContent.trim());
        return { inputs: inputs.length, buttons, head: ${textOf('.spec-card .spec-head')} };
      })()`);
      const confirm = info.buttons.find((b) => b.includes('开始设计'));
      if (!confirm) throw new Error(`确认按钮缺失（现有：${info.buttons.join('/')}）`);
      // 注意：这里的 page.click 收的是 **CSS 选择器**，不是函数字符串 ——
      // 传函数会抛 SyntaxError 且整步失败（实测踩过），所以在页面里点。
      const clicked = await page.evaluate(`(() => {
        const card = document.querySelector('.spec-card');
        const btn = [...card.querySelectorAll('button')]
          .find(b => b.textContent.includes('开始设计'));
        if (!btn) return false;
        btn.click();
        return true;
      })()`);
      if (!clicked) throw new Error('确认按钮点击失败');
      await page.waitForFunction(
        `(${textOf('.spec-card .spec-head')}).includes('已按这份规格')`,
        { label: '确认已生效', timeoutMs: 20000 },
      );
      await shot('07b-spec-confirmed');
      return `可编辑数字 ${info.inputs} 个，确认按钮「${confirm}」，确认后卡片进入已确认态`;
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
      // 结果卡必须有三个动作：下载 STL / 下载 SCAD / 打开文件夹
      const need = ['下载 STL', '下载 SCAD', '打开文件夹'];
      const missing = need.filter((n) => !info.buttons.some((b) => b.includes(n)));
      if (missing.length) throw new Error(`结果卡缺少按钮：${missing.join('、')}（实际 ${info.buttons.join('/')}）`);
      await shot('08-studio-result');
      return `${info.name}：评分 ${info.score}，${info.issues} 条建议，按钮 ${info.buttons.join('/')}`;
    });
    /* ---------------- 6b. 送到切片软件 ---------------- */
    await step('结果卡出现「送到切片软件」按钮，且说清楚送到哪个软件', async () => {
      await page.waitForFunction(`!!${SEND_BTN}`, { label: '送到切片软件按钮', timeoutMs: 15000 });
      const info = await page.evaluate(`(() => {
        const b = ${SEND_BTN};
        return { title: b.getAttribute('title') || '', disabled: b.disabled };
      })()`);
      if (info.disabled) throw new Error('按钮一出来就是禁用状态');
      if (!info.title.includes('Bambu Studio')) throw new Error(`按钮没说明目标软件：title="${info.title}"`);
      return `title「${info.title}」`;
    });
    await step('点一下真的送出：提示里带进程号（不拿“没报错”当成功）', async () => {
      await clearToasts(page);
      await page.evaluate(`(${SEND_BTN}).click()`);
      await page.waitForFunction(`!!document.querySelector('.toast-ok')`, {
        label: '送出成功提示',
        timeoutMs: 15000,
      });
      const toast = await page.evaluate(textOf('.toast-ok'));
      if (!/已启动 Bambu Studio（进程 \d+）/.test(toast)) throw new Error(`提示里没有可观测依据：${toast}`);
      await shot('08b-send-to-slicer');
      await clearToasts(page);
      return toast;
    });
    await step('切片器没检测到时：中文错误 + 下一步怎么办，且按钮不放死', async () => {
      await setSlicerScenario(page, 'none');
      await clearToasts(page);
      await page.evaluate(`(${SEND_BTN}).click()`);
      await page.waitForFunction(`!!document.querySelector('.toast-error')`, {
        label: '送出失败提示',
        timeoutMs: 15000,
      });
      const toast = await page.evaluate(textOf('.toast-error'));
      await clearToasts(page);
      const bad = [];
      if (!toast.includes('没有检测到切片软件')) bad.push(`错误文案不对：${toast}`);
      if (!toast.includes('手动指定主程序')) bad.push('没告诉用户下一步（后端 hint 没拼上）');
      // 失败后前端会强制重扫一次（假后端只是这一发返回 409，列表本身仍有 2 个）：
      // 重扫必须把按钮的目标信息恢复回来，不能错一次就把功能弄没
      const title = await page.evaluate(`(${SEND_BTN}?.getAttribute('title')) || ''`);
      if (!title.includes('Bambu Studio')) bad.push(`重扫后按钮目标信息丢了："${title}"`);
      await setSlicerScenario(page, 'ok');
      if (bad.length) throw new Error(bad.join('；'));
      return toast;
    });
    await step('切片器启动即退出（failed）：报退出码，不换绿标', async () => {
      await setSlicerScenario(page, 'failed');
      await clearToasts(page);
      await page.evaluate(`(${SEND_BTN}).click()`);
      await page.waitForFunction(`!!document.querySelector('.toast-error')`, {
        label: '退出码提示',
        timeoutMs: 15000,
      });
      const toast = await page.evaluate(textOf('.toast-error'));
      await setSlicerScenario(page, 'ok');
      await clearToasts(page);
      if (!toast.includes('退出码 1')) throw new Error(toast);
      return toast;
    });
    await step('单实例转发（forwarded）：中性提示，不写成“我启动了它”', async () => {
      await setSlicerScenario(page, 'forwarded');
      await clearToasts(page);
      await page.evaluate(`(${SEND_BTN}).click()`);
      await page.waitForFunction(`!!document.querySelector('.toast-info')`, {
        label: '转发提示',
        timeoutMs: 15000,
      });
      const toast = await page.evaluate(textOf('.toast-info'));
      await setSlicerScenario(page, 'ok');
      await clearToasts(page);
      if (!toast.includes('已经在运行')) throw new Error(toast);
      return toast;
    });

    await step('需求核对表逐条给出「要求 / 实测 / 判定」', async () => {
      // 全部达成时清单是折叠的，先展开再取证
      await page.evaluate(`(() => {
        const box = document.querySelector('.req-check');
        const head = box && box.querySelector('.acc-head');
        if (head && head.getAttribute('aria-expanded') !== 'true') head.click();
      })()`);
      await sleep(300);
      const rows = await page.evaluate(`(() => {
        const box = document.querySelector('.req-check');
        if (!box) return null;
        return {
          score: (box.querySelector('.req-head .pill')?.textContent || '').trim(),
          names: [...box.querySelectorAll('.req-name')].map((n) => n.textContent.trim()),
          nums: [...box.querySelectorAll('.req-nums')].map((n) => n.textContent.trim()),
          pills: [...box.querySelectorAll('.req-row .pill')].map((n) => n.textContent.trim()),
        };
      })()`);
      if (!rows) throw new Error('结果卡里没有需求核对表（.req-check）');
      if (!/\d+\/\d+ 达成/.test(rows.score)) throw new Error(`核对表抬头不对：${rows.score}`);
      const withNumbers = rows.nums.filter((n) => n.includes('要求') && n.includes('实测'));
      if (withNumbers.length < 3) {
        throw new Error(`核对项没有带「要求/实测」数字（只有 ${withNumbers.length} 条）：${rows.nums.join(' | ')}`);
      }
      if (!rows.pills.some((x) => x === '达成' || x === '未达成' || x === '没测出来')) {
        throw new Error(`核对行缺少判定文字：${rows.pills.join('/')}`);
      }
      await shot('08b-requirement-checklist');
      return `${rows.score} · ${rows.names.length} 条：${rows.names.slice(0, 3).join('、')}`;
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

    /* ---------------- 6b. 实时尺寸标注（未选中 = 整体外包络，选中 = 单条棱线） ---------------- */
    await step('尺寸标注：未选棱线时给出宽/深/高，与浮层读数和 STL 实测一致', async () => {
      await page.waitForFunction(`document.querySelectorAll('.dim-tag').length >= 3`, {
        label: '尺寸标签',
        timeoutMs: 20000,
      });
      const snap = await page.evaluate(DIM_TAGS_EXPR);
      const need = ['width', 'depth', 'height'];
      const LABEL = { width: '宽', depth: '深', height: '高' };
      const missing = need.filter((d) => !snap.tags.some((t) => t.dim === d));
      if (missing.length) {
        throw new Error(`缺少整体尺寸标签：${missing.join('、')}（实际 ${snap.tags.map((t) => t.dim).join('/')}）`);
      }
      if (!snap.sizeStat.label.includes('宽 × 深 × 高')) {
        throw new Error(`浮层尺寸行没说明轴序，读的人不知道哪个数是高：「${snap.sizeStat.label}」`);
      }
      // 1) 标签必须与浮层同源（同一个 bbox，不能一个对一个新得过旧）
      const parts = snap.sizeStat.value.split('×').map((s) => s.trim());
      if (parts.length !== 3) throw new Error(`浮层尺寸读数不是三段：「${snap.sizeStat.value}」`);
      for (const dim of need) {
        const tag = snap.tags.find((t) => t.dim === dim);
        const want = `${LABEL[dim]} ${parts[need.indexOf(dim)]}`;
        if (tag.text !== want) throw new Error(`标签「${tag.text}」与浮层不一致，应为「${want}」`);
      }
      // 2) 轴向语义：高 = STL 的 z 跨度、宽 = x、深 = y
      const stl = assertSpans(await page.evaluate(STL_SPANS_EXPR));
      const AXIS = { width: 0, depth: 1, height: 2 };
      for (const dim of need) {
        const tag = snap.tags.find((t) => t.dim === dim);
        const got = Number((tag.text.match(/[\d.]+/) ?? ['NaN'])[0]);
        const want = stl.spans[AXIS[dim]];
        if (Math.abs(got - want) > 0.06) {
          throw new Error(`「${tag.text}」与 STL 实测 ${mmText(want)} 不符（STL ${stl.count} 面，跨度 ${stl.spans.join('/')}）`);
        }
      }
      // 3) 标签不能被实底浮层吞掉（实测踩过：宽整个压在「模型数据」面板底下看不见）
      const buried = snap.tags.filter((t) => snap.blocks.some((b) => overlaps(t.rect, b.rect)));
      if (buried.length) throw new Error(`尺寸标签被浮层盖住：${buried.map((t) => t.text).join('、')}`);
      if (!snap.hint.includes('棱线')) throw new Error(`空选时没给出棱线点选引导：「${snap.hint}」`);
      if (snap.pressed !== 'true') throw new Error(`标注按钮 aria-pressed 应为 true，实际 ${snap.pressed}`);
      await shot('09a-studio-dimensions');
      return `${snap.tags.map((t) => t.text).join(' / ')}；浮层 ${snap.sizeStat.value}；STL 跨度 ${stl.spans.join('×')}`;
    });

    await step('尺寸标注：点中棱线只报该边长度，取消后恢复整体标注', async () => {
      const box = await page.waitForBox('.viewer-canvas canvas', { timeoutMs: 20000 });
      const hit = await findEdgeHit(page, box);
      if (!hit) throw new Error('在画布上扫了 49 个点，没有一个能命中棱线');
      const snap = await page.evaluate(DIM_TAGS_EXPR);
      const edge = snap.tags.find((t) => t.dim === 'edge');
      if (!edge) throw new Error(`命中棱线后没有 .dim-tag.is-edge：${snap.tags.map((t) => t.text).join('/')}`);
      // 选中一条边时整体标注收起：一屏只留一个数字，不跟读数抢注意力
      if (snap.tags.length !== 1) {
        throw new Error(`选中后应该只剩该边一个标签，实际 ${snap.tags.map((t) => t.text).join(' / ')}`);
      }
      const len = Number((edge.text.match(/[\d.]+/) ?? ['NaN'])[0]);
      const stl = assertSpans(await page.evaluate(STL_SPANS_EXPR));
      const diagonal = Math.hypot(...stl.spans);
      // 上界取包围盒空间对角线。不能用「≤ 最大整体尺寸」：
      // 实测斜边 87.4 就大于高 85.3，那条断言会假红。
      if (!(len > 0) || len > diagonal + 0.06) {
        throw new Error(`该边长度 ${edge.text} 不在 (0, 对角线 ${diagonal.toFixed(1)}] 内`);
      }
      if (!snap.hint.includes('已选边')) throw new Error(`选中后浮层没报当前边：「${snap.hint}」`);
      await shot('09b-studio-dim-edge');

      await page.clickText('取消选择', {
        expect: `document.querySelectorAll('.dim-tag').length >= 3 && !document.querySelector('.dim-tag.is-edge')`,
      });
      const back = await page.evaluate(DIM_TAGS_EXPR);
      if (back.tags.length < 3 || back.tags.some((t) => t.dim === 'edge')) {
        throw new Error(`取消后没恢复整体标注：${back.tags.map((t) => t.text).join(' / ')}`);
      }
      if (back.hint.includes('已选边')) throw new Error(`取消后提示还停在选中态：「${back.hint}」`);
      return `命中 @${hit.x},${hit.y} →「${edge.text}」（对角线上限 ${diagonal.toFixed(1)}），取消后恢复 ${back.tags.length} 条`;
    });

    await step('尺寸标注开关：关掉清空标签与提示，再开恢复', async () => {
      await page.click('button[aria-label="尺寸标注切换"]');
      await page.waitForFunction(
        `document.querySelectorAll('.dim-tag').length === 0 && !document.querySelector('.viewer-dim-hint')`,
        { label: '标注已关闭', timeoutMs: 10000 },
      );
      const off = await page.evaluate(
        `document.querySelector('button[aria-label="尺寸标注切换"]').getAttribute('aria-pressed')`,
      );
      await shot('09c-studio-dims-off');
      await page.click('button[aria-label="尺寸标注切换"]');
      await page.waitForFunction(`document.querySelectorAll('.dim-tag').length >= 3`, {
        label: '标注已恢复',
        timeoutMs: 10000,
      });
      if (off !== 'false') throw new Error(`关闭后 aria-pressed 应为 false，实际 ${off}`);
      return '关闭后标签与提示全部消失，重新打开后三条整体标注回来';
    });

    /* ---------------- 7. 参数分栏（归类规则见 src/lib/paramGroups.ts） ---------------- */
    await step('参数按类别分栏且归类正确', async () => {
      await page.waitForFunction(
        `document.querySelectorAll('.params-group').length >= 3`,
        { label: '参数分栏' },
      );
      const groups = await page.evaluate(`(() => {
        return [...document.querySelectorAll('.params-group')].map((g) => ({
          title: (g.querySelector('.params-group-title')?.firstChild?.textContent || '').trim(),
          count: Number(g.querySelector('.params-group-count')?.textContent || '-1'),
          chips: [...g.querySelectorAll('.chip-label')].map((c) => (c.textContent || '').trim()),
        }));
      })()`);
      /* 这几条归属钉的是规则的优先级：孔/壁厚/圆角/数量都必须先于「整体尺寸」判定，
         否则 穿线孔直径（名字里带“直径”）、防滑柱数量（grip_post_count 带“post”）
         会被外形尺寸抢走。有人改 tokens 顺序或删词，这里会先红。 */
      const EXPECT = {
        高度: '整体尺寸',
        前挡边高度: '整体尺寸',
        倾角: '整体尺寸',
        板厚: '壁厚与强度',
        穿线孔直径: '孔与螺纹',
        圆角半径: '圆角与配合',
        防滑柱数量: '数量与阵列',
      };
      const belongTo = (label) => groups.find((g) => g.chips.includes(label))?.title;
      for (const [label, title] of Object.entries(EXPECT)) {
        const actual = belongTo(label);
        if (actual !== title) {
          throw new Error(`「${label}」应在「${title}」，实际在「${actual ?? '不在任何分组'}」`);
        }
      }
      const total = await page.evaluate(`document.querySelectorAll('.chip').length`);
      const placed = groups.reduce((n, g) => n + g.chips.length, 0);
      // 分栏最容易出的错是「参数被吃掉了」：总数必须与平铺时一致
      if (placed !== total) throw new Error(`分栏里只有 ${placed} 个参数，实际有 ${total} 个`);
      const empty = groups.filter((g) => !g.chips.length).map((g) => g.title);
      if (empty.length) throw new Error(`出现空分组：${empty.join('、')}`);
      for (const g of groups) {
        if (g.count !== g.chips.length) {
          throw new Error(`「${g.title}」标题计数 ${g.count} 与实际 ${g.chips.length} 不符`);
        }
      }
      await shot('10a-studio-param-groups');
      // 分栏后每栏可用宽度更小（侧栏在 1024 以下会收成 72px），
      // 在这个最窄的常规布局上复检一次横向溢出与文字裁切。
      await page.setViewport(1024, 700);
      await sleep(600);
      await audit(page, '参数分栏 1024×700');
      await shot('10b-studio-param-groups-narrow');
      await page.setViewport(1280, 800);
      await sleep(600);
      return groups.map((g) => `${g.title}(${g.count})`).join(' ');
    });

    /* ---------------- 7b. 参数微调（不调用 AI） ---------------- */
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
      // 改了哪个类，那个类的标题就要亮起来（参数多时靠它找回改动位置）
      const flagged = await page.evaluate(`(() => {
        const group = [...document.querySelectorAll('.params-group')].find((g) =>
          [...g.querySelectorAll('.chip-label')].some((c) => (c.textContent || '').trim() === '高度'));
        if (!group) return null;
        return {
          title: (group.querySelector('.params-group-title')?.firstChild?.textContent || '').trim(),
          changed: group.classList.contains('is-changed'),
          flag: !!group.querySelector('.params-group-flag'),
          others: [...document.querySelectorAll('.params-group')].filter((g) => g !== group)
            .every((g) => !g.classList.contains('is-changed')),
        };
      })()`);
      if (!flagged?.changed || !flagged?.flag) {
        throw new Error(`改动所在的分组没有标记：${JSON.stringify(flagged)}`);
      }
      if (!flagged.others) throw new Error('没改动的分组也被标成了已改动');
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
      return `参数 ${before.join('/')}；应用后尺寸「${bbox}」；改动标记在「${flagged.title}」`;
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

    // 上面那一步只验了**按钮状态**。本轮修的真缺陷是：按钮状态对，但场景里
    // 的模型没跟着变 —— 线框那个 effect 跑在异步加载完成之前，`core.model`
    // 还是空的，等 STL 解析完没人再按当前状态设置一次可见性，新模型一律实心。
    // 所以这里必须去查**场景里对象的真实可见性**，否则那个 bug 照样全绿。
    await step('线框真的作用在模型上（不是只有按钮变色）', async () => {
      const readScene = () =>
        page.evaluate(`
          (() => {
            const cv = document.querySelector('canvas');
            if (!cv) return { error: 'no-canvas' };
            // 从 canvas 反查不到场景对象时，用 __aiforgeViewer 调试钩子
            const hook = window.__aiforgeViewer;
            if (!hook) return { error: 'no-hook' };
            const core = hook.coreRef?.current;
            if (!core) return { error: 'no-core' };
            let mesh = null, edges = null;
            core.model.traverse((o) => {
              if (o.name === 'stl') mesh = o;
              if (o.name === 'edges') edges = o;
            });
            return { mesh: !!mesh, meshVisible: mesh?.visible ?? null, edgesVisible: edges?.visible ?? null };
          })()
        `);

      // 1) 关线框：实体可见、描边隐藏
      const off = await readScene();
      if (off.error) throw new Error(`读不到 3D 场景：${off.error}`);
      if (!off.mesh) throw new Error('场景里没有模型网格');
      if (off.meshVisible !== true) throw new Error(`线框关着时模型却不可见（visible=${off.meshVisible}）`);
      if (off.edgesVisible !== false) throw new Error(`线框关着时描边仍显示（visible=${off.edgesVisible}）—— 实体上叠了一层棱线`);

      // 2) 开线框：实体隐藏、描边可见
      await page.click('button[aria-label="线框切换"]');
      await sleep(400);
      const on = await readScene();
      if (on.meshVisible !== false) throw new Error(`线框开着时模型仍是实心（visible=${on.meshVisible}）`);
      if (on.edgesVisible !== true) throw new Error(`线框开着时描边没显示（visible=${on.edgesVisible}）`);
      await shot('11b-studio-wireframe-applied');

      // 3) 关键：**重新触发一次加载后线框仍然生效**。这正是原缺陷的触发路径 ——
      //    切项目/换模型会重建模型，旧实现只用默认值渲染，于是线框"失灵"。
      //    这里不去点侧栏（那会牵动路由与后端），而是直接调钩子让 Viewer3D
      //    走一遍完整的 dispose → fetch → 建对象 流程。
      //    注意：此刻线框是**开着的**（第 2 步点开了还没关），不要再点一次按钮 ——
      //    点一下就变成关着了，后面断言"重建后仍开着"必然失败（那是测试自己的错）。
      const reloadState = await page.evaluate(`
        (() => {
          const hook = window.__aiforgeViewer;
          if (!hook) return { error: 'no-hook' };
          hook.bumpRevision();
          return { ok: true };
        })()
      `);
      if (reloadState.error) throw new Error(`读不到调试钩子：${reloadState.error}`);
      // 等 STL 重新拉完并建好对象
      await sleep(2500);
      const afterReload = await readScene();
      if (!afterReload.mesh) throw new Error('重新加载后场景里没有模型');
      if (afterReload.meshVisible !== false || afterReload.edgesVisible !== true) {
        throw new Error(
          `模型重建后线框状态丢失（mesh=${afterReload.meshVisible} edges=${afterReload.edgesVisible}）` +
            ' —— 这就是「开着线框切换模型变实心」那个缺陷',
        );
      }
      await page.click('button[aria-label="线框切换"]');
      await sleep(300);
      const restored = await readScene();
      if (restored.meshVisible !== true || restored.edgesVisible !== false) {
        throw new Error(
          `关掉线框后没有恢复（mesh=${restored.meshVisible} edges=${restored.edgesVisible}）`,
        );
      }
      return '实体/描边可见性随开关切换，且模型重建后仍然保持（已覆盖原缺陷触发路径）';
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
    await step('重开历史项目：数据来自 metrics 接口的实测值（不是全 0）', async () => {
      // 这条路径以前根本没被验收覆盖：打开历史项目时前端走
      // GET /api/projects/{id}/metrics 合成结果卡，接口缺失时界面上就是
      // 一整屏「0.0 × 0.0 × 0.0 / 体积 0.00 cm³ / 0 B」，看着像模型坏了。
      await page.waitForFunction(
        `document.querySelectorAll('.viewer-overlay-stats .stat').length >= 3`,
        { label: '历史项目模型数据浮层', timeoutMs: 20000 },
      );
      await page.waitForFunction(
        `!document.querySelector('.viewer-loading') && document.querySelectorAll('.result .stats-cell').length >= 4`,
        { label: '历史项目结果卡', timeoutMs: 20000 },
      );
      // 接口请求必须在数据到位之后查：刚点完卡片就去翻 performance 会抢跑（实测红过一次）
      await page.waitForFunction(
        `[...performance.getEntriesByType('resource')].some((e) => e.name.endsWith('/metrics'))`,
        { label: '/metrics 请求', timeoutMs: 10000 },
      );
      await sleep(600);
      const stats = await page.evaluate(`[...document.querySelectorAll('.viewer-overlay-stats .stat')].map((s) => ({
          label: (s.querySelector('.stat-label')?.textContent || '').replace(/\\s+/g, ' ').trim(),
          value: (s.querySelector('.stat-value')?.textContent || '').replace(/\\s+/g, ' ').trim(),
        }))`);
      const numbers = (text) => (text.match(/\d+(?:\.\d+)?/g) || []).map(Number);
      const bad = [];
      for (const key of ['尺寸', '三角面数', '体积', '预计耗材']) {
        const stat = stats.find((s) => s.label.includes(key));
        if (!stat) bad.push(`浮层缺「${key}」行`);
        else if (!numbers(stat.value).some((n) => n > 0)) bad.push(`「${stat.label}」读到 ${stat.value}`);
      }
      const cells = await page.evaluate(`[...document.querySelectorAll('.result .stats-cell')].map((c) => (c.textContent || '').replace(/\\s+/g, ' ').trim())`);
      const dead = cells.filter((c) => !numbers(c).some((n) => n > 0));
      if (dead.length) bad.push(`结果卡有全 0 数据格：${dead.join(' | ')}`);
      if (bad.length) throw new Error(bad.join('；'));
      await shot('11a-studio-reopen-metrics');
      return stats.map((s) => `${s.label.split('（')[0]} ${s.value}`).join(' ｜ ');
    });
    await step('搜索项目过滤生效', async () => {
      await page.setInputValue('.side-search', '花盆');
      await sleep(400);
      const count = await page.evaluate(`document.querySelectorAll('.proj').length`);
      await page.setInputValue('.side-search', '');
      await sleep(300);
      return `过滤后剩 ${count} 个`;
    });
    await step('新建项目进入空状态（无模型时标注层也必须空）', async () => {
      await page.clickText('新建项目');
      await page.waitForFunction(`!!document.querySelector('.viewer-empty')`, { label: '空状态', timeoutMs: 15000 });
      await sleep(400);
      // 标注开关默认是开着的。没有模型时「点击模型上的棱线」这类引导必须不出现，
      // 否则就是对着空画布编造了一个可交互假象（实测踩过）。
      const empty = await page.evaluate(DIM_TAGS_EXPR);
      if (empty.hint) throw new Error(`空状态下还显示标注提示：「${empty.hint}」`);
      if (empty.tags.length) throw new Error(`空状态下还有尺寸标签：${empty.tags.map((t) => t.text).join('/')}`);
      return '已新建并显示空状态，标注层为空';
    });

    /* ---------------- 10. 设置页 ---------------- */
    await step('设置页分区齐全（外观独立成卡，主题/材质都在里面）', async () => {
      await page.goto(`${BASE}/#/settings`);
      await page.waitForFunction(`document.querySelectorAll('.card').length >= 6`, { label: '设置卡片' });
      const info = await page.evaluate(`(() => {
        const cardOf = (title) => {
          const h2 = [...document.querySelectorAll('.card-head h2')].find((el) => el.textContent.trim() === title);
          return (h2 && h2.closest('.card')) || null;
        };
        const appearance = cardOf('外观');
        const behavior = cardOf('行为');
        return {
          heads: [...document.querySelectorAll('.card-head h2')].map(h => h.textContent.trim()),
          switches: document.querySelectorAll('.switch').length,
          paths: [...document.querySelectorAll('.path-box code')].map(c => c.textContent.trim()),
          masked: !!document.querySelector('code.mono'),
          // README / 用户手册都写「设置 → 外观」：界面上必须真有这么一张卡，
          // 而且主题与界面材质必须在它里面，不是寄在「行为」卡里。
          hasTheme: !!(appearance && appearance.querySelector('[aria-label="外观主题"]')),
          hasMaterial: !!(appearance && appearance.querySelector('[aria-label="界面材质"]')),
          themeLeakedToBehavior: !!(behavior && behavior.querySelector('[aria-label="外观主题"]')),
        };
      })()`);
      const missing = ['打印机', 'AI 服务', '3D 引擎', '切片软件', '文件位置', '行为', '外观'].filter(
        (name) => !info.heads.includes(name),
      );
      const bad = [];
      if (missing.length) bad.push(`缺少分区：${missing.join('、')}（实际 ${info.heads.join(' / ')}）`);
      if (!info.hasTheme) bad.push('「外观」卡里没有外观主题');
      if (!info.hasMaterial) bad.push('「外观」卡里没有界面材质');
      if (info.themeLeakedToBehavior) bad.push('外观主题还寄在「行为」卡里');
      if (bad.length) throw new Error(bad.join('；'));
      await shot('12-settings');
      await audit(page, '设置页 1280×800');
      return `分区 ${info.heads.join(' / ')}；开关 ${info.switches} 个`;
    });
    /* ---------------- 10a. 切片软件（设置页） ---------------- */
    await step('切片软件卡：列出检测到的软件、版本与从哪里找到的', async () => {
      const info = await page.evaluate(`(() => {
        const card = ${SLICER_CARD};
        if (!card) return null;
        const select = card.querySelector('select');
        return {
          options: [...(select ? select.options : [])].map((o) => o.textContent.trim()),
          value: select ? select.value : '',
          hint: (card.querySelector('.field-hint') ? card.querySelector('.field-hint').textContent : '').trim(),
          buttons: [...card.querySelectorAll('button')].map((b) => b.textContent.trim()),
        };
      })()`);
      if (!info) throw new Error('设置页没有「切片软件」卡');
      const bad = [];
      if (info.options.length < 2) bad.push(`下拉只有 ${info.options.length} 项`);
      if (!info.options.some((o) => o.includes('Bambu Studio') && o.includes('02.08.02.61'))) {
        bad.push(`选项里没带版本号：${info.options.join(' / ')}`);
      }
      if (!info.hint.includes('从已安装程序找到')) bad.push(`没说明从哪里找到：${info.hint}`);
      if (!info.buttons.includes('重新检测')) bad.push('缺「重新检测」');
      if (!info.buttons.some((b) => b.includes('手动指定主程序'))) bad.push('缺「手动指定主程序」');
      if (bad.length) throw new Error(bad.join('；'));
      await shot('12b-settings-slicers');
      return `${info.options.join(' / ')}｜${info.hint.slice(0, 46)}`;
    });
    await step('换默认目标：写进后端并回读一致（只存 id）', async () => {
      await clearToasts(page);
      await selectOne(page, '#slicer-default', 'orcaslicer');
      await page.waitForFunction(`!!document.querySelector('.toast-ok')`, {
        label: '切换提示',
        timeoutMs: 15000,
      });
      const toast = await page.evaluate(textOf('.toast-ok'));
      await clearToasts(page);
      const saved = await page.evaluate(`(async () => {
        const s = await (await fetch('/api/settings')).json();
        return s.default_slicer;
      })()`);
      const value = await page.evaluate(`(() => {
        const card = ${SLICER_CARD};
        return (card && card.querySelector('select') ? card.querySelector('select').value : '') || '';
      })()`);
      const bad = [];
      if (!toast.includes('OrcaSlicer')) bad.push(`提示没说清以后送到谁：${toast}`);
      if (saved !== 'orcaslicer') bad.push(`后端存的是 ${JSON.stringify(saved)}`);
      if (value !== 'orcaslicer') bad.push(`界面又跳回了 ${value}`);
      if (bad.length) throw new Error(bad.join('；'));
      return `${toast}｜后端 default_slicer=${saved}`;
    });
    await step('重新检测：真的带 rescan=1 重扫一次', async () => {
      await page.evaluate(`(() => {
        window.__slicerGets = [];
        const orig = window.fetch;
        window.fetch = function () {
          window.__slicerGets.push(String(arguments[0]));
          return orig.apply(this, arguments);
        };
        return true;
      })()`);
      await clearToasts(page);
      await page.clickText('重新检测', { expect: `!!document.querySelector('.toast-ok')` });
      await page.waitForFunction(`!!document.querySelector('.toast-ok')`, {
        label: '重检结果',
        timeoutMs: 20000,
      });
      const toast = await page.evaluate(textOf('.toast-ok'));
      const calls = await page.evaluate(`window.__slicerGets || []`);
      await clearToasts(page);
      if (!calls.some((u) => u.includes('/api/slicers?rescan=1'))) {
        throw new Error(`没发强制重扫：${calls.join(' / ') || '一个 /api/slicers 请求都没发'}`);
      }
      if (!toast.includes('检测到 2 个切片软件')) throw new Error(toast);
      return `${toast}｜/api/slicers 请求 ${calls.filter((u) => u.includes('/api/slicers')).length} 次`;
    });
    await step('自动检测漏网时：手动指定主程序能把它加进来', async () => {
      await clearToasts(page);
      await page.clickText('手动指定主程序', { expect: `!!document.querySelector('.toast-ok')` });
      await page.waitForFunction(`!!document.querySelector('.toast-ok')`, {
        label: '手动指定结果',
        timeoutMs: 20000,
      });
      const toast = await page.evaluate(textOf('.toast-ok'));
      await clearToasts(page);
      const options = await page.evaluate(`(() => {
        const card = ${SLICER_CARD};
        return card ? [...card.querySelectorAll('option')].map((o) => o.textContent.trim()) : [];
      })()`);
      const custom = await page.evaluate(`(async () => {
        const s = await (await fetch('/api/settings')).json();
        return s.slicer_custom_paths;
      })()`);
      const bad = [];
      if (!options.some((o) => o.includes('PrusaSlicer'))) {
        bad.push(`列表里没出现手动指定的软件：${options.join(' / ')}`);
      }
      if (!Array.isArray(custom) || custom.length !== 1) bad.push(`主程序路径没存住：${JSON.stringify(custom)}`);
      if (!toast.includes('3 个')) bad.push(`提示里的数字不对：${toast}`);
      // 收尾换回 Bambu Studio：后面的用例与真机验收都以它为准
      await selectOne(page, '#slicer-default', 'bambu_studio');
      await page.waitForFunction(`!!document.querySelector('.toast-ok')`, { label: '恢复默认', timeoutMs: 15000 });
      await clearToasts(page);
      if (bad.length) throw new Error(bad.join('；'));
      return `${toast}｜${options.length} 个选项`;
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

    /* ---------------- 12b. 页面切换动画 ---------------- */
    await step('页面切换有进入动画，且尊重 prefers-reduced-motion', async () => {
      // 光看 computed style 只能证明 CSS 接上了，证不了动画真的跑过：
      // 在 document 上装一个 animationstart 采集器（事件会冒泡），
      // 导航后看 page-in 有没有出现过。
      await page.goto(`${BASE}/#/studio`);
      await page.waitForFunction(`!!document.querySelector('.studio')`, { label: '工作台' });
      await page.evaluate(`(() => {
        window.__anims = [];
        document.addEventListener(
          'animationstart',
          (e) => window.__anims.push({
            name: e.animationName,
            cls: String((e.target && e.target.className) || '').split(' ')[0],
          }),
          true,
        );
      })()`);
      const seenFor = async (hash, waitSel) => {
        await page.goto(`${BASE}/${hash}`);
        await page.waitForFunction(`!!document.querySelector('${waitSel}')`, { label: `页面 ${hash}` });
        await sleep(120);
        const anim = await page.evaluate(`(() => {
          const el = document.querySelector('.app-main > div > *');
          if (!el) return null;
          const cs = getComputedStyle(el);
          return {
            cls: String(el.className || '').split(' ')[0],
            name: cs.animationName,
            durMs: Math.round(parseFloat(cs.animationDuration) * 1000),
          };
        })()`);
        if (!anim) throw new Error(`${hash} 里 .app-main > div > * 找不到节点，动画选择器失效`);
        if (anim.name !== 'page-in') throw new Error(`${hash} 的动画名是「${anim.name}」，应为 page-in`);
        // tokens 里 --dur 160ms × 1.5 = 240ms；设计规范禁止 >400ms，也不能小到根本看不出来
        if (anim.durMs < 120 || anim.durMs > 400) {
          throw new Error(`${hash} 动画时长 ${anim.durMs}ms 不在 120–400ms 区间`);
        }
        const fired = await page.evaluate(
          `(window.__anims || []).filter((a) => a.name === 'page-in').length`,
        );
        if (!fired) throw new Error(`导航到 ${hash} 没触发 page-in（animationstart 没抓到）`);
        return `${anim.cls} ${anim.durMs}ms`;
      };
      const studioToSettings = await seenFor('#/settings', '.card');
      const toAbout = await seenFor('#/about', '.license');
      const toStudio = await seenFor('#/studio', '.studio');

      await page.send('Emulation.setEmulatedMedia', {
        features: [{ name: 'prefers-reduced-motion', value: 'reduce' }],
      });
      await page.goto(`${BASE}/#/settings`);
      await page.waitForFunction(`!!document.querySelector('.card')`, { label: '减少动效下的设置页' });
      const reduced = await page.evaluate(`(() => {
        const el = document.querySelector('.app-main > div > *');
        return el ? Math.round(parseFloat(getComputedStyle(el).animationDuration) * 1000) : -1;
      })()`);
      await page.send('Emulation.setEmulatedMedia', {
        features: [{ name: 'prefers-reduced-motion', value: 'no-preference' }],
      });
      if (reduced > 20) throw new Error(`prefers-reduced-motion 下动画还有 ${reduced}ms`);
      return `工作台→${studioToSettings}、→${toAbout}、→${toStudio}；减少动效时 ${reduced}ms`;
    });

    /* ---------------- 12c. 液态玻璃外观 ---------------- */
    await step('开启液态玻璃：卡片/顶栏/侧栏真的半透带模糊，滑块出现', async () => {
      await page.goto(`${BASE}/#/settings`);
      await page.waitForFunction(
        `!!document.querySelector('.card') && !document.querySelector('.glass-range')`,
        { label: '默认普通材质' },
      );
      const before = await page.evaluate(GLASS_STATE_EXPR);
      if (before.appearance !== 'solid') {
        throw new Error(`没选材质时 data-appearance 应该是 solid，实际 ${before.appearance || '空'}`);
      }
      const stillSolid = checkSurfaces(before, false, ['card', 'topbar']);
      if (stillSolid.length) throw new Error(`普通材质下就已经半透：${stillSolid.join('；')}`);

      await page.clickText('液态玻璃', {
        expect: `document.documentElement.dataset.appearance === 'glass'`,
      });
      await sleep(300);
      const after = await page.evaluate(GLASS_STATE_EXPR);
      const bad = [];
      if (after.appearance !== 'glass') bad.push(`data-appearance=${after.appearance || '空'}`);
      // 默认透明度 25% => 留给 color-mix 的实底是 75%
      if (after.solid !== '75%') bad.push(`--glass-solid=${after.solid || '空'}（应为 75%）`);
      if (!after.rangeValue) bad.push('选了液态玻璃但没弹出透明度滑块');
      bad.push(...checkSurfaces(after, true, ['card', 'topbar']));
      if (bad.length) throw new Error(bad.join('；'));

      // 跟随指针的高光：JS 只写两个变量，没跑起来就是静态渐变，看不出「液态」。
      // 注意：点「液态玻璃」会把那张卡片滚到视口中间，其它卡片会跑到屏幕外 ——
      // 拿屏幕外的矩形去发 mouseMoved，命中的是视口外的空气（elementFromPoint 直接 null），
      // 高光自然写不出来。所以这里就用「刚变出来的那张」（外观卡），先主动滚进视口，
      // 再取「卡片 ∩ 视口」里的一个点 —— 卡片比视口还高时也能测（上轮那个「找一张
      // 完整落在视口里的卡片」的写法，卡片一分多就必然误报「没有完整可见的卡片」）。
      const picked = await page.evaluate(`(() => {
        const anchor = document.querySelector('.glass-tune') || document.querySelector('.card');
        const card = (anchor && anchor.closest && anchor.closest('.card')) || anchor;
        if (!card) return { error: '页面上没有卡片' };
        card.scrollIntoView({ block: 'center', behavior: 'instant' });
        card.setAttribute('data-sheen-probe', '1');
        return {
          title: (card.querySelector('.card-head h2') || {}).textContent || '（无标题）',
        };
      })()`);
      if (picked.error) throw new Error(picked.error);
      await sleep(200); // 滚动完再量矩形
      const probe = await page.evaluate(`(() => {
        const card = document.querySelector('[data-sheen-probe]');
        const r = card.getBoundingClientRect();
        const top = Math.max(r.top, 64);
        const left = Math.max(r.left, 4);
        const bottom = Math.min(r.bottom, innerHeight - 4);
        const right = Math.min(r.right, innerWidth - 4);
        if (right - left < 80 || bottom - top < 48) {
          return { error: '卡片几乎不在视口里：' + JSON.stringify({ top: top, bottom: bottom, left: left, right: right }) };
        }
        const x = Math.round(left + 46);
        const y = Math.round(top + 24);
        return { x, y, dx: Math.round(x - r.left), dy: Math.round(y - r.top) };
      })()`);
      if (probe.error) throw new Error(probe.error);
      const hit = await page.evaluate(
        `(() => { const el = document.elementFromPoint(${probe.x}, ${probe.y});
          const card = document.querySelector('[data-sheen-probe]');
          return el && card && card.contains(el) ? 'ok' : (el ? el.tagName + '.' + el.className : 'null'); })()`,
      );
      if (hit !== 'ok') throw new Error(`目标点 ${probe.x},${probe.y} 没命中卡片「${picked.title}」，实际 ${hit}`);
      await page.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: probe.x, y: probe.y, button: 'none' });
      await sleep(200);
      const sheen = await page.evaluate(
        `(() => { const el = document.querySelector('[data-sheen-probe]');
          return el ? [el.style.getPropertyValue('--sheen-x'), el.style.getPropertyValue('--sheen-y')] : null; })()`,
      );
      if (!sheen || !sheen[0] || !sheen[1]) {
        throw new Error(`指针经过卡片「${picked.title}」却没写出 --sheen-x/y（实际 ${JSON.stringify(sheen)}）`);
      }
      // 断言的是「卡片内坐标」而不是 clientX：漏减 rect.left 就会得到 240px 这种值
      const dx = Math.abs(parseFloat(sheen[0]) - probe.dx);
      const dy = Math.abs(parseFloat(sheen[1]) - probe.dy);
      if (dx > 2 || dy > 2) {
        throw new Error(`高光坐标不是卡片内坐标：期望 ${probe.dx}/${probe.dy}，实际 ${sheen[0]}/${sheen[1]}`);
      }
      await shot('18-settings-glass');
      await audit(page, '设置页液态玻璃 1280×800');
      return `alpha ${after.surfaces.card.alpha} ／ ${after.surfaces.card.blur} ／ 高光 ${sheen[0]}·${sheen[1]}（${picked.title}）`;
    });

    await step('拖透明度滑块：实时联动 --glass-solid，松手才发一次 PUT', async () => {
      // 统计 PUT 次数：直接注 fetch，避开「拖动过程中每一下 input 都存盘」这种实现
      await page.evaluate(`(() => {
        window.__puts = [];
        const orig = window.fetch;
        window.fetch = function () {
          const input = arguments[0];
          const init = arguments[1];
          if (String(input).indexOf('/api/settings') >= 0 && init && init.method === 'PUT') {
            window.__puts.push(String(init.body));
          }
          return orig.apply(this, arguments);
        };
        return true;
      })()`);
      // 不用 page.focus()：它会先点一下，而点滑块会把值改成点击位置（测不准）
      await page.evaluate(`document.querySelector('.glass-range').focus()`);
      await page.setInputValue('.glass-range', '40');
      await sleep(250);
      const mid = await page.evaluate(GLASS_STATE_EXPR);
      const putsOnDrag = await page.evaluate(`(window.__puts || []).length`);
      const bad = [];
      if (mid.solid !== '60%') bad.push(`拖到 40% 后 --glass-solid=${mid.solid || '空'}（应为 60%）`);
      if (mid.valueText !== '40%') bad.push(`滑块旁的读数=${mid.valueText || '空'}`);
      // 轨道填充比例：40 / 上限 60 = 67%（不能按 100 算，否则永远填不满）
      if (mid.fill !== '67') bad.push(`--range-fill=${mid.fill || '空'}（40/60 应为 67）`);
      if (mid.surfaces.card.alpha > 0.65)
        bad.push(`拖到 40% 后卡片还这么实：alpha=${mid.surfaces.card.alpha}`);
      if (putsOnDrag !== 0) bad.push(`仅改本地就发了 ${putsOnDrag} 次 PUT`);

      await page.evaluate(`document.querySelector('.glass-range').blur()`);
      await sleep(500);
      const puts = await page.evaluate(`(window.__puts || [])`);
      if (puts.length !== 1) bad.push(`失焦后应该只存一次，实际 ${puts.length} 次`);
      else {
        const body = JSON.parse(puts[0]);
        if (body.glass_transparency !== 40) bad.push(`PUT 内容=${puts[0]}`);
      }
      if (bad.length) throw new Error(bad.join('；'));
      return '40% → --glass-solid 60%、range-fill 67，PUT 1 次';
    });

    await step('刷新后材质与透明度从后端恢复（不是只活在内存里）', async () => {
      await page.reload();
      await page.waitForFunction(`!!document.querySelector('.glass-range')`, { label: '滑块回来' });
      await sleep(400);
      const state = await page.evaluate(GLASS_STATE_EXPR);
      const bad = [];
      if (state.appearance !== 'glass') bad.push(`data-appearance=${state.appearance || '空'}`);
      if (state.solid !== '60%') bad.push(`--glass-solid=${state.solid || '空'}（后端存的是 40%）`);
      if (state.rangeValue !== '40') bad.push(`滑块位置=${state.rangeValue || '空'}`);
      bad.push(...checkSurfaces(state, true, ['card']));
      if (bad.length) throw new Error(bad.join('；'));
      return `重启后仍是玻璃，透明度 ${state.rangeValue}%`;
    });

    await step('非法值由后端夹住：超上限/负数/非数字/假材质', async () => {
      const res = await page.evaluate(`(async () => {
        const put = async (body) => {
          const r = await fetch('/api/settings', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
          const json = await r.json();
          return { status: r.status, appearance: json.appearance, t: json.glass_transparency };
        };
        return {
          over: await put({ glass_transparency: 999 }),
          negative: await put({ glass_transparency: -5 }),
          junk: await put({ glass_transparency: 'abc' }),
          text: await put({ glass_transparency: '35' }),
          fakeMaterial: await put({ appearance: 'frosted' }),
        };
      })()`);
      const bad = [];
      if (res.over.t !== 60) bad.push(`999 没被夹到上限，回到 ${res.over.t}`);
      if (res.negative.t !== 0) bad.push(`-5 没被夹到下限，回到 ${res.negative.t}`);
      if (res.junk.t !== 25) bad.push(`非数字没回默认 25，回到 ${res.junk.t}`);
      if (res.text.t !== 35) bad.push(`字符串「35」没被接受，回到 ${res.text.t}`);
      if (res.fakeMaterial.appearance !== 'glass') {
        bad.push(`假材质「frosted」被写进去了：${res.fakeMaterial.appearance}`);
      }
      if (bad.length) throw new Error(bad.join('；'));
      return '999→60／-5→0／abc→25／“35”→35／frosted 被忽略';
    });

    await step('系统「降低透明度」下退成实底（内联变量不能顶掉这条保险）', async () => {
      await page.evaluate(`document.documentElement.style.setProperty('--glass-solid', '60%')`);
      await sleep(120);
      const on = await page.evaluate(GLASS_STATE_EXPR);
      if (!(on.surfaces.card.alpha < 0.999)) {
        throw new Error(`前置条件不成立：此时卡片本来就不半透（alpha=${on.surfaces.card.alpha}）`);
      }
      const restore = async () =>
        page.send('Emulation.setEmulatedMedia', {
          features: [{ name: 'prefers-reduced-transparency', value: 'no-preference' }],
        });
      await page.send('Emulation.setEmulatedMedia', {
        features: [{ name: 'prefers-reduced-transparency', value: 'reduce' }],
      });
      // matchMedia change 回调里会重跑 applyAppearance：内联值必须被删掉，
      // 否则内联 style 优先级在 :root 规则之上，那条保险等于没写（真踩过的洞）。
      await sleep(400);
      const off = await page.evaluate(GLASS_STATE_EXPR);
      const bad = [];
      if (!(off.surfaces.card.alpha >= 0.999)) {
        bad.push(`降透明后卡片还半透：alpha=${off.surfaces.card.alpha}`);
      }
      if (!off.surfaces.card.blur.includes('blur(0px)')) {
        bad.push(`降透明后还在烧模糊：${off.surfaces.card.blur}`);
      }
      await restore();
      await sleep(400);
      const back = await page.evaluate(GLASS_STATE_EXPR);
      if (!(back.surfaces.card.alpha < 0.999)) {
        bad.push(`取消降透明后没回到半透：alpha=${back.surfaces.card.alpha}`);
      }
      if (bad.length) throw new Error(bad.join('；'));
      return `reduce 时 alpha ${off.surfaces.card.alpha}，取消后 ${back.surfaces.card.alpha}`;
    });

    await step('恢复默认按钮把透明度拉回 25%', async () => {
      await page.clickText('恢复默认', {
        expect: `document.querySelector('.glass-range').value === '25'`,
      });
      await sleep(400);
      const state = await page.evaluate(GLASS_STATE_EXPR);
      if (state.solid !== '75%') throw new Error(`--glass-solid=${state.solid || '空'}，应为 75%`);
      if (state.valueText !== '25%') throw new Error(`读数=${state.valueText || '空'}`);
      const saved = await page.evaluate(
        `fetch('/api/settings').then((r) => r.json()).then((s) => s.glass_transparency)`,
      );
      if (saved !== 25) throw new Error(`后端存的是 ${saved}`);
      return '回到 25% 并写入后端';
    });

    await step('深色主题下玻璃高光跟着压暗（不会在黑底上变刺眼白边）', async () => {
      const light = await page.evaluate(GLASS_STATE_EXPR);
      await page.clickText('深色', {
        expect: `document.documentElement.getAttribute('data-theme') === 'dark'`,
      });
      await sleep(400);
      const dark = await page.evaluate(GLASS_STATE_EXPR);
      const lightSpec = parseAlpha(light.tokens.spec);
      const darkSpec = parseAlpha(dark.tokens.spec);
      const bad = [];
      if (lightSpec === null || darkSpec === null) {
        bad.push(`--glass-spec 读不到：浅色「${light.tokens.spec}」／深色「${dark.tokens.spec}」`);
      } else {
        if (!(darkSpec < lightSpec)) {
          bad.push(`深色高光没压暗：${darkSpec}（浅色 ${lightSpec}）`);
        }
        if (darkSpec > 0.3) bad.push(`深色高光 alpha=${darkSpec}，在黑底上会刺眼`);
      }
      if (!dark.tokens.edge) bad.push('--glass-edge 丢了');
      if (dark.tokens.spec === light.tokens.spec) bad.push('切深色后 --glass-spec 没变');
      if (bad.length) throw new Error(bad.join('；'));
      await shot('19-settings-glass-dark');
      await audit(page, '设置页深色玻璃 1280×800');
      await page.clickText('浅色', {
        expect: `document.documentElement.getAttribute('data-theme') === 'light'`,
      });
      return `高光 alpha：浅色 ${lightSpec} → 深色 ${darkSpec}`;
    });

    await step('玻璃不只在设置页：工作台顶栏与侧栏一起变，且不溢出', async () => {
      await page.goto(`${BASE}/#/studio`);
      await page.waitForFunction(`!!document.querySelector('.studio')`, { label: '工作台' });
      await sleep(900);
      const state = await page.evaluate(GLASS_STATE_EXPR);
      const bad = checkSurfaces(state, true, ['topbar', 'side', 'panel']);
      if (bad.length) throw new Error(bad.join('；'));
      await shot('20-studio-glass');
      await audit(page, '工作台液态玻璃 1280×800');
      await auditColors(page, '工作台液态玻璃');
      return 'topbar / side / panel 三面均半透带模糊';
    });

    await step('切回普通：面板退回实底、滑块消失', async () => {
      await page.goto(`${BASE}/#/settings`);
      await page.waitForFunction(
        `document.documentElement.dataset.appearance === 'glass' && !!document.querySelector('.glass-range')`,
        { label: '玻璃下的设置页' },
      );
      await page.clickText('普通', {
        expect: `document.documentElement.dataset.appearance === 'solid'`,
      });
      await sleep(400);
      const state = await page.evaluate(GLASS_STATE_EXPR);
      const bad = [];
      if (state.appearance !== 'solid') bad.push(`data-appearance=${state.appearance || '空'}`);
      if (state.rangeValue) bad.push('退回普通后滑块还在');
      bad.push(...checkSurfaces(state, false, ['card', 'topbar']));
      const persisted = await page.evaluate(
        `fetch('/api/settings').then((r) => r.json()).then((s) => s.appearance)`,
      );
      if (persisted !== 'solid') bad.push(`后端存的是 ${persisted}`);
      if (bad.length) throw new Error(bad.join('；'));
      return '材质、滑块、后端三处都退回普通';
    });

    /* ---------------- 12c. 缺 WebView2 时的那条横幅（用户手册 §2.2 的承诺） ----------------
       原生窗口装好时永远走不到这条分支，真实开发机上手工也复现不出来，
       所以让假后端扮演「这台电脑缺组件」（mock 的 windowRuntimeInstalled 默认 false）。 */
    const FALLBACK_NOTICE = `(() => {
      return [...document.querySelectorAll('.notice')].find(
        (n) => (n.textContent || '').includes('这个页面开在浏览器里'),
      ) || null;
    })()`;

    await step('没带回退标记：横幅不出现，且一次 window-runtime 请求都不发', async () => {
      let hits = 0;
      const off = page.on('Network.requestWillBeSent', (p) => {
        if ((p.request?.url || '').includes('/api/app/window-runtime')) hits += 1;
      });
      try {
        await page.goto(`${BASE}/#/settings`);
        await page.waitForFunction(`document.querySelectorAll('.card').length >= 6`, {
          label: '设置页',
        });
        await sleep(700);
        const bad = [];
        if (hits) bad.push(`多发了 ${hits} 次 window-runtime 请求`);
        if (await page.evaluate(`!!${FALLBACK_NOTICE}`)) bad.push('没带标记也弹了横幅');
        if (bad.length) throw new Error(bad.join('；'));
        return '绝大多数人（原生窗口）零打扰：0 请求 / 0 横幅';
      } finally {
        off();
      }
    });

    await step('带 ?browser_fallback=1：顶部说明原因并给出一键安装', async () => {
      await page.goto(`${BASE}/?browser_fallback=1#/studio`);
      await page.waitForFunction(
        `${FALLBACK_NOTICE} && [...${FALLBACK_NOTICE}.querySelectorAll('button')].some((b) => b.textContent.trim() === '一键安装')`,
        { label: '回退横幅', timeoutMs: 15000 },
      );
      const info = await page.evaluate(`(() => {
        const n = ${FALLBACK_NOTICE};
        return {
          text: (n.textContent || '').replace(/\\s+/g, ' ').trim(),
          buttons: [...n.querySelectorAll('button')].map((b) => b.textContent.trim()),
        };
      })()`);
      const bad = [];
      if (!info.text.includes('WebView2')) bad.push(`没点明缺的是哪个组件：${info.text}`);
      if (!info.text.includes('浏览器')) bad.push(`没说清为什么开了个网页：${info.text}`);
      if (!info.buttons.includes('知道了')) bad.push('缺「知道了」，用户没法收起它');
      if (bad.length) throw new Error(bad.join('；'));
      await shot('21-browser-fallback-notice');
      return info.text.slice(0, 52);
    });

    await step('点「一键安装」：真的发安装请求，并把结果说在横幅里', async () => {
      let posts = 0;
      const off = page.on('Network.requestWillBeSent', (p) => {
        const url = p.request?.url || '';
        if (url.includes('/api/app/window-runtime/install') && p.request?.method === 'POST') {
          posts += 1;
        }
      });
      try {
        await page.clickText('一键安装');
        await page.waitForFunction(
          `(${FALLBACK_NOTICE}?.textContent || '').includes('已安装完成')`,
          { label: '安装结果回显', timeoutMs: 15000 },
        );
        const text = await page.evaluate(
          `(${FALLBACK_NOTICE}?.textContent || '').replace(/\\s+/g, ' ').trim()`,
        );
        const bad = [];
        if (!posts) bad.push('横幅里那句话是写死的，根本没发安装请求');
        if (!text.includes('重开软件')) bad.push('装完没告诉用户下一步：' + text.slice(-40));
        if (bad.length) throw new Error(bad.join('；'));
        await shot('22-browser-fallback-installed');
        return `POST ${posts} 次｜${text.slice(-40)}`;
      } finally {
        off();
      }
    });

    await step('组件装好后再打开：横幅安静退出，不反复打扰', async () => {
      let pulls = 0;
      const off = page.on('Network.requestWillBeSent', (p) => {
        const url = p.request?.url || '';
        if (url.includes('/api/app/window-runtime') && p.request?.method === 'GET') pulls += 1;
      });
      try {
        // 先回空白页再过来：Page.navigate 到与当前完全一致的地址（连 hash 都一样）
        // 不会重新加载，上一轮那条带「已安装完成」的横幅会原样留着，
        // 既拉不到新状态也删不掉横幅，这一步就变成自己验自己（实测踩过）。
        await page.goto('about:blank');
        await page.goto(`${BASE}/?browser_fallback=1#/studio`);
        await page.waitForFunction(`!!document.querySelector('.studio')`, { label: '工作台' });
        // 先确认状态真的拉回来了：否则「没弹横幅」可能只是请求还没完成（假绿）
        const deadline = Date.now() + 6000;
        while (!pulls && Date.now() < deadline) await sleep(100);
        const bad = [];
        if (!pulls) bad.push('带着标记却没拉状态，下面的「没弹横幅」不算数');
        if (await page.evaluate(`!!${FALLBACK_NOTICE}`)) bad.push('组件已经在了，横幅还在挡路');
        if (bad.length) throw new Error(bad.join('；'));
        return `拉了 ${pulls} 次状态，installed=true 时不再渲染`;
      } finally {
        off();
      }
    });

    /* ---------------- 12d. 文件位置：日志路径必须来自后端 ----------------
       日志在可重建的 LOCALAPPDATA、设置在漫游的 APPDATA，两个目录不是一回事；
       界面以前写「设置、日志和临时文件都在这个目录」，是一句假话。 */
    await step('设置页「文件位置」：显示后端返回的真实 log_dir（不猜、不写死）', async () => {
      await page.goto(`${BASE}/#/settings`);
      await page.waitForFunction(`document.querySelectorAll('.card').length >= 6`, {
        label: '设置页',
      });
      const info = await page.evaluate(`(() => {
        const h2 = [...document.querySelectorAll('.card-head h2')].find(
          (el) => el.textContent.trim() === '文件位置',
        );
        const card = h2 && h2.closest('.card');
        const fields = card ? [...card.querySelectorAll('.field')] : [];
        const cache = fields.find(
          (f) => (f.querySelector('.field-label')?.textContent || '').trim() === '配置与缓存',
        );
        return {
          hint: cache
            ? (cache.querySelector('.field-hint')?.textContent || '').replace(/\\s+/g, ' ').trim()
            : '',
          shown: cache ? (cache.querySelector('.path-box code')?.textContent || '').trim() : '',
        };
      })()`);
      const api = await page.evaluate(`fetch('/api/settings').then((r) => r.json())`);
      const bad = [];
      if (!api.log_dir) bad.push('后端没返回 log_dir，界面只能靠猜');
      // 夹具里两个目录要是变得一样，这条检查就查不出东西了（防空转）
      if (api.log_dir === api.data_dir) bad.push('夹具里日志与配置同目录，这条检查等于没做');
      if (!info.hint.includes('日志不在这里')) bad.push(`提示还在撒谎：${info.hint}`);
      if (!info.hint.includes(api.log_dir)) bad.push(`界面上的日志路径与后端不一致：${info.hint}`);
      if (!info.shown.includes(api.data_dir)) bad.push('「配置与缓存」那行显示的不是 data_dir');
      if (bad.length) throw new Error(bad.join('；'));
      return `配置 …${api.data_dir.slice(-14)}｜日志 …${api.log_dir.slice(-14)}`;
    });

    /* ---------------- 12e. 回收站：删 → 列 → 恢复 → 彻底删 ----------------
       以前只能告诉用户「去资源管理器把目录挪回来」，界面没入口就等于没这个能力。
       这三步跑的是真链路：删的是**非当前项目**，不干扰后面「含真实模型」的布局复检；
       而不可逆的那一步必须能验到「没确认就删不掉」。 */
    const victim = { name: '', before: 0, after: 0 };

    await step('侧栏删除一个项目：先进回收站，不是真删（确认框文案得跟能力一致）', async () => {
      await page.goto(`${BASE}/#/studio`);
      await page.waitForFunction(`!!document.querySelector('.proj')`, { label: '项目侧栏' });
      await sleep(500);
      victim.name = '';
      const picked = await page.evaluate(`(() => {
        const rows = [...document.querySelectorAll('.proj')];
        const victimRow = rows.find((r) => !r.className.includes('is-active'));
        if (!victimRow) return null;
        const name = victimRow.querySelector('.proj-name').textContent.trim();
        victimRow.querySelector('.proj-more').click();
        // 只能把数据「递回 Node」：页面上没有 victim 这个对象，写进去就是 ReferenceError
        return { name, before: rows.length };
      })()`);
      if (!picked || !picked.name) throw new Error('侧栏里没有可删的非当前项目，这条会空跑');
      victim.name = picked.name;
      victim.before = picked.before;
      await sleep(300);
      const menuHit = await page.evaluate(`(() => {
        const item = [...document.querySelectorAll('[role="menuitem"]')]
          .find((b) => b.textContent.trim() === '删除');
        if (!item) return false;
        item.click();
        return true;
      })()`);
      if (!menuHit) throw new Error('项目菜单里找不到「删除」这一项');
      await page.waitForFunction(`!!document.querySelector('.modal')`, { label: '删除确认框' });
      const say = await page.evaluate(`(() => {
        const m = document.querySelector('.modal');
        return (m.querySelector('.muted')?.textContent || '').replace(/\\s+/g, ' ').trim();
      })()`);
      const bad = [];
      // 文案必须说「能在设置里恢复」——界面存在与否是一回事，文案骗人是另一回事
      if (!say.includes('回收站')) bad.push(`确认框没提回收站：${say}`);
      if (/暂不支持恢复|资源管理器/.test(say)) bad.push('确认框还在说「程序内不支持恢复」这种旧话');
      if (bad.length) throw new Error(bad.join('；'));
      await page.evaluate(`(() => {
        const btn = [...document.querySelectorAll('.modal-foot button')]
          .find((b) => b.textContent.trim() === '删除');
        btn.click();
      })()`);
      await sleep(900);
      victim.after = await page.evaluate(`document.querySelectorAll('.proj').length`);
      const still = await page.evaluate(
        `(async () => (await (await fetch('/api/projects/trash')).json()).count)()`,
      );
      const bad2 = [];
      if (victim.after !== victim.before - 1) bad2.push(`删后侧栏应有 ${victim.before - 1} 条，实际 ${victim.after}`);
      if (!(still >= 1)) bad2.push(`后端回收站里应该有东西，count=${still}`);
      if (await page.evaluate(`!!document.querySelector('.modal')`)) bad2.push('确认后弹窗没关');
      if (bad2.length) throw new Error(bad2.join('；'));
      return `侧栏 ${victim.before}→${victim.after}，后端回收站 ${still} 条`;
    });

    await step('设置→回收站：列得出刚删的项目，点「恢复」后侧栏数目回来', async () => {
      await page.goto(`${BASE}/#/settings`);
      await page.waitForFunction(`document.querySelectorAll('.card').length >= 6`, { label: '设置页' });
      const card = await page.evaluate(`(() => {
        const h2 = [...document.querySelectorAll('.card-head h2')]
          .find((el) => el.textContent.trim() === '文件位置');
        const box = h2 && h2.closest('.card');
        const field = box && [...box.querySelectorAll('.field')]
          .find((f) => (f.querySelector('.field-label')?.textContent || '').trim() === '回收站');
        if (!field) return { missing: '没有「回收站」这一栏' };
        const rows = [...field.querySelectorAll('.path-box')].map((r) => ({
          name: (r.querySelector('code')?.textContent || '').trim(),
          restore: !![...r.querySelectorAll('button')].find((b) => b.textContent.trim() === '恢复'),
          purge: !![...r.querySelectorAll('button')].find((b) => b.textContent.trim() === '彻底删除'),
        }));
        return { rows };
      })()`);
      if (card.missing) throw new Error(card.missing);
      const hit = card.rows.find((r) => r.name === victim.name);
      const bad = [];
      if (!hit) bad.push(`回收站里没有「${victim.name}」：${JSON.stringify(card.rows.map((r) => r.name))}`);
      if (hit && (!hit.restore || !hit.purge)) bad.push('条目上没有「恢复」或「彻底删除」按钮');
      if (hit) {
        await page.evaluate(`(() => {
          const field = [...document.querySelectorAll('.field')]
            .find((f) => (f.querySelector('.field-label')?.textContent || '').trim() === '回收站');
          const row = [...field.querySelectorAll('.path-box')]
            .find((r) => (r.querySelector('code')?.textContent || '').trim() === ${JSON.stringify(victim.name)});
          [...row.querySelectorAll('button')].find((b) => b.textContent.trim() === '恢复').click();
        })()`);
        await sleep(900);
        const left = await page.evaluate(
          `(async () => (await (await fetch('/api/projects/trash')).json()).items.map((i) => i.name))()`,
        );
        if (left.includes(victim.name)) bad.push('点了恢复，后端回收站里还有它');
        await page.goto(`${BASE}/#/studio`);
        await page.waitForFunction(`!!document.querySelector('.proj')`, { label: '项目侧栏' });
        await sleep(700);
        const back = await page.evaluate(`document.querySelectorAll('.proj').length`);
        if (back !== victim.before) bad.push(`恢复后侧栏应有 ${victim.before} 条，实际 ${back}`);
      }
      if (bad.length) throw new Error(bad.join('；'));
      return `列出 ${card.rows.length} 条→恢复→侧栏回到 ${victim.before} 条`;
    });

    await step('回收站「彻底删除」：没确认删不掉，确认了才真少一条', async () => {
      // 先再删一次同一个项目，让它回到回收站
      await page.goto(`${BASE}/#/studio`);
      await page.waitForFunction(`!!document.querySelector('.proj')`, { label: '项目侧栏' });
      await sleep(500);
      const found = await page.evaluate(`(() => {
        const rows = [...document.querySelectorAll('.proj')];
        const row = rows.find((r) => (r.querySelector('.proj-name')?.textContent || '').trim() === ${JSON.stringify(victim.name)});
        if (!row) return false;
        row.querySelector('.proj-more').click();
        return true;
      })()`);
      if (!found) throw new Error(`侧栏里找不到刚恢复的「${victim.name}」，恢复其实没成功`);
      await sleep(300);
      await page.evaluate(`(() => {
        const item = [...document.querySelectorAll('[role="menuitem"]')]
          .find((b) => b.textContent.trim() === '删除');
        item && item.click();
      })()`);
      await page.waitForFunction(`!!document.querySelector('.modal')`, { label: '删除确认框' });
      await page.evaluate(`(() => {
        const btn = [...document.querySelectorAll('.modal-foot button')]
          .find((b) => b.textContent.trim() === '删除');
        btn && btn.click();
      })()`);
      await sleep(900);
      await page.goto(`${BASE}/#/settings`);
      await page.waitForFunction(`document.querySelectorAll('.card').length >= 6`, { label: '设置页' });
      const openPurge = await page.evaluate(`(() => {
        const field = [...document.querySelectorAll('.field')]
          .find((f) => (f.querySelector('.field-label')?.textContent || '').trim() === '回收站');
        const row = field && [...field.querySelectorAll('.path-box')]
          .find((r) => (r.querySelector('code')?.textContent || '').trim() === ${JSON.stringify(victim.name)});
        if (!row) return false;
        [...row.querySelectorAll('button')].find((b) => b.textContent.trim() === '彻底删除').click();
        return true;
      })()`);
      if (!openPurge) throw new Error('回收站里找不到那一栏的「彻底删除」入口');
      await page.waitForFunction(`!!document.querySelector('.modal')`, { label: '二次确认弹窗' });
      const warn = await page.evaluate(`(() => {
        const m = document.querySelector('.modal');
        const t = m.querySelector('.modal-head')?.textContent || '';
        const b = m.querySelector('p')?.textContent || '';
        return (t + ' ' + b).replace(/\\s+/g, ' ').trim();
      })()`);
      const bad = [];
      if (!/无法恢复|永久/.test(warn)) bad.push(`不可逆操作没警告用户：${warn}`);
      // 先点「取消」：条目必须还在（这验的是「没确认就删不掉」）
      await page.evaluate(`(() => {
        const btn = [...document.querySelectorAll('.modal-foot button')]
          .find((b) => b.textContent.trim() === '取消');
        btn.click();
      })()`);
      await sleep(500);
      const cntBefore = await page.evaluate(
        `(async () => (await (await fetch('/api/projects/trash')).json()).count)()`,
      );
      await page.evaluate(`(() => {
        const field = [...document.querySelectorAll('.field')]
          .find((f) => (f.querySelector('.field-label')?.textContent || '').trim() === '回收站');
        const row = [...field.querySelectorAll('.path-box')]
          .find((r) => (r.querySelector('code')?.textContent || '').trim() === ${JSON.stringify(victim.name)});
        [...row.querySelectorAll('button')].find((b) => b.textContent.trim() === '彻底删除').click();
      })()`);
      await page.waitForFunction(`!!document.querySelector('.modal')`, { label: '二次确认弹窗' });
      await page.evaluate(`(() => {
        const btn = [...document.querySelectorAll('.modal-foot button')]
          .find((b) => b.textContent.trim() === '确认彻底删除');
        btn.click();
      })()`);
      await sleep(900);
      const cntAfter = await page.evaluate(
        `(async () => (await (await fetch('/api/projects/trash')).json()).count)()`,
      );
      const listed = await page.evaluate(
        `(async () => (await (await fetch('/api/projects/trash')).json()).items.map((i) => i.name))()`,
      );
      if (cntAfter !== cntBefore - 1) bad.push(`确认后条目应少一条：${cntBefore}→${cntAfter}`);
      if (listed.includes(victim.name)) bad.push('后端回收站里还能按名字查到它');
      const gone = await page.evaluate(`(() => {
        const field = [...document.querySelectorAll('.field')]
          .find((f) => (f.querySelector('.field-label')?.textContent || '').trim() === '回收站');
        return ![...field.querySelectorAll('.path-box')]
          .some((r) => (r.querySelector('code')?.textContent || '').trim() === ${JSON.stringify(victim.name)});
      })()`);
      if (!gone) bad.push('界面行没跟着消失（只能刷新才准）');
      if (bad.length) throw new Error(bad.join('；'));
      return `取消不删（仍 ${cntBefore} 条）→ 确认后 ${cntAfter} 条；${warn.slice(0, 26)}…`;
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
