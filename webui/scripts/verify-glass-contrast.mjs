/**
 * 玻璃上的字还读不读得清 —— 用截图像素实测，不看 CSS 声明。
 *
 * 为什么必须实测：audit-interface.mjs 算对比度是「向上找一个 alpha>0.95 的背景」，
 * 半透面板它找不到真实底，只能给一个乐观值。而液态玻璃的实际底 = 页底色 +
 * 环境光斑 + 透过来的内容（弹窗背后就是整页文字），混色后是什么亮度事先算不出来。
 * iOS 26 被骂得最狠的一条就是「太透看不清字」，26.1 才补上不透明度调节 ——
 * 我们既然开了这个口子，就必须拿像素证明它在自己允许的范围里不糊字。
 *
 * 口径（全部从同一对裁切图算，坐标逐像素对齐）：
 *   ratio     —— 字形 vs 面板底中位数（常规 WCAG 读数）
 *   worst     —— 字形 vs 面板底最不利的一侧（p05/p95）：背后内容透上来时掉的是这个数
 *   bleed     —— 面板底自身的亮度散布：底纹泄漏，越大越花
 *   separation—— 面板底 vs 「把面板藏起来同一位置的实底」：=1 就是和背后糊成一片
 * 字形像素由两步聚类判定，再把它周围 2px 的抗锯齿晕圈从「背景」里剔除 ——
 * 不剔除的话，边缘灰阶会被当成「很深的底」，任何材质都过不了门（第一版就栽在这）。
 * 「底」只取**字形外圈 3–11px 那一圈**，不是整个裁切框：一行小标题的裁切框里
 * 常常同时坐着右边的开关/色块（实测：深色设置页「卡片副标」的裁切框包含整张卡头，
 * 那块亮色分段控件把 p95 抬到 0.24，于是**完全实底、根本没透**的基线也被报成
 * 「最不利底 3.14:1」）。尺子量到别人身上，判出来的「读不动」就是假的。
 *
 * 前置：假后端 8760（node mock-server.mjs）、Vite dev 5273。
 * 运行：BASE_URL=http://localhost:5273 node scripts/verify-glass-contrast.mjs
 *       对照（复现修前）：ONLY=modal NO_FLOAT_FLOOR=1 node scripts/verify-glass-contrast.mjs
 *
 * 产出：scripts/shots/contrast/*.png + scripts/shots/contrast-report.json
 *       （对照模式写 contrast-report-no-floor.json，不会顶掉发布证据）
 *       写进 config.py / API-CONTRACT.md / COMPETITIVE-GAPS.md 的数字取自这里
 */
import { mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { connectPage, launchBrowser, sleep } from './cdp.mjs';
import { decodePng } from './png.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = join(HERE, 'shots', 'contrast');
const BASE = process.env.BASE_URL || 'http://127.0.0.1:5273';
const PORT = Number(process.env.CDP_PORT || 9343);
const VIEWPORT = { width: 1280, height: 800 };

/** WCAG 门限：正文 4.5:1（这些测量点都不到 18pt，一律按正文要求） */
const WCAG_BODY = 4.5;
/** 「字贴到最不利那块底」时的下限：按 WCAG 大字号 / 非文本控件那一档 3:1 */
const WCAG_LARGE = 3;
/** 浮层必须仍然像「浮在上面的一层」：拿同场景同主题同测量点的普通基线做参照。
 *  分界度的绝对值受主题支配 —— 深色里弹窗底 #141419 和被遮罩压暗的页底本来就只差
 *  1.02（靠描边和投影分层，不是玻璃引入的问题），所以门是相对量：
 *  玻璃不能把面板和背后拉得比实底基线更接近（给 10% 余量）。 */
const SEPARATION_SLACK = 0.9;
/** 没有基线行时的兜底绝对门 */
const MODAL_SEPARATION_MIN = 1.2;
/** 要扫的透明度：0/25/45/60 在产品允许范围内，75/90 用来看「超出上限会坏到哪」 */
const LEVELS = [0, 25, 45, 60, 75, 90];
const DEFAULT_LEVEL = 25;
/** 产品上限：这条线是像素实测定的（口径见 docs/COMPETITIVE-GAPS.md；数字在本脚本生成的
 *  shots/contrast-report.json 里 —— 那是生成物、不入库，要拿数字得自己跑一遍） */
const CAP = 60;
/** 只跑部分场景（调材质常量时迭代用）：ONLY=modal node scripts/verify-glass-contrast.mjs */
const ONLY = (process.env.ONLY || '')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);
const want = (key) => ONLY.length === 0 || ONLY.includes(key);

