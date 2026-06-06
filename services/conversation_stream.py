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


def _coerce_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "si"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return bool(value)


def _normalize_actor_type(*, source: str, payload: dict[str, Any]) -> str:
    actor = str(payload.get("actor_type") or payload.get("author_type") or payload.get("autor") or "").strip().lower()
    if actor in {"admin", "agent", "municipio", "pyme", "chatbot", "sistema"}:
        return "agent" if actor != "sistema" else "system"
    if actor in {"vecino", "cliente", "citizen", "user"}:
        return "citizen"
    if source == "timeline" and payload.get("tipo") in {"ticket_creado", "estado"}:
        return "system"
    if _coerce_bool(payload.get("es_admin")) is True:
        return "agent"
    return "citizen" if source == "chat_history" else "system"


def _normalize_preview_text(*, source: str, payload: dict[str, Any]) -> str | None:
    if payload.get("texto"):
        return str(payload.get("texto")).strip()
    if payload.get("comentario"):
        return str(payload.get("comentario")).strip()
    if source == "timeline" and payload.get("tipo") == "ticket_creado":
        return "Ticket creado"
    if source == "timeline" and payload.get("tipo") == "estado" and payload.get("estado"):
        return f"Estado actualizado a {payload.get('estado')}"
    return None


def _normalize_status_badge(*, source: str, payload: dict[str, Any]) -> tuple[str | None, str | None]:
    if source == "timeline" and payload.get("tipo") == "estado":
        status = str(payload.get("estado") or "").strip().lower() or None
        return status, "status_change" if status else None
    if source == "timeline" and payload.get("tipo") == "ticket_creado":
        return "nuevo", "created"
    return None, None


def build_unified_conversation_stream(
    *,
    timeline: list[dict[str, Any]] | None,
    historial_chat: list[dict[str, Any]] | None,
    latest_comment_id: int | None = None,
    last_read_comment_id: int | None = None,
) -> list[dict[str, Any]]:
    unified_items: list[dict[str, Any]] = []
    dedupe_index: dict[tuple[Any, ...], int] = {}

    def _message_dedupe_keys(
        *,
        source: str,
        payload: dict[str, Any],
        comment_id: int | None,
        actor_type: str,
        preview_text: str | None,
        timestamp: Any,
    ) -> list[tuple[Any, ...]]:
        if source not in {"timeline", "chat_history"}:
            return []
        is_message = source == "chat_history" or payload.get("tipo") == "comentario"
        if not is_message:
            return []

        keys: list[tuple[Any, ...]] = []
        if comment_id is not None:
            keys.append(("comment_id", comment_id))

        normalized_text = (preview_text or "").strip().lower()
        if normalized_text and timestamp:
            keys.append(("fingerprint", str(timestamp), actor_type, normalized_text))
        return keys

    def _append_items(source: str, items: list[dict[str, Any]] | None) -> None:
        for idx, item in enumerate(items or []):
            if not isinstance(item, dict):
                continue
            status, badge = _normalize_status_badge(source=source, payload=item)
            actor_type = _normalize_actor_type(source=source, payload=item)
            preview_text = _normalize_preview_text(source=source, payload=item)
            item_id = item.get("id") or item.get("comment_id") or item.get("comentario_id")
            timestamp = item.get("fecha") or item.get("timestamp")
            normalized_id = (
                f"{source}:{item_id}"
                if item_id not in (None, "")
                else f"{source}:{item.get('tipo') or item.get('autor') or 'entry'}:{idx}:{timestamp or 'na'}"
            )
            comment_id = item.get("comment_id") or item.get("comentario_id") or item.get("id")
            if source == "timeline" and item.get("tipo") != "comentario":
                comment_id = None
            read_id = int(last_read_comment_id or 0)
            normalized_comment_id = int(comment_id) if str(comment_id).isdigit() else None
            is_unread = bool(normalized_comment_id and read_id and normalized_comment_id > read_id)

            normalized_item = {
                "id": normalized_id,
                "source": source,
                "stream_type": "message" if source == "chat_history" else (item.get("tipo") or "timeline_event"),
                "timestamp": timestamp,
                "actor_type": actor_type,
                "actor_name": item.get("autor_nombre"),
                "preview_text": preview_text,
                "status": status,
                "badge": badge,
                "comment_id": normalized_comment_id,
                "is_read": False if is_unread else None,
                "is_unread": is_unread,
                "payload": item,
            }
            dedupe_keys = _message_dedupe_keys(
                source=source,
                payload=item,
                comment_id=normalized_comment_id,
                actor_type=actor_type,
                preview_text=preview_text,
                timestamp=timestamp,
            )
            existing_index = next(
                (dedupe_index[key] for key in dedupe_keys if key in dedupe_index),
                None,
            )
            if existing_index is not None:
                existing = unified_items[existing_index]
                if existing.get("source") == "timeline" and source == "chat_history":
                    unified_items[existing_index] = normalized_item
                    for key in dedupe_keys:
                        dedupe_index[key] = existing_index
                continue

            unified_items.append(normalized_item)
            item_index = len(unified_items) - 1
            for key in dedupe_keys:
                dedupe_index[key] = item_index

    _append_items("timeline", timeline)
    _append_items("chat_history", historial_chat)
    unified_items.sort(key=lambda item: (item.get("timestamp") or "", item.get("id") or ""))
    return unified_items
