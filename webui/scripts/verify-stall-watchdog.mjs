/**
 * 卡死看门狗：既要**真的停掉作业**，也不能对着「等用户确认」谎报卡死。
 *
 * 为什么要单独验：看门狗是「上游无论怎么坏，界面都不能变砖头」的最后一道底线，
 * 而它以前有两个漏洞，都是**只解锁界面、不动服务端**引起的：
 *
 *   1. 它只做了 `setJobId(null)`，却对用户说「已经为你停止」——
 *      服务端的作业还挂在 running 上。于是那句话是假的，而且那个项目被幽灵作业
 *      一直占着（后端会回 409 BUSY），用户接下来每一条需求都发不出去。
 *   2. 用户停在规格卡片上思考时，它照样会判「卡死」并谎报 ——
 *      人家只是去接杯水，作业完全正常。
 *
 * 这条测试**不调用 AI**：只打桩三个接口
 *   * `POST /api/jobs`          → 返回一个固定 job_id
 *   * `GET  /api/jobs/<id>`     → 永远返回「运行中、进度不变」（上游卡住的样子）
 *   * `GET  /api/jobs/<id>/stream` → 立刻断开（服务端不再推事件）
 *   * `POST /api/jobs/<id>/cancel` → 数一下到底有没有人来取消
 * 其余请求走真实后端。
 *
 * 看门狗窗口在生产代码里是 120 秒，测试通过 `window.__AIFORGE_STALL_MS`
 * 注入 2 秒 —— 否则这条「最要紧的路径」会永远没人测。
 *
 * 前置：真实后端（它会自己托管 webui/dist，不需要另起前端）
 *   python -m uvicorn aiforge.app:create_app --factory --host 127.0.0.1 --port 8892 --app-dir src
 *
 * 用法：node webui/scripts/verify-stall-watchdog.mjs http://127.0.0.1:8892
 */

import { chromium } from 'playwright';

const base = (process.argv[2] || 'http://127.0.0.1:8892').replace(/\/$/, '');
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

let failures = 0;
const step = (name, ok, detail = '') => {
  if (!ok) failures += 1;
  console.log(`  ${ok ? '[OK]  ' : '[FAIL]'} ${name}` + (detail ? ` —— ${detail}` : ''));
};

page.on('pageerror', (e) => step('页面无脚本异常', false, e.message.slice(0, 160)));

// 看门狗窗口缩到 2 秒（生产 120 秒）。这是生产代码里显式留的测试注入点。
await page.addInitScript(() => {
  window.__AIFORGE_STALL_MS = 2000;
});

const STALL_JOB = 'stall-probe-job';
const PARKED_JOB = 'parked-probe-job';
const SPEC_ITEM = (name, label, given) => ({
  name,
  label,
  value: given ? 100 : null,
  unit: 'mm',
  min: 20,
  max: 300,
  user_specified: given,
  display: `${label}：${given ? 100 : '未指定'}mm${given ? '  ← 你指定' : ''}`,
});
const SPEC = {
  kind: 'phone_stand',
  summary: '一个带背部支撑的桌面手机支架',
  specs: [SPEC_ITEM('height', '支架高度', true), SPEC_ITEM('thickness', '底座厚度', false)],
  features: ['背部挡板'],
  prohibitions: ['不要镂空底座'],
  acceptance: ['能稳定支撑 6 英寸手机'],
  open_questions: ['壁厚没指定，我按你的机型取了 1.6mm'],
  clarifications: [],
  fingerprint: 'stallprobe01',
};

/** 本轮要让后端扮演哪种作业：'running'（卡住）还是 'parked'（等确认） */
let phase = 'running';
const cancelHits = { running: 0, parked: 0 };

const jobPayload = (jobId) =>
  phase === 'running'
    ? {
        job_id: jobId,
        project_id: 'stall-probe-project',
        revision: 1,
        mode: 'create',
        status: 'running',
        stage: 'generate',
        percent: 40,
        // 进度、阶段、消息都不变 —— 正是「卡住」的定义
        messages: [{ seq: 1, text: '正在设计模型…', detail: null, kind: 'info' }],
        error: null,
        result: null,
        spec: null,
        created_at: new Date().toISOString(),
        finished_at: null,
      }
    : {
        job_id: jobId,
        project_id: 'stall-probe-project',
        revision: 1,
        mode: 'create',
        status: 'awaiting_confirmation',
        stage: 'awaiting_confirmation',
        percent: null,
        messages: [{ seq: 1, text: '等待你确认我对需求的理解', detail: null, kind: 'info' }],
        error: null,
        result: null,
        spec: { spec: SPEC, awaiting_confirmation: true },
        created_at: new Date().toISOString(),
        finished_at: null,
      };