/**
 * NO_FLOAT_FLOOR=1：拿掉浮层的 94% 厚玻璃下限，量「如果没有这条下限会坏到哪」。
 * 文档里那些「修前」读数（弹窗正文 4.14 / 1.84）就是在这个模式下取的 ——
 * 量尺换了口径，旧数字必须能在新口径下重新取一遍，不然文档里的对比就是空对空。
 */
const NO_FLOOR = process.env.NO_FLOAT_FLOOR === '1';

const SCENARIOS = {
  settings: {
    route: '#/settings',
    targets: [
      { sel: '.card-head h2', name: '卡片标题 19px' },
      { sel: '.card-hint', name: '卡片副标 13px' },
      { sel: '.field-hint', name: '字段提示 12px（最弱一档）' },
      { sel: '.glass-tune-label', name: '滑块标签 13px' },
    ],
  },
  /* 弹窗：背后是整页密集文字，这才是「太透」真正的案发现场 */
  modal: {
    route: '#/settings',
    targets: [
      { sel: '.modal-head h2', name: '弹窗标题' },
      { sel: '.modal p', name: '弹窗正文 13px' },
    ],
  },
  studio: {
    route: '#/studio',
    targets: [
      { sel: '.side .proj', name: '侧栏项目名' },
      { sel: '.panel .field-label', name: '右栏字段名' },
      { sel: '.viewer-overlay', name: '预览浮层（压在 3D 画面上）' },
    ],
  },
  /* 菜单也是浮层：它背着一摞项目名，同样属于「背后有密集文字」那一类 */
  menu: {
    route: '#/studio',
    targets: [{ sel: '.menu button[role="menuitem"]', name: '菜单项文字' }],
  },
};

function relLuminance(r, g, b) {
  const lin = (v) => {
    const s = v / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

function contrastRatio(l1, l2) {
  const hi = Math.max(l1, l2);
  const lo = Math.min(l1, l2);
  return (hi + 0.05) / (lo + 0.05);
}

function percentile(sorted, q) {
  if (!sorted.length) return 0;
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.round((sorted.length - 1) * q)));
  return sorted[idx];
}

const mean = (arr) => arr.reduce((s, x) => s + x, 0) / arr.length;

/** PNG 裁切图 → 亮度矩阵 */
function lumaGrid(img) {
  const { width, height, data } = img;
  const grid = new Float64Array(width * height);
  for (let i = 0, p = 0; i < grid.length; i += 1, p += 4) {
    grid[i] = relLuminance(data[p], data[p + 1], data[p + 2]);
  }
  return { width, height, grid };
}

/**
 * 可见图上先定「字 / 底」，剔除字形 2px 晕圈，再用同一套坐标去读隐藏图。
 * @returns 指标对象，样本不足时返回 { note }
 */
