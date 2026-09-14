/**
 * 真实后端联调验证：「停止生成」必须能立刻解锁输入区。
 *
 * 为什么单独留这一支（假后端的体检不能替代它）：
 * `spec-card-audit.mjs` 跑在假后端上，而假后端**曾经压根不推 `cancelled` 事件**。
 * 也就是说，这条最关键的用户路径在假后端上根本测不到 ——
 * 「点停止后发送键永久禁用」正是这样漏过所有测试的。
 * 所以这里必须对着**真实 FastAPI 后端**再跑一遍。
 *
 * 前置：
 *   1. 起真实后端（它会自己托管 webui/dist，所以不需要另外起前端）：
 *        $env:AIFORGE_DATA_DIR='...\cancel-data'
 *        $env:AIFORGE_PROJECTS_DIR='...\cancel-projects'
 *        $env:AIFORGE_LOCAL_DIR='...\build\work\testlocal'
 *        python -m uvicorn aiforge.app:create_app --factory --host 127.0.0.1 --port 8891 --app-dir src
 *   2. 前端构建产物要是最新的：cd webui && npm run build
 *
 * 用法：node webui/scripts/verify-cancel-real.mjs http://127.0.0.1:8891
 *
 * 需要能连上 DeepSeek（真实后端会先解析需求）。没有 Key 时会在更早的阶段失败，
 * 那种失败会明确打印出来，不会伪装成「通过」。
 */

import { chromium } from 'playwright';

const base = (process.argv[2] || 'http://127.0.0.1:8891').replace(/\/$/, '');
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

let failures = 0;
const step = (name, ok, detail = '') => {
  if (!ok) failures += 1;
  console.log(`  ${ok ? '[OK]  ' : '[FAIL]'} ${name}` + (detail ? ` —— ${detail}` : ''));
};

page.on('pageerror', (e) => step('页面无脚本异常', false, e.message.slice(0, 160)));

await page.goto(`${base}/#/studio`, { waitUntil: 'networkidle', timeout: 40000 });
await page.waitForTimeout(800);

const composer = page.locator('textarea').first();
const sendBtn = page.locator('.composer-send');
const runbar = page.locator('.runbar');

await composer.fill('设计一个桌面手机支架，高度100mm');
await composer.press('Enter');
await runbar.waitFor({ timeout: 20000 }).catch(() => undefined);
step('生成中：运行条出现，发送键处于禁用态', (await runbar.count()) > 0 && (await sendBtn.isDisabled()));

// 等规格卡片出现：这一步在真实后端会稳定发生（解析完规格就停在这等人确认）
await page.waitForSelector('.spec-card', { timeout: 30000 }).catch(() => undefined);
step(
  '真实后端已进入「等待确认规格」',
  (await page.locator('.spec-card').count()) > 0,
  '没等到规格卡片说明这次跑的环境不完整（例如没有 API Key）',
);

console.log('  点击「停止生成」…');
await page.locator('.panel-head button').first().click();

// 发送键在输入框为空时本来就禁用，所以必须先填字再判定
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
  '点停止后发送键恢复可点',
  recoveredAt !== null,
  recoveredAt === null ? '30 秒后仍禁用（用户被锁死）' : `t+${recoveredAt}s`,
);

if (recoveredAt !== null) {
  const draft = await composer.inputValue();
  step('停止不清空已输入的内容', draft === '停止后我还能不能发消息', JSON.stringify(draft));
  await sendBtn.click();
  await page.waitForTimeout(1800);
  const bubbles = await page.locator('.msg-user .bubble').allInnerTexts();
  step(
    '恢复后确实能发出下一条消息',
    bubbles.some((t) => t.includes('停止后我还能不能发消息')),
    JSON.stringify(bubbles.slice(-1)),
  );
}

await page.screenshot({ path: 'build/work/verify-cancel-real.png' });
await browser.close();
console.log('');
console.log(failures === 0 ? '真实后端联调验证：全部通过。' : `真实后端联调验证：${failures} 项失败。`);
process.exit(failures === 0 ? 0 : 1);
