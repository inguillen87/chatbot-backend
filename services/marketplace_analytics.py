from __future__ import annotations

import logging
from typing import Any, Mapping

from flask import has_request_context, request

from services.analytics.ingestor import analytics_ingestor

logger = logging.getLogger(__name__)

MARKETPLACE_ANALYTICS_CONTRACT_VERSION = "marketplace.commerce_loop.analytics.v1"

PII_METADATA_KEYS = {
    "apellido",
    "contact",
    "contact_email",
    "contact_name",
    "contact_phone",
    "contacto",
    "dni",
    "documento",
    "email",
    "mail",
    "name",
    "nombre",
    "phone",
    "telefono",
    "text_preview",
    "texto_original",
}


def _coerce_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def marketplace_analytics_tenant_id(tenant: object) -> int | None:
    return _coerce_int(getattr(tenant, "id", None))


def _identity_from_request() -> dict[str, str | None]:
    if not has_request_context():
        return {"anon_id": None, "session_id": None, "channel": None}
    payload = request.get_json(silent=True) if request.is_json else None
    if not isinstance(payload, Mapping):
        payload = {}
    return {
        "anon_id": (
            request.headers.get("X-Anon-Id")
            or request.headers.get("Anon-Id")
            or request.args.get("anon_id")
            or str(payload.get("anon_id") or payload.get("anonId") or "")[:120]
            or None
        ),
        "session_id": (
            request.headers.get("X-Chat-Session-Id")
            or request.args.get("chat_session_id")
            or request.args.get("session_id")
            or str(payload.get("chat_session_id") or payload.get("session_id") or "")[:120]
            or None
        ),
        "channel": (
            request.headers.get("X-Sales-Channel")
            or request.headers.get("X-Channel")
            or request.args.get("channel")
            or request.args.get("origen")
            or str(payload.get("channel") or payload.get("origen") or "")[:80]
            or None
        ),
    }


def _sanitize_metadata(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if key.lower() in PII_METADATA_KEYS:
                continue
            sanitized[key] = _sanitize_metadata(raw_value, depth=depth + 1)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_metadata(item, depth=depth + 1) for item in value[:30]]
    if isinstance(value, str):
        return value[:300]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:160]


def track_marketplace_event(
    tenant: object,
    event_name: str,
    payload: Mapping[str, Any] | None = None,
    *,
    channel: str | None = None,
    session_id: str | None = None,
    anon_id: str | None = None,
    entity_ref: str | None = None,
    user_id: int | None = None,
) -> None:
    tenant_id = marketplace_analytics_tenant_id(tenant)
    if not tenant_id:
        return

    identity = _identity_from_request()
    sanitized_payload = _sanitize_metadata(payload or {}) or {}
    if isinstance(sanitized_payload, dict) and "contract_version" in sanitized_payload:
        sanitized_payload["source_contract_version"] = sanitized_payload.pop("contract_version")
    metadata = {
        **sanitized_payload,
        "contract_version": MARKETPLACE_ANALYTICS_CONTRACT_VERSION,
        "tenant_slug": getattr(tenant, "slug", None),
        "tenant_type": getattr(tenant, "tipo", None) or getattr(tenant, "vertical", None),
    }
    try:
        analytics_ingestor.track(
            tenant_id=tenant_id,
            event_name=event_name,
            payload=metadata,
            user_id=user_id,
            anon_id=anon_id or identity.get("anon_id"),
            channel=channel or identity.get("channel") or "web",
            session_id=session_id or identity.get("session_id"),
            entity_ref=entity_ref,
            tenant_type=getattr(tenant, "tipo", None) or getattr(tenant, "vertical", None),
        )
    except Exception:
        logger.exception("[marketplace_analytics] failed to track %s", event_name)
