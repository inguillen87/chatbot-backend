"""Carry provider-event identity through chat orchestration contexts."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any


SOURCE_EVENT_CONTEXT_FIELDS = (
    "source_event_id",
    "durable_turn_id",
    "idempotency_key",
)


def normalize_source_event_context(source: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return bounded, JSON-serializable source-event identifiers."""

    if not isinstance(source, Mapping):
        return {}

    normalized: dict[str, Any] = {}

    source_event_id = str(source.get("source_event_id") or "").strip()
    if source_event_id and len(source_event_id) <= 191:
        normalized["source_event_id"] = source_event_id

    raw_turn_id = source.get("durable_turn_id")
    if isinstance(raw_turn_id, int) and not isinstance(raw_turn_id, bool) and raw_turn_id > 0:
        normalized["durable_turn_id"] = raw_turn_id
    elif isinstance(raw_turn_id, str):
        durable_turn_id = raw_turn_id.strip()
        if durable_turn_id and len(durable_turn_id) <= 64:
            normalized["durable_turn_id"] = (
                int(durable_turn_id) if durable_turn_id.isdigit() else durable_turn_id
            )

    idempotency_key = str(source.get("idempotency_key") or "").strip()
    if idempotency_key and len(idempotency_key) <= 128:
        normalized["idempotency_key"] = idempotency_key

    return normalized


def bind_source_event_context(
    context_data: MutableMapping[str, Any] | None,
    context_key: str,
    source: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], bool]:
    """Bind current-turn identity to an entity context and clear stale values."""

    normalized = normalize_source_event_context(source)
    if not isinstance(context_data, MutableMapping):
        return normalized, False

    entity_context = context_data.setdefault(context_key, {})
    if not isinstance(entity_context, MutableMapping):
        entity_context = {}
        context_data[context_key] = entity_context

    before = {field: entity_context.get(field) for field in SOURCE_EVENT_CONTEXT_FIELDS}
    for field in SOURCE_EVENT_CONTEXT_FIELDS:
        if field in normalized:
            entity_context[field] = normalized[field]
        else:
            entity_context.pop(field, None)
    after = {field: entity_context.get(field) for field in SOURCE_EVENT_CONTEXT_FIELDS}
    return normalized, before != after