await page.route('**/api/jobs', async (route) => {
  if (route.request().method() !== 'POST') return route.continue();
  await route.fulfill({
    status: 202,
    contentType: 'application/json',
    body: JSON.stringify({
      job_id: phase === 'running' ? STALL_JOB : PARKED_JOB,
      project_id: 'stall-probe-project',
      revision: 1,
    }),
  });
});
await page.route('**/api/jobs/*/stream', async (route) => {
  // 服务端不再推事件：界面只能靠快照维持「运行中」——上游卡住的样子
  await route.fulfill({ status: 200, contentType: 'text/event-stream', body: ': keep-alive\n\n' });
});
await page.route('**/api/jobs/*/cancel', async (route) => {
  cancelHits[phase] += 1;
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ ok: true }),
  });
});
await page.route('**/api/jobs/*', async (route) => {
  const url = route.request().url();
  if (url.includes('/stream') || url.includes('/cancel')) return route.continue();
  const jobId = url.split('/api/jobs/')[1].split('?')[0];
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(jobPayload(jobId)),
  });
});

const composer = () => page.locator('textarea').first();
const sendBtn = () => page.locator('.composer-send');

await page.goto(`${base}/#/studio`, { waitUntil: 'networkidle', timeout: 40000 });
await page.waitForTimeout(800);

/* ---------------- 一、真的卡住 → 必须解锁并真的取消 ---------------- */
await composer().fill('这一条会卡住，看门狗必须真的停掉它');
await composer().press('Enter');

const started = Date.now();
while (Date.now() - started < 25000 && cancelHits.running === 0) {
  await page.waitForTimeout(300);
}
step(
  '看门狗真的调用了取消接口（不是只解锁界面）',
  cancelHits.running === 1,
  `cancel 请求 ${cancelHits.running} 次，耗时 ${((Date.now() - started) / 1000).toFixed(1)}s`,
);

// 判断「解锁」必须先填字：输入框为空时发送键本来就禁用
await composer().fill('卡死之后我还能不能再发一条');
await page.waitForTimeout(300);
step('卡死之后发送键恢复可点', await sendBtn().isEnabled().catch(() => false));

const text = (await page.locator('body').innerText().catch(() => '')).replace(/\s+/g, ' ');
step('给出了可读的说明', /长时间没有任何进展/.test(text), text.slice(-120));

/* ---------------- 二、停在确认卡片上 → 不许谎报卡死 ---------------- */
phase = 'parked';
await page.reload({ waitUntil: 'networkidle', timeout: 40000 });
await page.waitForTimeout(600);
await composer().fill('设计一个桌面手机支架，高度100mm');
await composer().press('Enter');
await page.waitForSelector('.spec-card', { timeout: 20000 }).catch(() => undefined);
const cardVisible = await page.locator('.spec-card').isVisible().catch(() => false);
step('确认卡片渲染出来了（前提）', cardVisible);

// 等得比「窗口 + 轮询节拍」久得多：2s 窗口 + 5s 节拍 = 7s，这里给 10s
await page.waitForTimeout(10000);
step(
  '等用户确认不算卡死（没有谎报、也没有取消作业）',
  cancelHits.parked === 0,
  `cancel 请求 ${cancelHits.parked} 次`,
);
const parkedText = (await page.locator('body').innerText().catch(() => '')).replace(/\s+/g, ' ');
step(
  '确认卡片还在（没有被看门狗清掉）',
  await page.locator('.spec-card').isVisible().catch(() => false),
  parkedText.slice(-120),
);

await page.screenshot({ path: 'build/work/stall-watchdog.png' }).catch(() => {});
await browser.close();
console.log(failures === 0 ? 'STALL_WATCHDOG_OK' : `STALL_WATCHDOG_FAILED(${failures})`);
process.exit(failures ? 1 : 0);
