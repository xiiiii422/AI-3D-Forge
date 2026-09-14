/**
 * AI 3D Forge —— 打印机功能专项验收（契约 §3A）。
 *
 * 前置：真实后端已启动（webui/dist 已构建），默认 http://127.0.0.1:8795
 * 运行：node scripts/verify-printers.mjs [baseUrl]
 *
 * 覆盖：
 *   1. 工作台机型胶囊显示当前机型
 *   2. 选择器列出 >= 10 台机型、规格摘要正确、搜索可用
 *   3. 多材料机型会先让用户选材料；选择后写回后端并刷新页面依然生效
 *   4. 切换后的提示 toast 说明了影响
 *   5. 设置页「打印机」显示当前机型与 checklist（人话版设计规则）
 *   6. 新建/编辑表单里改喷嘴直径 → 实时预览的最小壁厚跟着变（0.4→1.2mm，0.8→2.4mm）
 *   7. 表单可回车提交；自定义机型可删除
 *   8. 欢迎向导仍是 4 步，且第 4 步能选机型
 *   9. 结果卡片显示机型 + 超限/薄壁警告（用 CDP 拦截 /api/jobs 注入一份结果）
 *  10. 已有模型时换机 → 弹出「按新机型重新生成」提醒
 *  11. 全程无 console 错误 / 未捕获异常
 *
 * 产出：scripts/shots/printer-*.png + scripts/shots/printers-report.json
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { connectPage, launchBrowser, sleep } from './cdp.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const SHOTS = join(HERE, 'shots');
const BASE = (process.argv[2] || process.env.BASE_URL || 'http://127.0.0.1:8795').replace(/\/$/, '');
const PORT = Number(process.env.CDP_PORT || 9341);

const JOB_ID = 'verify-printer-job';
const FAKE_PRINTER = {
  id: 'bambu-x1c-04',
  name: 'Bambu Lab X1 Carbon（0.4mm）',
  manufacturer: 'Bambu Lab',
  nozzle_mm: 0.4,
  material: 'PETG',
  build_volume_mm: { x: 256, y: 256, z: 256 },
  enclosure: true,
  is_resin: false,
  constraints: {
    min_wall_mm: 1.2,
    min_hole_dia_mm: 1.2,
    min_hole_dia_vertical_mm: 0.68,
    min_protrusion_mm: 0.6,
    clearance_mm: 0.24,
    max_layer_mm: 0.32,
    default_layer_mm: 0.2,
    max_horizontal_span_mm: 45,
    max_overhang_deg: 60,
    max_build_x: 256,
    max_build_y: 256,
    max_build_z: 256,
    max_build_diagonal_mm: 362,
    fn: 74,
    rough_min_dim_mm: 0.8,
    notes: ['喷嘴 0.4mm：竖直外墙建议不小于 1.2mm（3 倍喷嘴，层间强度才够）。'],
  },
  checklist: [
    '壁厚不小于 1.2mm',
    '孔径不小于 1.2mm（竖孔 0.7mm）',
    '成型范围 256×256×256mm',
  ],
};

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
    return detail;
  } catch (err) {
    record(name, false, err instanceof Error ? err.message : String(err));
    return null;
  }
}

const textOf = (selector) =>
  `(document.querySelector(${JSON.stringify(selector)})?.textContent || '').trim()`;

/** 下拉框赋值：React 对 select 监听的是 change 事件 */
async function setSelect(page, selector, value) {
  const applied = await page.evaluate(`(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) throw new Error('找不到下拉框 ${selector}');
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(
      el, ${JSON.stringify(String(value))}
    );
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return el.value;
  })()`);
  await sleep(150);
  return applied;
}

/** 读取实时预览里「壁厚不小于 X mm」这一行 */
const MIN_WALL_EXPR = `(() => {
  const lines = [...document.querySelectorAll('.printer-preview .rule-list li')].map(li => li.textContent.trim());
  const hit = lines.find(t => t.includes('壁厚不小于'));
  return hit || '';
})()`;

/** 等待所有 toast 消失：toast 是 fixed 定位，会挡住底部输入框的点击 */
/** 弹窗内部的布局体检（layout-audit.mjs 只跑路由，不会打开弹窗） */
const MODAL_AUDIT = `(() => {
  const dialogs = [...document.querySelectorAll('[role="dialog"]')];
  const top = dialogs[dialogs.length - 1];
  if (!top) return { problems: ['没有打开任何弹窗'], box: null };
  const problems = [];
  const vw = window.innerWidth, vh = window.innerHeight;
  const describe = (el) => el.tagName.toLowerCase() + '.' +
    (typeof el.className === 'string' ? el.className.trim().split(/\\s+/).slice(0, 2).join('.') : '');
  const box = top.getBoundingClientRect();
  if (box.right > vw + 2 || box.left < -2) problems.push('弹窗横向超出视口');
  if (box.bottom > vh + 2 || box.top < -2) problems.push('弹窗纵向超出视口');

  for (const el of top.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity) === 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const hasText = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length > 0);
    if (hasText && el.scrollWidth > el.clientWidth + 1) {
      if (cs.overflowX === 'visible' || cs.textOverflow !== 'ellipsis') {
        problems.push('文字横向溢出 ' + describe(el) + ' ' + el.scrollWidth + '>' + el.clientWidth);
      }
    }
    if (r.right > vw + 2 || r.left < -2) {
      problems.push('元素横向越界 ' + describe(el) + ' x=' + Math.round(r.left));
    }
    if (el.tagName === 'BUTTON') {
      const name = el.getAttribute('aria-label') || el.getAttribute('title') || (el.textContent || '').trim();
      if (!name) problems.push('按钮没有可读名称 ' + describe(el));
      if (r.height < 24 || r.width < 24) {
        problems.push('点击目标过小 ' + describe(el) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
      }
    }
  }
  return {
    problems: [...new Set(problems)].slice(0, 8),
    box: [Math.round(box.left), Math.round(box.top), Math.round(box.width), Math.round(box.height)],
    vw, vh,
  };
})()`;

