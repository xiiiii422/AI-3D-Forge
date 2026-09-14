/**
 * 零依赖 PNG 解码 + 像素分析。
 *
 * 用途：headless 截图之后，用像素证据判断「视口里是不是真的画出了模型」，
 * 以及统计界面用色是否越界（例如出现紫色/渐变）。WebGL 的后台缓冲在合成后会被清空，
 * readPixels 拿不到内容，而截图恰好是用户真正看到的画面。
 *
 * 支持 8bit RGB / RGBA、非隔行 PNG（Chromium 的截图就是这个格式）。
 */
import { inflateSync } from 'node:zlib';
import { readFileSync } from 'node:fs';

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

/** 解码 PNG → { width, height, data: Buffer(RGBA) } */
export function decodePng(path) {
  const buf = readFileSync(path);
  if (!buf.subarray(0, 8).equals(PNG_SIGNATURE)) throw new Error('不是 PNG 文件');

  let offset = 8;
  let width = 0;
  let height = 0;
  let bitDepth = 0;
  let colorType = 0;
  let interlace = 0;
  const idat = [];

  while (offset < buf.length) {
    const length = buf.readUInt32BE(offset);
    const type = buf.toString('ascii', offset + 4, offset + 8);
    const data = buf.subarray(offset + 8, offset + 8 + length);
    if (type === 'IHDR') {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      bitDepth = data[8];
      colorType = data[9];
      interlace = data[12];
    } else if (type === 'IDAT') {
      idat.push(data);
    } else if (type === 'IEND') {
      break;
    }
    offset += 12 + length;
  }

  if (bitDepth !== 8) throw new Error(`暂不支持 bitDepth=${bitDepth}`);
  if (interlace !== 0) throw new Error('暂不支持隔行 PNG');
  const channels = colorType === 6 ? 4 : colorType === 2 ? 3 : 0;
  if (!channels) throw new Error(`暂不支持 colorType=${colorType}`);

  const raw = inflateSync(Buffer.concat(idat));
  const stride = width * channels;
  const out = Buffer.alloc(width * height * 4);
  let prev = Buffer.alloc(stride);

  for (let y = 0; y < height; y += 1) {
    const filter = raw[y * (stride + 1)];
    const line = Buffer.from(raw.subarray(y * (stride + 1) + 1, y * (stride + 1) + 1 + stride));
    unfilter(filter, line, prev, channels);
    for (let x = 0; x < width; x += 1) {
      const s = x * channels;
      const d = (y * width + x) * 4;
      out[d] = line[s];
      out[d + 1] = line[s + 1];
      out[d + 2] = line[s + 2];
      out[d + 3] = channels === 4 ? line[s + 3] : 255;
    }
    prev = line;
  }

  return { width, height, data: out };
}

function unfilter(type, line, prev, bpp) {
  const n = line.length;
  switch (type) {
    case 0:
      break;
    case 1:
      for (let i = bpp; i < n; i += 1) line[i] = (line[i] + line[i - bpp]) & 0xff;
      break;
    case 2:
      for (let i = 0; i < n; i += 1) line[i] = (line[i] + prev[i]) & 0xff;
      break;
    case 3:
      for (let i = 0; i < n; i += 1) {
        const a = i >= bpp ? line[i - bpp] : 0;
        line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xff;
      }
      break;
    case 4:
      for (let i = 0; i < n; i += 1) {
        const a = i >= bpp ? line[i - bpp] : 0;
        const b = prev[i];
        const c = i >= bpp ? prev[i - bpp] : 0;
        const p = a + b - c;
        const pa = Math.abs(p - a);
        const pb = Math.abs(p - b);
        const pc = Math.abs(p - c);
        const pred = pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
        line[i] = (line[i] + pred) & 0xff;
      }
      break;
    default:
      throw new Error(`未知的 PNG 过滤器 ${type}`);
  }
}

const rgbAt = (img, x, y) => {
  const i = (y * img.width + x) * 4;
  return [img.data[i], img.data[i + 1], img.data[i + 2]];
};

/** 找出图中出现次数最多的颜色（当作背景色） */
export function dominantColor(img) {
  const counts = new Map();
  for (let y = 0; y < img.height; y += 2) {
    for (let x = 0; x < img.width; x += 2) {
      const [r, g, b] = rgbAt(img, x, y);
      const key = `${r >> 2},${g >> 2},${b >> 2}`;
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
  }
  let best = null;
  let bestCount = -1;
  for (const [key, count] of counts) {
    if (count > bestCount) {
      bestCount = count;
      best = key;
    }
  }
  const [r, g, b] = best.split(',').map((v) => Number(v) * 4);
  return { r, g, b, ratio: bestCount / ((img.width / 2) * (img.height / 2)) };
}

/**
 * 分析截图里的一个区域，判断里面是不是有「立体的模型」。
 *
 * 判据：背景是接近纯色的台面（浅色主题下是 #F2F3F5）。模型是中性灰、带明暗渐变，
 * 因此统计「与背景色差异明显，但不是网格线那种细线」的像素比例：
 * 比例过高说明画面被别的东西占满（异常），比例在合理区间说明画出了模型。
 */
export function analyzePng(path, region) {
  const img = decodePng(path);
  const x0 = Math.max(0, Math.floor(region?.x ?? 0));
  const y0 = Math.max(0, Math.floor(region?.y ?? 0));
  const x1 = Math.min(img.width, Math.ceil(x0 + (region?.width ?? img.width)));
  const y1 = Math.min(img.height, Math.ceil(y0 + (region?.height ?? img.height)));
  const bg = dominantColor(img);

  let total = 0;
  let differing = 0;
  let midTone = 0; // 中性灰的模型面：明度在 0.25~0.92 之间
  const hist = new Map();

  for (let y = y0; y < y1; y += 1) {
    for (let x = x0; x < x1; x += 1) {
      const [r, g, b] = rgbAt(img, x, y);
      total += 1;
      const dist = Math.abs(r - bg.r) + Math.abs(g - bg.g) + Math.abs(b - bg.b);
      if (dist > 24) {
        differing += 1;
        const lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
        if (lum > 0.22 && lum < 0.94) midTone += 1;
      }
      const key = `${r >> 4},${g >> 4},${b >> 4}`;
      hist.set(key, (hist.get(key) ?? 0) + 1);
    }
  }

  // 紫色检测：红蓝都显著高于绿（设计规范禁止紫色/紫罗兰强调色）
  let purple = 0;
  for (const [key, count] of hist) {
    const [r, g, b] = key.split(',').map((v) => Number(v) * 16);
    if (r > 90 && b > 120 && g < Math.min(r, b) - 34) purple += count;
  }

  const sampled = (img.width / 2) * (img.height / 2);
  void sampled;

  return {
    image: { width: img.width, height: img.height },
    background: bg,
    region: { x0, y0, x1, y1, total },
    differingRatio: differing / Math.max(total, 1),
    modelLikeRatio: midTone / Math.max(total, 1),
    purpleRatio: purple / Math.max(total, 1),
  };
}
