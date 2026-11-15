"""Signed WhatsApp account and template notifications that carry no chat.

Meta always delivers these webhook fields to the application callback; a
phone-number or WABA callback override does not move them (Meta, "Webhook
overrides", section "Supported webhook fields", read 2026-11-13). The message
pipeline rejects every field other than ``messages``, so these legitimate
deliveries were answered with an error.

This module only recognises such a delivery so the callback can acknowledge
it. It resolves no tenant, reads no payload value, persists nothing and never
replies to anyone. The decision is made strictly after the signature has been
verified with the application secret.
"""

from __future__ import annotations

import json
from typing import Any

from services.meta_whatsapp_webhook import MAX_EVENTS, MetaContractError, verify_signature

# Closed list, exactly the fields Meta documents as not supporting overrides.
TEMPLATE_FIELDS = frozenset({
    "message_template_status_update",
    "message_template_quality_update",
    "message_template_components_update",
    "template_category_update",
})
ACCOUNT_FIELDS = frozenset({"account_update", "account_review_update", "account_alerts"})
ACKNOWLEDGED_FIELDS = TEMPLATE_FIELDS | ACCOUNT_FIELDS
ACKNOWLEDGED_RESULT = "acknowledged_unprocessed"


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _bounded_list(value: Any) -> list[Any]:
    if not isinstance(value, list) or not value or len(value) > MAX_EVENTS:
        raise ValueError
    return value


def acknowledged_account_events(*, raw_body: bytes, signature: str, app_secret: str) -> dict[str, int] | None:
    """Return per-field counts when the whole signed delivery is account/template.

    ``None`` means "not mine": the caller continues with the message pipeline
    exactly as before, including its rejections. That covers a malformed
    document, another ``object``, any ``messages`` change, any field outside
    the closed list and a mixed delivery. A mixed delivery is deliberately not
    split: acknowledging part of a batch would change delivery semantics.

    Raises ``MetaContractError("meta_webhook_signature_invalid")`` before
    anything is parsed when the signature does not match.
    """

    verify_signature(raw_body=raw_body, signature=signature, app_secret=app_secret)
    try:
        document = json.loads(
            raw_body.decode("utf-8"),
            object_pairs_hook=_unique_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        if not isinstance(document, dict) or document.get("object") != "whatsapp_business_account":
            return None
        counts: dict[str, int] = {}
        total = 0
        for entry in _bounded_list(document.get("entry")):
            if not isinstance(entry, dict):
                return None
            for change in _bounded_list(entry.get("changes")):
                field = change.get("field") if isinstance(change, dict) else None
                if not isinstance(field, str) or field not in ACKNOWLEDGED_FIELDS:
                    return None
                total += 1
                if total > MAX_EVENTS:
                    return None
                counts[field] = counts.get(field, 0) + 1
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return counts


__all__ = [
    "ACCOUNT_FIELDS", "ACKNOWLEDGED_FIELDS", "ACKNOWLEDGED_RESULT", "TEMPLATE_FIELDS",
    "MetaContractError", "acknowledged_account_events",
]
