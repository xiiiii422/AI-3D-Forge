/**
 * AI 3D Forge —— 前端联调用的假后端（零依赖，纯 Node）。
 *
 * 用途：真实后端（FastAPI）还没起来时，用符合 docs/API-CONTRACT.md 的响应把整个前端流程跑通：
 *   欢迎向导 → 环境检测 → 3D 引擎 → AI 服务 → 工作台 → 发需求 → 进度 → 结果卡片 → STL 预览。
 *
 * 特点：
 *   - 端点、字段名严格照契约，不额外发明接口（除一个仅供自测的 /api/openscad/install/{id} 轮询兜底）；
 *   - 模型是「真的」：按参数程序化生成二进制 STL（毫米单位），Three.js 能真的渲染出来；
 *   - 作业进度是脚本化的 SSE 序列，最后推一帧完整 Job（含 JobResult）；
 *   - 可以故意失败：POST /api/openscad/install 带 {"method":"auto","confirm":true,"mock":"install-error"}，
 *     或访问 /api/openscad/install?mock=install-error，用来验证错误分支（need_admin / manual_steps）。
 *
 * 启动：node mock-server.mjs [--first-run]
 *   --first-run  让 /api/env/report 返回 first_run=true（用于验证欢迎向导）
 */
import http from 'node:http';
import { randomUUID } from 'node:crypto';

const PORT = Number(process.env.MOCK_PORT || 8760);
const FIRST_RUN = process.argv.includes('--first-run') || process.env.MOCK_FIRST_RUN === '1';

/* ================================================================== *
 * 几何：程序化生成二进制 STL（毫米）
 * ================================================================== */

/** 一个四边形面（4 个点，逆时针看向外侧）→ 2 个三角形 */
function quad(a, b, c, d) {
  return [
    [a, b, c],
    [a, c, d],
  ];
}

/** 轴对齐长方体的 6 个面（x0..x1, y0..y1, z0..z1） */
function boxQuads(x0, y0, z0, x1, y1, z1) {
  return [
    // 底面 (-Y)
    ...quad([x0, y0, z1], [x1, y0, z1], [x1, y0, z0], [x0, y0, z0]),
    // 顶面 (+Y)
    ...quad([x0, y1, z0], [x1, y1, z0], [x1, y1, z1], [x0, y1, z1]),
    // 前面 (+Z)
    ...quad([x0, y0, z1], [x0, y1, z1], [x1, y1, z1], [x1, y0, z1]),
    // 后面 (-Z)
    ...quad([x1, y0, z0], [x1, y1, z0], [x0, y1, z0], [x0, y0, z0]),
    // 右面 (+X)
    ...quad([x1, y0, z1], [x1, y1, z1], [x1, y1, z0], [x1, y0, z0]),
    // 左面 (-X)
    ...quad([x0, y0, z0], [x0, y1, z0], [x0, y1, z1], [x0, y0, z1]),
  ];
}

/**
 * mock 的几何用 Y 向上书写（画 box/圆柱直观），但真实交付的 STL 是 OpenSCAD 口径的
 * **Z 向上**（本机 8 个真实项目实测：min.z ≈ 0、z 跨度 = 总高）。
 * 出文件与算度量都走同一个映射，否则预览的轴向语义在 mock 与真实环境里不一致，
 * 验收会放过「模型躺倒」「高标到深度轴」这类错 —— 实测就真的一直没看出来。
 * (x, y, z) → (x, -z, y) 是纯旋转（行列式 +1），不会镜像几何。
 */
const toZUp = (p) => [p[0], -p[2], p[1]];

/** 三角形集合 → 二进制 STL Buffer（输出 Z 向上，与 OpenSCAD 一致） */
function trianglesToStl(triangles) {
  const buffer = Buffer.alloc(84 + triangles.length * 50);
  buffer.write('AI 3D Forge mock model (binary STL)', 0, 80, 'ascii');
  buffer.writeUInt32LE(triangles.length, 80);
  let offset = 84;
  for (const [q0, q1, q2] of triangles) {
    const p0 = toZUp(q0);
    const p1 = toZUp(q1);
    const p2 = toZUp(q2);
    // 法线
    const ux = p1[0] - p0[0];
    const uy = p1[1] - p0[1];
    const uz = p1[2] - p0[2];
    const vx = p2[0] - p0[0];
    const vy = p2[1] - p0[1];
    const vz = p2[2] - p0[2];
    let nx = uy * vz - uz * vy;
    let ny = uz * vx - ux * vz;
    let nz = ux * vy - uy * vx;
    const len = Math.hypot(nx, ny, nz) || 1;
    nx /= len;
    ny /= len;
    nz /= len;
    buffer.writeFloatLE(nx, offset);
    buffer.writeFloatLE(ny, offset + 4);
    buffer.writeFloatLE(nz, offset + 8);
    let o = offset + 12;
    for (const p of [p0, p1, p2]) {
      buffer.writeFloatLE(p[0], o);
      buffer.writeFloatLE(p[1], o + 4);
      buffer.writeFloatLE(p[2], o + 8);
      o += 12;
    }
    buffer.writeUInt16LE(0, offset + 48);
    offset += 50;
  }
  return buffer;
}

/** 由三角形集合算尺寸、体积与表面积（mm / mm³ / mm²）；轴序与写出的 STL 一致（Z 向上） */
function measure(triangles) {
  const min = [Infinity, Infinity, Infinity];
  const max = [-Infinity, -Infinity, -Infinity];
  let volume = 0;
  let area = 0;
  for (const [qp0, qp1, qp2] of triangles) {
    const p0 = toZUp(qp0);
    const p1 = toZUp(qp1);
    const p2 = toZUp(qp2);
    for (const p of [p0, p1, p2]) {
      for (let i = 0; i < 3; i += 1) {
        if (p[i] < min[i]) min[i] = p[i];
        if (p[i] > max[i]) max[i] = p[i];
      }
    }
    volume +=
      (p0[0] * (p1[1] * p2[2] - p1[2] * p2[1]) -
        p0[1] * (p1[0] * p2[2] - p1[2] * p2[0]) +
        p0[2] * (p1[0] * p2[1] - p1[1] * p2[0])) /
      6;
    // 三角形面积 = ½|AB × AC|（耗材估算要的是真实表面积，不能用包围盒糊）
    const ux = p1[0] - p0[0];
    const uy = p1[1] - p0[1];
    const uz = p1[2] - p0[2];
    const vx = p2[0] - p0[0];
    const vy = p2[1] - p0[1];
    const vz = p2[2] - p0[2];
    area +=
      0.5 *
      Math.hypot(uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx);
  }
  return {
    bbox: { x: max[0] - min[0], y: max[1] - min[1], z: max[2] - min[2] },
    bbox_min: min.map((v) => Number(v.toFixed(2))),
    bbox_max: max.map((v) => Number(v.toFixed(2))),
    volume_mm3: Math.abs(volume),
    surface_area_mm2: area,
    triangles: triangles.length,
  };
}

/** 桌面手机支架：底板 + 后靠背 + 前挡边 */
function phoneStand(p) {
  const w = p.width;
  const h = p.height;
  const d = p.depth;
  const t = p.thickness;
  const angle = p.angle;
  const tris = [];

  // 底板：从 y=0 起，厚 t
  tris.push(...boxQuads(-w / 2, 0, -d / 2, w / 2, t, d / 2));

  // 前挡边：立在底板前缘
  const lipHeight = Math.min(h * 0.16, 18);
  tris.push(...boxQuads(-w / 2, t, d / 2 - t, w / 2, t + lipHeight, d / 2));

  // 后靠背：倾斜的板，底边在后缘
  const rad = (angle * Math.PI) / 180;
  const backLen = Math.max(h - t - lipHeight, 20);
  const dz = Math.cos(rad) * backLen;
  const dy = Math.sin(rad) * backLen;
  const zBack = -d / 2 + t;
  const nx = -Math.sin(rad) * (t / 2);
  const ny = Math.cos(rad) * (t / 2);

  const a = [-w / 2, t, zBack];
  const b = [w / 2, t, zBack];
  const c = [w / 2, t + dy, zBack + dz];
  const dPt = [-w / 2, t + dy, zBack + dz];
  // 靠背朝 +Z 的一面
  const f = (pt) => [pt[0], pt[1] + ny, pt[2] + nx];
  const bk = (pt) => [pt[0], pt[1] - ny, pt[2] - nx];

  tris.push(...quad(f(a), f(b), f(c), f(dPt)));
  tris.push(...quad(bk(dPt), bk(c), bk(b), bk(a)));
  tris.push(...quad(a, dPt, bk(dPt), bk(a)));
  tris.push(...quad(bk(b), bk(c), c, b));
  tris.push(...quad(dPt, c, bk(c), bk(dPt)));
  tris.push(...quad(bk(a), bk(b), b, a));
  return tris;
}

/** 花盆：外筒 + 内筒 + 底 */
function flowerPot(p) {
  const segments = 48;
  const rOuter = p.diameter / 2;
  const rInner = Math.max(rOuter - p.wall, 1);
  const height = p.height;
  const bottom = p.bottom;
  const tris = [];

  const ring = (radius, y) =>
    Array.from({ length: segments }, (_, i) => {
      const a = (i / segments) * Math.PI * 2;
      return [Math.cos(a) * radius, y, Math.sin(a) * radius];
    });

  const outBottom = ring(rOuter, 0);
  const outTop = ring(rOuter, height);
  const inBottom = ring(rInner, bottom);
  const inTop = ring(rInner, height);

  for (let i = 0; i < segments; i += 1) {
    const j = (i + 1) % segments;
    // 外壁
    tris.push([outBottom[i], outTop[i], outTop[j]], [outBottom[i], outTop[j], outBottom[j]]);
    // 内壁（法线朝内）
    tris.push([inBottom[i], inTop[j], inTop[i]], [inBottom[i], inBottom[j], inTop[j]]);
    // 顶圈
    tris.push([outTop[i], inTop[i], inTop[j]], [outTop[i], inTop[j], outTop[j]]);
    // 盆底上表面
    tris.push([inBottom[i], [0, bottom, 0], inBottom[j]]);
    // 盆底下表面
    tris.push([outBottom[i], outBottom[j], [0, 0, 0]]);
  }
  return tris;
}

/** 收纳盒：四壁 + 底（无盖，带圆角外框的简化实现） */
function storageBox(p) {
  const { length: L, width: W, height: H, thickness: t } = p;
  const tris = [];
  // 底
  tris.push(...boxQuads(-L / 2, 0, -W / 2, L / 2, t, W / 2));
  // 前后壁
  tris.push(...boxQuads(-L / 2, t, -W / 2, L / 2, H, -W / 2 + t));
  tris.push(...boxQuads(-L / 2, t, W / 2 - t, L / 2, H, W / 2));
  // 左右壁
  tris.push(...boxQuads(-L / 2, t, -W / 2 + t, -L / 2 + t, H, W / 2 - t));
  tris.push(...boxQuads(L / 2 - t, t, -W / 2 + t, L / 2, H, W / 2 - t));
  return tris;
}

