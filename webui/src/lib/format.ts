/**
 * 界面通用格式化：字节与「几月几日 时:分」。
 *
 * 为什么单独立一个文件：这两个函数原先各写在 `ChatPanel.tsx` 里，
 * 回收站（设置页）也要显示「多大、什么时候删的」——同一段逻辑抄第二遍就会
 * 出现「一处改 KB/MB 阈值、另一处没改」的漂移，所以提到这里共用。
 */

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function formatDateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getMonth() + 1}月${d.getDate()}日 ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
