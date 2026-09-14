import { useEffect } from 'react';
import { HashRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { api } from './api/client';
import { TopBar } from './components/TopBar';
import { ToastProvider } from './components/Toast';
import { useEnvReport } from './hooks/useEnvReport';
import { PrinterProvider } from './hooks/usePrinters';
import { useTheme } from './hooks/useTheme';
import About from './pages/About';
import Diagnostics from './pages/Diagnostics';
import Settings from './pages/Settings';
import Studio from './pages/Studio';
import Welcome from './pages/Welcome';

/**
 * 根路由：契约 §7 —— first_run=true → /welcome，否则 → /studio。
 * 环境报告与主题提到这一层，避免每个页面各拉一次。
 */
function RootRedirect() {
  const { report, loading, error } = useEnvReport();

  if (loading && !report) {
    return (
      <div className="empty" style={{ minHeight: '50vh' }}>
        <span className="muted">正在准备…</span>
      </div>
    );
  }
  if (error && !report) {
    // 后端还没起来时不要把用户困在空白页：仍然放行到工作台，那里有明确的重试入口
    return <Navigate to="/studio" replace />;
  }
  return <Navigate to={report?.first_run ? '/welcome' : '/studio'} replace />;
}

function ScrollReset() {
  const { pathname } = useLocation();
  useEffect(() => {
    document.querySelector('.app-main')?.scrollTo({ top: 0 });
  }, [pathname]);
  return null;
}

function ThemeBridge() {
  const { setTheme } = useTheme();
  useEffect(() => {
    let alive = true;
    void api
      .getSettings()
      .then((settings) => {
        if (alive && settings.theme) setTheme(settings.theme, { persist: true });
      })
      .catch(() => {
        // 后端不可用时沿用本地偏好，不打扰用户
      });
    return () => {
      alive = false;
    };
  }, [setTheme]);
  return null;
}

function Shell() {
  const { report, loading } = useEnvReport();
  const location = useLocation();
  const isStudio = location.pathname.startsWith('/studio');

  return (
    <div className="app">
      {/* 键盘用户第一个 Tab 到的就是它：跳过顶栏导航直达正文
          （better-accessibility：正文之前有重复外壳时必须提供）。
          注意：这是 HashRouter（地址就是 #/studio），所以**不能**用普通的
          href="#main" —— 那会把 hash 改成 #main，等于把路由跳飞。
          这里拦截默认行为，改为把焦点移到 <main>（它带 tabIndex={-1}）。 */}
      <a
        className="skip-link"
        href="#main"
        onClick={(event) => {
          event.preventDefault();
          const target = document.getElementById('main');
          target?.focus();
          target?.scrollIntoView({ block: 'start' });
        }}
      >
        跳到主要内容
      </a>
      <TopBar report={report} loading={loading && !report} />
      <main
        id="main"
        tabIndex={-1}
        className="app-main"
        style={isStudio ? { overflow: 'hidden', display: 'flex' } : undefined}
      >
        <div style={isStudio ? { flex: 1, minWidth: 0, display: 'flex' } : { display: 'contents' }}>
          <Routes>
            <Route path="/" element={<RootRedirect />} />
            <Route path="/welcome" element={<Welcome />} />
            <Route path="/studio" element={<Studio />} />
            <Route path="/studio/:projectId" element={<Studio />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/diagnostics" element={<Diagnostics />} />
            <Route path="/about" element={<About />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}

export default function App() {
  return (
    // future 标记：提前适配 react-router v7 的行为，同时消掉控制台里的兼容性警告
    <HashRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <ToastProvider>
        {/* 打印机选择是全局状态：顶栏胶囊、设置页、欢迎向导必须看到同一台机器 */}
        <PrinterProvider>
          <ThemeBridge />
          <ScrollReset />
          <Shell />
        </PrinterProvider>
      </ToastProvider>
    </HashRouter>
  );
}
