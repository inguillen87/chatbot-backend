"""Centralised logging helpers with truncation utilities."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

# Maximum length for any single log line.
MAX_LOG_LINE_LENGTH = 1000


class TruncatingFormatter(logging.Formatter):
    """Formatter that truncates overly long log messages."""

    def __init__(self, fmt: str | None = None, datefmt: str | None = None, *, max_length: int = MAX_LOG_LINE_LENGTH):
        super().__init__(fmt, datefmt)
        self.max_length = max_length

    def format(self, record: logging.LogRecord) -> str:  # pragma: no cover - simple wrapper
        message = record.getMessage()
        if isinstance(message, str) and len(message) > self.max_length:
            record.msg = f"{message[: self.max_length]}... [truncated {len(message) - self.max_length} chars]"
            record.args = ()
        return super().format(record)


def setup_logging(max_length: int = MAX_LOG_LINE_LENGTH) -> None:
    """Configure root logger with truncation-friendly formatter."""

    root_logger = logging.getLogger()
    if root_logger.handlers:
        return

    handler = logging.StreamHandler(sys.stderr)
    formatter = TruncatingFormatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s", "%Y-%m-%d %H:%M:%S", max_length=max_length
    )
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)


def get_logger(name: str) -> logging.Logger:
    """Return a module-specific logger configured for truncation."""

    setup_logging()
    return logging.getLogger(name)


def log_text_block(
    logger: logging.Logger,
    title: str,
    data: Any,
    *,
    level: int = logging.INFO,
    chunk_size: int = MAX_LOG_LINE_LENGTH,
) -> None:
    """Log potentially long ``data`` in structured chunks.

    Large strings are split into multiple log records to avoid exceeding
    Render's line length limits. Non-string data is JSON-encoded when possible.
    """

    if isinstance(data, str):
        text = data
    else:
        try:
            text = json.dumps(data, ensure_ascii=False)
        except Exception:
            text = str(data)

    if len(text) <= chunk_size:
        logger.log(level, f"{title}: {text}")
        return

    logger.log(level, f"{title} (length {len(text)}):")
    for start in range(0, len(text), chunk_size):
        logger.log(level, text[start : start + chunk_size])


__all__ = [
    "get_logger",
    "log_text_block",
    "setup_logging",
    "TruncatingFormatter",
    "MAX_LOG_LINE_LENGTH",
]

