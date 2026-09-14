"""AI 3D Forge —— 日志。

日志写给「开发者与技术支持」，因此可以详细；但绝不写入 API Key。
位置：%LOCALAPPDATA%\\AI-3D-Forge\\logs\\，按大小滚动，最多保留 4 个文件。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys

from .paths import paths

_configured = False

# 任何以 sk- 开头、长度超过 12 的字符串都可能是密钥，日志里一律打码
_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9]{6})[A-Za-z0-9]+")


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        if "sk-" in message:
            record.msg = _SECRET_RE.sub(r"\1****", str(record.msg))
            if record.args:
                record.args = tuple(
                    _SECRET_RE.sub(r"\1****", str(arg)) if isinstance(arg, str) else arg for arg in record.args
                )
        return True


def setup(level: int = logging.INFO, console: bool = False) -> str | None:
    """初始化日志。返回日志文件路径（失败时返回 None，绝不因此崩溃）。"""
    global _configured
    if _configured:
        return None

    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redact = _RedactFilter()
    log_path: str | None = None

    try:
        log_dir = paths().log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = str(log_dir / "aiforge.log")
        handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        handler.addFilter(redact)
        root.addHandler(handler)
    except OSError:
        console = True

    if console or os.environ.get("AIFORGE_CONSOLE_LOG") == "1":
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream.addFilter(redact)
        root.addHandler(stream)

    if not root.handlers:
        root.addHandler(logging.NullHandler())

    # 第三方库降噪
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    _configured = True
    logging.getLogger(__name__).info("日志已启动：%s", log_path or "仅控制台")
    return log_path


def log_file() -> str | None:
    try:
        path = paths().log_dir / "aiforge.log"
        return str(path) if path.is_file() else None
    except OSError:
        return None
