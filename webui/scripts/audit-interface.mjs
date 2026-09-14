/**
 * AI 3D Forge —— 界面评审实测（按 jakubkrehel/skills 的条款取证）
 *
 * 为什么单独一支：layout-audit.mjs 只查「溢出 / 裁切 / 越界 / 图标按钮缺名字 / 点击区过小」。
 * 而 skills 里还有几条**一票 HIGH**的触发条件从没被查过：
 *   - 键盘可达的控件没有可见焦点指示（better-accessibility）
 *   - 正文/控件文字的**实测**对比度不达标（better-colors：不许估算，必须量）
 *   - 不尊重 prefers-reduced-motion（better-accessibility / better-ui）
 *   - 320px 宽或 200% 缩放下内容被裁切/横向滚动（better-accessibility / better-layout）
 *   - 状态只靠颜色传达（better-accessibility）
 *   - 会变化的数字没用 tabular-nums（better-typography）
 *   - 字号脱离设计令牌音阶（better-typography）
 *   - transition: all / 时长超过 400ms（better-ui）
 *   - 每页一个 h1、存在 main 地标、有跳到主内容的入口（better-accessibility）
 *
 * 用法：node webui/scripts/audit-interface.mjs [baseUrl]
 * 需要真实后端（它托管 webui/dist）：见 README-开发者.md
 */

import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, '..', '..');

const baseUrl = (process.argv[2] || 'http://127.0.0.1:8770').replace(/\/$/, '');
const routes = ['#/welcome', '#/studio', '#/settings', '#/diagnostics', '#/about'];

/** 会变化的数字（尺寸/面数/参数值）只有**加载了模型**才存在。
 *  只测空工作台会让 tabular-nums 这一项永远"0 个候选"，看起来全清其实没查。
 *  所以这里从后端取一个真实项目，把它也当成一个路由来审。 */
async function withProjectRoute(list) {
  try {
    const res = await fetch(`${baseUrl}/api/projects`);
    if (!res.ok) return list;
    const data = await res.json();
    const projects = Array.isArray(data) ? data : data.projects || [];
    const withModel = projects.find((p) => p.has_model || p.revision > 0) || projects[0];
    if (withModel && withModel.id) {
      return [...list, `#/studio/${withModel.id}`];
    }
  } catch {
    /* 取不到就按原来的路由审，并在输出里说明 */
  }
  return list;
}

/** 设计令牌里的音阶（tokens.css 的 --fs-*）：出现别的字号就是脱离系统 */
const TYPE_SCALE = [12, 13, 14, 15, 19, 26, 40];

/* ------------------------------------------------------------------ *
 * 浏览器侧：必须是**真函数**（不是字符串）。
 * 踩过的坑：page.evaluate 传字符串时它只求值、不调用，于是永远返回 undefined，
 * 结果这份"全清"的报告其实什么都没查 —— skills 管这叫静默的覆盖缺口。
 * ------------------------------------------------------------------ */
