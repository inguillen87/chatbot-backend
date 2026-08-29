from __future__ import annotations

from typing import Any
import uuid

from flask import Blueprint, current_app, g, jsonify, request

from cutover_writer_fence import cutover_writer_view
from extensions import db
from models import AnalyticsEventV2, TenantTicket
from routes.v2.tenants import V2TenantResolutionError, resolve_tenant_v2
from services.employee_ticket_access import (
    employee_ticket_category_access_allows,
    employee_ticket_category_scope,
    employee_ticket_category_values_allow,
)
from services.v2.ticket_event_service import list_ticket_events
from services.v2.ticket_service import (
    add_comment,
    create_ticket,
    list_tickets,
    patch_ticket,
    serialize_comment,
    serialize_ticket,
    ticket_attachment_payloads,
)
from utils.auth_decorators import _is_authorized_for_tenant
from utils.roles import ROLE_EMPLEADO, ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, canonical_role

v2_tickets_bp = Blueprint("v2_tickets", __name__, url_prefix="/api/v2")


def _request_id() -> str:
    incoming = (request.headers.get("X-Request-Id") or "").strip()
    return incoming or uuid.uuid4().hex


def _json_response(payload: dict[str, Any], status: int = 200):
    request_id = _request_id()
    body = dict(payload)
    body.setdefault("request_id", request_id)
    response = jsonify(body)
    response.status_code = status
    response.headers["X-Request-Id"] = request_id
    return response


def _error_response(message: str, status_code: int, reason_code: str = "request_error", action_hint: str = "check_request"):
    return _json_response(
        {
            "contract_version": "shared.error.v1",
            "status_code": status_code,
            "reason_code": reason_code,
            "retryable": False,
            "action_hint": action_hint,
            "error": {"code": status_code, "message": message},
            "message": message,
        },
        status_code,
    )


def _viewer():
    return getattr(g, "viewer", None)


def _tenant_access_error(tenant):
    viewer = _viewer()
    if viewer is None:
        return _error_response("Autenticacion requerida", 401, "auth_required", "login")
    if not _is_authorized_for_tenant(viewer, tenant_id=tenant.id, tenant_slug=tenant.slug):
        return _error_response("Acceso denegado para este tenant", 403, "tenant_access_denied", "switch_tenant")
    return None


def _viewer_role() -> str:
    return canonical_role(getattr(_viewer(), "rol", None))


def _is_operator() -> bool:
    return _viewer_role() in {ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, ROLE_EMPLEADO}


def _ticket_access_error(ticket: TenantTicket):
    if _is_operator():
        if not employee_ticket_category_access_allows(_viewer(), ticket):
            return _error_response("ticket no encontrado", 404, "ticket_not_found", "refresh_tickets")
        return None
    viewer = _viewer()
    if viewer is not None and ticket.user_id and str(ticket.user_id) == str(getattr(viewer, "id", "")):
        return None
    return _error_response("ticket no encontrado", 404, "ticket_not_found", "refresh_tickets")


def _resolve_ticket_or_error(ticket_id: int, tenant: Any):
    ticket = TenantTicket.query.get(ticket_id)
    if not ticket or ticket.tenant_id != tenant.id:
        return None, _error_response("ticket no encontrado", 404, "ticket_not_found", "refresh_tickets")
    ticket_error = _ticket_access_error(ticket)
    if ticket_error:
        return None, ticket_error
    return ticket, None


def _comment_author_type(ticket: TenantTicket, comment: dict[str, Any]) -> str:
    author_user_id = comment.get("author_user_id")
    if author_user_id and ticket.user_id and str(author_user_id) == str(ticket.user_id):
        return "citizen"
    if author_user_id:
        return "agent"
    return "citizen"


def _visible_ticket_comments(ticket: TenantTicket) -> list[dict[str, Any]]:
    extra = ticket.datos_extra if isinstance(ticket.datos_extra, dict) else {}
    comments = extra.get("comments") if isinstance(extra.get("comments"), list) else []
    if _is_operator():
        return [item for item in comments if isinstance(item, dict)]
    return [
        item
        for item in comments
        if isinstance(item, dict) and (item.get("visibility") or "public") == "public"
    ]


