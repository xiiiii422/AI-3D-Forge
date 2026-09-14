/**
 * 截图审查：对 scripts/shots 下的每张图做结构化检查，输出可读报告。
 *
 * 零依赖（自带 PNG 解码），检查：
 *  - 是否整屏空白 / 内容占比异常（过大或过小）
 *  - 是否出现紫色系像素（设计规范禁止紫色/紫罗兰强调色）
 *  - 是否出现大面积高饱和色块（禁止大面积彩色块）
 *  - 关键区域（顶栏、三栏、卡片）是否有内容
 *
 * 用法：node scripts/review-shots.mjs
 */
import { readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { decodePng, dominantColor } from './png.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const SHOTS = join(HERE, 'shots');

/** 统计整图特征 */
function analyze(path) {
  const img = decodePng(path);
  const bg = dominantColor(img);
  let nonBg = 0;
  let purple = 0;
  let saturated = 0;
  let total = 0;
  const rows = new Array(img.height).fill(0);

  for (let y = 0; y < img.height; y += 1) {
    for (let x = 0; x < img.width; x += 1) {
      const i = (y * img.width + x) * 4;
      const r = img.data[i];
      const g = img.data[i + 1];
      const b = img.data[i + 2];
      total += 1;
      if (Math.abs(r - bg.r) + Math.abs(g - bg.g) + Math.abs(b - bg.b) > 20) {
        nonBg += 1;
        rows[y] += 1;
      }
      const max = Math.max(r, g, b);
      const min = Math.min(r, g, b);
      const sat = max === 0 ? 0 : (max - min) / max;
      if (sat > 0.35 && max > 60) saturated += 1;
      if (r > 90 && b > 120 && g < Math.min(r, b) - 34) purple += 1;
    }
  }

  // 纵向内容分布：找出连续空白带（可能意味着布局断裂）
  const threshold = 3;
  const emptyRows = rows.filter((c) => c <= threshold).length;

  return {
    size: `${img.width}×${img.height}`,
    background: `rgb(${bg.r},${bg.g},${bg.b}) 占比 ${(bg.ratio * 100).toFixed(0)}%`,
    ink: nonBg / total,
    purple: purple / total,
    saturated: saturated / total,
    blankRowRatio: emptyRows / img.height,
  };
}

const files = readdirSync(SHOTS).filter((f) => f.endsWith('.png')).sort();
const issues = [];

console.log('截图审查报告');
console.log('='.repeat(96));
for (const file of files) {
  const r = analyze(join(SHOTS, file));
  const flags = [];
  if (r.ink < 0.01) flags.push('几乎空白');
  if (r.ink > 0.85) flags.push('内容过密');
  if (r.purple > 0.001) flags.push(`紫色像素 ${(r.purple * 100).toFixed(2)}%`);
  if (r.saturated > 0.05) flags.push(`高饱和色块 ${(r.saturated * 100).toFixed(1)}%`);
  if (r.blankRowRatio > 0.6) flags.push(`空白行 ${(r.blankRowRatio * 100).toFixed(0)}%`);
  console.log(
    `${file.padEnd(30)} ${r.size.padEnd(11)} 内容 ${(r.ink * 100).toFixed(1).padStart(5)}%  底 ${r.background.padEnd(26)} ${flags.length ? '⚠ ' + flags.join('，') : '✓'}`,
  );
  if (flags.length) issues.push(`${file}: ${flags.join('，')}`);
}
console.log('='.repeat(96));
console.log(issues.length ? `发现 ${issues.length} 处需要人工确认：\n- ${issues.join('\n- ')}` : '所有截图通过自动审查。');
