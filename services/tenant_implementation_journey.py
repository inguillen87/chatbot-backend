"""Authoritative, secret-free launch journey for an enterprise tenant.

The journey deliberately consumes backend-published readiness channels.  It
does not inspect frontend state and it never turns the presence of a feature
into production readiness.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence


CONTRACT_VERSION = "tenant.implementation_journey.v1"

_READY = "ready"
_ACTION_REQUIRED = "action_required"
_PENDING = "pending"
_BLOCKED = "blocked"
_NOT_PUBLISHED = "not_published"

_STAGES: tuple[dict[str, Any], ...] = (
    {
        "id": "institutional_identity",
        "label": "Identidad institucional",
        "description": "Definir marca, paleta y presencia institucional antes de publicar la experiencia.",
        "source_ids": ("institutional_branding",),
    },
    {
        "id": "channels",
        "label": "Canales de atencion",
        "description": "Conectar WhatsApp, widget, plantillas y derivacion humana con estado verificable.",
        "source_ids": ("whatsapp", "widget", "templates", "live_chat"),
    },
    {
        "id": "knowledge",
        "label": "Conocimiento y contenidos",
        "description": "Cargar contenido institucional y certificar su disponibilidad para respuestas asistidas.",
        "source_ids": ("knowledge_content",),
    },
    {
        "id": "team",
        "label": "Equipo y responsables",
        "description": "Asignar operadores, categorias y responsables para que ningun caso quede sin atender.",
        "source_ids": ("team_routing",),
    },
    {
        "id": "validation_release",
        "label": "Validacion y salida",
        "description": "Completar identidad, accesibilidad, territorio, proteccion publica y participacion antes del canario.",
        "source_ids": (
            "crm",
            "identity_auth",
            "accessibility",
            "territorial_intelligence",
            "public_intake_security",
            "analytics_surveys",
        ),
    },
)


def _clean_action(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    label = str(value.get("label") or "").strip()
    href = str(value.get("href") or "").strip()
    kind = str(value.get("kind") or "link").strip() or "link"
    action_id = str(value.get("id") or "").strip()
    if not action_id or not label or not href or kind != "link":
        return None
    return {
        "id": action_id,
        "label": label,
        "href": href,
        "kind": "link",
        "primary": bool(value.get("primary")),
    }


def _primary_action(sources: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for source in sources:
        if source.get("ready") is True and str(source.get("status") or "").strip().lower() == _READY:
            continue
        actions = source.get("actions")
        if not isinstance(actions, list):
            continue
        for action in actions:
            cleaned = _clean_action(action)
            if cleaned is not None:
                candidates.append(cleaned)
    if not candidates:
        return None
    return deepcopy(next((item for item in candidates if item["primary"]), candidates[0]))


def _stage_status(sources: Sequence[Mapping[str, Any]], *, published: bool) -> str:
    if not published:
        return _NOT_PUBLISHED
    statuses = [str(item.get("status") or "").strip().lower() for item in sources]
    if sources and all(status == _READY and item.get("ready") is True for status, item in zip(statuses, sources)):
        return _READY
    if any(status in {"blocked", "locked"} or item.get("locked") is True for status, item in zip(statuses, sources)):
        return _BLOCKED
    if any(status in {"action_required", "needs_attention"} for status in statuses):
        return _ACTION_REQUIRED
    if any(status == _PENDING for status in statuses):
        return _PENDING
    return _ACTION_REQUIRED


def _stage(definition: Mapping[str, Any], channels_by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    source_ids = [str(item) for item in definition["source_ids"]]
    sources = [channels_by_id[source_id] for source_id in source_ids if source_id in channels_by_id]
    missing = [source_id for source_id in source_ids if source_id not in channels_by_id]
    published = not missing and len(sources) == len(source_ids)
    status = _stage_status(sources, published=published)

    evidence: list[str] = []
    reason_codes: list[str] = []
    for source in sources:
        label = str(source.get("label") or source.get("id") or "Frente").strip()
        source_status = str(source.get("status") or _ACTION_REQUIRED).strip().lower()
        evidence.append(f"{label}: {source_status}")
        raw_reason = str(source.get("reason_code") or "").strip()
        if source_status != _READY and raw_reason and raw_reason not in reason_codes:
            reason_codes.append(raw_reason)
    if missing:
        evidence.extend(f"Fuente no publicada: {source_id}" for source_id in missing)
        reason_codes.append("implementation_source_not_published")

    return {
        "id": str(definition["id"]),
        "label": str(definition["label"]),
        "description": str(definition["description"]),
        "status": status,
        "ready": status == _READY,
        "published": published,
        "source_ids": source_ids,
        "evidence": evidence,
        "reason_codes": reason_codes,
        "primary_action": _primary_action(sources) if published else None,
    }


def build_implementation_journey(channels: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build the five-stage government launch path from published channels."""

    channels_by_id = {
        str(item.get("id") or "").strip(): item
        for item in channels
        if isinstance(item, Mapping) and str(item.get("id") or "").strip()
    }
    stages = [_stage(definition, channels_by_id) for definition in _STAGES]
    current = next((item for item in stages if not item["ready"]), None)
    ready_count = sum(1 for item in stages if item["ready"])
    blocked_count = sum(1 for item in stages if item["status"] == _BLOCKED)
    published_count = sum(1 for item in stages if item["published"])

    return {
        "contract_version": CONTRACT_VERSION,
        "stages": stages,
        "summary": {
            "total": len(stages),
            "ready": ready_count,
            "blocked": blocked_count,
            "published": published_count,
            "progress": round((ready_count / max(1, len(stages))) * 100),
            "current_stage_id": current["id"] if current else None,
            "next_action": deepcopy(current.get("primary_action")) if current else None,
        },
    }


__all__ = ["CONTRACT_VERSION", "build_implementation_journey"]