const PARAM_DEFS = {
  // 参数表照真实项目写：除了外形尺寸，还有穿线孔、挡边高度、圆角、防滑柱数量……
  // 正好覆盖分栏的五个类别，验收脚本要靠它验证分栏（真实项目里参数最多的
  // 也是这个「桌面手机支架」：16 个）。
  phone_stand: [
    { name: 'width', label: '宽度', value: 80, unit: 'mm', min: 40, max: 200 },
    { name: 'height', label: '高度', value: 100, unit: 'mm', min: 50, max: 260 },
    { name: 'depth', label: '底座深度', value: 70, unit: 'mm', min: 40, max: 180 },
    { name: 'lip_height', label: '前挡边高度', value: 12, unit: 'mm', min: 6, max: 30 },
    { name: 'angle', label: '倾角', value: 65, unit: '°', min: 30, max: 85 },
    { name: 'thickness', label: '板厚', value: 5, unit: 'mm', min: 2, max: 15 },
    { name: 'cable_hole_diameter', label: '穿线孔直径', value: 8, unit: 'mm', min: 4, max: 20 },
    { name: 'fillet_radius', label: '圆角半径', value: 2, unit: 'mm', min: 0, max: 8 },
    { name: 'grip_post_count', label: '防滑柱数量', value: 6, unit: '个', min: 2, max: 12 },
  ],
  flower_pot: [
    { name: 'diameter', label: '外径', value: 80, unit: 'mm', min: 30, max: 300 },
    { name: 'height', label: '高度', value: 90, unit: 'mm', min: 20, max: 300 },
    { name: 'wall', label: '壁厚', value: 3, unit: 'mm', min: 1.2, max: 12 },
    { name: 'bottom', label: '底厚', value: 4, unit: 'mm', min: 1.5, max: 20 },
  ],
  storage_box: [
    { name: 'length', label: '长度', value: 60, unit: 'mm', min: 20, max: 300 },
    { name: 'width', label: '宽度', value: 40, unit: 'mm', min: 20, max: 300 },
    { name: 'height', label: '高度', value: 20, unit: 'mm', min: 10, max: 200 },
    { name: 'thickness', label: '壁厚', value: 2.4, unit: 'mm', min: 1.2, max: 10 },
  ],
};

const KINDS = {
  phone_stand: { build: phoneStand, title: '桌面手机支架', summary: '按你的尺寸生成的桌面手机支架：底部配重板加前挡边，后靠背带倾角，可直接打印。' },
  flower_pot: { build: flowerPot, title: '多肉花盆', summary: '直筒花盆，壁厚均匀、底部带排水面，适合多肉与小型绿植。' },
  storage_box: { build: storageBox, title: '收纳盒', summary: '无盖收纳盒，四壁等厚、底面平整，可直接打印或加装盖子。' },
};

// 由 aiforge.threemf 生成的合法 3MF（10×20×30 长方体，声明 unit=millimeter），
// 用于 mock 的 /api/projects/{id}/3mf —— 保证验收脚本拿到的是真能解开的包。
const MOCK_3MF_B64 =
  'UEsDBBQAAAAIAEyzM13uVq7YxwAAAEYBAAATAAAAW0NvbnRlbnRfVHlwZXNdLnhtbIWQTWrDMBCF9z6F0DbYchMopdjOoklPkB5gkMc/RBoJaRzS22ectBQKpcvH++Z9MM3+6p26YMpzoFY/VbVWSDb0M42t/ji9ly963xXN6TNiVsJSbvXEHF+NyXZCD7kKEUmaISQPLDGNJoI9w4hmW9fPxgZiJC553dBdoVRzwAEWx+p4leahTuiyVm8PdtW1GmJ0swWW3lyo/yUqvySVXN6ZPM0xbwTQ5i+JDz26fyz+Z3nXe6BlAMtLkoeUkteBb0dj7m/pihtQSwMEFAAAAAgATLMzXdMqNku1AAAACAEAAAsAAABfcmVscy8ucmVsc2WPzQrCQAyE732KJXeb1oKIdOtFBK9SH2DZpj/Y/WF3FX17oyBYvARCMt/M1PuHmcWdQpyclVDmBQiy2nWTHSRc2uNqC/smq880q8QvcZx8FKyxUcKYkt8hRj2SUTF3nixfeheMSryGAb3SVzUQrotig+GXAU0mxAIrWhUGShKwOmDVGdfRnH8miFMngeUcrn16+nM2kw4uuj7l2pm3Vtlbr3S6Ba7B5mWFRfllAnIfXBRqshdQSwMEFAAAAAgATLMzXVIyyDB3AQAAugQAABAAAAAzRC8zZG1vZGVsLm1vZGVspZTLboMwEEX3/QrL+2AeeVQREHXTVStVavIBjpk0bv2IjEFJv742jlRQgpoqbLjAmZlrzCVfHaVALZiaa1XgJIoxAsV0xdVHgTfr58kjXpUPudQVCNQobgssuRBcggWDkateCupZUJPNe3dD1QXeW3tYElKzPUhaR5Izo2u9sxHTkmSVpKrZUWYb4+YQpg2QNE5mJE6xG2ag1o1hUDutt5/ALOKVM4eRPR3AGfBmMFJUdhfsyxdJqPfu5JZieSj1Eo7oWOA4iv2B0amnv381GdDJHXj6N38H3qez/7q5peAG/rb+pLcP1nD3hYiBRm3iWmHUpt2+tplr1hVeA9IAZJfANACzAMxHgUUAZqMjsgAsRoFzh+klkARgPjoiGZgcX+Z0dBXxYERyCWSDF3XFQzbwEJZJ+htDzvkhIXBe9WPo4k4ramlPnvO35lYALn0MfZMx7JW6/wWnApdvL08DsK+3DRdVmXMLEgUjIfikzEl45nkf//IHUEsBAhQAFAAAAAgATLMzXe5WrtjHAAAARgEAABMAAAAAAAAAAAAAAIABAAAAAFtDb250ZW50X1R5cGVzXS54bWxQSwECFAAUAAAACABMszNd0yo2S7UAAAAIAQAACwAAAAAAAAAAAAAAgAH4AAAAX3JlbHMvLnJlbHNQSwECFAAUAAAACABMszNdUjLIMHcBAAC6BAAAEAAAAAAAAAAAAAAAgAHWAQAAM0QvM2Rtb2RlbC5tb2RlbFBLBQYAAAAAAwADALgAAAB7AwAAAAA=';

/** mock 内置一台通用 FDM，供 /api/printers* 返回（前端机型胶囊/设置页需要） */
const MOCK_PRINTER = {
  id: 'generic-fdm-220',
  name: '通用 FDM 打印机 220×220×250',
  manufacturer: 'Generic',
  nozzle_mm: 0.4,
  build_x: 220,
  build_y: 220,
  build_z: 250,
  build_volume_mm: { x: 220, y: 220, z: 250, volume_cm3: 12.1 },
  nozzle_options: [0.4, 0.6],
  layer_min_mm: 0.08,
  layer_max_mm: 0.32,
  max_speed_mm_s: 150,
  max_accel_mm_s2: 2000,
  max_flow_mm3_s: 12,
  has_heated_bed: true,
  max_bed_temp_c: 100,
  max_hotend_temp_c: 260,
  enclosure: false,
  materials: ['PLA', 'PETG', 'TPU'],
  filament_diameter_mm: 1.75,
  extruder: 'direct',
  bed_leveling: 'abl',
  bed_surface: 'PEI',
  kinematics: 'cartesian',
  recommended_max_overhang_deg: 45,
  recommended_max_bridge_mm: 10,
  dimensional_tolerance_mm: 0.2,
  notes: 'mock 内置机型',
  builtin: true,
  sources: '经验值',
  updated_at: '',
  constraints: {
    min_wall_mm: 1.2,
    min_hole_mm: 0.8,
    min_feature_mm: 0.8,
    max_overhang_deg: 45,
    max_bridge_mm: 10,
    min_layer_mm: 0.08,
    max_layer_mm: 0.32,
  },
  checklist: ['壁厚 ≥1.2mm', '孔径 ≥0.8mm', '悬垂 ≤45°'],
  recommended_fn_examples: { '50mm': 96, '150mm': 128 },
};

/**
 * 切片软件：形状严格照 src/aiforge/slicers.py 的 describe()。
 * 两条数据就是本机实测结果（Bambu Studio 来自「已安装程序」的 DisplayIcon、
 * OrcaSlicer 来自「常见安装目录」，文件关联那一条在本机是空的）——
 * mock 不编“更干净”的样子，否则界面验出来的是假环境里才有的布局。
 * MOCK_SLICERS=none 起一台“什么都没装”的机器，用来验负分支。
 */
const MOCK_SLICER_NAMES = [
  'Bambu Studio',
  'OrcaSlicer',
  'PrusaSlicer',
  'UltiMaker Cura',
  'SuperSlicer',
  'Creality Print',
  'ElegooSlicer',
  'Anycubic Slicer',
  'CHITUBOX',
  'Lychee Slicer',
];

const slicerState = {
  none: process.env.MOCK_SLICERS === 'none',
  found: [
    {
      id: 'bambu_studio',
      name: 'Bambu Studio',
      exe: 'D:\\Bambu Studio\\bambu-studio.exe',
      version: '02.08.02.61',
      source_label: '已安装程序',
      running: false,
    },
    {
      id: 'orcaslicer',
      name: 'OrcaSlicer',
      exe: 'D:\\orcaslicer\\orca-slicer.exe',
      version: null,
      source_label: '常见安装目录',
      running: false,
    },
  ],
};

function slicersDescribe() {
  const items = slicerState.none ? [] : slicerState.found;
  const wanted = state.settings.default_slicer;
  // 与真后端同一道把关：记过的软件没检测到时回到「没有默认」，而不是发一个幽灵 id
  const defaultId = wanted && items.some((s) => s.id === wanted) ? wanted : null;
  return {
    slicers: items,
    default_id: defaultId,
    count: items.length,
    recognized_names: MOCK_SLICER_NAMES,
  };
}

function buildModel(kind, params) {
  const def = KINDS[kind] ? kind : 'phone_stand';
  const tris = KINDS[def].build(params);
  return { kind: def, triangles: tris, stl: trianglesToStl(tris), metrics: measure(tris) };
}

/* ================================================================== *
 * 内存状态
 * ================================================================== */

const sleepers = new Set(); // 保留给将来需要人为延迟的场景
void sleepers;

function makeParams(kind, overrides = {}) {
  const out = {};
  for (const p of PARAM_DEFS[kind]) out[p.name] = overrides[p.name] ?? p.value;
  return out;
}

function scadSource(kind, params, name) {
  const lines = PARAM_DEFS[kind].map((p) => `${p.name} = ${params[p.name]}; // ${p.label}`);
  return `// ${name} —— AI 3D Forge 生成的参数化源码\n// 修改下面的数值即可重新生成模型（也可以直接在界面里改参数）\n\n${lines.join('\n')}\n\nmodule ${kind}() {\n  // 参数已在上方注入\n}\n\n${kind}();\n`;
}

function parseScadParams(scad) {
  const out = {};
  for (const line of scad.split('\n')) {
    const m = /^(\w+)\s*=\s*(-?[\d.]+)\s*;/.exec(line.trim());
    if (m) out[m[1]] = Number(m[2]);
  }
  return out;
}

const state = {
  /** 回收站：[{ item, project }]。删项目不真删，与后端 `Projects/.deleted` 同构，
   *  这样「设置页列出/恢复/彻底删除」这条新链路在 mock 上也是真跑得动的。 */
  trash: [],
  /** 修改类作业的核对结论：ok=改得干净 / bad=没改到位还顺手改坏别处 */
  verdictKind: process.env.MOCK_VERDICT === 'bad' ? 'bad' : 'ok',
  /** 「送到切片软件」的返回场景：ok / forwarded / failed / none（错误分支靠它验） */
  slicerScenario: 'ok',
  /** 这台机器有没有 WebView2：默认没（否则页面顶部那条回退横幅永远验不到） */
  windowRuntimeInstalled: false,
  settings: {
    api_key_set: false,
    api_key_masked: null,
    api_key_store: 'credential_manager',
    base_url: 'https://api.deepseek.com',
    model: 'deepseek-chat',
    openscad_path: null,
    data_dir: 'C:\\Users\\dev\\AppData\\Roaming\\AI-3D-Forge',
    projects_dir: 'C:\\Users\\dev\\Documents\\AI-3D-Forge\\Projects',
    // 日志与配置不同目录（漫游 vs 可重建），设置页「文件位置」要显出来
    log_dir: 'C:\\Users\\dev\\AppData\\Local\\AI-3D-Forge\\logs',
    keep_stl_history: true,
    auto_install_openscad: true,
    first_run_done: !FIRST_RUN,
    theme: 'light',
    // 与真后端同一套默认值与约束（见 src/aiforge/config.py）：
    // 假后端不镜像校验，界面就只能验出「自己骗自己」的绿灯。
    appearance: 'solid',
    glass_transparency: 25,
    confirm_spec: true,
    announce_in_chat: true,
    // 送到切片软件：只存 id，路径每次实扫（与 config.py 的 SlicerSettings 一致）
    default_slicer: null,
    slicer_custom_paths: [],
  },
  openscad: {
    found: false,
    path: null,
    version: null,
    source: null,
    candidates: [],
    cli_ok: false,
    detail: '未检测到 3D 引擎（OpenSCAD）。',
  },
  projects: [],
  jobs: new Map(),
  installJobs: new Map(),
};