function score(visible, hidden) {
  const { width, height, grid } = lumaGrid(visible);
  const bins = new Array(64).fill(0);
  const sums = new Float64Array(64);
  for (const l of grid) {
    const bin = Math.min(63, Math.floor(l * 64));
    bins[bin] += 1;
    sums[bin] += l;
  }
  let bgBin = 0;
  for (let i = 1; i < 64; i += 1) if (bins[i] > bins[bgBin]) bgBin = i;
  const bgSeed = sums[bgBin] / Math.max(bins[bgBin], 1);
  const ranked = Array.from(grid, (l) => ({ l, d: Math.abs(l - bgSeed) })).sort(
    (a, b) => b.d - a.d,
  );
  const take = Math.min(Math.max(12, Math.round(grid.length * 0.01)), Math.floor(grid.length / 4));
  const textSeed = mean(ranked.slice(0, take).map((x) => x.l));

  const isGlyph = new Uint8Array(grid.length);
  let glyphCount = 0;
  for (let i = 0; i < grid.length; i += 1) {
    if (Math.abs(grid[i] - textSeed) < Math.abs(grid[i] - bgSeed)) {
      isGlyph[i] = 1;
      glyphCount += 1;
    }
  }
  // 晕圈：字形上下左右 2px 内的像素既不是纯字也不是纯底，别拿它当底
  const nearGlyph = new Uint8Array(grid.length);
  // 外圈：字形 11px 以内才算「这行字坐的那块底」，更远的可能坐着开关/色块
  const HALO = 2;
  const RING = 11;
  const nearRing = new Uint8Array(grid.length);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const i = y * width + x;
      if (!isGlyph[i]) continue;
      for (let dy = -RING; dy <= RING; dy += 1) {
        const yy = y + dy;
        if (yy < 0 || yy >= height) continue;
        for (let dx = -RING; dx <= RING; dx += 1) {
          const xx = x + dx;
          if (xx < 0 || xx >= width) continue;
          const j = yy * width + xx;
          nearRing[j] = 1;
          if (Math.abs(dy) <= HALO && Math.abs(dx) <= HALO) nearGlyph[j] = 1;
        }
      }
    }
  }
  const glyphLums = [];
  let backIdx = [];
  for (let i = 0; i < grid.length; i += 1) {
    if (isGlyph[i]) {
      glyphLums.push(grid[i]);
      continue;
    }
    if (!nearGlyph[i] && nearRing[i]) backIdx.push(i);
  }
  const ringCount = backIdx.length;
  let fallback = '';
  if (ringCount < 120) {
    // 字形很密（比如整段被判定为字）时外圈会被晕圈吃光，退回全裁切并说明
    fallback = `底样本退到全裁切（外圈只剩 ${ringCount}）`;
    backIdx = [];
    for (let i = 0; i < grid.length; i += 1) if (!isGlyph[i] && !nearGlyph[i]) backIdx.push(i);
  }
  if (glyphLums.length < 12 || backIdx.length < 120) {
    return { note: `样本太少（字 ${glyphLums.length} 底 ${backIdx.length}）` };
  }
  // 字色取「字形核心团队」的均值（离背景最远的那 1%），不是整个字形类别的均值 ——
  // 后者把晕圈算进去，会把任何材质的读数系统拉低（第一版把实底基线量成 3.3:1，
  // 这不是产品不达标，是尺子错了）。WCAG 说的是「文字颜色 vs 背景颜色」，
  // 颜色就是字形本色；底纹透上来带来的难度则由 worst / bleed 这两个口径负责。
  const textL = mean(ranked.slice(0, take).map((x) => x.l));
  const back = grid.slice();
  const backSorted = backIdx.map((i) => back[i]).sort((a, b) => a - b);
  const panelMed = percentile(backSorted, 0.5);
  const lo = percentile(backSorted, 0.05);
  const hi = percentile(backSorted, 0.95);

  const hiddenGrid = lumaGrid(hidden).grid;
  const behindSorted = backIdx.map((i) => hiddenGrid[i]).sort((a, b) => a - b);
  const behindMed = percentile(behindSorted, 0.5);

  return {
    ratio: Number(contrastRatio(panelMed, textL).toFixed(2)),
    worst: Number(Math.min(contrastRatio(textL, lo), contrastRatio(textL, hi)).toFixed(2)),
    bleed: Number((hi - lo).toFixed(4)),
    separation: Number(contrastRatio(behindMed, panelMed).toFixed(3)),
    panelMed: Number(panelMed.toFixed(4)),
    behindMed: Number(behindMed.toFixed(4)),
    textL: Number(textL.toFixed(4)),
    glyphPixels: glyphLums.length,
    pixels: backIdx.length,
    note: [fallback, ringCount < 400 ? `外圈底像素只有 ${ringCount}，读数偏噪` : ''].filter(Boolean).join('；'),
  };
}

