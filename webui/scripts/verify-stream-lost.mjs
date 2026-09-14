/**
 * 「作业在服务端消失 / 与本地服务失联」时，界面必须自己解锁。
 *
 * 为什么要单独验这一条：
 *   轮询是 SSE 连不上之后的兜底，而连不上的原因很可能是**本地服务已经不在了**
 *   （进程被杀、端口被占、休眠回来没恢复）。旧实现会一直轮询下去，永远不会收尾 ——
 *   界面就一直显示「生成中」、发送键一直禁用，用户除了刷新没别的出路。
 *   这和更早那次「点停止后发送键永久禁用」是同一类故障，只是入口不同。
 *
 * 这条测试**不需要调用 AI**：只打桩「创建作业」，让它返回一个服务端并不认识的
 * job_id，于是后续对 /api/jobs/<id> 的**真实**请求会拿到 404 —— 正是要验的场景。
 * 其余全部走真实后端，所以 SSE→重连→轮询→收尾 这条链路是真的被跑了一遍。
 *
 * 前置：真实后端（它会自己托管 webui/dist，不需要另起前端）
 *   python -m uvicorn aiforge.app:create_app --factory --host 127.0.0.1 --port 8892 --app-dir src
 *
 * 用法：node webui/scripts/verify-stream-lost.mjs http://127.0.0.1:8892
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

await page.route('**/api/jobs', async (route) => {
  if (route.request().method() !== 'POST') return route.continue();
  await route.fulfill({
    status: 202,
    contentType: 'application/json',
    body: JSON.stringify({
      job_id: 'nonexistent-job-for-test',
      project_id: 'ghost',
      revision: 1,
    }),
  });
});

await page.goto(`${base}/#/studio`, { waitUntil: 'networkidle', timeout: 40000 });
await page.waitForTimeout(800);

const composer = page.locator('textarea').first();
const sendBtn = page.locator('.composer-send');

await composer.fill('这个作业在服务端不存在，界面必须自己解锁');
await composer.press('Enter');

// 轮询 1.5s 一拍；404 应当**立刻**收尾，这里给足 25 秒余量
const started = Date.now();
let settled = false;
while (Date.now() - started < 25000) {
  await page.waitForTimeout(500);
  // 收尾的标志：运行中的「停止」按钮消失
  settled = !(await page.locator('.panel-head button').first().isVisible().catch(() => false));
  if (settled) break;
}
const elapsed = ((Date.now() - started) / 1000).toFixed(1);

step('作业不存在时界面自己收尾（不再显示「停止」）', settled, `耗时 ${elapsed}s`);

// 关键：判断「输入区是否解锁」必须**先往输入框里填字**。
// 发送键在输入框为空时本来就是禁用的（Enter 发送后输入框会被清空），
// 直接看 isEnabled() 只会把「空输入导致的正常禁用」误判成故障 ——
// 这个坑在本项目里踩过一次，注释在 spec-card-audit.mjs 里也留着。
await composer.fill('收尾之后我还能不能再发一条');
await page.waitForTimeout(300);
const enabled = await sendBtn.isEnabled().catch(() => false);
step('收尾之后发送键恢复可点（输入框有内容时）', enabled);

const text = (await page.locator('body').innerText().catch(() => '')).replace(/\s+/g, ' ');
step(
  '给出了可读的收尾提示',
  /不存在|过期|重新生成|中断|失败/.test(text),
  text.slice(-140),
);

await page.screenshot({ path: 'build/work/stream-lost.png' }).catch(() => {});
await browser.close();
console.log(failures === 0 ? 'STREAM_LOST_OK' : `STREAM_LOST_FAILED(${failures})`);
process.exit(failures ? 1 : 0);
