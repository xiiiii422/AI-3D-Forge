import { useEffect, useMemo, useState } from 'react';
import type { ChangeVerdict, DesignSpec, SpecEventPayload, SpecItem } from '../api/types';
import { Button } from './Button';
import { IconAlert, IconCheck, IconPencil, IconSparkle, IconX } from './Icons';

/* ------------------------------------------------------------------ *
 * 规格确认卡片
 *
 * 为什么必须有这张卡片：以前 AI 直接按自己的理解开工，用户只能在结果出来
 * 之后才发现「方向跑偏」，而且每次说的要求下一轮就可能被忘掉。
 * 现在把「我理解成了什么」先用可编辑的数字摆出来，用户确认或纠正后才开始
 * 设计；纠正过的规格会持久化进项目，后续每一轮都带着它。
 * ------------------------------------------------------------------ */

interface SpecCardProps {
  payload: SpecEventPayload;
  /** 正在提交确认请求 */
  busy?: boolean;
  /** 后端已进入生成阶段 → 卡片收成只读版 */
  locked?: boolean;
  onConfirm: (spec: DesignSpec | null, note: string) => void | Promise<void>;
  onCancel: () => void;
}

/** 把「未指定」的项排到后面：用户真正说过的数字应当先被看到 */
function sortSpecs(items: SpecItem[]): SpecItem[] {
  return [...items].sort((a, b) => {
    if (a.user_specified !== b.user_specified) return a.user_specified ? -1 : 1;
    const av = a.value === null ? 1 : 0;
    const bv = b.value === null ? 1 : 0;
    if (av !== bv) return av - bv;
    return 0;
  });
}