/**
 * 文字重叠检测：两两比较「含文字的叶子元素」的矩形。
 * 正常布局里文字块不会互相压住，这条能精确证伪「看起来像重叠」的误报。
 */
const OVERLAP_SCRIPT = (rootSelector) => `(() => {
  const root = document.querySelector(${JSON.stringify(rootSelector)});
  if (!root) return { problems: ['找不到 ' + ${JSON.stringify(rootSelector)}], pairs: 0 };
  const describe = (el) => el.tagName.toLowerCase() + '.' +
    (typeof el.className === 'string' ? el.className.trim().split(/\\s+/).slice(0, 2).join('.') : '');
  const leaves = [...root.querySelectorAll('*')].filter((el) => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity) === 0) return false;
    if (![...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim().length > 0)) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  const problems = [];
  let pairs = 0;
  for (let i = 0; i < leaves.length; i += 1) {
    for (let j = i + 1; j < leaves.length; j += 1) {
      const a = leaves[i];
      const b = leaves[j];
      if (a.contains(b) || b.contains(a)) continue;
      const ra = a.getBoundingClientRect();
      const rb = b.getBoundingClientRect();
      const ox = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
      const oy = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
      if (ox > 4 && oy > 4) {
        pairs += 1;
        problems.push(describe(a) + ' × ' + describe(b) + ' 重叠 ' + Math.round(ox) + '×' + Math.round(oy));
      }
    }
  }
  return { problems: [...new Set(problems)].slice(0, 6), pairs, leaves: leaves.length };
})()`;

async function waitForNoToast(page) {
  const deadline = Date.now() + 12000;
  for (;;) {
    const count = await page.evaluate(`document.querySelectorAll('.toast').length`);
    if (count === 0) return;
    if (Date.now() > deadline) throw new Error('toast 一直没消失');
    await sleep(300);
  }
}

/** 最上层弹窗的底部按钮（管理机型弹窗与确认弹窗会同时存在于 DOM 里） */
const TOP_DIALOG_BUTTONS = `(() => {
  const dialogs = [...document.querySelectorAll('[role="dialog"]')];
  const top = dialogs[dialogs.length - 1];
  return top ? [...top.querySelectorAll('.modal-foot button')].map(b => b.textContent.trim()) : [];
})()`;

/** 点击最上层弹窗底部的最后一个按钮（= 主操作），并校验它的文案 */
async function clickTopDialogConfirm(page, expectText) {
  const finder = async () =>
    page.evaluate(`(() => {
      const dialogs = [...document.querySelectorAll('[role="dialog"]')];
      const top = dialogs[dialogs.length - 1];
      if (!top) return null;
      const btns = [...top.querySelectorAll('.modal-foot button')];
      const btn = btns[btns.length - 1];
      if (!btn) return null;
      const m = 'cdpbtn' + Math.random().toString(36).slice(2, 8);
      btn.setAttribute('data-cdp-mark', m);
      const text = (btn.textContent || '').trim();
      if (text !== ${JSON.stringify(expectText)}) return null;
      return '[data-cdp-mark="' + m + '"]';
    })()`);
  const selector = await finder();
  if (!selector) {
    throw new Error(`最上层弹窗没有「${expectText}」按钮（实际：${(await page.evaluate(TOP_DIALOG_BUTTONS)).join('/')}）`);
  }
  await page.click(selector, { finder });
}