function rectExpr(sel) {
  return `(() => {
  const el = document.querySelector(${JSON.stringify(sel)});
  if (!el) return null;
  el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
  const r = el.getBoundingClientRect();
  return {
    x: r.x + window.scrollX,
    y: r.y + window.scrollY,
    width: r.width,
    height: r.height,
    top: r.top,
    bottom: r.bottom,
    panelX: r.x + window.scrollX,
  };
})()`;
}

/** 找到包住这段文字的「那一块玻璃」：最近的玻璃面板，找不到就用文字本身 */
function panelOfExpr(textSel, panelSels) {
  return `(() => {
  const el = document.querySelector(${JSON.stringify(textSel)});
  if (!el) return null;
  const sels = ${JSON.stringify(panelSels)};
  let panel = null;
  for (const s of sels) { const hit = el.closest(s); if (hit) { panel = hit; break; } }
  const target = panel || el;
  const r = target.getBoundingClientRect();
  // 全局只留一个标记：上一轮的残留会让「藏面板」藏到不相干的元素上，
  // 于是 behind 图与 visible 图完全相同，分界度永远量出 1.0
  document.querySelectorAll('[data-contrast-panel]').forEach((n) => {
    n.removeAttribute('data-contrast-panel');
    n.style.visibility = '';
  });
  target.setAttribute('data-contrast-panel', '1');
  return { x: r.x + window.scrollX, y: r.y + window.scrollY, width: r.width, height: r.height };
})()`;
}

async function clipShot(page, box, path) {
  const { data } = await page.send('Page.captureScreenshot', {
    format: 'png',
    clip: { x: box.x, y: box.y, width: box.width, height: box.height, scale: 1 },
  });
  writeFileSync(path, Buffer.from(data, 'base64'));
  return decodePng(path);
}

async function setHidden(hidden) {
  await pageEvaluate(
    `(() => { const el = document.querySelector('[data-contrast-panel]'); if (!el) return false;
      el.style.visibility = ${hidden ? "'hidden'" : "''"}; return true; })()`,
  );
}

/** 量一个测量点：同坐标拍两张（面板可见 / 隐藏），逐像素对齐后出四个口径 */
async function measureTarget(page, scenarioKey, theme, level, target, panelSels, rows) {
  const box = await page.evaluate(rectExpr(target.sel));
  if (!box || box.width < 8 || box.height < 8) {
    rows.push({ scenario: scenarioKey, theme, level, name: target.name, skipped: '元素缺失或太小' });
    return;
  }
  if (box.top < 2 || box.bottom > VIEWPORT.height - 2) {
    rows.push({ scenario: scenarioKey, theme, level, name: target.name, skipped: '不在视口内' });
    return;
  }
  const panel = await page.evaluate(panelOfExpr(target.sel, panelSels));
  if (!panel || panel.width < 8 || panel.height < 8) {
    rows.push({ scenario: scenarioKey, theme, level, name: target.name, skipped: '找不到所属面板' });
    return;
  }
  const crop = cropAround(box, panel);
  if (!crop) {
    rows.push({ scenario: scenarioKey, theme, level, name: target.name, sel: target.sel, skipped: '面板里没有可用的采样区域' });
    return;
  }
  const tag = `${scenarioKey}-${theme}-${level ?? 'solid'}-${target.sel.replace(/\W+/g, '')}`;
  try {
    const visible = await clipShot(page, crop, join(OUT, `${tag}.png`));
    await setHidden(true);
    const hidden = await clipShot(page, crop, join(OUT, `${tag}-behind.png`));
    const m = score(visible, hidden);
    rows.push({
      scenario: scenarioKey,
      theme,
      level,
      name: target.name,
      sel: target.sel,
      crop: { width: crop.width, height: crop.height },
      ...m,
      ...(m.note ? { skipped: m.note } : null),
    });
  } finally {
    // 藏了就必须恢复：否则下一个测量点背后是空气，而且页面上会少一块面板
    await setHidden(false);
    await sleep(60);
  }
}

/**
 * 采切区域 = 文字块向外扩 18px（拿够周圆的真实面板底），再裁到面板与视口里。
 * 只量文字那一行的小图会让「底」只剩几个像素，小字号标签直接量不出来。
 */