function nowIso() {
  return new Date().toISOString();
}

function createProject({ name, kind = 'phone_stand', params, revision = 1, prompt = null, summary = null }) {
  const id = randomUUID();
  const p = params ?? makeParams(kind);
  const model = buildModel(kind, p);
  const project = {
    id,
    name,
    kind,
    created_at: nowIso(),
    updated_at: nowIso(),
    revision,
    params: p,
    prompt,
    summary: summary ?? KINDS[kind].summary,
    last_status: 'ok',
  };
  state.projects.unshift(project);
  modelCache.set(id, { revision, stl: model.stl, metrics: model.metrics });
  return project;
}

/** projectId → { revision, stl, metrics } */
const modelCache = new Map();

function getModel(project) {
  const cached = modelCache.get(project.id);
  if (cached && cached.revision === project.revision) return cached;
  const model = buildModel(project.kind, project.params);
  const entry = { revision: project.revision, stl: model.stl, metrics: model.metrics };
  modelCache.set(project.id, entry);
  return entry;
}

/** 材料密度表（与 src/aiforge/printers.py 的 MATERIAL_DENSITY 同一组数字） */
const MATERIAL_DENSITY = { PLA: 1.24, 'PLA-CF': 1.21, PETG: 1.27, ABS: 1.04, ASA: 1.07, TPU: 1.21, PA: 1.14 };

/**
 * 耗材用量估算，算法照 src/aiforge/mesh.py::estimate_usage：
 *   外壳体积 ≈ 表面积 × 壁厚（壁厚 = 墙圈数 × 走线宽度），内部按填充率。
 *
 * 为什么 mock 必须自己算一份：假后端此前根本不产 usage，
 * 「预计耗材」那一整块界面在验收里永远不出现（实测：重开项目时结果卡
 * 只显示 0.0 × 0.0 × 0.0 / 0 B，就是因为 /metrics 这条路 mock 没有）。
 */
function estimateUsage(
  metrics,
  {
    material = 'PLA',
    wallLines = 2,
    lineWidth = 0.4,
    infillPct = 15,
    filamentDiameter = 1.75,
  } = {},
) {
  const density = Math.max(0.01, MATERIAL_DENSITY[material] ?? 1.24);
  const solidCm3 = metrics.volume_mm3 / 1000.0;
  const shellThickness = Math.max(0, wallLines * lineWidth);
  const shellCm3 = Math.min((metrics.surface_area_mm2 * shellThickness) / 1000.0, solidCm3);
  const coreCm3 = Math.max(0, solidCm3 - shellCm3);
  const printedCm3 = shellCm3 + coreCm3 * Math.min(1, Math.max(0, infillPct / 100.0));
  // mock 的两个夹具都没有悬垂（花盆直壁、支架靠背自支撑），支撑量按 0 算
  const supportCm3 = 0;
  const supportPrintedCm3 = 0;
  const crossSection = Math.PI * (filamentDiameter / 2) ** 2;
  const totalCm3 = printedCm3 + supportPrintedCm3;
  const grams = totalCm3 * density;
  const r = (v, n) => Number(v.toFixed(n));
  const notes = [
    `按「${wallLines} 层墙 × ${lineWidth}mm 走线 + ${infillPct}% 填充」估算；换切片参数会变。`,
    '支撑量由悬垂面往下打射线量到下方表面得到，仍是偏大的上界，不是切片精确值。',
    '该朝向不需要支撑。',
  ];
  if (solidCm3 > 0 && printedCm3 / solidCm3 < 0.35) {
    const pct = ((printedCm3 / solidCm3) * 100).toFixed(0);
    notes.push(`实体等效体积为 ${solidCm3.toFixed(1)}cm³，按填充估算约为它的 ${pct}%。`);
  }
  return {
    material,
    density_g_cm3: r(density, 3),
    model_solid_cm3: r(solidCm3, 2),
    model_printed_cm3: r(printedCm3, 2),
    model_grams: r(printedCm3 * density, 1),
    support_cm3: r(supportCm3, 2),
    support_grams: r(supportPrintedCm3 * density, 1),
    total_grams: r(grams, 1),
    // 上限按实体等效（100% 填充）——备料用这个，与后端口径一致
    total_grams_upper: r((solidCm3 + supportPrintedCm3) * density, 1),
    filament_length_m: crossSection ? r((totalCm3 * 1000.0) / crossSection / 1000.0, 2) : 0,
    support_ratio_pct: solidCm3 > 0 ? r((supportCm3 / solidCm3) * 100, 1) : 0,
    filament_diameter_mm: filamentDiameter,
    assumptions: { wall_lines: wallLines, line_width_mm: lineWidth, infill_pct: infillPct },
    notes,
  };
}

/**
 * GET /api/projects/{id}/metrics 的载荷。
 *
 * 真实后端由 api/projects_routes.py 现场分析 STL 得出，形状是
 * MeshMetrics.to_dict() + size_bytes / material / requirement_tests。
 * 这条路 mock 之前完全没有，导致「重开历史项目 → 合成结果卡」整条路径
 * 测不到，界面上就是一堆 0。
 */
function projectMetricsPayload(p) {
  const model = getModel(p);
  const bbox = model.metrics.bbox;
  const thin = Math.min(...Object.values(p.params).filter((v) => v > 0 && v < 40));
  const usage = estimateUsage(model.metrics);
  return {
    triangles: model.metrics.triangles,
    vertices: model.metrics.triangles * 3,
    bbox: {
      x: Number(bbox.x.toFixed(2)),
      y: Number(bbox.y.toFixed(2)),
      z: Number(bbox.z.toFixed(2)),
    },
    bbox_min: model.metrics.bbox_min,
    bbox_max: model.metrics.bbox_max,
    volume_mm3: Number(model.metrics.volume_mm3.toFixed(2)),
    volume_cm3: Number((model.metrics.volume_mm3 / 1000).toFixed(3)),
    surface_area_mm2: Number(model.metrics.surface_area_mm2.toFixed(2)),
    watertight: true,
    watertight_ratio: 1,
    boundary_edges: 0,
    non_manifold_edges: 0,
    degenerate_triangles: 0,
    sits_on_plate: Math.abs(model.metrics.bbox_min[2]) < 0.05,
    solid_parts: 1,
    stray_parts: 0,
    stray_details: [],
    cavity_islands: [],
    wall: null,
    hole_diameter_mm: null,
    hole_profile: [],
    support: null,
    orientation_hint: null,
    usage,
    issues: [],
    // 以下这几项是 /metrics 路由从文件与缓存里补的（MeshMetrics 本身不含）
    size_bytes: model.stl.length,
    material: usage.material,
    material_density: usage.density_g_cm3,
    within_build_volume:
      bbox.x <= MOCK_PRINTER.build_x + 0.01 &&
      bbox.y <= MOCK_PRINTER.build_y + 0.01 &&
      bbox.z <= MOCK_PRINTER.build_z + 0.01,
    min_dimension_mm: Number(thin.toFixed(2)),
    meets_min_wall: thin >= 1.2,
    requirement_tests: requirementTests(p, bbox, thin, model.metrics.triangles),
  };
}

function projectSummary(p) {
  return {
    id: p.id,
    name: p.name,
    created_at: p.created_at,
    updated_at: p.updated_at,
    revision: p.revision,
    thumbnail_url: `/api/projects/${p.id}/thumbnail`,
    stl_url: `/api/projects/${p.id}/stl`,
    summary: p.summary,
    last_status: p.last_status,
  };
}

function projectDetail(p) {
  return {
    ...projectSummary(p),
    scad: scadSource(p.kind, p.params, p.name),
    params: PARAM_DEFS[p.kind].map((def) => ({ ...def, value: p.params[def.name] })),
    history: [
      {
        revision: p.revision,
        prompt: p.prompt,
        instruction: null,
        created_at: p.updated_at,
        summary: p.summary ?? KINDS[p.kind].summary,
      },
    ],
    files: {
      scad_path: `${state.settings.projects_dir}\\${p.name}\\model.scad`,
      stl_path: `${state.settings.projects_dir}\\${p.name}\\model.stl`,
      export_dir: `${state.settings.projects_dir}\\${p.name}\\exports`,
    },
    // 真实后端的项目详情里带着上次实测数据（重开项目时的耗材估算只认这一项）
    metrics: projectMetricsPayload(p),
  };
}

/**
 * 需求核对表（形状与 src/aiforge/reqtests.py 逐字段一致）。
 *
 * mock 必须也产这份数据：界面只有拿到 requirement_tests 才渲染那一块，
 * 假后端不产它，界面验收就永远测不到这条路径（真后端跑一次既慢又花钱）。
 */
function requirementTests(project, bbox, thin, triangles) {
  const wallMin = 1.2;
  const dim = (group, name, want, got) => {
    const dev = want ? ((got - want) / want) * 100 : 0;
    const tol = Math.max(wallMin / 5, want * 0.02);
    return {
      group: `dim:${name}`,
      requirement: `${group}（实测）`,
      kind: 'dimension',
      status: Math.abs(got - want) <= tol ? 'pass' : 'fail',
      expected: `${want}mm`,
      actual: `${Number(got.toFixed(1))}mm`,
      deviation_pct: Number(dev.toFixed(2)),
      tolerance_mm: Number(tol.toFixed(2)),
      fix_hint: Math.abs(got - want) <= tol ? '' : '让该尺寸直接由对应参数决定，不要只写某一段的高度。',
      advisory: false,
    };
  };
  const tests = [
    {
      group: 'integrity:one-body',
      requirement: '模型是一整件（不需要自己拼）',
      kind: 'integrity',
      status: 'pass',
      expected: '1 个实体',
      actual: '1 个实体（散件 0 个）',
      deviation_pct: null,
      tolerance_mm: null,
      fix_hint: '',
      advisory: false,
    },
    {
      group: 'integrity:watertight',
      requirement: '网格封闭（切片软件能判断内外）',
      kind: 'integrity',
      status: 'pass',
      expected: '封闭、开放边 ≤10 条',
      actual: '封闭=是，开放边 0 条',
      deviation_pct: null,
      tolerance_mm: null,
      fix_hint: '',
      advisory: false,
    },
    dim('总高', 'height', Number(project.params.height) || Number(bbox.z) || 0, Number(bbox.z) || 0),
    {
      group: 'print:min-wall',
      requirement: `壁厚 ≥${wallMin}mm`,
      kind: 'print',
      status: thin < wallMin ? 'fail' : 'pass',
      expected: `≥${wallMin}mm`,
      actual: `p05 ${Number(thin.toFixed(2))}mm`,
      deviation_pct: null,
      tolerance_mm: null,
      fix_hint: thin < wallMin ? '加厚壁或加筋；壁厚由那个参数决定。' : '',
      advisory: true,
    },
    {
      group: 'print:triangle-budget',
      requirement: '面数在切片软件舒服的范围',
      kind: 'print',
      status: 'pass',
      expected: '≤400000 三角面',
      actual: `${(triangles ?? 0).toLocaleString('zh-CN')} 三角面`,
      deviation_pct: null,
      tolerance_mm: null,
      fix_hint: '',
      advisory: true,
    },
    {
      group: 'semantic:feature-1',
      requirement: '结构符合需求描述（如把手、孔位）',
      kind: 'semantic',
      status: 'manual',
      expected: '结构存在且位置正确',
      actual: '程序无法测量，已由外观复核/人工确认',
      deviation_pct: null,
      tolerance_mm: null,
      fix_hint: '',
      advisory: false,
    },
  ];
  const scored = tests.filter((t) => t.status !== 'manual');
  const groups = new Set(scored.map((t) => t.group));
  const badGroups = new Set(scored.filter((t) => t.status !== 'pass').map((t) => t.group));
  const counts = tests.reduce(
    (acc, t) => ({ ...acc, [t.status]: (acc[t.status] || 0) + 1 }),
    { pass: 0, fail: 0, invalid: 0, manual: 0 },
  );
  return {
    pass_rate: badGroups.size === 0 ? 1 : 0,
    requirement_score: groups.size ? Number(((groups.size - badGroups.size) / groups.size).toFixed(4)) : 1,
    invalid_ratio: 0,
    counts,
    groups: groups.size,
    manual: counts.manual || 0,
    tests,
  };
}

