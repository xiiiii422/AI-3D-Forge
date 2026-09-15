import { NavLink, useLocation, useNavigate } from 'react-router-dom';
import { EnvPill } from './EnvPill';
import { Button } from './Button';
import { IconLogo, IconSettings } from './Icons';
import { PrinterChip } from './PrinterPicker';
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