function cropAround(box, panel) {
  const PAD = 18;
  const bw = Math.round(box.width);
  const bh = Math.round(box.height);
  const width = Math.max(8, Math.min(bw + PAD * 2, Math.round(panel.width) - 4));
  const height = Math.max(8, Math.min(bh + PAD * 2, Math.round(panel.height) - 4));
  let x = Math.round(box.x) - Math.round((width - bw) / 2);
  let y = Math.round(box.y) - Math.round((height - bh) / 2);
  x = Math.max(Math.round(panel.x) + 2, x);
  x = Math.min(Math.round(panel.x + panel.width) - width - 2, x);
  x = Math.max(1, Math.min(x, VIEWPORT.width - width - 1));
  y = Math.max(Math.round(panel.y) + 2, y);
  y = Math.min(Math.round(panel.y + panel.height) - height - 2, y);
  y = Math.max(1, Math.min(y, VIEWPORT.height - height - 1));
  if (width < 8 || height < 8) return null;
  return { x, y, width, height };
}

/**
 * 透明度：注入 !important 规则，而不是写内联变量 ——
 * 内联值会被 React 的 applyAppearance 按状态重写回去（第一版栽过：六档量出同一张图）。
 * author + !important 压过普通内联声明，才真能代表「如果允许到这个透明度会怎样」。
 */
let pageEvaluateRef = null;
async function pageEvaluate(expr) {
  return pageEvaluateRef.evaluate(expr);
}

async function setLevel(page, level) {
  // 下面整段是在页面里求值的字符串：Node 侧的变量必须靠模板插值拼进去，
  // 直接写 NO_FLOOR 会得到页面的 ReferenceError（注释里也不能写插值语法，会被当真插值解析）。
  await page.evaluate(`(() => {
    const id = 'glass-contrast-override';
    let el = document.getElementById(id);
    if (!el) { el = document.createElement('style'); el.id = id; document.head.appendChild(el); }
    el.textContent = ':root { --glass-solid: ${100 - level}% !important;${NO_FLOOR ? ' --glass-solid-float: var(--glass-solid) !important;' : ''} }';
    return true;
  })()`);
  await sleep(260);
}

/**
 * 右下角的 toast 会盖住设置页的开关（命中测试直接报错），但**不能** .remove() 它 ——
 * 那些节点是 React 自家的，拆掉之后下一次渲染就会在 detached 节点上报错、整棵树卸载。
 * 只能等它自己超时消失。
 */
async function waitToastsGone(page) {
  try {
    await page.waitForFunction(`!document.querySelector('.toast')`, { label: '等 toast 消失', timeoutMs: 15000 });
  } catch {
    /* 还有就继续，下一步 clickText 自带重试 */
  }
}

/** 材质开关：按 aria-label 定位，不拿文本点 ——「普通为实底面板…」那段提示里也有「普通」 */
async function setMaterial(page, want) {
  const index = want === 'solid' ? 0 : 1;
  await waitToastsGone(page);
  await page.click(`.seg[aria-label="界面材质"] button`, { index });
  await page.waitForFunction(
    `document.documentElement.dataset.appearance === ${JSON.stringify(want)}`,
    { label: `材质 ${want}`, timeoutMs: 8000 },
  );
  await sleep(320);
}