function jobResult(project, job) {
  const model = getModel(project);
  const params = PARAM_DEFS[project.kind].map((def) => ({
    ...def,
    value: project.params[def.name],
  }));
  const bbox = model.metrics.bbox;
  const thin = Math.min(...Object.values(project.params).filter((v) => v > 0 && v < 40));
  const usage = estimateUsage(model.metrics);
  const issues = [];
  if (project.kind === 'phone_stand') {
    issues.push({
      severity: 'info',
      title: '后靠背倾角适合大多数手机',
      detail: `当前倾角 ${project.params.angle}°，配合前挡边可以稳定放置厚度 8-12mm 的手机。`,
      suggestion: '如果手机带厚壳，可以把「底座深度」加大到 80mm 以上。',
    });
    if (project.params.thickness < 3) {
      issues.push({
        severity: 'warn',
        title: '板厚偏薄',
        detail: `当前板厚 ${project.params.thickness}mm，长期受力可能变形。`,
        suggestion: '建议把「板厚」调到 3mm 以上。',
      });
    }
  }
  if (project.kind === 'flower_pot' && project.params.wall < 2) {
    issues.push({
      severity: 'warn',
      title: '盆壁偏薄',
      detail: `壁厚 ${project.params.wall}mm，打印时容易出现缝隙。`,
      suggestion: '建议壁厚不小于 2mm。',
    });
  }
  issues.push({
    severity: 'info',
    title: '无需支撑',
    detail: '模型的悬空角度都在 45° 以内，切片时不需要额外支撑。',
    suggestion: '直接按默认参数切片即可。',
  });

  const score = Math.max(60, 96 - issues.filter((i) => i.severity === 'warn').length * 9);
  const verdict = issues.some((i) => i.severity === 'warn') ? 'warn' : 'pass';

  return {
    project_id: project.id,
    revision: project.revision,
    name: project.name,
    scad: scadSource(project.kind, project.params, project.name),
    params,
    summary: job.summary ?? KINDS[project.kind].summary,
    stl: {
      url: `/api/projects/${project.id}/stl`,
      filename: `${project.name}.stl`,
      size_bytes: model.stl.length,
      triangles: model.metrics.triangles,
      volume_mm3: model.metrics.volume_mm3,
      bbox_mm: bbox,
      watertight: true,
      watertight_ratio: 1,
      /* 契约 §3A：作业结果里的 stl 必须带这一组打印机相关字段。
         之前 mock 只给前 9 项，「预计耗材」「能不能放进这台机器」在
         假后端下根本没数据可渲染，这两块界面从来没被验收跑过。 */
      material: usage.material,
      material_density: usage.density_g_cm3,
      within_build_volume:
        bbox.x <= MOCK_PRINTER.build_x + 0.01 &&
        bbox.y <= MOCK_PRINTER.build_y + 0.01 &&
        bbox.z <= MOCK_PRINTER.build_z + 0.01,
      min_dimension_mm: Number(thin.toFixed(2)),
      meets_min_wall: thin >= 1.2,
      usage,
    },
    printability: {
      score,
      verdict,
      summary:
        verdict === 'pass'
          ? '整体结构稳定，按默认参数可以直接打印。'
          : '基本可以打印，建议先按下面的提示调整一两个参数。',
      issues,
      metrics_note: `最小特征尺寸约 ${thin.toFixed(1)}mm，建议喷嘴 0.4mm、层高 0.2mm。`,
    },
    // 逐条需求核对（真实后端由 aiforge/reqtests.py 产出，mock 必须同形状）
    requirement_tests: requirementTests(project, bbox, thin, model.metrics.triangles),
    diagnostics: [
      `引擎: OpenSCAD ${state.openscad.version ?? '2021.01'}`,
      `编译: 成功（${(model.metrics.triangles / 100).toFixed(0)} 个面片）`,
      `体积: ${model.metrics.volume_mm3.toFixed(1)} mm³`,
      `水密: 是（闭合网格）`,
    ].join('\n'),
    // 修改类作业必须带上改动核对结论 —— 界面靠它说明「这次到底改了什么」。
    // 假后端两种结论都要能造出来（改得好 / 顺手改坏别处），否则这条路径测不全。
    ...(job.kindOfRequest === 'modify'
      ? job.verdictKind === 'bad'
        ? {
            change_verdict: {
              ok: false,
              performed: false,
              unauthorized: ['底座宽度'],
              spec_intact: false,
              fingerprint_kept: false,
              reasons: ['你要改的高度没改到位（目标 150mm，实际 118mm）', '顺带改了「底座宽度」'],
              target_misses: { height: 0.213 },
            },
            change_summary: '高度没改到位，而且顺带改了底座宽度。',
          }
        : {
            change_verdict: {
              ok: true,
              performed: true,
              unauthorized: [],
              spec_intact: true,
              fingerprint_kept: true,
              reasons: ['只改了「高度」，其余参数一个都没动'],
              target_misses: {},
            },
            change_summary: '高度已按你的要求改成 150mm，其它尺寸保持不变。',
          }
      : {}),
  };
}

/* ================================================================== *
 * 作业脚本（SSE 序列）
 * ================================================================== */

function scriptForJob(project, kindOfRequest) {
  const stage1 = kindOfRequest === 'modify' ? '正在理解修改要求…' : '正在理解你的需求…';
  const stage2 = kindOfRequest === 'params' ? '正在应用新的参数…' : '正在设计结构…';
  // 只有「从零设计」才走规格确认关卡（与真实后端 `_should_confirm()` 的策略一致）
  const gate = kindOfRequest === 'create';
  return [
    { at: 0, event: 'stage', data: { stage: 'thinking', percent: 6 } },
    { at: 120, event: 'message', data: { seq: 1, text: stage1, detail: null } },
    ...(gate
      ? [
          { at: 400, event: 'stage', data: { stage: 'understanding', percent: 10 } },
          { at: 700, event: 'message', data: { seq: 2, text: '需求里给了具体尺寸，先请你确认一下。', detail: null } },
          {
            at: 900,
            gate: true,
            event: 'spec',
            data: { spec: mockSpec(project), awaiting_confirmation: true },
          },
        ]
      : []),
    { at: 900, event: 'stage', data: { stage: 'generating_scad', percent: 24 } },
    { at: 1000, event: 'message', data: { seq: 3, text: stage2, detail: null } },
    { at: 1900, event: 'stage', data: { stage: 'validating_scad', percent: 42 } },
    { at: 2000, event: 'message', data: { seq: 4, text: '正在检查设计是否安全…', detail: null } },
    { at: 2700, event: 'stage', data: { stage: 'compiling', percent: 60 } },
    { at: 2800, event: 'message', data: { seq: 5, text: '正在生成三维模型…', detail: null } },
    { at: 3600, event: 'stage', data: { stage: 'analyzing', percent: 76 } },
    { at: 3700, event: 'message', data: { seq: 6, text: '正在测量尺寸与体积…', detail: null } },
    { at: 4400, event: 'stage', data: { stage: 'reviewing', percent: 90 } },
    { at: 4500, event: 'message', data: { seq: 7, text: '正在评估可打印性…', detail: null } },
    {
      at: 5200,
      event: 'done',
      // 契约：done 的 data 是完整 Job（含 result）
      build: (job) => ({
        job_id: job.id,
        project_id: project.id,
        revision: project.revision,
        status: 'done',
        stage: 'done',
        percent: 100,
        messages: job.messages,
        error: null,
        result: jobResult(project, job),
        created_at: job.created_at,
        finished_at: nowIso(),
      }),
    },
  ];
}

/** 假的设计规格：结构必须与真实后端 `DesignSpec.to_display()` 完全一致 */
function mockSpec(project) {
  // project.params 是「参数名 → 值」的对象（与真实后端一致），不是数组
  const bag = project.params || {};
  const value = (name) => (typeof bag[name] === 'number' ? bag[name] : null);
  const item = (name, label, given, unit = 'mm') => ({
    name,
    label,
    value: value(name),
    unit,
    min: 20,
    max: 300,
    user_specified: given,
    display: `${label}：${value(name) ?? '未指定'}mm${given ? '  ← 你指定' : ''}`,
  });
  return {
    kind: project.kind || 'phone_stand',
    summary: '一个带背部支撑的桌面手机支架',
    specs: [
      item('height', '支架高度', true),
      item('width', '底座宽度', true),
      item('thickness', '底座厚度', false),
      item('angle', '屏幕倾角', false, '°'),
    ],
    features: ['背部挡板', '前唇防滑', '底部平整可站立'],
    prohibitions: ['不要镂空底座'],
    acceptance: ['能稳定支撑 6 英寸手机', '整体可一次打印成型'],
    open_questions: ['壁厚没指定，我按你的机型取了 1.6mm', '没说要圆角还是直角，先按 2mm 圆角做'],
    clarifications: [],
    fingerprint: 'mock0000feed',
  };
}

function startJob({ project, kindOfRequest, summary }) {
  const id = randomUUID();
  const job = {
    id,
    project_id: project.id,
    revision: project.revision,
    created_at: nowIso(),
    kindOfRequest,
    summary,
    messages: [],
    script: scriptForJob(project, kindOfRequest),
    shift: 0,
    paused: false,
    confirmed: false,
    spec: null,
    // 允许逐次覆盖核对结论，方便一次跑完「改得干净」和「改坏了」两种界面
    verdictKind: globalThis.__mockVerdictKind || state.verdictKind,
  };
  console.log(
    `   [mock] 新作业 ${id.slice(0, 8)}（${kindOfRequest}）脚本 ${job.script.length} 步，` +
      `含规格关卡=${job.script.some((s) => s.gate)}`
  );
  state.jobs.set(id, job);
  return id;
}

/** 依当前时间推进脚本，返回已经发生的帧 */
function jobFrames(job) {
  const elapsed = Date.now() - new Date(job.created_at).getTime();
  const frames = [];
  for (const step of job.script) {
    if (step.at > elapsed) break;
    if (step.event === 'message') job.messages.push(step.data);
    frames.push({
      event: step.event,
      data: typeof step.build === 'function' ? step.build(job) : step.data,
    });
  }
  return frames;
}

