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

/** 三角形集合 → 二进制 STL Buffer */
function trianglesToStl(triangles) {
  const buffer = Buffer.alloc(84 + triangles.length * 50);
  buffer.write('AI 3D Forge mock model (binary STL)', 0, 80, 'ascii');
  buffer.writeUInt32LE(triangles.length, 80);
  let offset = 84;
  for (const [p0, p1, p2] of triangles) {
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

/** 由三角形集合算尺寸与体积（mm / mm³） */
function measure(triangles) {
  const min = [Infinity, Infinity, Infinity];
  const max = [-Infinity, -Infinity, -Infinity];
  let volume = 0;
  for (const [p0, p1, p2] of triangles) {
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
  }
  return {
    bbox: { x: max[0] - min[0], y: max[1] - min[1], z: max[2] - min[2] },
    volume_mm3: Math.abs(volume),
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
  phone_stand: [
    { name: 'width', label: '宽度', value: 80, unit: 'mm', min: 40, max: 200 },
    { name: 'height', label: '高度', value: 100, unit: 'mm', min: 50, max: 260 },
    { name: 'depth', label: '底座深度', value: 70, unit: 'mm', min: 40, max: 180 },
    { name: 'thickness', label: '板厚', value: 5, unit: 'mm', min: 2, max: 15 },
    { name: 'angle', label: '倾角', value: 65, unit: '°', min: 30, max: 85 },
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
  /** 修改类作业的核对结论：ok=改得干净 / bad=没改到位还顺手改坏别处 */
  verdictKind: process.env.MOCK_VERDICT === 'bad' ? 'bad' : 'ok',
  settings: {
    api_key_set: false,
    api_key_masked: null,
    api_key_store: 'credential_manager',
    base_url: 'https://api.deepseek.com',
    model: 'deepseek-chat',
    openscad_path: null,
    data_dir: 'C:\\Users\\dev\\AppData\\Roaming\\AI-3D-Forge',
    projects_dir: 'C:\\Users\\dev\\Documents\\AI-3D-Forge\\Projects',
    keep_stl_history: true,
    auto_install_openscad: true,
    first_run_done: !FIRST_RUN,
    theme: 'light',
    confirm_spec: true,
    announce_in_chat: true,
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

function projectSummary(p) {
  const model = getModel(p);
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
  const item = (name, label, given) => ({
    name,
    label,
    value: value(name),
    unit: 'mm',
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
      item('angle', '屏幕倾角', false),
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
  const finished = doneStep && doneStep.at <= elapsed;
  // 等待用户确认需求理解：这是一个**非终态**，前端不能据此停止订阅
  const waiting = Boolean(job.paused) && !job.confirmed;
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
    // 仅供体检脚本使用：切换修改类作业的核对结论（ok / bad）
    if (path === '/api/mock/verdict' && method === 'POST') {
      const body = await readBody(req);
      state.verdictKind = body.kind === 'bad' ? 'bad' : 'ok';
      console.log(`   [mock] 核对结论切换为 ${state.verdictKind}`);
      return sendJson(res, 200, { ok: true, kind: state.verdictKind });
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
        return sendJson(res, 200, { ok: true });
      }
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
      let cancelled = false;
      req.on('close', () => {
        cancelled = true;
      });
      const started = Date.now();
      const finishedStep = job.script.find((s) => s.event === 'done');
      // 首帧立刻推当前已有状态（契约要求：断线重连后能补齐）。
      // 必须用完整快照事件：确认卡片要靠它重建，只推 stage/message 是不够的。
      const snapshot = jobSnapshot(job);
      sseSend(res, 'snapshot', snapshot);
      for (const m of snapshot.messages) sseSend(res, 'message', m);

      void (async () => {
        // 用户点了取消：真实后端会推 `cancelled` + **完整快照** 再收流。
        // 假后端一度只悄悄 return，于是「停止生成」这条最关键的用户路径
        // 在假后端上根本没被测到（前端漏处理 cancelled 的 bug 就是这样漏过去的）。
        const emitCancelled = () => {
          job.cancelled = true;
          job.paused = false;
          sseSend(res, 'cancelled', { ...jobSnapshot(job), status: 'cancelled' });
          console.log(`   [mock] 作业 ${job.id.slice(0, 8)} 已取消，已推送 cancelled`);
        };

        for (const step of job.script) {
          if (cancelled) return;
          if (job.cancelled) {
            emitCancelled();
            return;
          }
          const wait = step.at + job.shift - (Date.now() - started);
          if (wait > 0) await new Promise((r) => setTimeout(r, wait));
          if (cancelled) return;
          if (job.cancelled) {
            emitCancelled();
            return;
          }
          if (step.event === 'message') {
            if (!job.messages.some((m) => m.seq === step.data.seq)) job.messages.push(step.data);
            sseSend(res, 'message', step.data);
          } else if (step.event === 'stage') {
            sseSend(res, 'stage', step.data);
          } else if (step.event === 'spec') {
            job.spec = step.data;
            sseSend(res, 'spec', step.data);
          } else if (step.event === 'done') {
            sseSend(res, 'done', step.build(job));
          }
          // 规格确认关卡：**先推送规格、再停在这里**，等用户 POST /confirm-spec 才继续。
          // 顺序不能反 —— 反过来用户永远看不到那张卡片，作业就在服务端死等。
          if (step.gate && !job.confirmed) {
            console.log(`   [mock] 作业 ${job.id.slice(0, 8)} 已在规格关卡等待确认`);
            job.paused = true;
            const gateReachedAt = Date.now();
            for (;;) {
              await new Promise((r) => setTimeout(r, 120));
              if (cancelled) return;
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
              sseSend(res, 'spec', job.spec);
            }
            sseSend(res, 'message', {
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
