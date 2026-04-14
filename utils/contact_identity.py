from __future__ import annotations

import re
from typing import Any, Dict, Optional

from flask import Request


_PHONE_DIGITS_RE = re.compile(r"\D+")


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


def _extract_payload(request: Request) -> Dict[str, Any]:
    if request.method in {"POST", "PUT", "PATCH"}:
        payload = request.get_json(silent=True)
        if isinstance(payload, dict):
            return payload
    return {}


def resolve_contact_identity_from_request(request: Request) -> Dict[str, Optional[str]]:
    """Resolve contact identity from headers/body with a stable precedence.

    Precedence for contact key:
      1) conversation_id
      2) phone_e164
      3) anon_id
    """

    payload = _extract_payload(request)

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