async function main() {
  rmSync(OUT, { recursive: true, force: true });
  mkdirSync(OUT, { recursive: true });
  if (NO_FLOOR) {
    console.log('【对照模式】已拿掉浮层厚玻璃下限（复现「修前」）——这一步不达标是预期的，不进发布门禁');
  }

  const browser = await launchBrowser({ port: PORT, width: VIEWPORT.width, height: VIEWPORT.height });
  const page = await connectPage(browser.port, 'about:blank');
  pageEvaluateRef = page;
  await page.setViewport(VIEWPORT.width, VIEWPORT.height);
  const rows = [];
  /** 一个场景里的「面板」候选：从上往下找第一个命中的祖先 */
  const PANEL_SELS = ['.modal', '.card', '.side', '.panel', '.params-sheet', '.viewer-overlay', '.topbar'];

  async function sweep(scenarioKey, level, theme = 'light') {
    const scenario = SCENARIOS[scenarioKey];
    for (const target of scenario.targets) {
      await measureTarget(page, scenarioKey, theme, level, target, PANEL_SELS, rows);
    }
  }

  const escKey = `(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); return true; })()`;

  async function openModal(label) {
    await waitToastsGone(page);
    await page.clickText('管理机型');
    await page.waitForFunction(`!!document.querySelector('.modal-head h2')`, { label });
    await sleep(600);
  }

  /**
   * 弹窗一轮：先扫玻璃档位，再量普通基线。
   * 普通基线必须先关掉弹窗才能点到「普通」开关（开关在遮罩底下）。
   */
  async function modalPass(theme, levels) {
    await setMaterial(page, 'glass');
    await openModal(`机型管理弹窗（${theme}）`);
    for (const level of levels) {
      await setLevel(page, level);
      await sweep('modal', level, theme);
    }
    await page.evaluate(escKey);
    await sleep(400);
    await setMaterial(page, 'solid');
    await openModal(`机型管理弹窗（${theme}·普通）`);
    await sweep('modal', null, theme);
    await page.evaluate(escKey);
    await sleep(400);
  }

  /**
   * 项目行的「更多」菜单：点开 → 量一轮 → Esc 关掉。
   * 用 JS click 而不用 page.click：菜单触发器在侧栏里，按坐标点容易先滚一下页面。
   */
  async function measureMenu(level, theme = 'light') {
    const openedJs = await page.evaluate(
      `(() => { const b = document.querySelector('.proj-more'); if (!b) return false; b.click(); return true; })()`,
    );
    const hasItem = await page.evaluate(`!!document.querySelector('.menu button[role="menuitem"]')`);
    if (!openedJs || !hasItem) {
      rows.push({ scenario: 'menu', theme, level, name: '菜单项文字', skipped: openedJs ? '菜单没展开' : '没有 .proj-more' });
      return;
    }
    await sleep(280);
    await sweep('menu', level, theme);
    await page.evaluate(escKey);
    await sleep(220);
  }

  try {
    /* ---- 1. 设置页：普通基线 + 逐档扫 ---- */
    if (want('settings')) {
    await page.goto(`${BASE}/${SCENARIOS.settings.route}`);
    await page.waitForFunction(`!!document.querySelector('.card-head h2')`, { label: '设置卡片' });
    await sleep(700);
    await setMaterial(page, 'solid');
    await sweep('settings', null);
    await setMaterial(page, 'glass');
    for (const level of LEVELS) {
      await setLevel(page, level);
      await sweep('settings', level);
    }
    }

    /* ---- 2. 弹窗：背后整页文字，透上来就是它 ---- */
    if (want('modal')) {
    // 单独跑这一步时没有前置：自己把页面摆好（modalPass 里会自己切材质）
    await page.goto(`${BASE}/${SCENARIOS.settings.route}`);
    await page.waitForFunction(`!!document.querySelector('.card-head h2')`, { label: '设置卡片' });
    await sleep(700);
    await modalPass('light', [DEFAULT_LEVEL, CAP, 75, 90]);
    }

    /* ---- 3. 深色主题：文字反过来是浅色，底是光斑，风险完全不同 ---- */
    if (want('settings') || want('modal')) {
    await page.goto(`${BASE}/${SCENARIOS.settings.route}`);
    await page.waitForFunction(`!!document.querySelector('.card-head h2')`, { label: '设置卡片' });
    await sleep(500);
    await waitToastsGone(page);
    await page.clickText('深色', {
      expect: `document.documentElement.getAttribute('data-theme') === 'dark'`,
    });
    await sleep(500);
    if (want('settings')) {
      await setMaterial(page, 'solid');
      await sweep('settings', null, 'dark');
      await setMaterial(page, 'glass');
      for (const level of [DEFAULT_LEVEL, CAP, 90]) {
        await setLevel(page, level);
        await sweep('settings', level, 'dark');
      }
    }
    // 深色弹窗：透上来的是浅色字，会把暗底提亮 —— 和浅色的失效方向相反，必须单独量
    if (want('modal')) await modalPass('dark', [DEFAULT_LEVEL, CAP, 90]);
    await waitToastsGone(page);
    await page.clickText('浅色', {
      expect: `document.documentElement.getAttribute('data-theme') === 'light'`,
    });
    await sleep(400);
    }

    /* ---- 4. 工作台：面板压在 3D 画面上（普通基线在设置页切好再过来） ---- */
    if (want('studio')) {
    // 材质开关只在设置页有：单独跑这一步也要先把页面摆到那儿
    await page.goto(`${BASE}/${SCENARIOS.settings.route}`);
    await page.waitForFunction(`!!document.querySelector('.seg[aria-label="界面材质"]')`, { label: '材质开关' });
    await setMaterial(page, 'solid');
    await page.goto(`${BASE}/${SCENARIOS.studio.route}`);
    await page.waitForFunction(`!!document.querySelector('.studio')`, { label: '工作台' });
    await waitToastsGone(page);
    await page.click('.proj');
    await page.waitForFunction(`!!document.querySelector('.viewer-overlay')`, {
      label: '预览浮层',
      timeoutMs: 20000,
    });
    await sleep(1600);
    await sweep('studio', null);
    await measureMenu(null);
    await page.goto(`${BASE}/${SCENARIOS.settings.route}`);
    await page.waitForFunction(`!!document.querySelector('.seg[aria-label="界面材质"]')`, { label: '材质开关' });
    await setMaterial(page, 'glass');
    await page.goto(`${BASE}/${SCENARIOS.studio.route}`);
    await page.waitForFunction(`!!document.querySelector('.side .proj')`, { label: '项目列表', timeoutMs: 20000 });
    await waitToastsGone(page);
    await page.click('.proj');
    await page.waitForFunction(`!!document.querySelector('.viewer-overlay')`, {
      label: '预览浮层',
      timeoutMs: 20000,
    });
    await sleep(1800);
    for (const level of LEVELS) {
      await setLevel(page, level);
      await sweep('studio', level);
      await measureMenu(level);
    }
    }
  } finally {
    page.close();
    await browser.kill();
  }

  /* ---- 汇总 ---- */
  const measured = rows.filter((r) => !r.skipped);
  const labelOf = (level, theme) =>
    `${theme === 'light' ? '' : `${theme}·`}${level === null ? '普通' : `玻璃${level}%`}`;
  console.log('场景      档位        测量点                          字/底    最不利底  底纹散布  分界度');
  for (const r of measured) {
    console.log(
      `${r.scenario.padEnd(9)} ${labelOf(r.level, r.theme).padEnd(11)} ${r.name.padEnd(30)}` +
        ` ${String(r.ratio).padStart(5)}:1 ${String(r.worst).padStart(7)}:1` +
        `   ${String(r.bleed).padStart(6)}   ${r.separation}` +
        // 采样口径退化时必须说出来：不然一份“全达标”的报告可能其实量的是错的像素
        (r.note ? `   ｜ ${r.note}` : ''),
    );
  }
  for (const r of rows.filter((x) => x.skipped)) {
    console.log(`${r.scenario.padEnd(9)} ${labelOf(r.level, r.theme).padEnd(11)} ${r.name.padEnd(30)} 跳过：${r.skipped}`);
  }

  const groups = new Map();
  for (const r of measured) {
    const key = `${r.scenario}｜${labelOf(r.level, r.theme)}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }
  const summary = {};
  for (const [key, set] of groups) {
    const worstRow = set.reduce((a, b) => (a.worst <= b.worst ? a : b));
    summary[key] = {
      minWorst: worstRow.worst,
      minWorstTarget: worstRow.name,
      maxBleed: Number(Math.max(...set.map((r) => r.bleed)).toFixed(4)),
      minSeparation: Number(Math.min(...set.map((r) => r.separation)).toFixed(3)),
      belowCount: set.filter((r) => r.worst < WCAG_BODY).length,
    };
    console.log(
      `${key.padEnd(22)} 最低最不利底 ${String(worstRow.worst).padStart(5)}:1（${worstRow.name}）` +
        ` 最大底纹散布 ${summary[key].maxBleed.toFixed(3)} 最小分界度 ${summary[key].minSeparation}` +
        (summary[key].belowCount ? ` —— ${summary[key].belowCount} 处 < ${WCAG_BODY}:1` : ' —— 文字全部达标'),
    );
  }

  const failures = [];
  const warnings = [];
  /** 字能不能读：ratio 走 WCAG 正文门；worst 是「字贴到最不利那块底」时的读数，按大字号 3:1 要求 */
  const gate = (r, why) => {
    if (r.ratio < WCAG_BODY) failures.push(`${why}：${r.scenario} ${r.theme} ${r.name} 对比度 ${r.ratio}:1 < ${WCAG_BODY}:1`);
    else if (r.worst < WCAG_LARGE)
      warnings.push(`${why}：${r.scenario} ${r.theme} ${r.name} 最不利底 ${r.worst}:1（ratio ${r.ratio}:1，底纹散布 ${r.bleed}）`);
  };
  for (const r of measured.filter((x) => x.level === null)) gate(r, '普通材质基线');
  for (const r of measured.filter((x) => x.level !== null && x.level <= CAP)) {
    gate(r, r.level === DEFAULT_LEVEL ? `默认 ${DEFAULT_LEVEL}%` : r.level === CAP ? `上限 ${CAP}%` : `玻璃 ${r.level}%`);
  }
  // 分界度不设绝对门（见 SEPARATION_SLACK 那段注释）：拿普通基线做参照。
  // 但浮层（弹窗、菜单）不能比实底版本更「和背后面糊成一片」，它们背后是整页文字。
  const FLOAT_SCENARIOS = new Set(['modal', 'menu']);
  const baseline = new Map();
  for (const r of measured.filter((x) => x.level === null)) {
    baseline.set(`${r.scenario}|${r.theme}|${r.sel}`, r.separation);
  }
  for (const r of measured.filter(
    (x) => FLOAT_SCENARIOS.has(x.scenario) && x.level !== null && x.level <= CAP,
  )) {
    const base = baseline.get(`${r.scenario}|${r.theme}|${r.sel}`);
    const floor = base === undefined ? MODAL_SEPARATION_MIN : Number((base * SEPARATION_SLACK).toFixed(3));
    if (r.separation < floor) {
      failures.push(
        `浮层与背后页面糊成一层：${r.scenario} ${r.theme} 透明度 ${r.level}% 分界度 ${r.separation} < ${floor}` +
          `（${r.name}，普通基线 ${base ?? '缺失'}）`,
      );
    }
  }

  const reportName = NO_FLOOR ? 'contrast-report-no-floor.json' : 'contrast-report.json';
  writeFileSync(
    join(HERE, 'shots', reportName),
    JSON.stringify(
      {
        wcag: WCAG_BODY,
        wcagLarge: WCAG_LARGE,
        separationSlack: SEPARATION_SLACK,
        modalSeparationMin: MODAL_SEPARATION_MIN,
        levels: LEVELS,
        defaultLevel: DEFAULT_LEVEL,
        cap: CAP,
        // 这两项是口径本身。改了它们，报告里的数字就不是同一把尺子量出来的，
        // 读的人（包括下次改材质的自己）必须能看到。
        floatFloor: !NO_FLOOR,
        backgroundSample: '字形外圈 3–11px（晕圈 2px 剔除）',
        method: '同坐标两张裁切（面板可见/隐藏）→ 字形聚类 + 2px 晕圈剔除 → WCAG 相对亮度',
        summary,
        rows,
      },
      null,
      2,
    ),
  );
  console.log(`\n明细：${join(HERE, 'shots', reportName)}`);
  if (warnings.length) {
    console.log('提醒（底纹泄漏，不计入失败）：');
    for (const w of warnings) console.log(`  · ${w}`);
  }
  if (failures.length) {
    console.log('不达标：');
    for (const f of failures) console.log(`  - ${f}`);
  }
  console.log(failures.length ? `CONTRAST_FAIL（${failures.length} 项）` : 'CONTRAST_OK');
  process.exit(failures.length ? 1 : 0);
}

main().catch((err) => {
  console.error('脚本崩溃：', err);
  process.exit(2);
});