def _message_from_comment(ticket: TenantTicket, comment: dict[str, Any]) -> dict[str, Any]:
    created_at = comment.get("created_at")
    text = comment.get("body") or comment.get("texto") or ""
    author_type = _comment_author_type(ticket, comment)
    attachment = (
        comment.get("attachmentInfo")
        or comment.get("attachment_info")
        or comment.get("source_attachment")
        or None
    )
    payload = {
        "id": comment.get("id"),
        "comment_id": comment.get("id"),
        "texto": text,
        "comentario": text,
        "body": text,
        "fecha": created_at,
        "timestamp": created_at,
        "visibility": comment.get("visibility") or "public",
        "author_user_id": comment.get("author_user_id"),
        "author_type": author_type,
        "actor_type": author_type,
        "es_admin": author_type == "agent",
    }
    if isinstance(attachment, dict):
        payload["attachmentInfo"] = attachment
        payload["attachments"] = [attachment]
    return payload


def _comment_attachment_fingerprints(comments: list[dict[str, Any]]) -> set[str]:
    fingerprints: set[str] = set()
    for comment in comments:
        attachment = (
            comment.get("attachmentInfo")
            or comment.get("attachment_info")
            or comment.get("source_attachment")
        )
        if not isinstance(attachment, dict):
            continue
        value = attachment.get("id") or attachment.get("url") or attachment.get("name")
        if value not in (None, ""):
            fingerprints.add(str(value))
    return fingerprints


