/**
 * 尺寸标注的纯计算部分。
 *
 * 刻意不 import three：这样这些数字可以脱离 WebGL 直接核对
 * （verify-flow 里会拿标签与浮层尺寸做交叉校验）。
 *
 * 坐标系约定（与 Viewer3D 里把 STL 转成 Y 向上之后的世界坐标一致）：
 *   X = 宽、Y = 高、Z = 深，1 世界单位 = 1 毫米。
 * 轴语义的来源：OpenSCAD 导出的 STL 是 Z 向上，后端 mesh.analyze 也按
 * 「z 跨度 = 总高、min.z ≈ 0 = 贴平台」判定，预览里必须用同一把尺子，
 * 否则「高」会标到水平轴上。
 */

export type Vec3 = [number, number, number];

export interface Box3Like {
  min: Vec3;
  max: Vec3;
}

export type DimAxis = 'width' | 'depth' | 'height';

export interface DimensionAnnotation {
  axis: DimAxis;
  /** 标签文字，例如「宽 80.0」 */
  text: string;
  /** 实测长度（毫米） */
  value: number;
  /** 尺寸线两端（世界坐标） */
  line: [Vec3, Vec3];
  /** 引出线：从模型角点连到尺寸线端点（工程图里的 extension line） */
  leaders: [Vec3, Vec3][];
  /** 箭头翼：每段是一条独立线段，两端各两条 */
  arrows: [Vec3, Vec3][];
  /** 标签锚点：尺寸线中点往外让开一点，避免压住线本身 */
  anchor: Vec3;
}

export const AXIS_LABEL: Record<DimAxis, string> = { width: '宽', depth: '深', height: '高' };

/** 1 位小数对毫米件够用，但薄壁（<1mm）要能看出差别 */
export function formatMm(value: number): string {
  const v = Math.abs(value) < 1 ? value.toFixed(2) : value.toFixed(1);
  // 避免出现「-0.0」这种既难看又像错误的读数
  return v === '-0.0' || v === '-0.00' ? v.replace('-', '') : v;
}

const add = (p: Vec3, d: Vec3): Vec3 => [p[0] + d[0], p[1] + d[1], p[2] + d[2]];
const scale = (d: Vec3, k: number): Vec3 => [d[0] * k, d[1] * k, d[2] * k];
const mid = (a: Vec3, b: Vec3): Vec3 => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2];

/**
 * 在端点 P 处画一个指回线内的箭头（u 为该端点指向线内的方向、n 为垂直方向）。
 * 用两条短线而不是圆锥：线宽在 WebGL 里恒为 1px，但箭头是几何形状，
 * 缩放时始终跟着模型走，不会像 sprite 那样糊成一团。
 */
function arrowWings(p: Vec3, u: Vec3, n: Vec3, len: number): [Vec3, Vec3][] {
  const back = add(p, scale(u, len));
  const spread = scale(n, len * 0.34);
  return [
    [back, add(back, spread)],
    [back, add(back, scale(spread, -1))],
  ];
}

/**
 * 由包围盒生成三条整体尺寸标注（宽 / 深 / 高）。
 *
 * 摆放位置是刻意选的：默认机位在 (+X, +Y, +Z) 一侧，所以
 *   宽 → 前下方（+Z 面外侧）、深 → 右侧下方（+X 面外侧）、高 → 左前棱外侧。
 * 三条线都贴着「看得见」的那一侧，转个角度也不会全被模型挡住。
 */
export function buildOverallDimensions(box: Box3Like): DimensionAnnotation[] {
  const span: Vec3 = [
    box.max[0] - box.min[0],
    box.max[1] - box.min[1],
    box.max[2] - box.min[2],
  ];
  const extent = Math.max(span[0], span[1], span[2], 1);
  // 让开距离按模型尺度走：固定毫米值在小件上会飞出画面、在大件上又贴着模型
  const off = Math.max(extent * 0.12, 1);
  const wing = off * 0.42;

  const out: DimensionAnnotation[] = [];

  const push = (
    axis: DimAxis,
    value: number,
    line: [Vec3, Vec3],
    dir: Vec3,
    perp: Vec3,
    leaders: [Vec3, Vec3][],
    anchorShift: Vec3,
  ) => {
    if (value <= 0) return; // 平板件某个方向可能是 0，画出一条零长度的线只会误导
    const [a, b] = line;
    out.push({
      axis,
      value,
      text: `${AXIS_LABEL[axis]} ${formatMm(value)}`,
      line,
      leaders,
      arrows: [...arrowWings(a, scale(dir, -1), perp, wing), ...arrowWings(b, dir, perp, wing)],
      anchor: add(mid(a, b), anchorShift),
    });
  };

  const floor = box.min[1];
  // 宽：沿 X，摆在底面前侧外方
  push(
    'width',
    span[0],
    [
      [box.min[0], floor, box.max[2] + off],
      [box.max[0], floor, box.max[2] + off],
    ],
    [1, 0, 0],
    [0, 0, 1],
    [
      [[box.min[0], floor, box.max[2]], [box.min[0], floor, box.max[2] + off]],
      [[box.max[0], floor, box.max[2]], [box.max[0], floor, box.max[2] + off]],
    ],
    [0, -off * 0.12, off * 0.16],
  );
  // 深：沿 Z，摆在底面右侧外方
  push(
    'depth',
    span[2],
    [
      [box.max[0] + off, floor, box.min[2]],
      [box.max[0] + off, floor, box.max[2]],
    ],
    [0, 0, 1],
    [1, 0, 0],
    [
      [[box.max[0], floor, box.min[2]], [box.max[0] + off, floor, box.min[2]]],
      [[box.max[0], floor, box.max[2]], [box.max[0] + off, floor, box.max[2]]],
    ],
    [off * 0.16, -off * 0.12, 0],
  );
  // 高：沿 Y，摆在左前棱外侧
  push(
    'height',
    span[1],
    [
      [box.min[0] - off, floor, box.max[2] + off],
      [box.min[0] - off, box.max[1], box.max[2] + off],
    ],
    [0, 1, 0],
    [0, 0, 1],
    [
      [[box.min[0], floor, box.max[2]], [box.min[0] - off, floor, box.max[2] + off]],
      [[box.min[0], box.max[1], box.max[2]], [box.min[0] - off, box.max[1], box.max[2] + off]],
    ],
    [-off * 0.14, 0, off * 0.16],
  );

  return out;
}

/** 把标注摊平成一组线段端点（喂给 LineSegments 的 position 数组） */
export function annotationSegments(
  annotations: DimensionAnnotation[],
): { dimLines: number[]; leaders: number[] } {
  const dimLines: number[] = [];
  const leaders: number[] = [];
  const put = (target: number[], seg: [Vec3, Vec3]) => {
    target.push(seg[0][0], seg[0][1], seg[0][2], seg[1][0], seg[1][1], seg[1][2]);
  };
  for (const item of annotations) {
    put(dimLines, item.line);
    for (const wing of item.arrows) put(dimLines, wing);
    for (const leader of item.leaders) put(leaders, leader);
  }
  return { dimLines, leaders };
}
