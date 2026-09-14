import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { api } from '../api/client';
import type {
  PrinterKnowledgeSummary,
  PrinterPreviewRequest,
  PrinterPreviewResponse,
  PrinterProfile,
  PrinterWriteRequest,
} from '../api/types';
import { toFriendlyError } from '../hooks/errors';
import {
  isResin,
  materialHint,
  printerSpecSummary,
  printerTitle,
  usePrinters,
} from '../hooks/usePrinters';
import { Accordion } from './Accordion';
import { Button } from './Button';
import { IconPencil, IconPlus, IconPrinter, IconRefresh, IconTrash } from './Icons';
import { ConfirmModal, Modal } from './Modal';
import { StatusDot } from './StatusDot';
import { useToast } from './Toast';

/* ------------------------------------------------------------------ *
 * 表单草稿：数字一律用字符串保存，空值不会变成 NaN
 * ------------------------------------------------------------------ */
interface Draft {
  name: string;
  manufacturer: string;
  nozzle_mm: string;
  filament_diameter_mm: string;
  materials: string[];
  enclosure: boolean;
  has_heated_bed: boolean;
  max_hotend_temp_c: string;
  max_bed_temp_c: string;
  build_x: string;
  build_y: string;
  build_z: string;
  layer_min_mm: string;
  layer_max_mm: string;
  max_speed_mm_s: string;
  max_accel_mm_s2: string;
  max_flow_mm3_s: string;
  recommended_max_overhang_deg: string;
  recommended_max_bridge_mm: string;
  dimensional_tolerance_mm: string;
  kinematics: PrinterProfile['kinematics'];
  extruder: PrinterProfile['extruder'];
  bed_leveling: PrinterProfile['bed_leveling'];
  bed_surface: string;
}

const NOZZLE_CHOICES = [0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0];

const KINEMATICS: { value: PrinterProfile['kinematics']; label: string }[] = [
  { value: 'cartesian', label: '框架式（XYZ 各自独立）' },
  { value: 'corexy', label: 'CoreXY（喷头轻、速度快）' },
  { value: 'bedslinger', label: '床身前后移动（常见入门机）' },
  { value: 'delta', label: 'Delta 并联臂（圆形有效区域）' },
  { value: 'resin', label: '光固化（树脂，无喷嘴）' },
];

const EXTRUDERS: { value: PrinterProfile['extruder']; label: string }[] = [
  { value: 'direct', label: '近端送料（喷头带电机）' },
  { value: 'bowden', label: '远端送料（Bowden 管）' },
  { value: 'resin', label: '光固化（无挤出机）' },
];

const LEVELING: { value: PrinterProfile['bed_leveling']; label: string }[] = [
  { value: 'manual', label: '手动调平' },
  { value: 'abl', label: '辅助调平（ABL）' },
  { value: 'auto', label: '全自动调平' },
];

function draftFrom(printer: PrinterProfile | null): Draft {
  return {
    name: printer?.name ?? '',
    manufacturer: printer?.manufacturer ?? '',
    nozzle_mm: String(printer?.nozzle_mm ?? 0.4),
    filament_diameter_mm: String(printer?.filament_diameter_mm ?? 1.75),
    materials: printer?.materials ? [...printer.materials] : ['PLA', 'PETG'],
    enclosure: printer?.enclosure ?? false,
    has_heated_bed: printer?.has_heated_bed ?? true,
    max_hotend_temp_c: String(printer?.max_hotend_temp_c ?? 260),
    max_bed_temp_c: String(printer?.max_bed_temp_c ?? 100),
    build_x: String(printer?.build_x ?? 220),
    build_y: String(printer?.build_y ?? 220),
    build_z: String(printer?.build_z ?? 250),
    layer_min_mm: String(printer?.layer_min_mm ?? 0.08),
    layer_max_mm: String(printer?.layer_max_mm ?? 0.32),
    max_speed_mm_s: String(printer?.max_speed_mm_s ?? 150),
    max_accel_mm_s2: String(printer?.max_accel_mm_s2 ?? 3000),
    max_flow_mm3_s: String(printer?.max_flow_mm3_s ?? 12),
    recommended_max_overhang_deg: String(printer?.recommended_max_overhang_deg ?? 55),
    recommended_max_bridge_mm: String(printer?.recommended_max_bridge_mm ?? 30),
    dimensional_tolerance_mm: String(printer?.dimensional_tolerance_mm ?? 0.25),
    kinematics: printer?.kinematics ?? 'cartesian',
    extruder: printer?.extruder ?? 'direct',
    bed_leveling: printer?.bed_leveling ?? 'manual',
    bed_surface: printer?.bed_surface ?? '',
  };
}