async function main() {
  mkdirSync(SHOTS, { recursive: true });

  // 后端必须在线
  const health = await fetch(`${BASE}/api/app/health`).then((r) => r.json()).catch(() => null);
  if (!health?.ok) {
    console.error(`后端未就绪：${BASE} —— 请先启动 main.py --server`);
    process.exit(2);
  }

  // 需要一个真实项目来承载「结果卡片」与「换机提醒」
  const projects = await fetch(`${BASE}/api/projects`).then((r) => r.json());
  let projectId = projects[0]?.id;
  if (!projectId) {
    const created = await fetch(`${BASE}/api/projects`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: '打印机验证项目' }),
    }).then((r) => r.json());
    projectId = created.id;
  }

  // 清理上一次运行留下的自定义机型，保证断言与初始状态一致
  const before = await fetch(`${BASE}/api/printers`).then((r) => r.json());
  for (const p of before.printers.filter((x) => !x.builtin && x.name.includes('验证用机型'))) {
    await fetch(`${BASE}/api/printers/${encodeURIComponent(p.id)}`, { method: 'DELETE' });
    console.log(`  · 已清理上次遗留的自定义机型 ${p.id}`);
  }
  // 恢复默认机型，避免上次运行残留导致首屏断言不稳
  await fetch(`${BASE}/api/printers/select`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ printer_id: before.default_id }),
  });

  const browser = await launchBrowser({ port: PORT, width: 1280, height: 800 });
  const page = await connectPage(browser.port, 'about:blank');
  const problems = page.collectProblems();
  await page.setViewport(1280, 800);

  const shot = (name) => page.screenshot(join(SHOTS, `printer-${name}.png`));

  /* ---------- 注入一份「已完成」的作业，用来验证结果卡片 ---------- */
  let injected = false;
  const fakeJob = {
    job_id: JOB_ID,
    project_id: projectId,
    revision: 1,
    status: 'done',
    stage: 'done',
    percent: 100,
    messages: [{ seq: 1, text: '已完成', detail: null }],
    error: null,
    result: {
      project_id: projectId,
      revision: 1,
      name: '验证用手机支架',
      scad: 'cube([10,10,10]);',
      params: [],
      summary: '这是一个用于验证结果卡片渲染的模型说明。',
      printer: FAKE_PRINTER,
      stl: {
        url: `/api/projects/${projectId}/stl/raw`,
        filename: 'verify.stl',
        size_bytes: 20480,
        triangles: 1200,
        volume_mm3: 24000,
        bbox_mm: { x: 300, y: 120, z: 80 },
        watertight: true,
        watertight_ratio: 1,
        material: 'PETG',
        material_density: 1.27,
        within_build_volume: false,
        min_dimension_mm: 0.4,
        meets_min_wall: false,
      },
      printability: {
        score: 62,
        verdict: 'warn',
        summary: '整体结构合理，但有两处需要调整。',
        issues: [
          {
            severity: 'error',
            title: '模型超出该机型的成型范围，需要缩小或分件后再打印',
            detail:
              'X 方向 300mm 超过 Bambu Lab X1 Carbon（0.4mm）的 256mm 成型范围，直接切片会无法放入打印区域，' +
              '请把模型整体缩放到 250mm 以内，或者沿 X 方向拆成两件分别打印后用胶水或螺丝拼装。',
            suggestion: '整体缩放到 85%，或沿 X 方向拆成两件。',
          },
        ],
        metrics_note: '几何数据来自 STL 网格分析。',
      },
      diagnostics: null,
    },
    created_at: new Date().toISOString(),
    finished_at: new Date().toISOString(),
  };

  const fulfillJson = (requestId, code, payload) =>
    page.send('Fetch.fulfillRequest', {
      requestId,
      responseCode: code,
      responseHeaders: [{ name: 'Content-Type', value: 'application/json; charset=utf-8' }],
      body: Buffer.from(JSON.stringify(payload), 'utf8').toString('base64'),
    });

  await page.send('Fetch.enable', {
    patterns: [{ urlPattern: '*/api/jobs*', requestStage: 'Request' }],
  });
  page.on('Fetch.requestPaused', (p) => {
    const url = p.request.url;
    const method = p.request.method;
    void (async () => {
      try {
        if (method === 'POST' && /\/api\/jobs$/.test(url)) {
          injected = true;
          await fulfillJson(p.requestId, 202, {
            job_id: JOB_ID,
            project_id: projectId,
            revision: 1,
          });
          return;
        }
        if (method === 'GET' && url.includes(`/api/jobs/${JOB_ID}`)) {
          await fulfillJson(p.requestId, 200, fakeJob);
          return;
        }
      } catch {
        /* 落到 continue */
      }
      await page.send('Fetch.continueRequest', { requestId: p.requestId }).catch(() => {});
    })();
  });

  try {
    /* ================= 1. 工作台机型胶囊 ================= */
    await page.goto(`${BASE}/#/studio`);
    await page.waitForFunction(`!!document.querySelector('.studio')`, { label: '工作台' });

    await step('工作台顶栏出现机型胶囊，且显示真实机型名', async () => {
      await page.waitForFunction(
        `(() => { const t = ${textOf('.printer-chip-text')}; return !!t && !t.includes('正在读取'); })()`,
        { label: '机型胶囊', timeoutMs: 15000 },
      );
      const chip = await page.evaluate(textOf('.printer-chip-text'));
      const hasNozzleIcon = await page.evaluate(`!!document.querySelector('.printer-chip svg')`);
      const label = await page.evaluate(
        `document.querySelector('.printer-chip')?.getAttribute('aria-label')`,
      );
      if (!chip.includes('·')) throw new Error(`胶囊未带材料：${chip}`);
      if (!hasNozzleIcon) throw new Error('胶囊缺少喷嘴图标');
      if (!label?.includes('当前打印机')) throw new Error(`缺少 aria-label：${label}`);
      await shot('01-chip');
      return `胶囊「${chip}」`;
    });

    /* ================= 2. 选择器：列表 / 分组 / 搜索 ================= */
    await step('点击胶囊打开选择器，列出 >= 10 台机型且规格摘要正确', async () => {
      await page.click('.printer-chip');
      await page.waitForFunction(`document.querySelectorAll('.printer-row').length >= 10`, {
        label: '机型列表',
        timeoutMs: 15000,
      });
      const info = await page.evaluate(`(() => {
        const rows = [...document.querySelectorAll('.printer-row')];
        return {
          count: rows.length,
          groups: [...document.querySelectorAll('.printer-group-title')].map(g => g.textContent.trim()),
          specs: rows.map(r => r.querySelector('.printer-row-spec').textContent.trim()),
          names: rows.map(r => r.querySelector('.printer-row-name').textContent.trim()),
          active: rows.filter(r => r.classList.contains('is-active')).length,
        };
      })()`);
      if (info.groups.length === 0) throw new Error('没有分组标题');
      if (!info.groups.some((g) => g.includes('内置机型'))) throw new Error('缺少「内置机型」分组');
      if (info.active !== 1) throw new Error(`当前机型标记数量异常：${info.active}`);
      const bad = info.specs.filter(
        (s) => !/(喷嘴|光固化)/.test(s) || !s.includes('×') || !/(封闭腔体|开放机身)/.test(s),
      );
      if (bad.length) throw new Error(`规格摘要格式不对：${bad.slice(0, 3).join(' / ')}`);
      await shot('02-picker');
      return `${info.count} 台机型，分组「${info.groups.join(' / ')}」，示例：${info.specs[1]}`;
    });

    await step('搜索框按品牌过滤', async () => {
      await page.setInputValue('.printer-search-input', 'Prusa');
      await sleep(400);
      const prusa = await page.evaluate(`document.querySelectorAll('.printer-row').length`);
      await page.setInputValue('.printer-search-input', '不存在的品牌');
      await sleep(400);
      const none = await page.evaluate(`document.querySelectorAll('.printer-row').length`);
      await page.setInputValue('.printer-search-input', '');
      await sleep(400);
      const all = await page.evaluate(`document.querySelectorAll('.printer-row').length`);
      if (prusa < 2 || none !== 0 || all < 10) {
        throw new Error(`过滤异常：Prusa=${prusa} 无结果=${none} 全部=${all}`);
      }
      return `Prusa 命中 ${prusa} 台，无结果 ${none} 台，清空后 ${all} 台`;
    });

    /* ================= 3. 多材料机型 → 材料子选择 ================= */
    let switchedChip = '';
    await step('选择多材料机型时先选材料，切换后 toast 说明影响', async () => {
      await page.clickText('Bambu Lab X1 Carbon', { exact: false });
      await page.waitForFunction(`document.querySelectorAll('.material-chip').length >= 3`, {
        label: '材料选择',
        timeoutMs: 10000,
      });
      const matInfo = await page.evaluate(`(() => {
        const chips = [...document.querySelectorAll('.material-chip')];
        return {
          count: chips.length,
          checked: chips.filter(c => c.getAttribute('aria-checked') === 'true').map(c => c.textContent.trim()),
          hasPetg: chips.some(c => c.textContent.includes('PETG')),
        };
      })()`);
      if (!matInfo.hasPetg) throw new Error('材料列表里没有 PETG');
      if (matInfo.checked.length !== 1) throw new Error('材料默认选中项不是唯一');
      await shot('03-material');
      // 选 PETG → 应用
      await page.clickText('PETG', { exact: false });
      await sleep(200);
      await page.clickText('使用 PETG');
      await page.waitForFunction(`!!document.querySelector('.toast')`, {
        label: '切换提示',
        timeoutMs: 15000,
      });
      await sleep(300);
      const toast = await page.evaluate(textOf('.toast'));
      const chip = await page.evaluate(textOf('.printer-chip-text'));
      const closed = await page.evaluate(`!document.querySelector('[role="dialog"]')`);
      if (!toast.includes('已切换到')) throw new Error(`toast 文案不对：${toast}`);
      if (!/最小壁厚\s*1\.2mm/.test(toast)) throw new Error(`toast 未说明最小壁厚：${toast}`);
      if (!toast.includes('已有模型不会自动改变')) throw new Error('toast 未说明旧模型不受影响');
      if (!chip.includes('PETG')) throw new Error(`胶囊未更新材料：${chip}`);
      if (!closed) throw new Error('选择后弹窗没有关闭');
      switchedChip = chip;
      await shot('04-switched');
      return `材料 ${matInfo.count} 项；toast「${toast.slice(0, 46)}…」；胶囊「${chip}」`;
    });

    await step('刷新页面后仍是刚才选的机型（后端已持久化）', async () => {
      await page.reload({ settleMs: 900 });
      await page.waitForFunction(
        `(${textOf('.printer-chip-text')}).includes('PETG')`,
        { label: '刷新后的机型', timeoutMs: 15000 },
      );
      const chip = await page.evaluate(textOf('.printer-chip-text'));
      record('刷新前后胶囊一致', chip === switchedChip, `${switchedChip} → ${chip}`);
      return chip;
    });

    /* ================= 4. 单材料机型：直接切换 ================= */
    await step('单材料机型无需二次选择', async () => {
      await page.click('.printer-chip');
      await page.waitForFunction(`document.querySelectorAll('.printer-row').length >= 10`, {
        label: '机型列表',
      });
      await page.clickText('光固化', { exact: false });
      await page.waitForFunction(
        `(${textOf('.printer-chip-text')}).includes('树脂')`,
        { label: '切换到光固化', timeoutMs: 15000 },
      );
      const chip = await page.evaluate(textOf('.printer-chip-text'));
      const stillOpen = await page.evaluate(`!!document.querySelector('.material-chip')`);
      if (stillOpen) throw new Error('单材料机型不应出现材料步骤');
      return chip;
    });

    /* ================= 5. 结果卡片（注入作业） ================= */
    await step('结果卡片显示机型 / 超限警告 / 薄壁警告', async () => {
      // 先切回 0.4mm 的 FDM 机型，保证卡片与注入结果对得上
      await page.click('.printer-chip');
      await page.waitForFunction(`document.querySelectorAll('.printer-row').length >= 10`, {
        label: '机型列表',
      });
      await page.clickText('通用 FDM 打印机', { exact: false });
      await sleep(300);
      await page.clickText('使用 PLA');
      await page.waitForFunction(`!document.querySelector('[role="dialog"]')`, { label: '弹窗关闭' });

      await waitForNoToast(page);
      await page.focus('.composer-box textarea');
      await page.type('验证结果卡片');
      await page.pressKey('Enter');
      await page.waitForFunction(`!!document.querySelector('.result')`, {
        label: '结果卡片',
        timeoutMs: 20000,
      });
      const info = await page.evaluate(`(() => ({
        printerLine: ${textOf('.result-printer')},
        warns: [...document.querySelectorAll('.warn-row')].map(w => w.textContent.replace(/\\s+/g, ' ').trim()),
        issues: document.querySelectorAll('.issue').length,
      }))()`);
      if (!info.printerLine.includes('Bambu Lab X1 Carbon')) {
        throw new Error(`结果卡片未显示机型：${info.printerLine}`);
      }
      if (!info.printerLine.includes('PETG')) throw new Error('结果卡片未显示材料');
      if (info.warns.length !== 2) throw new Error(`警告行应为 2 条，实际 ${info.warns.length}`);
      if (!info.warns[0].includes('超出该机型成型范围')) throw new Error('缺少超限警告');
      if (!info.warns[0].includes('300.0 > 256.0mm') && !info.warns[0].includes('300 > 256mm')) {
        throw new Error(`超限警告没有三轴对比：${info.warns[0]}`);
      }
      if (!info.warns[1].includes('低于该机型建议壁厚')) throw new Error('缺少薄壁警告');
      if (!info.warns[1].includes('1.2mm')) throw new Error('薄壁警告没有给出建议值');
      if (info.issues < 1) throw new Error('打印性建议列表为空');
      await shot('05-result-card');
      return `机型行「${info.printerLine}」；警告 ${info.warns.length} 条`;
    });

    await step('长文案可换行，结果卡片不横向溢出、无文字重叠', async () => {
      const res = await page.evaluate(`(() => {
        const card = document.querySelector('.result');
        const vw = window.innerWidth;
        const offenders = [];
        for (const el of card.querySelectorAll('*')) {
          const r = el.getBoundingClientRect();
          if (r.width > 0 && r.right > vw + 1) offenders.push(el.className || el.tagName);
          if (el.children.length === 0 && (el.textContent || '').trim()) {
            const cs = getComputedStyle(el);
            if (el.scrollWidth > el.clientWidth + 1 && cs.overflowX === 'visible') {
              offenders.push('text-overflow:' + (el.className || el.tagName));
            }
          }
        }
        const body = document.querySelector('.issue-body');
        return { offenders: [...new Set(offenders)].slice(0, 5), wrap: body ? getComputedStyle(body).overflowWrap : '' };
      })()`);
      if (res.offenders.length) throw new Error(`溢出：${res.offenders.join(' | ')}`);
      const overlap = await page.evaluate(OVERLAP_SCRIPT('.result'));
      if (overlap.problems.length) throw new Error(overlap.problems.join(' | '));
      return `无溢出；${overlap.leaves} 个文字块两两比对无重叠；issue-body overflow-wrap=${res.wrap}`;
    });

    /* ================= 6. 已有模型时换机 → 提醒 ================= */
    await step('已有模型时换机弹出「按新机型重新生成」提醒', async () => {
      await page.click('.printer-chip');
      await page.waitForFunction(`document.querySelectorAll('.printer-row').length >= 10`, {
        label: '机型列表',
      });
      await page.clickText('Creality Ender-3', { exact: false });
      await sleep(300);
      await page.clickText('使用 PLA');
      await page.waitForFunction(
        `!!document.querySelector('[role="dialog"]') && document.body.textContent.includes('按新机型重新生成')`,
        { label: '换机提醒', timeoutMs: 15000 },
      );
      const modal = await page.evaluate(`(() => {
        const dlg = document.querySelector('[role="dialog"]');
        return {
          text: dlg.textContent.replace(/\\s+/g, ' ').trim(),
          hasButton: [...dlg.querySelectorAll('button')].some(b => b.textContent.includes('按新机型重新生成')),
          listsPrinter: dlg.textContent.includes('Bambu Lab X1 Carbon'),
        };
      })()`);
      if (!modal.hasButton) throw new Error('提醒里没有重新生成按钮');
      if (!modal.listsPrinter) throw new Error(`提醒没有说明原机型：${modal.text.slice(0, 60)}`);
      await shot('06-switch-warning');
      await page.clickText('稍后再说');
      await page.waitForFunction(`!document.querySelector('[role="dialog"]')`, { label: '提醒关闭' });
      return modal.text.slice(0, 72);
    });

    /* ================= 7. 设置页 ================= */
    await step('设置页「打印机」是第一张卡片并显示 checklist', async () => {
      await page.goto(`${BASE}/#/settings`);
      await page.waitForFunction(`!!document.querySelector('.printer-current')`, {
        label: '打印机卡片',
        timeoutMs: 15000,
      });
      const info = await page.evaluate(`(() => ({
        firstHead: ${textOf('.card-head h2')},
        name: ${textOf('.printer-current-name')},
        spec: ${textOf('.printer-current-spec')},
        rules: [...document.querySelectorAll('.rule-list li')].map(li => li.textContent.trim()),
        buttons: [...document.querySelectorAll('.printer-current button')].map(b => b.textContent.trim()),
        knowledge: [...document.querySelectorAll('.acc-head')].some(h => h.textContent.includes('这些参数是怎么影响模型的')),
      }))()`);
      if (info.firstHead !== '打印机') throw new Error(`首个分区是「${info.firstHead}」`);
      if (!info.spec.includes('喷嘴') || !info.spec.includes('×')) {
        throw new Error(`机型信息不完整：${info.spec}`);
      }
      if (info.rules.length < 5) throw new Error(`checklist 只有 ${info.rules.length} 条`);
      if (!info.rules.some((r) => r.includes('壁厚不小于'))) throw new Error('checklist 缺少壁厚规则');
      if (!info.buttons.includes('更换打印机') || !info.buttons.includes('管理机型')) {
        throw new Error(`按钮缺失：${info.buttons.join('/')}`);
      }
      if (!info.knowledge) throw new Error('缺少「这些参数是怎么影响模型的？」折叠块');
      await shot('07-settings');
      return `${info.name}｜${info.rules.length} 条设计规则，如「${info.rules[0]}」`;
    });

    await step('知识库折叠块可展开并列出参数影响', async () => {
      await page.clickText('这些参数是怎么影响模型的？', {
        expect: `!!document.querySelector('.kv-list')`,
      });
      const rows = await page.evaluate(
        `[...document.querySelectorAll('.kv-list dt')].map(d => d.textContent.trim())`,
      );
      if (rows.length < 5) throw new Error(`只列出 ${rows.length} 项`);
      await shot('08-knowledge');
      return `${rows.length} 项：${rows.slice(0, 4).join(' / ')}`;
    });

    /* ================= 8. 机型管理 + 实时预览 ================= */
    await step('管理机型：内置/自定义徽标与新建入口', async () => {
      await page.clickText('管理机型');
      await page.waitForFunction(`document.querySelectorAll('.printer-manage-row').length >= 10`, {
        label: '机型管理列表',
        timeoutMs: 15000,
      });
      const info = await page.evaluate(`(() => {
        const rows = [...document.querySelectorAll('.printer-manage-row')];
        const builtin = rows.filter(r => r.querySelector('.tag') && r.querySelector('.tag').textContent.includes('内置'));
        const has = (r, text) => [...r.querySelectorAll('button')].some(b => b.textContent.includes(text));
        return {
          rows: rows.length,
          builtinBadges: builtin.length,
          builtinWithRemove: builtin.filter(r => has(r, '删除') || has(r, '恢复默认')).length,
          overridden: rows.filter(r => r.textContent.includes('已修改')).length,
          hasNew: [...document.querySelectorAll('button')].some(b => b.textContent.includes('新建机型')),
          hasReset: [...document.querySelectorAll('button')].some(b => b.textContent.includes('恢复内置机型库')),
          editButtons: rows.filter(r => has(r, '编辑')).length,
        };
      })()`);
      if (info.builtinBadges < 10) throw new Error('内置徽标缺失');
      if (!info.hasNew || !info.hasReset) throw new Error('缺少新建/恢复入口');
      if (info.builtinWithRemove !== info.overridden) {
        throw new Error(
          `只有「已修改」的内置机型才能恢复默认（已修改 ${info.overridden} 台，带按钮 ${info.builtinWithRemove} 台）`,
        );
      }
      if (info.editButtons !== info.rows) throw new Error('内置机型也应可以编辑');
      await shot('09-manage');
      return `${info.rows} 台，内置徽标 ${info.builtinBadges} 个，全部可编辑；未改过的内置机型没有删除按钮`;
    });

    await step('改内置机型 → 标为「已修改」并可「恢复默认」，恢复后回到出厂参数', async () => {
      const before = await fetch(`${BASE}/api/printers/bambu-a1-04`).then((r) => r.json());
      if (before.updated_at) throw new Error('起始状态不干净：该内置机型已是覆盖版本');

      const markInRow = (rowText, buttonText) => async () =>
        page.evaluate(`(() => {
          const row = [...document.querySelectorAll('.printer-manage-row')].find(r => r.textContent.includes(${JSON.stringify(
            rowText,
          )}));
          if (!row) return null;
          const btn = [...row.querySelectorAll('button')].find(b => b.textContent.trim() === ${JSON.stringify(
            buttonText,
          )});
          if (!btn) return null;
          const m = 'cdprow' + Math.random().toString(36).slice(2, 8);
          btn.setAttribute('data-cdp-mark', m);
          return '[data-cdp-mark="' + m + '"]';
        })()`);

      // 1) 编辑内置机型：喷嘴 0.4 → 0.6
      const editFinder = markInRow('Bambu Lab A1', '编辑');
      const editSelector = await editFinder();
      if (!editSelector) throw new Error('找不到 A1 的编辑按钮');
      await page.click(editSelector, { finder: editFinder });
      await page.waitForFunction(`!!document.querySelector('#pf-nozzle')`, { label: '编辑表单' });
      await setSelect(page, '#pf-nozzle', '0.6');
      await clickTopDialogConfirm(page, '保存');
      await page.waitForFunction(
        `[...document.querySelectorAll('.printer-manage-row')].some(r => r.textContent.includes('Bambu Lab A1') && r.textContent.includes('已修改'))`,
        { label: '已修改标记', timeoutMs: 20000 },
      );
      const afterSave = await fetch(`${BASE}/api/printers/bambu-a1-04`).then((r) => r.json());
      if (afterSave.nozzle_mm !== 0.6) throw new Error(`保存后喷嘴为 ${afterSave.nozzle_mm}`);
      if (afterSave.builtin !== true) throw new Error('覆盖版本应仍是内置机型（不可当自定义删除）');
      if (!afterSave.updated_at) throw new Error('覆盖版本没有 updated_at 标记');

      const badge = await page.evaluate(`(() => {
        const row = [...document.querySelectorAll('.printer-manage-row')].find(r => r.textContent.includes('Bambu Lab A1'));
        return {
          tags: [...row.querySelectorAll('.tag')].map(t => t.textContent.trim()),
          buttons: [...row.querySelectorAll('button')].map(b => b.textContent.trim()),
        };
      })()`);
      if (!badge.tags.includes('已修改')) throw new Error(`缺少「已修改」标记：${badge.tags.join('/')}`);
      if (!badge.buttons.includes('恢复默认')) throw new Error(`缺少「恢复默认」按钮：${badge.buttons.join('/')}`);
      if (badge.buttons.includes('删除')) throw new Error('内置机型的按钮不应叫「删除」');
      const tip = await page.evaluate(`(() => {
        const row = [...document.querySelectorAll('.printer-manage-row')].find(r => r.textContent.includes('Bambu Lab A1'));
        const btn = [...row.querySelectorAll('button')].find(b => b.textContent.trim() === '恢复默认');
        return btn.getAttribute('title') || '';
      })()`);
      if (!tip.includes('出厂')) throw new Error(`「恢复默认」缺少解释性 tooltip：${tip}`);
      await shot('11-builtin-override');

      // 2) 恢复默认 → 参数回到内置原值
      const restoreFinder = markInRow('Bambu Lab A1', '恢复默认');
      const restoreSelector = await restoreFinder();
      if (!restoreSelector) throw new Error('找不到「恢复默认」按钮');
      await page.click(restoreSelector, { finder: restoreFinder });
      await page.waitForFunction(`document.body.textContent.includes('恢复默认参数')`, {
        label: '恢复确认',
      });
      await clickTopDialogConfirm(page, '恢复默认');
      await page.waitForFunction(
        `![...document.querySelectorAll('.printer-manage-row')].some(r => r.textContent.includes('Bambu Lab A1') && r.textContent.includes('已修改'))`,
        { label: '恢复完成', timeoutMs: 20000 },
      );
      const restored = await fetch(`${BASE}/api/printers/bambu-a1-04`).then((r) => r.json());
      if (restored.nozzle_mm !== before.nozzle_mm) {
        throw new Error(`恢复后喷嘴为 ${restored.nozzle_mm}，应为 ${before.nozzle_mm}`);
      }
      if (restored.name !== before.name) throw new Error('恢复后名称没有回到出厂值');
      if (restored.updated_at !== '') throw new Error('恢复后仍带覆盖标记');
      return `0.4 → 0.6mm 保存成功并标为「已修改」，点「恢复默认」后回到 ${restored.nozzle_mm}mm／${restored.name}`;
    });

    await step('新建表单：喷嘴 0.4 → 0.8mm，实时预览最小壁厚 1.2 → 2.4mm', async () => {
      await page.clickText('新建机型');
      await page.waitForFunction(`!!document.querySelector('#pf-nozzle') && !!document.querySelector('.printer-preview')`, {
        label: '机型表单',
        timeoutMs: 15000,
      });
      // 等第一次预览返回
      await page.waitForFunction(`${MIN_WALL_EXPR}.includes('壁厚不小于')`, {
        label: '首次预览',
        timeoutMs: 15000,
      });
      const at04 = await page.evaluate(MIN_WALL_EXPR);

      await setSelect(page, '#pf-nozzle', '0.8');
      await page.waitForFunction(`${MIN_WALL_EXPR}.includes('2.4mm')`, {
        label: '0.8mm 预览',
        timeoutMs: 15000,
      });
      const at08 = await page.evaluate(MIN_WALL_EXPR);

      await setSelect(page, '#pf-nozzle', '0.4');
      await page.waitForFunction(`${MIN_WALL_EXPR}.includes('1.2mm')`, {
        label: '0.4mm 预览',
        timeoutMs: 15000,
      });
      const back = await page.evaluate(MIN_WALL_EXPR);

      // 成型尺寸也参与预览
      await page.setInputValue('#pf-x', '300');
      await page.waitForFunction(
        `[...document.querySelectorAll('.printer-preview .rule-list li')].some(li => li.textContent.includes('300×'))`,
        { label: '成型尺寸预览', timeoutMs: 15000 },
      );
      const span = await page.evaluate(
        `[...document.querySelectorAll('.printer-preview .rule-list li')].map(l => l.textContent.trim()).find(t => t.includes('成型范围'))`,
      );
      await page.setInputValue('#pf-x', '220');
      await shot('10-live-preview');
      if (at04 !== '壁厚不小于 1.2mm') throw new Error(`0.4mm 预览为「${at04}」`);
      if (at08 !== '壁厚不小于 2.4mm') throw new Error(`0.8mm 预览为「${at08}」`);
      if (back !== '壁厚不小于 1.2mm') throw new Error(`改回 0.4mm 后预览为「${back}」`);
      return `0.4mm → 「${at04}」，0.8mm → 「${at08}」，X=300 时「${span}」`;
    });

    await step('机型表单（超宽弹窗）内部无布局缺陷', async () => {
      const res = await page.evaluate(MODAL_AUDIT);
      if (res.problems.length) throw new Error(res.problems.join(' | '));
      const modal = await page.evaluate(OVERLAP_SCRIPT('[role="dialog"]'));
      if (modal.problems.length) throw new Error(`文字重叠：${modal.problems.join(' | ')}`);
      await shot('13-form-audit');
      return `弹窗 ${res.box[2]}×${res.box[3]}（视口 ${res.vw}×${res.vh}），${modal.leaves} 个文字块无重叠、无溢出、无空白按钮`;
    });

    await step('机型选择器（列表弹窗）内部无布局缺陷', async () => {
      // 关掉编辑表单：它会退回机型列表弹窗，所以再关一次
      await page.pressKey('Escape');
      await page.waitForFunction(`!document.querySelector('#pf-name')`, { label: '关闭表单' });
      await page.pressKey('Escape');
      await page.waitForFunction(`!document.querySelector('.printer-manage-row')`, {
        label: '关闭管理弹窗',
      });
      await sleep(200);
      // 从设置页的 [更换打印机] 打开同一个选择器（胶囊只在工作台顶栏出现）
      await page.clickText('更换打印机');
      await page.waitForFunction(`document.querySelectorAll('.printer-row').length >= 10`, {
        label: '选择器',
      });
      const res = await page.evaluate(MODAL_AUDIT);
      if (res.problems.length) throw new Error(res.problems.join(' | '));
      const overlap = await page.evaluate(OVERLAP_SCRIPT('[role="dialog"]'));
      if (overlap.problems.length) throw new Error(`文字重叠：${overlap.problems.join(' | ')}`);
      await shot('14-picker-audit');
      await page.pressKey('Escape');
      await page.waitForFunction(`!document.querySelector('.printer-row')`, { label: '关闭选择器' });
      return `弹窗 ${res.box[2]}×${res.box[3]}，${overlap.leaves} 个文字块无重叠、无溢出、无空白按钮`;
    });

    await step('回车提交表单创建自定义机型，随后可删除', async () => {
      await page.clickText('管理机型');
      await page.waitForFunction(`document.querySelectorAll('.printer-manage-row').length >= 10`, {
        label: '管理弹窗',
      });
      await page.clickText('新建机型');
      await page.waitForFunction(`!!document.querySelector('#pf-name')`, { label: '新建表单' });
      await page.focus('#pf-name');
      await page.type('验证用机型');
      // 没有提交按钮的隐式提交不可靠，这里回车应由表单的提交按钮接管
      await page.pressKey('Enter');
      await page.waitForFunction(
        `[...document.querySelectorAll('.printer-manage-row .printer-row-name')].some(n => n.textContent.includes('验证用机型'))`,
        { label: '新建的机型', timeoutMs: 20000 },
      );
      const badge = await page.evaluate(`(() => {
        const row = [...document.querySelectorAll('.printer-manage-row')].find(r => r.textContent.includes('验证用机型'));
        return row ? row.querySelector('.tag').textContent.trim() : '';
      })()`);
      if (badge !== '自定义') throw new Error(`新机型徽标为「${badge}」`);
      await shot('11-created');

      // 删除（仅自定义可删）——每次点击前重新定位按钮，避免 React 重渲染把节点换掉
      const markDelete = async () =>
        page.evaluate(`(() => {
          const row = [...document.querySelectorAll('.printer-manage-row')].find(r => r.textContent.includes('验证用机型'));
          if (!row) return null;
          const btn = [...row.querySelectorAll('button')].find(b => b.textContent.includes('删除'));
          if (!btn) return null;
          const m = 'cdpdel' + Math.random().toString(36).slice(2, 8);
          btn.setAttribute('data-cdp-mark', m);
          return '[data-cdp-mark="' + m + '"]';
        })()`);
      const delSelector = await markDelete();
      if (!delSelector) throw new Error('自定义机型没有删除按钮');
      await page.click(delSelector, { finder: markDelete });
      await page.waitForFunction(`document.body.textContent.includes('删除机型')`, { label: '删除确认' });
      await clickTopDialogConfirm(page, '删除');
      await page.waitForFunction(
        `![...document.querySelectorAll('.printer-manage-row')].some(n => n.textContent.includes('验证用机型'))`,
        { label: '删除完成', timeoutMs: 15000 },
      );
      return '已创建并删除「验证用机型」';
    });

    await step('Esc 关闭弹窗并把焦点还给触发按钮', async () => {
      // 收尾：先把当前还开着的弹窗关掉
      await page.pressKey('Escape');
      await page.waitForFunction(`!document.querySelector('[role="dialog"]')`, {
        label: '管理弹窗关闭',
      });
      await sleep(250);
      // 键盘路径：焦点放到触发按钮 → 回车打开 → Esc 关闭 → 焦点必须回到触发按钮
      const reopened = await page.evaluate(`(() => {
        const btn = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === '管理机型');
        if (!btn) return false;
        btn.focus();
        return document.activeElement === btn;
      })()`);
      if (!reopened) throw new Error('找不到或无法聚焦「管理机型」按钮');
      await page.pressKey('Enter');
      await page.waitForFunction(`document.querySelectorAll('.printer-manage-row').length >= 10`, {
        label: '回车打开管理弹窗',
      });
      const insideFocus = await page.evaluate(
        `(document.activeElement?.textContent || '').trim().slice(0, 10)`,
      );
      await page.pressKey('Escape');
      await page.waitForFunction(`!document.querySelector('[role="dialog"]')`, { label: '再次关闭' });
      await sleep(250);
      const after = await page.evaluate(`(document.activeElement?.textContent || '').trim()`);
      if (after !== '管理机型') throw new Error(`焦点没有还给触发按钮，而是「${after}」`);
      return `回车可打开弹窗（焦点先落在「${insideFocus}」），Esc 关闭后回到「${after}」`;
    });

    /* ================= 9. 欢迎向导 ================= */
    await step('向导仍是 4 步，第 4 步可「选打印机」', async () => {
      await page.goto(`${BASE}/#/welcome`);
      await page.waitForFunction(`document.querySelectorAll('.wizard-step').length === 4`, {
        label: '向导步骤',
      });
      const stepCount = await page.evaluate(`document.querySelectorAll('.wizard-step').length`);
      await page.clickText('开始配置');
      await sleep(600);
      await page.clickText('跳过向导');
      await page.waitForFunction(`document.body.textContent.includes('你的打印机')`, {
        label: '机型步骤',
        timeoutMs: 15000,
      });
      const info = await page.evaluate(`(() => ({
        options: document.querySelectorAll('.printer-option').length,
        labels: [...document.querySelectorAll('.printer-option-name')].map(n => n.textContent.trim()),
        hasOther: document.body.textContent.includes('其他 / 稍后设置'),
        hint: document.body.textContent.includes('选对机型，AI 才知道该给你设计多厚、多大'),
      }))()`);
      if (stepCount !== 4) throw new Error(`向导变成 ${stepCount} 步`);
      if (info.options < 6) throw new Error(`机型选项只有 ${info.options} 个`);
      if (!info.hasOther) throw new Error('缺少「其他 / 稍后设置」');
      if (!info.hint) throw new Error('缺少一行解释文案');
      await shot('12-wizard');
      return `4 步；${info.options} 个机型选项：${info.labels.slice(0, 3).join(' / ')}`;
    });

    await step('向导里选机型会写回后端并标记选中', async () => {
      await page.clickText('Original Prusa MK4', { exact: false });
      await page.waitForFunction(
        `(() => {
          const el = [...document.querySelectorAll('.printer-option')].find(o => o.textContent.includes('Original Prusa MK4'));
          return !!el && el.getAttribute('aria-checked') === 'true';
        })()`,
        { label: '机型选中', timeoutMs: 15000 },
      );
      const saved = await fetch(`${BASE}/api/printers/selected`).then((r) => r.json());
      if (saved.printer.id !== 'prusa-mk4-04') {
        throw new Error(`后端选中机型为 ${saved.printer.id}`);
      }
      // 收尾：恢复通用机型，避免影响后续人工查看
      await page.clickText('通用机型 220×220×250', { exact: false });
      await page.waitForFunction(
        `(() => {
          const el = [...document.querySelectorAll('.printer-option')].find(o => o.textContent.includes('通用机型 220×220×250'));
          return !!el && el.getAttribute('aria-checked') === 'true';
        })()`,
        { label: '恢复通用机型', timeoutMs: 15000 },
      );
      const finalPrinter = await fetch(`${BASE}/api/printers/selected`).then((r) => r.json());
      return `后端已记录 ${saved.printer.name}，随后恢复为 ${finalPrinter.printer.name}`;
    });

    await step('注入的作业确实走了 /api/jobs（拦截器生效）', async () => {
      if (!injected) throw new Error('没有拦截到 POST /api/jobs');
      return 'POST /api/jobs + GET /api/jobs/{id} 均已被拦截替换';
    });

    /* ================= 10. 控制台 ================= */
    await step('全程无 console 错误 / 未捕获异常', async () => {
      await sleep(400);
      const real = problems.console.filter((c) => !c.includes('favicon'));
      const failed = problems.failedRequests.filter((f) => !f.includes('favicon'));
      if (problems.pageErrors.length) throw new Error(problems.pageErrors.join(' | '));
      if (real.length) throw new Error(real.join(' | '));
      return `console 干净；失败请求 ${failed.length} 条${failed.length ? `：${failed.slice(0, 3).join(' | ')}` : ''}`;
    });
  } finally {
    page.close();
    await browser.kill();
  }

  const pass = steps.filter((s) => s.ok).length;
  console.log(`\n===== 打印机验收 ${pass}/${steps.length} =====`);
  if (failures.length) {
    console.log('失败项：');
    for (const f of failures) console.log(`  - ${f}`);
  }
  writeFileSync(
    join(SHOTS, 'printers-report.json'),
    JSON.stringify({ pass, total: steps.length, steps, problems, base: BASE }, null, 2),
    'utf8',
  );
  console.log(`截图与报告: ${SHOTS}`);
  process.exit(failures.length ? 1 : 0);
}

main().catch((err) => {
  console.error('验收脚本崩溃：', err);
  process.exit(2);
});
