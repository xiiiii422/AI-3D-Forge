import { ApiError } from '../api/client';

/** 归一化后的可展示错误：文案 + 契约 §8 的机器码 + 可选建议 */
export interface FriendlyError {
  /** 面向用户的中文一句话 */
  message: string;
  /** 契约 §8 的错误码；本地网络异常时为 API_UNREACHABLE */
  code: string;
  /** 后端给的「用户该怎么做」 */
  hint: string | null;
  /** HTTP 状态码，0 表示没连上 */
  status: number;
}

export function toFriendlyError(err: unknown): FriendlyError {
  if (err instanceof ApiError) {
    return {
      message: err.friendly,
      code: err.code,
      hint: err.hint,
      status: err.status,
    };
  }
  if (err instanceof DOMException && err.name === 'AbortError') {
    return { message: '操作已取消。', code: 'CANCELLED', hint: null, status: 0 };
  }
  if (err instanceof Error) {
    return { message: err.message || '出了点问题，请稍后重试。', code: 'INTERNAL', hint: null, status: 0 };
  }
  return { message: '出了点问题，请稍后重试。', code: 'INTERNAL', hint: null, status: 0 };
}

/** 契约 §8 错误码 → 前端应给出的补充说明（不覆盖后端 message，仅在后端没给 hint 时兜底） */
export const ERROR_HINT_FALLBACK: Record<string, string> = {
  NO_API_KEY: '需要先配置 AI 服务的 API Key。',
  API_KEY_INVALID: '请检查 API Key 是否正确，或账户余额是否充足。',
  API_UNREACHABLE: '请检查网络连接后重试。',
  OPENSCAD_MISSING: '需要先安装 3D 引擎（OpenSCAD），可在设置中一键安装。',
  OPENSCAD_FAILED: '模型代码编译失败，可以让 AI 自动修复后重试。',
  SCAD_INVALID: '生成的模型代码未通过安全校验，已自动重试一次。',
  NEED_ADMIN: '该操作需要管理员权限，Windows 会弹出授权提示。',
  DISK_FULL: '磁盘空间不足，请清理后重试。',
  CANCELLED: '',
  INTERNAL: '可以展开技术详情查看原因。',
};

export function hintFor(error: FriendlyError): string | null {
  if (error.hint) return error.hint;
  const fallback = ERROR_HINT_FALLBACK[error.code];
  return fallback ? fallback : null;
}