function toNumber(value: string, fallback: number): number {
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

/** 草稿 → POST /api/printers/preview 的请求体（超出后端允许范围的先夹住，避免 422） */
function previewBody(draft: Draft): PrinterPreviewRequest {
  const clamp = (value: number, min: number, max: number) =>
    Math.min(Math.max(value, min), max);
  const rawNozzle = toNumber(draft.nozzle_mm, 0.4);
  const resin = draft.extruder === 'resin' || rawNozzle <= 0;
  return {
    // 光固化没有喷嘴，但接口要求 nozzle_mm > 0，这里给一个极小值即可
    nozzle_mm: resin ? 0.01 : clamp(rawNozzle, 0.01, 2),
    build_x: clamp(toNumber(draft.build_x, 220), 1, 2000),
    build_y: clamp(toNumber(draft.build_y, 220), 1, 2000),
    build_z: clamp(toNumber(draft.build_z, 250), 1, 2000),
    material: draft.materials[0] ?? null,
    enclosure: draft.enclosure,
    max_bed_temp_c: clamp(Math.round(toNumber(draft.max_bed_temp_c, 100)), 0, 200),
    max_hotend_temp_c: clamp(Math.round(toNumber(draft.max_hotend_temp_c, 260)), 0, 500),
    layer_min_mm: clamp(toNumber(draft.layer_min_mm, 0.08), 0.01, 2),
    layer_max_mm: clamp(toNumber(draft.layer_max_mm, 0.32), 0.01, 2),
    recommended_max_overhang_deg: clamp(
      Math.round(toNumber(draft.recommended_max_overhang_deg, 55)),
      10,
      89,
    ),
    recommended_max_bridge_mm: clamp(toNumber(draft.recommended_max_bridge_mm, 30), 0.1, 500),
    extruder: resin ? 'resin' : draft.extruder,
  };
}

function buildSaveBody(draft: Draft): PrinterWriteRequest {
  const rawNozzle = toNumber(draft.nozzle_mm, 0.4);
  const resin = draft.extruder === 'resin' || rawNozzle <= 0;
  // 接口要求 0 < nozzle_mm <= 2；光固化的 0 用 0.01 代替，工艺判定靠 extruder
  const nozzle = resin ? 0.01 : Math.min(Math.max(rawNozzle, 0.01), 2);
  return {
    name: draft.name.trim(),
    manufacturer: draft.manufacturer.trim(),
    nozzle_mm: nozzle,
    build_x: toNumber(draft.build_x, 220),
    build_y: toNumber(draft.build_y, 220),
    build_z: toNumber(draft.build_z, 250),
    nozzle_options: resin
      ? [nozzle]
      : Array.from(new Set([0.2, 0.4, 0.6, 0.8, nozzle])).sort((a, b) => a - b),
    layer_min_mm: toNumber(draft.layer_min_mm, 0.08),
    layer_max_mm: toNumber(draft.layer_max_mm, 0.32),
    max_speed_mm_s: Math.round(toNumber(draft.max_speed_mm_s, 150)),
    max_accel_mm_s2: Math.round(toNumber(draft.max_accel_mm_s2, 3000)),
    max_flow_mm3_s: toNumber(draft.max_flow_mm3_s, 12),
    has_heated_bed: draft.has_heated_bed,
    max_bed_temp_c: Math.round(toNumber(draft.max_bed_temp_c, 100)),
    max_hotend_temp_c: Math.round(toNumber(draft.max_hotend_temp_c, 260)),
    enclosure: draft.enclosure,
    materials: draft.materials.length > 0 ? draft.materials : ['PLA'],
    filament_diameter_mm: toNumber(draft.filament_diameter_mm, 1.75),
    extruder: resin ? 'resin' : draft.extruder,
    bed_leveling: draft.bed_leveling,
    bed_surface: draft.bed_surface.trim(),
    kinematics: draft.kinematics,
    recommended_max_overhang_deg: Math.round(toNumber(draft.recommended_max_overhang_deg, 55)),
    recommended_max_bridge_mm: toNumber(draft.recommended_max_bridge_mm, 30),
    dimensional_tolerance_mm: toNumber(draft.dimensional_tolerance_mm, 0.25),
  };
}

/* ------------------------------------------------------------------ *
 * 小组件
 * ------------------------------------------------------------------ */
function NumField({
  id,
  label,
  value,
  unit,
  hint,
  step = 'any',
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  unit?: string;
  hint?: string;
  step?: string;
  onChange: (value: string) => void;
}) {
  return (
    <div className="field">
      <label className="field-label" htmlFor={id}>
        {label}
      </label>
      <div className="input-group">
        <input
          id={id}
          className="input"
          type="number"
          inputMode="decimal"
          step={step}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
        {unit ? <span className="field-unit">{unit}</span> : null}
      </div>
      {hint ? <span className="field-hint">{hint}</span> : null}
    </div>
  );
}

function TextField({
  id,
  label,
  value,
  placeholder,
  hint,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  placeholder?: string;
  hint?: string;
  onChange: (value: string) => void;
}) {
  return (
    <div className="field">
      <label className="field-label" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        className="input"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
      {hint ? <span className="field-hint">{hint}</span> : null}
    </div>
  );
}

function SelectField<T extends string>({
  id,
  label,
  value,
  options,
  hint,
  onChange,
}: {
  id: string;
  label: string;
  value: T;
  options: { value: T; label: string }[];
  hint?: string;
  onChange: (value: T) => void;
}) {
  return (
    <div className="field">
      <label className="field-label" htmlFor={id}>
        {label}
      </label>
      <select
        id={id}
        className="select"
        value={value}
        onChange={(e) => onChange(e.target.value as T)}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      {hint ? <span className="field-hint">{hint}</span> : null}
    </div>
  );
}

/** 「这台机器的设计要求」实时预览 —— 让知识库变得看得见 */
function LivePreview({
  preview,
  loading,
  error,
}: {
  preview: PrinterPreviewResponse | null;
  loading: boolean;
  error: string | null;
}) {
  return (
    <aside className="printer-preview" aria-label="设计要求预览">
      <div className="printer-preview-head">
        <IconPrinter size={15} />
        这台机器的设计要求
        {loading ? <StatusDot status="checking" /> : null}
      </div>
      {preview ? (
        <>
          <ul className="rule-list">
            {preview.checklist.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          {preview.notes.length > 0 ? (
            <div className="printer-preview-notes">
              {preview.notes.map((note) => (
                <p key={note}>{note}</p>
              ))}
            </div>
          ) : null}
        </>
      ) : (
        <p className="field-hint">
          {error ? `暂时无法计算：${error}` : '正在按当前参数计算…'}
        </p>
      )}
      <p className="field-hint">
        这些数字就是 AI 设计模型时会遵守的规则，改喷嘴或成型尺寸会立刻反映在这里。
      </p>
    </aside>
  );
}

/* ------------------------------------------------------------------ *
 * 新建 / 编辑表单
 * ------------------------------------------------------------------ */
function PrinterEditor({
  source,
  allMaterials,
  onCancel,
  onSaved,
}: {
  /** null = 新建 */
  source: PrinterProfile | null;
  allMaterials: string[];
  onCancel: () => void;
  onSaved: (printer: PrinterProfile) => void;
}) {
  const toast = useToast();
  const [draft, setDraft] = useState<Draft>(() => draftFrom(source));
  const [saving, setSaving] = useState(false);
  const [customNozzle, setCustomNozzle] = useState(
    () => !NOZZLE_CHOICES.includes(Number(source?.nozzle_mm ?? 0.4)),
  );
  const [preview, setPreview] = useState<PrinterPreviewResponse | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(true);

  const patch = useCallback((part: Partial<Draft>) => setDraft((cur) => ({ ...cur, ...part })), []);

  /* ---------- 实时预览：防抖 400ms，丢弃过期响应 ---------- */
  const seqRef = useRef(0);
  useEffect(() => {
    const body = previewBody(draft);
    const seq = ++seqRef.current;
    setPreviewing(true);
    const timer = window.setTimeout(() => {
      void api
        .previewPrinter(body)
        .then((res) => {
          if (seq !== seqRef.current) return;
          setPreview(res);
          setPreviewError(null);
        })
        .catch((err: unknown) => {
          if (seq !== seqRef.current) return;
          setPreviewError(toFriendlyError(err).message);
        })
        .finally(() => {
          if (seq === seqRef.current) setPreviewing(false);
        });
    }, 400);
    return () => window.clearTimeout(timer);
  }, [draft]);

  const materialChoices = useMemo(
    () => Array.from(new Set([...allMaterials, ...draft.materials])),
    [allMaterials, draft.materials],
  );

  const toggleMaterial = (material: string) => {
    setDraft((cur) => {
      const has = cur.materials.includes(material);
      const next = has
        ? cur.materials.filter((m) => m !== material)
        : [...cur.materials, material];
      return { ...cur, materials: next.length > 0 ? next : cur.materials };
    });
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!draft.name.trim()) {
      toast.warn('请先给这台打印机起个名字。');
      return;
    }
    setSaving(true);
    try {
      const body = buildSaveBody(draft);
      const saved = source
        ? await api.updatePrinter(source.id, body)
        : await api.createPrinter(body);
      toast.ok(source ? '已保存为你的自定义机型。' : '已新建机型。');
      onSaved(saved);
    } catch (err) {
      toast.error(toFriendlyError(err).message);
    } finally {
      setSaving(false);
    }
  };

  const resin = isResin({ nozzle_mm: toNumber(draft.nozzle_mm, 0.4), extruder: draft.extruder });

  return (
    <form className="printer-editor" onSubmit={(e) => void handleSubmit(e)}>
      <div className="printer-editor-grid">
        <div className="stack">
          {source?.builtin ? (
            <div className="notice">
              <IconPrinter size={16} />
              <span>
                这是内置机型。保存后会在你的机型库里生成一个自定义版本（列表里标为「已修改」），
                <strong>内置原值不会被改动</strong>，之后在机型列表点「恢复默认」即可还原。
              </span>
            </div>
          ) : null}

          {/* ---------------- 基本信息 ---------------- */}
          <fieldset className="fieldset">
            <legend>基本信息</legend>
            <div className="form-grid">
              <TextField
                id="pf-name"
                label="名称"
                value={draft.name}
                placeholder="例如：我的 Ender-3"
                onChange={(v) => patch({ name: v })}
              />
              <TextField
                id="pf-maker"
                label="厂商"
                value={draft.manufacturer}
                placeholder="例如：Creality"
                onChange={(v) => patch({ manufacturer: v })}
              />
            </div>
          </fieldset>

          {/* ---------------- 喷头与材料 ---------------- */}
          <fieldset className="fieldset">
            <legend>喷头与材料</legend>
            <div className="form-grid">
              <div className="field">
                <label className="field-label" htmlFor="pf-nozzle">
                  喷嘴直径
                </label>
                <select
                  id="pf-nozzle"
                  className="select"
                  value={customNozzle ? 'custom' : draft.nozzle_mm}
                  onChange={(e) => {
                    if (e.target.value === 'custom') {
                      setCustomNozzle(true);
                      return;
                    }
                    setCustomNozzle(false);
                    // 选「无喷嘴」时同步把送料方式切成光固化；选回数值时切回近端送料
                    if (e.target.value === '0') {
                      patch({ nozzle_mm: '0', extruder: 'resin' });
                    } else {
                      patch({
                        nozzle_mm: e.target.value,
                        extruder: draft.extruder === 'resin' ? 'direct' : draft.extruder,
                      });
                    }
                  }}
                >
                  {NOZZLE_CHOICES.map((n) => (
                    <option key={n} value={String(n)}>
                      {n}mm
                    </option>
                  ))}
                  <option value="0">无喷嘴（光固化）</option>
                  <option value="custom">自定义…</option>
                </select>
                <span className="field-hint">
                  喷嘴越细细节越好、越慢；越粗越快但细纹会糊。喷嘴决定最小壁厚（约 3 倍喷嘴）。
                </span>
              </div>

              {customNozzle ? (
                <NumField
                  id="pf-nozzle-custom"
                  label="自定义喷嘴直径"
                  value={draft.nozzle_mm}
                  unit="mm"
                  step="0.05"
                  hint="常见值 0.2 / 0.4 / 0.6 / 0.8mm。拿不准就填 0.4mm。"
                  onChange={(v) => patch({ nozzle_mm: v })}
                />
              ) : null}

              <NumField
                id="pf-filament"
                label="耗材直径"
                value={draft.filament_diameter_mm}
                unit="mm"
                step="0.05"
                hint="绝大多数桌面机是 1.75mm；老机器可能是 2.85mm。不确定就保持 1.75。"
                onChange={(v) => patch({ filament_diameter_mm: v })}
              />

              <NumField
                id="pf-hotend"
                label="最高喷头温度"
                value={draft.max_hotend_temp_c}
                unit="℃"
                step="5"
                hint="决定能不能打 ABS/PA 等高温材料。常见值是 260℃（全金属热端 300℃）。"
                onChange={(v) => patch({ max_hotend_temp_c: v })}
              />

              <NumField
                id="pf-bed-temp"
                label="最高热床温度"
                value={draft.max_bed_temp_c}
                unit="℃"
                step="5"
                hint="热床温度不够时首层容易粘不住。常见值是 100℃，封闭机型可到 120℃。"
                onChange={(v) => patch({ max_bed_temp_c: v })}
              />
            </div>

            <div className="field">
              <span className="field-label">可用材料</span>
              <div className="material-grid">
                {materialChoices.map((m) => {
                  const on = draft.materials.includes(m);
                  return (
                    <button
                      key={m}
                      type="button"
                      className={`material-chip${on ? ' is-active' : ''}`}
                      aria-pressed={on}
                      onClick={() => toggleMaterial(m)}
                    >
                      <span className="material-chip-name">{m}</span>
                      {materialHint(m) ? (
                        <span className="material-chip-hint">{materialHint(m)}</span>
                      ) : null}
                    </button>
                  );
                })}
              </div>
              <span className="field-hint">
                只勾选你实际会用的材料。AI 会按第一种材料估算密度与热变形温度。
              </span>
            </div>

            <div className="switch-row">
              <div>
                <div className="field-label">封闭腔体</div>
                <div className="field-hint">
                  有外壳/罩子就打开。ABS、ASA、PA 这类材料必须封闭，否则大件会翘边开裂。
                </div>
              </div>
              <button
                type="button"
                role="switch"
                className="switch"
                aria-checked={draft.enclosure}
                aria-label="封闭腔体"
                onClick={() => patch({ enclosure: !draft.enclosure })}
              />
            </div>

            <div className="switch-row">
              <div>
                <div className="field-label">有热床</div>
                <div className="field-hint">几乎全部 FDM 机型都有；光固化没有热床。</div>
              </div>
              <button
                type="button"
                role="switch"
                className="switch"
                aria-checked={draft.has_heated_bed}
                aria-label="有热床"
                onClick={() => patch({ has_heated_bed: !draft.has_heated_bed })}
              />
            </div>
          </fieldset>

          {/* ---------------- 成型尺寸 ---------------- */}
          <fieldset className="fieldset">
            <legend>成型尺寸</legend>
            <div className="form-grid form-grid-3">
              <NumField
                id="pf-x"
                label="X 方向"
                value={draft.build_x}
                unit="mm"
                step="1"
                hint="能打印的最大宽度。"
                onChange={(v) => patch({ build_x: v })}
              />
              <NumField
                id="pf-y"
                label="Y 方向"
                value={draft.build_y}
                unit="mm"
                step="1"
                hint="能打印的最大深度。"
                onChange={(v) => patch({ build_y: v })}
              />
              <NumField
                id="pf-z"
                label="Z 方向"
                value={draft.build_z}
                unit="mm"
                step="1"
                hint="能打印的最大高度。"
                onChange={(v) => patch({ build_z: v })}
              />
            </div>
            <span className="field-hint">
              按官方标称值填即可。超大或超高的模型会被提示缩小、旋转或分件。
            </span>
          </fieldset>

          {/* ---------------- 打印能力 ---------------- */}
          <fieldset className="fieldset">
            <legend>打印能力</legend>
            <div className="form-grid">
              <NumField
                id="pf-layer-min"
                label="最小层高"
                value={draft.layer_min_mm}
                unit="mm"
                step="0.01"
                hint="越小越精细。0.05–0.1mm 是常见范围，填标称最小值即可。"
                onChange={(v) => patch({ layer_min_mm: v })}
              />
              <NumField
                id="pf-layer-max"
                label="最大层高"
                value={draft.layer_max_mm}
                unit="mm"
                step="0.01"
                hint="越大约快但层纹明显。一般不超过喷嘴的 0.8 倍。"
                onChange={(v) => patch({ layer_max_mm: v })}
              />
              <NumField
                id="pf-speed"
                label="最大速度"
                value={draft.max_speed_mm_s}
                unit="mm/s"
                step="10"
                hint="影响打印时间与 AI 的走线建议，填官方标称值即可。"
                onChange={(v) => patch({ max_speed_mm_s: v })}
              />
              <NumField
                id="pf-accel"
                label="最大加速度"
                value={draft.max_accel_mm_s2}
                unit="mm/s²"
                step="100"
                hint="决定拐角与细节的稳定程度。不确定就保持默认，影响不大。"
                onChange={(v) => patch({ max_accel_mm_s2: v })}
              />
              <NumField
                id="pf-flow"
                label="最大挤出流量"
                value={draft.max_flow_mm3_s}
                unit="mm³/s"
                step="0.5"
                hint="决定粗喷嘴能跑多快。不确定就保持默认。"
                onChange={(v) => patch({ max_flow_mm3_s: v })}
              />
              <NumField
                id="pf-overhang"
                label="最大悬垂角"
                value={draft.recommended_max_overhang_deg}
                unit="°"
                step="1"
                hint="超过这个角度的斜面需要支撑。开放机型一般 50–55°，带强冷却可到 60°。"
                onChange={(v) => patch({ recommended_max_overhang_deg: v })}
              />
              <NumField
                id="pf-bridge"
                label="最大桥接跨度"
                value={draft.recommended_max_bridge_mm}
                unit="mm"
                step="1"
                hint="两个支点之间能直接拉过去的距离。超限时 AI 会加肋或改成拱形。"
                onChange={(v) => patch({ recommended_max_bridge_mm: v })}
              />
              <NumField
                id="pf-tolerance"
                label="配合公差"
                value={draft.dimensional_tolerance_mm}
                unit="mm"
                step="0.05"
                hint="决定轴孔之间留多少缝隙。0.2–0.3mm 比较稳妥，太小会装不上。"
                onChange={(v) => patch({ dimensional_tolerance_mm: v })}
              />
              <SelectField
                id="pf-kinematics"
                label="运动结构"
                value={draft.kinematics}
                options={KINEMATICS}
                hint="影响有效成型区域（Delta 是圆形）与速度表现。不确定选「框架式」。"
                onChange={(v) => patch({ kinematics: v })}
              />
              <SelectField
                id="pf-extruder"
                label="送料方式"
                value={draft.extruder}
                options={EXTRUDERS}
                hint="近端送料更适合软性材料。不确定就选近端。"
                onChange={(v) => patch({ extruder: v })}
              />
              <SelectField
                id="pf-leveling"
                label="调平方式"
                value={draft.bed_leveling}
                options={LEVELING}
                hint="只影响提示文案，不会改变模型设计。不确定选「手动调平」。"
                onChange={(v) => patch({ bed_leveling: v })}
              />
              <TextField
                id="pf-surface"
                label="打印平台"
                value={draft.bed_surface}
                placeholder="例如：PEI 弹簧钢板"
                hint="选填。写清楚方便自己以后核对。"
                onChange={(v) => patch({ bed_surface: v })}
              />
            </div>
            {resin ? (
              <div className="notice">
                <span>已按光固化机型处理：不检查喷嘴相关壁厚，改按最小可成型细节评估。</span>
              </div>
            ) : null}
          </fieldset>
        </div>

        <LivePreview preview={preview} loading={previewing} error={previewError} />
      </div>

      <div className="modal-foot">
        <Button onClick={onCancel} disabled={saving}>
          取消
        </Button>
        <Button type="submit" variant="primary" loading={saving}>
          {source ? '保存' : '创建机型'}
        </Button>
      </div>
    </form>
  );
}

/* ------------------------------------------------------------------ *
 * 机型管理
 * ------------------------------------------------------------------ */
export function PrinterManager({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { printers, selectedId, materials: allMaterials, reload } = usePrinters();
  const toast = useToast();

  /** null = 列表；'new' = 新建；PrinterProfile = 编辑 */
  const [editing, setEditing] = useState<PrinterProfile | 'new' | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<PrinterProfile | null>(null);
  /** 被改过的内置机型：删除即「恢复出厂参数」，界面要按这个语义表达 */
  const [restoreTarget, setRestoreTarget] = useState<PrinterProfile | null>(null);
  const [resetOpen, setResetOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) setEditing(null);
  }, [open]);

  /** 后端把「删除自定义机型」与「撤销内置覆盖」都实现为同一个 DELETE */
  const dropPrinter = async (target: PrinterProfile, mode: 'delete' | 'restore') => {
    setBusy(true);
    try {
      await api.deletePrinter(target.id);
      const wasSelected = target.id === selectedId;
      const name = printerTitle(target);
      if (mode === 'restore') {
        if (wasSelected) toast.warn(`「${name}」已恢复为内置参数，当前机型因为被重置已切回通用机型。`);
        else toast.ok(`「${name}」已恢复为内置参数。`);
      } else if (wasSelected) {
        toast.warn(`「${name}」已删除，当前机型已切回通用机型。`);
      } else {
        toast.ok(`「${name}」已删除。`);
      }
      setDeleteTarget(null);
      setRestoreTarget(null);
      await reload();
    } catch (err) {
      toast.error(toFriendlyError(err).message);
    } finally {
      setBusy(false);
    }
  };

  const handleReset = async () => {
    setBusy(true);
    try {
      const res = await api.resetPrinters();
      toast.ok(res.message || '已恢复内置机型库。');
      setResetOpen(false);
      await reload();
    } catch (err) {
      toast.error(toFriendlyError(err).message);
    } finally {
      setBusy(false);
    }
  };

  if (editing !== null) {
    return (
      <Modal
        open={open}
        xwide
        title={editing === 'new' ? '新建机型' : `编辑：${printerTitle(editing)}`}
        onClose={() => setEditing(null)}
      >
        <PrinterEditor
          source={editing === 'new' ? null : editing}
          allMaterials={allMaterials}
          onCancel={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void reload();
          }}
        />
      </Modal>
    );
  }

  return (
    <>
      <Modal
        open={open}
        wide
        title="管理机型"
        onClose={onClose}
        footer={
          <>
            <Button onClick={() => setResetOpen(true)} icon={<IconRefresh size={15} />}>
              恢复内置机型库
            </Button>
            <Button variant="primary" onClick={() => setEditing('new')} icon={<IconPlus size={15} />}>
              新建机型
            </Button>
          </>
        }
      >
        <div className="printer-list">
          {printers.map((printer) => {
            // builtin + updated_at 非空 = 用户改过的内置机型（保存为覆盖版本，可以撤销）
            const overridden = printer.builtin && printer.updated_at !== '';
            return (
              <div
                className={`printer-manage-row${printer.id === selectedId ? ' is-active' : ''}`}
                key={printer.id}
              >
                <div className="printer-row-main">
                  <div className="printer-row-name">
                    {printerTitle(printer)}
                    <span className={printer.builtin ? 'tag' : 'tag tag-ok'}>
                      {printer.builtin ? '内置' : '自定义'}
                    </span>
                    {overridden ? (
                      <span className="tag tag-warn" title="你修改过这台内置机型，参数已保存为你的版本">
                        已修改
                      </span>
                    ) : null}
                    {printer.id === selectedId ? <span className="tag tag-ok">当前使用</span> : null}
                  </div>
                  <div className="printer-row-spec">{printerSpecSummary(printer)}</div>
                </div>
                <div className="printer-row-actions">
                  <Button
                    size="sm"
                    icon={<IconPencil size={14} />}
                    onClick={() => setEditing(printer)}
                    aria-label={`编辑 ${printerTitle(printer)}`}
                  >
                    编辑
                  </Button>
                  {overridden ? (
                    <Button
                      size="sm"
                      icon={<IconRefresh size={14} />}
                      title="撤销你对这台内置机型的修改，参数回到出厂值"
                      aria-label={`把 ${printerTitle(printer)} 恢复为出厂参数`}
                      onClick={() => setRestoreTarget(printer)}
                    >
                      恢复默认
                    </Button>
                  ) : printer.builtin ? null : (
                    <Button
                      size="sm"
                      variant="danger"
                      icon={<IconTrash size={14} />}
                      title="删除这台自定义机型"
                      aria-label={`删除 ${printerTitle(printer)}`}
                      onClick={() => setDeleteTarget(printer)}
                    >
                      删除
                    </Button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
        <p className="field-hint" style={{ marginTop: 'var(--sp-3)' }}>
          内置机型也可以编辑：保存后会生成你的自定义版本（列表里标为「已修改」），内置原值保留，
          随时点「恢复默认」还原。
        </p>
      </Modal>

      <ConfirmModal
        open={deleteTarget !== null}
        danger
        busy={busy}
        title="删除机型"
        message={<>将删除「{deleteTarget ? printerTitle(deleteTarget) : ''}」。删除后需要重新创建，确定吗？</>}
        confirmText="删除"
        onCancel={() => setDeleteTarget(null)}
        onConfirm={() => deleteTarget && void dropPrinter(deleteTarget, 'delete')}
      />

      <ConfirmModal
        open={restoreTarget !== null}
        busy={busy}
        title="恢复默认参数"
        message={
          <>
            将撤销你对「{restoreTarget ? printerTitle(restoreTarget) : ''}」的修改，
            参数回到内置原值。你自己新建的机型不受影响，之后也可以再改。
          </>
        }
        confirmText="恢复默认"
        onCancel={() => setRestoreTarget(null)}
        onConfirm={() => restoreTarget && void dropPrinter(restoreTarget, 'restore')}
      />

      <ConfirmModal
        open={resetOpen}
        danger
        busy={busy}
        title="恢复内置机型库"
        message={
          <>
            将清空你所有的自定义机型与改动（当前共 {printers.filter((p) => !p.builtin).length}{' '}
            台自定义机型），并把当前选择恢复为通用机型。内置机型库会回到出厂状态。确定要恢复吗？
          </>
        }
        confirmText="恢复"
        onCancel={() => setResetOpen(false)}
        onConfirm={() => void handleReset()}
      />
    </>
  );
}

/** 知识库自述：这些参数到底影响什么（可折叠） */
export function PrinterKnowledge() {
  const [summary, setSummary] = useState<PrinterKnowledgeSummary | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    void api
      .printerKnowledge()
      .then((res) => {
        if (alive) setSummary(res);
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, []);

  if (failed) return null;

  return (
    <div style={{ marginTop: 'var(--sp-4)' }}>
      <Accordion title="这些参数是怎么影响模型的？">
        {summary ? (
          <>
            <p>
              机型库内置了 {summary.builtin_count} 台常见打印机。下面每一项都会真实改变 AI
              的设计方式，不是装饰：
            </p>
            <dl className="kv-list">
              {summary.features.map((feature) => (
                <div key={feature.key} style={{ display: 'contents' }}>
                  <dt>{feature.label}</dt>
                  <dd>{feature.affects}</dd>
                </div>
              ))}
            </dl>
          </>
        ) : (
          <p className="muted small">正在读取知识库…</p>
        )}
      </Accordion>
    </div>
  );
}

/** 机型清单：人话版的设计规则列表 */
export function PrinterChecklist({ items }: { items: string[] }) {
  if (items.length === 0) return null;
  return (
    <ul className="rule-list rule-list-grid">
      {items.map((line) => (
        <li key={line}>{line}</li>
      ))}
    </ul>
  );
}
