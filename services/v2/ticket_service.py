from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
import copy

from flask import g, request
from sqlalchemy import or_
from sqlalchemy.orm.attributes import flag_modified

from extensions import db
from models import TenantTicket, User, TicketComentario
from services.v2.sla_service import apply_sla_to_ticket, get_policies_for_tenant, is_ticket_overdue
from services.v2.ticket_event_service import record_ticket_event

_ALLOWED_STATUSES = {
    "nuevo",
    "open",
    "in_progress",
    "waiting_customer",
    "resuelto",
    "cerrado",
    "closed",
}
_ALLOWED_PRIORITIES = {"low", "medium", "high", "urgent"}
_ALLOWED_CHANNELS = {"web", "widget", "whatsapp", "manual", "chat"}


def _role_of(user: User | None) -> str:
    return str(getattr(user, "rol", "usuario") or "usuario").lower()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _sla_status(ticket: TenantTicket) -> str:
    if is_ticket_overdue(ticket):
        return "breached"

    extra = ticket.datos_extra if isinstance(ticket.datos_extra, dict) else {}
    sla = extra.get("sla") if isinstance(extra.get("sla"), dict) else {}
    due = _parse_iso(sla.get("resolution_due_at") or sla.get("next_update_due_at"))
    if due is None:
        return "ok"
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    if due <= datetime.now(timezone.utc) + timedelta(hours=12):
        return "warning"
    return "ok"


def _assignee_payload(assignee_id: Any) -> dict[str, Any] | None:
    if assignee_id in (None, ""):
        return None
    try:
        assignee_id_int = int(assignee_id)
    except (TypeError, ValueError):
        return {"id": assignee_id, "name": None}

    assignee = User.query.get(assignee_id_int)
    return {
        "id": assignee_id_int,
        "name": getattr(assignee, "name", None) or getattr(assignee, "email", None) if assignee else None,
    }


def _ensure_extra(ticket: TenantTicket) -> dict[str, Any]:
    extra = copy.deepcopy(ticket.datos_extra) if isinstance(ticket.datos_extra, dict) else {}
    ticket.datos_extra = extra
    return extra


def serialize_comment(comment: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": comment.get("id"),
        "body": comment.get("body") or "",
        "visibility": comment.get("visibility") or "public",
        "author_user_id": comment.get("author_user_id"),
        "created_at": comment.get("created_at"),
    }


def serialize_ticket(ticket: TenantTicket, *, viewer: User | None = None) -> dict[str, Any]:
    extra = ticket.datos_extra if isinstance(ticket.datos_extra, dict) else {}
    role = _role_of(viewer)
    comments = extra.get("comments") if isinstance(extra.get("comments"), list) else []
    if role == "usuario":
        comments = [c for c in comments if (c.get("visibility") or "public") == "public"]
    assignee_id = extra.get("assignee_id")
    assignee = _assignee_payload(assignee_id)
    sla_status = _sla_status(ticket)

    return {
        "id": ticket.id,
        "tenant_id": ticket.tenant_id,
        "title": extra.get("title"),
        "description": ticket.descripcion,
        "type": extra.get("type"),
        "category": ticket.categoria,
        "status": ticket.estado,
        "priority": extra.get("priority", "medium"),
        "sla_status": sla_status,
        "sla_state": sla_status,
        "channel": extra.get("channel") or ticket.origen,
        "assignee_id": assignee_id,
        "assignee": assignee,
        "assignee_name": (assignee or {}).get("name"),
        "conversation_id": extra.get("conversation_id"),
        "contact": extra.get("contact") or {},
        "location": {
            "address": extra.get("address"),
            "lat": ticket.latitud,
            "lng": ticket.longitud,
        },
        "sla": extra.get("sla") or {},
        "overdue": is_ticket_overdue(ticket),
        "comments": [serialize_comment(c) for c in comments],
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
        "updated_at": ticket.updated_at.isoformat() if ticket.updated_at else None,
    }


