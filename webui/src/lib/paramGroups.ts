/**
 * 参数微调的分类规则。
 *
 * 为什么需要分类：拿本机真实项目量了一下 —— 11 个带参数表的项目合计 132 个可调
 * 参数，单项目 8~16 个、中位数 12（最多的是「桌面手机支架」16 个）。
 * 全平铺在一格里时，改一个「壁厚」要先逐个读完十来个标签。
 *
 * 为什么放在前端而不是后端：这是纯展示归类，后端 param_meta 里没有类别字段，
 * 历史项目也不会因为加了字段就补上 —— 放这里可以让老项目立刻受益，
 * 且只有一份实现（规则改动不需要重编译模型、不动 API 契约）。
 *
 * 归类同时看变量名与中文说明：AI 写的 `// @param wall_thickness 2.4 壁厚` 两种都要能命中，
 * 只按英文名的话，一旦模型用了中文变量名或自定义缩写就全落到「其它」。
 */

export type ParamGroupId = 'overall' | 'hole' | 'wall' | 'detail' | 'count' | 'other';

export interface ParamLike {
  name: string;
  label: string;
  /** 后端将来若直接给类别，这里优先采用，不再走关键词猜 */
  group?: string | null;
}

/** 展示顺序：从「决定能不能放进打印机」到「细节工艺」，用户最常改的排前面 */
export const PARAM_GROUP_ORDER: { id: ParamGroupId; label: string }[] = [
  { id: 'overall', label: '整体尺寸' },
  { id: 'hole', label: '孔与螺纹' },
  { id: 'wall', label: '壁厚与强度' },
  { id: 'detail', label: '圆角与配合' },
  { id: 'count', label: '数量与阵列' },
  { id: 'other', label: '其它参数' },
];

/**
 * 判定优先级：从上到下第一条命中的规则生效。
 * 顺序很讲究，不要随手调整：
 *  * 孔/螺纹必须排在尺寸前面 —— `hole_diameter`（孔径）既含 hole 又含 diameter，
 *    先判尺寸会把它错分到「整体尺寸」，用户按外形去找就找不到。
 *  * 壁厚/结构排在尺寸前面：`wall_height`（墙高）、`支撑柱厚度` 这类名字同时带着
 *    高/厚 词根，先判外形会把结构参数归到「整体尺寸」里。
 *  * 圆角/配合的 radius 排在尺寸前：`fillet_radius`（圆角半径）不是外形尺寸。
 * 倾角、角度这类「整个零件怎么摆」的参数归进整体尺寸，不单开一类：
 * 它们和长宽高一起决定成品能不能放稳、放不放得进打印机。
 */
const RULES: { id: ParamGroupId; tokens: string[] }[] = [
  {
    id: 'hole',
    tokens: [
      'hole', 'bore', 'thread', 'pitch', 'screw', 'bolt', 'nut', 'tap', 'drill', 'perforation',
      '孔', '螺纹', '牙', '螺距', '孔径', '轴孔', '排水', '进线', '穿线', '通孔', '沉头',
    ],
  },
  {
    id: 'wall',
    tokens: [
      'wall', 'thick', 'rib', 'gusset', 'reinforce', 'bracing', 'flange', 'beam', 'column',
      '壁', '厚', '筋', '加强', '立柱', '支撑', '承托', '承重',
    ],
  },
  {
    id: 'detail',
    tokens: [
      'fillet', 'chamfer', 'round', 'radius', 'gap', 'clearance', 'tolerance', 'bevel', 'offset',
      'margin', 'inset', 'overlap',
      '圆角', '倒角', '半径', '间隙', '公差', '余量', '配合', '搭接', '内缩', '让位',
    ],
  },
  {
    id: 'count',
    tokens: [
      'count', 'number', 'num_', 'qty', 'steps', 'segments', 'faces', 'teeth', 'pieces', 'layers',
      'reps',
      '数量', '个数', '齿数', '层数', '分段', '孔数', '根数', '圈数', '阵列',
    ],
  },
  {
    id: 'overall',
    tokens: [
      'height', 'width', 'depth', 'length', 'diameter', 'size', 'span', 'total', 'outer', 'inner',
      'overall', 'base', 'top', 'side', 'arm', 'leg', 'post', 'body', 'stand',
      'angle', 'tilt', 'incline', 'rotation',
      '高', '宽', '深', '长', '直径', '外径', '内径', '口径', '尺寸', '对边', '边长', '底座',
      '整体', '总', '倾角', '角度', '夹角',
    ],
  },
];

export function classifyParam(param: ParamLike): ParamGroupId {
  const declared = (param.group || '').trim();
  if (declared && PARAM_GROUP_ORDER.some((g) => g.id === declared)) return declared as ParamGroupId;
  const haystack = `${param.name} ${param.label}`.toLowerCase();
  for (const rule of RULES) {
    if (rule.tokens.some((token) => haystack.includes(token.toLowerCase()))) return rule.id;
  }
  return 'other';
}

export interface ParamGroup<T extends ParamLike = ParamLike> {
  id: ParamGroupId;
  label: string;
  items: T[];
}

/** 按固定顺序分组，空组不返回；组内保持源码里的原始顺序 */
export function groupParams<T extends ParamLike>(params: T[]): ParamGroup<T>[] {
  const buckets = new Map<ParamGroupId, T[]>();
  for (const param of params) {
    const id = classifyParam(param);
    const list = buckets.get(id);
    if (list) list.push(param);
    else buckets.set(id, [param]);
  }
  return PARAM_GROUP_ORDER.filter((g) => buckets.has(g.id)).map((g) => ({
    id: g.id,
    label: g.label,
    items: buckets.get(g.id) as T[],
  }));
}

/**
 * 什么时候不值得分类：参数本来就少、或者全落在同一类时，
 * 再套一层分类标题只会多一排没信息量的空壳。
 */
export function shouldGroup(params: ParamLike[]): boolean {
  if (params.length < 5) return false;
  return groupParams(params).length > 1;
}
