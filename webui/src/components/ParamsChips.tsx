import { useEffect, useMemo, useState } from 'react';
import type { JobParam } from '../api/types';
import { Button } from './Button';
import { IconSliders } from './Icons';

interface ParamsChipsProps {
  params: JobParam[];
  /** 作业运行中时禁用编辑 */
  disabled?: boolean;
  applying?: boolean;
  onApply: (values: Record<string, number>) => void;
}

/**
 * 参数微调：把 result.params 渲染成内联数字输入。
 * [应用参数] 只调用 POST /api/projects/{id}/params（不经过 AI），秒级重编译。
 */
export function ParamsChips({ params, disabled, applying, onApply }: ParamsChipsProps) {
  const initial = useMemo(() => {
    const map: Record<string, number> = {};
    for (const p of params) map[p.name] = p.value;
    return map;
  }, [params]);

  const [draft, setDraft] = useState<Record<string, number>>(initial);

  // 参数表变化（新版本生成完成）时重置草稿
  useEffect(() => {
    setDraft(initial);
  }, [initial]);

  const dirtyNames = params
    .filter((p) => draft[p.name] !== undefined && draft[p.name] !== p.value)
    .map((p) => p.name);
  const dirty = dirtyNames.length > 0;

  const clamp = (p: JobParam, raw: string): number => {
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) return p.value;
    let value = parsed;
    if (p.min !== null && value < p.min) value = p.min;
    if (p.max !== null && value > p.max) value = p.max;
    return value;
  };

  return (
    <section className="params-sheet" aria-label="参数微调">
      <div className="params-head">
        <div className="params-title">
          <IconSliders size={14} style={{ verticalAlign: -2, marginRight: 6 }} />
          参数微调
          <span>改完点「应用参数」，不会调用 AI，几秒出结果</span>
        </div>
        <div className="row" style={{ gap: 6 }}>
          {dirty ? (
            <Button size="sm" variant="ghost" onClick={() => setDraft(initial)} disabled={disabled}>
              还原
            </Button>
          ) : null}
          <Button
            size="sm"
            variant={dirty ? 'primary' : 'default'}
            disabled={!dirty || disabled}
            loading={applying}
            onClick={() => {
              const next: Record<string, number> = {};
              for (const name of dirtyNames) next[name] = draft[name];
              onApply(next);
            }}
          >
            应用参数
          </Button>
        </div>
      </div>

      <div className="params-grid">
        {params.map((p) => {
          const value = draft[p.name] ?? p.value;
          const isDirty = value !== p.value;
          return (
            <label className="chip" key={p.name}>
              <span className="chip-label" title={`${p.label}（${p.name}）`}>
                {p.label}
              </span>
              <span className={`chip-input${isDirty ? ' is-dirty' : ''}`}>
                <input
                  type="number"
                  inputMode="decimal"
                  aria-label={`${p.label}${p.unit ? `，单位${p.unit}` : ''}`}
                  value={Number.isFinite(value) ? value : ''}
                  min={p.min ?? undefined}
                  max={p.max ?? undefined}
                  step="any"
                  disabled={disabled}
                  onChange={(e) => setDraft((prev) => ({ ...prev, [p.name]: Number(e.target.value) }))}
                  onBlur={(e) => setDraft((prev) => ({ ...prev, [p.name]: clamp(p, e.target.value) }))}
                />
                {p.unit ? <span className="chip-unit">{p.unit}</span> : null}
              </span>
            </label>
          );
        })}
      </div>
    </section>
  );
}
