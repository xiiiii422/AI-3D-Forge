/**
 * 记住「本会话最后打开的项目」。
 *
 * 为什么需要：顶栏的「工作台」以前永远指向 `/studio`（不带项目 id）。
 * 用户在建模过程中点去设置/诊断再点回工作台，落到的就是**空工作台** ——
 * 项目没了、进度条没了、对话没了，而他以为"界面凭空消失"。
 * 作业其实一直在服务端跑（见 Studio 里重新接管作业的那段注释）。
 *
 * 用 sessionStorage：只在本次会话有效，重开软件仍从项目列表进 ——
 * 不引入需要长期维护的持久化格式。
 */

const KEY = 'aiforge.lastProject';

export function rememberProject(id: string | null | undefined): void {
  if (!id) return;
  try {
    window.sessionStorage.setItem(KEY, id);
  } catch {
    // 隐私模式 / 存储被禁用时忽略：退化成旧的「回空工作台」行为，不影响主流程
  }
}

export function lastProject(): string | null {
  try {
    return window.sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

/** 工作台入口：有记住的项目就回那个项目，否则回空工作台。 */
export function studioPath(): string {
  const id = lastProject();
  return id ? `/studio/${id}` : '/studio';
}
