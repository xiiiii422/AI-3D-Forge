import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { api } from '../api/client';
import type { PrinterConstraints, PrinterProfile } from '../api/types';
import { toFriendlyError, type FriendlyError } from './errors';

/**
 * 打印机状态（契约 §3A）。
 *
 * 为什么放在一个 Provider 里：工作台顶部的机型胶囊、设置页的「打印机」卡片、
 * 欢迎向导的机型选择都必须看到同一个选择结果，否则用户会在两处看到不同的机器。
 * 选择结果由后端持久化（POST /api/printers/select），这里只负责缓存与广播。
 */

export interface PrinterSwitchEvent {
  /** 切换后的机型 */
  printer: PrinterProfile;
  /** 切换后的材料 */
  material: string;
  /** 切换前选中的机型（首次加载时为 null） */
  previous: PrinterProfile | null;
  previousMaterial: string | null;
  /** 每次切换递增的时间戳，供页面用它触发「一次性」副作用 */
  at: number;
}

/** 切换机型的结果：成功给出新机型，失败给出可直接展示的中文原因 */
export type SelectOutcome =
  | { ok: true; printer: PrinterProfile; material: string }
  | { ok: false; message: string };

export interface PrinterStore {
  printers: PrinterProfile[];
  selectedId: string;
  selectedMaterial: string;
  defaultId: string;
  /** 所有机型可用材料的并集 */
  materials: string[];
  selected: PrinterProfile | null;
  loading: boolean;
  error: FriendlyError | null;
  /** 正在切换机型 */
  selecting: boolean;
  reload: () => Promise<void>;
  /** 切换机型（可同时指定材料）。成功后刷新列表并记录 lastSwitch。 */
  select: (printerId: string, material?: string) => Promise<SelectOutcome>;
  /** 最近一次切换事件；页面用它提示「旧模型是按别的机器生成的」 */
  lastSwitch: PrinterSwitchEvent | null;
}

const PrinterContext = createContext<PrinterStore | null>(null);