def _ticket_source_attachment_messages(ticket: TenantTicket, comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    comment_fingerprints = _comment_attachment_fingerprints(comments)
    messages: list[dict[str, Any]] = []
    for index, attachment in enumerate(ticket_attachment_payloads(ticket), start=1):
        fingerprint = attachment.get("id") or attachment.get("url") or attachment.get("name") or index
        if str(fingerprint) in comment_fingerprints:
            continue
        attachment_name = attachment.get("name") or attachment.get("filename") or "archivo"
        created_at = ticket.created_at.isoformat() if ticket.created_at else None
        messages.append(
            {
                "id": f"attachment-{ticket.id}-{index}",
                "comment_id": None,
                "texto": f"Adjunto recibido: {attachment_name}",
                "comentario": f"Adjunto recibido: {attachment_name}",
                "body": f"Adjunto recibido: {attachment_name}",
                "fecha": created_at,
                "timestamp": created_at,
                "visibility": "public",
                "author_user_id": ticket.user_id,
                "author_type": "citizen",
                "actor_type": "citizen",
                "es_admin": False,
                "attachmentInfo": attachment,
                "attachments": [attachment],
                "source": "tenant_ticket_attachment",
            }
        )
    return messages


def _ticket_v2_realtime_summary(ticket: TenantTicket, comments: list[dict[str, Any]]) -> dict[str, Any]:
    latest_comment_id = max(
        (int(item.get("id") or 0) for item in comments if str(item.get("id") or "").isdigit()),
        default=0,
    )
    return {
        "presence": {
            "active_count": 0,
            "active_viewers": [],
            "idle_count": 0,
            "idle_viewers": [],
        },
        "read_state": {
            "latest_comment_id": latest_comment_id,
            "viewers": [],
            "unread_viewers": [],
            "unread_viewer_count": 0,
        },
        "meta": {
            "ticket_type": "tenant",
            "ticket_id": ticket.id,
            "source": "tickets.v2.json_comments",
        },
    }


def _ticket_v2_timeline(ticket: TenantTicket, tenant: Any, comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    extra = ticket.datos_extra if isinstance(ticket.datos_extra, dict) else {}
    title = extra.get("title") or ticket.categoria or "Ticket"
    timeline: list[dict[str, Any]] = [
        {
            "id": f"ticket-created-{ticket.id}",
            "tipo": "ticket_creado",
            "event_type": "ticket.created",
            "fecha": ticket.created_at.isoformat() if ticket.created_at else None,
            "estado": ticket.estado,
            "texto": f"Ticket creado: {title}",
            "actor_type": "system",
            "source": "tenant_ticket",
        }
    ]

    for comment in comments:
        message = _message_from_comment(ticket, comment)
        timeline.append(
            {
                **message,
                "id": f"comment-{message.get('id')}",
                "tipo": "comentario",
                "event_type": "ticket.comment_added",
                "source": "tenant_ticket_comment",
            }
        )

    for message in _ticket_source_attachment_messages(ticket, comments):
        timeline.append(
            {
                **message,
                "id": f"ticket-attachment-{message.get('id')}",
                "tipo": "archivo",
                "event_type": "ticket.attachment_received",
                "source": "tenant_ticket_attachment",
            }
        )

    if _is_operator():
        for event in list_ticket_events(tenant_id=tenant.id, ticket_id=ticket.id):
            if event.event_type == "ticket.created":
                continue
            timeline.append(
                {
                    "id": f"audit-{event.id}",
                    "tipo": "estado" if event.event_type == "ticket.status_changed" else "evento",
                    "event_type": event.event_type,
                    "resource_type": event.resource_type,
                    "resource_id": event.resource_id,
                    "fecha": event.created_at.isoformat() if event.created_at else None,
                    "estado": (event.details or {}).get("to") if event.event_type == "ticket.status_changed" else None,
                    "texto": event.event_type,
                    "actor_user_id": event.actor_user_id,
                    "actor_type": "agent" if event.actor_user_id else "system",
                    "details": event.details or {},
                    "source": "audit_event",
                }
            )

    return sorted(timeline, key=lambda item: item.get("fecha") or "")


def _ticket_detail_payload(ticket: TenantTicket, *, viewer: Any = None) -> dict[str, Any]:
    serialized = serialize_ticket(ticket, viewer=viewer)
    base = f"/api/v2/tickets/{ticket.id}"
    return {
        **serialized,
        "source_model": "TenantTicket",
        "ticket_type": "tenant_ticket",
        "detail_endpoint": base,
        "messages_endpoint": f"{base}/messages",
        "timeline_endpoint": f"{base}/timeline",
        "events_endpoint": f"{base}/events",
        "ai_enrichment_endpoint": f"{base}/ai-enrichment",
    }


def _payload_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "si"}


def _reject_ai_enrichment_mutation_request(payload: dict[str, Any]) -> str | None:
    mutation_flags = ("apply", "persist", "mutate", "auto_update", "auto_apply")
    if any(_payload_truthy(payload.get(flag)) for flag in mutation_flags):
        return "ai-enrichment is advisory-only; state mutation is not supported"

    mutation_fields = ("estado", "state", "new_state", "assigned_to", "asignado_a_id")
    if any(field in payload for field in mutation_fields):
        return "ai-enrichment cannot receive operational mutation fields"
    return None


def _safe_action_ids(actions: Any) -> list[str]:
    if not isinstance(actions, list):
        return []
    ids: list[str] = []
    for item in actions[:10]:
        if isinstance(item, dict):
            raw = item.get("id") or item.get("action_id") or item.get("name")
        else:
            raw = item
        text = str(raw or "").strip()
        if text:
            ids.append(text[:80])
    return ids


def _record_ai_enrichment_analytics_event(
    *,
    tenant: Any,
    ticket: TenantTicket,
    enrichment: dict[str, Any],
    domain_scope: str,
    comments_count: int,
) -> None:
    crm_hints = enrichment.get("crm_hints") if isinstance(enrichment.get("crm_hints"), dict) else {}
    huggingface = enrichment.get("huggingface") if isinstance(enrichment.get("huggingface"), dict) else {}
    intent = huggingface.get("intent") if isinstance(huggingface.get("intent"), dict) else {}
    provider_family = huggingface.get("provider_family") or intent.get("provider_family") or "local"
    provider = intent.get("provider") or huggingface.get("provider") or enrichment.get("provider")
    recommended_actions = _safe_action_ids(crm_hints.get("recommended_actions") or enrichment.get("recommended_actions"))
    text_chars = len(str(getattr(ticket, "descripcion", "") or getattr(ticket, "description", "") or ""))

    metadata = {
        "advisory_only": True,
        "ticket_id": ticket.id,
        "source_model": "TenantTicket",
        "domain_scope": domain_scope,
        "provider_family": str(provider_family)[:80],
        "mode": str(huggingface.get("mode") or enrichment.get("mode") or "advisory")[:80],
        "provider": str(provider or "unavailable")[:120],
        "suggested_queue": str(crm_hints.get("suggested_queue") or "")[:120],
        "requires_human_attention": bool(crm_hints.get("requires_human_attention")),
        "recommended_action_ids": recommended_actions,
        "text_chars": text_chars,
        "comments_count": comments_count,
    }

    try:
        db.session.add(
            AnalyticsEventV2(
                tenant_id=tenant.id,
                tenant_type=domain_scope,
                user_id=getattr(_viewer(), "id", None),
                channel="crm_panel",
                event_name="ticket_ai_enrichment_generated",
                metadata_payload=metadata,
                entity_ref=f"ticket:{ticket.id}",
            )
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("ticket_ai_enrichment_analytics_event_failed", extra={"ticket_id": ticket.id})


def _parse_comments_limit(payload: dict[str, Any], *, default: int = 40) -> int:
    if _payload_truthy(payload.get("exclude_comments")):
        return 0
    raw_limit = payload.get("comments_limit", default)
    try:
        return max(0, min(int(raw_limit), 100))
    except (TypeError, ValueError):
        return default


def _domain_scope_for_tenant(tenant: Any) -> str:
    return "municipio" if str(getattr(tenant, "tipo", "") or "").strip().lower() == "municipio" else "pyme"


def _operator_error():
    if _is_operator():
        return None
    return _error_response("Permisos insuficientes para operar tickets", 403, "operator_required", "ask_operator")


def _resolve_tenant_or_error():
    explicit_slug = (
        request.headers.get("X-Tenant-Slug")
        or request.headers.get("X-Tenant")
        or request.args.get("tenant_slug")
        or request.args.get("tenant")
        or ""
    ).strip()
    if not explicit_slug:
        return None, _error_response("X-Tenant-Slug es obligatorio en tickets v2", 400, "missing_tenant", "send_tenant_slug")
    try:
        return resolve_tenant_v2(required=True, explicit_slug=explicit_slug), None
    except V2TenantResolutionError as exc:
        return None, _error_response(exc.message, exc.status_code, "tenant_resolution_failed", "check_tenant_slug")


@v2_tickets_bp.route("/tickets", methods=["GET"])
@cutover_writer_view
def list_tickets_v2():
    tenant, error = _resolve_tenant_or_error()
    if error:
        return error
    access_error = _tenant_access_error(tenant)
    if access_error:
        return access_error

    filters = {
        "status": request.args.get("status"),
        "priority": request.args.get("priority"),
        "assignee_id": request.args.get("assignee_id"),
        "category": request.args.get("category"),
        "channel": request.args.get("channel"),
        "from": request.args.get("from"),
        "to": request.args.get("to"),
        "q": request.args.get("q"),
        "page": request.args.get("page"),
        "per_page": request.args.get("per_page"),
    }

    items, pagination, summary = list_tickets(tenant=tenant, viewer=_viewer(), filters=filters)
    db.session.commit()
    return _json_response({"contract_version": "tickets.v2.list", "items": items, "pagination": pagination, "summary": summary})


@v2_tickets_bp.route("/tickets", methods=["POST"])
def create_ticket_v2():
    tenant, error = _resolve_tenant_or_error()
    if error:
        return error
    access_error = _tenant_access_error(tenant)
    if access_error:
        return access_error

    payload = dict(request.get_json(silent=True) or {})
    if _viewer_role() == ROLE_EMPLEADO and payload.get("category") in (None, ""):
        category_scope = employee_ticket_category_scope(_viewer())
        if len(category_scope.names) == 1:
            payload["category"] = next(iter(category_scope.names))
        else:
            return _error_response(
                "category es obligatoria para crear un ticket como empleado",
                400,
                "category_required",
                "choose_allowed_category",
            )
    if (
        _is_operator()
        and payload.get("category") not in (None, "")
        and not employee_ticket_category_values_allow(
            _viewer(),
            category=payload.get("category"),
        )
    ):
        return _error_response("ticket no encontrado", 404, "ticket_not_found", "refresh_tickets")
    try:
        ticket = create_ticket(tenant=tenant, actor_user=_viewer(), payload=payload)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        if str(exc) == "assignee_category_scope_mismatch":
            return _error_response(
                "El agente no tiene acceso a la categoria del ticket",
                409,
                "assignee_category_scope_mismatch",
                "choose_compatible_assignee",
            )
        return _error_response(str(exc), 400, "validation_failed", "fix_ticket_payload")

    serialized = _ticket_detail_payload(ticket, viewer=_viewer())
    return _json_response(
        {
            **serialized,
            "contract_version": "tickets.v2.detail",
            "ok": True,
            "ticket": serialized,
        },
        201,
    )


@v2_tickets_bp.route("/tickets/<int:ticket_id>", methods=["GET"])
def get_ticket_v2(ticket_id: int):
    tenant, error = _resolve_tenant_or_error()
    if error:
        return error
    access_error = _tenant_access_error(tenant)
    if access_error:
        return access_error

    ticket, error = _resolve_ticket_or_error(ticket_id, tenant)
    if error:
        return error

    comments = _visible_ticket_comments(ticket)
    serialized = _ticket_detail_payload(ticket, viewer=_viewer())
    return _json_response(
        {
            **serialized,
            "contract_version": "tickets.v2.detail",
            "ok": True,
            "source_model": "TenantTicket",
            "ticket_type": "tenant_ticket",
            "ticket": serialized,
            "realtime_state": _ticket_v2_realtime_summary(ticket, comments),
        }
    )


@v2_tickets_bp.route("/tickets/<int:ticket_id>", methods=["PATCH"])
def patch_ticket_v2(ticket_id: int):
    tenant, error = _resolve_tenant_or_error()
    if error:
        return error
    access_error = _tenant_access_error(tenant)
    if access_error:
        return access_error
    role_error = _operator_error()
    if role_error:
        return role_error

    ticket, error = _resolve_ticket_or_error(ticket_id, tenant)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    if payload.get("category") not in (None, "") and not employee_ticket_category_values_allow(
        _viewer(),
        category=payload.get("category"),
    ):
        return _error_response("ticket no encontrado", 404, "ticket_not_found", "refresh_tickets")
    try:
        updated = patch_ticket(tenant=tenant, actor_user=_viewer(), ticket=ticket, payload=payload)
        db.session.commit()
    except LookupError as exc:
        db.session.rollback()
        if str(exc) == "assignee_not_found":
            return _error_response("Empleado no encontrado para este tenant", 404, "assignee_not_found", "choose_valid_assignee")
        return _error_response("ticket no encontrado", 404, "ticket_not_found", "refresh_tickets")
    except ValueError as exc:
        db.session.rollback()
        if str(exc) == "assignee_category_scope_mismatch":
            return _error_response(
                "El agente no tiene acceso a la categoria del ticket",
                409,
                "assignee_category_scope_mismatch",
                "choose_compatible_assignee",
            )
        return _error_response(str(exc), 400, "validation_failed", "fix_ticket_payload")

    serialized = _ticket_detail_payload(updated, viewer=_viewer())
    return _json_response(
        {
            **serialized,
            "contract_version": "tickets.v2.detail",
            "ok": True,
            "ticket": serialized,
        }
    )


@v2_tickets_bp.route("/tickets/<int:ticket_id>/comments", methods=["POST"])
def add_ticket_comment_v2(ticket_id: int):
    tenant, error = _resolve_tenant_or_error()
    if error:
        return error
    access_error = _tenant_access_error(tenant)
    if access_error:
        return access_error

    ticket, error = _resolve_ticket_or_error(ticket_id, tenant)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    if (payload.get("visibility") or "public").strip().lower() == "internal" and not _is_operator():
        return _error_response("Solo el equipo puede crear notas internas", 403, "operator_required", "send_public_comment")
    try:
        comment = add_comment(
            tenant=tenant,
            actor_user=_viewer(),
            ticket=ticket,
            body=payload.get("body") or "",
            visibility=payload.get("visibility") or "public",
        )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _error_response(str(exc), 400, "validation_failed", "fix_comment_payload")

    serialized = serialize_comment(comment)
    return _json_response(
        {
            **serialized,
            "contract_version": "tickets.v2.comment",
            "ok": True,
            "comment": serialized,
        },
        201,
    )


@v2_tickets_bp.route("/tickets/<int:ticket_id>/messages", methods=["GET"])
def list_ticket_messages_v2(ticket_id: int):
    tenant, error = _resolve_tenant_or_error()
    if error:
        return error
    access_error = _tenant_access_error(tenant)
    if access_error:
        return access_error

    ticket, error = _resolve_ticket_or_error(ticket_id, tenant)
    if error:
        return error

    comments = _visible_ticket_comments(ticket)
    messages = [
        *_ticket_source_attachment_messages(ticket, comments),
        *[_message_from_comment(ticket, comment) for comment in comments],
    ]
    return _json_response(
        {
            "contract_version": "tickets.v2.messages",
            "ticket_id": ticket.id,
            "ticket_type": "tenant",
            "estado_chat": ticket.estado,
            "messages": messages,
            "mensajes": messages,
            "realtime_state": _ticket_v2_realtime_summary(ticket, comments),
        }
    )


@v2_tickets_bp.route("/tickets/<int:ticket_id>/timeline", methods=["GET"])
def list_ticket_timeline_v2(ticket_id: int):
    tenant, error = _resolve_tenant_or_error()
    if error:
        return error
    access_error = _tenant_access_error(tenant)
    if access_error:
        return access_error

    ticket, error = _resolve_ticket_or_error(ticket_id, tenant)
    if error:
        return error

    comments = _visible_ticket_comments(ticket)
    messages = [
        *_ticket_source_attachment_messages(ticket, comments),
        *[_message_from_comment(ticket, comment) for comment in comments],
    ]
    timeline = _ticket_v2_timeline(ticket, tenant, comments)
    realtime_state = _ticket_v2_realtime_summary(ticket, comments)
    unified = [
        {
            "id": item.get("id"),
            "source": item.get("source") or "tenant_ticket",
            "stream_type": "message" if item.get("tipo") == "comentario" else item.get("tipo") or "timeline_event",
            "timestamp": item.get("fecha") or item.get("timestamp"),
            "actor_type": item.get("actor_type") or item.get("author_type") or "system",
            "preview_text": item.get("texto") or item.get("comentario") or item.get("body"),
            "status": item.get("estado"),
            "comment_id": item.get("comment_id"),
            "payload": item,
        }
        for item in timeline
    ]

    return _json_response(
        {
            "contract_version": "tickets.v2.timeline",
            "ticket_id": ticket.id,
            "ticket_type": "tenant",
            "estado_chat": ticket.estado,
            "timeline": timeline,
            "historial_chat": messages,
            "unified_conversation_stream": unified,
            "realtime_state": realtime_state,
        }
    )


@v2_tickets_bp.route("/tickets/<int:ticket_id>/ai-enrichment", methods=["GET", "POST"])
@cutover_writer_view
def ticket_ai_enrichment_v2(ticket_id: int):
    tenant, error = _resolve_tenant_or_error()
    if error:
        return error
    access_error = _tenant_access_error(tenant)
    if access_error:
        return access_error
    role_error = _operator_error()
    if role_error:
        return role_error

    ticket, error = _resolve_ticket_or_error(ticket_id, tenant)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return _error_response("payload debe ser un objeto JSON", 400, "validation_failed", "fix_ai_payload")

    mutation_rejection = _reject_ai_enrichment_mutation_request(payload)
    if mutation_rejection:
        return _error_response(mutation_rejection, 400, "ai_enrichment_mutation_rejected", "remove_mutation_fields")

    comments_limit = _parse_comments_limit(payload)
    comments = _visible_ticket_comments(ticket)[:comments_limit] if comments_limit else []
    domain_scope = _domain_scope_for_tenant(tenant)

    from services.ticket_ai_enrichment import build_ticket_ai_enrichment

    enrichment = build_ticket_ai_enrichment(
        ticket,
        scope=domain_scope,
        comments=comments,
        tenant=tenant,
    )
    enrichment["ticket_id"] = ticket.id
    enrichment["ticket_type"] = "tenant"
    enrichment["source_model"] = "TenantTicket"
    enrichment["domain_scope"] = domain_scope
    _record_ai_enrichment_analytics_event(
        tenant=tenant,
        ticket=ticket,
        enrichment=enrichment,
        domain_scope=domain_scope,
        comments_count=len(comments),
    )
    return _json_response(enrichment)


@v2_tickets_bp.route("/tickets/<int:ticket_id>/events", methods=["GET"])
def list_ticket_events_v2(ticket_id: int):
    tenant, error = _resolve_tenant_or_error()
    if error:
        return error
    access_error = _tenant_access_error(tenant)
    if access_error:
        return access_error
    role_error = _operator_error()
    if role_error:
        return role_error

    ticket, error = _resolve_ticket_or_error(ticket_id, tenant)
    if error:
        return error

    events = list_ticket_events(tenant_id=tenant.id, ticket_id=ticket.id)
    items = [
        {
            "id": event.id,
            "event_type": event.event_type,
            "resource_type": event.resource_type,
            "resource_id": event.resource_id,
            "details": event.details or {},
            "actor_user_id": event.actor_user_id,
            "created_at": event.created_at.isoformat() if event.created_at else None,
        }
        for event in events
    ]
    return _json_response({"contract_version": "tickets.v2.events", "items": items})