def create_ticket(*, tenant, actor_user: User | None, payload: dict[str, Any]) -> TenantTicket:
    title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or "").strip()
    if not title or not description:
        raise ValueError("title y description son obligatorios")

    channel = str(payload.get("channel") or "web").strip().lower()
    if channel not in _ALLOWED_CHANNELS:
        channel = "web"

    priority = str(payload.get("priority") or "medium").strip().lower()
    if priority not in _ALLOWED_PRIORITIES:
        priority = "medium"

    status = str(payload.get("status") or "nuevo").strip().lower()
    if status not in _ALLOWED_STATUSES:
        status = "nuevo"

    location = payload.get("location") if isinstance(payload.get("location"), dict) else {}
    assignee_id = payload.get("assignee_id")
    if assignee_id not in (None, ""):
        try:
            assignee_id = int(assignee_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("assignee_id invalido") from exc
        assignee = User.query.filter_by(id=assignee_id, tenant_id=tenant.id).first()
        assignee_role = str(getattr(assignee, "rol", "") or "").lower() if assignee else ""
        is_assignable = bool(getattr(assignee, "es_empleado", False)) or assignee_role in {"empleado", "employee", "admin", "tenant_admin"}
        if not assignee or not is_assignable:
            raise ValueError("assignee_not_found")

    ticket = TenantTicket(
        tenant_id=tenant.id,
        user_id=getattr(actor_user, "id", None),
        categoria=(payload.get("category") or None),
        descripcion=description,
        estado=status,
        origen=channel,
        latitud=location.get("lat"),
        longitud=location.get("lng"),
        datos_extra={
            "title": title,
            "type": payload.get("type") or "reclamo",
            "priority": priority,
            "channel": channel,
            "conversation_id": payload.get("conversation_id"),
            "contact": payload.get("contact") if isinstance(payload.get("contact"), dict) else {},
            "address": location.get("address"),
            "assignee_id": assignee_id,
            "comments": [],
            "source": payload.get("source") or "api_v2",
        },
    )
    db.session.add(ticket)
    db.session.flush()

    # Bridge comment for legacy timeline analytics (optional compatibility hook)
    legacy_comment = TicketComentario(
        comentario=f"[v2] Ticket creado: {title}",
        user_id=getattr(actor_user, "id", None),
        es_admin=_role_of(actor_user) in {"admin", "super_admin", "empleado"},
        origen="api_v2",
    )
    db.session.add(legacy_comment)

    policies = get_policies_for_tenant(tenant)
    apply_sla_to_ticket(ticket, policies, force_recalculate=True)

    record_ticket_event(
        tenant_id=tenant.id,
        event_type="ticket.created",
        ticket=ticket,
        actor_user=actor_user,
        details={"channel": channel, "priority": priority},
    )

    return ticket


def _query_for_viewer(tenant_id: int, viewer: User | None):
    query = TenantTicket.query.filter(TenantTicket.tenant_id == tenant_id)
    role = _role_of(viewer)
    if role == "usuario" and viewer is not None:
        query = query.filter(TenantTicket.user_id == viewer.id)
    return query


def list_tickets(*, tenant, viewer: User | None, filters: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, int]]:
    query = _query_for_viewer(tenant.id, viewer)

    status = filters.get("status")
    if status:
        query = query.filter(TenantTicket.estado == status)

    category = filters.get("category")
    if category:
        query = query.filter(TenantTicket.categoria == category)

    from_dt = _parse_iso(filters.get("from"))
    if from_dt:
        query = query.filter(TenantTicket.created_at >= from_dt)
    to_dt = _parse_iso(filters.get("to"))
    if to_dt:
        query = query.filter(TenantTicket.created_at <= to_dt)

    q = str(filters.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        query = query.filter(or_(TenantTicket.descripcion.ilike(like), TenantTicket.categoria.ilike(like)))

    all_items = query.order_by(TenantTicket.created_at.desc()).all()

    # JSON-field filters handled in Python for sqlite compatibility
    priority = (filters.get("priority") or "").strip().lower()
    assignee_id = filters.get("assignee_id")
    channel = (filters.get("channel") or "").strip().lower()

    filtered: list[TenantTicket] = []
    for ticket in all_items:
        extra = ticket.datos_extra if isinstance(ticket.datos_extra, dict) else {}
        if priority and str(extra.get("priority") or "").lower() != priority:
            continue
        if assignee_id not in (None, "") and str(extra.get("assignee_id") or "") != str(assignee_id):
            continue
        if channel and str(extra.get("channel") or ticket.origen or "").lower() != channel:
            continue
        filtered.append(ticket)

    page = max(1, int(filters.get("page") or 1))
    per_page = max(1, min(100, int(filters.get("per_page") or 50)))
    start = (page - 1) * per_page
    end = start + per_page
    paged = filtered[start:end]

    policies = get_policies_for_tenant(tenant)
    for ticket in filtered:
        apply_sla_to_ticket(ticket, policies)

    items = [serialize_ticket(ticket, viewer=viewer) for ticket in paged]

    summary = {"open": 0, "in_progress": 0, "overdue": 0, "closed": 0}
    for ticket in filtered:
        status_value = str(ticket.estado or "").lower()
        if status_value in {"cerrado", "closed", "resuelto", "resolved"}:
            summary["closed"] += 1
        elif status_value in {"in_progress"}:
            summary["in_progress"] += 1
        else:
            summary["open"] += 1
        if is_ticket_overdue(ticket):
            summary["overdue"] += 1

    pagination = {
        "page": page,
        "per_page": per_page,
        "total": len(filtered),
        "pages": (len(filtered) + per_page - 1) // per_page,
    }

    return items, pagination, summary


def patch_ticket(*, tenant, actor_user: User | None, ticket: TenantTicket, payload: dict[str, Any]) -> TenantTicket:
    if ticket.tenant_id != tenant.id:
        raise LookupError("ticket_not_found")

    extra = _ensure_extra(ticket)

    if "status" in payload:
        new_status = str(payload.get("status") or "").strip().lower()
        if new_status and new_status in _ALLOWED_STATUSES and new_status != ticket.estado:
            previous = ticket.estado
            ticket.estado = new_status
            record_ticket_event(
                tenant_id=tenant.id,
                event_type="ticket.status_changed",
                ticket=ticket,
                actor_user=actor_user,
                details={"from": previous, "to": new_status},
            )

    if "priority" in payload:
        new_priority = str(payload.get("priority") or "").strip().lower()
        if new_priority in _ALLOWED_PRIORITIES and new_priority != str(extra.get("priority") or "").lower():
            previous = extra.get("priority")
            extra["priority"] = new_priority
            record_ticket_event(
                tenant_id=tenant.id,
                event_type="ticket.priority_changed",
                ticket=ticket,
                actor_user=actor_user,
                details={"from": previous, "to": new_priority},
            )

    if "assignee_id" in payload:
        previous = extra.get("assignee_id")
        new_assignee = payload.get("assignee_id")
        if new_assignee not in (None, ""):
            try:
                new_assignee_int = int(new_assignee)
            except (TypeError, ValueError) as exc:
                raise ValueError("assignee_id invalido") from exc
            assignee = User.query.filter_by(id=new_assignee_int, tenant_id=tenant.id).first()
            assignee_role = str(getattr(assignee, "rol", "") or "").lower() if assignee else ""
            is_assignable = bool(getattr(assignee, "es_empleado", False)) or assignee_role in {"empleado", "employee", "admin", "tenant_admin"}
            if not assignee or not is_assignable:
                raise LookupError("assignee_not_found")
            new_assignee = assignee.id
        if str(previous or "") != str(new_assignee or ""):
            extra["assignee_id"] = new_assignee
            record_ticket_event(
                tenant_id=tenant.id,
                event_type="ticket.assigned",
                ticket=ticket,
                actor_user=actor_user,
                details={"from": previous, "to": new_assignee},
            )

    if "category" in payload:
        ticket.categoria = payload.get("category") or ticket.categoria

    ticket.datos_extra = extra
    policies = get_policies_for_tenant(tenant)
    apply_sla_to_ticket(ticket, policies, force_recalculate=True)
    flag_modified(ticket, "datos_extra")
    db.session.add(ticket)
    return ticket


def add_comment(*, tenant, actor_user: User | None, ticket: TenantTicket, body: str, visibility: str) -> dict[str, Any]:
    if ticket.tenant_id != tenant.id:
        raise LookupError("ticket_not_found")

    body = (body or "").strip()
    if not body:
        raise ValueError("body es obligatorio")

    visibility = (visibility or "public").strip().lower()
    if visibility not in {"public", "internal"}:
        visibility = "public"

    extra = _ensure_extra(ticket)
    comments = extra.get("comments") if isinstance(extra.get("comments"), list) else []
    now_iso = datetime.utcnow().isoformat()
    comment = {
        "id": len(comments) + 1,
        "body": body,
        "visibility": visibility,
        "author_user_id": getattr(actor_user, "id", None),
        "created_at": now_iso,
    }
    comments.append(comment)
    extra["comments"] = comments
    ticket.datos_extra = extra
    db.session.add(ticket)

    record_ticket_event(
        tenant_id=tenant.id,
        event_type="ticket.comment_added",
        ticket=ticket,
        actor_user=actor_user,
        details={"visibility": visibility, "comment_id": comment["id"]},
    )

    return comment
