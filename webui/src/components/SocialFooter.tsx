import type { MouseEvent } from 'react';
import { api } from '../api/client';

/**
 * 底部的作者账号入口。
 *
 * 为什么用 api.openExternal 而不是 target="_blank"：
 * 原生窗口（WebView2）会拦截新窗口请求，点了没反应；本地服务用系统默认浏览器
 * 打开才可靠。浏览器模式下 openExternal 也通（后端 webbrowser.open），
 * 真失败时再退到 window.open。
 */
const CHANNELS = [
  {
    label: '抖音',
    handle: '@不会拍的锡',
    url: 'https://www.douyin.com/user/MS4wLjABAAAAZKhfrEjQLxhJP6fbu4wBGSaxhi5LWmLzyCWNwMetgiQ?from_tab_name=main',
  },
  {
    label: 'B站',
    handle: '@xiiiiii422',
    url: 'https://space.bilibili.com/437196663?spm_id_from=333.337.0.0',
  },
];

export function SocialFooter() {
  const openInBrowser = (event: MouseEvent<HTMLAnchorElement>, url: string) => {
    // 保留 href：键盘/右键「复制链接」照常可用，这里只是换一种打开方式
    event.preventDefault();
    api.openExternal(url).catch(() => {
      window.open(url, '_blank', 'noopener,noreferrer');
    });
  };

  return (
    <footer className="app-footer" aria-label="作者社交账号">
      <span className="app-footer-title">开发者：</span>
      {CHANNELS.map((channel) => (
        <a
          key={channel.label}
          className="app-footer-link"
          href={channel.url}
          title={`在浏览器打开 ${channel.label} ${channel.handle}`}
          onClick={(event) => openInBrowser(event, channel.url)}
        >
          {channel.label}：{channel.handle}
        </a>
      ))}
    </footer>
  );
}
