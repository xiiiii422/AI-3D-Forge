/**
 * 切页 / 刷新之后，正在跑的作业**必须还在界面上**。
 *
 * 用户实测反馈：OpenSCAD 在后台建模时点到别的界面，回到建模页，
 * 进度条、停止按钮、规格卡片全都不见了 —— 而作业其实还在服务端跑。
 * 三个根因（都在这一轮修掉）：
 *   1. 作业状态在后端，而前端把 jobId / 对话都放在 Studio 的组件状态里，
 *      组件一卸载就全忘；`openProject` 还会把对话清空成一条摘要；
 *   2. 顶栏「工作台」永远指向不带项目 id 的 `/studio`，从别的页面点回来就是**空工作台**；
 *   3. 假后端把「SSE 断开」当成作业被放弃（真实后端不会），
 *      于是这条路径在夹具上根本测不出来。
 *
 * 这条测试**不需要 AI**：用假后端（webui/mock-server.mjs），它会在
 * 「等你确认需求」这一步停下 —— 正好是最该被重新接管的状态。
 *
 * 前置：
 *   node webui/mock-server.mjs          # 假后端，8760
 *   cd webui && npm run dev             # dev server，5273（proxy → 8760）
 * 用法：node webui/scripts/verify-job-survives-navigation.mjs http://localhost:5273
 */

import { chromium } from 'playwright';

const base = (process.argv[2] || 'http://localhost:5273').replace(/\/$/, '');
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

let failures = 0;
const step = (name, ok, detail = '') => {
  if (!ok) failures += 1;
  console.log(`  ${ok ? '[OK]  ' : '[FAIL]'} ${name}` + (detail ? ` —— ${detail}` : ''));
};

page.on('pageerror', (e) => step('页面无脚本异常', false, e.message.slice(0, 160)));
if (process.env.JOB_NAV_DEBUG === '1') {
  page.on('request', (r) => {
    if (r.url().includes('/api/jobs')) console.log('    （诊断）请求：', r.method(), r.url().replace(base, ''));
  });
}

/** 查这个项目当前的作业状态（诊断 + 前置条件都用它）。 */
const jobStates = (projectId) =>
  page.evaluate(async (pid) => {
    const res = await fetch(`/api/jobs?project_id=${pid}`);
    const data = await res.json();
    return (data.jobs || []).map((j) => `${j.status}/${j.stage}`);
  }, projectId);

// 有长连接（SSE）时 networkidle 永远不会触发，所以一律用 domcontentloaded + 显式等待
await page.goto(`${base}/#/studio`, { waitUntil: 'domcontentloaded', timeout: 40000 });
await page.waitForTimeout(900);

/* ---------- 1. 发一条需求，走到「等确认」 ---------- */
const composer = () => page.locator('textarea').first();
await composer().fill('设计一个桌面手机支架，高度100mm');
await composer().press('Enter');
await page.waitForSelector('.spec-card', { timeout: 25000 }).catch(() => undefined);
const cardFirst = await page.locator('.spec-card').isVisible().catch(() => false);
const projectId = page.url().split('/studio/')[1] || null;
step('前提：作业停在确认卡片上', cardFirst, `project=${projectId}`);

// 卡片可见**不等于**作业还停着：快照里的 spec 在作业结束之后仍然在，卡片会留在界面上。
// 所以这里必须查一次真实状态（第一版测试就误判在这儿）。
await page.waitForTimeout(800);
const jobsNow = await jobStates(projectId);
step(
  '作业确实停在等待确认（而不是已经跑完）',
  jobsNow.some((s) => s.startsWith('awaiting_confirmation')),
  JSON.stringify(jobsNow),
);

/* ---------- 2. 走开去设置页，再回工作台（用户报的那一步） ---------- */
// 用可见文案选链接，而不是写死 href：工作台入口现在会带回项目 id（`#/studio/<id>`），
// 写死 `a[href$="#/studio"]` 会匹配不到、白等 30 秒超时。
await page.getByRole('link', { name: '设置' }).first().click({ timeout: 5000 }).catch(async () => {
  await page.goto(`${base}/#/settings`, { waitUntil: 'domcontentloaded' });
});
await page.waitForTimeout(700);
step('已切到设置页', page.url().includes('/settings'), page.url());
step(
  '切页不会把作业停掉',
  (await jobStates(projectId)).some((s) => s.startsWith('awaiting_confirmation')),
);

await page.getByRole('link', { name: '工作台' }).first().click({ timeout: 5000 }).catch(async () => {
  await page.goto(`${base}/#/studio/${projectId}`, { waitUntil: 'domcontentloaded' });
});
await page.waitForTimeout(1500);
step('回到的是刚才那个项目而不是空工作台', page.url().includes('/studio/'), page.url());
const cardBack = await page.locator('.spec-card').isVisible().catch(() => false);
step('切回工作台后确认卡片还在（作业被重新接管）', cardBack);

const bodyText = (await page.locator('body').innerText().catch(() => '')).replace(/\s+/g, ' ');
step('对话里的需求没有被清空', bodyText.includes('设计一个桌面手机支架'), bodyText.slice(-100));

/* ---------- 3. 整页刷新（F5）也要能接管 ---------- */
await page.reload({ waitUntil: 'domcontentloaded', timeout: 40000 });
await page.waitForTimeout(2200);
const cardAfterReload = await page.locator('.spec-card').isVisible().catch(() => false);
step('刷新页面后确认卡片仍然回来', cardAfterReload);
if (!cardAfterReload) {
  await page.screenshot({ path: 'build/work/job-nav-reload.png' }).catch(() => {});
}

await browser.close();
console.log(failures === 0 ? 'JOB_NAV_OK' : `JOB_NAV_FAILED(${failures})`);
process.exit(failures ? 1 : 0);
