"""AI 3D Forge —— 统一错误结构。

所有非 2xx 响应都返回 {"detail", "code", "hint"}，前端据此决定展示哪个修复入口。
绝不把 Python 堆栈或 HTTP 状态码直接丢给用户。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


class AppError(HTTPException):
    def __init__(
        self,
        message: str,
        *,
        code: str = "INTERNAL",
        hint: str | None = None,
        status_code: int = 400,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"detail": message, "code": code, "hint": hint}
        if extra:
            payload.update(extra)
        super().__init__(status_code=status_code, detail=payload)


def not_found(what: str = "请求的内容") -> AppError:
    return AppError(f"没有找到{what}。", code="INTERNAL", hint="它可能已被删除或移动。", status_code=404)


def need_api_key() -> AppError:
    return AppError(
        "还没有配置 DeepSeek API Key。",
        code="NO_API_KEY",
        hint="请在「设置 → AI 服务」中填入 API Key。",
        status_code=409,
    )
