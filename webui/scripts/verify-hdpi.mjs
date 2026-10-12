/**
 * 高分辨率（DPI）适配验收。
 *
 * 为什么单独一个脚本：普通布局体检只在 1440/1180 的 1x 视口下跑，
 * 而真实用户的高分辨率屏是「逻辑尺寸变小 + 后备缓冲变大」两件事同时发生：
 *   * 4K @200% 只有 1920×1080 DIP，界面必须在更小的逻辑画布里不溢出；
 *   * devicePixelRatio=2 时 WebGL 画布的后备缓冲必须跟着翻倍，否则 3D 线条发虚。
 * 用 CDP 的 Emulation.setDeviceMetricsOverride 直接模拟这些组合。
 *
 * 用法：node webui/scripts/verify-hdpi.mjs [BASE_URL]
 *   先起 mock（8760）与 vite（5273）。
 */
import { mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { connectPage, launchBrowser, sleep } from './cdp.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const base = process.argv[2] || 'http://127.0.0.1:5273';
const shotDir = join(__dirname, 'shots');
mkdirSync(shotDir, { recursive: true });

/** 场景：逻辑视口 + devicePixelRatio（模拟不同物理分辨率与系统缩放组合） */
const SCENES = [
  { name: '4k-200', width: 1920, height: 1080, dpr: 2, label: '4K 屏 @200% 缩放' },
  { name: 'uhd-150', width: 1707, height: 960, dpr: 1.5, label: 'UHD 屏 @150%' },
  { name: 'fhd-125', width: 1536, height: 780, dpr: 1.25, label: '1080p 屏 @125%' },
  { name: 'small-250', width: 1024, height: 640, dpr: 2.5, label: '小屏高缩放（像素比上限夹紧测试）' },
  { name: 'wide-100', width: 2560, height: 1440, dpr: 1, label: '2K 屏 @100%' },
];

const ROUTES = ['#/welcome', '#/studio', '#/settings'];
const failures = [];
const report = [];

async function setMetrics(page, scene) {
  // 不走 client.setViewport（它把 deviceScaleFactor 写死成 1）
  await page.send('Emulation.setDeviceMetricsOverride', {
    width: scene.width,
    height: scene.height,
    deviceScaleFactor: scene.dpr,
    mobile: false,
  });
}

/** 溢出与可读性检查：任何横跨全宽的块都不允许超出视口 */
async function auditOverflow(page) {
  return await page.evaluate(`(() => {
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const bad = [];

    // 某个元素是不是"在一段可滚动的内容里"。
    //
    // 为什么必须先问这个：设置页是长表单，内容比视口高是**正常**的
    // （实测 .app-main scrollHeight=3130 / clientHeight=613，滚动条一切正常）。
    // 早期版本只按 "bottom > vh + 2000" 判垂直溢出，把这类**正常的长页面**
    // 误报成"元素越界" —— 于是 4 个场景全红，而产品其实没有任何问题。
    //
    // 判据：向上找最近的、overflow-y 为 auto/scroll 且确实能滚动的祖先。
    // 找到了就说明这个元素的内容**用户滚得到**，不算越界。
    // 找不到（真正的溢出，比如一个没人能滚到的高元素）才报。
    const inScrollable = (el) => {
      for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
        const cs = getComputedStyle(p);
        if (/(auto|scroll)/.test(cs.overflowY) && p.scrollHeight > p.clientHeight + 2) {
          return true;
        }
      }
      return false;
    };

    for (const el of document.querySelectorAll('body *')) {
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') continue;
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) continue;
      // 允许 2px 误差（滚动条与亚像素）
      if (r.right > vw + 2 || r.left < -2) {
        bad.push({ tag: el.tagName, cls: (el.className || '').toString().slice(0, 60), right: Math.round(r.right), left: Math.round(r.left) });
        if (bad.length > 6) break;
      }
      if (r.bottom > vh + 2000 && !inScrollable(el)) {
        bad.push({ tag: el.tagName, cls: '垂直方向超出 ' + Math.round(r.bottom), right: 0, left: 0 });
        if (bad.length > 6) break;
      }
    }
    return {
      vw, vh,
      dpr: window.devicePixelRatio,
      docOverflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      bad: bad.slice(0, 6),
      minFontSize: Math.min(...[...document.querySelectorAll('p,span,li,button,label,td,th,h1,h2,h3,strong,code')]
        .filter((el) => el.getBoundingClientRect().width > 0 && getComputedStyle(el).display !== 'none')
        .map((el) => parseFloat(getComputedStyle(el).fontSize))),
    };
  })()`);
}

/** WebGL 画布后备缓冲必须跟随 DPR（上限 2），否则高 DPI 上模型发虚 */
async function auditCanvas(page) {
  return await page.evaluate(`(() => {
    const canvas = document.querySelector('.viewer-canvas canvas');
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    return {
      cssWidth: Math.round(rect.width),
      bufferWidth: canvas.width,
      ratio: rect.width ? +(canvas.width / rect.width).toFixed(2) : 0,
      expected: Math.min(window.devicePixelRatio || 1, 2),
    };
  })()`);
}

const browser = await launchBrowser({ port: 9333, width: 1600, height: 1000 });
try {
  const page = await connectPage(browser.port, `${base}/`);
  await page.send('Page.enable');
  await page.send('Runtime.enable');

  for (const scene of SCENES) {
    for (const route of ROUTES) {
      await setMetrics(page, scene);
      await page.goto(`${base}/${route}`, { settleMs: 900 });
      await sleep(500);

      const info = await auditOverflow(page);
      const tag = `${scene.name} ${route}`;

      if (Math.abs(info.dpr - scene.dpr) > 0.01) {
        failures.push(`${tag}: devicePixelRatio 应为 ${scene.dpr}，实际 ${info.dpr}`);
      }
      if (info.docOverflowX > 1) {
        failures.push(`${tag}: 文档横向溢出 ${info.docOverflowX}px`);
      }
      if (info.bad.length) {
        failures.push(`${tag}: 元素越界 ${JSON.stringify(info.bad)}`);
      }
      if (info.minFontSize && info.minFontSize < 11.5) {
        failures.push(`${tag}: 出现 ${info.minFontSize}px 的过小文字（高缩放下不可读）`);
      }

      const line = {
        scene: tag,
        dpr: info.dpr,
        viewport: `${info.vw}x${info.vh}`,
        overflowX: info.docOverflowX,
        minFont: info.minFontSize,
      };

      if (route === '#/studio') {
        await page.waitForFunction(`!!document.querySelector('.viewer-canvas canvas')`, {
          label: '3D 画布',
          timeoutMs: 15000,
        });
        const canvas = await auditCanvas(page);
        line.canvas = canvas;
        if (!canvas) {
          failures.push(`${tag}: 找不到 3D 画布`);
        } else if (Math.abs(canvas.ratio - canvas.expected) > 0.12) {
          // 后备缓冲必须按 DPR 放大（上限 2），否则高 DPI 上模型是糊的
          failures.push(
            `${tag}: 画布后备缓冲比 ${canvas.ratio}，应为 ${canvas.expected}（CSS ${canvas.cssWidth}px / buffer ${canvas.bufferWidth}px）`,
          );
        }
        const png = join(shotDir, `hdpi-${scene.name}.png`);
        await page.screenshot(png);
        line.shot = png;
      }
      report.push(line);
    }
  }
} finally {
  await browser.kill();
}

console.log('');
for (const r of report) {
  console.log(
    `  ${r.scene.padEnd(22)} dpr=${String(r.dpr).padEnd(4)} 视口=${r.viewport.padEnd(10)} 横溢=${String(r.overflowX).padStart(2)}px 最小字号=${r.minFont}px` +
      (r.canvas ? `  画布 CSS${r.canvas.cssWidth}→buffer${r.canvas.bufferWidth}（×${r.canvas.ratio}，应 ×${r.canvas.expected}）` : ''),
  );
}
console.log('');

if (failures.length) {
  console.log(`HDPI_FAIL（${failures.length} 项）`);
  for (const f of failures) console.log(`  - ${f}`);
  process.exit(1);
}
console.log(`HDPI_OK（${SCENES.length} 个高分辨率场景 × ${ROUTES.length} 页全部通过）`);
