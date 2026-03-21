from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


CONVERSATION_STREAM_SCHEMA_VERSION = "2026-03-21"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_entity_id(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def build_realtime_envelope(*, event_name: str, payload: dict[str, Any], room: str | None = None) -> dict[str, Any]:
    """Return an enterprise-style realtime envelope without mutating the source payload."""

    normalized_payload = deepcopy(payload) if isinstance(payload, dict) else {"payload": payload}
    tenant_type = normalized_payload.get("tenant_type") or normalized_payload.get("tipo")
    tenant_id = _normalize_entity_id(normalized_payload, "tenant_id", "municipio_id", "pyme_id", "rubro_id")
    ticket_id = _normalize_entity_id(normalized_payload, "ticket_id", "id")
    comment_id = _normalize_entity_id(normalized_payload, "comment_id", "comentario_id")
    conversation_id = (
        normalized_payload.get("conversation_id")
        or normalized_payload.get("chat_session_id")
        or normalized_payload.get("session_id")
        or (f"ticket:{tenant_type}:{ticket_id}" if tenant_type and ticket_id else None)
    )

    envelope = {
        "event_name": event_name,
        "schema_version": CONVERSATION_STREAM_SCHEMA_VERSION,
        "occurred_at": _utc_now_iso(),
        "room": room,
        "conversation": {
            "id": conversation_id,
            "channel": normalized_payload.get("channel") or normalized_payload.get("canal"),
            "origin": normalized_payload.get("origin") or normalized_payload.get("origen"),
            "visibility": normalized_payload.get("visibility") or normalized_payload.get("visibilidad") or "public",
        },
        "ticket": {
            "id": ticket_id,
            "tenant_type": tenant_type,
            "tenant_id": tenant_id,
            "status": normalized_payload.get("estado") or normalized_payload.get("status"),
            "priority": normalized_payload.get("priority") or normalized_payload.get("prioridad"),
        },
        "message": {
            "id": comment_id,
            "text": normalized_payload.get("comentario") or normalized_payload.get("message") or normalized_payload.get("mensaje"),
            "author_type": normalized_payload.get("author_type") or normalized_payload.get("actor_type"),
            "read_at": normalized_payload.get("read_at"),
        },
        "payload": normalized_payload,
    }
    return envelope
