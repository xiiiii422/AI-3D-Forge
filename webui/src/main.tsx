import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import './styles/tokens.css';
import './styles/app.css';

const host = document.getElementById('root');
if (!host) throw new Error('未找到 #root 挂载点');

// 首屏先把主题定下来，避免浅→深闪一下
try {
  const stored = window.localStorage.getItem('aiforge.theme');
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  const theme = stored === 'dark' || stored === 'light' ? stored : prefersDark ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', theme);
  document.documentElement.style.colorScheme = theme;
} catch {
  document.documentElement.setAttribute('data-theme', 'light');
}

createRoot(host).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