function jobSnapshot(job) {
  const project = state.projects.find((p) => p.id === job.project_id);
  const elapsed = Date.now() - new Date(job.created_at).getTime() - (job.shift || 0);
  const last = [...job.script].reverse().find((s) => s.at <= elapsed && s.event !== 'done');
  const doneStep = job.script.find((s) => s.event === 'done');
  // 等待用户确认需求理解：这是一个**非终态**，前端不能据此停止订阅。
  const waiting = Boolean(job.paused) && !job.confirmed;
  // 暂停在关卡上时，时间轴**不能**继续往前走：否则作业会在"等人确认"的状态下
  // 被时间轴判成 finished，快照报 done（约 5 秒后就发生）——
  // 于是"停在确认卡片上"这件事根本撑不到用户回来，
  // 「切页/刷新后能否重新接管作业」这条路径在假后端上永远测不出来。
  const finished = !waiting && Boolean(doneStep) && doneStep.at <= elapsed;
  const wasCancelled = Boolean(job.cancelled);

  if (wasCancelled) {
    return {
      job_id: job.id,
      project_id: job.project_id,
      revision: job.revision,
      status: 'cancelled',
      stage: 'cancelled',
      percent: 0,
      messages: job.messages,
      error: null,
      result: null,
      spec: job.spec || null,
      created_at: job.created_at,
      finished_at: nowIso(),
    };
  }

  if (finished) {
    return {
      job_id: job.id,
      project_id: job.project_id,
      revision: job.revision,
      status: 'done',
      stage: 'done',
      percent: 100,
      messages: job.messages,
      error: null,
      result: jobResult(project, job),
      spec: job.spec || null,
      created_at: job.created_at,
      finished_at: new Date(new Date(job.created_at).getTime() + doneStep.at).toISOString(),
    };
  }
  const stage = waiting ? 'awaiting_confirmation' : last?.event === 'stage' ? last.data.stage : 'queued';
  const percent = waiting ? 12 : last?.event === 'stage' ? last.data.percent : 2;
  return {
    job_id: job.id,
    project_id: job.project_id,
    revision: job.revision,
    status: waiting ? 'awaiting_confirmation' : 'running',
    stage,
    percent,
    messages: job.messages,
    error: null,
    result: null,
    spec: job.spec || null,
    created_at: job.created_at,
    finished_at: null,
  };
}

/* ================================================================== *
 * 假数据：环境 / 诊断
 * ================================================================== */

function checkItem(id, title, status, detail, action = null, fixable = false, meta = null) {
  return { id, title, status, detail, action, fixable, meta };
}

function envItems() {
  const engineOk = state.openscad.found && state.openscad.cli_ok;
  const apiOk = state.settings.api_key_set;
  return [
    checkItem('windows', 'Windows 版本', 'ok', 'Windows 11 专业工作站版（build 26200）', null, false, {
      build: '26200',
      arch: 'x64',
    }),
    checkItem('runtime', '运行环境', 'ok', '已内置运行环境，无需单独安装', null, false, { bundled: true }),
    checkItem('python', 'Python 运行环境', 'ok', '3.13.14（内置）', null, false, { version: '3.13.14' }),
    checkItem('python_version', 'Python 版本', 'ok', '版本满足要求（需要 3.10 以上）', null, false, {
      required: '>=3.10',
    }),
    engineOk
      ? checkItem(
          'openscad',
          '3D 引擎',
          'ok',
          `OpenSCAD ${state.openscad.version} 已就绪`,
          null,
          false,
          { path: state.openscad.path, source: state.openscad.source },
        )
      : checkItem(
          'openscad',
          '3D 引擎',
          'fail',
          '未检测到 3D 引擎，无法生成模型文件',
          '点击「自动安装」，或手动指定 openscad.exe',
          true,
          { searched: ['PATH', 'Program Files', '注册表'] },
        ),
    engineOk
      ? checkItem('openscad_cli', '引擎可用性', 'ok', '引擎命令行测试通过', null, false, {
          duration_ms: 412,
        })
      : checkItem('openscad_cli', '引擎可用性', 'fail', '没有可用的 3D 引擎', '先安装 3D 引擎', true, null),
    engineOk
      ? checkItem('openscad_version', '引擎版本', 'ok', `版本 ${state.openscad.version}`, null, false, null)
      : checkItem('openscad_version', '引擎版本', 'warn', '无法读取版本号', '安装后会自动读取', false, null),
    checkItem('deps', '必要组件', 'ok', '依赖组件齐全', null, false, { count: 12 }),
    checkItem('project_dir', '项目目录', 'ok', '项目目录可读写', null, false, {
      path: state.settings.projects_dir,
    }),
    checkItem('network', '网络连接', 'ok', '可以访问 AI 服务与下载源', null, false, {
      latency_ms: 68,
    }),
    apiOk
      ? checkItem('api', 'AI 服务配置', 'ok', `已配置（${state.settings.api_key_masked}）`, null, false, {
          model: state.settings.model,
        })
      : checkItem(
          'api',
          'AI 服务配置',
          'warn',
          '还没有配置 API Key，暂时无法生成模型',
          '在「设置 → AI 服务」里填入 API Key',
          false,
          null,
        ),
    checkItem(
      'api_live',
      'AI 服务可用性',
      apiOk ? 'ok' : 'warn',
      apiOk ? '连接正常，响应 320 毫秒' : '未配置 API Key，跳过联网测试',
      apiOk ? null : '配置后会自动测试',
      false,
      { checked: '仅在「联网复检」时执行' },
    ),
    checkItem('temp', '临时目录', 'ok', '临时目录可读写', null, false, { path: 'C:\\Users\\dev\\AppData\\Local\\Temp' }),
    checkItem('stl_preview', '模型预览', 'ok', '三维预览可用（WebGL 2）', null, false, {
      renderer: 'WebGL 2.0',
    }),
    checkItem('mesh_engine', '模型引擎', 'ok', 'STL 解析与体积计算正常', null, false, {
      library: 'trimesh 4.4.0',
    }),
  ];
}

function envReport() {
  const items = envItems();
  const okCount = items.filter((i) => i.status === 'ok').length;
  const hasFail = items.some((i) => i.status === 'fail');
  return {
    checked_at: nowIso(),
    overall: hasFail ? 'fail' : okCount === items.length ? 'ok' : 'warn',
    ok_count: okCount,
    total: items.length,
    first_run: !state.settings.first_run_done,
    items,
  };
}

const GROUPS = [
  ['AI 服务', ['api', 'api_live']],
  ['3D 引擎', ['openscad', 'openscad_cli', 'openscad_version']],
  ['模型生成', ['deps', 'mesh_engine']],
  ['STL 预览', ['stl_preview']],
  ['文件系统', ['project_dir', 'temp']],
  ['网络', ['network', 'windows', 'runtime', 'python', 'python_version']],
];

function diagnostics() {
  const items = envItems();
  const byId = new Map(items.map((i) => [i.id, i]));
  const groups = GROUPS.map(([group, ids]) => ({
    group,
    items: ids.map((id) => byId.get(id)).filter(Boolean),
  }));
  const okCount = items.filter((i) => i.status === 'ok').length;
  const hasFail = items.some((i) => i.status === 'fail');
  return {
    checked_at: nowIso(),
    overall: hasFail ? 'fail' : okCount === items.length ? 'ok' : 'warn',
    groups,
  };
}

/* ================================================================== *
 * 极小 PNG（1×1 中性灰），用于缩略图端点
 * ================================================================== */
const TINY_PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
  'base64',
);

/* ================================================================== *
 * HTTP 工具
 * ================================================================== */

function sendJson(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(payload),
    'Cache-Control': 'no-store',
  });
  res.end(payload);
}

function sendError(res, status, detail, code = 'INTERNAL', hint = null) {
  sendJson(res, status, { detail, code, hint });
}

function readBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch {
        resolve({});
      }
    });
  });
}

function sseHead(res) {
  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no',
  });
  res.write(': connected\n\n');
}

function sseSend(res, event, data, id) {
  if (id !== undefined) res.write(`id: ${id}\n`);
  res.write(`event: ${event}\n`);
  res.write(`data: ${JSON.stringify(data)}\n\n`);
}

