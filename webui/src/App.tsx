import { useEffect, useState } from 'react';
import { HashRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { api } from './api/client';
import type { WindowRuntimeStatus } from './api/types';
import { Button } from './components/Button';
import { IconInfo } from './components/Icons';
import { TopBar } from './components/TopBar';
import { ToastProvider } from './components/Toast';
import { toFriendlyError } from './hooks/errors';
import { useEnvReport } from './hooks/useEnvReport';
import { useAppearance } from './hooks/useAppearance';
import { PrinterProvider } from './hooks/usePrinters';
import { SocialFooter } from './components/SocialFooter';
import { attachLiquidSheen } from './lib/liquidSheen';
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

/**
 * 界面材质（实面 / 液态玻璃）的后端同步：与 ThemeBridge 同一套做法。
 * 本地 localStorage 先上屏，这里只负责把「权威那一份」盖回来 ——
 * 换机器、重装、在设置页改了又重开，都不会回到默认样子。
 */
function AppearanceBridge() {
  const { preview } = useAppearance();
  // 玻璃上的跟随指针高光：全程只挂一个被动监听，非玻璃模式下第一行就返回
  useEffect(() => attachLiquidSheen(), []);
  useEffect(() => {
    let alive = true;
    void api
      .getSettings()
      .then((settings) => {
        if (!alive) return;
        if (settings.appearance || typeof settings.glass_transparency === 'number') {
          preview({
            appearance: settings.appearance === 'glass' ? 'glass' : 'solid',
            transparency: settings.glass_transparency,
          });
        }
      })
      .catch(() => {
        // 后端不可用时沿用本地偏好
      });
    return () => {
      alive = false;
    };
  }, [preview]);
  return null;
}

/**
 * 浏览器回退横幅。WebView2 缺失时后端会改用系统浏览器打开界面，并在地址上带
 * `?browser_fallback=1`（见 `src/aiforge/server.py`）。用户手册 §2.2 承诺
 * 「软件会在页面顶部说明原因，并给出一键安装入口」—— 这条就是那个落点：
 * 没它时用户只会觉得「这软件怎么开了个网页」，而那两个窗口组件接口也变成没人调的接口。
 *
 * 只在地址带了标记时才拉状态：绝大多数人（原生窗口）一行不渲染、一次请求也不多发。
 */
function BrowserFallbackNotice() {
  const [status, setStatus] = useState<WindowRuntimeStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [closed, setClosed] = useState(false);
  const fallback = new URLSearchParams(window.location.search).get('browser_fallback') === '1';

  useEffect(() => {
    if (!fallback) return;
    let alive = true;
    void api
      .windowRuntime()
      .then((info) => {
        if (alive) setStatus(info);
      })
      .catch(() => {
        // 拉不到状态就当没这回事：这条横幅是补充说明，不该自己变成一个新错误
      });
    return () => {
      alive = false;
    };
  }, [fallback]);

  if (!fallback || closed || !status || status.installed) return null;

  const install = async () => {
    setBusy(true);
    try {
      const result = await api.installWindowRuntime();
      setMessage(result.message);
    } catch (err) {
      const friendly = toFriendlyError(err);
      setMessage(friendly.hint ? `${friendly.message}${friendly.hint}` : friendly.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="notice" role="status" style={{ margin: 'var(--sp-3) var(--sp-6) 0' }}>
      <IconInfo size={16} />
      <span>
        <strong>这个页面开在浏览器里。</strong>
        {status.detail || '你的系统缺少原生窗口组件（WebView2 运行时）。'}
        {message ? ` ${message}` : ''}
        装好之后重开软件就是独立窗口，功能完全一样。
      </span>
      <Button size="sm" variant="primary" loading={busy} onClick={() => void install()}>
        一键安装
      </Button>
      <Button size="sm" variant="ghost" onClick={() => setClosed(true)}>
        知道了
      </Button>
    </div>
  );
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
      <BrowserFallbackNotice />
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
      {/* 作者账号入口：放在外壳里，每一页底部都可见 */}
      <SocialFooter />
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
          <AppearanceBridge />
          <ScrollReset />
          <Shell />
        </PrinterProvider>
      </ToastProvider>
    </HashRouter>
  );
}
