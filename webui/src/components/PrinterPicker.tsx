import { useEffect, useMemo, useState } from 'react';
import type { PrinterProfile } from '../api/types';
import {
  materialHint,
  num,
  printerImpactToast,
  printerSpecSummary,
  printerTitle,
  trimNum,
  usePrinters,
} from '../hooks/usePrinters';
import { Button } from './Button';
import { IconCheck, IconNozzle, IconSearch } from './Icons';
import { Modal } from './Modal';
import { useToast } from './Toast';

interface PrinterPickerProps {
  open: boolean;
  onClose: () => void;
  /** 列表下方的补充说明（设置页用），可选 */
  hint?: string;
}

/**
 * 机型选择器 —— 工作台与设置页共用同一个（契约 §3A `GET/POST /api/printers`）。
 *
 * 交互：搜索 + 内置/自定义分组 → 选中机型；若该机型支持多种材料，
 * 再让用户选材料（默认沿用当前材料，不可用时取该机型的第一种）。
 */
export function PrinterPicker({ open, onClose, hint }: PrinterPickerProps) {
  const { printers, selectedId, selectedMaterial, select, selecting } = usePrinters();
  const toast = useToast();

  const [query, setQuery] = useState('');
  /** 正在为哪台机器选材料（null = 显示机型列表） */
  const [pending, setPending] = useState<PrinterProfile | null>(null);
  const [material, setMaterial] = useState('');
  const [failed, setFailed] = useState<string | null>(null);

  // 每次打开都回到「机型列表」这一步，避免上次的材料选择残留
  useEffect(() => {
    if (!open) return;
    setQuery('');
    setPending(null);
    setFailed(null);
  }, [open]);

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const match = (p: PrinterProfile) =>
      !q ||
      p.name.toLowerCase().includes(q) ||
      p.manufacturer.toLowerCase().includes(q) ||
      p.materials.join('/').toLowerCase().includes(q);
    const hit = printers.filter(match);
    return [
      { title: '内置机型', items: hit.filter((p) => p.builtin) },
      { title: '我的机型', items: hit.filter((p) => !p.builtin) },
    ].filter((g) => g.items.length > 0);
  }, [printers, query]);

  const total = groups.reduce((sum, g) => sum + g.items.length, 0);

  const apply = async (printer: PrinterProfile, chosen: string) => {
    setFailed(null);
    const outcome = await select(printer.id, chosen);
    if (outcome.ok) {
      toast.ok(printerImpactToast(outcome.printer, outcome.material));
      onClose();
      return;
    }
    setFailed(outcome.message);
  };

  const pick = (printer: PrinterProfile) => {
    setFailed(null);
    if (printer.materials.length > 1) {
      setPending(printer);
      setMaterial(
        printer.materials.includes(selectedMaterial) ? selectedMaterial : printer.materials[0],
      );
      return;
    }
    void apply(printer, printer.materials[0] ?? 'PLA');
  };

  return (
    <Modal
      open={open}
      wide
      title={pending ? '选择材料' : '选择打印机'}
      onClose={onClose}
      footer={
        pending ? (
          <>
            <Button variant="ghost" onClick={() => setPending(null)}>
              返回机型列表
            </Button>
            <Button
              variant="primary"
              loading={selecting}
              onClick={() => void apply(pending, material)}
            >
              使用 {material}
            </Button>
          </>
        ) : (
          <Button onClick={onClose}>关闭</Button>
        )
      }
    >
      {pending ? (
        <div className="stack-sm">
          <div className="printer-picked">
            <span className="printer-picked-name">{printerTitle(pending)}</span>
            <span className="field-hint">{printerSpecSummary(pending)}</span>
          </div>
          {/* Enter 即确认当前材料 */}
          <div className="material-grid" role="radiogroup" aria-label="可用材料">
            {pending.materials.map((m) => (
              <button
                key={m}
                type="button"
                role="radio"
                aria-checked={m === material}
                className={`material-chip${m === material ? ' is-active' : ''}`}
                onClick={() => setMaterial(m)}
              >
                <span className="material-chip-name">
                  {m === material ? <IconCheck size={13} /> : null}
                  {m}
                </span>
                {materialHint(m) ? (
                  <span className="material-chip-hint">{materialHint(m)}</span>
                ) : null}
              </button>
            ))}
          </div>
          <p className="field-hint">
            材料会影响壁厚、桥接与支撑建议。不确定就保持默认，之后可以随时更换。
          </p>
        </div>
      ) : (
        <div className="stack-sm">
          <div className="printer-search">
            <IconSearch size={15} />
            <input
              className="input printer-search-input"
              placeholder="搜索品牌或机型"
              aria-label="搜索打印机"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>

          {printers.length === 0 ? (
            <div className="empty">
              <IconNozzle size={22} />
              <div className="small">正在读取机型库…</div>
            </div>
          ) : null}

          {printers.length > 0 && total === 0 ? (
            <div className="empty">
              <IconNozzle size={22} />
              <div className="small">没有匹配的机型</div>
              <div className="xs muted-3">试试搜索「Bambu」「Prusa」或「光固化」</div>
            </div>
          ) : null}

          <div className="printer-list">
            {groups.map((group) => (
              <div className="printer-group" key={group.title}>
                <div className="printer-group-title">
                  {group.title}
                  <span className="muted-3">{group.items.length}</span>
                </div>
                {group.items.map((printer) => {
                  const active = printer.id === selectedId;
                  return (
                    <button
                      key={printer.id}
                      type="button"
                      className={`printer-row${active ? ' is-active' : ''}`}
                      aria-current={active ? 'true' : undefined}
                      disabled={selecting}
                      onClick={() => pick(printer)}
                    >
                      <span className="printer-row-icon">
                        <IconNozzle size={17} />
                      </span>
                      <span className="printer-row-main">
                        <span className="printer-row-name">
                          {printerTitle(printer)}
                          {active ? <span className="tag tag-ok">当前</span> : null}
                        </span>
                        <span className="printer-row-spec">{printerSpecSummary(printer)}</span>
                      </span>
                      <span className="printer-row-side">
                        {printer.materials.length > 1 ? (
                          <span className="tag">{printer.materials.length} 种材料</span>
                        ) : (
                          <span className="tag">{printer.materials[0] ?? '—'}</span>
                        )}
                      </span>
                    </button>
                  );
                })}
              </div>
            ))}
          </div>

          {failed ? (
            <div className="notice" role="status">
              <span>{failed}</span>
            </div>
          ) : null}

          <p className="field-hint">
            {hint ??
              `共 ${printers.length} 台机型。切换后生成的模型会按新机器的喷嘴与成型范围重新设计。`}
          </p>
        </div>
      )}
    </Modal>
  );
}

/** 机型胶囊：顶栏上显示当前机型与材料，点击打开选择器（自包含状态） */
export function PrinterChip() {
  const { selected, selectedMaterial, loading } = usePrinters();
  const [open, setOpen] = useState(false);

  const label = selected
    ? `${printerTitle(selected)}${selectedMaterial ? ` · ${selectedMaterial}` : ''}`
    : loading
      ? '正在读取机型…'
      : '选择打印机';
  const spec = selected
    ? `${trimNum(selected.nozzle_mm, 2)}mm 喷嘴 · ${num(selected.build_x)}×${num(
        selected.build_y,
      )}×${num(selected.build_z)}mm`
    : '点击选择你的打印机';

  return (
    <>
      <button
        type="button"
        className="printer-chip"
        onClick={() => setOpen(true)}
        title={`${label}｜${spec}｜点击更换`}
        aria-label={`当前打印机：${label}，点击更换`}
        aria-haspopup="dialog"
      >
        <IconNozzle size={15} />
        <span className="printer-chip-text">{label}</span>
      </button>
      <PrinterPicker open={open} onClose={() => setOpen(false)} />
    </>
  );
}
