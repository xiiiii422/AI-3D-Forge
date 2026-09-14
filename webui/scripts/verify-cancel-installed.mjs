/**
 * 对着**已安装的正式版**验证「停止生成」能解锁输入区。
 *
 * 为什么还要这一步：前面几支验的都是源码/开发模式。用户拿到的
 * 是安装包里的 exe + 它自己托管的 webui/dist —— 只有这一步能证明
 * 「用户实际装的那一份」确实修好了。
 *
 * 前置：AI-3D-Forge 已安装并可通过 HTTP 驱动（见 scripts/test_install.ps1 -KeepInstalled）。
 *
 * 用法：node webui/scripts/verify-cancel-installed.mjs <installedAppUrl>
 *   例如：node webui/scripts/verify-cancel-installed.mjs http://127.0.0.1:8781
 */

import { chromium } from 'playwright';

const base = (process.argv[2] || 'http://127.0.0.1:8781').replace(/\/$/, '');
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

let failures = 0;
const step = (name, ok, detail = '') => {
  if (!ok) failures += 1;
  console.log(`  ${ok ? '[OK]  ' : '[FAIL]'} ${name}` + (detail ? ` —— ${detail}` : ''));
};

page.on('pageerror', (e) => step('页面无脚本异常', false, e.message.slice(0, 160)));

await page.goto(`${base}/#/studio`, { waitUntil: 'networkidle', timeout: 40000 });
await page.waitForTimeout(1000);

const composer = page.locator('textarea').first();
const sendBtn = page.locator('.composer-send');
const runbar = page.locator('.runbar');

await composer.fill('设计一个桌面手机支架，高度100mm');
await composer.press('Enter');
await runbar.waitFor({ timeout: 25000 }).catch(() => undefined);
step('已安装版：生成中运行条出现', (await runbar.count()) > 0);

// 等规格卡片（已安装版同样会先解析需求并停下等确认）
await page.waitForSelector('.spec-card', { timeout: 30000 }).catch(() => undefined);
const hadSpec = (await page.locator('.spec-card').count()) > 0;
console.log(`  （规格卡片出现=${hadSpec}；没出现通常是这台机器没配 API Key，不影响下面这条检查）`);

console.log('  点击「停止生成」…');
await page.locator('.panel-head button').first().click();

// 发送键在空输入时本来就禁用 → 先填字
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
  '已安装版：点停止后发送键恢复可点',
  recoveredAt !== null,
  recoveredAt === null ? '30 秒后仍禁用（用户被锁死）' : `t+${recoveredAt}s`,
);

if (recoveredAt !== null) {
  const draft = await composer.inputValue();
  step('已安装版：草稿保留', draft === '停止后我还能不能发消息', JSON.stringify(draft));
  await sendBtn.click();
  await page.waitForTimeout(1800);
  const bubbles = await page.locator('.msg-user .bubble').allInnerTexts();
  step(
    '已安装版：恢复后能发出下一条',
    bubbles.some((t) => t.includes('停止后我还能不能发消息')),
    JSON.stringify(bubbles.slice(-1)),
  );
}

await page.screenshot({ path: 'build/work/verify-cancel-installed.png' });
await browser.close();
console.log('');
console.log(failures === 0 ? '已安装版验证：全部通过。' : `已安装版验证：${failures} 项失败。`);
process.exit(failures === 0 ? 0 : 1);
