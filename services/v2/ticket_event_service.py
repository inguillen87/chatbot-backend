from __future__ import annotations

from typing import Any

from flask import has_request_context, request

from extensions import db
from models import AuditEvent, TenantTicket, User


def record_ticket_event(
    *,
    tenant_id: int,
    event_type: str,
    ticket: TenantTicket,
    actor_user: User | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    payload = dict(details or {})
    payload.setdefault("ticket_id", ticket.id)
    payload.setdefault("tenant_ticket", True)

    ip_address = None
    if has_request_context():
        ip_address = request.headers.get("X-Forwarded-For") or request.remote_addr

    event = AuditEvent(
        tenant_id=tenant_id,
        actor_user_id=getattr(actor_user, "id", None),
        event_type=event_type,
        resource_type="tenant_ticket",
        resource_id=str(ticket.id),
        details=payload,
        ip_address=ip_address,
    )
    db.session.add(event)
    return event


def list_ticket_events(*, tenant_id: int, ticket_id: int) -> list[AuditEvent]:
    return (
        AuditEvent.query.filter_by(
            tenant_id=tenant_id,
            resource_type="tenant_ticket",
            resource_id=str(ticket_id),
        )
        .order_by(AuditEvent.created_at.asc())
        .all()
    )
