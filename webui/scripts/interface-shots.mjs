/**
 * 截取界面评审用的页面快照（浅色 + 深色），并核对品牌按钮的**实际涂色**。
 * 用法：node webui/scripts/interface-shots.mjs [baseUrl]
 */

import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, '..', '..');
const baseUrl = (process.argv[2] || 'http://127.0.0.1:8892').replace(/\/$/, '');
const outDir = resolve(root, 'build', 'work', 'interface-shots');
mkdirSync(outDir, { recursive: true });

const { chromium } = await import('playwright');
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const report = {};

/** 主题由后端设置决定，所以切主题要改后端；结束时恢复。 */
async function readTheme() {
  try {
    const res = await fetch(`${baseUrl}/api/settings`);
    return res.ok ? (await res.json()).theme || null : null;
  } catch {
    return null;
  }
}
async function writeTheme(theme) {
  if (!theme) return;
  try {
    await fetch(`${baseUrl}/api/settings`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme }),
    });
  } catch {
    /* ignore */
  }
}

const originalTheme = await readTheme();

for (const theme of ['light', 'dark']) {
  await writeTheme(theme);
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  // 前端首帧也会读 localStorage，两份都设上，保证截图与目标主题一致
  await context.addInitScript((value) => {
    try {
      window.localStorage.setItem('aiforge.theme', value);
    } catch {
      /* ignore */
    }
  }, theme);
  const page = await context.newPage();
  for (const route of ['#/welcome', '#/studio', '#/settings', '#/diagnostics', '#/about']) {
    await page.goto(`${baseUrl}/${route}`, { waitUntil: 'networkidle', timeout: 40000 });
    await page.waitForTimeout(700);
    const applied = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
    if (applied !== theme) console.warn(`  [警告] ${route} 期望 ${theme}，实际 ${applied}`);
    const name = `${route.replace(/[#/]/g, '') || 'root'}-${theme}.png`;
    await page.screenshot({ path: resolve(outDir, name) });
    // 品牌按钮的实际涂色（渲染后的计算值，而不是我写的令牌字面量）
    const brand = await page.evaluate(() => {
      const el = document.querySelector('.btn-brand, .pill-brand, a[href^="http"]');
      if (!el) return null;
      const s = getComputedStyle(el);
      return {
        sel: el.className || el.tagName,
        theme: document.documentElement.getAttribute('data-theme'),
        color: s.color,
        background: s.backgroundColor,
        fontSize: s.fontSize,
      };
    });
    if (brand) report[`${route}-${theme}`] = brand;
  }
  await context.close();
}

await browser.close();
await writeTheme(originalTheme || 'light');
writeFileSync(resolve(outDir, 'brand-colors.json'), JSON.stringify(report, null, 2), 'utf8');
console.log('快照目录：', outDir);
console.log(`主题已恢复为 ${originalTheme || 'light'}`);
for (const [key, value] of Object.entries(report)) {
  console.log(`  ${key.padEnd(24)} ${value.sel.slice(0, 28).padEnd(30)} fg=${value.color} bg=${value.background}`);
}