export function SpecCard({ payload, busy, locked, onConfirm, onCancel }: SpecCardProps) {
  const { spec } = payload;
  /** 用户改过的数值：参数名 → 输入框里的原始字符串 */
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [note, setNote] = useState('');
  const [editing, setEditing] = useState(false);

  // 换了一份新规格（例如用户确认后又纠正过）就重置草稿
  useEffect(() => {
    if (locked) return;
    setDrafts({});
    setNote('');
    setEditing(false);
  }, [spec.fingerprint, locked]);

  const ordered = useMemo(() => sortSpecs(spec.specs), [spec.specs]);
  const numeric = ordered.filter((item) => item.value !== null);
  const unspecified = ordered.filter((item) => item.value === null);
  // 只读版（已确认 / 结果里回顾）不再展示「你没说清」这类待办问题
  const questions = locked ? [] : spec.open_questions;
  const dirtyNames = Object.keys(drafts).filter((name) => {
    const item = spec.specs.find((s) => s.name === name);
    if (!item || item.value === null) return false;
    const parsed = Number(drafts[name]);
    return Number.isFinite(parsed) && parsed !== item.value;
  });

  const handleConfirm = () => {
    // 只有用户真的改了数字才回传整份规格；否则 null = 「就按你理解的做」
    let next: DesignSpec | null = null;
    if (dirtyNames.length > 0) {
      next = {
        ...spec,
        specs: spec.specs.map((item) => {
          if (!dirtyNames.includes(item.name)) return item;
          // 夹到规格自带的合理区间：0 或负的高度这类值不该送去生成
          let parsed = Number(drafts[item.name]);
          if (item.min !== null && parsed < item.min) parsed = item.min;
          if (item.max !== null && parsed > item.max) parsed = item.max;
          return { ...item, value: parsed, user_specified: true };
        }),
      };
    }
    void onConfirm(next, note.trim());
  };

  return (
    <div className="spec-card" role="group" aria-label="需求确认">
      <div className="spec-head">
        <IconSparkle size={15} style={{ color: 'var(--accent)', flex: 'none' }} />
        <div className="spec-head-main">
          <strong>{spec.summary || spec.kind || '你要的模型'}</strong>
          <div className="muted-3 xs">
            {locked
              ? '已按这份规格开始设计'
              : '先确认一下我理解得对不对 —— 数字可以直接改，改了我就按你的算'}
          </div>
        </div>
        {locked ? (
          <span className="tag tag-ok">
            <IconCheck size={12} style={{ marginRight: 4 }} />
            已确认
          </span>
        ) : (
          <button
            type="button"
            className="spec-edit-toggle"
            aria-pressed={editing}
            onClick={() => setEditing((v) => !v)}
          >
            <IconPencil size={13} />
            {editing ? '收起编辑' : '改数字'}
          </button>
        )}
      </div>

      {numeric.length > 0 ? (
        <div className="spec-grid">
          {numeric.map((item) => {
            const display = drafts[item.name] ?? String(item.value);
            const dirty = dirtyNames.includes(item.name);
            const range =
              item.min !== null || item.max !== null
                ? `${item.min ?? '—'}–${item.max ?? '—'}${item.unit}`
                : '';
            return (
              <label
                className={`spec-cell${item.user_specified ? ' is-user' : ''}${dirty ? ' is-dirty' : ''}`}
                key={item.name}
              >
                <span className="spec-label" title={item.display}>
                  {item.label}
                </span>
                {editing && !locked ? (
                  <span className="spec-input">
                    <input
                      className="input"
                      inputMode="decimal"
                      value={display}
                      aria-label={item.label}
                      onChange={(e) =>
                        setDrafts((prev) => ({ ...prev, [item.name]: e.target.value }))
                      }
                    />
                    <span className="spec-unit">{item.unit}</span>
                  </span>
                ) : (
                  <b className="spec-value">
                    {item.value}
                    <span className="spec-unit">{item.unit}</span>
                  </b>
                )}
                <span className="spec-origin">
                  {item.user_specified ? '你指定' : '按常用值'}
                  {editing && range ? ` · 可填 ${range}` : ''}
                </span>
              </label>
            );
          })}
        </div>
      ) : null}

      {unspecified.length > 0 ? (
        <div className="spec-line">
          <span className="spec-line-key">没给数字的项</span>
          <span className="spec-line-val">{unspecified.map((s) => s.label).join('、')}</span>
        </div>
      ) : null}

      {spec.features.length > 0 ? (
        <div className="spec-line">
          <span className="spec-line-key">结构要点</span>
          <span className="spec-line-val">{spec.features.join('、')}</span>
        </div>
      ) : null}

      {spec.prohibitions.length > 0 ? (
        <div className="spec-line">
          <span className="spec-line-key">明确不要</span>
          <span className="spec-line-val">{spec.prohibitions.join('、')}</span>
        </div>
      ) : null}

      {spec.acceptance.length > 0 ? (
        <div className="spec-line">
          <span className="spec-line-key">验收标准</span>
          <span className="spec-line-val">{spec.acceptance.join('；')}</span>
        </div>
      ) : null}

      {spec.clarifications.length > 0 ? (
        <div className="spec-line">
          <span className="spec-line-key">你说过的</span>
          <span className="spec-line-val">{spec.clarifications.join('；')}</span>
        </div>
      ) : null}

      {questions.length > 0 ? (
        <div className="spec-questions">
          <IconAlert size={14} style={{ flex: 'none', marginTop: 2 }} />
          <div>
            <strong>这几点你没说清，我按常见做法先定了：</strong>
            <ul>
              {questions.map((q, i) => (
                <li key={`${q}-${i}`}>{q}</li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}

      {!locked ? (
        <>
          <div className="spec-note">
            <input
              className="input"
              placeholder="还有别的要求？写在这里，之后每一轮都会记住（可留空）"
              value={note}
              maxLength={200}
              aria-label="补充说明"
              onChange={(e) => setNote(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleConfirm();
              }}
            />
          </div>
          <div className="spec-actions">
            <Button variant="primary" size="sm" loading={busy} onClick={handleConfirm}>
              {dirtyNames.length > 0 || note.trim()
                ? '按我改的来，开始设计'
                : '就是这样，开始设计'}
            </Button>
            <Button size="sm" variant="ghost" icon={<IconX size={14} />} onClick={onCancel}>
              取消
            </Button>
            {dirtyNames.length > 0 ? (
              <span className="muted-3 xs">已改动 {dirtyNames.length} 项</span>
            ) : null}
          </div>
        </>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 改动核对结论
 *
 * 用户抱怨的核心是「改完可能不会改善、还顺手改了别的」。
 * 所以每次修改都逐条亮出来：你点名的那项改没改到、别处有没有被动过。
 * ------------------------------------------------------------------ */

const VERDICT_TONE: Record<string, string> = {
  ok: 'change-verdict-ok',
  warn: 'change-verdict-warn',
  bad: 'change-verdict-bad',
};

export function ChangeVerdictCard({
  verdict,
  summary,
}: {
  verdict: ChangeVerdict;
  summary?: string | null;
}) {
  const tone = verdict.ok ? 'ok' : !verdict.performed || !verdict.spec_intact ? 'bad' : 'warn';
  const headline = verdict.ok
    ? '这次改动只动了你要改的地方'
    : !verdict.performed
      ? '你要的改动没完全生效'
      : verdict.unauthorized.length > 0
        ? '顺带改了没让你改的地方'
        : '改动通过，但有偏差';

  return (
    <div className={`change-verdict ${VERDICT_TONE[tone]}`}>
      <div className="change-verdict-head">
        {verdict.ok ? (
          <IconCheck size={14} style={{ color: 'var(--ok)', flex: 'none' }} />
        ) : (
          <IconAlert size={14} style={{ color: 'var(--warn)', flex: 'none' }} />
        )}
        <strong>{headline}</strong>
      </div>
      {summary ? <div className="change-verdict-summary">{summary}</div> : null}
      {verdict.reasons.length > 0 ? (
        <ul className="change-verdict-reasons">
          {verdict.reasons.map((reason, i) => (
            <li key={`${reason}-${i}`}>{reason}</li>
          ))}
        </ul>
      ) : null}
      {verdict.unauthorized.length > 0 ? (
        <div className="change-verdict-unauth">
          被顺带改动：{verdict.unauthorized.join('、')}（下次可以要求「只改 X，别动别的」）
        </div>
      ) : null}
    </div>
  );
}
