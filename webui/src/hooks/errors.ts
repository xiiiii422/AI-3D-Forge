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
