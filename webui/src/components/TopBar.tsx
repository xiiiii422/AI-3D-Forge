import { useState } from 'react';
import { NavLink, useLocation, useNavigate } from 'react-router-dom';
import { api, MODE_PRESETS } from '../api/client';
import { EnvPill } from './EnvPill';
import { Button } from './Button';
import { IconLogo, IconSettings } from './Icons';
import { PrinterChip } from './PrinterPicker';
import { useToast } from './Toast';
import { useSettings } from '../hooks/useSettings';
import { studioPath } from '../lib/lastProject';
import type { EnvReport } from '../api/types';

interface TopBarProps {
  report: EnvReport | null;
  loading: boolean;
}

const NAV = [
  { to: '/diagnostics', label: '系统诊断' },
  { to: '/about', label: '关于' },
];

/** 顶栏：应用名 + 环境就绪胶囊 + 机型胶囊 + 导航 + 设置入口 */
export function TopBar({ report, loading }: TopBarProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const inStudio = location.pathname.startsWith('/studio');
  // 工作台入口要带回**本会话最后打开的项目**：否则从设置/诊断点回来会落到空工作台，
  // 用户看到的是"我刚才的模型和进度都没了"（而作业其实还在跑）。
  const home = studioPath();

  return (
    <header className="topbar">
      <div className="row" style={{ minWidth: 0 }}>
        <a className="brand" href={`#${home}`} onClick={(e) => { e.preventDefault(); navigate(home); }}>
          <span className="brand-mark">
            <IconLogo size={15} />
          </span>
          <span className="nowrap">AI 3D Forge</span>
        </a>
      </div>

      <div className="row" style={{ gap: 'var(--sp-3)' }}>
        {!inStudio ? (
          <nav className="topbar-nav" aria-label="主导航">
            <NavLink
              to={home}
              className={({ isActive }) => (isActive ? 'is-active' : undefined)}
            >
              工作台
            </NavLink>
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) => (isActive ? 'is-active' : undefined)}
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
        ) : null}

        <EnvPill
          okCount={report?.ok_count ?? 0}
          total={report?.total ?? 0}
          loading={loading}
          onClick={() => navigate('/diagnostics')}
        />

        {/* 工作台里紧挨「环境就绪」显示当前机型，点击即可更换 */}
        {inStudio ? <PrinterChip /> : null}

        {/* 一键切换使用模式：免费 / 付费均衡 / 混合性价比 */}
        {inStudio ? <ModeChip /> : null}

        <Button
          iconOnly
          variant="ghost"
          aria-label="设置"
          title="设置"
          onClick={() => navigate('/settings')}
        >
          <IconSettings size={17} />
        </Button>
      </div>
    </header>
  );
}

/** 顶栏“模式”下拉：免费 / DeepSeek 付费 / 混合性价比，选中即保存 base_url+model+经济档 */
function ModeChip() {
  const { settings, reload } = useSettings();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  if (!settings) return null;
  const currentId = MODE_PRESETS.find(
    (m) =>
      m.base_url === settings.base_url &&
      m.model === settings.model &&
      (m.economy_model || '') === (settings.economy_model || '') &&
      (m.economy_base_url || '') === (settings.economy_base_url || ''),
  )?.id;
  return (
    <label
      className="mode-chip"
      title="切换使用模式：免费 / 付费均衡 / 混合性价比（生成付费、解析评审免费）"
    >
      <span>模式</span>
      <select
        className="select"
        value={currentId ?? 'custom'}
        disabled={busy}
        onChange={async (e) => {
          const preset = MODE_PRESETS.find((m) => m.id === e.target.value);
          if (!preset) return;
          setBusy(true);
          try {
            await api.updateSettings({
              base_url: preset.base_url,
              model: preset.model,
              economy_base_url: preset.economy_base_url,
              economy_model: preset.economy_model,
            });
            await reload();
            toast.ok(`已切换到「${preset.label}」`);
          } catch {
            toast.error('切换模式失败');
          } finally {
            setBusy(false);
          }
        }}
      >
        {MODE_PRESETS.map((m) => (
          <option key={m.id} value={m.id}>
            {m.label}
          </option>
        ))}
        {currentId === undefined ? <option value="custom">自定义（{settings.model}）</option> : null}
      </select>
    </label>
  );
}
