from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


CRM_SENSITIVE_CONTENT_PLACEHOLDER = "Contenido sensible oculto por seguridad."


# These patterns intentionally require either explicit secret vocabulary or a
# well-known credential shape.  Ordinary municipal references such as ticket
# numbers, addresses and postal codes must remain readable in the CRM.
_SENSITIVE_PATTERNS = (
    re.compile(
        r"\b\d{4,10}\b(?=.{0,80}\b(?:es|sera|ser[aá])\s+tu\s+c[oó]digo\b)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\btu\s+c[oó]digo(?:\s+de\s+(?:acceso|seguridad|verificaci[oó]n))?"
        r"\s*(?::|=|es)\s*\d{4,10}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:verification\s+code|c[oó]digo\s+de\s+"
        r"(?:instagram|whatsapp|facebook|meta|google|microsoft|acceso|seguridad|verificaci[oó]n))"
        r"\s*(?::|=|es)\s*\d{4,10}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:otp|pin)\b(?:\s+(?:de\s+)?(?:acceso|seguridad|verificaci[oó]n))?"
        r"\s*(?::|=|#|-|es)?\s*\d{4,10}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:one[\s_-]*time[\s_-]*(?:password|passcode|code)|passcode|password|"
        r"contrase(?:ñ|n)a|c[oó]digo(?:\s+de)?\s+(?:verificaci[oó]n|seguridad|acceso|"
        r"confirmaci[oó]n|autenticaci[oó]n))\b\s*(?::|=|#|-|es)\s*"
        r"[^\s,;]{4,256}",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:api[\s_-]*key|clave\s+(?:de\s+)?api|(?:access|refresh|auth|authorization|api)"
        r"[\s_-]*token|token\s+(?:de\s+)?acceso)\b"
        r"\s*(?::|=|#|-|es)?\s*[A-Za-z0-9][A-Za-z0-9._~+/=-]{8,512}",
        re.IGNORECASE,
    ),
    re.compile(
        r"\btoken\b\s*(?::|=|es)\s*[A-Za-z0-9][A-Za-z0-9._~+/=-]{8,512}",
        re.IGNORECASE,
    ),
    re.compile(r"\bBearer\s+[A-Za-z0-9][A-Za-z0-9._~+/=-]{8,512}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,512}\b", re.IGNORECASE),
    re.compile(
        r"\b\d{4,10}\b(?=.{0,100}\bno\s+(?:lo\s+)?compartas\b)",
        re.IGNORECASE | re.DOTALL,
    ),
)


def contains_crm_sensitive_content(value: Any) -> bool:
    """Return True only for high-confidence credentials or verification codes."""

    if not isinstance(value, str) or not value.strip():
        return False
    return any(pattern.search(value) for pattern in _SENSITIVE_PATTERNS)


def redact_crm_sensitive_text(value: Any) -> Any:
    """Redact a sensitive display value without changing its stored source."""

    if not isinstance(value, str):
        return value
    if contains_crm_sensitive_content(value):
        return CRM_SENSITIVE_CONTENT_PLACEHOLDER
    return value


def redact_crm_sensitive_value(value: Any) -> Any:
    """Build a sanitized JSON-safe copy of nested CRM display data."""

    if isinstance(value, str):
        return redact_crm_sensitive_text(value)
    if isinstance(value, Mapping):
        return {
            key: redact_crm_sensitive_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_crm_sensitive_value(item) for item in value]
    return value
