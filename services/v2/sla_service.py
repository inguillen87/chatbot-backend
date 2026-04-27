from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
import copy

from flask import current_app

from extensions import db
from models import TenantProfile, TenantTicket
from services.v2.ticket_event_service import record_ticket_event

_DEFAULT_POLICIES = {
    "low": {"first_response_minutes": 240, "resolution_minutes": 2880, "next_update_minutes": 1440},
    "medium": {"first_response_minutes": 120, "resolution_minutes": 1440, "next_update_minutes": 720},
    "high": {"first_response_minutes": 60, "resolution_minutes": 720, "next_update_minutes": 240},
    "urgent": {"first_response_minutes": 15, "resolution_minutes": 240, "next_update_minutes": 60},
}

_PAUSED_STATUSES = {"waiting_customer", "esperando_cliente"}
_CLOSED_STATUSES = {"resuelto", "cerrado", "closed", "resolved"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_tenant_cfg(tenant: TenantProfile) -> dict[str, Any]:
    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    tenant.configuracion = cfg
    return cfg


def get_policies_for_tenant(tenant: TenantProfile) -> dict[str, Any]:
    cfg = _ensure_tenant_cfg(tenant)
    stored = cfg.get("v2_sla_policies")
    if not isinstance(stored, dict):
        return dict(_DEFAULT_POLICIES)
    merged = dict(_DEFAULT_POLICIES)
    for key, value in stored.items():
        if key in merged and isinstance(value, dict):
            merged[key].update({k: int(v) for k, v in value.items() if str(v).isdigit()})
    return merged


def save_policies_for_tenant(tenant: TenantProfile, policies: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, dict[str, int]] = {}
    for priority, base in _DEFAULT_POLICIES.items():
        incoming = policies.get(priority) if isinstance(policies, dict) else None
        normalized[priority] = dict(base)
        if isinstance(incoming, dict):
            for key in ("first_response_minutes", "resolution_minutes", "next_update_minutes"):
                try:
                    normalized[priority][key] = int(incoming.get(key, normalized[priority][key]))
                except (TypeError, ValueError):
                    pass

    cfg = _ensure_tenant_cfg(tenant)
    cfg["v2_sla_policies"] = normalized
    tenant.configuracion = cfg
    db.session.add(tenant)
    return normalized


def apply_sla_to_ticket(ticket: TenantTicket, policies: dict[str, Any], *, force_recalculate: bool = False) -> dict[str, Any]:
    extra = copy.deepcopy(ticket.datos_extra) if isinstance(ticket.datos_extra, dict) else {}
    priority = str(extra.get("priority") or "medium").lower()
    policy = policies.get(priority) or policies.get("medium") or _DEFAULT_POLICIES["medium"]

    created_at = ticket.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    status = str(ticket.estado or "").lower()
    paused = status in _PAUSED_STATUSES

    existing_sla = extra.get("sla") if isinstance(extra.get("sla"), dict) else {}

    if paused:
        due_fields = {
            "first_response_due_at": None,
            "resolution_due_at": None,
            "next_update_due_at": None,
            "paused": True,
        }
    else:
        due_fields = dict(existing_sla)
        if force_recalculate or not due_fields.get("first_response_due_at"):
            due_fields["first_response_due_at"] = (created_at + timedelta(minutes=policy["first_response_minutes"])).isoformat()
        if force_recalculate or not due_fields.get("resolution_due_at"):
            due_fields["resolution_due_at"] = (created_at + timedelta(minutes=policy["resolution_minutes"])).isoformat()
        if force_recalculate or not due_fields.get("next_update_due_at"):
            due_fields["next_update_due_at"] = (created_at + timedelta(minutes=policy["next_update_minutes"])).isoformat()
        due_fields["paused"] = False

    extra["sla"] = due_fields
    ticket.datos_extra = extra
    return due_fields


def is_ticket_overdue(ticket: TenantTicket) -> bool:
    status = str(ticket.estado or "").lower()
    if status in _CLOSED_STATUSES or status in _PAUSED_STATUSES:
        return False

    extra = ticket.datos_extra if isinstance(ticket.datos_extra, dict) else {}
    sla = extra.get("sla") if isinstance(extra.get("sla"), dict) else {}

    due_text = sla.get("resolution_due_at") or sla.get("next_update_due_at")
    if not due_text:
        return False

    try:
        due = datetime.fromisoformat(str(due_text))
    except ValueError:
        return False
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)

    return _utc_now() > due


def detect_sla_breaches_for_tenant(tenant: TenantProfile, actor_user=None) -> list[dict[str, Any]]:
    policies = get_policies_for_tenant(tenant)
    breaches: list[dict[str, Any]] = []

    tickets = TenantTicket.query.filter_by(tenant_id=tenant.id).all()
    for ticket in tickets:
        apply_sla_to_ticket(ticket, policies)
        if not is_ticket_overdue(ticket):
            continue

        extra = ticket.datos_extra if isinstance(ticket.datos_extra, dict) else {}
        breach_key = "sla_breach_event_emitted_at"
        already_emitted = bool(extra.get(breach_key))

        breach_payload = {
            "ticket_id": ticket.id,
            "estado": ticket.estado,
            "categoria": ticket.categoria,
        }
        breaches.append(breach_payload)

        if not already_emitted:
            record_ticket_event(
                tenant_id=tenant.id,
                event_type="sla.breach_detected",
                ticket=ticket,
                actor_user=actor_user,
                details=breach_payload,
            )
            extra[breach_key] = _utc_now().isoformat()
            ticket.datos_extra = extra
            db.session.add(ticket)

    if breaches:
        current_app.logger.info("[v2.sla] Detected %s SLA breaches for tenant=%s", len(breaches), tenant.id)

    return breaches
