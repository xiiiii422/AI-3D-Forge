import type { ReqSuite, ReqTest } from '../api/types';
import { Accordion } from './Accordion';
import { statusPillClass } from './StatusDot';

/**
 * 需求核对表：把「用户提的每条要求」与「导出模型的实测值」逐条对账。
 *
 * 为什么要有这一块（对标 2026 年成熟产品，见 docs/COMPETITIVE-GAPS.md 的 G3/G4）：
 *   * AI 建模品类最集中的抱怨是「尺寸不能保持精确」「看着像但不对」，
 *     而多数工具只会说「生成成功」；实测有评测指出「尺寸、公差仍需人工检查」。
 *   * CADTests（arXiv 2605.07807）的做法是把需求编译成可执行测试、按需求组打分，
 *     并把失败项回喂生成端。这里就是它的**用户可见面**。
 *   * 只给一个「打印评分 92」不回答问题：用户要知道的是「我要的 100mm 到底是不是 100mm」。
 *
 * 状态一律带文字，不用颜色单独表意（无障碍）。
 */
const STATUS_TEXT: Record<ReqTest['status'], string> = {
  pass: '达成',
  fail: '未达成',
  invalid: '没测出来',
  manual: '需确认',
};

const STATUS_PILL: Record<ReqTest['status'], 'ok' | 'fail' | 'warn' | 'checking'> = {
  pass: 'ok',
  fail: 'fail',
  invalid: 'warn',
  manual: 'checking',
};

function trim(value: number | null | undefined, digits = 1): string | null {
  if (value === null || value === undefined || Number.isNaN(value)) return null;
  return Number(value).toFixed(digits).replace(/\.0+$/, '');
}

function detailOf(t: ReqTest): string {
  const parts = [`要求 ${t.expected || '—'}`, `实测 ${t.actual || '—'}`];
  const dev = trim(t.deviation_pct);
  if (dev !== null && t.kind === 'dimension') parts.push(`偏差 ${dev}%`);
  const tol = trim(t.tolerance_mm, 2);
  if (tol !== null) parts.push(`公差 ±${tol}mm`);
  return parts.join(' · ');
}

function Row({ t }: { t: ReqTest }) {
  return (
    <li className="req-row">
      <span className={statusPillClass(STATUS_PILL[t.status])}>{STATUS_TEXT[t.status]}</span>
      <div className="req-main">
        <b className="req-name">
          {t.requirement}
          {t.advisory && t.status !== 'pass' ? <em className="req-tag">工艺建议</em> : null}
        </b>
        <span className="req-nums">{detailOf(t)}</span>
        {t.status === 'fail' && t.fix_hint ? <span className="req-hint">{t.fix_hint}</span> : null}
      </div>
    </li>
  );
}

export function RequirementChecklist({ suite }: { suite: ReqSuite | null | undefined }) {
  if (!suite || !suite.tests.length) return null;

  // 三类「不算达成」要分开：用户提的要求没做到（红）、工艺上偏弱（黄）、压根没测出来（黄）。
  // 混成一类会让「面数偏多」这种建议看起来像「你的需求失败了」。
  const blocking = suite.tests.filter((t) => t.status === 'fail' && !t.advisory);
  const advisory = suite.tests.filter((t) => t.status === 'fail' && t.advisory);
  const unreadable = suite.tests.filter((t) => t.status === 'invalid');
  const manual = suite.tests.filter((t) => t.status === 'manual');
  const scored = suite.tests.filter((t) => t.status !== 'manual');
  const passed = scored.filter((t) => t.status === 'pass').length;
  const clean = !blocking.length && !advisory.length && !unreadable.length;
  const tone: 'ok' | 'warn' | 'fail' = blocking.length ? 'fail' : clean ? 'ok' : 'warn';

  const head = (
    <div className="req-head">
      <span className="field-label" style={{ marginBottom: 0 }}>
        需求核对
      </span>
      <span className={statusPillClass(tone)}>
        {passed}/{scored.length} 达成
      </span>
      <span className="muted small">
        逐条按导出模型的实测几何判定，公差取自你选的机型
        {manual.length ? `；${manual.length} 条属于外观/人工确认` : ''}
      </span>
    </div>
  );

  const list = (
    <ul className="req-list">
      {suite.tests.map((t, i) => (
        <Row t={t} key={`${t.group}-${i}`} />
      ))}
    </ul>
  );

  // 全部达成时收起来，免得结果卡被一堵清单墙占满
  if (clean) {
    return (
      <div className="req-check">
        {head}
        <Accordion title={`查看 ${scored.length} 项实测核对`}>{list}</Accordion>
      </div>
    );
  }

  return (
    <div className="req-check">
      {head}
      {list}
      {blocking.length > 0 ? (
        <p className="field-hint">
          这些数字已经连同实测证据一起交给 AI 定点修正；仍然残留时，直接说
          「把{blocking[0].requirement.replace(/（[^）]*）/g, '')}改成 {blocking[0].expected || '你要的数值'}」
          再改一次最快。
        </p>
      ) : null}
    </div>
  );
}
