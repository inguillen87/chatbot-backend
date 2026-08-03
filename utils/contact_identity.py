from __future__ import annotations

import re
from typing import Any, Dict, Optional

from flask import Request


_PHONE_DIGITS_RE = re.compile(r"\D+")


# Only these public/conversational surfaces may enrich the request identity
# from JSON.  Protected admin/control-plane routes must be able to apply their
# feature, plan and tenant gates before a domain payload is materialized.
_BODY_IDENTITY_PATH_PREFIXES = (
    "/ask",
    "/api/ask",
    "/widget",
    "/api/widget",
    "/auth/widget",
    "/api/auth/widget",
    "/api/pwa/public",
    "/api/pwa/kits",
    "/pwa/anon-id",
    "/api/pwa/anon-id",
    "/pwa/tenant-info",
    "/api/pwa/tenant-info",
    "/public",
    "/api/public",
    "/api/v2/public",
    "/analytics/event",
    "/api/analytics/event",
)


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_phone_e164(raw_value: Any) -> str:
    text = _clean(raw_value)
    if not text:
        return ""

    # Preserve leading + when present, otherwise normalize as +<digits>.
    has_plus = text.startswith("+")
    digits = _PHONE_DIGITS_RE.sub("", text)
    if len(digits) < 8:
        return ""

    if has_plus:
        return f"+{digits}"

    return f"+{digits}"


def request_path_allows_contact_identity_body(path: Any) -> bool:
    normalized = f"/{_clean(path).lstrip('/')}".rstrip("/") or "/"
    return any(
        normalized == prefix or normalized.startswith(f"{prefix}/")
        for prefix in _BODY_IDENTITY_PATH_PREFIXES
    )


def _extract_payload(
    request: Request,
    *,
    include_body: bool,
    max_body_bytes: int,
) -> Dict[str, Any]:
    if not include_body or request.method not in {"POST", "PUT", "PATCH"}:
        return {}
    if not request.is_json:
        return {}

    try:
        bounded_max = int(max_body_bytes)
    except (TypeError, ValueError):
        return {}
    content_length = request.content_length
    if (
        bounded_max <= 0
        or bounded_max > 1024 * 1024
        or content_length is None
        or content_length < 0
        or content_length > bounded_max
    ):
        return {}

    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        return payload
    return {}


def resolve_contact_identity_from_request(
    request: Request,
    *,
    include_body: bool = True,
    max_body_bytes: int = 64 * 1024,
) -> Dict[str, Optional[str]]:
    """Resolve contact identity from headers/body with a stable precedence.

    Precedence for contact key:
      1) conversation_id
      2) phone_e164
      3) anon_id
    """

    payload = _extract_payload(
        request,
        include_body=include_body,
        max_body_bytes=max_body_bytes,
    )

    conversation_id = (
        _clean(request.headers.get("X-Conversation-Id"))
        or _clean(payload.get("conversation_id"))
        or _clean(payload.get("conversationId"))
    )

    raw_phone = (
        _clean(request.headers.get("X-Contact-Phone"))
        or _clean(request.headers.get("X-User-Phone"))
        or _clean(payload.get("phone_e164"))
        or _clean(payload.get("phone"))
        or _clean(payload.get("telefono"))
    )
    phone_e164 = _normalize_phone_e164(raw_phone)

    anon_id = (
        _clean(request.headers.get("X-Anon-Id"))
        or _clean(request.headers.get("Anon-Id"))
        or _clean(request.cookies.get("chatboc_anon_id"))
        or _clean(request.cookies.get("anon_id"))
        or _clean(payload.get("anon_id"))
        or _clean(payload.get("anonId"))
    )

    explicit_contact_key = (
        _clean(request.headers.get("X-Contact-Key"))
        or _clean(payload.get("contact_key"))
        or _clean(payload.get("contactKey"))
    )

    contact_key = explicit_contact_key or conversation_id or phone_e164 or anon_id or None

    key_source = None
    if explicit_contact_key:
        key_source = "explicit"
    elif conversation_id:
        key_source = "conversation_id"
    elif phone_e164:
        key_source = "phone_e164"
    elif anon_id:
        key_source = "anon_id"

    return {
        "contact_key": contact_key,
        "conversation_id": conversation_id or None,
        "phone_e164": phone_e164 or None,
        "anon_id": anon_id or None,
        "source": key_source,
    }
