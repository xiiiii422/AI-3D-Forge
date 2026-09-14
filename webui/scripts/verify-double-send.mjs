/**
 * 连点两次发送，只能产生一个作业。
 *
 * 为什么要单独验这一条：
 *   `running` 是由「job_id 已经到手」推出来的，而创建作业是一次网络往返。
 *   在「按下 Enter」到「响应回来」之间，`running` 仍然是 false ——
 *   旧实现里发送键只看 `running`，于是这段时间里发送键是可点的：
 *   用户连敲两次 Enter 就会发出两个创建请求。
 *
 *   后端已经用 409 BUSY 挡住了第二个（不会真的并发写同一个项目目录），
 *   但那不够：用户会看到一条「这个项目上还有一次生成没有结束」的错误卡片，
 *   以为是自己的操作有问题，而且多一次请求就意味着多一份额度风险。
 *   所以前端要有**乐观锁**：按下瞬间就锁住，请求结束再解锁。
 *
 * 这条测试**不需要调用 AI**：只打桩 `POST /api/jobs` 并故意**延迟 1.5 秒**返回，
 * 人为制造出那段空窗期，然后在空窗期里再按一次 Enter，最后数真实发出的请求数。
 * 其余请求（含幽灵作业的 404 收尾链路）全部走真实后端。
 *
 * 前置：真实后端（它会自己托管 webui/dist，不需要另起前端）
 *   python -m uvicorn aiforge.app:create_app --factory --host 127.0.0.1 --port 8892 --app-dir src
 *
 * 用法：node webui/scripts/verify-double-send.mjs http://127.0.0.1:8892
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

const FIRST = '先发一条：设计一个桌面手机支架';
const SECOND = '空窗期里再敲一条：高度改成 120mm';
const CREATE_DELAY_MS = 1500;
let createCount = 0;

await page.route('**/api/jobs', async (route) => {
  if (route.request().method() !== 'POST') return route.continue();
  createCount += 1;
  // 故意拖着不回：这就是「响应还没回来」的那段空窗期
  await new Promise((resolve) => setTimeout(resolve, CREATE_DELAY_MS));
  await route.fulfill({
    status: 202,
    contentType: 'application/json',
    body: JSON.stringify({
      job_id: 'double-send-probe',
      project_id: 'ghost',
      revision: 1,
    }),
  });
});

await page.goto(`${base}/#/studio`, { waitUntil: 'networkidle', timeout: 40000 });
await page.waitForTimeout(800);

const composer = page.locator('textarea').first();
const sendBtn = page.locator('.composer-send');

/* ---------------- 1. 制造空窗期，并在空窗期里再敲一次 ---------------- */
await composer.fill(FIRST);
await composer.press('Enter');
await page.waitForTimeout(80);
// 关键：必须**先重新填字**再按 Enter。
// 第一次发送成功后输入框会被清空，而发送键在输入框为空时本来就禁用 ——
// 直接按 Enter 只会被「文本为空」早早挡回去，根本走不到防重入那条分支，
// 测试就会假绿。这个坑在本项目的 spec-card-audit.mjs 里也留着注释。
await composer.fill(SECOND);
await composer.press('Enter');
await page.waitForTimeout(120);

/* ---------------- 2. 空窗期内的可见状态 ---------------- */
// 此刻输入框有内容，若发送键仍可点，说明锁根本不存在
const lockedDuring = !(await sendBtn.isEnabled().catch(() => false));
step('请求未返回期间发送键保持禁用（输入框有内容时）', lockedDuring);

// 防重入必须是「原样退回」：如果把它当成已发送而清空输入框，
// 用户辛苦敲的那句话就被悄悄吃掉了 —— 那是另一种 bug，也必须挡住。
const kept = await composer.inputValue().catch(() => '');
step('被挡下的那条仍留在输入框里（没被悄悄吃掉）', kept === SECOND, kept);

/* ---------------- 3. 等这轮收尾（幽灵作业 404 → 界面自己解锁） ---------------- */
const started = Date.now();
let unlocked = false;
while (Date.now() - started < 25000) {
  await page.waitForTimeout(400);
  unlocked = await sendBtn.isEnabled().catch(() => false);
  if (unlocked && createCount > 0) break;
}

step('只发出了一次创建请求', createCount === 1, `实际 ${createCount} 次`);

const text = (await page.locator('body').innerText().catch(() => '')).replace(/\s+/g, ' ');
step(
  '被挡下的那条没有变成第二条对话气泡',
  !text.includes('空窗期里再敲一条'),
  text.slice(-160),
);

/* ---------------- 4. 锁必须是可解的，不能变成砖头 ---------------- */
await composer.fill('收尾之后我还能不能再发一条');
await page.waitForTimeout(300);
const enabled = await sendBtn.isEnabled().catch(() => false);
step('收尾之后发送键恢复可点（锁没有永久卡住）', enabled);

await page.screenshot({ path: 'build/work/double-send.png' }).catch(() => {});
await browser.close();
console.log(failures === 0 ? 'DOUBLE_SEND_OK' : `DOUBLE_SEND_FAILED(${failures})`);
process.exit(failures ? 1 : 0);