/* ================================================================== *
 * 路由
 * ================================================================== */

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`);
  const path = url.pathname;
  const method = req.method || 'GET';

  // 开发期跨域（Vite 有 proxy，这里只是保险）
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,PATCH,DELETE,OPTIONS');
  if (method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  console.log(`${method} ${path}${url.search}`);
  // 体检脚本要一次跑完「改得干净」和「改坏了」两种界面：用查询参数逐次覆盖核对结论
  const verdictOverride = url.searchParams.get('verdict');
  if (verdictOverride === 'bad' || verdictOverride === 'ok') {
    globalThis.__mockVerdictKind = verdictOverride;
  }

  try {
    /* ---------- 6. 应用控制 ---------- */
    if (path === '/api/app/health') {
      return sendJson(res, 200, { ok: true, version: '1.0.0', uptime_s: process.uptime() });
    }
    /* 缺 WebView2 的那台机器长什么样：用户手册 §2.2 承诺的「顶部说明 + 一键安装」
       全靠这两条才能验（真实开发机装了 WebView2，横幅分支永远跑不到）。 */
    if (path === '/api/app/window-runtime' && method === 'GET') {
      const on = state.windowRuntimeInstalled;
      return sendJson(res, 200, {
        installed: on,
        version: on ? '150.0.4078.65' : null,
        source: on ? 'registry' : 'none',
        detail: on
          ? '原生窗口可用。'
          : '这台电脑缺少微软的 WebView2 运行时，软件改用系统浏览器打开界面。',
        install_hint: on ? '' : '点「一键安装」会走系统自带的 winget；也可以自己到微软官网下载。',
        download_url: 'https://developer.microsoft.com/microsoft-edge/webview2/',
        bootstrapper_url: 'https://go.microsoft.com/fwlink/p/?LinkId=2124703',
      });
    }
    if (path === '/api/app/window-runtime/install' && method === 'POST') {
      state.windowRuntimeInstalled = true;
      return sendJson(res, 200, { ok: true, message: 'WebView2 运行时已安装完成。' });
    }
    // 仅供体检脚本使用：切换修改类作业的核对结论（ok / bad）
    if (path === '/api/mock/verdict' && method === 'POST') {
      const body = await readBody(req);
      state.verdictKind = body.kind === 'bad' ? 'bad' : 'ok';
      console.log(`   [mock] 核对结论切换为 ${state.verdictKind}`);
      return sendJson(res, 200, { ok: true, kind: state.verdictKind });
    }
    // 仅供体检脚本使用：切换「送到切片软件」的返回场景（ok / forwarded / failed / none）
    if (path === '/api/mock/slicer' && method === 'POST') {
      const body = await readBody(req);
      const allowed = ['ok', 'forwarded', 'failed', 'none'];
      state.slicerScenario = allowed.includes(body.scenario) ? body.scenario : 'ok';
      console.log(`   [mock] 送出场景切换为 ${state.slicerScenario}`);
      return sendJson(res, 200, { ok: true, scenario: state.slicerScenario });
    }
    if (path === '/api/app/open-folder' && method === 'POST') {
      const body = await readBody(req);
      console.log(`   [mock] 打开文件夹: ${body.kind} ${body.project_id ?? ''}`);
      return sendJson(res, 200, { ok: true });
    }
    if (path === '/api/app/open-external' && method === 'POST') {
      const body = await readBody(req);
      console.log(`   [mock] 打开外部链接: ${body.url}`);
      return sendJson(res, 200, { ok: true });
    }
    if (path === '/api/app/shutdown' && method === 'POST') {
      return sendJson(res, 200, { ok: true });
    }

    /* ---------- 1. 环境与诊断 ---------- */
    if (path === '/api/env/report') {
      return sendJson(res, 200, envReport());
    }
    if (path === '/api/env/version') {
      return sendJson(res, 200, {
        app: '1.0.0',
        build: '2025.01.15-a1b2c3d',
        app_full: 'AI 3D Forge 1.0.0',
        python: '3.13.14 (bundled)',
        python_bundled: true,
        openscad: state.openscad.version,
        os: 'Windows 11 专业工作站版 (build 26200)',
      });
    }
    if (path === '/api/env/detect' && method === 'POST') {
      return sendJson(res, 200, envReport());
    }
    if (path === '/api/diagnostics') {
      return sendJson(res, 200, diagnostics());
    }
    if (path === '/api/diagnostics/fix' && method === 'POST') {
      const { id } = await readBody(req);
      if (id === 'openscad' || id === 'openscad_cli') {
        state.openscad = {
          found: true,
          path: 'C:\\Users\\dev\\AppData\\Local\\AI-3D-Forge\\runtime\\openscad\\openscad.exe',
          version: '2021.01',
          source: 'common',
          candidates: [
            {
              path: 'C:\\Users\\dev\\AppData\\Local\\AI-3D-Forge\\runtime\\openscad\\openscad.exe',
              version: '2021.01',
            },
          ],
          cli_ok: true,
          detail: 'OpenSCAD 2021.01 已就绪（免安装版）',
        };
        state.settings.openscad_path = state.openscad.path;
        return sendJson(res, 200, {
          ok: true,
          message: '已找到并配置 3D 引擎。',
          report: envReport(),
        });
      }
      return sendJson(res, 200, {
        ok: true,
        message: '已完成检查，未发现需要修复的问题。',
        report: envReport(),
      });
    }

    /* ---------- 2. OpenSCAD ---------- */
    if (path === '/api/openscad/detect') {
      return sendJson(res, 200, state.openscad);
    }
    // 仅用于自测：把引擎状态重置回「未安装」，方便演示诊断页的 [自动修复] 分支。
    // 契约里没有这个端点，真实后端不需要实现。
    if (path === '/api/openscad/forget' && method === 'POST') {
      state.openscad = {
        found: false,
        path: null,
        version: null,
        source: null,
        candidates: [],
        cli_ok: false,
        detail: '未检测到 3D 引擎（OpenSCAD）。',
      };
      state.settings.openscad_path = null;
      return sendJson(res, 200, { ok: true });
    }
    if (path === '/api/openscad/browse' && method === 'POST') {
      // 模拟用户在文件对话框里选中了 openscad.exe
      state.openscad = {
        found: true,
        path: 'D:\\OpenSCAD\\openscad.exe',
        version: '2021.01',
        source: 'user',
        candidates: [{ path: 'D:\\OpenSCAD\\openscad.exe', version: '2021.01' }],
        cli_ok: true,
        detail: 'OpenSCAD 2021.01 已就绪',
      };
      state.settings.openscad_path = state.openscad.path;
      return sendJson(res, 200, { path: state.openscad.path });
    }
    if (path === '/api/openscad/locate' && method === 'POST') {
      const body = await readBody(req);
      if (!body.path || !/openscad\.exe$/i.test(body.path)) {
        return sendError(
          res,
          400,
          '这个文件不是 OpenSCAD 主程序，请选择 openscad.exe。',
          'OPENSCAD_MISSING',
          '通常在安装目录的根下，例如 D:\\OpenSCAD\\openscad.exe',
        );
      }
      state.openscad = {
        found: true,
        path: body.path,
        version: '2021.01',
        source: 'user',
        candidates: [{ path: body.path, version: '2021.01' }],
        cli_ok: true,
        detail: 'OpenSCAD 2021.01 已就绪',
      };
      state.settings.openscad_path = body.path;
      return sendJson(res, 200, state.openscad);
    }
    if (path === '/api/openscad/test' && method === 'POST') {
      if (!state.openscad.cli_ok) {
        return sendJson(res, 200, {
          ok: false,
          message: '没有可用的 3D 引擎，请先安装。',
          duration_ms: 12,
          diagnostics: 'openscad.exe not found',
        });
      }
      return sendJson(res, 200, {
        ok: true,
        message: '测试通过，成功生成了一个 10mm 的立方体',
        duration_ms: 486,
        diagnostics: null,
      });
    }
    if (path === '/api/openscad/install' && method === 'POST') {
      const body = await readBody(req);
      const wantError = body.mock === 'install-error' || url.searchParams.get('mock') === 'install-error';
      const jobId = randomUUID();
      state.installJobs.set(jobId, {
        id: jobId,
        created_at: Date.now(),
        wantError,
        status: 'running',
        percent: 0,
        message: '正在准备…',
      });
      return sendJson(res, 202, { job_id: jobId });
    }
    if (path.startsWith('/api/openscad/install/') && path.endsWith('/stream')) {
      const jobId = path.split('/')[4];
      const job = state.installJobs.get(jobId);
      if (!job) return sendError(res, 404, '找不到这个安装任务。', 'INTERNAL');
      sseHead(res);

      const steps = job.wantError
        ? [
            { at: 0, event: 'progress', data: { stage: 'prepare', percent: 4, message: '正在准备安装…' } },
            { at: 300, event: 'log', data: { message: '已连接到官方下载源' } },
            {
              at: 700,
              event: 'progress',
              data: { stage: 'download', percent: 18, message: '正在从官方源下载…', received: 3932160, total: 21876824 },
            },
            {
              at: 1300,
              event: 'progress',
              data: { stage: 'download', percent: 37.5, message: '正在从官方源下载…', received: 8214528, total: 21876824 },
            },
            { at: 1800, event: 'log', data: { message: '已校验文件完整性（SHA-256 匹配官方发布值）' } },
            {
              at: 2000,
              event: 'error',
              data: {
                ok: false,
                message: '需要管理员权限才能安装到 Program Files。',
                need_admin: true,
                manual_url: 'https://openscad.org/downloads.html',
                manual_steps: [
                  '打开 OpenSCAD 官方下载页：https://openscad.org/downloads.html',
                  '下载 Windows 版安装包（.exe）并运行。',
                  '按默认选项完成安装（不要修改安装目录）。',
                  '回到本程序，点击「我已手动装好」，或直接「手动选择 openscad.exe」。',
                ],
              },
            },
          ]
        : [
            { at: 0, event: 'progress', data: { stage: 'prepare', percent: 3, message: '正在准备安装…' } },
            { at: 300, event: 'log', data: { message: '免安装版将解压到你的用户目录，不需要管理员权限' } },
            {
              at: 700,
              event: 'progress',
              data: { stage: 'download', percent: 12, message: '正在从官方源下载…', received: 2621440, total: 21876824 },
            },
            {
              at: 1400,
              event: 'progress',
              data: { stage: 'download', percent: 38, message: '正在从官方源下载…', received: 8314528, total: 21876824 },
            },
            {
              at: 2100,
              event: 'progress',
              data: { stage: 'download', percent: 74, message: '正在从官方源下载…', received: 16189000, total: 21876824 },
            },
            { at: 2600, event: 'log', data: { message: '下载完成，开始校验文件完整性' } },
            {
              at: 2900,
              event: 'progress',
              data: { stage: 'verify', percent: 82, message: '正在校验文件完整性…' },
            },
            { at: 3100, event: 'log', data: { message: '已校验文件完整性（SHA-256 匹配官方发布值）' } },
            {
              at: 3400,
              event: 'progress',
              data: { stage: 'install', percent: 90, message: '正在解压到用户目录…' },
            },
            { at: 4000, event: 'progress', data: { stage: 'cli_test', percent: 96, message: '正在测试引擎…' } },
            { at: 4400, event: 'progress', data: { stage: 'config', percent: 99, message: '正在写入配置…' } },
            {
              at: 4700,
              event: 'done',
              data: {
                ok: true,
                message: 'OpenSCAD 已安装并配置完成。',
                info: {
                  found: true,
                  path: 'C:\\Users\\dev\\AppData\\Local\\AI-3D-Forge\\runtime\\openscad\\openscad.exe',
                  version: '2021.01',
                  source: 'common',
                  candidates: [
                    {
                      path: 'C:\\Users\\dev\\AppData\\Local\\AI-3D-Forge\\runtime\\openscad\\openscad.exe',
                      version: '2021.01',
                    },
                  ],
                  cli_ok: true,
                  detail: 'OpenSCAD 2021.01 已就绪（免安装版）',
                },
              },
            },
          ];

      const started = Date.now();
      let cancelled = false;
      req.on('close', () => {
        cancelled = true;
      });
      const tick = async () => {
        for (const step of steps) {
          if (cancelled) return;
          const wait = step.at - (Date.now() - started);
          if (wait > 0) await new Promise((r) => setTimeout(r, wait));
          if (cancelled) return;
          sseSend(res, step.event, step.data);
          job.percent = step.data.percent ?? job.percent;
          job.message = step.data.message ?? job.message;
          if (step.event === 'done') {
            job.status = 'done';
            state.openscad = step.data.info;
            state.settings.openscad_path = step.data.info.path;
            console.log('   [mock] 安装完成，已写入引擎配置');
          }
          if (step.event === 'error') {
            job.status = 'error';
            job.need_admin = step.data.need_admin;
            job.manual_url = step.data.manual_url;
            job.manual_steps = step.data.manual_steps;
            job.errorMessage = step.data.message;
          }
        }
        res.end();
      };
      void tick();
      return;
    }
    // 轮询兜底（契约未定义，仅为前端降级路径提供可验证的实现）
    if (path.startsWith('/api/openscad/install/') && method === 'GET') {
      const jobId = path.split('/')[4];
      const job = state.installJobs.get(jobId);
      if (!job) return sendError(res, 404, '找不到这个安装任务。', 'INTERNAL');
      return sendJson(res, 200, {
        status: job.status,
        percent: job.percent,
        message: job.message,
        need_admin: job.need_admin ?? false,
        manual_url: job.manual_url ?? null,
        manual_steps: job.manual_steps ?? null,
        error_message: job.errorMessage ?? null,
        info: job.status === 'done' ? state.openscad : null,
      });
    }

    /* ---------- 5. 设置 ---------- */
    if (path === '/api/settings' && method === 'GET') {
      return sendJson(res, 200, state.settings);
    }
    if (path === '/api/settings' && method === 'PUT') {
      const body = await readBody(req);
      if ('api_key' in body) {
        const key = String(body.api_key ?? '');
        if (key === '') {
          state.settings.api_key_set = false;
          state.settings.api_key_masked = null;
        } else if (key.length < 8) {
          return sendError(res, 400, 'API Key 看起来不完整，请检查后重试。', 'API_KEY_INVALID', 'Key 一般以 sk- 开头。');
        } else {
          state.settings.api_key_set = true;
          state.settings.api_key_masked = `${key.slice(0, 7)}****${key.slice(-4)}`;
        }
      }
      for (const field of ['base_url', 'model', 'keep_stl_history', 'auto_install_openscad', 'theme', 'openscad_path']) {
        if (field in body) state.settings[field] = body[field];
      }
      if (body.appearance === 'solid' || body.appearance === 'glass') {
        state.settings.appearance = body.appearance;
      }
      if (body.glass_transparency !== undefined && body.glass_transparency !== null) {
        const raw = Number(body.glass_transparency);
        state.settings.glass_transparency = Number.isFinite(raw)
          ? Math.max(0, Math.min(60, Math.round(raw)))
          : 25;
      }
      // 默认切片软件：照真后端同样把关（只认现在检测得到的 id，"" = 清空），
      // 否则前端验不出“选了个存不住的幽灵 id”这条错误分支。
      if ('default_slicer' in body) {
        const wanted = String(body.default_slicer ?? '').trim();
        if (!wanted) {
          state.settings.default_slicer = null;
        } else if (!slicersDescribe().slicers.some((s) => s.id === wanted)) {
          return sendError(res, 409, '这个切片软件当前没检测到。', 'SLICER_NOT_FOUND', '重新检测一次，或换个目标。');
        } else {
          state.settings.default_slicer = wanted;
        }
      }
      if (Array.isArray(body.slicer_custom_paths)) {
        const cleaned = [];
        for (const raw of body.slicer_custom_paths) {
          const text = String(raw ?? '').trim();
          if (text && !cleaned.includes(text)) cleaned.push(text);
        }
        state.settings.slicer_custom_paths = cleaned;
      }
      return sendJson(res, 200, state.settings);
    }
    if (path === '/api/settings/api-key/test' && method === 'POST') {
      const body = await readBody(req);
      const key = body.api_key || (state.settings.api_key_set ? 'stored' : '');
      if (!key) {
        return sendError(res, 400, '还没有配置 API Key。', 'NO_API_KEY', '在「设置 → AI 服务」里填入 API Key。');
      }
      if (String(key).includes('bad')) {
        return sendError(
          res,
          401,
          'API Key 无效或额度不足。',
          'API_KEY_INVALID',
          '请检查 Key 是否正确，或到 DeepSeek 平台确认额度。',
        );
      }
      return sendJson(res, 200, {
        ok: true,
        message: '连接正常，模型可用',
        model: state.settings.model,
        latency_ms: 318,
      });
    }
    if (path === '/api/settings/first-run/complete' && method === 'POST') {
      state.settings.first_run_done = true;
      return sendJson(res, 200, { ok: true });
    }
    // 下载保存：mock 无原生对话框 → 告知前端退回浏览器下载
    if (/^\/api\/projects\/[^/]+\/save$/.test(path) && method === 'POST') {
      return sendJson(res, 200, { status: 'no-dialog' });
    }

    /* ---------- 3A. 打印机（mock 内置一台通用 FDM） ---------- */
    if (path === '/api/printers' && method === 'GET') {
      return sendJson(res, 200, {
        selected_id: MOCK_PRINTER.id,
        selected_material: 'PLA',
        default_id: MOCK_PRINTER.id,
        materials: MOCK_PRINTER.materials,
        printers: [MOCK_PRINTER],
      });
    }
    if (path === '/api/printers/selected' && method === 'GET') {
      return sendJson(res, 200, {
        printer: MOCK_PRINTER,
        material: 'PLA',
        material_options: MOCK_PRINTER.materials,
        note: '',
      });
    }
    if (path === '/api/printers/select' && method === 'POST') {
      const body = await readBody(req);
      return sendJson(res, 200, {
        printer: MOCK_PRINTER,
        material: body.material || 'PLA',
        previous: null,
      });
    }

    /* ---------- 4A. 切片软件（送到切片软件） ---------- */
    if (path === '/api/slicers' && method === 'GET') {
      return sendJson(res, 200, slicersDescribe());
    }
    if (path === '/api/slicers/pick' && method === 'POST') {
      // mock 没有原生对话框：照“用户选中了一个 exe”的效果假装一次，让 added 分支可验
      // （与 /api/openscad/browse 同一套做法）。MOCK_SLICER_PICK=0 走 canceled。
      if (process.env.MOCK_SLICER_PICK === '0') {
        return sendJson(res, 200, { status: 'canceled', ...slicersDescribe() });
      }
      const exe = 'D:\\PrusaSlicer\\prusa-slicer.exe';
      if (!slicerState.found.some((s) => s.exe === exe)) {
        slicerState.found.push({
          id: 'prusa_slicer',
          name: 'PrusaSlicer',
          exe,
          version: null,
          source_label: '手动指定',
          running: false,
        });
      }
      if (!state.settings.slicer_custom_paths.includes(exe)) {
        state.settings.slicer_custom_paths = [...state.settings.slicer_custom_paths, exe];
      }
      slicerState.none = false;
      return sendJson(res, 200, { status: 'added', path: exe, ...slicersDescribe() });
    }
    const sendMatch = /^\/api\/projects\/([^/]+)\/send-to-slicer$/.exec(path);
    if (sendMatch && method === 'POST') {
      const project = state.projects.find((p) => p.id === sendMatch[1]);
      if (!project) return sendError(res, 404, '找不到这个项目。', 'INTERNAL', '它可能已经被删除。');
      const body = await readBody(req).catch(() => ({}));
      const items = slicerState.none ? [] : slicerState.found;
      const wanted = String(body.slicer ?? '').trim();
      const scenario = state.slicerScenario;
      const target = scenario === 'none' ? null : wanted ? items.find((s) => s.id === wanted) : items[0] ?? null;
      if (!target) {
        // 文案照真后端 slicers.resolve()：错误分支要验的就是用户会看到的那一句
        return sendError(
          res,
          409,
          '没有检测到切片软件。请先安装 Bambu Studio / OrcaSlicer / PrusaSlicer / Cura 其中之一，或在「设置 → 切片软件」里手动指定它的主程序。',
          'SLICER_NOT_FOUND',
          '在「设置 → 切片软件」里点「手动指定主程序」也能接上。',
        );
      }
      const file = `C:\\Users\\dev\\Documents\\AI-3D-Forge\\Projects\\${project.id.slice(0, 8)}\\slicer\\${project.name}-r${String(project.revision).padStart(2, '0')}.3mf`;
      const base = {
        pid: 41236,
        detached: true,
        slicer: target.id,
        slicer_name: target.name,
        file,
        exe: target.exe,
        kind: '3mf',
        source_label: target.source_label,
      };
      target.running = true;
      if (scenario === 'failed') {
        return sendJson(res, 200, {
          ...base,
          status: 'failed',
          detail: `${target.name} 启动后立即退出（退出码 1）`,
          exit_code: 1,
        });
      }
      if (scenario === 'forwarded') {
        return sendJson(res, 200, {
          ...base,
          status: 'forwarded',
          detail: `${target.name} 已经在运行，文件已交给它的现有窗口`,
          exit_code: 0,
        });
      }
      return sendJson(res, 200, {
        ...base,
        status: 'started',
        detail: `已启动 ${target.name}（进程 ${base.pid}）`,
        exit_code: null,
      });
    }

    // 3MF：真实字节（由 src/aiforge/threemf.py 生成的 10×20×30 长方体，12 面/8 顶点），
    // 用于验收“下载 3MF”按钮不会退成 404。
    const mfMatch = /^\/api\/projects\/[^/]+\/3mf$/.exec(path);
    if (mfMatch && method === 'GET') {
      const buf = Buffer.from(MOCK_3MF_B64, 'base64');
      res.writeHead(200, {
        'Content-Type': 'model/3mf',
        'Content-Length': buf.length,
        'Content-Disposition': 'attachment; filename="model.3mf"',
        'Cache-Control': 'no-store',
      });
      return res.end(buf);
    }

    /* ---------- 4. 项目 ---------- */
    if (path === '/api/projects' && method === 'GET') {
      return sendJson(res, 200, state.projects.map(projectSummary));
    }
    if (path === '/api/projects' && method === 'POST') {
      const body = await readBody(req);
      const name = String(body.name ?? '').trim() || '未命名项目';
      const project = createProject({ name, kind: 'phone_stand', revision: 0 });
      project.last_status = 'unknown';
      project.summary = null;
      return sendJson(res, 200, projectSummary(project));
    }
    const projectMatch = /^\/api\/projects\/([^/]+)$/.exec(path);
    /* ---------- 回收站：必须排在 `/api/projects/{id}` 前面 ----------
       真实后端同样靠注册顺序；mock 写成后置的话，界面验收会拿到 404，
       而 404 看起来就是「回收站是空的」—— 顺序错在两边都会饶过去。 */
    if (path === '/api/projects/trash' && method === 'GET') {
      return sendJson(res, 200, {
        items: state.trash.map((row) => row.item),
        count: state.trash.length,
        dir: 'C:\\Users\\dev\\Documents\\AI-3D-Forge\\Projects\\.deleted',
      });
    }
    const trashRestore = /^\/api\/projects\/trash\/([^/]+)\/restore$/.exec(path);
    if (trashRestore && method === 'POST') {
      const index = state.trash.findIndex((row) => row.item.entry === decodeURIComponent(trashRestore[1]));
      if (index < 0) return sendError(res, 404, '找不到回收站里的这个条目。', 'INTERNAL');
      const [row] = state.trash.splice(index, 1);
      state.projects.unshift(row.project);
      return sendJson(res, 200, {
        ok: true,
        project: projectSummary(row.project),
        message: `已恢复「${row.project.name}」。`,
      });
    }
    const trashPurge = /^\/api\/projects\/trash\/([^/]+)\/purge$/.exec(path);
    if (trashPurge && method === 'POST') {
      const body = await readBody(req);
      if (body.confirm !== true) {
        return sendError(res, 400, '彻底删除后无法恢复，需要明确确认。', 'INTERNAL', '界面上需要再点一次「确认彻底删除」。');
      }
      const entry = decodeURIComponent(trashPurge[1]);
      const before = state.trash.length;
      state.trash = state.trash.filter((row) => row.item.entry !== entry);
      if (state.trash.length === before) return sendError(res, 404, '找不到回收站里的这个条目。', 'INTERNAL');
      return sendJson(res, 200, { ok: true, message: '已彻底删除。' });
    }
    if (projectMatch) {
      const project = state.projects.find((p) => p.id === projectMatch[1]);
      if (!project) return sendError(res, 404, '找不到这个项目。', 'INTERNAL', '它可能已经被删除。');
      if (method === 'GET') return sendJson(res, 200, projectDetail(project));
      if (method === 'PATCH') {
        const body = await readBody(req);
        const name = String(body.name ?? '').trim();
        if (!name) return sendError(res, 400, '项目名称不能为空。', 'INTERNAL');
        project.name = name;
        project.updated_at = nowIso();
        return sendJson(res, 200, projectSummary(project));
      }
      if (method === 'DELETE') {
        state.projects = state.projects.filter((p) => p.id !== project.id);
        modelCache.delete(project.id);
        // 与真实后端同构：不是真删，而是进回收站等恢复（条目名带时间戳）。
        const stamp = new Date().toISOString().replace(/[-:TZ]/g, '').slice(0, 13);
        state.trash.unshift({
          project,
          item: {
            entry: `${project.id.slice(0, 8)}__${stamp}00`,
            name: project.name,
            project_id: project.id,
            revision: project.revision,
            deleted_at: nowIso(),
            files: 3,
            size_bytes: 24_576,
            has_stl: true,
          },
        });
        return sendJson(res, 200, {
          ok: true,
          message: '已移入回收站（项目文件夹的 .deleted 目录），可在「设置 → 回收站」恢复。',
        });
      }
    }
    /* GET /api/projects/{id}/metrics —— 真实后端会现场重分 STL，
       前端「重开历史项目时合成结果卡」完全依赖这条路；缺了它，
       卡片上就是「尺寸 0.0 × 0.0 × 0.0 / 文件大小 0 B」。 */
    const metricsMatch = /^\/api\/projects\/([^/]+)\/metrics$/.exec(path);
    if (metricsMatch && method === 'GET') {
      const project = state.projects.find((p) => p.id === metricsMatch[1]);
      if (!project) return sendError(res, 404, '找不到这个项目。', 'INTERNAL');
      return sendJson(res, 200, projectMetricsPayload(project));
    }
    const downloadMatch = /^\/api\/projects\/([^/]+)\/(stl|stl\/raw|scad|thumbnail)$/.exec(path);
    if (downloadMatch) {
      const project = state.projects.find((p) => p.id === downloadMatch[1]);
      if (!project) return sendError(res, 404, '找不到这个项目。', 'INTERNAL');
      const what = downloadMatch[2];
      if (what === 'thumbnail') {
        res.writeHead(200, { 'Content-Type': 'image/png', 'Content-Length': TINY_PNG.length });
        return res.end(TINY_PNG);
      }
      const model = getModel(project);
      if (what === 'scad') {
        const scad = scadSource(project.kind, project.params, project.name);
        res.writeHead(200, {
          'Content-Type': 'text/plain; charset=utf-8',
          'Content-Disposition': `attachment; filename="${encodeURIComponent(project.name)}.scad"`,
        });
        return res.end(scad);
      }
      const inline = what === 'stl/raw';
      res.writeHead(200, {
        'Content-Type': 'application/sla',
        'Content-Length': model.stl.length,
        'Content-Disposition': `${inline ? 'inline' : 'attachment'}; filename="${encodeURIComponent(project.name)}.stl"`,
        'Cache-Control': 'no-store',
      });
      return res.end(model.stl);
    }

    /* ---------- 3.4 参数微调 ---------- */
    const paramsMatch = /^\/api\/projects\/([^/]+)\/params$/.exec(path);
    if (paramsMatch && method === 'POST') {
      const project = state.projects.find((p) => p.id === paramsMatch[1]);
      if (!project) return sendError(res, 404, '找不到这个项目。', 'INTERNAL');
      const body = await readBody(req);
      const incoming = body.params ?? {};
      for (const def of PARAM_DEFS[project.kind]) {
        if (incoming[def.name] === undefined) continue;
        let value = Number(incoming[def.name]);
        if (!Number.isFinite(value)) continue;
        if (def.min !== null && value < def.min) value = def.min;
        if (def.max !== null && value > def.max) value = def.max;
        project.params[def.name] = value;
      }
      project.revision += 1;
      project.updated_at = nowIso();
      modelCache.delete(project.id); // 让下一次 getModel 重新生成
      const jobId = startJob({
        project,
        kindOfRequest: 'params',
        summary: `已按新参数重新生成：${Object.entries(incoming)
          .map(([k, v]) => `${k}=${v}`)
          .join('，')}`,
      });
      return sendJson(res, 202, { job_id: jobId, project_id: project.id, revision: project.revision });
    }

    /* ---------- 3. 作业 ---------- */
    // GET /api/jobs?project_id=xxx —— 真实后端有这条（docs/API-CONTRACT.md §3.1），
    // 前端用它**重新接管正在跑的作业**（切页/刷新回来时）。夹具缺了它，
    // 「切页后建模界面是否还在」这类回归就没法用假后端验。
    if (path === '/api/jobs' && method === 'GET') {
      const wanted = url.searchParams.get('project_id');
      const jobs = [...state.jobs.values()]
        .filter((job) => !wanted || job.project_id === wanted)
        .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))
        .slice(0, 20)
        .map((job) => jobSnapshot(job));
      return sendJson(res, 200, { jobs });
    }

    if (path === '/api/jobs' && method === 'POST') {
      const body = await readBody(req);
      const prompt = String(body.prompt ?? '').trim();
      const instruction = String(body.instruction ?? '').trim();
      const mode = body.mode === 'modify' ? 'modify' : 'create';

      if (process.env.MOCK_REQUIRE_KEY === '1' && !state.settings.api_key_set) {
        // 默认放开，方便直接跑通「发需求 → 结果」；需要验证 NO_API_KEY 分支时设 MOCK_REQUIRE_KEY=1
        return sendError(
          res,
          400,
          '还没有配置 AI 服务的 API Key。',
          'NO_API_KEY',
          '在「设置 → AI 服务」里填入 API Key 后即可生成。',
        );
      }
      if (!prompt && !instruction) {
        return sendError(res, 400, '请先描述你想设计的模型。', 'INTERNAL');
      }
      if (/bad|失败/.test(prompt) && /触发失败/.test(prompt)) {
        return sendError(res, 422, '模型代码编译失败。', 'OPENSCAD_FAILED', '可以让 AI 自动修复后重试。');
      }

      let project;
      if (mode === 'modify' && body.project_id) {
        project = state.projects.find((p) => p.id === body.project_id);
        if (!project) return sendError(res, 404, '找不到要修改的项目。', 'INTERNAL');
        project.revision += 1;
        project.updated_at = nowIso();
        modelCache.delete(project.id);
      } else {
        const text = prompt || instruction;
        const kind = /花盆|盆栽|pot/i.test(text)
          ? 'flower_pot'
          : /盒|收纳|box/i.test(text)
            ? 'storage_box'
            : 'phone_stand';
        const guessed = {};
        const h = /(\d+(?:\.\d+)?)\s*mm/i.exec(text);
        const sizeMatch = /(\d+(?:\.\d+)?)\s*[×x*]\s*(\d+(?:\.\d+)?)\s*[×x*]\s*(\d+(?:\.\d+)?)/.exec(text);
        if (sizeMatch) {
          if (kind === 'storage_box') {
            guessed.length = Number(sizeMatch[1]);
            guessed.width = Number(sizeMatch[2]);
            guessed.height = Number(sizeMatch[3]);
          } else {
            guessed.diameter = Number(sizeMatch[1]);
            guessed.height = Number(sizeMatch[3]);
          }
        } else if (h) {
          if (kind === 'phone_stand') guessed.height = Number(h[1]);
          else guessed.height = Number(h[1]);
        }
        if (/壁厚\s*(\d+(?:\.\d+)?)/.test(text)) {
          const wall = Number(/壁厚\s*(\d+(?:\.\d+)?)/.exec(text)[1]);
          if (kind === 'flower_pot') guessed.wall = wall;
          else if (kind === 'storage_box') guessed.thickness = wall;
        }
        const name = body.name || `${KINDS[kind].title} ${new Date().getMonth() + 1}/${new Date().getDate()}`;
        project = createProject({
          name,
          kind,
          params: makeParams(kind, guessed),
          revision: 1,
          prompt: text,
        });
      }

      const jobId = startJob({
        project,
        kindOfRequest: mode === 'modify' ? 'modify' : 'create',
        summary:
          mode === 'modify'
            ? `已按要求修改：${instruction || prompt}`
            : KINDS[project.kind].summary,
      });
      return sendJson(res, 202, { job_id: jobId, project_id: project.id, revision: project.revision });
    }

    const jobMatch = /^\/api\/jobs\/([^/]+)$/.exec(path);
    if (jobMatch && method === 'GET') {
      const job = state.jobs.get(jobMatch[1]);
      if (!job) return sendError(res, 404, '找不到这个任务。', 'INTERNAL');
      return sendJson(res, 200, jobSnapshot(job));
    }
    const streamMatch = /^\/api\/jobs\/([^/]+)\/stream$/.exec(path);
    if (streamMatch) {
      const job = state.jobs.get(streamMatch[1]);
      if (!job) return sendError(res, 404, '找不到这个任务。', 'INTERNAL');
      sseHead(res);
      // 客户端断开**只代表没人看了，不代表作业被放弃**。
      // 真实后端就是这样：作业继续跑，重连时首帧补完整快照（见下面 sseSend('snapshot')）。
      // 旧夹具在 req.close 时直接 return，把作业"顺带"终止了 ——
      // 于是「建模时切到别的界面再回来，作业是否还在」这条路径在假后端上根本测不了，
      // 而且它给出的行为与契约相反（用户报的正是这个现象）。
      let clientGone = false;
      req.on('close', () => {
        clientGone = true;
      });
      const send = (event, data) => {
        if (!clientGone) sseSend(res, event, data);
      };
      const started = Date.now();
      const finishedStep = job.script.find((s) => s.event === 'done');
      // 首帧立刻推当前已有状态（契约要求：断线重连后能补齐）。
      // 必须用完整快照事件：确认卡片要靠它重建，只推 stage/message 是不够的。
      const snapshot = jobSnapshot(job);
      send('snapshot', snapshot);
      for (const m of snapshot.messages) send('message', m);

      void (async () => {
        // 用户点了取消：真实后端会推 `cancelled` + **完整快照** 再收流。
        // 假后端一度只悄悄 return，于是「停止生成」这条最关键的用户路径
        // 在假后端上根本没被测到（前端漏处理 cancelled 的 bug 就是这样漏过去的）。
        const emitCancelled = () => {
          job.cancelled = true;
          job.paused = false;
          send('cancelled', { ...jobSnapshot(job), status: 'cancelled' });
          console.log(`   [mock] 作业 ${job.id.slice(0, 8)} 已取消，已推送 cancelled`);
        };

        for (const step of job.script) {
          if (job.cancelled) {
            emitCancelled();
            return;
          }
          const wait = step.at + job.shift - (Date.now() - started);
          if (wait > 0) await new Promise((r) => setTimeout(r, wait));
          if (job.cancelled) {
            emitCancelled();
            return;
          }
          if (step.event === 'message') {
            if (!job.messages.some((m) => m.seq === step.data.seq)) job.messages.push(step.data);
            send('message', step.data);
          } else if (step.event === 'stage') {
            send('stage', step.data);
          } else if (step.event === 'spec') {
            job.spec = step.data;
            send('spec', step.data);
          } else if (step.event === 'done') {
            send('done', step.build(job));
          }
          // 规格确认关卡：**先推送规格、再停在这里**，等用户 POST /confirm-spec 才继续。
          // 顺序不能反 —— 反过来用户永远看不到那张卡片，作业就在服务端死等。
          if (step.gate && !job.confirmed) {
            console.log(`   [mock] 作业 ${job.id.slice(0, 8)} 已在规格关卡等待确认`);
            job.paused = true;
            const gateReachedAt = Date.now();
            for (;;) {
              await new Promise((r) => setTimeout(r, 120));
              if (job.cancelled) {
                emitCancelled();
                return;
              }
              if (job.confirmed) break;
            }
            job.paused = false;
            // 时间轴整体后移，后续步骤之间的间隔保持不变
            job.shift += Date.now() - gateReachedAt;
            // 真实后端在用户确认后会重推一次规格（awaiting_confirmation=false），
            // 界面据此把卡片收成只读版 —— 假后端必须照做，否则这条路径根本没被测到。
            if (job.spec) {
              job.spec = { ...job.spec, awaiting_confirmation: false };
              send('spec', job.spec);
            }
            send('message', {
              seq: (job.messages.at(-1)?.seq ?? 0) + 1,
              text: '已确认，开始设计。',
              detail: null,
            });
          }
        }
        if (finishedStep) console.log(`   [mock] 作业 ${job.id.slice(0, 8)} 完成`);
        res.end();
      })();
      return;
    }
    const cancelMatch = /^\/api\/jobs\/([^/]+)\/cancel$/.exec(path);
    if (cancelMatch && method === 'POST') {
      const job = state.jobs.get(cancelMatch[1]);
      if (job) job.cancelled = true;
      return sendJson(res, 200, { ok: true });
    }
    const confirmMatch = /^\/api\/jobs\/([^/]+)\/confirm-spec$/.exec(path);
    if (confirmMatch && method === 'POST') {
      const job = state.jobs.get(confirmMatch[1]);
      if (!job) return sendError(res, 404, '找不到这个任务。', 'INTERNAL');
      if (!job.paused) {
        return sendError(res, 409, '这次任务当前不需要确认规格。', 'INTERNAL');
      }
      const body = await readBody(req);
      // 用户改了数字 → 采用用户的版本（界面必须能真的把改动带回来）
      if (body && body.spec) job.spec = { spec: body.spec, awaiting_confirmation: false };
      else if (job.spec) job.spec = { ...job.spec, awaiting_confirmation: false };
      job.confirmed = true;
      return sendJson(res, 200, { ok: true });
    }

    /* ---------- 3.5 直接编译 ---------- */
    if (path === '/api/compile' && method === 'POST') {
      const body = await readBody(req);
      const values = parseScadParams(String(body.scad ?? ''));
      const project = createProject({
        name: body.name || '调试模型',
        kind: 'phone_stand',
        params: makeParams('phone_stand', values),
        revision: 1,
        prompt: '（直接编译 SCAD 源码）',
      });
      const jobId = startJob({ project, kindOfRequest: 'compile', summary: '已编译你提供的源码。' });
      return sendJson(res, 202, { job_id: jobId, project_id: project.id, revision: project.revision });
    }

    return sendError(res, 404, `没有这个接口：${method} ${path}`, 'INTERNAL');
  } catch (err) {
    console.error('mock server error', err);
    if (!res.headersSent) return sendError(res, 500, '假后端内部错误。', 'INTERNAL');
    res.end();
  }
});

/* 初始示例数据：让首次进入工作台不是空的 */
createProject({
  name: '桌面手机支架',
  kind: 'phone_stand',
  params: makeParams('phone_stand', { height: 110 }),
  revision: 2,
  prompt: '设计一个桌面手机支架，高度100mm',
});
const pot = createProject({
  name: '多肉花盆',
  kind: 'flower_pot',
  params: makeParams('flower_pot', { diameter: 90, height: 85 }),
  revision: 1,
  prompt: '做一个 90mm 的花盆，壁厚3mm',
});
pot.summary = KINDS.flower_pot.summary;

server.listen(PORT, '127.0.0.1', () => {
  console.log(`AI 3D Forge 假后端已启动: http://127.0.0.1:${PORT}`);
  console.log(`  first_run = ${!state.settings.first_run_done}`);
  console.log(`  示例项目 ${state.projects.length} 个`);
  console.log('  提示：POST /api/openscad/install {"mock":"install-error"} 可验证安装失败分支');
});

process.on('SIGINT', () => {
  for (const s of sleepers) clearTimeout(s);
  server.close(() => process.exit(0));
});