function auditPage(TYPE_SCALE_ARR) {
  const TYPE_SCALE_SET = new Set(TYPE_SCALE_ARR);
  const out = {
    name: [], contrast: [], sizes: [], transitions: [], nums: [], colorOnly: [], structure: {},
    // 每个检查「实际看了多少个候选」。skills 里明确要求不能出现静默的覆盖缺口：
    // 「0 个问题」和「什么都没查」必须能区分开。
    inspected: {},
  };

  const visible = (el) => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const label = (el) => {
    const raw = typeof el.className === 'string' ? el.className : '';
    const cls = raw ? '.' + raw.trim().split(/\s+/).slice(0, 2).join('.') : '';
    const t = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40);
    return el.tagName.toLowerCase() + cls + (t ? ' :: "' + t + '"' : '');
  };
  const interactive =
    'a[href], button, input, select, textarea, [role="button"], [role="tab"], [role="checkbox"], [role="switch"], [tabindex]:not([tabindex="-1"])';

  // ---- 1. 可访问名称：所有交互元素，而不只是图标按钮 ----
  let interactiveSeen = 0;
  for (const el of document.querySelectorAll(interactive)) {
    if (!visible(el)) continue;
    if (el.tagName === 'INPUT' && el.getAttribute('type') === 'hidden') continue;
    interactiveSeen += 1;
    const named =
      (el.getAttribute('aria-label') || '').trim() ||
      (el.getAttribute('aria-labelledby') || '').trim() ||
      (el.getAttribute('title') || '').trim() ||
      (el.textContent || '').trim() ||
      (el.id && document.querySelector('label[for="' + el.id + '"]')) ||
      (el.closest('label') ? 'wrapped-label' : '') ||
      (el.getAttribute('alt') || '').trim() ||
      (el.tagName === 'INPUT' ? (el.getAttribute('placeholder') || '').trim() : '');
    if (!named) out.name.push(label(el));
  }
  out.inspected.interactive = interactiveSeen;

  // ---- 2. 字号是否落在令牌音阶上 ----
  const badSizes = new Set();
  let sizeCandidates = 0;
  for (const el of document.querySelectorAll('body *')) {
    if (!visible(el)) continue;
    const hasText = Array.from(el.childNodes).some((n) => n.nodeType === 3 && n.textContent.trim());
    if (!hasText) continue;
    sizeCandidates += 1;
    const px = Math.round(parseFloat(getComputedStyle(el).fontSize));
    if (!TYPE_SCALE_SET.has(px)) badSizes.add(px + 'px @ ' + label(el));
  }
  out.inspected.sizes = sizeCandidates;
  out.sizes = Array.from(badSizes);

  // ---- 3. 实测对比度（量渲染后的前景/背景，不估算）----
  const lum = (rgb) => {
    const [r, g, b] = rgb.map((v) => {
      const c = v / 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const parse = (css) => {
    const m = (css || '').match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const parts = m[1].split(',').map((s) => parseFloat(s));
    return { rgb: [parts[0], parts[1], parts[2]], a: parts.length > 3 ? parts[3] : 1 };
  };
  const bgOf = (el) => {
    let node = el;
    while (node && node !== document.documentElement) {
      const c = parse(getComputedStyle(node).backgroundColor);
      if (c && c.a > 0.95) return c.rgb;
      node = node.parentElement;
    }
    return [255, 255, 255];
  };
  const seenContrast = new Set();
  let contrastCandidates = 0;
  for (const el of document.querySelectorAll('body *')) {
    if (!visible(el)) continue;
    const text = Array.from(el.childNodes)
      .filter((n) => n.nodeType === 3)
      .map((n) => n.textContent)
      .join('')
      .trim();
    if (!text) continue;
    const s = getComputedStyle(el);
    const fg = parse(s.color);
    if (!fg) continue;
    contrastCandidates += 1;
    const size = parseFloat(s.fontSize);
    const weight = parseInt(s.fontWeight, 10) || 400;
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    const need = large ? 3 : 4.5;
    const bg = bgOf(el);
    const l1 = lum(fg.rgb);
    const l2 = lum(bg);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    if (ratio + 0.005 < need) {
      const key = label(el) + '|' + ratio.toFixed(2);
      if (!seenContrast.has(key)) {
        seenContrast.add(key);
        out.contrast.push({
          el: label(el),
          ratio: Number(ratio.toFixed(2)),
          need,
          fg: s.color,
          bg: 'rgb(' + bg.join(', ') + ')',
          size,
          weight,
        });
      }
    }
  }
  out.inspected.contrast = contrastCandidates;

  // ---- 4. transition: all / 时长过长 ----
  // 只看**真的声明了过渡**的元素：`transition-property` 的 CSS 初始值就是 `all`，
  // 一个没写过渡的元素计算出来也是 `all 0s`。第一版没排除 0s，于是每页报了
  // 36~138 条「transition: all」——全部是误报，而源码里一个 `transition: all` 都没有。
  const seenTr = new Set();
  let transitionCandidates = 0;
  for (const el of document.querySelectorAll('body *')) {
    if (!visible(el)) continue;
    const s = getComputedStyle(el);
    const durations = s.transitionDuration.split(',').map((d) => parseFloat(d) * (d.includes('ms') ? 1 : 1000));
    const worst = Math.max(0, ...durations);
    if (worst <= 0) continue; // 没声明过渡
    transitionCandidates += 1;
    if (s.transitionProperty.includes('all')) {
      const k = 'all|' + label(el);
      if (!seenTr.has(k)) {
        seenTr.add(k);
        out.transitions.push({ el: label(el), kind: 'transition: all', value: s.transitionProperty + ' ' + s.transitionDuration });
      }
    }
    if (worst > 400) {
      const k = 'long|' + label(el) + worst;
      if (!seenTr.has(k)) {
        seenTr.add(k);
        out.transitions.push({ el: label(el), kind: '时长 > 400ms', value: s.transitionDuration });
      }
    }
  }
  out.inspected.transitions = transitionCandidates;

  // ---- 5. 会变化的数字有没有 tabular-nums ----
  // 选择器必须对着**真实类名**：`.stat-value`（尺寸/面数，每次生成都会变）、
  // `.readiness-num`（顶栏 11/12）、`.runbar-status-text`（进度文案）、
  // `.chip-unit`（参数值）、`.verdict-score span`、`.downloads`（文件大小）。
  const numSelector = [
    '.stat-value', '.readiness-num', '.runbar-status-text', '.chip-unit',
    '.verdict-score span', '.downloads', '.stats-cell b', '[data-changing-number]',
  ].join(', ');
  // 候选还要**含数字**才算：`.chip-unit` 的文本是单位 "mm"，它不会变，
  // 把它算进来会得到 16 条假问题（第一版就是这样）。会跳动的是数值本身。
  const numEls = Array.from(document.querySelectorAll(numSelector))
    .filter(visible)
    .filter((el) => /\d/.test(el.textContent || ''));
  out.inspected.numbers = numEls.length;
  for (const el of numEls) {
    const s = getComputedStyle(el);
    if (!s.fontVariantNumeric.includes('tabular-nums')) {
      out.nums.push({ el: label(el), variant: s.fontVariantNumeric || '(默认)' });
    }
  }

  // ---- 6. 状态是否只靠颜色 ----
  // 真实类名是 .dot / .dot-ok / .dot-warn / .dot-fail / .dot-checking。
  const dots = Array.from(document.querySelectorAll('.dot, [data-status]')).filter(visible);
  out.inspected.colorOnly = dots.length;
  for (const el of dots) {
    const parent = el.parentElement;
    const text = ((parent && parent.textContent) || '').trim();
    if (!text && !el.querySelector('svg')) out.colorOnly.push(label(el));
  }

  // ---- 7. 结构：h1 / main / 跳转入口 ----
  const h1s = Array.from(document.querySelectorAll('h1')).filter(visible);
  out.structure.h1 = h1s.length;
  out.structure.h1Text = h1s.map((h) => (h.textContent || '').trim().slice(0, 30));
  out.structure.main = document.querySelectorAll('main, [role="main"]').length;
  // 文案是"跳到主要内容"（没有"跳过"两字）——第一版正则只找 /跳过|skip/，
  // 于是明明加了跳转链接还是报 N。这种"检查项自己写错"的假阴性同样是覆盖缺口。
  out.structure.skipLink = Array.from(document.querySelectorAll('a')).some((a) =>
    /跳到|跳过|skip\s*to|skip/i.test(a.textContent || '')
  );

  return out;
}

/** 当前聚焦元素有没有可见的焦点指示（键盘走查时逐个调用）。 */
function focusIndicatorProbe() {
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  const s = getComputedStyle(el);
  const outlineWidth = parseFloat(s.outlineWidth) || 0;
  const hasOutline = outlineWidth >= 1 && s.outlineStyle !== 'none';
  const hasRing = !!s.boxShadow && s.boxShadow !== 'none';
  const raw = typeof el.className === 'string' ? el.className : '';
  const cls = raw ? '.' + raw.trim().split(/\s+/).slice(0, 2).join('.') : '';
  const text = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 28);
  return {
    el: el.tagName.toLowerCase() + cls + (text ? ' :: "' + text + '"' : ''),
    hasIndicator: hasOutline || hasRing,
    outline: s.outlineStyle + ' ' + s.outlineWidth + ' ' + s.outlineColor,
    shadow: (s.boxShadow || 'none').slice(0, 70),
  };
}

/** 主题是**后端设置**说了算（ThemeBridge 启动后会用它覆盖 localStorage），
 *  所以审深色必须真的把后端设置切过去，审完再切回来 —— 评审不能留下副作用。 */
async function readTheme() {
  try {
    const res = await fetch(`${baseUrl}/api/settings`);
    if (!res.ok) return null;
    const data = await res.json();
    return data.theme || null;
  } catch {
    return null;
  }
}

async function writeTheme(theme) {
  if (!theme) return false;
  try {
    const res = await fetch(`${baseUrl}/api/settings`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

async function main() {
  const { chromium } = await import('playwright');
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const report = { base: baseUrl, routes: {}, generatedAt: new Date().toISOString() };
  let failures = 0;
  const auditRoutes = await withProjectRoute(routes);
  const originalTheme = await readTheme();
  report.originalTheme = originalTheme;

  for (const route of auditRoutes) {
    const entry = { normal: null, tabbed: [], narrow: null, zoom: null, reduced: null };
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();
    try {
      await page.goto(`${baseUrl}/${route}`, { waitUntil: 'networkidle', timeout: 40000 });
      await page.waitForTimeout(700);
      entry.normal = await page.evaluate(auditPage, TYPE_SCALE);

      // 键盘走查：连按 Tab，逐个记录焦点指示（:focus-visible 只在键盘导航时生效）
      for (let i = 0; i < 25; i += 1) {
        await page.keyboard.press('Tab');
        await page.waitForTimeout(30);
        const probe = await page.evaluate(focusIndicatorProbe);
        if (probe) entry.tabbed.push(probe);
      }

      // 视口按**产品真正支持的尺寸**测：窗口 min_size = 1080×700（src/aiforge/server.py:146）。
      //   1080px = 最小窗口；
      //   540px  = 最小窗口在 200% 缩放下的等效 CSS 宽（WCAG 1.4.4 要求 200% 可用）；
      //   320px  = WCAG 1.4.10 的重排基准，**本项目到不了**（窗口最小 1080），
      //            所以只记录、不计入失败，避免拿不适用的尺子报假问题。
      const measure = () => {
        const doc = document.documentElement;
        const overflow = doc.scrollWidth - doc.clientWidth;
        const offenders = [];
        if (overflow > 1) {
          for (const el of document.querySelectorAll('body *')) {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.right > doc.clientWidth + 1) {
              const raw = typeof el.className === 'string' ? el.className : '';
              const cls = raw ? '.' + raw.trim().split(/\s+/).slice(0, 2).join('.') : '';
              offenders.push(el.tagName.toLowerCase() + cls + ' right=' + Math.round(r.right));
            }
          }
        }
        return { overflow, width: doc.clientWidth, offenders: offenders.slice(0, 8) };
      };

      entry.minWindow = { width: 1080, height: 700 };
      await page.setViewportSize({ width: 1080, height: 700 });
      await page.waitForTimeout(400);
      entry.narrow = await page.evaluate(measure);

      await page.setViewportSize({ width: 540, height: 700 });
      await page.waitForTimeout(400);
      entry.zoom200 = await page.evaluate(measure);

      await page.setViewportSize({ width: 320, height: 720 });
      await page.waitForTimeout(300);
      entry.belowSupported = await page.evaluate(measure);

      await page.setViewportSize({ width: 1440, height: 900 });
    } catch (err) {
      entry.error = String(err).slice(0, 200);
    }
    await context.close();

    // 深色主题单独取一次对比度（skills 要求明暗两套都量）。
    // **必须**用 localStorage + addInitScript 让应用自己以深色启动：
    // 先加载再 setAttribute 会被 ThemeBridge（读后端设置后 setTheme）覆盖回浅色，
    // 于是"深色那一轮"其实量的是浅色 —— 一份看起来全清的假报告。
    // 这里额外断言 data-theme 真的是 dark，测错主题就立刻失败。
    const darkCtx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await writeTheme('dark');
    await darkCtx.addInitScript(() => {
      try {
        window.localStorage.setItem('aiforge.theme', 'dark');
      } catch {
        /* 隐私模式等场景忽略 */
      }
    });
    const darkPage = await darkCtx.newPage();
    try {
      await darkPage.goto(`${baseUrl}/${route}`, { waitUntil: 'networkidle', timeout: 40000 });
      await darkPage.waitForTimeout(700);
      const applied = await darkPage.evaluate(() => document.documentElement.getAttribute('data-theme'));
      if (applied !== 'dark') {
        entry.dark = { error: `深色未生效（data-theme=${applied}）—— 这一轮结果不可信` };
        failures += 1;
        console.log(`  ${route.padEnd(14)} [ERROR] 深色主题没生效：data-theme=${applied}`);
      } else {
        const dark = await darkPage.evaluate(auditPage, TYPE_SCALE);
        entry.dark = { contrast: dark.contrast, inspected: dark.inspected };
      }
    } catch (err) {
      entry.dark = { error: String(err).slice(0, 120) };
    }
    await darkCtx.close();

    // 深色那一轮量完了：切回浅色，免得影响紧接着的 200% 缩放轮次
    await writeTheme(originalTheme || 'light');

    // 200% 缩放且更宽窗口（1440/2 = 720）
    const zoomCtx = await browser.newContext({ viewport: { width: 720, height: 450 }, deviceScaleFactor: 2 });
    const zoomPage = await zoomCtx.newPage();
    try {
      await zoomPage.goto(`${baseUrl}/${route}`, { waitUntil: 'networkidle', timeout: 40000 });
      await zoomPage.waitForTimeout(500);
      entry.zoom = await zoomPage.evaluate(() => {
        const doc = document.documentElement;
        return { overflow: doc.scrollWidth - doc.clientWidth };
      });
    } catch (err) {
      entry.zoom = { error: String(err).slice(0, 120) };
    }
    await zoomCtx.close();

    const reduceCtx = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' });
    const reducePage = await reduceCtx.newPage();
    try {
      await reducePage.goto(`${baseUrl}/${route}`, { waitUntil: 'networkidle', timeout: 40000 });
      await reducePage.waitForTimeout(600);
      entry.reduced = await reducePage.evaluate(() => {
        const long = [];
        for (const el of document.querySelectorAll('body *')) {
          const s = getComputedStyle(el);
          const worst = Math.max(
            0,
            ...s.transitionDuration.split(',').map((d) => parseFloat(d) * (d.includes('ms') ? 1 : 1000)),
            ...s.animationDuration.split(',').map((d) => parseFloat(d) * (d.includes('ms') ? 1 : 1000))
          );
          if (worst > 5) {
            const raw = typeof el.className === 'string' ? el.className : '';
            const cls = raw ? '.' + raw.trim().split(/\s+/).slice(0, 2).join('.') : '';
            long.push(el.tagName.toLowerCase() + cls + ' ' + worst + 'ms');
          }
        }
        return { offenders: Array.from(new Set(long)).slice(0, 8) };
      });
    } catch (err) {
      entry.reduced = { error: String(err).slice(0, 120) };
    }
    await reduceCtx.close();

    report.routes[route] = entry;

    // 测不到就大声说出来。第一版这里用 `(n.contrast || [])` 把「脚本自己报错」
    // 印成了一排 0 —— skills 把这种现象叫静默的覆盖缺口：一份什么都没查的报告
    // 看起来和一份全清的报告一模一样。
    const n = entry.normal;
    if (entry.error || !n) {
      failures += 1;
      console.log(`  ${route.padEnd(14)} [ERROR] ${entry.error || '未取到审计结果'}`);
      continue;
    }
    const counts = {
      contrast: (n.contrast || []).length,
      darkContrast: ((entry.dark || {}).contrast || []).length,
      sizes: (n.sizes || []).length,
      names: (n.name || []).length,
      transitions: (n.transitions || []).length,
      nums: (n.nums || []).length,
      colorOnly: (n.colorOnly || []).length,
      noFocus: (entry.tabbed || []).filter((t) => !t.hasIndicator).length,
      narrow: entry.narrow ? entry.narrow.overflow : -1,
      zoom200: entry.zoom200 ? entry.zoom200.overflow : -1,
      reduced: ((entry.reduced || {}).offenders || []).length,
    };
    failures +=
      counts.contrast + counts.darkContrast + counts.sizes + counts.names + counts.transitions +
      counts.nums + counts.colorOnly + counts.noFocus + counts.reduced +
      (counts.narrow > 1 ? 1 : 0) + (counts.zoom200 > 1 ? 1 : 0);
    console.log(
      `  ${route.padEnd(14)} 对比度 ${counts.contrast}(浅)/${counts.darkContrast}(深)，字号越界 ${counts.sizes}，` +
        `缺名字 ${counts.names}，过渡 ${counts.transitions}，tabular ${counts.nums}/${n.inspected.numbers}，` +
        `仅颜色 ${counts.colorOnly}/${n.inspected.colorOnly}，` +
        `无焦点 ${counts.noFocus}/${(entry.tabbed || []).length}，` +
        `1080px 溢出 ${counts.narrow < 0 ? 'N/A' : counts.narrow + 'px'}，` +
        `200%(540) 溢出 ${counts.zoom200 < 0 ? 'N/A' : counts.zoom200 + 'px'}，` +
        `h1=${n.structure.h1}，main=${n.structure.main}，skip=${n.structure.skipLink ? 'Y' : 'N'}，` +
        `reduced 未收敛 ${counts.reduced}，候选取样 ${n.inspected.contrast}/${n.inspected.interactive}`
    );
  }

  // ---- 主题切换必须"啪"地切过去，不能整页拖影（better-ui）----
  // 只验证机制不验证"感觉"：换肤时 <head> 里必须**出现**那条
  // `transition:none !important` 规则，并在下一帧被移除。
  // 用 MutationObserver 抓，而不是靠肉眼看截图。
  {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();
    try {
      await page.goto(`${baseUrl}/#/settings`, { waitUntil: 'networkidle', timeout: 40000 });
      await page.waitForTimeout(600);
      await page.evaluate(() => {
        window.__swapLog = [];
        const observer = new MutationObserver((records) => {
          for (const record of records) {
            for (const node of Array.from(record.addedNodes)) {
              if (node.nodeType === 1 && node.tagName === 'STYLE' && /transition:\s*none/i.test(node.textContent || '')) {
                window.__swapLog.push('added');
              }
            }
            for (const node of Array.from(record.removedNodes)) {
              if (node.nodeType === 1 && node.tagName === 'STYLE') {
                window.__swapLog.push('removed');
              }
            }
          }
        });
        observer.observe(document.head, { childList: true });
        window.__swapObserver = observer;
      });
      // 点「深色」/「浅色」触发真正的 setTheme（走 useTheme.apply）
      const themeButtons = page.locator('.seg button');
      const count = await themeButtons.count();
      let clicked = false;
      for (let i = 0; i < count; i += 1) {
        const text = (await themeButtons.nth(i).innerText()).trim();
        if (text === '深色' || text === '浅色') {
          await themeButtons.nth(i).click();
          clicked = true;
          break;
        }
      }
      await page.waitForTimeout(400);
      const log = await page.evaluate(() => window.__swapLog || []);
      const applied = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
      report.themeSwap = { clicked, log, applied, ok: clicked && log.includes('added') };
      if (!report.themeSwap.ok) failures += 1;
      console.log(
        `  主题切换  clicked=${clicked} 抑制规则=${log.includes('added') ? '已注入' : '未注入'} ` +
          `清理=${log.includes('removed') ? '已移除' : '仍残留'} data-theme=${applied}`
      );
    } catch (err) {
      report.themeSwap = { error: String(err).slice(0, 160) };
      failures += 1;
      console.log(`  主题切换  [ERROR] ${String(err).slice(0, 120)}`);
    }
    await context.close();
  }

  await browser.close();

  // 评审不留副作用：把主题恢复成进来时的样子
  // （主题切换那一项会点「深色/浅色」，确实会改到后端设置）
  if (originalTheme) {
    const restored = await writeTheme(originalTheme);
    console.log(`\n主题已恢复为 ${originalTheme}：${restored ? 'OK' : '恢复失败，请手动改回'}`);
  }
  const dir = resolve(root, 'build', 'work');
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
  const path = resolve(dir, 'interface-audit.json');
  writeFileSync(path, JSON.stringify(report, null, 2), 'utf8');
  console.log(`\n明细：${path}`);
  console.log(failures === 0 ? 'INTERFACE_AUDIT_OK' : `INTERFACE_AUDIT_FINDINGS(${failures})`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