export function PrinterProvider({ children }: { children: ReactNode }) {
  const [printers, setPrinters] = useState<PrinterProfile[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [selectedMaterial, setSelectedMaterial] = useState('');
  const [defaultId, setDefaultId] = useState('');
  const [materials, setMaterials] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<FriendlyError | null>(null);
  const [selecting, setSelecting] = useState(false);
  const [lastSwitch, setLastSwitch] = useState<PrinterSwitchEvent | null>(null);

  const alive = useRef(true);
  /** 记录当前选择，供 select() 计算「上一台机器」 */
  const currentRef = useRef<{ printer: PrinterProfile | null; material: string | null }>({
    printer: null,
    material: null,
  });

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const reload = useCallback(async () => {
    try {
      const res = await api.listPrinters();
      if (!alive.current) return;
      setPrinters(res.printers);
      setSelectedId(res.selected_id);
      setSelectedMaterial(res.selected_material);
      setDefaultId(res.default_id);
      setMaterials(res.materials);
      currentRef.current = {
        printer: res.printers.find((p) => p.id === res.selected_id) ?? null,
        material: res.selected_material,
      };
      setError(null);
    } catch (err) {
      if (alive.current) setError(toFriendlyError(err));
    } finally {
      if (alive.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const select = useCallback(
    async (printerId: string, material?: string): Promise<SelectOutcome> => {
      setSelecting(true);
      try {
        const res = await api.selectPrinter(
          material === undefined
            ? { printer_id: printerId }
            : { printer_id: printerId, material },
        );
        const before = currentRef.current;
        currentRef.current = { printer: res.printer, material: res.material };
        if (alive.current) {
          setLastSwitch({
            printer: res.printer,
            material: res.material,
            previous: before.printer,
            previousMaterial: before.material,
            at: Date.now(),
          });
          setSelectedId(res.printer.id);
          setSelectedMaterial(res.material);
          setError(null);
          // 列表里的 constraints / checklist 是按当前材料算的，切换后必须重取
          await reload();
        }
        return { ok: true, printer: res.printer, material: res.material };
      } catch (err) {
        const friendly = toFriendlyError(err);
        if (alive.current) setError(friendly);
        return { ok: false, message: friendly.message };
      } finally {
        if (alive.current) setSelecting(false);
      }
    },
    [reload],
  );

  const value = useMemo<PrinterStore>(
    () => ({
      printers,
      selectedId,
      selectedMaterial,
      defaultId,
      materials,
      selected: printers.find((p) => p.id === selectedId) ?? null,
      loading,
      error,
      selecting,
      reload,
      select,
      lastSwitch,
    }),
    [
      printers,
      selectedId,
      selectedMaterial,
      defaultId,
      materials,
      loading,
      error,
      selecting,
      reload,
      select,
      lastSwitch,
    ],
  );

  return <PrinterContext.Provider value={value}>{children}</PrinterContext.Provider>;
}

export function usePrinters(): PrinterStore {
  const ctx = useContext(PrinterContext);
  if (!ctx) throw new Error('usePrinters 必须在 <PrinterProvider> 内使用');
  return ctx;
}

/* ------------------------------------------------------------------ *
 * 展示用的纯函数（中文、非技术）
 * ------------------------------------------------------------------ */

/** 数值 → 界面文案：整数不带小数，其余保留一位 */
export function num(value: number, digits = 1): string {
  if (!Number.isFinite(value)) return '—';
  return Number.isInteger(value) ? String(value) : value.toFixed(digits);
}

/** 去掉无意义的尾随零：0.40 → 0.4、1.20 → 1.2（喷嘴与壁厚都按这个格式显示） */
export function trimNum(value: number, digits = 2): string {
  if (!Number.isFinite(value)) return '—';
  return String(Number(value.toFixed(digits)));
}

/** 光固化机型没有喷嘴 */
export function isResin(printer: { nozzle_mm: number; extruder?: PrinterProfile['extruder'] }): boolean {
  return printer.extruder === 'resin' || printer.nozzle_mm <= 0;
}

/**
 * 机型显示名：内置机型的名字尾部常带「（0.4mm）」，而喷嘴在界面上是单独一列，
 * 所以把这层重复的信息去掉，让胶囊能显示成「Bambu Lab X1 Carbon · PETG」。
 */
export function printerTitle(printer: { name: string; nozzle_mm: number }): string {
  const nozzle = trimNum(printer.nozzle_mm, 2);
  return printer.name
    .replace(new RegExp(`[（(]\\s*${nozzle}\\s*mm\\s*[）)]\\s*$`), '')
    .replace(new RegExp(`[（(]\\s*${nozzle}\\s*mm\\s*喷嘴\\s*[）)]\\s*$`), '')
    .trim();
}

/** 一行规格摘要，例如「0.4mm 喷嘴 · 256×256×256mm · 封闭腔体」 */
export function printerSpecSummary(printer: PrinterProfile): string {
  const parts: string[] = [];
  if (isResin(printer)) parts.push('光固化');
  else parts.push(`${trimNum(printer.nozzle_mm, 2)}mm 喷嘴`);
  parts.push(
    `${num(printer.build_x)}×${num(printer.build_y)}×${num(printer.build_z)}mm`,
  );
  parts.push(printer.enclosure ? '封闭腔体' : '开放机身');
  return parts.join(' · ');
}

/** 切换机型后要告诉用户的「这意味着什么」 */
export function printerImpactToast(printer: PrinterProfile, material: string): string {
  const c: PrinterConstraints = printer.constraints;
  const span = Math.min(c.max_build_x, c.max_build_y, c.max_build_z);
  const head = isResin(printer)
    ? `${trimNum(printer.nozzle_mm, 2)} 级精度、${num(span)}mm 成型范围`
    : `${trimNum(printer.nozzle_mm, 2)}mm 喷嘴、${num(span)}mm 成型范围、最小壁厚 ${trimNum(
        c.min_wall_mm,
        2,
      )}mm`;
  return `已切换到 ${printerTitle(printer)}${material ? `（${material}）` : ''}。之后生成的模型会按 ${head} 来设计。已有模型不会自动改变，重新生成即可适配。`;
}

/** 材料的一句话说明（给设置页与向导用，避免用户看到裸的英文缩写） */
export const MATERIAL_HINT: Record<string, string> = {
  PLA: '最好打、最通用',
  'PLA-CF': '加了碳纤维，更硬',
  PETG: '韧、耐水，日常首选',
  ABS: '耐热但需要封闭腔体',
  ASA: '耐候，户外件',
  TPU: '软胶，可弯折',
  PA: '尼龙，耐磨',
  'PA-CF': '碳纤尼龙，强度高',
  PC: '很硬很耐热，难打',
  PVA: '水溶支撑',
  HIPS: '支撑材料',
  树脂: '光固化专用',
};

export function materialHint(material: string): string {
  return MATERIAL_HINT[material] ?? '';
}
