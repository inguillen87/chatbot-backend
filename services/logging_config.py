"""Centralised, privacy-safe logging helpers.

Application logs are operational telemetry, not a second copy of citizen or
customer conversations.  The redaction layer below is intentionally applied at
the handler boundary so provider exceptions and legacy log calls receive the
same protection while they are progressively converted to metadata-only logs.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import traceback
from typing import Any

# Maximum length for any single log line.
MAX_LOG_LINE_LENGTH = 1000

REDACTED = "[REDACTED]"

# Keys that commonly carry credentials.  Match both JSON-ish payloads and the
# ``key=value`` representation emitted by provider SDKs.
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:authorization|api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"password|passwd|secret|signature|account[_-]?sid|(?:access|consulta)[_-]?pin|pin)\b"
    r"[\"']?\s*[:=]\s*[\"']?)([^\s,;&}\]\"']+)",
)
_AUTHORIZATION_HEADER_RE = re.compile(
    r"(?i)(\bauthorization\b[\"']?\s*[:=]\s*[\"']?)"
    r"(?:bearer|basic)\s+[^\s,;&}\]\"']+"
)

# Structured customer/citizen values.  This deliberately excludes operational
# identifiers such as ticket_id and tenant_id.
_PRIVATE_STRUCTURED_VALUE_RE = re.compile(
    r"(?i)([\"']?(?:nombre(?:_cliente)?|email(?:_cliente)?|correo|"
    r"tel[eé]fono(?:_cliente)?|celular|dni|documento|direcci[oó]n(?:_cliente)?|"
    r"ubicaci[oó]n(?:_problema)?|pregunta|mensaje|transcripci[oó]n|transcript|"
    r"texto(?:_completo|_transcrito)?|descripcion|(?:access_|consulta_)?pin)"
    r"[\"']?\s*[:=]\s*)"
    r"([\"'])(.*?)(\2)(?=\s*[,}\]])",
)

_EMAIL_RE = re.compile(
    r"(?i)(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])"
)
_WHATSAPP_PHONE_RE = re.compile(r"(?i)\bwhatsapp:\s*\+?[0-9][0-9 .()\-]{6,}[0-9]")
_INTERNATIONAL_PHONE_RE = re.compile(r"(?<![\w])\+[0-9][0-9 .()\-]{7,}[0-9](?![\w])")
_PRIVATE_LABEL_PATTERN = (
    r"dni|documento|email|correo|tel[eé]fono|celular|direcci[oó]n|"
    r"ubicaci[oó]n|nombre(?: completo)?|pregunta|mensaje|transcripci[oó]n|"
    r"consulta|comentario|contenido|respuesta|destino|payload|(?:action_)?data|"
    r"question|message|transcript|address|location|name|phone|document|comment|"
    r"content|response|query|text|(?:access_|consulta_)?pin"
)
_NEXT_FIELD_PATTERN = (
    _PRIVATE_LABEL_PATTERN
    + r"|authorization|api[_-]?key|access[_-]?token|auth[_-]?token|password|"
    r"secret|signature|account[_-]?sid|lat(?:itude)?|lng|lon(?:gitude)?"
)
_LABELED_PRIVATE_RE = re.compile(
    rf"(?i)(\b(?:{_PRIVATE_LABEL_PATTERN})\b\s*[:=]\s*)(.*?)"
    rf"(?=\s+\b(?:{_NEXT_FIELD_PATTERN})\b\s*[:=]|[,;|\n]|$)"
)
_COORDINATES_RE = re.compile(
    r"(?i)(\b(?:lat(?:itude)?|lng|lon(?:gitude)?)\s*[:=]\s*)"
    r"-?\d{1,3}(?:\.\d+)?"
)


def sanitize_log_message(value: Any) -> str:
    """Return a deterministic log representation with common PII/secrets removed.

    This is a safety net.  Call sites should still prefer counts, field names,
    hashes and opaque internal IDs over raw user content.
    """

    text = str(value)
    text = _AUTHORIZATION_HEADER_RE.sub(
        lambda match: f"{match.group(1)}{REDACTED}",
        text,
    )
    text = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{REDACTED}", text)
    text = _PRIVATE_STRUCTURED_VALUE_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}{match.group(4)}",
        text,
    )
    text = _LABELED_PRIVATE_RE.sub(
        lambda match: f"{match.group(1)}{REDACTED}",
        text,
    )
    text = _EMAIL_RE.sub(REDACTED, text)
    text = _WHATSAPP_PHONE_RE.sub("whatsapp:[REDACTED]", text)
    text = _INTERNATIONAL_PHONE_RE.sub(REDACTED, text)
    text = _COORDINATES_RE.sub(lambda match: f"{match.group(1)}{REDACTED}", text)
    return text


class PrivacyRedactionFilter(logging.Filter):
    """Redact formatted arguments and exception text before any handler emits."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = sanitize_log_message(record.getMessage())
            record.args = ()
            if record.exc_info:
                rendered_exception = "".join(traceback.format_exception(*record.exc_info))
                record.exc_text = sanitize_log_message(rendered_exception)
        except Exception:
            # Logging must never break the application path it is observing.
            record.msg = "Log record suppressed after privacy redaction failure"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


class TruncatingFormatter(logging.Formatter):
    """Formatter that redacts and truncates overly long log messages."""

    def __init__(self, fmt: str | None = None, datefmt: str | None = None, *, max_length: int = MAX_LOG_LINE_LENGTH):
        super().__init__(fmt, datefmt)
        self.max_length = max_length

    def format(self, record: logging.LogRecord) -> str:
        rendered = sanitize_log_message(super().format(record))
        if len(rendered) > self.max_length:
            return (
                f"{rendered[: self.max_length]}... "
                f"[truncated {len(rendered) - self.max_length} chars]"
            )
        return rendered


def setup_logging(max_length: int = MAX_LOG_LINE_LENGTH) -> None:
    """Configure root logger with truncation-friendly formatter."""

    root_logger = logging.getLogger()
    if root_logger.handlers:
        for existing_handler in root_logger.handlers:
            if not any(
                isinstance(item, PrivacyRedactionFilter)
                for item in existing_handler.filters
            ):
                existing_handler.addFilter(PrivacyRedactionFilter())
        return

    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(PrivacyRedactionFilter())
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
    "PrivacyRedactionFilter",
    "MAX_LOG_LINE_LENGTH",
    "REDACTED",
    "sanitize_log_message",
]
