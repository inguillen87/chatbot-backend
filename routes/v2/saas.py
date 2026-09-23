from __future__ import annotations

import ast
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from html import escape
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote_plus
import uuid

from flask import Blueprint, current_app, g, has_request_context, jsonify, request
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm.attributes import flag_modified
from werkzeug.exceptions import RequestEntityTooLarge

from cutover_writer_fence import cutover_writer_view
from extensions import db
from models import (
    ArchivoAdjunto,
    CatalogoItem,
    DomainEffectOutbox,
    EncEncuesta,
    EncRespuesta,
    InboxTicketArtifact,
    MarketOrder,
    MessageTemplateRegistry,
    MunicipioTicket,
    MunicipioTicketHandoffEvent,
    MunicipioTicketReplyEvent,
    Notification,
    NotificationTemplate,
    PublicSurvey,
    PublicSurveyResponse,
    PymeTicket,
    TenantProfile,
    TenantTicket,
    TenantTicketReplyEvent,
    TicketComentario,
    TicketDomainEffectReceipt,
    User,
)
from routes.v2.tenants import V2TenantResolutionError, resolve_tenant_v2
from services.education_contracts import build_education_admin_menu, build_education_profile, is_education_tenant
from services.employee_ticket_access import (
    apply_employee_ticket_category_scope,
    employee_ticket_category_access_allows,
    ticket_assignee_is_compatible,
    ticket_assignee_is_operational,
)
from services.employee_routing import (
    build_employee_routing_payload,
    employee_ref,
    eligible_employees_for_ticket,
    find_ticket_for_assignment,
    normalize_scope_list,
    tenant_open_ticket_snapshots,
    tenant_operational_dimensions,
    workload_by_employee,
)
from services.ticket_assignment_policy import (
    TicketAssignmentPolicyError,
    actor_can_assign_tickets,
    assignment_transition,
)
from services.catalog_quality import build_catalog_quality_fallback_payload, build_catalog_quality_payload
from services.channel_activation import build_channel_activation_payload
from services.attachment_delivery import serialize_attachment_for_delivery
from services.crm_operational_queue import (
    OperationalQueueError,
    build_operational_queue,
    parse_queue_request,
)
from services.crm_operational_queue_guard import (
    OperationalQueueGuardError,
    attach_operational_queue_rate_limit_headers,
    enforce_operational_queue_rate_limit,
)
from services.demo_sandbox_contract import build_demo_whatsapp_sandbox_contract, sandbox_context_from_contract
from services.demo_surveys import resolve_demo_public_frontend_base_url
from services.live_chat_schedule import build_tenant_live_chat_status
from services.omnichannel_message_policy import (
    OMNICHANNEL_REPLY_MAX_BODY_BYTES,
    OmnichannelMessagePolicyError,
    normalize_omnichannel_reply_body,
)
from services.survey_response_provenance import (
    SURVEY_RESPONSE_ORIGIN_LEGACY_UNVERIFIED,
    SURVEY_RESPONSE_ORIGIN_REAL,
    SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO,
    build_survey_response_provenance,
)
from services.operational_intelligence import build_operational_dashboard, build_operational_freshness
from services.provider_platform import build_whatsapp_provider_status, sync_twilio_provider_records
from services.plan_access import integration_access_payload, integration_frontend_contract, plan_allows_full_integrations
from services.tenant_whatsapp_onboarding import refresh_tenant_whatsapp_onboarding
from services.tenant_ticket_scope import scoped_municipio_ticket_query
from services.twilio_tech_provider import (
    STATE_KEY,
    build_twilio_tech_provider_contract,
    merge_twilio_state,
    poll_whatsapp_sender_status,
    provision_twilio_subaccount,
    provision_twilio_voice_application,
    register_whatsapp_sender,
    verify_meta_embedded_signup_completion,
)
from services.v2.sla_service import (
    apply_priority_change_sla,
    apply_reopen_sla,
    apply_resolution_sla,
    evaluate_ticket_sla,
    get_policies_for_tenant,
    is_ticket_overdue,
)
from services.whatsapp_experience import _template_creation_manifest_payload, build_whatsapp_experience
from services.whatsapp_workflow_studio import (
    MAX_DRAFT_BYTES,
    MAX_EVENT_BYTES,
    build_workflow_studio_contract,
    simulate_workflow_draft,
    validate_workflow_draft,
)
from services.whatsapp_workflow_versioning import (
    WorkflowStudioError,
    get_workflow_ledger,
    list_workflow_ledgers,
    publish_workflow,
    require_workflow_durable_control_plane,
    review_workflow_subject,
    rollback_workflow,
    save_workflow_draft,
    workflow_durable_control_plane_gate,
)
from utils.auth_helpers import token_requerido
from utils.permissions import require_role
from utils.roles import ROLE_EMPLEADO, canonical_role, first_specific_tenant_slug, is_authorized_superadmin_user

v2_saas_bp = Blueprint("v2_saas", __name__, url_prefix="/api/v2")


_ACTIVE_TICKET_STATES = {"nuevo", "open", "pendiente", "in_progress", "en_proceso", "waiting_customer"}
_CLOSED_TICKET_STATES = {"resuelto", "cerrado", "closed", "resolved"}
_LIVE_CHAT_QUEUE_STATES = {
    "esperando_agente_en_vivo",
    "queued_for_agent",
    "waiting_agent",
    "pending_admin_response",
    "offline_waiting_admin_response",
}


def _ticket_transition_status(action: str, raw_status: Any, *, default: str) -> str | None:
    """Normalize an action status without allowing a contradictory transition."""

    status = default if raw_status is None else (str(raw_status).strip().lower() or default)
    allowed = _CLOSED_TICKET_STATES if action == "close" else _ACTIVE_TICKET_STATES
    return status if status in allowed else None
_INBOX_TEAM_ORIGINS = {"admin_panel", "agent", "team", "operator", "internal", "municipio", "pyme"}
_HANDOFF_QUEUED_STATES = {
    "pending",
    "queued",
    "waiting_agent",
    "esperando_agente_en_vivo",
}
_HANDOFF_TERMINAL_STATES = {"resolved", "cancelled", "canceled", "expired", "rejected"}
_HANDOFF_SUPPORTED_CHANNELS = {"operator", "live_chat", "phone"}
_OPERATIONAL_OWNERSHIP_ACTIONS = {
    "reply",
    "attach_file",
    "share_location",
    "send_form",
    "handoff",
    "resume_ai",
    "close",
    "reopen",
    "set_priority",
}
# Administrative replies can target WhatsApp, email, or web. Keep the JSON
# envelope bounded while leaving room for routing/idempotency metadata, and
# align the durable normalized body with the repository's 8 KiB omnichannel
# message policy (services.whatsapp_inbound_turns.MAX_BODY_BYTES).
_OMNICHANNEL_ACTION_MAX_REQUEST_BYTES = 16 * 1024
_OMNICHANNEL_REPLY_MAX_BODY_BYTES = OMNICHANNEL_REPLY_MAX_BODY_BYTES
_WHATSAPP_QA_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "qa_whatsapp_flows.py"
_TWILIO_STATE_SECRET_KEYS = {
    "embedded_signup_code",
    "auth_code",
    "authorization_code",
    "access_token",
    "refresh_token",
}


class _OmnichannelActionRequestTooLarge(RequestEntityTooLarge):
    """Raised before app middleware can deserialize an oversized action body."""


def _scrub_twilio_state_secrets(tenant: TenantProfile, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Remove short-lived Meta/Twilio secrets from persisted provider state."""

    cfg = tenant.configuracion if isinstance(getattr(tenant, "configuracion", None), dict) else {}
    provider_state = dict(state or cfg.get(STATE_KEY) or {})
    changed = False
    for key in _TWILIO_STATE_SECRET_KEYS:
        if key in provider_state:
            provider_state.pop(key, None)
            changed = True

    if changed:
        cfg[STATE_KEY] = provider_state
        tenant.configuracion = cfg
        flag_modified(tenant, "configuracion")

    return provider_state


def _request_id() -> str:
    incoming = (request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id") or "").strip()
    return incoming or uuid.uuid4().hex


def _json_response(payload: dict[str, Any], status: int = 200):
    request_id = _request_id()
    body = dict(payload)
    body.setdefault("request_id", request_id)
    response = jsonify(body)
    response.status_code = status
    response.headers["X-Request-Id"] = request_id
    return response


def _error_response(message: str, status_code: int, reason_code: str, action_hint: str):
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


def _bounded_omnichannel_action_raw_body() -> bytes:
    configured_max = request.max_content_length
    effective_max = (
        min(configured_max, _OMNICHANNEL_ACTION_MAX_REQUEST_BYTES)
        if configured_max is not None and configured_max > 0
        else _OMNICHANNEL_ACTION_MAX_REQUEST_BYTES
    )
    if (
        request.content_length is not None
        and request.content_length > effective_max
    ):
        raise _OmnichannelActionRequestTooLarge()

    # Werkzeug's limited stream may return exactly its cap without consuming an
    # unknown-length remainder. Permit one sentinel byte so chunked requests can
    # be rejected without buffering an unbounded body.
    request.max_content_length = effective_max + 1
    try:
        raw_body = request.get_data(cache=True)
    except RequestEntityTooLarge as exc:
        raise _OmnichannelActionRequestTooLarge() from exc
    if len(raw_body) > effective_max:
        raise _OmnichannelActionRequestTooLarge()
    return raw_body


@v2_saas_bp.url_value_preprocessor
def _guard_omnichannel_action_before_app_middleware(endpoint, _values):
    """Run before app ``before_request`` hooks that may inspect JSON bodies."""

    if (
        endpoint == f"{v2_saas_bp.name}.omnichannel_inbox_action_v2"
        and request.method == "POST"
    ):
        _bounded_omnichannel_action_raw_body()


@v2_saas_bp.errorhandler(_OmnichannelActionRequestTooLarge)
def _omnichannel_action_request_too_large(_error):
    return _error_response(
        "El request de accion de inbox supera el tamano permitido.",
        413,
        "inbox_action_request_too_large",
        "reduce_inbox_action_payload",
    )


def _omnichannel_action_json_payload():
    """Parse only after the routing-stage byte guard has accepted the body."""

    _bounded_omnichannel_action_raw_body()
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, Mapping) else {}


def _omnichannel_reply_body(payload: Mapping[str, Any]):
    """Normalize and bound the exact text copied into durable reply records."""

    value = payload.get("body") or payload.get("message") or payload.get("comentario")
    try:
        body = normalize_omnichannel_reply_body(value)
    except OmnichannelMessagePolicyError as exc:
        if exc.reason_code == "reply_body_too_large":
            return None, _error_response(
                "El mensaje supera el tamano permitido.",
                413,
                "reply_body_too_large",
                "reduce_reply_body",
            )
        return None, _error_response(
            "El mensaje no puede estar vacio",
            400,
            "reply_body_required",
            "send_reply_body",
        )
    return body, None


def _integration_plan_error(tenant: TenantProfile, feature_id: str = "whatsapp_business_platform"):
    access = integration_access_payload(tenant)
    frontend_contract = integration_frontend_contract(access, feature_id)
    return _json_response(
        {
            **access,
            "access": access,
            "frontend_contract": frontend_contract,
            "frontend": frontend_contract,
            "contract_version": "tenant.integration_access.v1",
            "status_code": 403,
            "tenant": _tenant_ref(tenant),
            "retryable": False,
            "action_hint": "upgrade_to_full",
            "error": "plan_required",
            "error_detail": {"code": 403, "message": access["message"]},
        },
        403,
    )


def _require_full_integration_plan(tenant: TenantProfile, feature_id: str = "whatsapp_business_platform"):
    if plan_allows_full_integrations(tenant):
        return None
    return _integration_plan_error(tenant, feature_id)


def _tenant_slug_from_request(path_slug: str | None = None) -> str:
    body = request.get_json(silent=True) if request.method in {"POST", "PUT", "PATCH"} else None
    body_slug = body.get("tenant_slug") if isinstance(body, dict) else None
    return first_specific_tenant_slug(
        path_slug,
        request.headers.get("X-Tenant-Slug"),
        request.headers.get("X-Tenant"),
        request.args.get("tenant_slug"),
        request.args.get("tenant"),
        body_slug,
    )


def _resolve_tenant_or_error(current_user: User, path_slug: str | None = None):
    slug = _tenant_slug_from_request(path_slug)
    try:
        tenant = resolve_tenant_v2(required=True, explicit_slug=slug or None)
    except V2TenantResolutionError as exc:
        return None, _error_response(exc.message, exc.status_code, "tenant_resolution_failed", "send_valid_tenant")

    if not _user_can_access_tenant(current_user, tenant):
        return None, _error_response("Permisos insuficientes para este tenant", 403, "forbidden_tenant", "switch_tenant")
    return tenant, None


def _user_can_access_tenant(user: User, tenant: TenantProfile) -> bool:
    if is_authorized_superadmin_user(user):
        return True
    if str(getattr(user, "tenant_id", "") or "") == str(tenant.id):
        return True
    if (getattr(user, "tenant_slug", "") or "").strip().lower() == (tenant.slug or "").strip().lower():
        return True
    owner_ids = {getattr(tenant, "pyme_id", None), getattr(tenant, "municipio_id", None)}
    return getattr(user, "id", None) in owner_ids


def _tenant_ref(tenant: TenantProfile) -> dict[str, Any]:
    return {
        "id": tenant.id,
        "slug": tenant.slug,
        "nombre": tenant.nombre,
        "tipo": tenant.tipo,
        "vertical": tenant.vertical,
        "subvertical": tenant.subvertical,
        "plan": tenant.plan,
        "is_active": bool(getattr(tenant, "is_active", True)),
    }


def _tenant_live_chat_socket_room(tenant: TenantProfile) -> str | None:
    tenant_type = str(getattr(tenant, "tipo", "") or "").strip().lower()
    if tenant_type == "municipio" and getattr(tenant, "municipio_id", None):
        return f"municipio_{tenant.municipio_id}"
    if getattr(tenant, "pyme_id", None):
        return f"pyme_{tenant.pyme_id}"
    if getattr(tenant, "municipio_id", None):
        return f"municipio_{tenant.municipio_id}"
    return f"tenant_{tenant.id}" if getattr(tenant, "id", None) else None


def _tenant_inbox_live_chat_status(tenant: TenantProfile) -> dict[str, Any]:
    socket_room = _tenant_live_chat_socket_room(tenant)
    status = build_tenant_live_chat_status(tenant, socket_room=socket_room)
    status["contract_version"] = "inbox.live_chat_channel.v1"
    status["base_channel_state"] = "online" if status.get("enabled") and status.get("available") else "offline"
    status["channel_state"] = status["base_channel_state"]
    status["accepts_offline_messages"] = True
    return status


def _inbox_live_chat_contract(
    base_status: Mapping[str, Any],
    *,
    queued: bool = False,
    pending_customer_messages: int = 0,
    pending_since: str | None = None,
    ticket_id: Any = None,
    source_model: str | None = None,
    crm_route: str | None = None,
) -> dict[str, Any]:
    status = deepcopy(dict(base_status or {}))
    base_state = str(status.get("base_channel_state") or ("online" if status.get("available") else "offline"))
    channel_state = "queued" if queued else base_state
    schedule_label = status.get("description")
    offline_message = status.get("offline_message") if isinstance(status.get("offline_message"), dict) else {}
    offline_fallback_message = status.get("offline_fallback_message") or offline_message.get("message")
    source_model_suffix = f"&source_model={source_model}" if source_model else ""
    admin_route = crm_route or (
        f"/perfil?tab=tickets&ticket_id={ticket_id}&focus=live_chat&source=omnichannel_inbox{source_model_suffix}"
        if ticket_id is not None
        else "/perfil?tab=tickets"
    )
    status["channel_state"] = channel_state
    status["availability"] = {
        "state": channel_state,
        "base_state": base_state,
        "label": {
            "online": "Atencion en vivo disponible",
            "offline": "Mesa de ayuda fuera de horario",
            "queued": "Mensaje en cola para el equipo",
        }.get(channel_state, channel_state),
        "schedule_label": schedule_label,
        "timezone": status.get("timezone"),
        "offline_fallback_message": offline_fallback_message,
    }
    status["queue"] = {
        "enabled": True,
        "state": "waiting_team_response" if queued else "empty",
        "pending_customer_messages": max(0, int(pending_customer_messages or 0)),
        "pending_since": pending_since,
        "next_team_action": "reply_from_inbox" if queued else "monitor_ticket",
        "admin_route": admin_route,
    }
    status["admin_response_surface"] = {
        "id": "tenant_claims_inbox",
        "label": "Inbox de reclamos",
        "route": admin_route,
        "href": admin_route,
        "frontend_path": admin_route,
        "ticket_id": ticket_id,
        "source_model": source_model,
        "focus": "live_chat",
    }
    status["actions"] = [
        {
            "id": "open_live_chat_thread" if channel_state == "online" else "open_offline_queue_thread",
            "label": "Abrir hilo de atencion" if channel_state == "online" else "Responder mensaje en cola",
            "href": admin_route,
            "frontend_path": admin_route,
            "ui_hint": "focus_reply_composer" if queued else "focus_ticket_conversation",
        }
    ]
    status["frontend_contract"] = {
        "render_as": "live_chat_channel_state",
        "states": ["online", "offline", "queued"],
        "must_show_schedule": True,
        "must_show_offline_fallback": True,
        "deep_link_supported": True,
    }
    return status


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _date_range_from_request(default_days: int = 30) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    start_date = now - timedelta(days=default_days)
    end_date = now

    from_str = request.args.get("from")
    to_str = request.args.get("to")

    if from_str:
        try:
            start_date = datetime.fromisoformat(from_str.replace("Z", "+00:00"))
        except ValueError:
            pass
    if to_str:
        try:
            end_date = datetime.fromisoformat(to_str.replace("Z", "+00:00"))
        except ValueError:
            pass

    return start_date, end_date


def _tenant_owner_ref(tenant: TenantProfile) -> dict[str, Any] | None:
    owner = tenant.pyme or tenant.municipio
    if not owner:
        return None
    return {
        "id": owner.id,
        "name": owner.name,
        "email": owner.email,
        "role": owner.rol,
        "tenant_slug": owner.tenant_slug,
    }


def _employee_scope(emp: User) -> dict[str, list[str]]:
    data = emp.accesibilidad if isinstance(emp.accesibilidad, dict) else {}
    scope = data.get("employee_scope") if isinstance(data.get("employee_scope"), dict) else {}
    return {
        "categorias": [str(v).strip() for v in scope.get("categorias", []) if str(v).strip()][:30],
        "zonas": [str(v).strip() for v in scope.get("zonas", []) if str(v).strip()][:30],
        "permisos": [str(v).strip() for v in scope.get("permisos", []) if str(v).strip()][:30],
        "channels": [str(v).strip() for v in scope.get("channels", []) if str(v).strip()][:30],
    }


def _ticket_extra(ticket: TenantTicket) -> dict[str, Any]:
    return ticket.datos_extra if isinstance(ticket.datos_extra, dict) else {}


def _ticket_status(ticket: TenantTicket) -> str:
    return str(ticket.estado or "").strip().lower()


def _ticket_channel(ticket: TenantTicket) -> str:
    extra = _ticket_extra(ticket)
    return str(extra.get("channel") or ticket.origen or "web").strip().lower()


def _open_tickets(tenant_id: int) -> list[TenantTicket]:
    tickets = TenantTicket.query.filter_by(tenant_id=tenant_id).all()
    return [ticket for ticket in tickets if _ticket_status(ticket) not in _CLOSED_TICKET_STATES]


def _employee_workload(tenant_id: int, employee_id: int) -> int:
    total = 0
    for ticket in _open_tickets(tenant_id):
        if str(_ticket_extra(ticket).get("assignee_id") or "") == str(employee_id):
            total += 1
    return total


def _coverage_items(tenant: TenantProfile) -> dict[str, Any]:
    employees = User.query.filter_by(tenant_id=tenant.id, es_empleado=True).order_by(User.id.asc()).all()
    ticket_snapshots = tenant_open_ticket_snapshots(tenant)
    workloads = workload_by_employee(tenant)
    supported_dimensions = tenant_operational_dimensions(tenant, ticket_snapshots)

    categories = sorted(set(supported_dimensions["categorias"]))
    channels = sorted(set(supported_dimensions["channels"]) | {item["channel"] for item in ticket_snapshots if item["channel"]})
    zones = sorted(set(supported_dimensions["zonas"]))

    category_map: dict[str, list[dict[str, Any]]] = {category: [] for category in categories}
    zone_map: dict[str, list[dict[str, Any]]] = {zone: [] for zone in zones}
    channel_map: dict[str, list[dict[str, Any]]] = {channel: [] for channel in channels}
    category_set = set(categories)
    zone_set = set(zones)
    channel_set = set(channels)
    employee_items = []

    for emp in employees:
        scope = _employee_scope(emp)
        coverage_score = 0
        if categories:
            coverage_score += len(set(scope["categorias"]) & set(categories)) / len(categories)
        if zones:
            coverage_score += len(set(scope["zonas"]) & set(zones)) / len(zones)
        if channels:
            coverage_score += len(set(scope["channels"]) & set(channels)) / len(channels)
        normalized_score = round(min(100.0, (coverage_score / max(1, bool(categories) + bool(zones) + bool(channels))) * 100), 2)

        ref = {"employee_id": emp.id, "name": emp.name, "email": emp.email}
        for category in scope["categorias"]:
            if category_set and category not in category_set:
                continue
            category_map.setdefault(category, []).append(ref)
        for zone in scope["zonas"]:
            if zone_set and zone not in zone_set:
                continue
            zone_map.setdefault(zone, []).append(ref)
        for channel in scope["channels"]:
            if channel_set and channel not in channel_set:
                continue
            channel_map.setdefault(channel, []).append(ref)

        employee_items.append(
            {
                "employee_id": emp.id,
                "id": emp.id,
                "name": emp.name,
                "email": emp.email,
                "roles": [getattr(emp, "rol", None)] if getattr(emp, "rol", None) else [],
                "scope": scope,
                "workload_open_tickets": workloads.get(emp.id, 0),
                "coverage_score": normalized_score,
            }
        )

    uncovered_categories = [key for key, value in category_map.items() if not value]
    uncovered_zones = [key for key, value in zone_map.items() if not value]
    uncovered_channels = [key for key, value in channel_map.items() if not value]
    total_dimensions = len(category_map) + len(zone_map) + len(channel_map)
    covered_dimensions = sum(1 for value in category_map.values() if value) + sum(1 for value in zone_map.values() if value) + sum(
        1 for value in channel_map.values() if value
    )
    coverage_rate = round((covered_dimensions / total_dimensions) * 100, 2) if total_dimensions else 100.0

    alerts = []
    if not employees:
        alerts.append({"severity": "high", "reason_code": "no_employees", "message": "No hay empleados activos para cubrir tickets."})
    if uncovered_categories:
        alerts.append({"severity": "medium", "reason_code": "uncovered_categories", "items": uncovered_categories})
    if uncovered_channels:
        alerts.append({"severity": "medium", "reason_code": "uncovered_channels", "items": uncovered_channels})

    return {
        "employees": employee_items,
        "coverage": {
            "categorias": category_map,
            "zonas": zone_map,
            "channels": channel_map,
            "dimension_sources": supported_dimensions.get("sources") or {},
            "uncovered_categories": uncovered_categories,
            "uncovered_zones": uncovered_zones,
            "uncovered_channels": uncovered_channels,
        },
        "summary": {
            "employees": len(employees),
            "open_tickets": len(ticket_snapshots),
            "coverage_rate": coverage_rate,
            "covered_dimensions": covered_dimensions,
            "total_dimensions": total_dimensions,
            "alerts_count": len(alerts),
        },
        "alerts": alerts,
    }


def _notification_delivery_status(tenant_id: int, period_days: int = 7) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(days=period_days)
    rows = (
        db.session.query(Notification.channel, Notification.status, func.count(Notification.id))
        .filter(Notification.tenant_id == tenant_id)
        .filter(Notification.created_at >= since)
        .group_by(Notification.channel, Notification.status)
        .all()
    )
    by_channel: dict[str, dict[str, int]] = {}
    totals = {"queued": 0, "sent": 0, "failed": 0, "delayed": 0, "sending": 0}
    for channel, status, count in rows:
        channel_key = str(channel or "unknown")
        status_key = str(status or "unknown")
        amount = int(count or 0)
        by_channel.setdefault(channel_key, {"queued": 0, "sent": 0, "failed": 0, "delayed": 0, "sending": 0})
        by_channel[channel_key][status_key] = by_channel[channel_key].get(status_key, 0) + amount
        totals[status_key] = totals.get(status_key, 0) + amount

    delivered = totals.get("sent", 0)
    failed = totals.get("failed", 0)
    denominator = delivered + failed
    success_rate = round((delivered / denominator) * 100, 2) if denominator else 100.0
    return {
        "period_days": period_days,
        "since": since.isoformat(),
        "totals": {**totals, "success_rate": success_rate},
        "by_channel": by_channel,
    }


def _tenant_health_payload(tenant: TenantProfile) -> dict[str, Any]:
    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    open_tickets = _open_tickets(tenant.id)
    overdue_tickets = [ticket for ticket in open_tickets if is_ticket_overdue(ticket) or _ticket_status(ticket) in {"vencido", "overdue"}]
    employees_count = User.query.filter_by(tenant_id=tenant.id, es_empleado=True).count()
    notification_status = _notification_delivery_status(tenant.id)

    checks = {
        "active": bool(getattr(tenant, "is_active", True)),
        "widget_configured": bool(tenant.widget_config or tenant.widget_settings or cfg.get("widget_tokens")),
        "whatsapp_configured": bool(tenant.whatsapp_sender_id or cfg.get("whatsapp_sender_id")),
        "team_configured": employees_count > 0,
        "notifications_healthy": float((notification_status.get("totals") or {}).get("success_rate", 100.0)) >= 90.0,
        "sla_healthy": len(overdue_tickets) == 0,
    }
    score = round((sum(1 for ok in checks.values() if ok) / len(checks)) * 100, 2)
    status = "healthy" if score >= 85 else "warning" if score >= 60 else "critical"
    alerts = []
    for key, ok in checks.items():
        if not ok:
            alerts.append({"severity": "high" if key in {"active", "sla_healthy"} else "medium", "reason_code": key, "message": f"Check pendiente: {key}"})

    return {
        "contract_version": "tenant.health.v1",
        "tenant": _tenant_ref(tenant),
        "health": {
            "score": score,
            "status": status,
            "checks": checks,
            "last_checked_at": datetime.now(timezone.utc).isoformat(),
        },
        "metrics": {
            "open_tickets": len(open_tickets),
            "overdue_tickets": len(overdue_tickets),
            "employees": employees_count,
            "notification_success_rate": notification_status["totals"]["success_rate"],
        },
        "integrations": {
            "widget": checks["widget_configured"],
            "whatsapp": checks["whatsapp_configured"],
            "payments": bool(cfg.get("mercadopago_access_token")),
            "notifications": checks["notifications_healthy"],
        },
        "queues": {
            "notifications_queued": notification_status["totals"].get("queued", 0),
            "notifications_delayed": notification_status["totals"].get("delayed", 0),
            "tickets_open": len(open_tickets),
        },
        "errors_recent": {
            "notifications_failed": notification_status["totals"].get("failed", 0),
            "tickets_overdue": len(overdue_tickets),
        },
        "alerts": alerts,
        "recommended_actions": [
            {"kind": alert["reason_code"], "priority": alert["severity"], "message": alert["message"]}
            for alert in alerts[:5]
        ],
    }


def _safe_count(query) -> int:
    try:
        return int(query.count() or 0)
    except Exception:
        return 0


def _survey_ops_summary(tenant: TenantProfile) -> dict[str, Any]:
    encuestas_query = EncEncuesta.query.filter_by(tenant_id=tenant.id)
    survey_count = _safe_count(encuestas_query)
    encuestas = encuestas_query.order_by(EncEncuesta.id.asc()).limit(12).all()
    public_survey_count = _safe_count(PublicSurvey.query.filter_by(tenant_id=tenant.id))
    public_responses = _safe_count(
        PublicSurveyResponse.query.join(
            PublicSurvey,
            PublicSurveyResponse.survey_id == PublicSurvey.id,
        ).filter(PublicSurvey.tenant_id == tenant.id)
    )

    legacy_response_query = EncRespuesta.query.filter_by(tenant_id=tenant.id)
    legacy_responses = _safe_count(
        legacy_response_query.filter(
            EncRespuesta.response_origin == SURVEY_RESPONSE_ORIGIN_REAL
        )
    )
    synthetic_response_count = _safe_count(
        legacy_response_query.filter(
            EncRespuesta.response_origin == SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO
        )
    )
    unverified_response_count = _safe_count(
        legacy_response_query.filter(
            EncRespuesta.response_origin
            == SURVEY_RESPONSE_ORIGIN_LEGACY_UNVERIFIED
        )
    )

    live_vote_predicate = or_(
        EncEncuesta.es_votacion_envivo.is_(True),
        func.lower(func.coalesce(EncEncuesta.tipo, "")).like("%vot%"),
        func.lower(func.coalesce(EncEncuesta.titulo, "")).like("%vot%"),
    )
    live_vote_count = _safe_count(encuestas_query.filter(live_vote_predicate))
    active_count = _safe_count(
        encuestas_query.filter(
            func.lower(func.coalesce(EncEncuesta.estado, "")).in_(
                {"publicada", "activa", "active", "published"}
            )
        )
    )

    def is_live_vote(encuesta: EncEncuesta) -> bool:
        return (
            bool(getattr(encuesta, "es_votacion_envivo", False))
            or "vot" in str(getattr(encuesta, "tipo", "") or "").lower()
            or "vot" in str(getattr(encuesta, "titulo", "") or "").lower()
        )

    return {
        "contract_version": "tenant.surveys_ops.v1",
        "summary": {
            "surveys": survey_count,
            "public_surveys": public_survey_count,
            "active": active_count,
            "live_votes": live_vote_count,
            "responses": legacy_responses + public_responses,
            "public_responses": public_responses,
        },
        "response_provenance": build_survey_response_provenance(
            real_count=legacy_responses + public_responses,
            synthetic_count=synthetic_response_count,
            unverified_count=unverified_response_count,
            mode="real",
        ),
        "items": [
            {
                "id": encuesta.id,
                "slug": encuesta.slug,
                "title": encuesta.titulo,
                "type": encuesta.tipo,
                "status": encuesta.estado,
                "is_live_vote": is_live_vote(encuesta),
                "show_live_results": bool(getattr(encuesta, "mostrar_resultados_envivo", False)),
            }
            for encuesta in encuestas
        ],
        "endpoints": {
            "admin": "/api/v2/surveys",
            "analytics": "/api/v2/analytics/operations/dashboard",
            "draft": "/api/v2/surveys/draft",
        },
    }


def _marketplace_ops_summary(tenant: TenantProfile) -> dict[str, Any]:
    products_query = CatalogoItem.query.filter_by(tenant_id=tenant.id)
    products_count = _safe_count(products_query)
    with_images = _safe_count(CatalogoItem.query.filter(CatalogoItem.tenant_id == tenant.id, CatalogoItem.imagen_url.isnot(None)))
    quality = build_catalog_quality_payload(tenant, limit=8)
    quality_summary = quality.get("summary") or {}
    latest_imports = ((quality.get("imports") or {}).get("latest") or [])
    latest_import_status = (latest_imports[0] or {}).get("status") if latest_imports else None
    products_missing_images = max(0, products_count - with_images)
    try:
        orders_count = MarketOrder.legacy_safe_count(tenant_id=tenant.id)
        pending_orders = MarketOrder.legacy_safe_count(MarketOrder.status.in_(["pending", "created", "confirmed"]), tenant_id=tenant.id)
    except Exception:
        orders_count = 0
        pending_orders = 0

    return {
        "contract_version": "tenant.marketplace_ops.v1",
        "summary": {
            "products": products_count,
            "with_images": with_images,
            "missing_images": products_missing_images,
            "products_without_image": products_missing_images,
            "products_with_images": with_images,
            "products_missing_images": products_missing_images,
            "image_coverage_rate": round((with_images / products_count) * 100, 2) if products_count else 100.0,
            "ready_to_sell": quality_summary.get("ready_to_sell", 0),
            "missing_price": quality_summary.get("missing_price", 0),
            "missing_stock": quality_summary.get("missing_stock", 0),
            "low_stock": quality_summary.get("low_stock", 0),
            "out_of_stock": quality_summary.get("out_of_stock", 0),
            "ready_rate": quality_summary.get("ready_rate", 100.0),
            "orders": orders_count,
            "pending_orders": pending_orders,
            "bulk_import_status": latest_import_status or "idle",
        },
        "quality": {
            "contract_version": quality.get("contract_version"),
            "summary": quality_summary,
            "queues": quality.get("queues"),
            "endpoint": "/api/v2/catalog/quality",
        },
        "inventory": quality.get("inventory"),
        "media_capabilities": {
            "product_images": True,
            "product_gallery": True,
            "bulk_import": ["csv", "xlsx", "txt", "pdf"],
            "stock_only_import": ["csv", "xlsx"],
            "image_extraction_from_import": True,
            "manual_image_upload": True,
            "pdf_catalog_generation": True,
        },
        "endpoints": {
            "items": f"/api/admin/tenants/{tenant.slug}/catalog/items",
            "catalog": f"/api/admin/tenants/{tenant.slug}/catalog",
            "catalog_quality": "/api/v2/catalog/quality",
            "bulk_import": "/api/admin/catalogo/importar",
            "bulk_import_v2": "/api/admin/catalog/import",
            "stock_only_import_v2": "/api/admin/catalog/import",
            "orders": f"/api/admin/tenants/{tenant.slug}/orders",
        },
    }


def _ticket_item_from_tenant(ticket: TenantTicket, source: str = "tenant_ticket") -> dict[str, Any]:
    extra = _ticket_extra(ticket)
    status = ticket.estado
    intent = extra.get("intent") or extra.get("action") or extra.get("lead_intent") or ticket.categoria
    return {
        "source": source,
        "id": ticket.id,
        "ticket_id": ticket.id,
        "title": extra.get("title") or ticket.categoria or f"Ticket {ticket.id}",
        "status": status,
        "stage": extra.get("lead_stage") or status,
        "channel": _ticket_channel(ticket),
        "category": ticket.categoria,
        "intent": intent,
        "next_action": extra.get("next_action") or ("assign_or_reply" if str(status or "").lower() not in _CLOSED_TICKET_STATES else "view_history"),
        "origin": ticket.origen,
        "contact": extra.get("contact") if isinstance(extra.get("contact"), dict) else {},
        "location": {"lat": ticket.latitud, "lng": ticket.longitud, "address": extra.get("address")},
        "created_at": _iso(ticket.created_at),
        "updated_at": _iso(ticket.updated_at),
    }


def _ticket_item_from_legacy(ticket: Any, source: str) -> dict[str, Any]:
    created_at = getattr(ticket, "fecha", None)
    updated_at = getattr(ticket, "ultima_actividad", None) or created_at
    status = getattr(ticket, "estado", None)
    category = getattr(ticket, "categoria", None)
    ticket_id = getattr(ticket, "id", None)
    return {
        "source": source,
        "id": ticket_id,
        "ticket_id": ticket_id,
        "title": getattr(ticket, "asunto", None) or category or f"Ticket {getattr(ticket, 'id', '')}",
        "status": status,
        "stage": status,
        "channel": getattr(ticket, "canal_ingreso", None) or "web",
        "category": category,
        "intent": category or source,
        "next_action": "assign_or_reply" if str(status or "").lower() not in _CLOSED_TICKET_STATES else "view_history",
        "origin": source,
        "contact": {
            "name": getattr(ticket, "nombre_vecino", None) or getattr(ticket, "nombre_cliente", None),
            "phone": getattr(ticket, "telefono", None),
            "email": getattr(ticket, "email", None),
        },
        "location": {
            "lat": getattr(ticket, "latitud", None),
            "lng": getattr(ticket, "longitud", None),
            "address": getattr(ticket, "direccion", None),
        },
        "created_at": _iso(created_at),
        "updated_at": _iso(updated_at),
    }


def _tenant_lead_capture_summary(
    tenant: TenantProfile,
    limit: int = 20,
    *,
    viewer: User | None,
) -> dict[str, Any]:
    tenant_query = apply_employee_ticket_category_scope(
        TenantTicket.query.filter_by(tenant_id=tenant.id),
        viewer,
        TenantTicket,
    )
    municipio_query = apply_employee_ticket_category_scope(
        MunicipioTicket.query.filter_by(tenant_id=tenant.id),
        viewer,
        MunicipioTicket,
    )
    pyme_query = apply_employee_ticket_category_scope(
        PymeTicket.query.filter_by(tenant_id=tenant.id),
        viewer,
        PymeTicket,
    )
    tenant_rows = (
        tenant_query.order_by(TenantTicket.updated_at.desc()).limit(limit).all()
    )
    municipio_rows = (
        municipio_query.order_by(MunicipioTicket.fecha.desc()).limit(limit).all()
    )
    pyme_rows = pyme_query.order_by(PymeTicket.fecha.desc()).limit(limit).all()

    items = [_ticket_item_from_tenant(ticket) for ticket in tenant_rows]
    items.extend(_ticket_item_from_legacy(ticket, "municipio_ticket") for ticket in municipio_rows)
    items.extend(_ticket_item_from_legacy(ticket, "pyme_ticket") for ticket in pyme_rows)
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    items = items[:limit]

    open_items = [item for item in items if str(item.get("status") or "").lower() not in _CLOSED_TICKET_STATES]
    demo_items = [
        item
        for item in items
        if str(item.get("origin") or "").lower() in {"demo", "landing", "widget", "pwa"}
        or str(item.get("channel") or "").lower() in {"widget", "web", "landing"}
    ]
    return {
        "contract_version": "tenant.lead_capture.v1",
        "summary": {
            "total_recent": len(items),
            "open": len(open_items),
            "demo_or_widget": len(demo_items),
            "channels": sorted({str(item.get("channel") or "unknown") for item in items}),
        },
        "items": items,
        "endpoints": {
            "tenant_leads": f"/api/admin/tenants/{tenant.slug}/leads",
            "omnichannel_inbox": "/api/v2/inbox/omnichannel",
            "tickets": "/api/v2/tickets",
        },
    }


def _tenant_readiness_payload(tenant: TenantProfile, *, marketplace: dict[str, Any], health: dict[str, Any]) -> dict[str, Any]:
    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    market_summary = marketplace.get("summary") or {}
    checks = {
        "profile": bool(tenant.nombre and tenant.slug and tenant.tipo),
        "branding": bool(tenant.logo_url or tenant.tema or tenant.theme_json),
        "widget": bool(tenant.widget_settings or tenant.widget_config or cfg.get("widget_tokens")),
        "whatsapp": bool(tenant.whatsapp_sender_id or cfg.get("whatsapp_sender_id")),
        "team": User.query.filter_by(tenant_id=tenant.id, es_empleado=True).count() > 0,
        "catalog": int(market_summary.get("products") or 0) > 0,
        "surveys": EncEncuesta.query.filter_by(tenant_id=tenant.id).count() > 0 or PublicSurvey.query.filter_by(tenant_id=tenant.id).count() > 0,
        "sla": int((health.get("metrics") or {}).get("overdue_tickets") or 0) == 0,
    }
    completed = sum(1 for ok in checks.values() if ok)
    score = round((completed / len(checks)) * 100, 2)
    return {
        "contract_version": "tenant.readiness.v1",
        "score": score,
        "completed": completed,
        "total": len(checks),
        "checks": checks,
        "missing": [key for key, ok in checks.items() if not ok],
    }


def _admin_modules_payload(tenant: TenantProfile, *, education_profile: dict[str, Any]) -> list[dict[str, Any]]:
    base = f"/t/{tenant.slug}"
    modules = [
        {
            "id": "profile",
            "label": "Perfil operativo",
            "route": f"{base}/profile",
            "endpoint": "/api/v2/tenant/admin-experience",
            "widgets": ["readiness", "branding", "capabilities", "integrations"],
        },
        {
            "id": "inbox",
            "label": "Inbox omnicanal",
            "route": f"{base}/inbox",
            "endpoint": "/api/v2/inbox/omnichannel",
            "widgets": ["tickets", "timeline", "presence", "handoff"],
        },
        {
            "id": "analytics",
            "label": "Metricas y mapas",
            "route": f"{base}/analytics",
            "endpoint": "/api/v2/analytics/operations/dashboard",
            "secondary_endpoints": ["/api/v2/analytics/operations/heatmap", "/api/v2/analytics/operations/freshness"],
            "widgets": ["kpis", "heatmap", "trends", "action_center"],
        },
        {
            "id": "surveys_votings",
            "label": "Encuestas y votaciones",
            "route": f"{base}/surveys",
            "endpoint": "/api/v2/surveys",
            "secondary_endpoints": ["/api/v2/surveys/draft"],
            "widgets": ["public_surveys", "live_votes", "responses", "comments"],
        },
        {
            "id": "employees",
            "label": "Equipo y cobertura",
            "route": f"{base}/employees",
            "endpoint": "/api/v2/employee-coverage",
            "secondary_endpoints": ["/api/v2/employee-routing", "/api/v2/employees/{employee_id}/routing-scope"],
            "widgets": ["coverage", "workload", "routing_rules", "assignment"],
        },
        {
            "id": "marketplace",
            "label": "Marketplace y catalogo",
            "route": f"{base}/marketplace",
            "endpoint": f"/api/admin/tenants/{tenant.slug}/catalog/items",
            "secondary_endpoints": ["/api/v2/catalog/quality", "/api/admin/catalogo/importar", "/api/admin/catalog/import", f"/api/admin/tenants/{tenant.slug}/orders"],
            "widgets": ["catalog_quality", "bulk_import", "image_coverage", "orders", "pdf_catalog"],
        },
        {
            "id": "transactions",
            "label": "Transacciones",
            "route": f"{base}/transactions",
            "endpoint": "/api/v2/whatsapp/experience",
            "secondary_endpoints": [
                "/api/v2/payments/checkout-status",
                f"/api/v2/tenants/{tenant.slug}/payments/status",
                f"/api/v2/tenants/{tenant.slug}/payments/checkout-session",
            ],
            "widgets": ["finance_flows", "secure_checkout", "kyc", "collections", "signature", "audit_trail"],
        },
        {
            "id": "widget_whatsapp",
            "label": "Widget/WhatsApp/Voz",
            "route": f"{base}/channels",
            "endpoint": "/api/v2/whatsapp/experience",
            "secondary_endpoints": [
                f"/api/public/tenants/{tenant.slug}/widget-config",
                "/api/public/realtime/voice-capabilities",
                "/api/v2/notifications/hooks",
            ],
            "widgets": ["channel_health", "quick_menu", "media_capabilities", "realtime_voice", "tracking", "notifications"],
        },
    ]
    if education_profile.get("is_education"):
        modules.append(
            {
                "id": "education",
                "label": "Operacion colegio",
                "route": f"{base}/educacion",
                "endpoint": "/api/v1/education/admin/menu",
                "secondary_endpoints": ["/api/v1/education/operations/summary", "/api/v1/education/cases?envelope=1"],
                "widgets": ["family_context", "school_cases", "attendance", "communications"],
            }
        )
    for module in modules:
        module.setdefault("audience", "admin")
        module.setdefault("secondary_endpoints", [])
        module.setdefault("widgets", [])
    return modules


def _admin_navigation_payload(tenant: TenantProfile, modules: list[dict[str, Any]]) -> dict[str, Any]:
    primary = [
        {
            "id": module.get("id"),
            "label": module.get("label"),
            "route": module.get("route"),
            "endpoint": module.get("endpoint"),
            "audience": module.get("audience") or "admin",
            "visible": True,
        }
        for module in modules
        if module.get("id") in {"profile", "inbox", "analytics", "surveys_votings", "employees", "marketplace", "transactions", "widget_whatsapp"}
    ]
    base = f"/t/{tenant.slug}"
    return {
        "contract_version": "tenant.admin_navigation.v1",
        "primary": primary,
        "quick_actions": [
            {
                "id": "open_operations",
                "label": "Tablero",
                "route": f"{base}/analytics",
                "endpoint": "/api/v2/analytics/operations/dashboard",
                "icon": "layout-dashboard",
                "visible": True,
            },
            {
                "id": "open_heatmap",
                "label": "Mapa",
                "route": f"{base}/analytics?view=heatmap",
                "endpoint": "/api/v2/analytics/operations/heatmap",
                "icon": "map",
                "visible": True,
            },
            {
                "id": "open_surveys",
                "label": "Encuestas",
                "route": f"{base}/surveys",
                "admin_route": f"/admin/encuestas?tenant_slug={tenant.slug}",
                "endpoint": "/api/v2/surveys",
                "icon": "clipboard-list",
                "visible": True,
            },
            {
                "id": "open_employees",
                "label": "Equipo",
                "route": f"{base}/employees",
                "endpoint": "/api/v2/employee-routing",
                "icon": "users",
                "visible": True,
            },
            {
                "id": "open_inbox",
                "label": "Reclamos",
                "route": f"{base}/inbox",
                "endpoint": "/api/v2/inbox/omnichannel",
                "icon": "inbox",
                "visible": True,
            },
        ],
        "inbox_tabs": [
            {"id": "open", "label": "Abiertos", "visible": True},
            {"id": "assigned", "label": "Asignados", "visible": True},
            {"id": "unassigned", "label": "Sin asignar", "visible": True},
            {"id": "resolved", "label": "Resueltos", "visible": True},
        ],
        "hide_legacy_tabs": ["workspace", "live_bridge", "templates"],
    }


def _admin_panel_widgets_payload(tenant: TenantProfile, *, dashboard: dict[str, Any], employee_routing: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_version": "tenant.admin_panel_widgets.v1",
        "layout": "operational_command_center",
        "hero_widgets": [
            {
                "id": "operations_summary",
                "label": "Resumen operativo",
                "render_as": "kpi_summary",
                "endpoint": "/api/v2/analytics/operations/dashboard",
                "data": dashboard.get("summary") or dashboard.get("metrics") or {},
                "empty_state": "Todavia no hay actividad suficiente para calcular el resumen.",
            },
            {
                "id": "heatmap_summary",
                "label": "Mapa de calor",
                "render_as": "heatmap_preview",
                "endpoint": "/api/v2/analytics/operations/heatmap",
                "can_render_heatmap": True,
                "empty_state": "Compartiendo ubicaciones o reclamos con direccion se activa este mapa.",
            },
            {
                "id": "location_widget",
                "label": "Ubicacion y cobertura",
                "render_as": "location_coverage",
                "endpoint": "/api/v2/analytics/operations/heatmap",
                "map_style_endpoint": "/api/map/config",
                "empty_state": "Configura zonas o genera reclamos con ubicacion para ver cobertura territorial.",
            },
            {
                "id": "employee_assignment",
                "label": "Equipo y asignacion",
                "render_as": "assignment_queue",
                "endpoint": "/api/v2/employee-routing",
                "summary": {
                    "employees": len(employee_routing.get("employees") or []),
                    "unassigned": ((employee_routing.get("queues") or {}).get("unassigned_count") or 0),
                    "categories": len((employee_routing.get("routing_rules") or {}).get("categories") or []),
                },
                "empty_state": "Carga empleados y reglas por categoria para asignar tickets sin ruido.",
            },
        ],
        "ticket_workspace": {
            "render_as": "focused_ticket_board",
            "primary_actions": ["review_unassigned", "auto_assign", "assign_by_category", "change_status", "handoff_human"],
            "employees_endpoint": "/api/v2/employee-routing",
            "routing_scope_endpoint": "/api/v2/employees/{employee_id}/routing-scope",
            "auto_assign_endpoint": "/api/v2/employee-routing/auto-assign",
            "recommended_filters": ["estado", "categoria", "asignado_a", "canal", "prioridad"],
        },
    }


def _build_tenant_admin_experience_payload(
    tenant: TenantProfile,
    *,
    start_date: datetime,
    end_date: datetime,
    app_config: Mapping[str, Any] | None = None,
    viewer: User | None,
) -> dict[str, Any]:
    health = _tenant_health_payload(tenant)
    dashboard = build_operational_dashboard(tenant, start_date, end_date, viewer=viewer)
    freshness = build_operational_freshness(
        tenant,
        start_date,
        end_date,
        viewer=viewer,
    )
    marketplace = _marketplace_ops_summary(tenant)
    surveys = _survey_ops_summary(tenant)
    lead_capture = _tenant_lead_capture_summary(tenant, viewer=viewer)
    education_profile = build_education_profile(tenant)
    readiness = _tenant_readiness_payload(tenant, marketplace=marketplace, health=health)
    whatsapp = build_whatsapp_experience(tenant, app_config=app_config)
    employee_routing = build_employee_routing_payload(tenant)
    modules = _admin_modules_payload(tenant, education_profile=education_profile)

    payload = {
        "contract_version": "tenant.admin_experience.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": {"from": _iso(start_date), "to": _iso(end_date)},
        "tenant": _tenant_ref(tenant),
        "owner": _tenant_owner_ref(tenant),
        "profile": {
            "display_name": tenant.nombre,
            "status": "active" if getattr(tenant, "is_active", True) else "inactive",
            "tipo": tenant.tipo,
            "vertical": "educacion" if is_education_tenant(tenant) else (tenant.vertical or tenant.tipo),
            "subvertical": tenant.subvertical,
            "plan": tenant.plan,
            "domain": tenant.dominio,
            "logo_url": tenant.logo_url,
            "theme_config": tenant.get_theme_config() if hasattr(tenant, "get_theme_config") else {},
            "readiness": readiness,
        },
        "modules": modules,
        "navigation": _admin_navigation_payload(tenant, modules),
        "admin_panel_widgets": _admin_panel_widgets_payload(
            tenant,
            dashboard=dashboard,
            employee_routing=employee_routing,
        ),
        "health": health,
        "operations": {
            "dashboard": dashboard,
            "freshness": freshness,
        },
        "lead_capture": lead_capture,
        "surveys_votings": surveys,
        "marketplace": marketplace,
        "employee_routing": {
            "contract_version": employee_routing.get("contract_version"),
            "summary": {
                "employees": len(employee_routing.get("employees") or []),
                "unassigned": ((employee_routing.get("queues") or {}).get("unassigned_count") or 0),
                "recommendations": len(employee_routing.get("recommendations") or []),
            },
            "endpoint": "/api/v2/employee-routing",
        },
        "whatsapp": {
            "contract_version": whatsapp.get("contract_version"),
            "channel": whatsapp.get("channel"),
            "conversation_intelligence": whatsapp.get("conversation_intelligence"),
            "tracking": whatsapp.get("tracking"),
            "content_modules": whatsapp.get("content_modules"),
            "endpoint": "/api/v2/whatsapp/experience",
        },
        "education": {
            "profile": education_profile,
            "admin_menu": build_education_admin_menu(tenant) if education_profile.get("is_education") else None,
        },
        "frontend_contract": {
            "render_as": "tenant_admin_operating_system",
            "primary_refresh_seconds": 30,
            "recommended_views": [
                "profile_header",
                "operations_summary",
                "inbox_board",
                "heatmap",
                "surveys_votings",
                "marketplace_catalog_quality",
                "whatsapp_operations_hub",
                "employee_coverage",
                "e2e_flow_readiness",
            ],
            "empty_state_behavior": "show_module_readiness_and_next_best_actions",
            "show_e2e_flow_readiness": True,
        },
    }
    payload["e2e_flow_readiness"] = _build_production_e2e_readiness(
        tenant=tenant,
        admin_payload=payload,
        marketplace=marketplace,
        whatsapp=whatsapp,
    )
    return payload


def _ops_qa_check_result(
    *,
    check_id: str,
    label: str,
    ok: bool,
    status: str | None = None,
    severity: str = "warning",
    details: Mapping[str, Any] | None = None,
    next_action: str | None = None,
    endpoint: str | None = None,
) -> dict[str, Any]:
    resolved_status = status or ("pass" if ok else severity)
    return {
        "id": check_id,
        "label": label,
        "ok": bool(ok),
        "status": resolved_status,
        "severity": "info" if ok else severity,
        "endpoint": endpoint,
        "details": dict(details or {}),
        "next_action": next_action or ("continue" if ok else "review_configuration"),
    }


def _whatsapp_qa_script_matrix_contract() -> dict[str, Any]:
    contract: dict[str, Any] = {
        "contract_version": "whatsapp.qa_script_matrix.v1",
        "source": "scripts/qa_whatsapp_flows.py",
        "local_command": "python scripts/qa_whatsapp_flows.py",
        "safe_by_default": True,
        "uses_fake_twilio": True,
        "sends_real_message": False,
        "loaded": False,
        "summary": {"scenarios": 0, "cases": 0},
        "scenarios": [],
    }
    try:
        module = ast.parse(_WHATSAPP_QA_SCRIPT.read_text(encoding="utf-8"), filename=str(_WHATSAPP_QA_SCRIPT))
        scenarios: dict[str, list[str]] = {}
        for node in module.body:
            if not isinstance(node, ast.Assign):
                continue
            target_names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if "QA_SCENARIOS" not in target_names:
                continue
            raw_value = ast.literal_eval(node.value)
            scenarios = {
                str(scenario_id): [str(case_id) for case_id in case_ids]
                for scenario_id, case_ids in raw_value.items()
                if isinstance(case_ids, list)
            }
            break
        if not scenarios:
            raise ValueError("QA_SCENARIOS not found")
        items = [
            {"id": scenario_id, "case_count": len(case_ids), "cases": case_ids}
            for scenario_id, case_ids in sorted(scenarios.items())
        ]
        case_count = sum(item["case_count"] for item in items)
        contract.update(
            {
                "loaded": True,
                "summary": {
                    "scenarios": len(items),
                    "cases": case_count,
                    "municipal_flows": len([item for item in items if item["id"].startswith("gov_")]),
                    "commerce_flows": len([item for item in items if "catalog" in item["id"] or "order" in item["id"]]),
                    "finance_flows": len([item for item in items if item["id"].startswith("finance_")]),
                },
                "scenarios": items,
            }
        )
    except Exception as exc:
        contract["error"] = {"reason_code": "qa_script_matrix_unavailable", "detail": str(exc)}
    return contract


def _build_tenant_ops_qa_playbook(
    tenant: TenantProfile,
    *,
    start_date: datetime,
    end_date: datetime,
    app_config: Mapping[str, Any] | None = None,
    viewer: User | None,
) -> dict[str, Any]:
    admin = _build_tenant_admin_experience_payload(
        tenant,
        start_date=start_date,
        end_date=end_date,
        app_config=app_config,
        viewer=viewer,
    )
    modules = {str(item.get("id")): item for item in admin.get("modules", []) if isinstance(item, Mapping)}
    operations = admin.get("operations") if isinstance(admin.get("operations"), Mapping) else {}
    freshness = operations.get("freshness") if isinstance(operations.get("freshness"), Mapping) else {}
    freshness_summary = freshness.get("summary") if isinstance(freshness.get("summary"), Mapping) else {}
    lead_capture = admin.get("lead_capture") if isinstance(admin.get("lead_capture"), Mapping) else {}
    lead_summary = lead_capture.get("summary") if isinstance(lead_capture.get("summary"), Mapping) else {}
    marketplace = admin.get("marketplace") if isinstance(admin.get("marketplace"), Mapping) else {}
    market_summary = marketplace.get("summary") if isinstance(marketplace.get("summary"), Mapping) else {}
    surveys = admin.get("surveys_votings") if isinstance(admin.get("surveys_votings"), Mapping) else {}
    survey_summary = surveys.get("summary") if isinstance(surveys.get("summary"), Mapping) else {}
    employee_routing = build_employee_routing_payload(tenant)
    employee_summary = admin.get("employee_routing", {}).get("summary", {}) if isinstance(admin.get("employee_routing"), Mapping) else {}
    whatsapp = build_whatsapp_experience(tenant, app_config=app_config)
    whatsapp_templates = (whatsapp.get("template_blueprint") or {}).get("registry_summary") if isinstance(whatsapp.get("template_blueprint"), Mapping) else {}
    whatsapp_webviews = (whatsapp.get("webview_blueprint") or {}).get("summary") if isinstance(whatsapp.get("webview_blueprint"), Mapping) else {}
    finance_runtime = whatsapp.get("finance_transactional") if isinstance(whatsapp.get("finance_transactional"), Mapping) else {}
    finance_summary = finance_runtime.get("summary") if isinstance(finance_runtime.get("summary"), Mapping) else {}
    hf_runtime = (((whatsapp.get("conversation_intelligence") or {}).get("huggingface_ai") or {}) if isinstance(whatsapp.get("conversation_intelligence"), Mapping) else {})

    checks = [
        _ops_qa_check_result(
            check_id="admin_os_contract",
            label="Panel del tenant",
            ok=admin.get("contract_version") == "tenant.admin_experience.v1"
            and {"inbox", "analytics", "transactions", "widget_whatsapp"}.issubset(set(modules.keys())),
            severity="critical",
            endpoint="/api/v2/tenant/admin-experience",
            next_action="fix_admin_experience_contract",
            details={
                "contract_version": admin.get("contract_version"),
                "modules": sorted(modules.keys()),
                "render_as": (admin.get("frontend_contract") or {}).get("render_as"),
            },
        ),
        _ops_qa_check_result(
            check_id="claims_inbox",
            label="Reclamos, tickets e inbox omnicanal",
            ok=int(lead_summary.get("open") or 0) >= 0 and "inbox" in modules,
            severity="critical",
            endpoint="/api/v2/inbox/omnichannel",
            next_action="review_inbox_contract_and_ticket_sources",
            details={
                "open": lead_summary.get("open"),
                "recent": lead_summary.get("total_recent"),
                "channels": lead_summary.get("channels"),
                "sample_count": len(lead_capture.get("items") or []),
            },
        ),
        _ops_qa_check_result(
            check_id="catalog_orders",
            label="Catalogo, pedidos y checkout conversacional",
            ok=int(market_summary.get("products") or 0) > 0
            and (whatsapp.get("commerce") or {}).get("checkout_experience", {}).get("ready") is True,
            severity="critical",
            endpoint="/api/v2/catalog/quality",
            next_action="complete_catalog_images_prices_stock_and_checkout",
            details={
                "products": market_summary.get("products"),
                "ready_to_sell": market_summary.get("ready_to_sell"),
                "missing_images": market_summary.get("missing_images"),
                "orders": market_summary.get("orders"),
                "checkout_ready": (whatsapp.get("commerce") or {}).get("checkout_experience", {}).get("ready"),
            },
        ),
        _ops_qa_check_result(
            check_id="survey_live_vote",
            label="Encuestas, votaciones y resultados en vivo",
            ok=int(survey_summary.get("live_votes") or 0) > 0 or int(survey_summary.get("active") or 0) > 0,
            severity="warning",
            endpoint="/api/v2/surveys",
            next_action="publish_survey_or_live_vote",
            details={
                "surveys": survey_summary.get("surveys"),
                "active": survey_summary.get("active"),
                "live_votes": survey_summary.get("live_votes"),
                "responses": survey_summary.get("responses"),
            },
        ),
        _ops_qa_check_result(
            check_id="heatmap_analytics",
            label="Metricas, mapas y heatmap territorial",
            ok=bool(freshness_summary.get("can_render_heatmap")),
            severity="warning",
            endpoint="/api/v2/analytics/operations/heatmap",
            next_action="collect_locations_or_geocode_ticket_addresses",
            details={
                "freshness_status": freshness.get("status"),
                "can_render_heatmap": freshness_summary.get("can_render_heatmap"),
                "dashboard_contract": (operations.get("dashboard") or {}).get("contract_version") if isinstance(operations.get("dashboard"), Mapping) else None,
            },
        ),
        _ops_qa_check_result(
            check_id="employee_routing",
            label="Ruteo, asignacion y cobertura del equipo",
            ok=int(employee_summary.get("employees") or 0) > 0
            and employee_routing.get("contract_version") == "employee.routing.v1",
            severity="warning",
            endpoint="/api/v2/employee-routing",
            next_action="load_employees_and_category_scope",
            details={
                "contract_version": employee_routing.get("contract_version"),
                "employees": employee_summary.get("employees"),
                "unassigned": employee_summary.get("unassigned"),
                "recommendations": employee_summary.get("recommendations"),
            },
        ),
        _ops_qa_check_result(
            check_id="whatsapp_templates_webviews",
            label="Plantillas WhatsApp, botones y webviews",
            ok=whatsapp.get("contract_version") == "whatsapp.experience.v1"
            and int((whatsapp_templates or {}).get("operational_webviews") or 0) > 0
            and int((whatsapp_webviews or {}).get("flows_total") or 0) > 0,
            severity="critical",
            endpoint="/api/v2/whatsapp/experience",
            next_action="sync_twilio_templates_and_validate_signed_webviews",
            details={
                "contract_version": whatsapp.get("contract_version"),
                "operational_catalog_total": (whatsapp_templates or {}).get("operational_catalog_total"),
                "operational_webviews": (whatsapp_templates or {}).get("operational_webviews"),
                "webview_flows_total": (whatsapp_webviews or {}).get("flows_total"),
                "qa_scenarios": (whatsapp.get("qa_playbook") or {}).get("scenario_count") if isinstance(whatsapp.get("qa_playbook"), Mapping) else None,
            },
        ),
        _ops_qa_check_result(
            check_id="transactional_finance_flows",
            label="Transacciones in-chat: alta, KYC, cobranza, pago y firma",
            ok="transactions" in modules
            and int((whatsapp_webviews or {}).get("flows_total") or 0) >= 2
            and "financial_services" in (((whatsapp.get("template_blueprint") or {}).get("operational_template_groups") or {}) if isinstance(whatsapp.get("template_blueprint"), Mapping) else {}),
            severity="critical",
            endpoint="/api/v2/whatsapp/experience",
            next_action="run_finance_onboarding_collection_signature_smoke",
            details={
                "module": "transactions" if "transactions" in modules else None,
                "templates_group": "financial_services",
                "webview_flows": ["finance_onboarding_kyc", "finance_credit_collection_signature"],
                "finance_runtime": finance_runtime.get("contract_version"),
                "finance_journeys": finance_summary.get("journeys"),
                "finance_ready_journeys": finance_summary.get("ready_journeys"),
                "confirmation_policy": "server_to_server_webhook",
            },
        ),
        _ops_qa_check_result(
            check_id="ai_runtime_multimodal",
            label="IA multimodal y fallback controlado",
            ok=bool((hf_runtime or {}).get("enabled")) and not bool((hf_runtime or {}).get("quota_depleted")),
            severity="warning",
            endpoint="/api/v2/whatsapp/experience",
            next_action="review_huggingface_runtime_or_fallback_provider",
            details={
                "enabled": (hf_runtime or {}).get("enabled"),
                "runtime_status": (hf_runtime or {}).get("runtime_status"),
                "quota_depleted": (hf_runtime or {}).get("quota_depleted"),
                "fallback_behavior": (hf_runtime or {}).get("fallback_behavior"),
            },
        ),
    ]
    passed = sum(1 for item in checks if item.get("ok"))
    critical_failed = [item for item in checks if item.get("severity") == "critical" and not item.get("ok")]
    status = "pass" if passed == len(checks) else ("fail" if critical_failed else "warning")
    e2e_readiness = _build_production_e2e_readiness(
        tenant=tenant,
        admin_payload=admin,
        marketplace=marketplace,
        whatsapp=whatsapp,
    )
    return {
        "contract_version": "tenant.ops_qa.playbook.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant": _tenant_ref(tenant),
        "period": {"from": _iso(start_date), "to": _iso(end_date)},
        "safe_by_default": True,
        "status": status,
        "score": round((passed / len(checks)) * 100, 2) if checks else 0,
        "summary": {
            "checks_total": len(checks),
            "passed": passed,
            "warnings": sum(1 for item in checks if item.get("status") == "warning"),
            "critical_failed": len(critical_failed),
            "real_messages_sent": 0,
            "e2e_flows_total": (e2e_readiness.get("summary") or {}).get("total"),
            "e2e_flows_ready": (e2e_readiness.get("summary") or {}).get("ready"),
        },
        "checks": checks,
        "e2e_flow_readiness": e2e_readiness,
        "recommended_next_actions": [item for item in checks if not item.get("ok")][:5],
        "execution": {
            "endpoint": f"/api/v2/tenants/{tenant.slug}/ops-qa/check/{{check_id}}",
            "method": "POST",
            "policy": "read_only_safe_checks_only",
            "real_message_policy": "blocked_in_this_playbook",
        },
        "frontend_contract": {
            "render_as": "tenant_ops_qa_command_center",
            "recommended_views": [
                "readiness_score",
                "critical_blockers",
                "safe_check_runner",
                "module_drilldown",
                "e2e_flow_matrix",
            ],
            "refresh_seconds": 30,
            "show_e2e_flow_readiness": True,
        },
    }


def _tenant_ops_qa_execution_result(
    *,
    tenant: TenantProfile,
    check: Mapping[str, Any],
    playbook: Mapping[str, Any],
) -> dict[str, Any]:
    details = dict(check.get("details") or {})
    check_id = str(check.get("id") or "")
    if check_id == "whatsapp_templates_webviews":
        matrix = _whatsapp_qa_script_matrix_contract()
        e2e = playbook.get("e2e_flow_readiness") if isinstance(playbook.get("e2e_flow_readiness"), Mapping) else {}
        flows = e2e.get("flows") if isinstance(e2e.get("flows"), list) else []
        matrix_scenarios = {
            str(item.get("id"))
            for item in matrix.get("scenarios", [])
            if isinstance(item, Mapping) and item.get("id")
        }
        e2e_scenarios = {
            str(item.get("qa_scenario_id"))
            for item in flows
            if isinstance(item, Mapping) and item.get("qa_scenario_id")
        }
        details["executable_matrix"] = matrix
        details["e2e_matrix_coverage"] = {
            "contract_version": "whatsapp.qa_e2e_matrix_coverage.v1",
            "e2e_flows": len(flows),
            "e2e_scenarios": len(e2e_scenarios),
            "covered_scenarios": sorted(e2e_scenarios.intersection(matrix_scenarios)),
            "missing_from_script": sorted(e2e_scenarios.difference(matrix_scenarios)),
            "safe_by_default": True,
        }
        details["runner"] = {
            "contract_version": "tenant.ops_qa.runner.v1",
            "local_command": matrix.get("local_command"),
            "sends_real_message": False,
            "requires_operator_confirmation_for_live_whatsapp": True,
        }
    return {
        "contract_version": "tenant.ops_qa.execution.v1",
        "tenant": _tenant_ref(tenant),
        "check_id": check_id,
        "label": check.get("label"),
        "ok": bool(check.get("ok")),
        "status": check.get("status"),
        "severity": check.get("severity"),
        "execution_mode": "read_only",
        "sends_real_message": False,
        "details": details,
        "next_action": check.get("next_action"),
        "playbook_status": playbook.get("status"),
        "playbook_score": playbook.get("score"),
    }


def _build_superadmin_command_center_payload(
    *,
    start_date: datetime,
    end_date: datetime,
    limit: int = 50,
    viewer: User | None,
) -> dict[str, Any]:
    tenants = TenantProfile.query.order_by(TenantProfile.created_at.desc()).limit(limit).all()
    tenant_items = []
    total_open = 0
    total_overdue = 0
    total_leads = 0
    total_health = 0.0

    for tenant in tenants:
        health = _tenant_health_payload(tenant)
        lead_capture = _tenant_lead_capture_summary(
            tenant,
            limit=5,
            viewer=viewer,
        )
        readiness = _tenant_readiness_payload(tenant, marketplace=_marketplace_ops_summary(tenant), health=health)
        tenant_ref = _tenant_ref(tenant)
        health_block = health.get("health") or {}
        metrics = health.get("metrics") or {}
        health_score = float(health_block.get("score") or 0)
        risk_reason = "none"
        if int(metrics.get("overdue_tickets") or 0) > 0:
            risk_reason = "overdue_tickets"
        elif str(health_block.get("status") or "") in {"warning", "critical"}:
            risk_reason = "tenant_health_warning"
        elif readiness.get("missing"):
            risk_reason = "readiness_incomplete"
        total_open += int(metrics.get("open_tickets") or 0)
        total_overdue += int(metrics.get("overdue_tickets") or 0)
        total_leads += int((lead_capture.get("summary") or {}).get("open") or 0)
        total_health += health_score
        tenant_items.append(
            {
                "slug": tenant_ref.get("slug"),
                "tenant_slug": tenant_ref.get("slug"),
                "display_name": tenant_ref.get("nombre"),
                "tenant_name": tenant_ref.get("nombre"),
                "health_score": health_score,
                "status": health_block.get("status") or ("active" if tenant_ref.get("is_active") else "inactive"),
                "risk_reason": risk_reason,
                "tenant": tenant_ref,
                "owner": _tenant_owner_ref(tenant),
                "health": health_block,
                "metrics": metrics,
                "readiness": readiness,
                "lead_capture": lead_capture.get("summary"),
                "routes": {
                    "profile_360": f"/api/v2/tenants/{tenant.slug}/admin-experience",
                    "legacy_profile_360": f"/api/admin/tenants/{tenant.slug}/profile-360",
                    "impersonate": f"/api/admin/tenants/{tenant.slug}/impersonate",
                },
            }
        )

    risky = [
        item
        for item in tenant_items
        if str((item.get("health") or {}).get("status") or "") in {"warning", "critical"}
        or int((item.get("metrics") or {}).get("overdue_tickets") or 0) > 0
    ]
    risky.sort(key=lambda item: ((item.get("health") or {}).get("score") or 0, -int((item.get("metrics") or {}).get("overdue_tickets") or 0)))

    return {
        "contract_version": "superadmin.command_center.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": {"from": _iso(start_date), "to": _iso(end_date)},
        "summary": {
            "tenants": len(tenant_items),
            "active_tenants": len([item for item in tenant_items if (item.get("tenant") or {}).get("is_active")]),
            "avg_health_score": round(total_health / len(tenant_items), 2) if tenant_items else 100.0,
            "open_tickets": total_open,
            "overdue_tickets": total_overdue,
            "open_leads": total_leads,
            "risky_tenants": len(risky),
        },
        "tenants": {
            "items": tenant_items,
            "top_risky": risky[:10],
        },
        "tenant_creation": {
            "endpoint": "/api/admin/tenants",
            "method": "POST",
            "required_fields": ["nombre", "tipo"],
            "optional_fields": ["slug", "plan", "owner_email", "vertical", "subvertical"],
            "supported_types": ["pyme", "municipio", "colegio"],
            "supported_verticals": ["empresas", "gobierno", "educacion"],
        },
        "lead_capture": {
            "endpoint": "/api/admin/leads/strategic-overview",
            "recent_by_tenant": [
                {"tenant": item["tenant"], "summary": item.get("lead_capture") or {}}
                for item in tenant_items[:20]
            ],
        },
        "recommended_actions": [
            {
                "kind": "review_risky_tenants",
                "priority": "high" if total_overdue else "medium",
                "message": "Revisar tenants con health bajo, SLA vencido o readiness incompleta.",
            },
            {
                "kind": "standardize_profile_modules",
                "priority": "medium",
                "message": "Usar admin-experience como fuente unica para perfil, panel operativo y modulos.",
            },
        ],
        "frontend_contract": {
            "render_as": "superadmin_command_center",
            "primary_refresh_seconds": 60,
            "recommended_views": ["tenant_grid", "health_ranking", "lead_pipeline", "tenant_creation", "profile_360_drawer"],
            "drilldown_endpoint_template": "/api/v2/tenants/{tenant_slug}/admin-experience",
        },
    }


def _templates_payload(tenant_id: int) -> list[dict[str, Any]]:
    rows = NotificationTemplate.query.filter_by(tenant_id=tenant_id).order_by(NotificationTemplate.created_at.desc()).all()
    return [
        {
            "id": item.id,
            "key": item.key,
            "channel": item.channel,
            "subject_template": item.subject_template,
            "body_template": item.body_template,
            "is_active": bool(item.is_active),
            "quiet_hours_start": item.quiet_hours_start,
            "quiet_hours_end": item.quiet_hours_end,
            "metadata": item.metadata_json if isinstance(item.metadata_json, dict) else {},
        }
        for item in rows
    ]


@v2_saas_bp.route("/tenant/activation/channels", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/activation/channels", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def tenant_channel_activation_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    return _json_response(build_channel_activation_payload(tenant, actor=current_user))


@v2_saas_bp.route("/employee-coverage", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/employee-coverage", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def employee_coverage_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    payload = _coverage_items(tenant)
    return _json_response({"contract_version": "employee.coverage.v1", "tenant": _tenant_ref(tenant), **payload})


@v2_saas_bp.route("/employee-routing", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/employee-routing", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def employee_routing_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    return _json_response(build_employee_routing_payload(tenant, viewer=current_user))


@v2_saas_bp.route("/employees/<int:employee_id>/routing-scope", methods=["PATCH", "POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/employees/<int:employee_id>/routing-scope", methods=["PATCH", "POST"])
@token_requerido
@require_role("admin", "super_admin")
def update_employee_routing_scope_v2(current_user, employee_id: int, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    employee = User.query.filter_by(id=employee_id, tenant_id=tenant.id, es_empleado=True).first()
    if not employee:
        return _error_response("Empleado no encontrado para este tenant", 404, "employee_not_found", "choose_valid_employee")

    payload = request.get_json(silent=True) or {}
    raw_scope = payload.get("employee_scope") if isinstance(payload.get("employee_scope"), dict) else payload
    categorias = normalize_scope_list(raw_scope.get("categorias") or raw_scope.get("categories"))
    zonas = normalize_scope_list(raw_scope.get("zonas") or raw_scope.get("zones"))
    channels = normalize_scope_list(raw_scope.get("channels") or raw_scope.get("canales"))
    permisos = normalize_scope_list(raw_scope.get("permisos") or raw_scope.get("permissions"))

    data = deepcopy(employee.accesibilidad) if isinstance(employee.accesibilidad, dict) else {}
    data["employee_scope"] = {
        "categorias": categorias,
        "zonas": zonas,
        "channels": channels,
        "permisos": permisos,
    }
    employee.accesibilidad = data
    employee.categorias_lista = categorias
    flag_modified(employee, "accesibilidad")
    db.session.add(employee)
    db.session.commit()

    return _json_response(
        {
            "contract_version": "employee.routing_scope.v1",
            "tenant": _tenant_ref(tenant),
            "employee": employee_ref(employee),
        }
    )


def _apply_employee_assignment(ticket: Any, assignee: User, actor: User) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    if isinstance(ticket, TenantTicket):
        extra = dict(ticket.datos_extra) if isinstance(ticket.datos_extra, dict) else {}
        extra["assignee_id"] = assignee.id
        extra["assignee_name"] = assignee.name
        extra["assignee_email"] = assignee.email
        _append_ticket_event(extra, action="assign", actor=actor, body=f"Asignado a {assignee.name}")
        ticket.datos_extra = extra
        ticket.updated_at = now
        flag_modified(ticket, "datos_extra")
    elif isinstance(ticket, (MunicipioTicket, PymeTicket)):
        ticket.asignado_a_id = assignee.id
        ticket.asignado_en = now
        if isinstance(ticket, MunicipioTicket):
            ticket.ultima_actividad = now
    db.session.add(ticket)
    return {"assignee_id": assignee.id, "assignee_name": assignee.name, "assigned_at": now.isoformat()}


@v2_saas_bp.route("/employee-routing/auto-assign", methods=["POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/employee-routing/auto-assign", methods=["POST"])
@token_requerido
@require_role("admin", "empleado", "supervisor", "manager", "super_admin")
def employee_routing_auto_assign_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    dry_run = payload.get("dry_run", True) is not False
    if not dry_run and canonical_role(getattr(current_user, "rol", None)) == ROLE_EMPLEADO:
        return _assignment_policy_error(
            TicketAssignmentPolicyError(
                403,
                "ticket_assignment_forbidden",
                "La autoasignacion aplicada requiere supervision; el empleado solo puede simularla",
                "request_supervisor_assignment",
            )
        )
    if not dry_run and not actor_can_assign_tickets(current_user):
        return _assignment_policy_error(
            TicketAssignmentPolicyError(
                403,
                "ticket_assignment_forbidden",
                "La asignacion a otro operador requiere supervision o la capacidad tickets.assign",
                "request_supervisor_assignment",
            )
        )
    limit = max(1, min(int(payload.get("limit", 25) or 25), 100))
    routing = build_employee_routing_payload(tenant, viewer=current_user)
    recommendations = routing.get("recommendations") or []
    explicit_tickets = payload.get("tickets") if isinstance(payload.get("tickets"), list) else []
    expected_by_identity: dict[tuple[str, int], Any] = {}
    if not dry_run and not explicit_tickets:
        return _error_response(
            "tickets con source_model, ticket_id y expected_assignee_id son obligatorios para aplicar autoasignacion",
            400,
            "assignment_cas_batch_identity_required",
            "send_explicit_ticket_cas_items",
        )
    if explicit_tickets:
        wanted: set[tuple[str, int]] = set()
        wanted_order: list[tuple[str, int]] = []
        for item in explicit_tickets:
            if not isinstance(item, Mapping):
                return _error_response(
                    "Cada ticket debe declarar una identidad source_model + ticket_id valida",
                    400,
                    "ticket_identity_invalid",
                    "send_exact_ticket_identity",
                )
            source = str(item.get("source_model") or "").strip()
            raw_ids = [item.get(key) for key in ("id", "ticket_id") if item.get(key) not in (None, "")]
            parsed_ids = [int(value) for value in raw_ids if str(value).isdigit() and int(value) > 0]
            if (
                source not in {"TenantTicket", "MunicipioTicket", "PymeTicket"}
                or len(parsed_ids) != len(raw_ids)
                or not parsed_ids
            ):
                return _error_response(
                    "Cada ticket debe declarar una identidad source_model + ticket_id valida",
                    400,
                    "ticket_identity_invalid",
                    "send_exact_ticket_identity",
                )
            if len(set(parsed_ids)) > 1:
                return _error_response(
                    "id y ticket_id deben identificar el mismo caso",
                    409,
                    "ticket_identity_conflict",
                    "refresh_ticket_identity",
                )
            identity = (source, parsed_ids[0])
            if identity in wanted:
                return _error_response(
                    "La identidad del ticket esta repetida",
                    409,
                    "ticket_identity_conflict",
                    "deduplicate_ticket_identity",
                )
            wanted.add(identity)
            wanted_order.append(identity)
            if not dry_run:
                if "expected_assignee_id" not in item:
                    return _error_response(
                        "expected_assignee_id es obligatorio por ticket",
                        400,
                        "expected_assignee_id_required",
                        "send_expected_assignee_id_per_ticket",
                    )
                expected_by_identity[identity] = item.get("expected_assignee_id")
        recommendations_by_identity = {
            (
                str((item.get("ticket") or {}).get("source_model") or ""),
                int((item.get("ticket") or {}).get("id") or 0),
            ): item
            for item in recommendations
        }

        # ``recommendations`` normally contains only unassigned cases.  An
        # explicit CAS retry must still resolve an already-assigned case so a
        # same-target replay is observable and a different stale transition
        # reaches the locked compare-and-set boundary.
        if not dry_run:
            snapshots_by_identity = {
                (str(item.get("source_model") or ""), int(item.get("id") or 0)): item
                for item in ((routing.get("queues") or {}).get("open") or [])
            }
            employees = User.query.filter_by(tenant_id=tenant.id, es_empleado=True).all()
            workloads = workload_by_employee(tenant)
            for identity in wanted_order:
                if identity in recommendations_by_identity:
                    continue
                snapshot = snapshots_by_identity.get(identity)
                if snapshot is None:
                    db.session.rollback()
                    return _error_response(
                        "Ticket no encontrado para esta identidad exacta",
                        404,
                        "ticket_not_found",
                        "refresh_ticket_identity",
                    )
                eligible = eligible_employees_for_ticket(snapshot, employees, workloads)
                current_assignee_id = snapshot.get("assignee_id")
                best = next(
                    (
                        item
                        for item in eligible
                        if str(item["employee"].get("id") or "")
                        == str(current_assignee_id or "")
                    ),
                    eligible[0] if eligible else None,
                )
                recommendations_by_identity[identity] = {
                    "ticket": snapshot,
                    "suggested_assignee": best["employee"] if best else None,
                    "score": best["score"] if best else 0,
                    "reasons": best["reasons"] if best else ["no_employee_available"],
                    "candidate_ids": [item["employee"]["id"] for item in eligible],
                    "eligible_assignees": eligible,
                }

        recommendations = [
            recommendations_by_identity[identity]
            for identity in wanted_order
            if identity in recommendations_by_identity
        ]

    results = []
    for item in recommendations[:limit]:
        ticket_ref = item.get("ticket") or {}
        assignee_ref = item.get("suggested_assignee") or {}
        assignee_id = assignee_ref.get("id") or assignee_ref.get("employee_id")
        ticket_id = ticket_ref.get("id") or ticket_ref.get("ticket_id")
        source_model = str(ticket_ref.get("source_model") or "")
        applied = False
        assignment = None
        assignment_reason = None
        if assignee_id and ticket_id and not dry_run:
            assignee = User.query.filter_by(id=int(assignee_id), tenant_id=tenant.id, es_empleado=True).first()
            ticket = find_ticket_for_assignment(
                tenant,
                source_model,
                int(ticket_id),
                for_update=True,
            )
            if assignee and ticket and ticket_assignee_is_compatible(assignee, ticket):
                current_assignee_id = (
                    (_ticket_extra(ticket) or {}).get("assignee_id")
                    if isinstance(ticket, TenantTicket)
                    else getattr(ticket, "asignado_a_id", None)
                )
                identity = (source_model, int(ticket_id))
                try:
                    transition = assignment_transition(
                        actor=current_user,
                        payload={"expected_assignee_id": expected_by_identity.get(identity)},
                        current_assignee_id=current_assignee_id,
                        target_assignee_id=assignee.id,
                    )
                except TicketAssignmentPolicyError as exc:
                    db.session.rollback()
                    return _assignment_policy_error(exc)
                if transition.replayed:
                    assignment = {
                        "assignee_id": assignee.id,
                        "assignee_name": assignee.name,
                        "assigned_at": None,
                        "replayed": True,
                    }
                    assignment_reason = "assignment_idempotent_same_target"
                else:
                    assignment = {
                        **_apply_employee_assignment(ticket, assignee, current_user),
                        "replayed": False,
                    }
                    applied = True
            elif assignee and ticket:
                assignment_reason = "assignee_category_scope_mismatch"
            elif not assignee:
                db.session.rollback()
                return _error_response(
                    "Empleado no encontrado para este tenant",
                    404,
                    "assignee_not_found",
                    "refresh_employee_routing",
                )
            else:
                db.session.rollback()
                return _error_response(
                    "Ticket no encontrado para esta identidad exacta",
                    404,
                    "ticket_not_found",
                    "refresh_ticket_identity",
                )
        elif not assignee_id:
            assignment_reason = "no_compatible_assignee"
        results.append(
            {
                **item,
                "applied": applied,
                "assignment": assignment,
                "assignment_reason": assignment_reason,
            }
        )

    if not dry_run:
        db.session.commit()

    return _json_response(
        {
            "contract_version": "employee.routing.auto_assign.v1",
            "tenant": _tenant_ref(tenant),
            "dry_run": dry_run,
            "applied_count": len([item for item in results if item.get("applied")]),
            "items": results,
        }
    )


@v2_saas_bp.route("/catalog/quality", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/catalog/quality", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def catalog_quality_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    try:
        limit = max(1, min(int(request.args.get("limit", 20) or 20), 100))
    except (TypeError, ValueError):
        limit = 20
    try:
        payload = build_catalog_quality_payload(tenant, limit=limit)
    except Exception:
        current_app.logger.exception(
            "catalog_quality_v2 failed for tenant %s; returning degraded payload",
            getattr(tenant, "slug", None),
        )
        payload = build_catalog_quality_fallback_payload(tenant)
    return _json_response(payload)


@v2_saas_bp.route("/tenant-health", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/health", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def tenant_health_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    return _json_response(_tenant_health_payload(tenant))


@v2_saas_bp.route("/tenant/admin-experience", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/admin-experience", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def tenant_admin_experience_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    start_date, end_date = _date_range_from_request(default_days=30)
    try:
        payload = _build_tenant_admin_experience_payload(
            tenant,
            start_date=start_date,
            end_date=end_date,
            app_config=current_app.config,
            viewer=current_user,
        )
    except Exception as exc:  # pragma: no cover - defensive production guard
        current_app.logger.exception("[tenant_admin_experience] degraded payload for tenant=%s", getattr(tenant, "slug", None))
        fallback_modules = [
            {
                "id": "inbox",
                "label": "Inbox omnicanal",
                "route": f"/t/{tenant.slug}/inbox",
                "endpoint": "/api/v2/inbox/omnichannel",
                "audience": "admin",
                "secondary_endpoints": [],
                "widgets": ["tickets"],
            },
            {
                "id": "analytics",
                "label": "Metricas y mapas",
                "route": f"/t/{tenant.slug}/analytics",
                "endpoint": "/api/v2/analytics/operations/dashboard",
                "audience": "admin",
                "secondary_endpoints": [],
                "widgets": ["kpis", "heatmap"],
            },
            {
                "id": "employees",
                "label": "Equipo y cobertura",
                "route": f"/t/{tenant.slug}/employees",
                "endpoint": "/api/v2/employee-routing",
                "audience": "admin",
                "secondary_endpoints": [],
                "widgets": ["assignment"],
            },
        ]
        payload = {
            "contract_version": "tenant.admin_experience.v1",
            "tenant": _tenant_ref(tenant),
            "period": {"from": _iso(start_date), "to": _iso(end_date)},
            "modules": fallback_modules,
            "navigation": _admin_navigation_payload(tenant, fallback_modules),
            "health": {
                "contract_version": "tenant.health.v1",
                "status": "degraded",
                "reason_code": "admin_experience_source_failed",
            },
            "frontend_contract": {
                "render_as": "tenant_admin_operating_system",
                "empty_state_behavior": "show_module_readiness_and_next_best_actions",
            },
            "error": {
                "code": 200,
                "message": "admin_experience_degraded",
                "reason_code": "source_failed",
                "detail": str(exc),
            },
        }
    return _json_response(payload)


@v2_saas_bp.route("/tenant/ops-qa/playbook", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/ops-qa/playbook", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def tenant_ops_qa_playbook_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    start_date, end_date = _date_range_from_request(default_days=30)
    try:
        payload = _build_tenant_ops_qa_playbook(
            tenant,
            start_date=start_date,
            end_date=end_date,
            app_config=current_app.config,
            viewer=current_user,
        )
    except Exception as exc:  # pragma: no cover - defensive degradation for ops UI
        current_app.logger.exception("[tenant_ops_qa] degraded payload for tenant=%s", getattr(tenant, "slug", None))
        payload = {
            "contract_version": "tenant.ops_qa.playbook.v1",
            "tenant": _tenant_ref(tenant),
            "period": {"from": _iso(start_date), "to": _iso(end_date)},
            "safe_by_default": True,
            "status": "fail",
            "score": 0,
            "summary": {"checks_total": 0, "passed": 0, "warnings": 0, "critical_failed": 1, "real_messages_sent": 0},
            "checks": [],
            "recommended_next_actions": [
                {
                    "id": "ops_qa_source_failed",
                    "label": "No se pudo construir el QA operativo",
                    "status": "fail",
                    "severity": "critical",
                    "next_action": "review_backend_logs",
                    "details": {"detail": str(exc)},
                }
            ],
            "frontend_contract": {"render_as": "tenant_ops_qa_command_center"},
        }
    return _json_response(payload)


@v2_saas_bp.route("/tenant/ops-qa/check/<string:check_id>", methods=["POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/ops-qa/check/<string:check_id>", methods=["POST"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def tenant_ops_qa_check_v2(current_user, check_id: str, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    normalized_check = str(check_id or "").strip().lower().replace("-", "_")
    start_date, end_date = _date_range_from_request(default_days=30)
    playbook = _build_tenant_ops_qa_playbook(
        tenant,
        start_date=start_date,
        end_date=end_date,
        app_config=current_app.config,
        viewer=current_user,
    )
    checks = {
        str(item.get("id")): item
        for item in playbook.get("checks", [])
        if isinstance(item, Mapping) and item.get("id")
    }
    if normalized_check not in checks:
        return _error_response("Check operativo no soportado", 404, "ops_qa_check_not_found", "refresh_playbook")
    return _json_response(
        _tenant_ops_qa_execution_result(
            tenant=tenant,
            check=checks[normalized_check],
            playbook=playbook,
        )
    )


@v2_saas_bp.route("/whatsapp/experience", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/experience", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def whatsapp_experience_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    return _json_response(build_whatsapp_experience(tenant, app_config=current_app.config))


@v2_saas_bp.route("/whatsapp/workflow-studio", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/workflow-studio", methods=["GET"])
@token_requerido
@require_role("admin", "super_admin")
def whatsapp_workflow_studio_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_workflow_studio_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    return _json_response(
        build_workflow_studio_contract(
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            app_config=current_app.config,
            plan_allowed=plan_allows_full_integrations(tenant),
        )
    )


@v2_saas_bp.after_request
def _workflow_studio_no_store(response):
    """Never cache tenant-scoped drafts, validation details, or simulations."""

    if "/whatsapp/workflow-studio" in request.path:
        response.headers["Cache-Control"] = "no-store"
    return response


def _resolve_workflow_studio_tenant_or_error(
    current_user: User,
    path_slug: str | None = None,
):
    """Resolve and authorize the tenant without inspecting the request body."""

    token_payload = getattr(g, "token_payload", None)
    token_slug = token_payload.get("tenant_slug") if isinstance(token_payload, Mapping) else None
    slug = first_specific_tenant_slug(
        path_slug,
        request.headers.get("X-Tenant-Slug"),
        request.headers.get("X-Tenant"),
        request.args.get("tenant_slug"),
        request.args.get("tenant"),
        token_slug,
        getattr(current_user, "tenant_slug", None),
    )

    tenant = None
    if slug:
        tenant = TenantProfile.query.filter(func.lower(TenantProfile.slug) == slug).one_or_none()
    elif getattr(current_user, "tenant_id", None):
        tenant = db.session.get(TenantProfile, current_user.tenant_id)

    if tenant is None:
        if slug:
            return None, _error_response(
                "Tenant no encontrado",
                404,
                "tenant_resolution_failed",
                "send_valid_tenant",
            )
        return None, _error_response(
            "tenant_slug es obligatorio para este endpoint",
            400,
            "tenant_resolution_failed",
            "send_valid_tenant",
        )
    if not _user_can_access_tenant(current_user, tenant):
        return None, _error_response(
            "Permisos insuficientes para este tenant",
            403,
            "forbidden_tenant",
            "switch_tenant",
        )
    return tenant, None


def _workflow_studio_json_payload(
    *,
    allowed_fields: set[str],
    max_bytes: int,
):
    if not request.is_json:
        return None, _error_response(
            "Workflow Studio requiere Content-Type application/json.",
            415,
            "workflow_content_type_invalid",
            "send_application_json",
        )
    if request.content_length is not None and request.content_length > max_bytes:
        return None, _error_response(
            "El request de Workflow Studio supera el tamano permitido.",
            413,
            "workflow_request_too_large",
            "reduce_workflow_payload",
        )
    raw_body = request.get_data(cache=True)
    if len(raw_body) > max_bytes:
        return None, _error_response(
            "El body JSON de Workflow Studio supera el tamano permitido.",
            413,
            "workflow_request_too_large",
            "reduce_workflow_payload",
        )
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        return None, _error_response(
            "Se requiere un body JSON.",
            400,
            "workflow_request_invalid",
            "send_json_object",
        )
    if set(payload) - allowed_fields:
        return None, _error_response(
            "El request de Workflow Studio contiene campos desconocidos.",
            400,
            "workflow_request_unknown_fields",
            "send_only_documented_fields",
        )
    return payload, None


def _workflow_studio_operation_error(exc: WorkflowStudioError):
    db.session.rollback()
    return _error_response(
        exc.message,
        exc.status_code,
        exc.reason_code,
        exc.action_hint,
    )


def _workflow_studio_storage_error():
    db.session.rollback()
    current_app.logger.error(
        "Workflow Studio durable storage unavailable; request content and SQL parameters omitted"
    )
    return _error_response(
        "El almacenamiento durable de Workflow Studio no esta disponible.",
        503,
        "workflow_storage_unavailable",
        "verify_migration_and_retry",
    )


def _require_workflow_studio_durable_or_error(tenant: TenantProfile):
    plan_error = _require_full_integration_plan(tenant, "whatsapp_business_platform")
    if plan_error:
        return plan_error
    try:
        require_workflow_durable_control_plane(
            current_app.config,
            tenant_id=tenant.id,
        )
    except WorkflowStudioError as exc:
        return _workflow_studio_operation_error(exc)
    return None


@v2_saas_bp.route("/whatsapp/workflow-studio/validate", methods=["POST"])
@v2_saas_bp.route(
    "/tenants/<string:tenant_slug>/whatsapp/workflow-studio/validate",
    methods=["POST"],
)
@token_requerido
@require_role("admin", "super_admin")
def validate_whatsapp_workflow_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_workflow_studio_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    payload, payload_error = _workflow_studio_json_payload(
        allowed_fields={"draft"},
        max_bytes=MAX_DRAFT_BYTES + 4096,
    )
    if payload_error:
        return payload_error
    gate = workflow_durable_control_plane_gate(
        current_app.config,
        tenant_id=tenant.id,
        plan_allowed=plan_allows_full_integrations(tenant),
    )
    return _json_response(
        validate_workflow_draft(
            payload.get("draft"),
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            durable_control_plane_ready=gate["available"] is True,
        )
    )


@v2_saas_bp.route("/whatsapp/workflow-studio/simulate", methods=["POST"])
@v2_saas_bp.route(
    "/tenants/<string:tenant_slug>/whatsapp/workflow-studio/simulate",
    methods=["POST"],
)
@token_requerido
@require_role("admin", "super_admin")
def simulate_whatsapp_workflow_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_workflow_studio_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    payload, payload_error = _workflow_studio_json_payload(
        allowed_fields={"draft", "event"},
        max_bytes=MAX_DRAFT_BYTES + MAX_EVENT_BYTES + 4096,
    )
    if payload_error:
        return payload_error
    gate = workflow_durable_control_plane_gate(
        current_app.config,
        tenant_id=tenant.id,
        plan_allowed=plan_allows_full_integrations(tenant),
    )
    result = simulate_workflow_draft(
        payload.get("draft"),
        payload.get("event"),
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        durable_control_plane_ready=gate["available"] is True,
    )
    return _json_response(result, 422 if result.get("status") == "blocked" else 200)


@v2_saas_bp.route("/whatsapp/workflow-studio/workflows", methods=["GET"])
@v2_saas_bp.route(
    "/tenants/<string:tenant_slug>/whatsapp/workflow-studio/workflows",
    methods=["GET"],
)
@token_requerido
@require_role("admin", "super_admin")
def list_whatsapp_workflows_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_workflow_studio_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    gate_error = _require_workflow_studio_durable_or_error(tenant)
    if gate_error:
        return gate_error
    try:
        return _json_response(list_workflow_ledgers(tenant_id=tenant.id))
    except WorkflowStudioError as exc:
        return _workflow_studio_operation_error(exc)
    except SQLAlchemyError:
        return _workflow_studio_storage_error()


@v2_saas_bp.route(
    "/whatsapp/workflow-studio/workflows/<string:workflow_id>",
    methods=["GET"],
)
@v2_saas_bp.route(
    "/tenants/<string:tenant_slug>/whatsapp/workflow-studio/workflows/<string:workflow_id>",
    methods=["GET"],
)
@token_requerido
@require_role("admin", "super_admin")
def get_whatsapp_workflow_v2(
    current_user,
    workflow_id: str,
    tenant_slug: str | None = None,
):
    tenant, error = _resolve_workflow_studio_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    gate_error = _require_workflow_studio_durable_or_error(tenant)
    if gate_error:
        return gate_error
    try:
        return _json_response(
            get_workflow_ledger(tenant_id=tenant.id, workflow_id=workflow_id)
        )
    except WorkflowStudioError as exc:
        return _workflow_studio_operation_error(exc)
    except SQLAlchemyError:
        return _workflow_studio_storage_error()


@v2_saas_bp.route("/whatsapp/workflow-studio/drafts", methods=["POST"])
@v2_saas_bp.route(
    "/tenants/<string:tenant_slug>/whatsapp/workflow-studio/drafts",
    methods=["POST"],
)
@token_requerido
@require_role("admin", "super_admin")
def create_whatsapp_workflow_draft_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_workflow_studio_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    gate_error = _require_workflow_studio_durable_or_error(tenant)
    if gate_error:
        return gate_error
    payload, payload_error = _workflow_studio_json_payload(
        allowed_fields={"draft", "idempotency_key"},
        max_bytes=MAX_DRAFT_BYTES + 4096,
    )
    if payload_error:
        return payload_error
    try:
        result = save_workflow_draft(
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            actor_user_id=current_user.id,
            draft=payload.get("draft"),
            idempotency_key=payload.get("idempotency_key"),
        )
        return _json_response(result, 200 if result["idempotent_replay"] else 201)
    except WorkflowStudioError as exc:
        return _workflow_studio_operation_error(exc)
    except SQLAlchemyError:
        return _workflow_studio_storage_error()


@v2_saas_bp.route(
    "/whatsapp/workflow-studio/workflows/<string:workflow_id>/drafts",
    methods=["POST"],
)
@v2_saas_bp.route(
    "/tenants/<string:tenant_slug>/whatsapp/workflow-studio/workflows/<string:workflow_id>/drafts",
    methods=["POST"],
)
@token_requerido
@require_role("admin", "super_admin")
def revise_whatsapp_workflow_draft_v2(
    current_user,
    workflow_id: str,
    tenant_slug: str | None = None,
):
    tenant, error = _resolve_workflow_studio_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    gate_error = _require_workflow_studio_durable_or_error(tenant)
    if gate_error:
        return gate_error
    payload, payload_error = _workflow_studio_json_payload(
        allowed_fields={"draft", "expected_revision", "idempotency_key"},
        max_bytes=MAX_DRAFT_BYTES + 4096,
    )
    if payload_error:
        return payload_error
    try:
        result = save_workflow_draft(
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            actor_user_id=current_user.id,
            draft=payload.get("draft"),
            idempotency_key=payload.get("idempotency_key"),
            workflow_id=workflow_id,
            expected_revision=payload.get("expected_revision"),
        )
        return _json_response(result, 200 if result["idempotent_replay"] else 201)
    except WorkflowStudioError as exc:
        return _workflow_studio_operation_error(exc)
    except SQLAlchemyError:
        return _workflow_studio_storage_error()


@v2_saas_bp.route(
    "/whatsapp/workflow-studio/workflows/<string:workflow_id>/reviews",
    methods=["POST"],
)
@v2_saas_bp.route(
    "/tenants/<string:tenant_slug>/whatsapp/workflow-studio/workflows/<string:workflow_id>/reviews",
    methods=["POST"],
)
@token_requerido
@require_role("admin", "super_admin")
def review_whatsapp_workflow_v2(
    current_user,
    workflow_id: str,
    tenant_slug: str | None = None,
):
    tenant, error = _resolve_workflow_studio_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    gate_error = _require_workflow_studio_durable_or_error(tenant)
    if gate_error:
        return gate_error
    payload, payload_error = _workflow_studio_json_payload(
        allowed_fields={"operation", "subject_id", "decision", "note", "idempotency_key"},
        max_bytes=16 * 1024,
    )
    if payload_error:
        return payload_error
    try:
        result = review_workflow_subject(
            tenant_id=tenant.id,
            workflow_id=workflow_id,
            reviewer_user_id=current_user.id,
            operation=payload.get("operation"),
            subject_id=payload.get("subject_id"),
            decision=payload.get("decision"),
            review_note=payload.get("note"),
            idempotency_key=payload.get("idempotency_key"),
        )
        return _json_response(result, 200 if result["idempotent_replay"] else 201)
    except WorkflowStudioError as exc:
        return _workflow_studio_operation_error(exc)
    except SQLAlchemyError:
        return _workflow_studio_storage_error()


def _workflow_publication_request(
    *,
    current_user: User,
    tenant: TenantProfile,
    workflow_id: str,
    operation: str,
):
    allowed_fields = (
        {"draft_revision_id", "review_id", "idempotency_key"}
        if operation == "publish"
        else {"target_version_id", "review_id", "idempotency_key"}
    )
    payload, payload_error = _workflow_studio_json_payload(
        allowed_fields=allowed_fields,
        max_bytes=8 * 1024,
    )
    if payload_error:
        return payload_error
    try:
        if operation == "publish":
            result = publish_workflow(
                tenant_id=tenant.id,
                workflow_id=workflow_id,
                publisher_user_id=current_user.id,
                draft_revision_id=payload.get("draft_revision_id"),
                review_id=payload.get("review_id"),
                idempotency_key=payload.get("idempotency_key"),
            )
        else:
            result = rollback_workflow(
                tenant_id=tenant.id,
                workflow_id=workflow_id,
                publisher_user_id=current_user.id,
                target_version_id=payload.get("target_version_id"),
                review_id=payload.get("review_id"),
                idempotency_key=payload.get("idempotency_key"),
            )
        return _json_response(result, 200 if result["idempotent_replay"] else 201)
    except WorkflowStudioError as exc:
        return _workflow_studio_operation_error(exc)
    except SQLAlchemyError:
        return _workflow_studio_storage_error()


@v2_saas_bp.route(
    "/whatsapp/workflow-studio/workflows/<string:workflow_id>/publish",
    methods=["POST"],
)
@v2_saas_bp.route(
    "/tenants/<string:tenant_slug>/whatsapp/workflow-studio/workflows/<string:workflow_id>/publish",
    methods=["POST"],
)
@token_requerido
@require_role("admin", "super_admin")
def publish_whatsapp_workflow_v2(
    current_user,
    workflow_id: str,
    tenant_slug: str | None = None,
):
    tenant, error = _resolve_workflow_studio_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    gate_error = _require_workflow_studio_durable_or_error(tenant)
    if gate_error:
        return gate_error
    return _workflow_publication_request(
        current_user=current_user,
        tenant=tenant,
        workflow_id=workflow_id,
        operation="publish",
    )


@v2_saas_bp.route(
    "/whatsapp/workflow-studio/workflows/<string:workflow_id>/rollback",
    methods=["POST"],
)
@v2_saas_bp.route(
    "/tenants/<string:tenant_slug>/whatsapp/workflow-studio/workflows/<string:workflow_id>/rollback",
    methods=["POST"],
)
@token_requerido
@require_role("admin", "super_admin")
def rollback_whatsapp_workflow_v2(
    current_user,
    workflow_id: str,
    tenant_slug: str | None = None,
):
    tenant, error = _resolve_workflow_studio_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    gate_error = _require_workflow_studio_durable_or_error(tenant)
    if gate_error:
        return gate_error
    return _workflow_publication_request(
        current_user=current_user,
        tenant=tenant,
        workflow_id=workflow_id,
        operation="rollback",
    )


@v2_saas_bp.route("/whatsapp/flow-runtime", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/flow-runtime", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def whatsapp_flow_runtime_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    experience = build_whatsapp_experience(tenant, app_config=current_app.config)
    runtime = experience.get("flow_runtime") if isinstance(experience, Mapping) else {}
    if not isinstance(runtime, dict):
        runtime = {}
    return _json_response(runtime)


@v2_saas_bp.route("/integrations/whatsapp/status", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/integrations/whatsapp/status", methods=["GET"])
@cutover_writer_view
@token_requerido
@require_role("admin", "empleado", "super_admin")
def whatsapp_provider_status_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    return _json_response(build_whatsapp_provider_status(tenant, current_app.config))


@v2_saas_bp.route("/whatsapp/tech-provider", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/tech-provider", methods=["GET"])
@token_requerido
@require_role("admin", "super_admin")
def whatsapp_tech_provider_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    plan_error = _require_full_integration_plan(tenant, "whatsapp_sender_management")
    if plan_error:
        return plan_error
    return _json_response(build_twilio_tech_provider_contract(tenant, current_app.config))


def _whatsapp_smoke_execution_result(
    *,
    test_id: str,
    ok: bool,
    label: str,
    execution_mode: str,
    danger_level: str,
    details: Mapping[str, Any] | None = None,
    next_action: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    return {
        "contract_version": "twilio.tech_provider.smoke_execution.v1",
        "test_id": test_id,
        "ok": bool(ok),
        "status": status or ("pass" if ok else "warning"),
        "label": label,
        "execution_mode": execution_mode,
        "danger_level": danger_level,
        "sends_real_message": danger_level == "real_message",
        "details": dict(details or {}),
        "next_action": next_action or ("continue_playbook" if ok else "review_result"),
    }


@v2_saas_bp.route("/whatsapp/tech-provider/smoke-test/<string:test_id>", methods=["POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/tech-provider/smoke-test/<string:test_id>", methods=["POST"])
@token_requerido
@require_role("admin", "super_admin")
def whatsapp_tech_provider_smoke_test_v2(current_user, test_id: str, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    plan_error = _require_full_integration_plan(tenant, "whatsapp_sender_management")
    if plan_error:
        return plan_error

    normalized_test = str(test_id or "").strip().lower().replace("-", "_")
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    contract = build_twilio_tech_provider_contract(tenant, current_app.config)
    playbook_tests = {
        str(item.get("id")): item
        for item in (contract.get("smoke_playbook") or {}).get("tests", [])
        if isinstance(item, Mapping) and item.get("id")
    }
    if normalized_test not in playbook_tests:
        return _error_response("Smoke test no soportado", 404, "smoke_test_not_found", "refresh_playbook")

    playbook_item = playbook_tests[normalized_test]
    danger_level = str(playbook_item.get("danger_level") or "safe")
    if danger_level == "real_message" and not payload.get("confirm_real_message"):
        return _json_response(
            _whatsapp_smoke_execution_result(
                test_id=normalized_test,
                ok=False,
                label=str(playbook_item.get("label") or normalized_test),
                execution_mode=str(playbook_item.get("execution_mode") or "manual_confirmation_required"),
                danger_level=danger_level,
                status="blocked",
                next_action="confirm_real_message_required",
                details={
                    "reason_code": "confirmation_required",
                    "message": "Esta prueba enviaria un mensaje real y requiere confirmacion explicita.",
                },
            ),
            409,
        )

    if normalized_test == "provider_status":
        provider_status = build_whatsapp_provider_status(tenant, current_app.config)
        checks = provider_status.get("readiness_checks") if isinstance(provider_status.get("readiness_checks"), list) else []
        failed = [item for item in checks if isinstance(item, Mapping) and not item.get("ok")]
        return _json_response(
            _whatsapp_smoke_execution_result(
                test_id=normalized_test,
                ok=not failed,
                label="Estado del proveedor",
                execution_mode="read_only",
                danger_level="safe",
                details={
                    "checks_total": len(checks),
                    "failed": failed,
                    "next_action": provider_status.get("next_action"),
                    "provider_contract": provider_status.get("contract_version"),
                    "readiness_check_ids": [
                        item.get("id")
                        for item in checks
                        if isinstance(item, Mapping) and item.get("id")
                    ],
                },
                next_action=provider_status.get("next_action") or "continue_playbook",
                status="pass" if not failed else "warning",
            )
        )

    if normalized_test == "whatsapp_experience":
        experience = build_whatsapp_experience(tenant, app_config=current_app.config)
        ok = (
            experience.get("contract_version") == "whatsapp.experience.v1"
            and "conversation_intelligence" in experience
            and "tracking" in experience
        )
        return _json_response(
            _whatsapp_smoke_execution_result(
                test_id=normalized_test,
                ok=ok,
                label="Experiencia WhatsApp completa",
                execution_mode="read_only",
                danger_level="safe",
                details={
                    "contract_version": experience.get("contract_version"),
                    "channel": experience.get("channel"),
                    "has_tracking": "tracking" in experience,
                    "has_conversation_intelligence": "conversation_intelligence" in experience,
                },
                next_action="review_operations_hub" if ok else "fix_whatsapp_experience_contract",
            )
        )

    if normalized_test == "template_registry":
        experience = build_whatsapp_experience(tenant, app_config=current_app.config)
        required_templates = (experience.get("templates") or {}).get("required") or []
        vertical_templates = (experience.get("templates") or {}).get("vertical") or {}
        manifest = _template_creation_manifest_payload(
            tenant=tenant,
            required_templates=required_templates if isinstance(required_templates, list) else [],
            vertical_templates=vertical_templates if isinstance(vertical_templates, Mapping) else {},
        )
        blocking = [
            item
            for item in manifest.get("items", [])
            if isinstance(item, Mapping) and (item.get("readiness") or {}).get("severity") == "blocking"
        ]
        return _json_response(
            _whatsapp_smoke_execution_result(
                test_id=normalized_test,
                ok=not blocking,
                label="Plantillas y webviews",
                execution_mode="dry_run_first",
                danger_level="safe_when_dry_run",
                details={
                    "manifest_contract": manifest.get("contract_version"),
                    "templates_total": manifest.get("templates_total"),
                    "actionable_total": manifest.get("actionable_total"),
                    "webview_ready_total": manifest.get("webview_ready_total"),
                    "by_content_family": manifest.get("by_content_family"),
                    "blocking": blocking[:5],
                },
                next_action="submit_or_sync_templates" if not blocking else "complete_template_copy_and_samples",
                status="pass" if not blocking else "warning",
            )
        )

    if normalized_test == "sandbox_message":
        sandbox_number = _twilio_sandbox_number()
        join_phrase = _twilio_sandbox_join_phrase(payload)
        wa_number = "".join(ch for ch in sandbox_number if ch.isdigit())
        message = str(payload.get("message") or "Hola, quiero probar el asistente").strip()
        return _json_response(
            _whatsapp_smoke_execution_result(
                test_id=normalized_test,
                ok=bool(sandbox_number and join_phrase),
                label="Mensaje sandbox",
                execution_mode="copy_or_deeplink",
                danger_level="safe",
                details={
                    "sends_real_message": False,
                    "sandbox_number": f"whatsapp:{sandbox_number}",
                    "join_phrase": join_phrase,
                    "wa_deeplink": f"https://wa.me/{wa_number}?text={quote_plus(join_phrase)}",
                    "copy_text": f"{join_phrase}\n\n{message}",
                },
                next_action="open_whatsapp_or_copy_instructions",
            )
        )

    if normalized_test == "production_channel":
        result = poll_whatsapp_sender_status(tenant, current_app.config)
        merged_state = merge_twilio_state(tenant, result.get("state_patch") or {})
        sync_twilio_provider_records(
            tenant,
            merged_state,
            app_config=current_app.config,
            actor_user=current_user,
            request_id=_request_id(),
            event_type="twilio_sender_status_smoke",
        )
        onboarding = refresh_tenant_whatsapp_onboarding(
            tenant,
            app_config=current_app.config,
            source="twilio_sender_status_smoke",
        )
        channel_activation = build_channel_activation_payload(tenant)
        flag_modified(tenant, "configuracion")
        db.session.commit()
        sender_status = str(merged_state.get("sender_status") or "").upper()
        ok = sender_status in {"ONLINE", "APPROVED", "CONNECTED", "ACTIVE"}
        return _json_response(
            _whatsapp_smoke_execution_result(
                test_id=normalized_test,
                ok=ok,
                label="Canal productivo",
                execution_mode="status_poll",
                danger_level="safe",
                details={
                    "provider_ok": result.get("ok"),
                    "sender_status": merged_state.get("sender_status"),
                    "sender_sid": merged_state.get("sender_sid"),
                    "sender_id": merged_state.get("sender_id"),
                    "onboarding": onboarding,
                    "channel_activation": channel_activation,
                },
                next_action="send_whatsapp_smoke_test" if ok else "wait_for_meta_approval_or_poll_again",
                status="pass" if ok else "warning",
            )
        )

    return _json_response(
        _whatsapp_smoke_execution_result(
            test_id=normalized_test,
            ok=False,
            label=str(playbook_item.get("label") or normalized_test),
            execution_mode=str(playbook_item.get("execution_mode") or "manual"),
            danger_level=danger_level,
            status="blocked",
            next_action="not_implemented_yet",
            details={"reason_code": "execution_not_implemented"},
        ),
        501,
    )


@v2_saas_bp.route("/whatsapp/tech-provider/provision", methods=["POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/tech-provider/provision", methods=["POST"])
@token_requerido
@require_role("admin", "super_admin")
def whatsapp_tech_provider_provision_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    plan_error = _require_full_integration_plan(tenant, "whatsapp_sender_management")
    if plan_error:
        return plan_error

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}

    result = provision_twilio_subaccount(tenant, payload, current_app.config)
    merged_state = merge_twilio_state(tenant, result.get("state_patch") or {})
    sync_twilio_provider_records(
        tenant,
        merged_state,
        app_config=current_app.config,
        actor_user=current_user,
        request_id=_request_id(),
        event_type="twilio_provisioning_plan",
    )
    onboarding = refresh_tenant_whatsapp_onboarding(
        tenant,
        app_config=current_app.config,
        source="twilio_provisioning_plan",
    )
    channel_activation = build_channel_activation_payload(tenant)
    flag_modified(tenant, "configuracion")
    db.session.commit()
    return _json_response(
        {
            **result,
            "tenant": _tenant_ref(tenant),
            "state": {
                "status": merged_state.get("status"),
                "last_step": merged_state.get("last_step"),
                "twilio_account_sid": merged_state.get("twilio_account_sid"),
                "messaging_service_sid": merged_state.get("messaging_service_sid"),
                "render_subaccount_secret_synced": merged_state.get("render_subaccount_secret_synced"),
                "render_subaccount_secret_sync_status": merged_state.get("render_subaccount_secret_sync_status"),
                "sender_sid": merged_state.get("sender_sid"),
                "sender_id": merged_state.get("sender_id"),
                "updated_at": merged_state.get("updated_at"),
            },
            "onboarding": onboarding,
            "channel_activation": channel_activation,
            "contract": build_twilio_tech_provider_contract(tenant, current_app.config),
        },
        200 if result.get("ok", True) else 400,
    )


@v2_saas_bp.route("/whatsapp/tech-provider/voice-app", methods=["POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/tech-provider/voice-app", methods=["POST"])
@token_requerido
@require_role("admin", "super_admin")
def whatsapp_tech_provider_voice_app_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    plan_error = _require_full_integration_plan(tenant, "whatsapp_sender_management")
    if plan_error:
        return plan_error

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}

    result = provision_twilio_voice_application(tenant, payload, current_app.config)
    merged_state = merge_twilio_state(tenant, result.get("state_patch") or {})
    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    cfg.update({key: value for key, value in (result.get("tenant_config_patch") or {}).items() if value is not None})
    tenant.configuracion = cfg
    sync_twilio_provider_records(
        tenant,
        merged_state,
        app_config=current_app.config,
        actor_user=current_user,
        request_id=_request_id(),
        event_type="twilio_voice_application",
    )
    onboarding = refresh_tenant_whatsapp_onboarding(
        tenant,
        app_config=current_app.config,
        source="twilio_voice_application",
    )
    channel_activation = build_channel_activation_payload(tenant)
    flag_modified(tenant, "configuracion")
    db.session.commit()
    return _json_response(
        {
            **result,
            "tenant": _tenant_ref(tenant),
            "state": {
                "status": merged_state.get("status"),
                "last_step": merged_state.get("last_step"),
                "voice_status": merged_state.get("voice_status"),
                "voice_last_step": merged_state.get("voice_last_step"),
                "voice_twiml_app_sid": merged_state.get("voice_twiml_app_sid"),
                "voice_url": merged_state.get("voice_url"),
                "voice_fallback_url": merged_state.get("voice_fallback_url"),
                "voice_status_callback_url": merged_state.get("voice_status_callback_url"),
                "voice_sender_attached": merged_state.get("voice_sender_attached"),
                "updated_at": merged_state.get("updated_at"),
            },
            "onboarding": onboarding,
            "channel_activation": channel_activation,
            "contract": build_twilio_tech_provider_contract(tenant, current_app.config),
        },
        200 if result.get("ok", True) else 400,
    )


@v2_saas_bp.route("/whatsapp/tech-provider/embedded-signup", methods=["POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/tech-provider/embedded-signup", methods=["POST"])
@token_requerido
@require_role("admin", "super_admin")
def whatsapp_tech_provider_embedded_signup_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    plan_error = _require_full_integration_plan(tenant, "whatsapp_sender_management")
    if plan_error:
        return plan_error

    raw_payload = request.get_json(silent=True)
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    validate_only = payload.get("validate_only", False)
    if not isinstance(validate_only, bool):
        return _error_response(
            "validate_only debe ser booleano",
            400,
            "embedded_signup_validate_only_invalid",
            "send_boolean_validate_only",
        )

    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    existing_state = cfg.get(STATE_KEY) if isinstance(cfg.get(STATE_KEY), dict) else {}
    verification = verify_meta_embedded_signup_completion(
        payload,
        existing_state=existing_state,
    )
    if not verification.get("accepted"):
        return _json_response(
            {
                "contract_version": "twilio.tech_provider.embedded_signup.v1",
                "ok": False,
                "status": "rejected",
                "reason_code": "embedded_signup_completion_invalid",
                "retryable": False,
                "retryable_after_correction": True,
                "tenant": _tenant_ref(tenant),
                "verification": verification,
                "persisted": False,
                "state_unchanged": True,
                "provider_calls_performed": False,
                "next_action": verification.get("next_action"),
                "error": {
                    "code": 422,
                    "message": "El resultado de Embedded Signup no cumple el contrato.",
                },
            },
            422,
        )

    if validate_only:
        return _json_response(
            {
                "contract_version": "twilio.tech_provider.embedded_signup.v1",
                "ok": True,
                "status": "validated",
                "tenant": _tenant_ref(tenant),
                "verification": verification,
                "persisted": False,
                "state_unchanged": True,
                "provider_calls_performed": False,
                "next_action": "submit_verified_embedded_signup_completion",
            }
        )

    normalized = verification.get("normalized") or {}
    now = datetime.now(timezone.utc).isoformat()
    state_patch = {
        "status": "pending_sender_registration",
        "last_step": "embedded_signup_completed",
        "updated_at": now,
        "waba_id": normalized.get("waba_id"),
        "phone_number_id": normalized.get("phone_number_id"),
        "embedded_signup_session_id": normalized.get("session_id"),
        "embedded_signup_code_present": bool(
            (verification.get("security") or {}).get("authorization_code_received")
        ),
        "embedded_signup_type": normalized.get("type"),
        "embedded_signup_event": normalized.get("event") or "LEGACY_FLAT",
        "embedded_signup_completion_mode": verification.get("completion_mode"),
        "embedded_signup_payload_shape": verification.get("payload_shape"),
        "embedded_signup_completion_validated": True,
        "embedded_signup_verification_contract": verification.get("contract_version"),
        "embedded_signup_verification_level": verification.get("verification_level"),
        "embedded_signup_remote_attestation_performed": False,
        "embedded_signup_completed_at": now,
        "embedded_signup_verification_warnings": [
            item.get("code")
            for item in verification.get("warnings") or []
            if isinstance(item, Mapping) and item.get("code")
        ],
    }
    merged_state = merge_twilio_state(tenant, state_patch)
    public_state = _scrub_twilio_state_secrets(tenant, merged_state)
    sync_twilio_provider_records(
        tenant,
        public_state,
        app_config=current_app.config,
        actor_user=current_user,
        request_id=_request_id(),
        event_type="meta_embedded_signup_completed",
    )
    onboarding = refresh_tenant_whatsapp_onboarding(
        tenant,
        app_config=current_app.config,
        source="meta_embedded_signup_completed",
    )
    channel_activation = build_channel_activation_payload(tenant)
    flag_modified(tenant, "configuracion")
    db.session.commit()
    persisted_verification = {**verification, "writes_performed": True}
    return _json_response(
        {
            "contract_version": "twilio.tech_provider.embedded_signup.v1",
            "ok": True,
            "status": "accepted",
            "tenant": _tenant_ref(tenant),
            "state": public_state,
            "verification": persisted_verification,
            "persisted": True,
            "state_unchanged": False,
            "provider_calls_performed": False,
            "onboarding": onboarding,
            "channel_activation": channel_activation,
            "next_action": "register_whatsapp_sender_via_senders_api",
            "contract": build_twilio_tech_provider_contract(tenant, current_app.config),
        }
    )


@v2_saas_bp.route("/whatsapp/tech-provider/register-sender", methods=["POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/tech-provider/register-sender", methods=["POST"])
@token_requerido
@require_role("admin", "super_admin")
def whatsapp_tech_provider_register_sender_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    plan_error = _require_full_integration_plan(tenant, "whatsapp_sender_management")
    if plan_error:
        return plan_error

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}

    result = register_whatsapp_sender(tenant, payload, current_app.config)
    merged_state = merge_twilio_state(tenant, result.get("state_patch") or {})
    voice_result = None
    voice_retry = None
    warnings = []
    if result.get("ok", True):
        try:
            voice_result = provision_twilio_voice_application(tenant, payload, current_app.config)
        except Exception as exc:
            current_app.logger.exception(
                "Optional Twilio Voice provisioning failed after sender registration for tenant=%s",
                getattr(tenant, "slug", None),
            )
            voice_result = {
                "contract_version": "twilio.tech_provider.voice_application.v1",
                "ok": False,
                "mode": "failed",
                "reason_code": "twilio_voice_optional_step_failed",
                "error": str(exc),
                "steps": [],
                "state_patch": {
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "voice_status": "voice_application_failed",
                    "voice_last_step": "optional_voice_provisioning",
                },
            }
        voice_result["optional"] = True
        merged_state = merge_twilio_state(tenant, voice_result.get("state_patch") or {})
        cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
        cfg.update({key: value for key, value in (voice_result.get("tenant_config_patch") or {}).items() if value is not None})
        tenant.configuracion = cfg
        if not voice_result.get("ok", True):
            voice_retry = {
                "required": True,
                "method": "POST",
                "endpoint": f"/api/v2/tenants/{tenant.slug}/whatsapp/tech-provider/voice-app",
                "reason_code": voice_result.get("reason_code") or "twilio_voice_optional_step_failed",
            }
            warnings.append(
                {
                    "code": "optional_voice_provisioning_failed",
                    "message": "WhatsApp sender registration succeeded; Voice setup remains pending.",
                    "retry": voice_retry,
                }
            )
    sync_twilio_provider_records(
        tenant,
        merged_state,
        app_config=current_app.config,
        actor_user=current_user,
        request_id=_request_id(),
        event_type="twilio_sender_registration",
    )
    onboarding = refresh_tenant_whatsapp_onboarding(
        tenant,
        app_config=current_app.config,
        source="twilio_sender_registration",
    )
    channel_activation = build_channel_activation_payload(tenant)
    flag_modified(tenant, "configuracion")
    db.session.commit()
    return _json_response(
        {
            **result,
            "voice_app": voice_result,
            "voice_retry": voice_retry,
            "warnings": warnings,
            "tenant": _tenant_ref(tenant),
            "state": {
                "status": merged_state.get("status"),
                "last_step": merged_state.get("last_step"),
                "twilio_account_sid": merged_state.get("twilio_account_sid"),
                "messaging_service_sid": merged_state.get("messaging_service_sid"),
                "sender_sid": merged_state.get("sender_sid"),
                "sender_id": merged_state.get("sender_id"),
                "sender_status": merged_state.get("sender_status"),
                "waba_id": merged_state.get("waba_id"),
                "phone_number_id": merged_state.get("phone_number_id"),
                "render_subaccount_secret_synced": merged_state.get("render_subaccount_secret_synced"),
                "render_subaccount_secret_sync_status": merged_state.get("render_subaccount_secret_sync_status"),
                "voice_status": merged_state.get("voice_status"),
                "voice_twiml_app_sid": merged_state.get("voice_twiml_app_sid"),
                "voice_sender_attached": merged_state.get("voice_sender_attached"),
                "updated_at": merged_state.get("updated_at"),
            },
            "onboarding": onboarding,
            "channel_activation": channel_activation,
            "contract": build_twilio_tech_provider_contract(tenant, current_app.config),
        },
        200 if result.get("ok", True) else 400,
    )


@v2_saas_bp.route("/whatsapp/tech-provider/sender-status", methods=["GET", "POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/tech-provider/sender-status", methods=["GET", "POST"])
@cutover_writer_view
@token_requerido
@require_role("admin", "super_admin")
def whatsapp_tech_provider_sender_status_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    plan_error = _require_full_integration_plan(tenant, "whatsapp_sender_management")
    if plan_error:
        return plan_error

    result = poll_whatsapp_sender_status(tenant, current_app.config)
    merged_state = merge_twilio_state(tenant, result.get("state_patch") or {})
    sync_twilio_provider_records(
        tenant,
        merged_state,
        app_config=current_app.config,
        actor_user=current_user,
        request_id=_request_id(),
        event_type="twilio_sender_status_poll",
    )
    onboarding = refresh_tenant_whatsapp_onboarding(
        tenant,
        app_config=current_app.config,
        source="twilio_sender_status_poll",
    )
    channel_activation = build_channel_activation_payload(tenant)
    flag_modified(tenant, "configuracion")
    db.session.commit()
    return _json_response(
        {
            **result,
            "tenant": _tenant_ref(tenant),
            "state": {
                "status": merged_state.get("status"),
                "last_step": merged_state.get("last_step"),
                "sender_sid": merged_state.get("sender_sid"),
                "sender_id": merged_state.get("sender_id"),
                "sender_status": merged_state.get("sender_status"),
                "updated_at": merged_state.get("updated_at"),
            },
            "onboarding": onboarding,
            "channel_activation": channel_activation,
            "contract": build_twilio_tech_provider_contract(tenant, current_app.config),
        },
        200 if result.get("ok", True) else 400,
    )


def _twilio_sandbox_number() -> str:
    raw = (
        current_app.config.get("TWILIO_WHATSAPP_SANDBOX_NUMBER")
        or current_app.config.get("TWILIO_SANDBOX_WHATSAPP_NUMBER")
        or current_app.config.get("TWILIO_WHATSAPP_NUMBER_SANDBOX")
        or "+14155238886"
    )
    value = str(raw or "").strip()
    if value.startswith("whatsapp:"):
        value = value.replace("whatsapp:", "", 1)
    return value or "+14155238886"


def _twilio_sandbox_join_phrase(payload: Mapping[str, Any]) -> str:
    return str(
        payload.get("join_phrase")
        or current_app.config.get("TWILIO_WHATSAPP_SANDBOX_JOIN_PHRASE")
        or current_app.config.get("TWILIO_SANDBOX_JOIN_PHRASE")
        or "join brief-yesterday"
    ).strip()


def _sandbox_quick_menu(tenant: TenantProfile) -> list[dict[str, Any]]:
    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    candidates = [
        cfg.get("quick_menu"),
        (cfg.get("builder_config") or {}).get("quick_menu") if isinstance(cfg.get("builder_config"), dict) else None,
        (cfg.get("widget") or {}).get("quick_menu") if isinstance(cfg.get("widget"), dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)][:8]
    return []


def _sandbox_demo_context(tenant: TenantProfile, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    sector = str(payload.get("sector") or tenant.vertical or ("gobierno" if tenant.tipo == "municipio" else "empresas")).strip()
    rubro = str(payload.get("rubro") or tenant.subvertical or tenant.vertical or tenant.tipo or "").strip()
    contract = build_demo_whatsapp_sandbox_contract(
        tenant_slug=tenant.slug,
        sector=sector,
        rubro=rubro,
        sandbox_number=_twilio_sandbox_number(),
        join_phrase=_twilio_sandbox_join_phrase(payload),
        source=str(payload.get("source") or "tenant_integrations_panel"),
        public_base_url=resolve_demo_public_frontend_base_url(current_app.config),
    )
    context = sandbox_context_from_contract(contract)
    context.update(
        {
            "sector": sector,
            "tenant_slug": tenant.slug,
            "rubro": rubro,
            "brief": str(payload.get("brief") or "Probar menu del tenant y crear un caso/pedido/reclamo").strip(),
            "test_message": str(payload.get("test_message") or "Hola, quiero probar el asistente").strip(),
            "quick_menu": payload.get("menu_preview") if isinstance(payload.get("menu_preview"), list) else _sandbox_quick_menu(tenant),
            "widget_config_endpoint": f"/api/public/tenants/{tenant.slug}/widget-config",
            "whatsapp_sandbox": contract,
        }
    )
    return context


@v2_saas_bp.route("/whatsapp/sandbox-session", methods=["OPTIONS"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/sandbox-session", methods=["OPTIONS"])
def whatsapp_sandbox_session_options_v2(tenant_slug: str | None = None):
    return _json_response({"ok": True, "contract_version": "whatsapp.sandbox_session.v1"})


@v2_saas_bp.route("/whatsapp/sandbox-session", methods=["POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/sandbox-session", methods=["POST"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def whatsapp_sandbox_session_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}

    sandbox_number = _twilio_sandbox_number()
    join_phrase = _twilio_sandbox_join_phrase(payload)
    demo_context = _sandbox_demo_context(tenant, payload)
    rubro = demo_context["rubro"]
    brief = demo_context["brief"]
    test_message = demo_context["test_message"]
    wa_number = "".join(ch for ch in sandbox_number if ch.isdigit())
    wa_deeplink = f"https://wa.me/{wa_number}?text={quote_plus(join_phrase)}"

    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    sandbox_sessions = cfg.get("whatsapp_sandbox_sessions")
    if not isinstance(sandbox_sessions, list):
        sandbox_sessions = []
    session_id = f"wsp_sandbox_{uuid.uuid4().hex[:12]}"
    sandbox_sessions.append(
        {
            "id": session_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "whatsapp": payload.get("whatsapp"),
            "join_phrase": join_phrase,
            "rubro": rubro,
            "brief": brief,
            "test_message": test_message,
            "source": payload.get("source") or "tenant_integrations_panel",
        }
    )
    cfg["whatsapp_sandbox_sessions"] = sandbox_sessions[-20:]
    tenant.configuracion = cfg
    flag_modified(tenant, "configuracion")
    db.session.commit()

    return _json_response(
        {
            "contract_version": "whatsapp.sandbox_session.v1",
            "ok": True,
            "tenant": _tenant_ref(tenant),
            "twilio": {
                "provider": "twilio_sandbox",
                "sandbox_number": f"whatsapp:{sandbox_number}",
                "display_number": "+1 (415) 523-8886" if wa_number == "14155238886" else sandbox_number,
                "join_phrase": join_phrase,
                "wa_deeplink": wa_deeplink,
            },
            "demo_context": {
                "tenant_slug": tenant.slug,
                "rubro": rubro,
                "brief": brief,
                "test_message": test_message,
                "quick_menu": demo_context["quick_menu"],
                "widget_config_endpoint": f"/api/public/tenants/{tenant.slug}/widget-config",
                "trial_policy": demo_context.get("trial_policy"),
                "supported_inputs": demo_context.get("supported_inputs"),
                "catalog": demo_context.get("catalog"),
                "surveys_votings": demo_context.get("surveys_votings"),
            },
            "whatsapp_sandbox": demo_context.get("whatsapp_sandbox"),
            "session": {
                "id": session_id,
                "mode": "copy_or_deeplink",
                "sends_real_message": False,
                "source": payload.get("source") or "tenant_integrations_panel",
            },
        }
    )


@v2_saas_bp.route("/whatsapp/sandbox-setup", methods=["OPTIONS"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/sandbox-setup", methods=["OPTIONS"])
def whatsapp_sandbox_setup_options_v2(tenant_slug: str | None = None):
    return _json_response({"ok": True, "contract_version": "whatsapp.sandbox_setup.v1"})


@v2_saas_bp.route("/whatsapp/sandbox-setup", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/sandbox-setup", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def whatsapp_sandbox_setup_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    payload = {
        "sector": request.args.get("sector"),
        "rubro": request.args.get("rubro"),
        "brief": request.args.get("brief"),
        "test_message": request.args.get("test_message"),
    }
    sandbox_number = _twilio_sandbox_number()
    join_phrase = _twilio_sandbox_join_phrase({})
    wa_number = "".join(ch for ch in sandbox_number if ch.isdigit())
    enabled = bool(sandbox_number and join_phrase)
    demo_context = _sandbox_demo_context(tenant, payload)

    return _json_response(
        {
            "contract_version": "whatsapp.sandbox_setup.v1",
            "tenant_slug": tenant.slug,
            "tenant": _tenant_ref(tenant),
            "provider": "twilio_whatsapp",
            "enabled": enabled,
            "sandbox": {
                "enabled": enabled,
                "join_number": f"whatsapp:{sandbox_number}",
                "display_number": "+1 (415) 523-8886" if wa_number == "14155238886" else sandbox_number,
                "join_phrase": join_phrase,
                "wa_deeplink": f"https://wa.me/{wa_number}?text={quote_plus(join_phrase)}",
                "qr_url": f"https://api.qrserver.com/v1/create-qr-code/?size=220x220&data={quote_plus(f'https://wa.me/{wa_number}?text={join_phrase}')}",
                "instructions": [
                    {"id": "save_number", "label": "Guarda el numero de prueba"},
                    {"id": "send_phrase", "label": "Envia la frase de activacion"},
                    {"id": "try_menu", "label": "Proba el menu del tenant"},
                ],
            },
            "demo_context": demo_context,
            "whatsapp_sandbox": demo_context.get("whatsapp_sandbox"),
            "test": {
                "endpoint": f"/api/v2/tenants/{tenant.slug}/whatsapp/sandbox-test",
                "method": "POST",
                "payload_template": {"to": "{whatsapp_number}", "message": "{message}"},
            }
            if enabled
            else None,
            "setup_checklist": []
            if enabled
            else [
                {
                    "id": "configure_twilio_sandbox",
                    "label": "Configurar numero y frase de Twilio Sandbox",
                    "required": True,
                }
            ],
            "frontend_contract": {
                "render_as": "whatsapp_sandbox_onboarding",
                "show_preview": True,
                "show_status_check": True,
            },
        }
    )


@v2_saas_bp.route("/whatsapp/sandbox-test", methods=["OPTIONS"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/sandbox-test", methods=["OPTIONS"])
def whatsapp_sandbox_test_options_v2(tenant_slug: str | None = None):
    return _json_response({"ok": True, "contract_version": "whatsapp.sandbox_test.v1"})


@v2_saas_bp.route("/whatsapp/sandbox-test", methods=["POST"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/whatsapp/sandbox-test", methods=["POST"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def whatsapp_sandbox_test_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    sandbox_number = _twilio_sandbox_number()
    join_phrase = _twilio_sandbox_join_phrase(payload)
    wa_number = "".join(ch for ch in sandbox_number if ch.isdigit())
    message = str(payload.get("message") or payload.get("test_message") or "Hola, quiero probar el asistente").strip()
    demo_context = _sandbox_demo_context(tenant, payload)

    return _json_response(
        {
            "contract_version": "whatsapp.sandbox_test.v1",
            "ok": True,
            "tenant": _tenant_ref(tenant),
            "mode": "copy_or_deeplink",
            "sends_real_message": False,
            "twilio": {
                "provider": "twilio_sandbox",
                "sandbox_number": f"whatsapp:{sandbox_number}",
                "join_phrase": join_phrase,
                "wa_deeplink": f"https://wa.me/{wa_number}?text={quote_plus(join_phrase)}",
            },
            "message_preview": {
                "to": payload.get("to"),
                "message": message,
                "copy_text": f"{join_phrase}\n\n{message}",
            },
            "demo_context": demo_context,
            "whatsapp_sandbox": demo_context.get("whatsapp_sandbox"),
            "next_action": "open_whatsapp_or_copy_instructions",
        }
    )


@v2_saas_bp.route("/superadmin/executive-summary", methods=["GET"])
@v2_saas_bp.route("/super-admin/executive-summary", methods=["GET"])
@token_requerido
@require_role("super_admin")
def executive_summary_v2(current_user):
    tenants = TenantProfile.query.order_by(TenantProfile.id.asc()).all()
    tenant_health = [_tenant_health_payload(tenant) for tenant in tenants]
    open_tickets = sum((item.get("metrics") or {}).get("open_tickets", 0) for item in tenant_health)
    overdue_tickets = sum((item.get("metrics") or {}).get("overdue_tickets", 0) for item in tenant_health)
    avg_health = round(sum((item.get("health") or {}).get("score", 0) for item in tenant_health) / len(tenant_health), 2) if tenant_health else 100.0
    risky = sorted(tenant_health, key=lambda item: (item["health"]["score"], item["metrics"]["overdue_tickets"]), reverse=False)[:10]

    return _json_response(
        {
            "contract_version": "superadmin.executive_summary.v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "tenants": len(tenants),
                "active_tenants": sum(1 for tenant in tenants if getattr(tenant, "is_active", True)),
                "avg_health_score": avg_health,
                "open_tickets": open_tickets,
                "overdue_tickets": overdue_tickets,
                "risky_tenants": len([item for item in tenant_health if item["health"]["status"] != "healthy"]),
            },
            "tenant_health": {
                "items": [
                    {
                        "tenant": item["tenant"],
                        "health": item["health"],
                        "metrics": item["metrics"],
                        "alerts": item["alerts"],
                    }
                    for item in tenant_health
                ],
                "top_risky": [
                    {"tenant": item["tenant"], "health": item["health"], "metrics": item["metrics"]}
                    for item in risky
                ],
            },
            "recommended_actions": [
                {
                    "kind": "review_risky_tenants",
                    "priority": "high" if overdue_tickets else "medium",
                    "message": "Revisar tenants con health bajo, tickets vencidos o integraciones incompletas.",
                }
            ]
            if tenants
            else [],
        }
    )


@v2_saas_bp.route("/superadmin/command-center", methods=["GET"])
@v2_saas_bp.route("/super-admin/command-center", methods=["GET"])
@token_requerido
@require_role("super_admin")
def superadmin_command_center_v2(current_user):
    start_date, end_date = _date_range_from_request(default_days=30)
    limit = max(1, min(int(request.args.get("limit", 50) or 50), 200))
    payload = _build_superadmin_command_center_payload(
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        viewer=current_user,
    )
    return _json_response(payload)


def _smoke_check(
    check_id: str,
    *,
    ok: bool,
    label: str,
    severity: str = "critical",
    details: Mapping[str, Any] | None = None,
    endpoint: str | None = None,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "label": label,
        "ok": bool(ok),
        "status": "pass" if ok else "fail",
        "severity": severity,
        "endpoint": endpoint,
        "details": dict(details or {}),
    }


def _routes_available(paths: list[str]) -> dict[str, bool]:
    registered = {str(rule.rule) for rule in current_app.url_map.iter_rules()}
    return {path: path in registered for path in paths}


def _e2e_flow_qa_guidance(flow_id: str, *, endpoint: str, surface: str) -> dict[str, Any]:
    default_guidance = {
        "frontend_entry": "/perfil",
        "manual_test_steps": [
            "Abrir el modulo operativo del tenant.",
            "Ejecutar el flujo completo con datos de prueba.",
            "Verificar que el evento queda visible para el equipo administrativo.",
        ],
        "acceptance_criteria": [
            "El usuario final recibe una respuesta clara y accionable.",
            "El admin ve el caso con historial, canal y siguiente accion.",
            "El flujo degrada con un estado explicito si falta una integracion.",
        ],
        "suggested_command": "python -m pytest tests/test_v2_saas_contracts.py -q",
    }
    guidance_by_flow: dict[str, dict[str, Any]] = {
        "gov_claim_text_to_tracking": {
            "frontend_entry": "/perfil?tab=tickets",
            "manual_test_steps": [
                "Enviar un reclamo por WhatsApp o widget con categoria y direccion.",
                "Confirmar el reclamo y abrir el link publico de seguimiento.",
                "Abrir el CRM y verificar que la conversacion queda en la bandeja de reclamos.",
            ],
            "acceptance_criteria": [
                "Se crea un ticket con codigo, pin y estado inicial.",
                "El link publico muestra resumen, timeline y canal de seguimiento.",
                "El admin puede leer el reclamo, responder y cambiar estado sin salir del CRM.",
            ],
            "suggested_command": "python -m pytest tests/test_v2_saas_contracts.py -q",
        },
        "claim_live_or_offline_helpdesk": {
            "frontend_entry": "/tracking/claim/{nro_ticket}#pin={pin}",
            "manual_test_steps": [
                "Abrir el seguimiento publico del reclamo desde el link seguro.",
                "Enviar una consulta desde la mesa de ayuda del ticket.",
                "Verificar en el CRM que el mensaje entra como conversacion del reclamo.",
            ],
            "acceptance_criteria": [
                "El mensaje publico exige pin o token seguro.",
                "Fuera de horario se guarda como offline y mantiene contexto del ticket.",
                "En horario de atencion queda listo para respuesta de un agente humano.",
            ],
            "suggested_command": "python -m pytest tests/test_v2_saas_contracts.py -q",
        },
        "pyme_catalog_order_checkout": {
            "frontend_entry": "/t/{tenant_slug}/market",
            "manual_test_steps": [
                "Abrir el marketplace publico del tenant.",
                "Buscar productos, agregar items al pedido o subir una nota de pedido.",
                "Confirmar el pedido y verificar que aparece en CRM/pedidos.",
            ],
            "acceptance_criteria": [
                "El catalogo muestra precio, stock, promociones o un estado vacio util.",
                "La nota de pedido se convierte en items sugeridos o en lead de compra.",
                "El checkout o contacto por WhatsApp devuelve confirmacion trazable.",
            ],
            "suggested_command": "python -m pytest tests/test_pedidos_from_file_marketplace.py tests/test_pyme_multimodal_flow.py -q",
        },
        "survey_vote_realtime": {
            "frontend_entry": "/perfil?tab=surveys",
            "manual_test_steps": [
                "Publicar una encuesta o votacion demo.",
                "Votar desde el link publico o QR.",
                "Verificar resultados en vivo, conteos y capas geograficas cuando existan datos.",
            ],
            "acceptance_criteria": [
                "Cada voto queda registrado una sola vez por identidad o sesion permitida.",
                "El panel actualiza resultados sin recargar toda la pagina.",
                "El estado publico diferencia encuesta activa, cerrada y sin datos.",
            ],
            "suggested_command": "python -m pytest tests/product_flow/test_surveys_live_vote_flow.py tests/test_v2_surveys.py -q",
        },
        "school_family_case": {
            "frontend_entry": "/perfil?tab=education",
            "manual_test_steps": [
                "Simular una familia consultando cuota, comprobante o tramite escolar.",
                "Adjuntar comprobante o mensaje desde WhatsApp/widget.",
                "Verificar que el caso queda asignable en el panel del colegio.",
            ],
            "acceptance_criteria": [
                "El agente pide solo los datos faltantes y respeta privacidad.",
                "El comprobante queda asociado al alumno, familia o caso administrativo.",
                "El equipo puede responder y cerrar el caso desde el CRM.",
            ],
            "suggested_command": "python -m pytest tests/test_v2_saas_contracts.py -q",
        },
        "finance_in_chat_transactional": {
            "frontend_entry": "/perfil?tab=transactions",
            "manual_test_steps": [
                "Iniciar alta, KYC, cobranza, pago o firma desde una conversacion.",
                "Abrir el webview seguro para completar datos sensibles.",
                "Confirmar que el backend recibe webhook server-to-server antes de actualizar estado.",
            ],
            "acceptance_criteria": [
                "Datos sensibles no quedan expuestos en mensajes de WhatsApp.",
                "El webview usa token firmado y estado verificable.",
                "El CRM muestra etapa, comprobante y proxima accion transaccional.",
            ],
            "suggested_command": "python -m pytest tests/test_v2_saas_contracts.py -q",
        },
        "finance_servicing_transfer_insurance": {
            "frontend_entry": "/perfil?tab=transactions",
            "manual_test_steps": [
                "Probar estado de cuenta, transferencia/remesa, seguro y financiacion.",
                "Verificar que cada journey tenga plantilla, webview y confirmacion backend.",
                "Revisar que el panel muestre trazabilidad por journey.",
            ],
            "acceptance_criteria": [
                "Cada journey financiero declara datos requeridos y riesgos.",
                "Las acciones criticas esperan confirmacion del servicio externo.",
                "El admin ve estado, historial y error recuperable si el proveedor falla.",
            ],
            "suggested_command": "python -m pytest tests/test_v2_saas_contracts.py -q",
        },
        "analytics_heatmap": {
            "frontend_entry": "/perfil?tab=analytics",
            "manual_test_steps": [
                "Abrir analitica operacional y mapa de calor.",
                "Filtrar por canal, estado, categoria y periodo.",
                "Verificar capas de tickets, pedidos, encuestas y estados sin datos.",
            ],
            "acceptance_criteria": [
                "El mapa no muestra una imagen estatica si no hay datos georreferenciados.",
                "Cada punto o zona de calor abre contexto operativo.",
                "El panel muestra frescura, fuente de datos y fallback cuando falta geocoding.",
            ],
            "suggested_command": "python -m pytest tests/test_v2_saas_contracts.py -q",
        },
    }
    selected = guidance_by_flow.get(flow_id, default_guidance)
    return {
        "frontend_entry": selected["frontend_entry"],
        "manual_test_steps": list(selected["manual_test_steps"]),
        "acceptance_criteria": list(selected["acceptance_criteria"]),
        "automation": {
            "safe_by_default": True,
            "live_side_effects": False,
            "uses_real_whatsapp": False,
            "surface": surface,
            "endpoint": endpoint,
            "suggested_command": selected["suggested_command"],
        },
    }


def _smoke_e2e_flow(
    flow_id: str,
    *,
    label: str,
    ready: bool,
    surface: str,
    endpoint: str,
    evidence: Mapping[str, Any] | None = None,
    next_action: str | None = None,
    qa_scenario_id: str | None = None,
    meta_flow_ready: bool | None = None,
) -> dict[str, Any]:
    status = "ready" if ready else "needs_attention"
    guidance = _e2e_flow_qa_guidance(flow_id, endpoint=endpoint, surface=surface)
    return {
        "id": flow_id,
        "label": label,
        "surface": surface,
        "ready": bool(ready),
        "status": status,
        "endpoint": endpoint,
        "qa_scenario_id": qa_scenario_id,
        "meta_flow_ready": bool(meta_flow_ready) if meta_flow_ready is not None else None,
        "evidence": dict(evidence or {}),
        "next_action": next_action or ("run_live_smoke" if ready else "complete_flow_contract"),
        "frontend_entry": guidance["frontend_entry"],
        "manual_test_steps": guidance["manual_test_steps"],
        "acceptance_criteria": guidance["acceptance_criteria"],
        "automation": guidance["automation"],
    }


def _build_production_e2e_readiness(
    *,
    tenant: TenantProfile | None,
    admin_payload: Mapping[str, Any] | None = None,
    marketplace: Mapping[str, Any] | None = None,
    whatsapp: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    admin_payload = admin_payload or {}
    marketplace = marketplace or {}
    whatsapp = whatsapp or {}
    qa_playbook = whatsapp.get("qa_playbook") if isinstance(whatsapp.get("qa_playbook"), Mapping) else {}
    scenarios = {
        str(item.get("id")): item
        for item in qa_playbook.get("scenarios", [])
        if isinstance(item, Mapping) and item.get("id")
    }
    webviews = whatsapp.get("webview_blueprint") if isinstance(whatsapp.get("webview_blueprint"), Mapping) else {}
    webview_summary = webviews.get("summary") if isinstance(webviews.get("summary"), Mapping) else {}
    flow_states = {
        str(item.get("id")): item
        for item in webviews.get("flows", [])
        if isinstance(item, Mapping) and item.get("id")
    }
    lead_summary = ((admin_payload.get("lead_capture") or {}).get("summary") or {}) if isinstance(admin_payload.get("lead_capture"), Mapping) else {}
    survey_summary = ((admin_payload.get("surveys_votings") or {}).get("summary") or {}) if isinstance(admin_payload.get("surveys_votings"), Mapping) else {}
    freshness = (admin_payload.get("operations") or {}).get("freshness") if isinstance(admin_payload.get("operations"), Mapping) else {}
    freshness_summary = freshness.get("summary") if isinstance(freshness, Mapping) and isinstance(freshness.get("summary"), Mapping) else {}
    market_summary = marketplace.get("summary") if isinstance(marketplace.get("summary"), Mapping) else {}
    commerce = whatsapp.get("commerce") if isinstance(whatsapp.get("commerce"), Mapping) else {}
    checkout = commerce.get("checkout_experience") if isinstance(commerce.get("checkout_experience"), Mapping) else {}
    finance_runtime = whatsapp.get("finance_transactional") if isinstance(whatsapp.get("finance_transactional"), Mapping) else {}
    finance_summary = finance_runtime.get("summary") if isinstance(finance_runtime.get("summary"), Mapping) else {}

    def scenario_ready(scenario_id: str) -> bool:
        scenario = scenarios.get(scenario_id) or {}
        meta_flow = scenario.get("meta_flow_coverage") if isinstance(scenario.get("meta_flow_coverage"), Mapping) else {}
        return bool(scenario and (meta_flow.get("ready") is True or scenario.get("ready") is True))

    def flow_meta_ready(flow_id: str) -> bool:
        flow = flow_states.get(flow_id) or {}
        if "meta_flow_blueprint_ready" in flow:
            return bool(flow.get("meta_flow_blueprint_ready"))
        meta = flow.get("meta_flow_blueprint") if isinstance(flow.get("meta_flow_blueprint"), Mapping) else {}
        return bool(meta.get("screens") and meta.get("data_contract"))

    flows = [
        _smoke_e2e_flow(
            "gov_claim_text_to_tracking",
            label="Municipio: reclamo por WhatsApp hasta seguimiento publico",
            surface="municipios_gobiernos",
            ready=scenario_ready("gov_claim_text_to_tracking") and bool(lead_summary.get("total_recent") is not None),
            endpoint="/api/public/tracking/experience?kind=claim&code={code}",
            qa_scenario_id="gov_claim_text_to_tracking",
            meta_flow_ready=flow_meta_ready("claim_tracking_helpdesk"),
            evidence={
                "tickets_recent": lead_summary.get("total_recent"),
                "open_tickets": lead_summary.get("open"),
                "webview_flow": "claim_tracking_helpdesk",
                "tracking_contract": (whatsapp.get("tracking") or {}).get("contract_version") if isinstance(whatsapp.get("tracking"), Mapping) else None,
            },
            next_action="run_whatsapp_claim_text_to_tracking_and_open_public_status",
        ),
        _smoke_e2e_flow(
            "claim_live_or_offline_helpdesk",
            label="Mesa de ayuda: chat en vivo u offline del reclamo",
            surface="municipios_gobiernos",
            ready=flow_meta_ready("claim_tracking_helpdesk") and bool(_routes_available(["/api/public/tracking/claims/<int:ticket_id>/messages"]).get("/api/public/tracking/claims/<int:ticket_id>/messages")),
            endpoint="/api/public/tracking/claims/{ticket_id}/messages",
            qa_scenario_id="gov_claim_text_to_tracking",
            meta_flow_ready=flow_meta_ready("claim_tracking_helpdesk"),
            evidence={
                "pin_required": True,
                "public_message_endpoint": "/api/public/tracking/claims/{ticket_id}/messages",
                "working_hours_configurable": True,
            },
            next_action="validate_public_claim_message_reaches_admin_inbox",
        ),
        _smoke_e2e_flow(
            "pyme_catalog_order_checkout",
            label="Pyme: catalogo, carrito, pedido y checkout",
            surface="pymes_empresas",
            ready=scenario_ready("pyme_catalog_order_checkout")
            and (bool(checkout.get("ready")) or int(market_summary.get("products") or 0) > 0),
            endpoint="/api/v2/catalog/quality",
            qa_scenario_id="pyme_catalog_order_checkout",
            meta_flow_ready=flow_meta_ready("catalog_order_builder"),
            evidence={
                "products": market_summary.get("products"),
                "ready_to_sell": market_summary.get("ready_to_sell"),
                "checkout_ready": checkout.get("ready"),
                "webview_flow": "catalog_order_builder",
            },
            next_action="run_catalog_order_checkout_smoke_with_demo_tenant",
        ),
        _smoke_e2e_flow(
            "survey_vote_realtime",
            label="Encuestas y votaciones con resultado en vivo",
            surface="gobiernos_empresas_colegios",
            ready=scenario_ready("survey_vote_realtime")
            and (int(survey_summary.get("active") or 0) > 0 or int(survey_summary.get("responses") or 0) >= 0),
            endpoint="/api/v2/surveys",
            qa_scenario_id="survey_vote_realtime",
            meta_flow_ready=flow_meta_ready("survey_vote"),
            evidence={
                "surveys": survey_summary.get("surveys"),
                "active": survey_summary.get("active"),
                "responses": survey_summary.get("responses"),
                "webview_flow": "survey_vote",
            },
            next_action="publish_demo_survey_and_verify_live_results_heatmap",
        ),
        _smoke_e2e_flow(
            "school_family_case",
            label="Colegios: familia, cuota, comprobante y caso administrativo",
            surface="colegios_educacion",
            ready=scenario_ready("school_family_case") and bool((admin_payload.get("education") or {}).get("profile") is not None),
            endpoint="/api/v2/tenant/admin-experience",
            qa_scenario_id="school_family_case",
            meta_flow_ready=flow_meta_ready("school_payment_receipt") or flow_meta_ready("order_checkout"),
            evidence={
                "education_profile": (admin_payload.get("education") or {}).get("profile") if isinstance(admin_payload.get("education"), Mapping) else None,
                "webview_flow": "school_payment_receipt",
            },
            next_action="run_school_payment_receipt_and_family_case_demo",
        ),
        _smoke_e2e_flow(
            "finance_in_chat_transactional",
            label="Finance in-chat: alta, KYC, cobranza, pago y firma",
            surface="finanzas_pymes_gobiernos_colegios",
            ready=flow_meta_ready("finance_onboarding_kyc")
            and flow_meta_ready("finance_credit_collection_signature")
            and "financial_services" in ((whatsapp.get("template_blueprint") or {}).get("operational_template_groups") or {}),
            endpoint="/api/v2/whatsapp/experience",
            qa_scenario_id="finance_onboarding_collection_signature",
            meta_flow_ready=flow_meta_ready("finance_onboarding_kyc") and flow_meta_ready("finance_credit_collection_signature"),
            evidence={
                "templates_group": "financial_services",
                "onboarding_flow": "finance_onboarding_kyc",
                "operation_flow": "finance_credit_collection_signature",
                "checkout_ready": checkout.get("ready"),
                "finance_runtime": finance_runtime.get("contract_version"),
                "ready_journeys": finance_summary.get("ready_journeys"),
            },
            next_action="run_finance_onboarding_collection_signature_smoke",
        ),
        _smoke_e2e_flow(
            "finance_servicing_transfer_insurance",
            label="Finance avanzado: estado de cuenta, remesas, seguros y financiacion",
            surface="finanzas_banca_seguros",
            ready=bool(finance_runtime.get("contract_version") == "finance.transactional_whatsapp.v1")
            and int(finance_summary.get("journeys") or 0) >= 6
            and flow_meta_ready("finance_account_servicing")
            and flow_meta_ready("finance_remittance_transfer")
            and flow_meta_ready("finance_insurance_claim")
            and flow_meta_ready("finance_fee_financing_tax"),
            endpoint="/api/v2/whatsapp/experience",
            qa_scenario_id="finance_account_servicing",
            meta_flow_ready=flow_meta_ready("finance_account_servicing")
            and flow_meta_ready("finance_remittance_transfer")
            and flow_meta_ready("finance_insurance_claim")
            and flow_meta_ready("finance_fee_financing_tax"),
            evidence={
                "finance_runtime": finance_runtime.get("contract_version"),
                "journeys": finance_summary.get("journeys"),
                "ready_journeys": finance_summary.get("ready_journeys"),
                "webview_flows": [
                    "finance_account_servicing",
                    "finance_remittance_transfer",
                    "finance_insurance_claim",
                    "finance_fee_financing_tax",
                ],
            },
            next_action="run_finance_servicing_transfer_insurance_smoke",
        ),
        _smoke_e2e_flow(
            "analytics_heatmap",
            label="Analitica: mapa de calor, territorios y frescura operacional",
            surface="analytics_maps",
            ready=bool(freshness_summary.get("can_render_heatmap")),
            endpoint="/api/v2/analytics/operations/heatmap",
            evidence={
                "can_render_heatmap": freshness_summary.get("can_render_heatmap"),
                "freshness_status": freshness.get("status") if isinstance(freshness, Mapping) else None,
                "realtime_sources": (whatsapp.get("tracking") or {}).get("realtime_sources") if isinstance(whatsapp.get("tracking"), Mapping) else None,
            },
            next_action="collect_geocoded_claims_orders_and_render_heatmap_layers",
        ),
    ]
    ready_count = sum(1 for item in flows if item.get("ready"))
    return {
        "contract_version": "platform.e2e_flow_readiness.v1",
        "tenant": _tenant_ref(tenant) if tenant else None,
        "status": "ready" if ready_count == len(flows) and flows else "needs_attention",
        "summary": {
            "total": len(flows),
            "ready": ready_count,
            "needs_attention": len(flows) - ready_count,
            "meta_flow_ready": sum(1 for item in flows if item.get("meta_flow_ready")),
            "qa_scenarios": len(scenarios),
            "webview_flows": webview_summary.get("flows_total"),
        },
        "flows": flows,
        "frontend_contract": {
            "render_as": "e2e_flow_readiness_grid",
            "recommended_views": ["flow_cards", "evidence", "next_actions", "qa_scenario_links"],
        },
    }


def _resolve_smoke_tenant(current_user: User, tenant_slug: str | None = None) -> tuple[TenantProfile | None, Any]:
    if is_authorized_superadmin_user(current_user):
        resolved_slug = tenant_slug or _tenant_slug_from_request()
        if resolved_slug:
            tenant = TenantProfile.query.filter_by(slug=resolved_slug).first()
            if tenant:
                return tenant, None
            return None, _error_response("Tenant no encontrado", 404, "tenant_not_found", "check_tenant_slug")
        tenant = TenantProfile.query.order_by(TenantProfile.created_at.desc()).first()
        if tenant:
            return tenant, None
        return None, None
    return _resolve_tenant_or_error(current_user, tenant_slug)


@v2_saas_bp.route("/production-smoke", methods=["GET"])
@v2_saas_bp.route("/platform/production-smoke", methods=["GET"])
@v2_saas_bp.route("/tenants/<string:tenant_slug>/production-smoke", methods=["GET"])
@cutover_writer_view
@token_requerido
@require_role("admin", "super_admin")
def production_smoke_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_smoke_tenant(current_user, tenant_slug)
    if error:
        return error

    start_date, end_date = _date_range_from_request(default_days=7)
    required_routes = [
        "/api/public/widget-config",
        "/api/v2/demo/session",
        "/ask/pyme",
        "/ask/municipio",
        "/api/ask/pyme",
        "/api/ask/municipio",
        "/api/public/realtime/voice-capabilities",
        "/api/v2/inbox/omnichannel",
        "/api/v2/tenant/admin-experience",
        "/api/v2/whatsapp/experience",
        "/api/v2/catalog/quality",
        "/api/public/tracking/experience",
    ]
    route_status = _routes_available(required_routes)
    e2e_readiness = _build_production_e2e_readiness(tenant=None)
    checks = [
        _smoke_check(
            "routes_registered",
            ok=all(route_status.values()),
            label="Rutas criticas registradas",
            endpoint="flask.url_map",
            details={"routes": route_status},
        )
    ]

    try:
        from routes.public_resolver import _platform_widget_config_payload

        widget_payload = _platform_widget_config_payload()
        onboarding = widget_payload.get("onboarding") or {}
        quick_menu = widget_payload.get("quick_menu") or []
        realtime = widget_payload.get("realtime") or {}
        checks.append(
            _smoke_check(
                "widget_platform_onboarding",
                ok=(
                    widget_payload.get("contract_version") == "public.widget_config.v1"
                    and (widget_payload.get("tenant") or {}).get("slug") == "chatboc-platform"
                    and onboarding.get("mode") == "platform_sector_selector"
                    and len(quick_menu) >= 3
                ),
                label="Widget landing selector plataforma",
                endpoint="/api/public/widget-config",
                details={
                    "tenant": widget_payload.get("tenant"),
                    "onboarding_mode": onboarding.get("mode"),
                    "quick_menu_count": len(quick_menu),
                },
            )
        )
        checks.append(
            _smoke_check(
                "socket_disabled_for_landing",
                ok=not bool(realtime.get("socket_enabled")) or bool(realtime.get("socket_url")),
                label="Socket.IO no se abre sin contrato valido",
                endpoint="/api/public/widget-config",
                details={"realtime": realtime, "visibility_rules": widget_payload.get("visibility_rules")},
            )
        )
    except Exception as exc:
        checks.append(
            _smoke_check(
                "widget_platform_onboarding",
                ok=False,
                label="Widget landing selector plataforma",
                endpoint="/api/public/widget-config",
                details={"error": str(exc)},
            )
        )

    if tenant:
        marketplace = _marketplace_ops_summary(tenant)
        admin_payload = _build_tenant_admin_experience_payload(
            tenant,
            start_date=start_date,
            end_date=end_date,
            app_config=current_app.config,
            viewer=current_user,
        )
        whatsapp = build_whatsapp_experience(tenant, app_config=current_app.config)
        e2e_readiness = _build_production_e2e_readiness(
            tenant=tenant,
            admin_payload=admin_payload,
            marketplace=marketplace,
            whatsapp=whatsapp,
        )
        freshness = (admin_payload.get("operations") or {}).get("freshness") or {}
        first_ticket = TenantTicket.query.filter_by(tenant_id=tenant.id).order_by(TenantTicket.updated_at.desc()).first()
        live_chat_status = _tenant_inbox_live_chat_status(tenant)
        inbox_item = (
            _inbox_ticket_payload(first_ticket, tenant=tenant, live_chat_status=live_chat_status, actor=current_user)
            if first_ticket
            else None
        )
        checks.extend(
            [
                _smoke_check(
                    "tenant_admin_experience",
                    ok=admin_payload.get("contract_version") == "tenant.admin_experience.v1"
                    and bool(admin_payload.get("modules"))
                    and isinstance(((freshness.get("summary") or {}).get("can_render_heatmap")), bool),
                    label="Tenant Admin OS listo",
                    endpoint="/api/v2/tenant/admin-experience",
                    details={
                        "modules": [item.get("id") for item in admin_payload.get("modules") or []],
                        "can_render_heatmap": (freshness.get("summary") or {}).get("can_render_heatmap"),
                    },
                ),
                _smoke_check(
                    "catalog_quality",
                    ok=marketplace.get("quality", {}).get("contract_version") == "catalog.quality.v1"
                    and "media_capabilities" in marketplace,
                    label="Marketplace quality e imagenes",
                    endpoint="/api/v2/catalog/quality",
                    details={"summary": marketplace.get("summary")},
                ),
                _smoke_check(
                    "whatsapp_operations",
                    ok=whatsapp.get("contract_version") == "whatsapp.experience.v1"
                    and "conversation_intelligence" in whatsapp
                    and "tracking" in whatsapp,
                    label="WhatsApp Operations Hub",
                    endpoint="/api/v2/whatsapp/experience",
                    details={
                        "channel_enabled": (whatsapp.get("channel") or {}).get("enabled"),
                        "voice_enabled": ((whatsapp.get("conversation_intelligence") or {}).get("voice_calls") or {}).get("enabled"),
                    },
                ),
                _smoke_check(
                    "inbox_360",
                    ok=not first_ticket
                    or (
                        bool(inbox_item)
                        and "timeline" in inbox_item
                        and "sla" in inbox_item
                        and "allowed_actions" in inbox_item
                        and "source_metadata" in inbox_item
                    ),
                    label="Inbox 360 drawer contract",
                    endpoint="/api/v2/inbox/omnichannel",
                    details={"has_ticket": bool(first_ticket), "ticket_id": getattr(first_ticket, "id", None)},
                ),
            ]
        )

    failed = [item for item in checks if not item.get("ok")]
    warnings = [item for item in failed if item.get("severity") != "critical"]
    critical = [item for item in failed if item.get("severity") == "critical"]
    status = "pass" if not failed else "warning" if warnings and not critical else "fail"
    payload = {
        "contract_version": "platform.production_smoke.v1",
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant": _tenant_ref(tenant) if tenant else None,
        "summary": {
            "total": len(checks),
            "passed": len([item for item in checks if item.get("ok")]),
            "failed": len(failed),
            "critical_failed": len(critical),
            "e2e_flows_total": (e2e_readiness.get("summary") or {}).get("total"),
            "e2e_flows_ready": (e2e_readiness.get("summary") or {}).get("ready"),
        },
        "checks": checks,
        "e2e_flow_readiness": e2e_readiness,
        "frontend_contract": {
            "render_as": "production_smoke_report",
            "recommended_refresh_seconds": 120,
            "fail_http_query_param": "fail_http=1",
            "show_e2e_flow_readiness": True,
            "show_flow_evidence": True,
        },
    }
    http_status = 500 if status == "fail" and request.args.get("fail_http") == "1" else 200
    return _json_response(payload, http_status)


@v2_saas_bp.route("/notifications/hooks", methods=["GET", "POST"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def notification_hooks_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error

    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    hooks = cfg.get("notification_hooks") if isinstance(cfg.get("notification_hooks"), dict) else {}

    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        hooks = {
            "preferences": payload.get("preferences") if isinstance(payload.get("preferences"), dict) else hooks.get("preferences", {}),
            "triggers": payload.get("triggers") if isinstance(payload.get("triggers"), list) else hooks.get("triggers", []),
            "delivery": payload.get("delivery") if isinstance(payload.get("delivery"), dict) else hooks.get("delivery", {}),
        }
        cfg["notification_hooks"] = hooks
        tenant.configuracion = cfg
        db.session.add(tenant)
        db.session.commit()

    delivery_status = _notification_delivery_status(tenant.id, period_days=max(1, min(int(request.args.get("period_days", 7) or 7), 90)))
    return _json_response(
        {
            "contract_version": "notifications.hooks.v1",
            "tenant": _tenant_ref(tenant),
            "preferences": hooks.get("preferences", {}),
            "triggers": hooks.get("triggers", []),
            "delivery": hooks.get("delivery", {}),
            "templates": _templates_payload(tenant.id),
            "delivery_status": delivery_status,
        }
    )


@v2_saas_bp.route("/notifications/delivery-status", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def notification_delivery_status_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error
    period_days = max(1, min(int(request.args.get("period_days", 7) or 7), 90))
    return _json_response(
        {
            "contract_version": "notifications.delivery_status.v1",
            "tenant": _tenant_ref(tenant),
            **_notification_delivery_status(tenant.id, period_days=period_days),
        }
    )


def _operational_queue_tenant_conflict(tenant: TenantProfile, current_user: User):
    for key in ("tenant_slug", "tenant"):
        if len(request.args.getlist(key)) > 1:
            return _error_response(
                f"{key} no puede repetirse",
                403,
                "tenant_scope_conflict",
                "send_one_tenant_scope",
            )

    requested_slugs = {
        slug
        for raw_value in (
            request.headers.get("X-Tenant-Slug"),
            request.headers.get("X-Tenant"),
            request.args.get("tenant_slug"),
            request.args.get("tenant"),
        )
        if (slug := first_specific_tenant_slug(raw_value))
    }
    if len(requested_slugs) > 1 or (
        requested_slugs
        and (tenant.slug or "").strip().lower() not in requested_slugs
    ):
        return _error_response(
            "Los identificadores de tenant no coinciden",
            403,
            "tenant_scope_conflict",
            "send_one_tenant_scope",
        )

    actor_tenant_id = getattr(current_user, "tenant_id", None)
    actor_tenant_slug = str(getattr(current_user, "tenant_slug", None) or "").strip().lower()
    if actor_tenant_id is not None and actor_tenant_slug:
        try:
            actor_tenant_id = int(actor_tenant_id)
        except (TypeError, ValueError):
            actor_tenant_id = None
        actor_tenant = db.session.get(TenantProfile, actor_tenant_id) if actor_tenant_id else None
        if actor_tenant is None or actor_tenant_slug != str(actor_tenant.slug or "").strip().lower():
            return _error_response(
                "La membresia persistida del actor es contradictoria",
                403,
                "actor_tenant_scope_conflict",
                "repair_actor_tenant_membership",
            )

    raw_tenant_ids = request.args.getlist("tenant_id")
    if len(raw_tenant_ids) > 1:
        return _error_response(
            "tenant_id no puede repetirse",
            403,
            "tenant_scope_conflict",
            "send_one_tenant_scope",
        )
    if raw_tenant_ids:
        try:
            requested_tenant_id = int(raw_tenant_ids[0])
        except (TypeError, ValueError):
            return _error_response(
                "tenant_id no coincide con el tenant solicitado",
                403,
                "tenant_scope_conflict",
                "send_matching_tenant_scope",
            )
        if requested_tenant_id <= 0 or requested_tenant_id != tenant.id:
            return _error_response(
                "tenant_id no coincide con el tenant solicitado",
                403,
                "tenant_scope_conflict",
                "send_matching_tenant_scope",
            )
    return None


@v2_saas_bp.route("/inbox/operational-queue", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def operational_queue_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error

    conflict = _operational_queue_tenant_conflict(tenant, current_user)
    if conflict:
        return conflict

    rate_limit = None
    try:
        queue_request = parse_queue_request(request.args)
        rate_limit = enforce_operational_queue_rate_limit(
            tenant_id=tenant.id,
            actor_id=current_user.id,
        )
        payload = build_operational_queue(
            tenant=tenant,
            actor=current_user,
            queue_request=queue_request,
        )
    except OperationalQueueGuardError as exc:
        response = _json_response(
            {
                "contract_version": "shared.error.v1",
                "status_code": exc.status_code,
                "reason_code": exc.reason_code,
                "retryable": exc.retryable,
                "action_hint": exc.action_hint,
                "error": {"code": exc.status_code, "message": str(exc)},
                "message": str(exc),
                "details": exc.details,
            },
            exc.status_code,
        )
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        return attach_operational_queue_rate_limit_headers(
            response,
            exc.rate_limit or rate_limit,
            retry_after_seconds=exc.retry_after_seconds,
        )
    except OperationalQueueError as exc:
        return _error_response(
            str(exc),
            exc.status_code,
            exc.reason_code,
            exc.action_hint,
        )

    response = _json_response(payload)
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    return attach_operational_queue_rate_limit_headers(response, rate_limit)


@v2_saas_bp.route("/inbox/omnichannel", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "supervisor", "manager", "super_admin")
def omnichannel_inbox_v2(current_user):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error

    limit = max(1, min(int(request.args.get("limit", 50) or 50), 200))
    tenant_ticket_query = apply_employee_ticket_category_scope(
        TenantTicket.query.filter_by(tenant_id=tenant.id),
        current_user,
        TenantTicket,
    )
    tenant_tickets = (
        tenant_ticket_query
        .order_by(TenantTicket.updated_at.desc())
        .limit(limit)
        .all()
    )
    legacy_claim_query = apply_employee_ticket_category_scope(
        _legacy_claim_query_for_tenant(tenant),
        current_user,
        MunicipioTicket,
    )
    legacy_claims = legacy_claim_query.order_by(MunicipioTicket.ultima_actividad.desc()).limit(limit).all()
    live_chat_status = _tenant_inbox_live_chat_status(tenant)
    artifact_map = _inbox_artifact_event_map(
        tenant_id=tenant.id,
        identities=(
            [("TenantTicket", ticket.id) for ticket in tenant_tickets]
            + [("MunicipioTicket", ticket.id) for ticket in legacy_claims]
        ),
    )
    items = [
        _inbox_ticket_payload(
            ticket, tenant=tenant, live_chat_status=live_chat_status, actor=current_user,
            artifact_events=artifact_map.get(("TenantTicket", ticket.id), []),
        )
        for ticket in tenant_tickets
    ]
    items.extend(
        _legacy_claim_inbox_payload(
            ticket, tenant=tenant, live_chat_status=live_chat_status, actor=current_user,
            artifact_events=artifact_map.get(("MunicipioTicket", ticket.id), []),
        )
        for ticket in legacy_claims
    )
    items.sort(key=_inbox_sort_key, reverse=True)
    items = items[:limit]

    return _json_response(
        {
            "contract_version": "inbox.omnichannel.v1",
            "tenant": _tenant_ref(tenant),
            "live_chat": _inbox_live_chat_contract(live_chat_status),
            "items": items,
            "summary": {
                "total": len(items),
                "open": len([item for item in items if str(item.get("status") or "").lower() not in _CLOSED_TICKET_STATES]),
                "unassigned": len([item for item in items if not item.get("assignee")]),
                "queued_live_chat": len(
                    [item for item in items if ((item.get("live_chat") or {}).get("channel_state") == "queued")]
                ),
            },
            "frontend_contract": {
                "render_as": "omnichannel_inbox",
                "detail_endpoint_template": "/api/v2/inbox/omnichannel/{ticket_id}",
                "drawer_contract": "inbox.omnichannel.detail.v1",
                "live_chat_contract": "inbox.live_chat_channel.v1",
            },
        }
    )


def _legacy_claim_query_for_tenant(tenant: TenantProfile):
    return scoped_municipio_ticket_query(tenant)


def _legacy_claim_for_tenant(tenant: TenantProfile, ticket_id: int) -> MunicipioTicket | None:
    return _legacy_claim_query_for_tenant(tenant).filter(MunicipioTicket.id == ticket_id).first()


def _inbox_sort_key(item: Mapping[str, Any]) -> datetime:
    for key in ("updated_at", "created_at"):
        value = item.get(key)
        if not value:
            continue
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return datetime.min.replace(tzinfo=timezone.utc)


def _attachment_items(extra: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = (
        extra.get("attachments")
        or extra.get("attachmentInfo")
        or extra.get("attachment_info")
        or extra.get("source_attachment")
        or extra.get("sourceAttachment")
        or extra.get("uploaded_file_info")
        or extra.get("files")
        or []
    )
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        mime_type = item.get("mimeType") or item.get("mime_type") or item.get("content_type")
        normalized = serialize_attachment_for_delivery(
            {
                **item,
                "id": item.get("id") or item.get("key") or f"attachment-{index + 1}",
                "name": item.get("name") or item.get("filename") or item.get("file_name") or f"Adjunto {index + 1}",
                "mime_type": mime_type,
                "size": item.get("size") or item.get("bytes"),
                "url": item.get("url") or item.get("file_url") or item.get("public_url"),
            }
        )
        normalized.update(
            {
                "kind": item.get("kind") or ("image" if str(mime_type or "").startswith("image/") else "file"),
                "source": item.get("source") or item.get("origin") or "chat_attachment",
                "origin": item.get("origin") or item.get("source") or "chat_attachment",
                "status": item.get("status") or "ready",
            }
        )
        for key in ("flow_id", "interaction_id"):
            if item.get(key) is not None:
                normalized[key] = item.get(key)
        items.append(normalized)
    return items


def _timeline_items(extra: Mapping[str, Any], *, limit: int = 30) -> list[dict[str, Any]]:
    comments = extra.get("comments") if isinstance(extra.get("comments"), list) else []
    timeline = []
    for comment in comments[-limit:]:
        if not isinstance(comment, dict):
            continue
        origin = comment.get("origin") or ("admin_panel" if comment.get("visibility") == "internal" else "public_tracking")
        timeline.append(
            {
                "id": comment.get("id"),
                "type": comment.get("type") or "message",
                "origin": origin,
                "body": comment.get("body") or "",
                "content_source": comment.get("content_source") or "operator_free_form",
                "visibility": comment.get("visibility") or "public",
                "created_at": comment.get("created_at"),
                "actor": comment.get("actor") if isinstance(comment.get("actor"), dict) else None,
                "action": comment.get("action"),
                "attachments": _attachment_items(comment),
            }
        )
    return timeline


def _timeline_pending_customer_response(timeline: list[dict[str, Any]]) -> tuple[int, str | None]:
    latest_team_index = -1
    pending: list[dict[str, Any]] = []
    for index, item in enumerate(timeline):
        origin = str(item.get("origin") or "").strip().lower()
        actor = item.get("actor") if isinstance(item.get("actor"), dict) else {}
        actor_type = str(actor.get("type") or actor.get("role") or "").strip().lower()
        visibility = str(item.get("visibility") or "").strip().lower()
        if origin in _INBOX_TEAM_ORIGINS or actor_type in {"agent", "admin", "empleado", "team"} or visibility == "internal":
            latest_team_index = index
            pending = []
            continue
        body = str(item.get("body") or "").strip()
        if body and index > latest_team_index:
            pending.append(item)
    return len(pending), (pending[0].get("created_at") if pending else None)


def _ticket_live_chat_queue_signals(
    *,
    status: Any,
    timeline: list[dict[str, Any]],
    handoff: Mapping[str, Any] | None = None,
) -> tuple[bool, int, str | None]:
    pending_count, pending_since = _timeline_pending_customer_response(timeline)
    normalized_status = str(status or "").strip().lower()
    handoff_status = str((handoff or {}).get("status") or "").strip().lower()
    queued = (
        normalized_status in _LIVE_CHAT_QUEUE_STATES
        or handoff_status in {"requested", "pending", "queued", "waiting_agent", "esperando_agente_en_vivo"}
        or pending_count > 0
    )
    return queued, pending_count, pending_since


def _ticket_sla_payload(ticket: TenantTicket, extra: Mapping[str, Any]) -> dict[str, Any]:
    sla = extra.get("sla") if isinstance(extra.get("sla"), dict) else {}
    evaluation = evaluate_ticket_sla(ticket, sla_override=sla)
    return {
        "contract_version": evaluation["contract_version"],
        "status": evaluation["state"],
        "state": evaluation["state"],
        "known": evaluation["known"],
        "unknown": evaluation["unknown"],
        "overdue": evaluation["overdue"],
        "priority": extra.get("priority") or "medium",
        "first_response_due_at": sla.get("first_response_due_at"),
        "resolution_due_at": sla.get("resolution_due_at"),
        "next_update_due_at": sla.get("next_update_due_at"),
        "paused": bool(sla.get("paused")),
        "breached_clocks": evaluation["breached_clocks"],
        "warning_clocks": evaluation["warning_clocks"],
        "unknown_clocks": evaluation["unknown_clocks"],
        "clocks": evaluation["clocks"],
    }


def _handoff_lifecycle_state(handoff: Mapping[str, Any] | None) -> str:
    if not isinstance(handoff, Mapping):
        return "idle"
    status = str(handoff.get("status") or "").strip().lower()
    if not status or status in _HANDOFF_TERMINAL_STATES:
        return "idle"
    if status == "requested":
        return "requested"
    if status in _HANDOFF_QUEUED_STATES:
        return "queued"
    if status == "accepted":
        return "accepted"
    return "invalid"


def _handoff_actor(user: User) -> dict[str, Any]:
    return {"id": user.id, "name": user.name}


def _handoff_action_contracts(
    *,
    endpoint: str,
    handoff: Mapping[str, Any] | None,
    payload_defaults: Mapping[str, Any] | None = None,
    actor: User | None = None,
    assignee_id: Any = None,
) -> list[dict[str, Any]]:
    state = _handoff_lifecycle_state(handoff)
    defaults = dict(payload_defaults or {})
    spec = {
        "idle": ("handoff", "Derivar a una persona", {**defaults, "channel": "operator"}),
        "requested": ("accept_handoff", "Tomar conversación", defaults),
        "queued": ("accept_handoff", "Tomar conversación", defaults),
        "accepted": ("resume_ai", "Devolver a IA", defaults),
    }.get(state)
    if not spec:  # Unknown persisted states expose no lifecycle mutation.
        return []
    action_id, label, action_defaults = spec
    action = {
        "id": action_id,
        "label": label,
        "method": "POST",
        "endpoint": endpoint,
        "requires": [],
        "payload_defaults": action_defaults,
        "delivery_mode": "internal_event",
        "external_dispatch": False,
    }
    is_municipio_claim = defaults.get("source_model") == "MunicipioTicket"
    if is_municipio_claim and action_id == "handoff":
        action["requires"] = ["channel", "reason", "idempotency_key_header"]
        action["idempotency"] = {
            "contract_version": "municipio_ticket.handoff_idempotency.v1",
            "required_header": "Idempotency-Key",
            "body_fallback": False,
            "same_key_same_payload": "replay",
            "same_key_different_payload": "conflict_409",
            "raw_value_persisted": False,
        }
        action["ledger"] = {
            "contract_version": MunicipioTicketHandoffEvent.CONTRACT_VERSION,
            "normalized_event": True,
            "projection_contract_version": (
                MunicipioTicketHandoffEvent.PROJECTION_CONTRACT_VERSION
            ),
            "external_dispatch": False,
        }
    elif is_municipio_claim and action_id in {"accept_handoff", "resume_ai"}:
        # These pre-existing lifecycle mutations still use the historical
        # datos_extra/comment projection.  Do not advertise normalized or
        # idempotent persistence until a dedicated follow-up event exists.
        action["ledger"] = {
            "contract_version": "municipio_ticket.handoff_follow_up.v1",
            "normalized_event": False,
            "mode": "legacy_projection_only",
            "idempotency_supported": False,
            "external_dispatch": False,
        }
    if action_id == "accept_handoff":
        action["authorization"] = {
            "mode": "handoff_recipient",
            "requires_category_scope": True,
        }
        action["ownership_transfer"] = {
            "contract_version": "inbox.handoff_assignment.v1",
            "allowed_states": ["requested", "queued"],
            "atomic": True,
        }
    if action_id in _OPERATIONAL_OWNERSHIP_ACTIONS:
        _apply_operational_ownership_contract(action, actor, assignee_id)
    return [action]


def _validate_handoff_transition(action: str, state: str):
    allowed = {
        "handoff": {"idle"},
        "accept_handoff": {"requested", "queued"},
        "resume_ai": {"accepted"},
    }
    if action in allowed and state not in allowed[action]:
        return _error_response(
            "La transicion de handoff no es valida para el estado actual",
            409,
            "invalid_handoff_transition",
            "refresh_inbox",
        )
    return None


def _normalize_handoff_channel(value: Any) -> str | None:
    channel = str(value or "operator").strip().lower()
    channel = {
        "agent": "operator",
        "human": "operator",
        "humano": "operator",
        "operador": "operator",
        "livechat": "live_chat",
    }.get(channel, channel)
    return channel if channel in _HANDOFF_SUPPORTED_CHANNELS else None


def _apply_handoff_transition(
    extra: dict[str, Any],
    *,
    action: str,
    actor: User,
    occurred_at: str,
    channel: str | None = None,
    reason: Any = None,
    transferred_from: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    handoff = deepcopy(dict(extra.get("handoff") or {}))
    actor_ref = _handoff_actor(actor)
    if action == "handoff":
        handoff = {
            "contract_version": "inbox.handoff.v1",
            "channel": channel or "operator",
            "status": "requested",
            "requested_at": occurred_at,
            "requested_by": actor_ref,
            "reason": str(reason).strip()[:500] if reason is not None and str(reason).strip() else None,
        }
    elif action == "accept_handoff":
        handoff.update(
            contract_version="inbox.handoff.v1",
            status="accepted",
            accepted_at=occurred_at,
            accepted_by=actor_ref,
        )
        if transferred_from:
            handoff["transferred_from"] = dict(transferred_from)
    elif action == "resume_ai":
        handoff.update(
            contract_version="inbox.handoff.v1",
            status="resolved",
            resolved_at=occurred_at,
            resolved_by=actor_ref,
            resolution="resume_ai",
        )
        archive = extra.get("handoff_history") if isinstance(extra.get("handoff_history"), list) else []
        extra["handoff_history"] = [*archive, deepcopy(handoff)][-30:]
    extra["handoff"] = handoff
    return handoff


def _assignment_action_contract(
    *,
    endpoint: str,
    payload_defaults: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    action = {
        "id": "assign",
        "label": "Asignar",
        "method": "POST",
        "endpoint": endpoint,
        "requires": ["assignee_id", "expected_assignee_id"],
        "authorization": {
            "mode": "supervised_or_capability",
            "roles": ["supervisor", "admin", "super_admin"],
            "capability": "tickets.assign",
        },
        "concurrency": {
            "contract_version": "inbox.assignment_cas.v1",
            "expected_field": "expected_assignee_id",
            "unassigned_value": None,
            "same_target_replay": "idempotent",
            "stale_state": "assignment_state_conflict",
        },
    }
    if payload_defaults:
        action["payload_defaults"] = dict(payload_defaults)
    return action


def _assignment_policy_error(error: TicketAssignmentPolicyError):
    return _error_response(
        error.message,
        error.status_code,
        error.reason_code,
        error.action_hint,
    )


def _operational_ownership_block(
    actor: User | None,
    assignee_id: Any,
) -> tuple[str, str, str] | None:
    """Require the ticket owner (or a narrow supervisor override) to mutate it.

    Only supervised roles may override operational ownership.  The
    ``tickets.assign`` capability authorizes the separate CAS assignment
    transition; it never authorizes replies or lifecycle mutations.
    """

    actor_role = canonical_role(getattr(actor, "rol", None)) if actor is not None else None
    if actor_role in {"supervisor", "admin", "super_admin"}:
        return None
    normalized_assignee_id = _coerce_inbox_ticket_id(assignee_id)
    if normalized_assignee_id is None:
        return (
            "ticket_claim_required",
            "Toma el ticket antes de continuar",
            "claim_ticket",
        )
    if actor is None or normalized_assignee_id != getattr(actor, "id", None):
        return (
            "ticket_assigned_to_other",
            "El ticket esta asignado a otro operador",
            "refresh_inbox",
        )
    return None


def _validate_handoff_recipient(
    extra: Mapping[str, Any],
    *,
    actor: User,
    current_assignee_id: Any,
):
    """Require a real A-to-B transfer when accepting a handoff.

    A handoff is not an acknowledgement button for its requester.  The current
    owner and the recorded requester must both be different from the recipient;
    otherwise the audit trail could say ``accepted`` without transferring
    ownership at all.
    """

    actor_id = _coerce_inbox_ticket_id(getattr(actor, "id", None))
    assignee_id = _coerce_inbox_ticket_id(current_assignee_id)
    handoff = extra.get("handoff") if isinstance(extra.get("handoff"), Mapping) else {}
    requested_by = handoff.get("requested_by") if isinstance(handoff.get("requested_by"), Mapping) else {}
    requester_id = _coerce_inbox_ticket_id(requested_by.get("id"))
    if actor_id is not None and actor_id in {assignee_id, requester_id}:
        return _error_response(
            "El handoff debe ser aceptado por otro operador compatible",
            409,
            "handoff_self_accept_forbidden",
            "choose_different_handoff_recipient",
        )
    return None


def _operational_ownership_error(actor: User | None, assignee_id: Any):
    block = _operational_ownership_block(actor, assignee_id)
    if block is None:
        return None
    reason_code, message, action_hint = block
    return _error_response(message, 409, reason_code, action_hint)


def _apply_operational_ownership_contract(
    action: dict[str, Any],
    actor: User | None,
    assignee_id: Any,
) -> dict[str, Any]:
    block = _operational_ownership_block(actor, assignee_id)
    if block is not None:
        reason_code, message, action_hint = block
        action.update(
            disabled=True,
            disabled_reason=message,
            reason_code=reason_code,
            action_hint=action_hint,
        )
    return action


def _reply_action_contract(
    *,
    endpoint: str,
    actor: User | None,
    assignee_id: Any,
    payload_defaults: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    action = {
        "id": "reply",
        "label": "Responder",
        "method": "POST",
        "endpoint": endpoint,
        "requires": ["body", "client_message_id_or_idempotency_key"],
        "idempotency": {
            "contract_version": "inbox.reply_idempotency.v1",
            "preferred_header": "Idempotency-Key",
            "body_field": "client_message_id",
            "retry_rule": "reuse_same_value",
            "request_id_compatibility": True,
        },
        "delivery_contract_version": "inbox.action_delivery.v2",
    }
    if payload_defaults:
        action["payload_defaults"] = dict(payload_defaults)
    return _apply_operational_ownership_contract(action, actor, assignee_id)


def _unsupported_reply_action(*, action_id: str, label: str, reason_code: str) -> dict[str, Any]:
    return {
        "id": action_id,
        "label": label,
        "method": "POST",
        "enabled": False,
        "disabled": True,
        "reason_code": reason_code,
        "disabled_reason": "Esta accion todavia no tiene un contrato durable de envio para este inbox.",
        "action_hint": "use_text_reply_or_handoff",
        "external_dispatch": False,
    }


def _verified_tenant_form_options(tenant: TenantProfile) -> list[dict[str, Any]]:
    cache_key = f"tenant:{tenant.id}"
    request_cache: dict[str, list[dict[str, Any]]] | None = None
    if has_request_context():
        request_cache = request.environ.setdefault("chatboc.inbox_form_options", {})
        cached = request_cache.get(cache_key)
        if cached is not None:
            return cached

    rows = MessageTemplateRegistry.query.filter(
        MessageTemplateRegistry.tenant_id == tenant.id,
        func.lower(MessageTemplateRegistry.status).in_(("approved", "active", "ready", "published")),
    ).order_by(
        MessageTemplateRegistry.updated_at.desc(),
        MessageTemplateRegistry.id.desc(),
    ).limit(50).all()
    options: list[dict[str, Any]] = []
    for form in rows:
        metadata = form.metadata_json if isinstance(form.metadata_json, Mapping) else {}
        flow_id = str(metadata.get("flow_id") or "").strip()
        if not flow_id or not str(form.external_template_id or "").strip() or not str(form.content_sid or "").strip():
            continue
        options.append(
            {
                "id": form.id,
                "label": form.name,
                "name": form.name,
                "language": form.language,
                "flow_id": flow_id,
                "revision": _iso(form.updated_at),
                "tenant_owned": True,
                "approved": True,
                "tenant_verified": True,
                "evidence": {
                    "tenant_owned": True,
                    "approved": True,
                    "flow_contract_verified": True,
                },
            }
        )
    if request_cache is not None:
        request_cache[cache_key] = options
    return options


def _crm_artifact_action(
    *, action_id: str, label: str, source_model: str, tenant: TenantProfile,
    actor: User | None, assignee_id: Any, payload_defaults: Mapping[str, Any],
    endpoint: str, closed: bool = False,
) -> dict[str, Any]:
    binding_unavailable = action_id == "attach_file" and source_model == "TenantTicket"
    form_options = _verified_tenant_form_options(tenant) if action_id == "send_form" else []
    form_unavailable = action_id == "send_form" and not form_options
    disabled = binding_unavailable or closed or form_unavailable
    reason_code = None
    disabled_reason = None
    action_hint = None
    if closed:
        reason_code = "ticket_closed"
        disabled_reason = "El ticket debe reabrirse antes de agregar recursos."
        action_hint = "reopen_ticket"
    elif binding_unavailable:
        reason_code = "tenant_ticket_attachment_binding_unavailable"
        disabled_reason = "Este tipo de ticket todavia no tiene adjuntos vinculados de forma verificable."
        action_hint = "upload_and_bind_attachment_first"
    elif form_unavailable:
        reason_code = "artifact_form_options_unavailable"
        disabled_reason = "No hay formularios tenant-owned aprobados y verificables para este caso."
        action_hint = "configure_approved_tenant_flow_form"

    action = {
        "id": action_id,
        "label": label,
        "method": "POST",
        "endpoint": endpoint,
        "enabled": not disabled,
        "disabled": disabled,
        "reason_code": reason_code,
        "disabled_reason": disabled_reason,
        "action_hint": action_hint,
        "requires": {
            "attach_file": ["attachment_id", "Idempotency-Key"],
            "share_location": ["lat", "lng", "Idempotency-Key"],
            "send_form": ["form_id", "Idempotency-Key"],
        }[action_id],
        "accepted_fields": {
            "attach_file": ["attachment_id"],
            "share_location": ["lat", "lng", "label", "address", "capture_source"],
            "send_form": ["form_id"],
        }[action_id],
        "payload_defaults": dict(payload_defaults),
        "options": form_options,
        "delivery_mode": "crm_only",
        "external_dispatch": False,
        "delivery_contract_version": "inbox.action_delivery.v2",
    }
    _apply_operational_ownership_contract(action, actor, assignee_id)
    action["enabled"] = not bool(action.get("disabled"))
    return action


def _reply_delivery_evidence(value: Mapping[str, Any] | None) -> dict[str, Any]:
    evidence = value if isinstance(value, Mapping) else {}
    status = str(evidence.get("status") or "").strip().lower()
    reason = str(evidence.get("reason") or "").strip().lower()
    final = evidence.get("final_delivery") if isinstance(evidence.get("final_delivery"), Mapping) else {}
    final_status = str(final.get("status") or "").strip().lower()
    external_dispatch = bool(evidence.get("external_dispatch"))
    return {
        "saved_in_crm": bool(evidence),
        "dispatch_attempted": external_dispatch or status in {"dispatch_attempted", "provider_accepted", "external_dispatch_failed", "failed"} or reason in {"acceptance_unverified", "external_dispatch_failed", "notification_dispatch_failed"},
        "provider_accepted": status == "provider_accepted" and bool(evidence.get("provider_message_id")),
        "delivered": final_status == "delivered",
        "failed": status in {"external_dispatch_failed", "failed"} or reason in {"external_dispatch_failed", "notification_dispatch_failed"} or final_status in {"failed", "undelivered"},
        "authoritative_delivery_source": final.get("authoritative_source") or "not_available",
        "provider_message_id_present": bool(evidence.get("provider_message_id")),
    }


def _ticket_reply_contract(
    *, source_model: str, ticket_id: int, channel: str | None, actor: User | None,
    assignee_id: Any, closed: bool, contact: Mapping[str, Any] | None,
    tenant: TenantProfile, latest_delivery: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_channel = str(channel or "web").strip().lower() or "web"
    contact = contact if isinstance(contact, Mapping) else {}
    ownership_block = _operational_ownership_block(actor, assignee_id)
    reason_code = None
    disabled_reason = None
    if closed:
        reason_code, disabled_reason = "ticket_closed", "El ticket debe reabrirse antes de responder."
    elif ownership_block is not None:
        reason_code, disabled_reason, _ = ownership_block

    whatsapp_source = normalized_channel in {"whatsapp", "wa", "twilio", "whatsapp_business"}
    email_source = normalized_channel in {"email", "mail", "correo"}
    raw_phone = str(contact.get("phone") or contact.get("telefono") or "").strip()
    from utils.validators import normalize_phone

    normalized_phone = normalize_phone(raw_phone) if raw_phone else None
    phone_present = bool(normalized_phone)
    email_present = bool(str(contact.get("email") or "").strip())
    sender_reason_code = None
    if source_model in {"TenantTicket", "MunicipioTicket"}:
        from services.tenant_twilio_messaging import (
            resolve_tenant_twilio_sender_snapshot,
        )

        sender_snapshot = resolve_tenant_twilio_sender_snapshot(
            tenant_id=int(tenant.id),
            channel="whatsapp",
            session=db.session,
        )
        sender_present = bool(
            sender_snapshot.sender is not None and not sender_snapshot.reason_code
        )
        sender_reason_code = sender_snapshot.reason_code
    else:
        sender_present = False
    tenant_outbox_enabled = True
    if source_model in {"TenantTicket", "MunicipioTicket"}:
        from services.domain_effect_gate import resolve_domain_effect_outbox_policy

        tenant_outbox_enabled = resolve_domain_effect_outbox_policy(
            current_app.config, tenant_id=int(tenant.id)
        ).enabled
    whatsapp_enabled = (
        whatsapp_source
        and phone_present
        and sender_present
        and tenant_outbox_enabled
        and source_model in {"TenantTicket", "MunicipioTicket"}
    )
    whatsapp_reason_code = None
    if not whatsapp_enabled:
        if not whatsapp_source:
            whatsapp_reason_code = "ticket_channel_not_whatsapp"
        elif not phone_present:
            whatsapp_reason_code = (
                "contact_phone_invalid" if raw_phone else "contact_phone_missing"
            )
        elif source_model == "MunicipioTicket":
            # Preserve the stable legacy surface code.  The nested municipal
            # WhatsApp contract exposes the precise readiness failure.
            whatsapp_reason_code = "legacy_whatsapp_enterprise_cutover_required"
        elif not sender_present:
            whatsapp_reason_code = (
                sender_reason_code or "tenant_whatsapp_sender_missing"
            )
        else:
            whatsapp_reason_code = "whatsapp_outbox_cutover_required"
    email_enabled = email_source and email_present
    return {
        "contract_version": "inbox.reply_contract.v1",
        "source_model": source_model,
        "ticket_id": ticket_id,
        "channel": normalized_channel,
        "endpoint": "/api/v2/inbox/omnichannel/actions",
        "method": "POST",
        "enabled": reason_code is None,
        "disabled_reason": disabled_reason,
        "reason_code": reason_code,
        "idempotency": {
            "contract_version": "inbox.reply_idempotency.v1", "required": True,
            "preferred_header": "Idempotency-Key", "body_field": "client_message_id",
            "retry_rule": "reuse_same_value",
        },
        "supported_message_types": {
            "text": {"enabled": reason_code is None},
            "attachment": {
                "enabled": source_model == "MunicipioTicket" and reason_code is None,
                "reason_code": (
                    reason_code if source_model == "MunicipioTicket"
                    else "tenant_ticket_attachment_binding_unavailable"
                ),
                "delivery_mode": "crm_only",
            },
            "location": {"enabled": reason_code is None, "reason_code": reason_code, "delivery_mode": "crm_only"},
            "form": {"enabled": reason_code is None, "reason_code": reason_code, "delivery_mode": "crm_only"},
        },
        "handoff": {"enabled": reason_code is None, "supported_channels": sorted(_HANDOFF_SUPPORTED_CHANNELS)},
        "delivery_channels": [
            {"id": "crm", "enabled": reason_code is None, "evidence": "durable_timeline"},
            {"id": "whatsapp", "enabled": reason_code is None and whatsapp_enabled,
             "reason_code": whatsapp_reason_code,
             "acceptance_semantics": "provider_accepted_is_not_delivered"},
            {"id": "email", "enabled": reason_code is None and email_enabled,
             "reason_code": None if email_enabled else ("ticket_channel_not_email" if not email_source else "contact_email_missing"),
             "acceptance_semantics": "provider_accepted_is_not_delivered"},
        ],
        "delivery_state_machine": {
            "contract_version": "inbox.reply_delivery_evidence.v1",
            "states": ["saved_in_crm", "dispatch_attempted", "provider_accepted", "delivered", "failed"],
            "delivered_requires": "provider_status_callback",
            "latest_evidence": _reply_delivery_evidence(latest_delivery),
        },
    }


def _allowed_inbox_actions(
    ticket: TenantTicket,
    extra: Mapping[str, Any],
    *,
    tenant: TenantProfile,
    actor: User | None = None,
) -> list[dict[str, Any]]:
    status = str(ticket.estado or "").lower()
    base_endpoint = f"/api/v2/inbox/omnichannel/{ticket.id}/actions"
    defaults = {"source_model": "TenantTicket", "ticket_id": ticket.id}
    reply_action = _reply_action_contract(
        endpoint=base_endpoint,
        actor=actor,
        assignee_id=extra.get("assignee_id"),
        payload_defaults=defaults,
    )
    from services.domain_effect_gate import resolve_domain_effect_outbox_policy

    outbox_enabled = resolve_domain_effect_outbox_policy(
        current_app.config, tenant_id=int(tenant.id)
    ).enabled
    whatsapp_source = _ticket_channel(ticket) in {
        "whatsapp",
        "wa",
        "twilio",
        "whatsapp_business",
    }
    external_dispatch_enabled = bool(outbox_enabled or not whatsapp_source)
    reply_action.update(
        delivery_mode=(
            "durable_queue"
            if outbox_enabled
            else ("crm_only" if whatsapp_source else "provider_acceptance")
        ),
        fallback="http_polling",
        external_dispatch=external_dispatch_enabled,
        operator_message=(
            (
                "La respuesta se guarda primero y se entrega mediante la cola durable. "
                "Los reintentos conservan la misma identidad sin duplicar el envio."
            )
            if outbox_enabled
            else (
                "La respuesta se guarda en el CRM sin despacho externo. "
                "WhatsApp requiere activar el cutover durable del tenant."
                if whatsapp_source
                else "La respuesta se guarda primero y usa el canal del ticket."
            )
        ),
    )
    actions = [reply_action]
    if not extra.get("assignee_id"):
        actions.append(
            {
                "id": "claim",
                "label": "Tomar ticket",
                "method": "POST",
                "endpoint": base_endpoint,
                "requires": [],
                "payload_defaults": defaults,
                "delivery_mode": "internal_event",
                "external_dispatch": False,
            }
        )
    if actor_can_assign_tickets(actor):
        actions.append(
            _assignment_action_contract(
                endpoint=base_endpoint,
                payload_defaults=defaults,
            )
        )
    actions.append(
        _apply_operational_ownership_contract(
            {
                "id": "set_priority",
                "label": "Cambiar prioridad",
                "method": "POST",
                "endpoint": base_endpoint,
                "requires": ["priority"],
                "payload_defaults": defaults,
            },
            actor,
            extra.get("assignee_id"),
        )
    )
    handoff = extra.get("handoff") if isinstance(extra.get("handoff"), Mapping) else None
    actions.extend(
        _handoff_action_contracts(
            endpoint=base_endpoint,
            handoff=handoff,
            payload_defaults=defaults,
            actor=actor,
            assignee_id=extra.get("assignee_id"),
        )
    )
    if status in _CLOSED_TICKET_STATES:
        actions.append(
            _apply_operational_ownership_contract(
                {
                    "id": "reopen",
                    "label": "Reabrir",
                    "method": "POST",
                    "endpoint": base_endpoint,
                    "requires": [],
                    "payload_defaults": defaults,
                },
                actor,
                extra.get("assignee_id"),
            )
        )
    else:
        actions.append(
            _apply_operational_ownership_contract(
                {
                    "id": "close",
                    "label": "Cerrar",
                    "method": "POST",
                    "endpoint": base_endpoint,
                    "requires": [],
                    "payload_defaults": defaults,
                    "destructive": True,
                },
                actor,
                extra.get("assignee_id"),
            )
        )
    actions.extend([
        _crm_artifact_action(
            action_id="attach_file", label="Adjuntar archivo", source_model="TenantTicket",
            tenant=tenant, actor=actor, assignee_id=extra.get("assignee_id"),
            payload_defaults=defaults, endpoint=base_endpoint,
            closed=status in _CLOSED_TICKET_STATES,
        ),
        _crm_artifact_action(
            action_id="share_location", label="Compartir ubicacion", source_model="TenantTicket",
            tenant=tenant, actor=actor, assignee_id=extra.get("assignee_id"),
            payload_defaults=defaults, endpoint=base_endpoint,
            closed=status in _CLOSED_TICKET_STATES,
        ),
        _crm_artifact_action(
            action_id="send_form", label="Agregar formulario al caso", source_model="TenantTicket",
            tenant=tenant, actor=actor, assignee_id=extra.get("assignee_id"),
            payload_defaults=defaults, endpoint=base_endpoint,
            closed=status in _CLOSED_TICKET_STATES,
        ),
    ])
    return actions


def _next_steps(ticket: TenantTicket, extra: Mapping[str, Any]) -> list[dict[str, Any]]:
    steps = []
    if not extra.get("assignee_id"):
        steps.append({"id": "claim_ticket", "label": "Tomar ticket", "action": "claim", "priority": "high"})
    if str(ticket.estado or "").lower() not in _CLOSED_TICKET_STATES:
        steps.append({"id": "reply_customer", "label": "Responder al contacto", "action": "reply", "priority": "medium"})
    if ticket.latitud is None and ticket.longitud is None and not extra.get("address"):
        steps.append({"id": "collect_location", "label": "Pedir ubicacion si aplica", "action": "reply", "priority": "low"})
    if _ticket_channel(ticket) == "whatsapp":
        steps.append({"id": "whatsapp_followup", "label": "Continuar por WhatsApp", "action": "reply", "priority": "medium"})
    return steps[:4]


def _source_metadata(ticket: TenantTicket, extra: Mapping[str, Any]) -> dict[str, Any]:
    contact = extra.get("contact") if isinstance(extra.get("contact"), dict) else {}
    lead_profile = extra.get("lead_profile") if isinstance(extra.get("lead_profile"), dict) else {}
    source = extra.get("source") or extra.get("lead_source") or ticket.origen
    pedido_reference = (
        extra.get("pedido_reference")
        or extra.get("order_reference")
        or extra.get("pedido_id")
        or extra.get("order_id")
    )
    return {
        "origin": ticket.origen,
        "channel": _ticket_channel(ticket),
        "conversation_id": extra.get("conversation_id") or f"ticket-{ticket.id}",
        "chat_session_id": extra.get("chat_session_id") or extra.get("session_id"),
        "demo_session_id": extra.get("demo_session_id"),
        "widget_id": extra.get("widget_id"),
        "contact_key": extra.get("contact_key"),
        "whatsapp_message_id": extra.get("whatsapp_message_id"),
        "lead_source": extra.get("lead_source") or ticket.origen,
        "source": source,
        "contact": contact,
        "lead_profile": lead_profile,
        "pedido_reference": pedido_reference,
        "anon_id": extra.get("anon_id") or contact.get("anon_id") or contact.get("external_id"),
        "source_model": "TenantTicket",
        "demo_mode": bool(extra.get("demo_mode")),
    }


def _legacy_claim_comments(ticket: MunicipioTicket, *, limit: int = 30) -> list[TicketComentario]:
    rows = (
        TicketComentario.query.filter_by(municipio_ticket_id=ticket.id)
        .order_by(TicketComentario.fecha.desc())
        .limit(limit)
        .all()
    )
    return list(reversed(rows))


def _legacy_claim_updated_at(ticket: MunicipioTicket, comments: list[TicketComentario]) -> datetime | None:
    if comments:
        latest = comments[-1].fecha
        if latest:
            return latest
    return ticket.ultima_actividad or ticket.fecha


def _legacy_claim_timeline(ticket: MunicipioTicket, comments: list[TicketComentario]) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []
    evidence_index = _legacy_claim_evidence_index(ticket)
    for comment in comments:
        origin = str(comment.origen or ("admin_panel" if comment.es_admin else "public_tracking")).strip().lower()
        comment_attachments: list[dict[str, Any]] = []
        if comment.archivo_adjunto is not None:
            comment_attachments.append(
                _serialize_legacy_claim_attachment(
                    comment.archivo_adjunto,
                    evidence=evidence_index.get(comment.archivo_adjunto.id),
                    fallback_source=origin,
                )
            )
        timeline.append(
            {
                "id": comment.id,
                "type": "message" if not comment.estado_ticket else "status_change",
                "origin": origin,
                "body": comment.comentario or "",
                "visibility": "internal" if comment.es_admin and origin == "internal" else "public",
                "created_at": _iso(comment.fecha),
                "actor": {
                    "id": comment.user_id,
                    "type": "agent" if comment.es_admin else "citizen",
                    "name": "Equipo" if comment.es_admin else (ticket.nombre_vecino or "Vecino/a"),
                },
                "action": comment.estado_ticket,
                "attachments": comment_attachments,
            }
        )
    return timeline


def _legacy_claim_evidence_index(ticket: MunicipioTicket) -> dict[int, dict[str, Any]]:
    extra = ticket.datos_extra if isinstance(ticket.datos_extra, Mapping) else {}
    batches = extra.get("whatsapp_flow_evidence")
    if not isinstance(batches, list):
        return {}
    index: dict[int, dict[str, Any]] = {}
    for batch in batches:
        if not isinstance(batch, Mapping):
            continue
        items = batch.get("items") if isinstance(batch.get("items"), list) else []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            try:
                attachment_id = int(item.get("attachment_id"))
            except (TypeError, ValueError):
                continue
            index[attachment_id] = {
                "source": batch.get("source") or "whatsapp_flow",
                "origin": batch.get("source") or "whatsapp_flow",
                "status": item.get("status") or "ready",
                "kind": item.get("kind"),
                "flow_id": batch.get("flow_id"),
                "interaction_id": batch.get("interaction_id"),
                "uploaded_at": batch.get("received_at"),
            }
    return index


def _serialize_legacy_claim_attachment(
    attachment: ArchivoAdjunto,
    *,
    evidence: Mapping[str, Any] | None = None,
    fallback_source: str = "claim_attachment",
) -> dict[str, Any]:
    metadata = evidence if isinstance(evidence, Mapping) else {}
    payload = serialize_attachment_for_delivery(attachment)
    mime_type = str(payload.get("mimeType") or "")
    payload.update(
        {
            "kind": metadata.get("kind") or ("image" if mime_type.startswith("image/") else "file"),
            "source": metadata.get("source") or fallback_source,
            "origin": metadata.get("origin") or fallback_source,
            "status": metadata.get("status") or "ready",
        }
    )
    for key in ("flow_id", "interaction_id"):
        if metadata.get(key) is not None:
            payload[key] = metadata.get(key)
    return payload


def _legacy_claim_attachments(ticket: MunicipioTicket) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    evidence_index = _legacy_claim_evidence_index(ticket)
    if ticket.foto_url_directa:
        photo = serialize_attachment_for_delivery(
            {
                "id": f"legacy-photo-{ticket.id}",
                "name": "Foto adjunta",
                "url": ticket.foto_url_directa,
            }
        )
        photo.update(
            {
                "kind": "image",
                "source": "claim_attachment",
                "origin": "claim_attachment",
                "status": "ready",
            }
        )
        attachments.append(photo)
    rows = (
        ArchivoAdjunto.query.filter_by(municipio_ticket_id=ticket.id)
        .order_by(ArchivoAdjunto.fecha.asc(), ArchivoAdjunto.id.asc())
        .all()
    )
    for attachment in rows:
        attachments.append(
            _serialize_legacy_claim_attachment(
                attachment,
                evidence=evidence_index.get(attachment.id),
            )
        )
    return attachments


def _legacy_claim_assignee(ticket: MunicipioTicket) -> dict[str, Any] | None:
    assignee = getattr(ticket, "asignado_a", None)
    if not assignee:
        return None
    return {"id": assignee.id, "name": assignee.name, "email": assignee.email}


def _legacy_claim_tracking_links(ticket: MunicipioTicket) -> dict[str, str]:
    public_code = str(ticket.nro_ticket or ticket.id)
    encoded_code = quote_plus(public_code)
    pin = str(ticket.consulta_pin or "").strip()
    pin_fragment = f"#pin={quote_plus(pin)}" if pin else ""
    return {
        "code": public_code,
        "endpoint": f"/api/public/tracking/experience?kind=claim&code={encoded_code}",
        "href": f"/tracking/claim/{encoded_code}{pin_fragment}",
        "credential_transport": "x-tracking-pin-header",
    }


def _legacy_claim_allowed_actions(
    ticket: MunicipioTicket,
    *,
    tenant: TenantProfile,
    actor: User | None = None,
) -> list[dict[str, Any]]:
    base_endpoint = "/api/v2/inbox/omnichannel/actions"
    defaults = {"source_model": "MunicipioTicket", "legacy_id": ticket.id, "ticket_id": ticket.id}
    extra = ticket.datos_extra if isinstance(ticket.datos_extra, Mapping) else {}
    handoff = extra.get("handoff") if isinstance(extra.get("handoff"), Mapping) else None
    tracking_links = _legacy_claim_tracking_links(ticket)
    reply_action = _reply_action_contract(
        endpoint=base_endpoint,
        actor=actor,
        assignee_id=ticket.asignado_a_id,
        payload_defaults=defaults,
    )
    reply_readiness = _ticket_reply_contract(
        source_model="MunicipioTicket",
        ticket_id=int(ticket.id),
        channel=ticket.canal_ingreso,
        actor=actor,
        assignee_id=ticket.asignado_a_id,
        closed=str(ticket.estado or "").lower() in _CLOSED_TICKET_STATES,
        contact={"phone": ticket.telefono_vecino, "email": ticket.email_vecino},
        tenant=tenant,
    )
    whatsapp_readiness = next(
        (
            item
            for item in reply_readiness.get("delivery_channels", [])
            if item.get("id") == "whatsapp"
        ),
        {},
    )
    whatsapp_ready = bool(whatsapp_readiness.get("enabled"))
    reply_action.update(
        {
            "delivery_mode": "durable_queue" if whatsapp_ready else "timeline_only",
            "external_dispatch": whatsapp_ready,
            # Keep the legacy action contract stable for older frontends; the
            # precise readiness failure remains available in reply_contract.
            "external_channel_reason_code": (
                None
                if whatsapp_ready
                else "legacy_whatsapp_enterprise_cutover_required"
            ),
            "operator_message": (
                "La respuesta se registra y se entrega por la cola WhatsApp auditable."
                if whatsapp_ready
                else "La respuesta puede registrarse en CRM; revisá la habilitación de WhatsApp."
            ),
        }
    )
    actions = [reply_action]
    if not ticket.asignado_a_id:
        actions.append(
            {
                "id": "claim",
                "label": "Tomar ticket",
                "method": "POST",
                "endpoint": base_endpoint,
                "requires": [],
                "payload_defaults": defaults,
                "delivery_mode": "internal_event",
                "external_dispatch": False,
            }
        )
    if actor_can_assign_tickets(actor):
        actions.append(
            _assignment_action_contract(
                endpoint=base_endpoint,
                payload_defaults=defaults,
            )
        )
    actions.extend(
        _handoff_action_contracts(
            endpoint=base_endpoint,
            handoff=handoff,
            payload_defaults=defaults,
            actor=actor,
            assignee_id=ticket.asignado_a_id,
        )
    )
    if str(ticket.estado or "").lower() in _CLOSED_TICKET_STATES:
        actions.append(
            _apply_operational_ownership_contract(
                {
                    "id": "reopen",
                    "label": "Reabrir",
                    "method": "POST",
                    "endpoint": base_endpoint,
                    "requires": [],
                    "payload_defaults": defaults,
                },
                actor,
                ticket.asignado_a_id,
            )
        )
    else:
        actions.append(
            _apply_operational_ownership_contract(
                {
                    "id": "close",
                    "label": "Cerrar",
                    "method": "POST",
                    "endpoint": base_endpoint,
                    "requires": [],
                    "payload_defaults": defaults,
                    "destructive": True,
                },
                actor,
                ticket.asignado_a_id,
            )
        )
    actions.append(
        {
            "id": "open_tracking",
            "label": "Ver seguimiento publico",
            "method": "GET",
            "endpoint": tracking_links["endpoint"],
            "href": tracking_links["href"],
            "frontend_path": tracking_links["href"],
            "credential_transport": tracking_links["credential_transport"],
            "requires": [],
        }
    )
    actions.extend([
        _crm_artifact_action(
            action_id="attach_file", label="Adjuntar archivo", source_model="MunicipioTicket",
            tenant=tenant, actor=actor, assignee_id=ticket.asignado_a_id,
            payload_defaults=defaults, endpoint=base_endpoint,
            closed=str(ticket.estado or "").lower() in _CLOSED_TICKET_STATES,
        ),
        _crm_artifact_action(
            action_id="share_location", label="Compartir ubicacion", source_model="MunicipioTicket",
            tenant=tenant, actor=actor, assignee_id=ticket.asignado_a_id,
            payload_defaults=defaults, endpoint=base_endpoint,
            closed=str(ticket.estado or "").lower() in _CLOSED_TICKET_STATES,
        ),
        _crm_artifact_action(
            action_id="send_form", label="Agregar formulario al caso", source_model="MunicipioTicket",
            tenant=tenant, actor=actor, assignee_id=ticket.asignado_a_id,
            payload_defaults=defaults, endpoint=base_endpoint,
            closed=str(ticket.estado or "").lower() in _CLOSED_TICKET_STATES,
        ),
    ])
    return actions


def _legacy_claim_next_steps(ticket: MunicipioTicket) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if not ticket.asignado_a_id:
        steps.append({"id": "claim_ticket", "label": "Tomar ticket", "action": "claim", "priority": "high"})
    if str(ticket.estado or "").lower() not in _CLOSED_TICKET_STATES:
        steps.append({"id": "reply_citizen", "label": "Responder al vecino", "action": "reply", "priority": "high"})
    if ticket.latitud is None and ticket.longitud is None and not ticket.direccion:
        steps.append({"id": "collect_location", "label": "Pedir ubicacion o referencia", "action": "reply", "priority": "medium"})
    return steps[:4]


def _legacy_claim_inbox_payload(
    ticket: MunicipioTicket,
    tenant: TenantProfile,
    live_chat_status: Mapping[str, Any] | None = None,
    *,
    actor: User | None = None,
    artifact_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    extra = ticket.datos_extra if isinstance(ticket.datos_extra, Mapping) else {}
    handoff = extra.get("handoff") if isinstance(extra.get("handoff"), Mapping) else None
    comments = _legacy_claim_comments(ticket)
    timeline = _legacy_claim_timeline(ticket, comments)
    timeline.extend(artifact_events if artifact_events is not None else _inbox_artifact_events(tenant_id=tenant.id, source_model="MunicipioTicket", ticket_id=ticket.id))
    timeline.sort(key=lambda item: str(item.get("created_at") or ""))
    latest_comment = comments[-1].comentario if comments else None
    updated_at = _legacy_claim_updated_at(ticket, comments)
    assignee = _legacy_claim_assignee(ticket)
    actions = _legacy_claim_allowed_actions(ticket, tenant=tenant, actor=actor)
    tracking_links = _legacy_claim_tracking_links(ticket)
    channel = str(ticket.canal_ingreso or "whatsapp").strip().lower()
    title = ticket.asunto or ticket.categoria or f"Reclamo {ticket.nro_ticket or ticket.id}"
    description = ticket.detalles or ticket.pregunta or title
    contact_name = ticket.nombre_vecino or ticket.nombre_display_whatsapp or "Vecino/a"
    queued, pending_count, pending_since = _ticket_live_chat_queue_signals(
        status=ticket.estado,
        timeline=timeline,
        handoff=handoff,
    )
    live_chat = _inbox_live_chat_contract(
        live_chat_status or {},
        queued=queued,
        pending_customer_messages=pending_count,
        pending_since=pending_since,
        ticket_id=ticket.id,
        source_model="MunicipioTicket",
    )

    delivery_history = extra.get("reply_delivery_history") if isinstance(extra.get("reply_delivery_history"), list) else []
    latest_delivery = extra.get("reply_delivery_latest_evidence") if isinstance(extra.get("reply_delivery_latest_evidence"), Mapping) else None
    from services.municipio_ticket_reply_delivery import (
        list_ticket_reply_deliveries as list_municipio_ticket_reply_deliveries,
    )

    reply_deliveries = list_municipio_ticket_reply_deliveries(
        tenant_id=int(tenant.id),
        ticket_id=int(ticket.id),
        session=db.session,
    )
    durable_latest_delivery = reply_deliveries[-1] if reply_deliveries else None
    from services.municipio_ticket_handoff import list_handoff_events

    handoff_events = list_handoff_events(
        tenant_id=int(tenant.id),
        ticket_id=int(ticket.id),
    )
    reply_contract = _ticket_reply_contract(
        source_model="MunicipioTicket", ticket_id=ticket.id, channel=channel,
        actor=actor, assignee_id=ticket.asignado_a_id,
        closed=str(ticket.estado or "").lower() in _CLOSED_TICKET_STATES,
        contact={"phone": ticket.telefono_vecino, "email": ticket.email_vecino},
        tenant=tenant, latest_delivery=durable_latest_delivery or latest_delivery or (delivery_history[-1] if delivery_history else None),
    )
    reply_contract["whatsapp"] = _municipio_ticket_whatsapp_reply_contract(
        ticket,
        tenant,
    )
    return {
        "id": f"municipio:{ticket.id}",
        "legacy_id": ticket.id,
        "ticket_id": ticket.id,
        "source_model": "MunicipioTicket",
        "legacy_kind": "claim",
        "conversation_id": f"municipio-ticket-{ticket.id}",
        "detail_endpoint": f"/api/v2/inbox/omnichannel/{ticket.id}?source_model=MunicipioTicket",
        "title": title,
        "description": description,
        "preview_text": latest_comment or description,
        "status": ticket.estado,
        "priority": "medium",
        "channel": channel,
        "category": ticket.categoria,
        "intent": ticket.categoria or "municipal_claim",
        "assignee": assignee,
        "contact": {
            "name": contact_name,
            "phone": ticket.telefono_vecino,
            "email": ticket.email_vecino,
            "document": ticket.dni_vecino,
            "avatar": {
                "url": None,
                "source": "fallback_identity",
                "fallback": "initials_or_deterministic",
                "reason": "WhatsApp profile images are not exposed unless a consented source is available.",
            },
        },
        "location": {"lat": ticket.latitud, "lng": ticket.longitud, "address": ticket.direccion, "district": ticket.distrito},
        "map": {
            "can_render": ticket.latitud is not None and ticket.longitud is not None,
            "fallback_when_no_coordinates": "timeline_only",
        },
        "attachments": _legacy_claim_attachments(ticket),
        # Legacy claims do not have certified SLA timestamps by default.  Reuse
        # the fail-closed contract so missing evidence is never painted healthy.
        "sla": _ticket_sla_payload(ticket, extra),
        "timeline": timeline,
        "presence": {"viewers": [], "locked_by": None},
        "actions": [item["id"] for item in actions],
        "allowed_actions": actions,
        "reply_contract": reply_contract,
        "reply_deliveries": reply_deliveries,
        "handoff_events": handoff_events,
        "handoff_contract": {
            "contract_version": MunicipioTicketHandoffEvent.CONTRACT_VERSION,
            "normalized_actions": ["handoff"],
            "legacy_projection_only_actions": ["accept_handoff", "resume_ai"],
            "projection_contract_version": (
                MunicipioTicketHandoffEvent.PROJECTION_CONTRACT_VERSION
            ),
            "external_dispatch": False,
        },
        "next_steps": _legacy_claim_next_steps(ticket),
        "source_metadata": {
            "origin": "municipio_ticket",
            "channel": channel,
            "conversation_id": f"municipio-ticket-{ticket.id}",
            "lead_source": ticket.canal_ingreso or "municipal_claim",
            "source_model": "MunicipioTicket",
            "read_model": "TicketComentario",
            "admin_surface": "tenant_claims_inbox",
            "legacy_reply_endpoint": f"/tickets/municipio/{ticket.id}/responder",
            "public_messages_endpoint": f"/api/public/tracking/claims/{ticket.id}/messages",
            "tracking_code": tracking_links["code"],
            "tracking_endpoint": tracking_links["endpoint"],
            "tracking_href": tracking_links["href"],
            "tracking_credential_transport": tracking_links["credential_transport"],
        },
        "handoff": handoff,
        "live_chat": live_chat,
        "created_at": _iso(ticket.fecha),
        "updated_at": _iso(updated_at),
        "frontend_contract": {
            "render_as": "inbox_360_drawer",
            "timeline_component": "conversation_timeline",
            "map_fallback": "timeline_only",
            "source_model": "MunicipioTicket",
            "uses_legacy_bridge": True,
            "avatar_policy": "consented_real_image_or_deterministic_fallback",
        },
    }


def _municipio_ticket_whatsapp_reply_contract(
    ticket: MunicipioTicket,
    tenant: TenantProfile,
) -> dict[str, Any]:
    from services.domain_effect_gate import resolve_domain_effect_outbox_policy
    from services.tenant_ticket_reply_delivery import (
        approved_whatsapp_templates,
        whatsapp_service_window,
    )
    from services.tenant_twilio_messaging import (
        resolve_tenant_twilio_sender_snapshot,
    )
    from utils.validators import normalize_phone

    raw_recipient = str(ticket.telefono_vecino or "").strip()
    recipient = normalize_phone(raw_recipient)
    sender_snapshot = resolve_tenant_twilio_sender_snapshot(
        tenant_id=int(tenant.id),
        channel="whatsapp",
        session=db.session,
    )
    sender_ready = bool(
        sender_snapshot.sender is not None and not sender_snapshot.reason_code
    )
    if sender_ready:
        service_window = whatsapp_service_window(
            tenant_id=int(tenant.id),
            provider_sender_id=int(sender_snapshot.sender.id),
            recipient=recipient,
            session=db.session,
        )
    else:
        service_window = {
            "contract_version": "whatsapp.service_window.v1",
            "status": "unknown",
            "last_inbound_at": None,
            "expires_at": None,
            "free_form_allowed": False,
            "template_required": True,
            "sender_bound": True,
            "authoritative_source": "provider_sender_unavailable",
        }
    outbox_enabled = resolve_domain_effect_outbox_policy(
        current_app.config,
        tenant_id=int(tenant.id),
    ).enabled
    external_dispatch_enabled = bool(outbox_enabled and sender_ready and recipient)
    disabled_reason = None
    if not recipient:
        disabled_reason = (
            "contact_phone_invalid" if raw_recipient else "contact_phone_missing"
        )
    elif not sender_ready:
        disabled_reason = (
            sender_snapshot.reason_code
            or "whatsapp_tenant_sender_resolution_invalid"
        )
    elif not outbox_enabled:
        disabled_reason = "whatsapp_outbox_cutover_required"
    return {
        "contract_version": "inbox.municipio_ticket_reply.v1",
        "source_model": "MunicipioTicket",
        "channel": "whatsapp",
        "recipient_available": bool(recipient),
        "sender_available": sender_ready,
        "service_window": service_window,
        "free_form_allowed": bool(
            external_dispatch_enabled and service_window["free_form_allowed"]
        ),
        "template_required": bool(service_window["template_required"]),
        "approved_templates": approved_whatsapp_templates(
            tenant_id=int(tenant.id),
            session=db.session,
        ),
        "request_fields": {
            "template_registry_id": "positive_integer_or_null",
            "template_variables": "numbered_string_map_or_null",
        },
        "dispatch_mode": "durable_outbox" if outbox_enabled else "crm_only",
        "external_dispatch_enabled": external_dispatch_enabled,
        "disabled_reason": disabled_reason,
        "final_delivery_evidence": "signed_provider_status_callback",
    }


def _tenant_ticket_whatsapp_reply_contract(
    ticket: TenantTicket,
    tenant: TenantProfile,
) -> dict[str, Any]:
    from services.domain_effect_gate import resolve_domain_effect_outbox_policy
    from services.tenant_ticket_reply_delivery import (
        approved_whatsapp_templates,
        whatsapp_service_window,
    )
    from services.tenant_twilio_messaging import (
        resolve_tenant_twilio_sender_snapshot,
    )
    from utils.validators import normalize_phone

    contact = _tenant_ticket_reply_contact(ticket)
    raw_recipient = str(contact.get("phone") or "").strip()
    recipient = normalize_phone(raw_recipient)
    sender_snapshot = resolve_tenant_twilio_sender_snapshot(
        tenant_id=int(tenant.id),
        channel="whatsapp",
        session=db.session,
    )
    sender_ready = bool(
        sender_snapshot.sender is not None and not sender_snapshot.reason_code
    )
    if sender_ready:
        service_window = whatsapp_service_window(
            tenant_id=int(tenant.id),
            provider_sender_id=int(sender_snapshot.sender.id),
            recipient=recipient,
            session=db.session,
        )
    else:
        service_window = {
            "contract_version": "whatsapp.service_window.v1",
            "status": "unknown",
            "last_inbound_at": None,
            "expires_at": None,
            "free_form_allowed": False,
            "template_required": True,
            "sender_bound": True,
            "authoritative_source": "provider_sender_unavailable",
        }
    outbox_enabled = resolve_domain_effect_outbox_policy(
        current_app.config, tenant_id=int(tenant.id)
    ).enabled
    external_dispatch_enabled = bool(outbox_enabled and sender_ready and recipient)
    disabled_reason = None
    if not recipient:
        disabled_reason = (
            "contact_phone_invalid" if raw_recipient else "contact_phone_missing"
        )
    elif not sender_ready:
        disabled_reason = (
            sender_snapshot.reason_code or "whatsapp_tenant_sender_resolution_invalid"
        )
    elif not outbox_enabled:
        disabled_reason = "whatsapp_outbox_cutover_required"
    return {
        "contract_version": "inbox.tenant_ticket_reply.v1",
        "channel": "whatsapp",
        "recipient_available": bool(recipient),
        "sender_available": sender_ready,
        "service_window": service_window,
        "free_form_allowed": bool(
            external_dispatch_enabled and service_window["free_form_allowed"]
        ),
        "template_required": bool(service_window["template_required"]),
        "approved_templates": approved_whatsapp_templates(
            tenant_id=int(tenant.id), session=db.session
        ),
        "request_fields": {
            "template_registry_id": "positive_integer_or_null",
            "template_variables": "numbered_string_map_or_null",
        },
        "dispatch_mode": "durable_outbox" if outbox_enabled else "crm_only",
        "external_dispatch_enabled": external_dispatch_enabled,
        "disabled_reason": disabled_reason,
        "final_delivery_evidence": "signed_provider_status_callback",
    }


def _inbox_ticket_payload(
    ticket: TenantTicket,
    tenant: TenantProfile,
    live_chat_status: Mapping[str, Any] | None = None,
    *,
    actor: User | None = None,
    artifact_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    extra = _ticket_extra(ticket)
    timeline = _timeline_items(extra)
    timeline.extend(artifact_events if artifact_events is not None else _inbox_artifact_events(tenant_id=tenant.id, source_model="TenantTicket", ticket_id=ticket.id))
    timeline.sort(key=lambda item: str(item.get("created_at") or ""))
    handoff = extra.get("handoff") if isinstance(extra.get("handoff"), dict) else None
    queued, pending_count, pending_since = _ticket_live_chat_queue_signals(
        status=ticket.estado,
        timeline=timeline,
        handoff=handoff,
    )
    live_chat = _inbox_live_chat_contract(
        live_chat_status or {},
        queued=queued,
        pending_customer_messages=pending_count,
        pending_since=pending_since,
        ticket_id=ticket.id,
        source_model="TenantTicket",
    )

    assignee = None
    if extra.get("assignee_id"):
        assignee = {
            "id": extra.get("assignee_id"),
            "name": extra.get("assignee_name"),
            "email": extra.get("assignee_email"),
        }

    actions = _allowed_inbox_actions(ticket, extra, tenant=tenant, actor=actor)
    delivery_history = extra.get("reply_delivery_history") if isinstance(extra.get("reply_delivery_history"), list) else []
    latest_delivery = extra.get("reply_delivery_latest_evidence") if isinstance(extra.get("reply_delivery_latest_evidence"), Mapping) else None
    reply_contract = _ticket_reply_contract(
        source_model="TenantTicket", ticket_id=ticket.id, channel=_ticket_channel(ticket),
        actor=actor, assignee_id=extra.get("assignee_id"),
        closed=str(ticket.estado or "").lower() in _CLOSED_TICKET_STATES,
        contact=extra.get("contact") if isinstance(extra.get("contact"), Mapping) else {},
        tenant=tenant, latest_delivery=latest_delivery or (delivery_history[-1] if delivery_history else None),
    )
    from services.tenant_ticket_reply_delivery import list_ticket_reply_deliveries

    reply_contract["whatsapp"] = _tenant_ticket_whatsapp_reply_contract(
        ticket, tenant
    )
    reply_deliveries = list_ticket_reply_deliveries(
        tenant_id=int(tenant.id), ticket_id=int(ticket.id), session=db.session
    )
    return {
        "id": ticket.id,
        "ticket_id": ticket.id,
        "source_model": "TenantTicket",
        "conversation_id": extra.get("conversation_id") or f"ticket-{ticket.id}",
        "detail_endpoint": f"/api/v2/inbox/omnichannel/{ticket.id}",
        "title": extra.get("title") or ticket.categoria or f"Ticket {ticket.id}",
        "description": ticket.descripcion,
        "preview_text": extra.get("preview_text") or ticket.descripcion,
        "status": ticket.estado,
        "priority": extra.get("priority") or "medium",
        "channel": _ticket_channel(ticket),
        "category": ticket.categoria,
        "intent": extra.get("intent") or extra.get("lead_intent") or ticket.categoria,
        "assignee": assignee,
        "contact": extra.get("contact") if isinstance(extra.get("contact"), dict) else {},
        "location": {"lat": ticket.latitud, "lng": ticket.longitud, "address": extra.get("address")},
        "map": {
            "can_render": ticket.latitud is not None and ticket.longitud is not None,
            "fallback_when_no_coordinates": "timeline_only",
        },
        "attachments": _attachment_items(extra),
        "sla": _ticket_sla_payload(ticket, extra),
        "timeline": timeline,
        "presence": extra.get("presence") if isinstance(extra.get("presence"), dict) else {"viewers": [], "locked_by": None},
        "actions": [item["id"] for item in actions],
        "allowed_actions": actions,
        "reply_contract": reply_contract,
        "reply_deliveries": reply_deliveries,
        "next_steps": _next_steps(ticket, extra),
        "source_metadata": _source_metadata(ticket, extra),
        "handoff": handoff,
        "live_chat": live_chat,
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
        "updated_at": ticket.updated_at.isoformat() if ticket.updated_at else None,
        "frontend_contract": {
            "render_as": "inbox_360_drawer",
            "timeline_component": "conversation_timeline",
            "map_fallback": "timeline_only",
        },
    }


@v2_saas_bp.route("/inbox/omnichannel/<int:ticket_id>", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "supervisor", "manager", "super_admin")
def omnichannel_inbox_detail_v2(current_user, ticket_id: int):
    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error
    live_chat_status = _tenant_inbox_live_chat_status(tenant)
    source_model = str(request.args.get("source_model") or "").strip().lower()
    if source_model in {"municipioticket", "municipio_ticket", "municipio"}:
        legacy_ticket = _legacy_claim_for_tenant(tenant, ticket_id)
        if not legacy_ticket or not employee_ticket_category_access_allows(current_user, legacy_ticket):
            return _error_response("Ticket no encontrado", 404, "ticket_not_found", "refresh_inbox")
        item = _legacy_claim_inbox_payload(legacy_ticket, tenant=tenant, live_chat_status=live_chat_status, actor=current_user)
        return _json_response(
            {
                "contract_version": "inbox.omnichannel.detail.v1",
                "tenant": _tenant_ref(tenant),
                "live_chat": _inbox_live_chat_contract(live_chat_status),
                "item": item,
                "ticket": item,
            }
        )

    ticket = TenantTicket.query.filter_by(id=ticket_id, tenant_id=tenant.id).first()
    if not ticket:
        legacy_ticket = _legacy_claim_for_tenant(tenant, ticket_id)
        if legacy_ticket and employee_ticket_category_access_allows(current_user, legacy_ticket):
            item = _legacy_claim_inbox_payload(legacy_ticket, tenant=tenant, live_chat_status=live_chat_status, actor=current_user)
            return _json_response(
                {
                    "contract_version": "inbox.omnichannel.detail.v1",
                    "tenant": _tenant_ref(tenant),
                    "live_chat": _inbox_live_chat_contract(live_chat_status),
                    "item": item,
                    "ticket": item,
                }
            )
        return _error_response("Ticket no encontrado", 404, "ticket_not_found", "refresh_inbox")
    if not employee_ticket_category_access_allows(current_user, ticket):
        return _error_response("Ticket no encontrado", 404, "ticket_not_found", "refresh_inbox")
    item = _inbox_ticket_payload(ticket, tenant=tenant, live_chat_status=live_chat_status, actor=current_user)
    return _json_response(
        {
            "contract_version": "inbox.omnichannel.detail.v1",
            "tenant": _tenant_ref(tenant),
            "live_chat": _inbox_live_chat_contract(live_chat_status),
            "item": item,
            "ticket": item,
        }
    )


def _append_ticket_event(extra: dict[str, Any], *, action: str, actor: User, body: str, visibility: str = "internal") -> None:
    comments = extra.get("comments") if isinstance(extra.get("comments"), list) else []
    comments.append(
        {
            "id": uuid.uuid4().hex,
            "origin": "admin_panel",
            "action": action,
            "body": body,
            "visibility": visibility,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "actor": {"id": actor.id, "name": actor.name, "role": actor.rol},
        }
    )
    extra["comments"] = comments[-100:]


def _is_legacy_claim_source(value: Any) -> bool:
    return str(value or "").strip().lower() in {"municipioticket", "municipio_ticket", "municipio", "legacy_claim"}


def _coerce_inbox_ticket_id(raw_value: Any) -> int | None:
    """Return an exact positive integer identity without lossy coercion."""

    if isinstance(raw_value, bool):
        return None
    if isinstance(raw_value, int):
        return raw_value if raw_value > 0 else None
    if not isinstance(raw_value, str):
        return None
    value = raw_value.strip()
    # Display IDs such as ``municipio:42`` are deliberately not accepted by
    # mutation endpoints. The source model is a separate required field; if a
    # typed display ID were stripped here, ``municipio:42`` paired with
    # ``TenantTicket`` could mutate a colliding tenant ticket.
    if not value or not value.isascii() or not value.isdecimal():
        return None
    parsed = int(value, 10)
    return parsed if parsed > 0 else None


def _resolve_aliased_inbox_id(
    payload: Mapping[str, Any],
    *,
    keys: tuple[str, ...],
    required_message: str,
    required_reason: str,
    conflict_message: str,
    conflict_reason: str,
):
    """Resolve one numeric identifier without silently preferring an alias.

    Assignment clients have historically sent more than one field name.  If
    two aliases disagree, selecting the first one can mutate the wrong record
    or operator, so writes fail closed instead.
    """

    parsed_values: list[int] = []
    for key in keys:
        raw_value = payload.get(key)
        if raw_value in (None, ""):
            continue
        parsed_value = _coerce_inbox_ticket_id(raw_value)
        if parsed_value is None or parsed_value <= 0:
            return None, _error_response(required_message, 400, required_reason, f"send_{keys[0]}")
        parsed_values.append(parsed_value)

    if len(set(parsed_values)) > 1:
        return None, _error_response(conflict_message, 409, conflict_reason, "refresh_assignment_identity")
    if not parsed_values:
        return None, _error_response(required_message, 400, required_reason, f"send_{keys[0]}")
    return parsed_values[0], None


def _omnichannel_reply_idempotency_identity(
    payload: Mapping[str, Any],
    *,
    tenant_id: int,
) -> tuple[str | None, str | None, Any | None]:
    """Resolve a stable client identity without persisting the raw value."""

    primary_header = str(request.headers.get("Idempotency-Key") or "").strip()
    alternate_header = str(request.headers.get("X-Idempotency-Key") or "").strip()
    if primary_header and alternate_header and primary_header != alternate_header:
        return None, None, _error_response(
            "Idempotency-Key y X-Idempotency-Key deben coincidir",
            400,
            "reply_idempotency_key_mismatch",
            "send_one_stable_idempotency_key",
        )
    header_value = primary_header or alternate_header

    client_message_id = str(payload.get("client_message_id") or "").strip()
    body_idempotency_key = str(payload.get("idempotency_key") or "").strip()
    if client_message_id and body_idempotency_key and client_message_id != body_idempotency_key:
        return None, None, _error_response(
            "client_message_id e idempotency_key deben coincidir",
            400,
            "reply_idempotency_key_mismatch",
            "send_one_stable_idempotency_key",
        )
    body_value = client_message_id or body_idempotency_key
    if header_value and body_value and header_value != body_value:
        return None, None, _error_response(
            "La identidad idempotente del header y del body debe coincidir",
            400,
            "reply_idempotency_key_mismatch",
            "reuse_the_same_client_message_id",
        )

    explicit_request_id = str(request.headers.get("X-Request-Id") or "").strip()
    raw_identity = header_value or body_value or explicit_request_id
    source = (
        "idempotency_key_header"
        if header_value
        else (
            "client_message_id"
            if client_message_id
            else ("body_idempotency_key" if body_value else "x_request_id_compatibility")
        )
    )
    if not raw_identity:
        return None, None, _error_response(
            "Idempotency-Key o client_message_id es obligatorio para responder",
            400,
            "reply_idempotency_key_required",
            "send_stable_client_message_id",
        )
    if len(raw_identity) < 8 or len(raw_identity) > 256 or any(
        ord(character) < 32 for character in raw_identity
    ):
        return None, None, _error_response(
            "La identidad idempotente debe tener entre 8 y 256 caracteres validos",
            400,
            "reply_idempotency_key_invalid",
            "send_stable_client_message_id",
        )

    digest = hashlib.sha256(
        f"inbox.reply.v1:{int(tenant_id)}:{raw_identity}".encode("utf-8")
    ).hexdigest()
    return f"crm-reply:{digest}", source, None


_CRM_ARTIFACT_ACTIONS = {"attach_file", "share_location", "send_form"}


def _inbox_artifact_idempotency(
    payload: Mapping[str, Any], *, tenant_id: int, source_model: str, ticket_id: int, action: str
) -> tuple[str | None, Any | None]:
    raw_key = str(request.headers.get("Idempotency-Key") or "").strip()
    body_key = str(payload.get("idempotency_key") or "").strip()
    if not raw_key:
        return None, _error_response(
            "Idempotency-Key es obligatorio", 400, "artifact_idempotency_key_required", "send_idempotency_key"
        )
    if body_key and body_key != raw_key:
        return None, _error_response(
            "El Idempotency-Key del header y body debe coincidir", 400,
            "artifact_idempotency_key_mismatch", "reuse_same_idempotency_key",
        )
    if len(raw_key) < 8 or len(raw_key) > 128 or any(ord(char) < 32 for char in raw_key):
        return None, _error_response(
            "Idempotency-Key debe tener entre 8 y 128 caracteres validos", 400,
            "artifact_idempotency_key_invalid", "send_valid_idempotency_key",
        )
    digest = hashlib.sha256(
        f"inbox.artifact.v1:{tenant_id}:{source_model}:{ticket_id}:{raw_key}".encode("utf-8")
    ).hexdigest()
    return digest, None


def _validated_artifact_payload(
    *, action: str, payload: Mapping[str, Any], tenant: TenantProfile,
    source_model: str, ticket_id: int,
) -> tuple[dict[str, Any] | None, Any | None]:
    if action == "share_location":
        try:
            lat = float(payload.get("lat"))
            lng = float(payload.get("lng"))
        except (TypeError, ValueError):
            return None, _error_response("lat y lng son obligatorios", 400, "artifact_location_invalid", "send_wgs84_coordinates")
        if not math.isfinite(lat) or not math.isfinite(lng) or not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
            return None, _error_response("Las coordenadas WGS84 no son validas", 400, "artifact_location_invalid", "send_wgs84_coordinates")
        capture_source = str(payload.get("capture_source") or "manual").strip().lower()
        if capture_source not in {"manual", "operator_browser_geolocation"}:
            return None, _error_response(
                "capture_source debe identificar una captura manual o el GPS opt-in del operador",
                400,
                "artifact_location_capture_source_invalid",
                "send_supported_capture_source",
            )
        label = str(payload.get("label") or "Ubicacion compartida").strip()[:160]
        address = str(payload.get("address") or "").strip()[:255] or None
        return {
            "contract_version": "inbox.artifact.location.v1", "kind": "location",
            "lat": lat, "lng": lng, "label": label, "address": address,
            "capture_source": capture_source,
            "evidence_scope": (
                "operator_device_location"
                if capture_source == "operator_browser_geolocation"
                else "operator_entered_reference"
            ),
            "location_role": "crm_reference",
            "claim_location_modified": False,
        }, None

    if action == "attach_file":
        attachment_id = _coerce_inbox_ticket_id(payload.get("attachment_id"))
        if attachment_id is None:
            return None, _error_response("attachment_id es obligatorio", 400, "artifact_attachment_id_invalid", "send_attachment_id")
        if source_model != "MunicipioTicket":
            return None, _error_response(
                "TenantTicket no tiene una vinculacion tenant-safe para adjuntos", 409,
                "tenant_ticket_attachment_binding_unavailable", "upload_and_bind_attachment_first",
            )
        attachment = ArchivoAdjunto.query.filter_by(id=attachment_id, municipio_ticket_id=ticket_id).one_or_none()
        if attachment is None:
            return None, _error_response("Adjunto no encontrado", 404, "artifact_attachment_not_found", "choose_ticket_attachment")
        serialized = serialize_attachment_for_delivery(attachment)
        return {
            "contract_version": "inbox.artifact.attachment.v1", "kind": "attachment",
            "attachment_id": attachment.id,
            "name": serialized.get("name"), "mime_type": serialized.get("mimeType"),
            "size": serialized.get("size"), "server_verified": True,
        }, None

    form_id = _coerce_inbox_ticket_id(payload.get("form_id") or payload.get("template_registry_id"))
    if form_id is None:
        return None, _error_response("form_id es obligatorio", 400, "artifact_form_id_invalid", "send_form_id")
    form = MessageTemplateRegistry.query.filter_by(id=form_id, tenant_id=tenant.id).one_or_none()
    if form is None:
        return None, _error_response("Formulario no encontrado", 404, "artifact_form_not_found", "choose_tenant_form")
    if str(form.status or "").strip().lower() not in {"approved", "active", "ready", "published"}:
        return None, _error_response("El formulario no esta aprobado", 409, "artifact_form_not_approved", "choose_approved_form")
    form_metadata = form.metadata_json if isinstance(form.metadata_json, Mapping) else {}
    flow_id = str(form_metadata.get("flow_id") or "").strip()
    if not flow_id or not str(form.external_template_id or "").strip() or not str(form.content_sid or "").strip():
        return None, _error_response(
            "El recurso aprobado no es un formulario nativo verificable", 409,
            "artifact_form_contract_unverified", "choose_verified_flow_form",
        )
    return {
        "contract_version": "inbox.artifact.form.v1", "kind": "form",
        "form_id": form.id, "name": form.name, "language": form.language,
        "flow_id": flow_id, "revision": _iso(form.updated_at), "tenant_verified": True,
    }, None


def _persist_inbox_artifact(
    *, payload: Mapping[str, Any], tenant: TenantProfile, actor: User,
    source_model: str, ticket_id: int, action: str,
) -> tuple[InboxTicketArtifact | None, bool, Any | None]:
    key_hash, error = _inbox_artifact_idempotency(
        payload, tenant_id=tenant.id, source_model=source_model, ticket_id=ticket_id, action=action
    )
    if error is not None:
        return None, False, error
    artifact_payload, error = _validated_artifact_payload(
        action=action, payload=payload, tenant=tenant, source_model=source_model, ticket_id=ticket_id
    )
    if error is not None:
        return None, False, error
    request_digest = hashlib.sha256(
        json.dumps(artifact_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    existing = InboxTicketArtifact.query.filter_by(
        tenant_id=tenant.id, source_model=source_model, ticket_id=ticket_id,
        idempotency_key_hash=key_hash,
    ).one_or_none()
    if existing is not None:
        if existing.request_digest != request_digest or existing.action != action:
            return None, False, _error_response(
                "El Idempotency-Key ya fue usado con otro artefacto", 409,
                "artifact_idempotency_payload_conflict", "reuse_key_only_for_identical_payload",
            )
        return existing, True, None
    artifact = InboxTicketArtifact(
        tenant_id=tenant.id, source_model=source_model, ticket_id=ticket_id,
        action=action, payload_json=artifact_payload, actor_user_id=actor.id,
        idempotency_key_hash=key_hash, request_digest=request_digest,
    )
    db.session.add(artifact)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = InboxTicketArtifact.query.filter_by(
            tenant_id=tenant.id, source_model=source_model, ticket_id=ticket_id,
            idempotency_key_hash=key_hash,
        ).one_or_none()
        if existing is not None and existing.request_digest == request_digest and existing.action == action:
            return existing, True, None
        return None, False, _error_response(
            "Conflicto al registrar el artefacto", 409,
            "artifact_idempotency_payload_conflict", "reuse_key_only_for_identical_payload",
        )
    except SQLAlchemyError:
        db.session.rollback()
        return None, False, _error_response(
            "No se pudo guardar el artefacto en el CRM", 500,
            "artifact_persistence_failed", "retry_with_same_idempotency_key",
        )
    return artifact, False, None


_INBOX_ARTIFACT_DETAIL_LIMIT = 100
_INBOX_ARTIFACT_LIST_LIMIT_PER_TICKET = 20


def _inbox_artifact_events(*, tenant_id: int, source_model: str, ticket_id: int) -> list[dict[str, Any]]:
    rows = InboxTicketArtifact.query.filter_by(
        tenant_id=tenant_id, source_model=source_model, ticket_id=ticket_id,
    ).order_by(
        InboxTicketArtifact.created_at.desc(), InboxTicketArtifact.id.desc()
    ).limit(_INBOX_ARTIFACT_DETAIL_LIMIT).all()
    rows.reverse()
    return [row.to_event_dict() for row in rows]


def _inbox_artifact_event_map(
    *, tenant_id: int, identities: list[tuple[str, int]],
) -> dict[tuple[str, int], list[dict[str, Any]]]:
    if not identities:
        return {}

    normalized_identities = list(dict.fromkeys(
        (str(source_model), int(ticket_id))
        for source_model, ticket_id in identities
    ))
    identity_filter = or_(*(
        and_(
            InboxTicketArtifact.source_model == source_model,
            InboxTicketArtifact.ticket_id == ticket_id,
        )
        for source_model, ticket_id in normalized_identities
    ))
    artifact_rank = func.row_number().over(
        partition_by=(
            InboxTicketArtifact.source_model,
            InboxTicketArtifact.ticket_id,
        ),
        order_by=(
            InboxTicketArtifact.created_at.desc(),
            InboxTicketArtifact.id.desc(),
        ),
    ).label("artifact_rank")
    ranked = db.session.query(
        InboxTicketArtifact.id.label("artifact_id"),
        artifact_rank,
    ).filter(
        InboxTicketArtifact.tenant_id == tenant_id,
        identity_filter,
    ).subquery()
    rows = InboxTicketArtifact.query.join(
        ranked,
        InboxTicketArtifact.id == ranked.c.artifact_id,
    ).filter(
        ranked.c.artifact_rank <= _INBOX_ARTIFACT_LIST_LIMIT_PER_TICKET,
    ).order_by(
        InboxTicketArtifact.source_model.asc(),
        InboxTicketArtifact.ticket_id.asc(),
        InboxTicketArtifact.created_at.asc(),
        InboxTicketArtifact.id.asc(),
    ).all()

    result: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in rows:
        identity = (row.source_model, row.ticket_id)
        result.setdefault(identity, []).append(row.to_event_dict())
    return result


def _crm_artifact_action_response(
    *, payload: Mapping[str, Any], tenant: TenantProfile, actor: User,
    source_model: str, ticket: TenantTicket | MunicipioTicket, action: str,
):
    artifact, replayed, error = _persist_inbox_artifact(
        payload=payload, tenant=tenant, actor=actor, source_model=source_model,
        ticket_id=ticket.id, action=action,
    )
    if error is not None:
        return error
    if artifact is None:  # defensive: never claim persistence without the row
        return _error_response(
            "No se pudo verificar la persistencia del artefacto", 500,
            "artifact_persistence_unverified", "retry_with_same_idempotency_key",
        )
    if source_model == "MunicipioTicket":
        refreshed = _legacy_claim_for_tenant(tenant, ticket.id)
        if refreshed is None:
            return _error_response("Ticket no encontrado", 404, "ticket_not_found", "refresh_inbox")
        ticket_payload = _legacy_claim_inbox_payload(
            refreshed, tenant=tenant, live_chat_status=_tenant_inbox_live_chat_status(tenant), actor=actor
        )
    else:
        refreshed = TenantTicket.query.filter_by(id=ticket.id, tenant_id=tenant.id).one_or_none()
        if refreshed is None:
            return _error_response("Ticket no encontrado", 404, "ticket_not_found", "refresh_inbox")
        ticket_payload = _inbox_ticket_payload(
            refreshed, tenant=tenant, live_chat_status=_tenant_inbox_live_chat_status(tenant), actor=actor
        )
    return _json_response({
        "ok": True,
        "contract_version": "inbox.omnichannel.action.v1",
        "tenant": _tenant_ref(tenant),
        "action": action,
        "artifact": artifact.to_event_dict(),
        "delivery": {
            "contract_version": "inbox.action_delivery.v2",
            "mode": "crm_only", "status": "already_recorded" if replayed else "saved_to_crm",
            "reason": "idempotent_replay_no_duplicate" if replayed else "artifact_saved_to_crm",
            "saved_in_crm": True, "timeline_updated": not replayed,
            "external_dispatch": False, "dispatch_attempted": False,
            "provider_accepted": False, "delivered": False, "failed": False,
            "receipt_persisted": True, "idempotent_replay": replayed,
            "operator_message": "Guardado en el CRM. No se envio por un canal externo.",
        },
        "ticket": ticket_payload,
    })


def _inbox_action_delivery_payload(
    *,
    action: str,
    channel: str | None,
    timeline_updated: bool,
    source_model: str,
    status: str | None = None,
    reason: str | None = None,
    external_dispatch: bool = False,
    delivery_results: Mapping[str, Any] | None = None,
    requested_channels: list[str] | None = None,
    delivery_skipped: Mapping[str, Any] | None = None,
    durably_staged: bool = False,
    idempotent_replay: bool = False,
    provider_message_id: str | None = None,
) -> dict[str, Any]:
    normalized_action = str(action or "").strip().lower()
    normalized_channel = str(channel or "crm").strip().lower() or "crm"
    is_reply = normalized_action == "reply"
    if is_reply and durably_staged:
        mode = "durable_queue"
        resolved_status = status or "durably_staged"
        resolved_reason = reason or "domain_effects_durably_staged"
        fallback = "domain_effect_worker"
        reply_status = "queued_for_delivery"
        evidence_stage = "durably_staged"
        operator_message = "Respuesta guardada y encolada de forma durable. La entrega final queda pendiente del callback del proveedor."
    elif is_reply and external_dispatch and provider_message_id:
        mode = "real_message"
        resolved_status = status or "provider_accepted"
        resolved_reason = reason or "provider_accepted"
        fallback = "none"
        reply_status = "provider_accepted"
        evidence_stage = "provider_accepted"
        operator_message = "El proveedor acepto el envio y la respuesta quedo registrada en el CRM. La entrega final queda pendiente de callback."
    elif is_reply and external_dispatch:
        mode = "real_message"
        resolved_status = status or "dispatch_attempted"
        resolved_reason = reason or "acceptance_unverified"
        fallback = "provider_receipt_pending"
        reply_status = "dispatch_attempted"
        evidence_stage = "acceptance_unverified"
        operator_message = "Se intento el envio y la respuesta quedo registrada en el CRM. El proveedor no devolvio un identificador correlacionable; aceptacion y entrega siguen sin verificar."
    elif is_reply and idempotent_replay:
        mode = "idempotent_replay"
        resolved_status = status or "already_recorded"
        resolved_reason = reason or "idempotent_replay_no_redispatch"
        fallback = "existing_effects_preserved"
        reply_status = "already_recorded"
        evidence_stage = "idempotent_replay"
        operator_message = "La respuesta ya estaba registrada. No se genero otro mensaje ni un segundo envio."
    elif is_reply:
        mode = "timeline_only"
        resolved_status = status or "saved_to_crm"
        resolved_reason = reason or "external_dispatch_not_configured_for_omnichannel_action"
        fallback = "saved_to_crm_no_external_dispatch"
        reply_status = "saved_to_timeline"
        evidence_stage = "crm_only"
        operator_message = "Guardado en el CRM. No se envio por un canal externo desde esta accion."
    else:
        mode = "internal_event"
        resolved_status = status or "applied"
        resolved_reason = reason or "internal_crm_action"
        fallback = "internal_crm_event"
        reply_status = "not_a_reply"
        evidence_stage = "internal_event"
        operator_message = "Accion registrada en el CRM."

    final_delivery_status = (
        "pending_provider_callback"
        if is_reply and (durably_staged or external_dispatch)
        else ("preserved_from_original_attempt" if is_reply and idempotent_replay else "not_dispatched")
    )
    final_delivery_source = (
        "provider_status_callback"
        if is_reply and (durably_staged or external_dispatch)
        else ("original_attempt_evidence" if is_reply and idempotent_replay else "not_applicable")
    )
    payload = {
        "contract_version": "inbox.action_delivery.v2",
        "legacy_contract_version": "inbox.action_delivery.v1",
        "mode": mode,
        "delivery_mode": mode,
        "channel": normalized_channel,
        "status": resolved_status,
        "reason": resolved_reason,
        "fallback": fallback,
        "external_dispatch": external_dispatch,
        "timeline_updated": timeline_updated,
        "reply_status": reply_status,
        "evidence_stage": evidence_stage,
        "final_delivery": {
            "status": final_delivery_status,
            "authoritative_source": final_delivery_source,
        },
        "admin_surface": "tenant_claims_inbox" if source_model == "MunicipioTicket" else "omnichannel_inbox",
        "source_model": source_model,
        "operator_message": operator_message,
        "evidence": {
            "contract_version": "inbox.reply_delivery_evidence.v1",
            "saved_in_crm": bool(is_reply and timeline_updated) or bool(is_reply and idempotent_replay),
            "dispatch_attempted": bool(
                is_reply
                and (
                    external_dispatch
                    or resolved_reason in {"external_dispatch_failed", "notification_dispatch_failed"}
                )
            ),
            "provider_accepted": bool(is_reply and external_dispatch and provider_message_id),
            "delivered": False,
            "failed": bool(
                is_reply
                and resolved_reason in {"external_dispatch_failed", "notification_dispatch_failed"}
            ),
            "delivered_requires": "provider_status_callback",
        },
    }
    if provider_message_id:
        payload["provider_message_id"] = provider_message_id
    if delivery_results is not None:
        payload["delivery_results"] = {
            "email": bool(delivery_results.get("email")),
            "sms": bool(delivery_results.get("sms")),
            "whatsapp": bool(delivery_results.get("whatsapp")),
        }
        payload["delivery_results_semantics"] = "dispatch_attempt_boolean_not_provider_receipt"
    if requested_channels is not None:
        payload["requested_channels"] = list(requested_channels)
    if delivery_skipped:
        payload["delivery_skipped"] = dict(delivery_skipped)
    return payload


def _claim_action_evidence(*, source_model: str, replayed: bool) -> dict[str, Any]:
    """Describe self-claim idempotency and audit behavior without case PII.

    Claims are serialized by the locked ticket row and use the persisted
    assignee as their replay identity.  The audit event is written in the same
    transaction as the first assignment; a same-operator replay deliberately
    creates neither another assignment nor another audit entry.
    """

    normalized_source = str(source_model or "").strip()
    audit_storage = (
        "ticket_comment"
        if normalized_source == "MunicipioTicket"
        else "ticket_metadata_timeline"
    )
    is_replay = bool(replayed)
    return {
        "idempotent_replay": is_replay,
        "idempotency": {
            "contract_version": "inbox.claim_idempotency.v1",
            "strategy": "locked_current_assignee",
            "replayed": is_replay,
            "same_operator_replay_only": True,
            "idempotency_key_required": False,
            "raw_idempotency_key_persisted": False,
        },
        "audit": {
            "contract_version": "inbox.claim_audit.v1",
            "event_type": "ticket_claimed",
            "storage": audit_storage,
            "assignment_event_transactionally_coupled": True,
            "event_recorded": not is_replay,
            "replay_deduplicated": is_replay,
            "duplicate_event_created": False,
            "response_includes_contact_data": False,
        },
    }


def _ticket_delivery_channel(results: Mapping[str, Any] | None, fallback: str | None) -> str:
    normalized_results = results or {}
    for channel in ("whatsapp", "sms", "email"):
        if normalized_results.get(channel):
            return channel
    return str(fallback or "crm").strip().lower() or "crm"


def _legacy_claim_delivery_channels(
    ticket: MunicipioTicket,
    payload: Mapping[str, Any],
    *,
    visibility: str,
) -> tuple[list[str], str | None]:
    """Select one explicit legacy reply channel without cross-channel blasts."""

    if visibility not in {"public", "internal"}:
        return [], "reply_visibility_invalid"
    has_send_external = "send_external" in payload
    send_external = payload.get("send_external")
    if has_send_external and not isinstance(send_external, bool):
        return [], "reply_send_external_boolean_required"
    if visibility == "internal" or send_external is False:
        return [], None

    aliases = {
        "wa": "whatsapp",
        "twilio": "whatsapp",
        "whatsapp_business": "whatsapp",
        "mail": "email",
        "correo": "email",
        "text": "sms",
        "texto": "sms",
    }
    has_delivery_channels = "delivery_channels" in payload
    has_channels_alias = "channels" in payload
    if has_delivery_channels and has_channels_alias:
        return [], "reply_channel_alias_conflict"
    explicit = has_delivery_channels or has_channels_alias
    raw_channels = (
        payload.get("delivery_channels")
        if has_delivery_channels
        else payload.get("channels")
    )
    if explicit:
        if raw_channels is None:
            return [], "reply_channel_invalid"
        values = (
            raw_channels
            if isinstance(raw_channels, (list, tuple, set))
            else [raw_channels]
        )
        requested: list[str] = []
        for value in values:
            channel = aliases.get(
                str(value or "").strip().lower(),
                str(value or "").strip().lower(),
            )
            if channel not in {"whatsapp", "email", "sms"}:
                return [], "reply_channel_invalid"
            if channel not in requested:
                requested.append(channel)
        if not requested:
            return [], "reply_channel_invalid"
    else:
        source = str(getattr(ticket, "canal_ingreso", None) or "web").strip().lower()
        source = aliases.get(source, source)
        requested = [source] if source in {"whatsapp", "email", "sms"} else []

    source = str(getattr(ticket, "canal_ingreso", None) or "web").strip().lower()
    source = aliases.get(source, source)
    if requested and (source not in {"whatsapp", "email", "sms"} or requested != [source]):
        return [], "reply_channel_source_mismatch"
    return requested, None


def _tenant_ticket_reply_contact(ticket: TenantTicket) -> dict[str, str]:
    from services.ticket_domain_effects import tenant_ticket_reply_contact

    return tenant_ticket_reply_contact(ticket)


def _tenant_ticket_delivery_channels(
    ticket: TenantTicket,
    payload: Mapping[str, Any],
    *,
    visibility: str,
) -> tuple[list[str], str | None]:
    """Validate the exact external channel contract before persisting a reply."""

    if visibility not in {"public", "internal"}:
        return [], "reply_visibility_invalid"
    has_send_external = "send_external" in payload
    send_external = payload.get("send_external")
    if has_send_external and not isinstance(send_external, bool):
        return [], "reply_send_external_boolean_required"
    if visibility == "internal" or send_external is False:
        return [], None

    has_delivery_channels = "delivery_channels" in payload
    has_channels_alias = "channels" in payload
    if has_delivery_channels and has_channels_alias:
        return [], "reply_channel_alias_conflict"
    explicit_channels = has_delivery_channels or has_channels_alias
    raw_channels = (
        payload.get("delivery_channels")
        if has_delivery_channels
        else payload.get("channels")
    )
    if explicit_channels:
        if raw_channels is None:
            return [], "reply_channel_invalid"
        values = raw_channels if isinstance(raw_channels, (list, tuple, set)) else [raw_channels]
        normalized = []
        for value in values:
            channel = str(value or "").strip().lower()
            if channel in {"wa", "twilio", "whatsapp_business"}:
                channel = "whatsapp"
            elif channel in {"mail", "correo"}:
                channel = "email"
            if channel not in {"whatsapp", "email"}:
                return [], "reply_channel_invalid"
            if channel not in normalized:
                normalized.append(channel)
        if not normalized:
            return [], "reply_channel_invalid"
        return normalized, None

    source_channel = _ticket_channel(ticket)
    if source_channel in {"whatsapp", "wa", "twilio", "whatsapp_business"}:
        return ["whatsapp"], None
    if source_channel in {"email", "mail", "correo"}:
        return ["email"], None
    return [], None


def _tenant_whatsapp_sender(tenant: TenantProfile) -> str | None:
    sender = str(getattr(tenant, "whatsapp_sender_id", None) or "").strip()
    if sender:
        return sender
    config = tenant.configuracion if isinstance(tenant.configuracion, Mapping) else {}
    sender = str(config.get("whatsapp_sender_id") or config.get("twilio_messaging_service_sid") or "").strip()
    return sender or None


def _dispatch_tenant_ticket_reply(
    *,
    tenant: TenantProfile,
    ticket: TenantTicket,
    body: str,
    requested_channels: list[str],
    reply_record: TenantTicketReplyEvent | None = None,
) -> tuple[dict[str, bool], str | None, dict[str, str]]:
    results = {"email": False, "sms": False, "whatsapp": False}
    skipped: dict[str, str] = {}
    if not requested_channels:
        return results, "external_dispatch_no_channel_requested", skipped

    contact = (
        {
            "email": str(reply_record.recipient_email or "").strip(),
            "phone": str(reply_record.recipient_phone or "").strip(),
        }
        if reply_record is not None
        else _tenant_ticket_reply_contact(ticket)
    )
    if reply_record is not None:
        body = str(reply_record.body or "").strip()
    attempted = False

    if "whatsapp" in requested_channels:
        # TenantTicket WhatsApp is outbox-only. The historical global helper
        # can select credentials outside this tenant-bound aggregate.
        skipped["whatsapp"] = "whatsapp_outbox_cutover_required"

    if "email" in requested_channels:
        email = contact.get("email")
        if not email:
            skipped["email"] = "contact_email_missing"
        else:
            attempted = True
            try:
                from services.email_service import enviar_email

                tenant_name = str(getattr(tenant, "nombre", None) or "el equipo").strip()
                subject = f"Respuesta de {tenant_name} - solicitud #{ticket.id}"
                body_html = "<p>" + escape(body).replace("\n", "<br>") + "</p>"
                results["email"] = bool(
                    enviar_email(
                        email,
                        subject,
                        body_html,
                        cuerpo_texto=body,
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive production logging
                current_app.logger.exception(
                    "Error dispatching TenantTicket email reply ticket=%s tenant=%s: %s",
                    ticket.id,
                    tenant.slug,
                    exc,
                )
                skipped["email"] = "provider_error"

    if any(results.values()):
        return results, None, skipped
    if attempted:
        return results, "external_dispatch_failed", skipped
    if skipped.get("whatsapp") == "whatsapp_outbox_cutover_required":
        return results, "whatsapp_outbox_cutover_required", skipped
    return results, "external_dispatch_contact_or_sender_missing", skipped


def _record_ticket_reply_delivery(
    ticket: TenantTicket | MunicipioTicket,
    *,
    tenant: TenantProfile,
    source_model: str,
    delivery: Mapping[str, Any],
    actor: User,
) -> TenantTicket | MunicipioTicket:
    if source_model == "TenantTicket" and isinstance(ticket, TenantTicket):
        persisted_ticket = (
            TenantTicket.query.filter_by(id=ticket.id, tenant_id=tenant.id)
            .with_for_update().populate_existing().one()
        )
    elif source_model == "MunicipioTicket" and isinstance(ticket, MunicipioTicket):
        persisted_ticket = (
            _legacy_claim_query_for_tenant(tenant)
            .filter(MunicipioTicket.id == ticket.id)
            .with_for_update().populate_existing().one()
        )
    else:
        raise ValueError("reply delivery source_model does not match ticket")
    extra = deepcopy(_ticket_extra(persisted_ticket))
    history = extra.get("reply_delivery_history") if isinstance(extra.get("reply_delivery_history"), list) else []
    entry = {
        "contract_version": delivery.get("contract_version"),
        "source_model": source_model,
        "mode": delivery.get("mode"),
        "status": delivery.get("status"),
        "reason": delivery.get("reason"),
        "channel": delivery.get("channel"),
        "evidence_stage": delivery.get("evidence_stage"),
        "evidence": deepcopy(delivery.get("evidence") or {}),
        "final_delivery": deepcopy(delivery.get("final_delivery") or {}),
        "provider_message_id": delivery.get("provider_message_id"),
        "external_dispatch": bool(delivery.get("external_dispatch")),
        "delivery_results": deepcopy(delivery.get("delivery_results") or {}),
        "requested_channels": list(delivery.get("requested_channels") or []),
        "delivery_skipped": deepcopy(delivery.get("delivery_skipped") or {}),
        # Reaching this durable entry means the receipt is being written in the
        # same transaction. If the later commit fails the entry is rolled back
        # and the response reports receipt_persisted=false instead.
        "receipt_persisted": True,
        "actor_user_id": actor.id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    history.append(entry)
    extra["reply_delivery_history"] = history[-100:]
    extra["reply_delivery_latest_evidence"] = entry
    persisted_ticket.datos_extra = extra
    flag_modified(persisted_ticket, "datos_extra")
    db.session.add(persisted_ticket)
    return persisted_ticket


def _emit_tenant_ticket_realtime_reply(
    ticket: TenantTicket,
    event: Mapping[str, Any],
) -> bool:
    """Emit only to the authenticated tenant room; HTTP detail stays authoritative."""

    try:
        from services.ticket_domain_effects import emit_tenant_ticket_reply_realtime

        emit_tenant_ticket_reply_realtime(ticket, event)
        return True
    except Exception as exc:  # pragma: no cover - reply remains durable and pollable
        current_app.logger.exception(
            "Error emitting TenantTicket reply ticket=%s tenant=%s: %s",
            getattr(ticket, "id", None),
            getattr(ticket, "tenant_id", None),
            exc,
        )
        return False


def _dispatch_legacy_claim_reply(
    ticket: MunicipioTicket,
    body: str,
    recent_comment: TicketComentario | None,
    *,
    requested_channels: list[str],
) -> tuple[dict[str, bool], str | None]:
    try:
        from services.notification_dispatcher import dispatch_ticket_update

        raw_results = dispatch_ticket_update(
            ticket,
            "municipio",
            body,
            comentario_reciente=recent_comment,
            enable_whatsapp="whatsapp" in requested_channels,
            enabled_channels=requested_channels,
            archivos_adjuntos=[],
        )
        if not isinstance(raw_results, Mapping):
            return {"email": False, "sms": False, "whatsapp": False}, "notification_dispatch_invalid_result"
        return {
            "email": "email" in requested_channels and bool(raw_results.get("email")),
            "sms": "sms" in requested_channels and bool(raw_results.get("sms")),
            "whatsapp": "whatsapp" in requested_channels and bool(raw_results.get("whatsapp")),
        }, None
    except Exception as exc:  # pragma: no cover - defensive production logging
        current_app.logger.exception(
            "Error dispatching legacy claim reply ticket=%s: %s",
            getattr(ticket, "nro_ticket", None) or getattr(ticket, "id", None),
            exc,
        )
        return {"email": False, "sms": False, "whatsapp": False}, "notification_dispatch_failed"


def _emit_legacy_claim_realtime_reply(
    ticket: MunicipioTicket,
    comment: TicketComentario,
    actor: User,
    *,
    visibility: str = "public",
) -> bool:
    """Emit public replies to the case room and internal notes as opaque refetches."""

    try:
        if visibility == "internal":
            from socket_service import emit_new_chat_message

            # Omitting case identifiers deliberately prevents a message body
            # from reaching the signed citizen room.  The socket boundary uses
            # the tenant id only to emit an opaque collection invalidation.
            emit_new_chat_message(
                {
                    "tenant_type": "municipio",
                    "tipo": "municipio",
                    "tenant_profile_id": getattr(ticket, "tenant_id", None),
                    "municipio_id": getattr(ticket, "municipio_id", None),
                }
            )
            return True

        from socket_service import emit_ticket_comment

        emit_ticket_comment(
            {
                "tenant_type": "municipio",
                "tipo": "municipio",
                "tenant_profile_id": getattr(ticket, "tenant_id", None),
                "municipio_id": getattr(ticket, "municipio_id", None),
                "ticket_id": ticket.id,
                "ticketId": ticket.id,
                "nro_ticket": ticket.nro_ticket,
                "estado": ticket.estado,
                "comment": {
                    "id": comment.id,
                    "comentario": comment.comentario,
                    "texto": comment.comentario,
                    "es_admin": True,
                    "origen": "agent",
                    "visibility": "public",
                    "estado_ticket": ticket.estado,
                    "fecha": comment.fecha.isoformat() if comment.fecha else None,
                    "autor": getattr(actor, "name", None) or "Equipo",
                },
            }
        )
        return True
    except Exception as exc:  # pragma: no cover - reply remains durable and pollable
        current_app.logger.exception(
            "Error emitting legacy claim reply ticket=%s: %s",
            getattr(ticket, "id", None),
            exc,
        )
        return False


def _emit_legacy_claim_realtime_state(
    ticket: MunicipioTicket,
    *,
    action: str,
    previous_status: str | None,
) -> list[str]:
    """Publish citizen-safe state changes while keeping full events tenant-scoped."""

    emitted: list[str] = []
    try:
        from socket_service import emit_ticket_assignment_changed, emit_ticket_status_changed

        event_payload = {
            "tenant_type": "municipio",
            "tipo": "municipio",
            "tenant_profile_id": getattr(ticket, "tenant_id", None),
            "municipio_id": getattr(ticket, "municipio_id", None),
            "ticket_id": ticket.id,
            "ticketId": ticket.id,
            "nro_ticket": ticket.nro_ticket,
            "estado": ticket.estado,
            "previous_status": previous_status,
            "changed_at": datetime.now(timezone.utc).isoformat(),
        }
        if str(previous_status or "") != str(ticket.estado or ""):
            emit_ticket_status_changed(event_payload)
            emitted.append("ticket.status.changed")
        if action in {"assign", "claim"}:
            emit_ticket_assignment_changed({**event_payload, "assignment_state": "assigned"})
            emitted.append("ticket.assignment.changed")
    except Exception as exc:  # pragma: no cover - polling remains authoritative
        current_app.logger.exception(
            "Error emitting legacy claim state ticket=%s action=%s: %s",
            getattr(ticket, "id", None),
            action,
            exc,
        )
    return emitted


def _municipio_handoff_action_response(
    *,
    current_user: User,
    tenant: TenantProfile,
    ticket: MunicipioTicket,
    payload: Mapping[str, Any],
):
    """Persist or replay one normalized municipal handoff request."""

    from services.municipio_ticket_handoff import (
        MunicipioTicketHandoffError,
        request_human_handoff,
    )

    if "channel" in payload and "target_channel" in payload:
        return _error_response(
            "Usa un solo campo para el canal de handoff.",
            400,
            "handoff_channel_alias_conflict",
            "send_channel_only",
        )
    raw_channel = (
        payload.get("channel")
        if "channel" in payload
        else payload.get("target_channel")
    )
    try:
        result = request_human_handoff(
            ticket=ticket,
            actor=current_user,
            tenant_id=int(tenant.id),
            raw_idempotency_key=request.headers.get("Idempotency-Key"),
            raw_channel=raw_channel,
            raw_reason=payload.get("reason"),
        )
    except MunicipioTicketHandoffError as exc:
        db.session.rollback()
        return _error_response(
            exc.message,
            exc.status_code,
            exc.reason_code,
            exc.action_hint,
        )
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception(
            "MunicipioTicket durable handoff failed ticket=%s tenant=%s: %s",
            ticket.id,
            tenant.id,
            exc,
        )
        return _error_response(
            "No se pudo guardar el handoff de forma durable",
            503,
            "handoff_durability_unavailable",
            "retry_with_same_idempotency_key",
        )

    refreshed = result.ticket
    live_chat_status = _tenant_inbox_live_chat_status(tenant)
    ticket_payload = _legacy_claim_inbox_payload(
        refreshed,
        tenant=tenant,
        live_chat_status=live_chat_status,
        actor=current_user,
    )
    delivery = _inbox_action_delivery_payload(
        action="handoff",
        channel="crm",
        timeline_updated=not result.replayed,
        source_model="MunicipioTicket",
        status="already_recorded" if result.replayed else "requested",
        reason=(
            "idempotent_replay_no_duplicate"
            if result.replayed
            else "human_handoff_durably_recorded"
        ),
        external_dispatch=False,
    )
    delivery["receipt_persisted"] = True
    delivery["idempotency"] = {
        "contract_version": "municipio_ticket.handoff_idempotency.v1",
        "replayed": result.replayed,
        "source": "Idempotency-Key",
        "raw_value_persisted": False,
    }
    delivery["ledger"] = {
        "contract_version": MunicipioTicketHandoffEvent.CONTRACT_VERSION,
        "event_id": result.event.event_id,
        "normalized_event": True,
        "projection_updated": not result.replayed,
        "external_dispatch": False,
    }
    delivery["realtime"] = {
        "emitted": False,
        "event": None,
        "events": [],
        "room": f"tenant_{tenant.id}",
        "fallback": "http_polling",
    }
    return _json_response(
        {
            "ok": True,
            "contract_version": "inbox.omnichannel.action.v1",
            "tenant": _tenant_ref(tenant),
            "action": "handoff",
            "handoff_event": result.event.to_event_dict(),
            "delivery": delivery,
            "live_chat": ticket_payload.get("live_chat"),
            "ticket": ticket_payload,
        }
    )


def _omnichannel_legacy_claim_action_v2(current_user: User, tenant: TenantProfile, ticket_id: int, payload: Mapping[str, Any]):
    ticket = (
        _legacy_claim_query_for_tenant(tenant)
        .filter(MunicipioTicket.id == ticket_id)
        .with_for_update()
        .first()
    )
    if not ticket or not employee_ticket_category_access_allows(current_user, ticket):
        return _error_response("Ticket no encontrado", 404, "ticket_not_found", "refresh_inbox")

    action = str(payload.get("action") or payload.get("type") or "").strip().lower()
    if action not in {"claim", "assign", "reply", "handoff", "accept_handoff", "resume_ai", "close", "reopen", "attach_file", "share_location", "send_form"}:
        return _error_response("Accion de inbox no soportada para reclamos municipales", 400, "unsupported_legacy_inbox_action", "send_supported_action")

    if action == "handoff":
        ownership_error = _operational_ownership_error(
            current_user,
            ticket.asignado_a_id,
        )
        if ownership_error is not None:
            return ownership_error
        return _municipio_handoff_action_response(
            current_user=current_user,
            tenant=tenant,
            ticket=ticket,
            payload=payload,
        )

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    extra = deepcopy(ticket.datos_extra) if isinstance(ticket.datos_extra, Mapping) else {}
    handoff_state = _handoff_lifecycle_state(
        extra.get("handoff") if isinstance(extra.get("handoff"), Mapping) else None
    )
    transition_error = _validate_handoff_transition(action, handoff_state)
    if transition_error is not None:
        return transition_error

    timeline_updated = False
    recent_comment: TicketComentario | None = None
    delivery_results: dict[str, bool] | None = None
    dispatch_error_reason: str | None = None
    realtime_emitted = False
    realtime_state_events: list[str] = []
    previous_status = str(ticket.estado or "")
    handoff_event_body: str | None = None
    reply_outbox_enabled = False
    reply_outbox_effect_count = 0
    reply_outbox_external_effect_count = 0
    reply_replayed = False
    reply_idempotency_source: str | None = None
    reply_event: dict[str, Any] | None = None
    reply_record: MunicipioTicketReplyEvent | None = None
    claim_idempotent = False
    assignment_idempotent = False
    assignment_expected_id: int | None = None
    assignment_target_id: int | None = None
    requested_channels: list[str] | None = None
    reply_visibility = "public"
    legacy_whatsapp_cutover_blocked = False

    if action in _OPERATIONAL_OWNERSHIP_ACTIONS:
        ownership_error = _operational_ownership_error(current_user, ticket.asignado_a_id)
        if ownership_error is not None:
            return ownership_error

    if action in _CRM_ARTIFACT_ACTIONS:
        if str(ticket.estado or "").lower() in _CLOSED_TICKET_STATES:
            return _error_response(
                "El reclamo esta cerrado. Reabrilo antes de agregar recursos.", 403,
                "ticket_closed", "reopen_ticket",
            )
        return _crm_artifact_action_response(
            payload=payload, tenant=tenant, actor=current_user,
            source_model="MunicipioTicket", ticket=ticket, action=action,
        )

    if action == "claim":
        current_assignee_id = _coerce_inbox_ticket_id(ticket.asignado_a_id)
        if current_assignee_id is not None:
            if current_assignee_id != current_user.id:
                return _error_response(
                    "El ticket ya fue tomado por otro operador",
                    409,
                    "already_claimed",
                    "refresh_inbox",
                )
            claim_idempotent = True
        else:
            if not ticket_assignee_is_compatible(current_user, ticket):
                return _error_response(
                    "El operador no tiene acceso a la categoria del ticket",
                    404,
                    "ticket_not_found",
                    "refresh_inbox",
                )
            ticket.asignado_a_id = current_user.id
            ticket.asignado_en = now
            if str(ticket.estado or "").lower() in {"nuevo", "open"}:
                ticket.estado = "en_proceso"
            db.session.add(
                TicketComentario(
                    municipio_ticket_id=ticket.id,
                    comentario=f"Ticket tomado por {current_user.name}",
                    user_id=current_user.id,
                    es_admin=True,
                    origen="admin_panel",
                    estado_ticket=ticket.estado,
                )
            )
            timeline_updated = True

    elif action == "assign":
        assignee_id, assignee_error = _resolve_aliased_inbox_id(
            payload,
            keys=("assignee_id", "user_id"),
            required_message="assignee_id es obligatorio",
            required_reason="assignee_required",
            conflict_message="assignee_id y user_id deben identificar el mismo empleado",
            conflict_reason="assignee_identity_conflict",
        )
        if assignee_error is not None:
            return assignee_error
        assignment_target_id = assignee_id
        try:
            transition = assignment_transition(
                actor=current_user,
                payload=payload,
                current_assignee_id=ticket.asignado_a_id,
                target_assignee_id=assignee_id,
            )
        except TicketAssignmentPolicyError as exc:
            return _assignment_policy_error(exc)
        assignment_expected_id = transition.expected_assignee_id
        owner_ids = [
            owner_id
            for owner_id in (getattr(tenant, "municipio_id", None), getattr(tenant, "pyme_id", None))
            if owner_id is not None
        ]
        assignee_query = User.query.filter(User.id == assignee_id)
        assignee_query = assignee_query.filter(or_(User.tenant_id == tenant.id, User.id.in_(owner_ids)))
        assignee = assignee_query.first()
        if not ticket_assignee_is_operational(assignee):
            return _error_response("Empleado no encontrado para este tenant", 404, "assignee_not_found", "choose_valid_assignee")
        if not ticket_assignee_is_compatible(assignee, ticket):
            return _error_response(
                "El agente no tiene acceso a la categoria del ticket",
                409,
                "assignee_category_scope_mismatch",
                "choose_compatible_assignee",
            )
        if transition.replayed:
            assignment_idempotent = True
        else:
            ticket.asignado_a_id = assignee.id
            ticket.asignado_en = now
            if str(ticket.estado or "").lower() in {"nuevo", "open"}:
                ticket.estado = "en_proceso"
            db.session.add(
                TicketComentario(
                    municipio_ticket_id=ticket.id,
                    comentario=f"Asignado a {assignee.name}",
                    user_id=current_user.id,
                    es_admin=True,
                    origen="admin_panel",
                    estado_ticket=ticket.estado,
                )
            )
            timeline_updated = True

    elif action == "accept_handoff":
        current_assignee_id = _coerce_inbox_ticket_id(ticket.asignado_a_id)
        if not ticket_assignee_is_compatible(current_user, ticket):
            return _error_response(
                "El operador no tiene acceso a la categoria del ticket",
                404,
                "ticket_not_found",
                "refresh_inbox",
            )
        recipient_error = _validate_handoff_recipient(
            extra,
            actor=current_user,
            current_assignee_id=current_assignee_id,
        )
        if recipient_error is not None:
            return recipient_error
        previous_assignee = getattr(ticket, "asignado_a", None)
        transferred_from = (
            {
                "id": current_assignee_id,
                "name": getattr(previous_assignee, "name", None),
            }
            if current_assignee_id is not None and current_assignee_id != current_user.id
            else None
        )
        _apply_handoff_transition(
            extra,
            action="accept_handoff",
            actor=current_user,
            occurred_at=now_iso,
            transferred_from=transferred_from,
        )
        ticket.asignado_a_id = current_user.id
        ticket.asignado_en = now
        if str(ticket.estado or "").lower() in {"nuevo", "open"}:
            ticket.estado = "en_proceso"
        handoff_event_body = f"Conversación tomada por {current_user.name}"

    elif action == "resume_ai":
        _apply_handoff_transition(extra, action="resume_ai", actor=current_user, occurred_at=now_iso)
        handoff_event_body = f"Conversación devuelta a IA por {current_user.name}"

    elif action == "reply":
        if str(ticket.estado or "").lower() in _CLOSED_TICKET_STATES:
            return _error_response("El reclamo esta cerrado. Reabrilo antes de responder.", 403, "ticket_closed", "reopen_ticket")
        body, body_error = _omnichannel_reply_body(payload)
        if body_error is not None:
            return body_error
        raw_visibility = payload["visibility"] if "visibility" in payload else "public"
        if not isinstance(raw_visibility, str):
            return _error_response(
                "La visibilidad de la respuesta no es valida.",
                400,
                "reply_visibility_invalid",
                "choose_public_or_internal_visibility",
            )
        reply_visibility = raw_visibility.strip().lower()
        requested_channels, channel_error = _legacy_claim_delivery_channels(
            ticket,
            payload,
            visibility=reply_visibility,
        )
        if channel_error is not None:
            is_source_mismatch = channel_error == "reply_channel_source_mismatch"
            return _error_response(
                (
                    "El canal solicitado no coincide con el canal de origen del reclamo."
                    if is_source_mismatch
                    else "La configuración de entrega de la respuesta no es válida."
                ),
                409 if is_source_mismatch else 400,
                channel_error,
                (
                    "use_original_channel_or_internal_note"
                    if is_source_mismatch
                    else "review_reply_delivery_fields"
                ),
            )
        if "template_variables" in payload and "content_variables" in payload:
            return _error_response(
                "Usá un solo campo para las variables de la plantilla.",
                400,
                "whatsapp_template_variable_alias_conflict",
                "send_template_variables_only",
            )
        reply_idempotency_key, reply_idempotency_source, idempotency_error = (
            _omnichannel_reply_idempotency_identity(payload, tenant_id=tenant.id)
        )
        if idempotency_error is not None:
            return idempotency_error

        from services.domain_effect_gate import resolve_domain_effect_outbox_policy
        from services.ticket_service import (
            ServicioTickets,
            TicketIdempotencyConflict,
            TicketIdempotencyReplayUnavailable,
            TicketIdempotencyValidationError,
            TicketReplyOwnershipError,
        )

        outbox_policy = resolve_domain_effect_outbox_policy(
            current_app.config,
            tenant_id=tenant.id,
        )
        reply_outbox_enabled = outbox_policy.enabled
        # Preserve the historical compatibility contract for callers that do
        # not declare a delivery target.  The secure provider path is opt-in
        # through an explicit channel (or ``send_external: true``), which lets
        # the v2 CRM adopt the durable municipal reply contract without
        # silently changing older timeline-only clients.
        explicit_delivery_target = bool(
            "delivery_channels" in payload
            or "channels" in payload
            or payload.get("send_external") is True
        )
        if not explicit_delivery_target and requested_channels == ["whatsapp"]:
            # Preserve the historical timeline-only behaviour for older
            # clients that inferred WhatsApp from the ticket source without
            # explicitly requesting external delivery.  New CRM clients opt
            # into the enterprise outbox contract by declaring the channel.
            legacy_whatsapp_cutover_blocked = True
            requested_channels = []
        enterprise_whatsapp_reply = bool(
            explicit_delivery_target and requested_channels == ["whatsapp"]
        )
        from services.tenant_ticket_reply_delivery import (
            TenantTicketReplyDeliveryError,
        )

        try:
            if enterprise_whatsapp_reply:
                reply_result = ServicioTickets().crear_respuesta_municipio(
                    ticket,
                    {
                        "body": body,
                        "visibility": reply_visibility,
                        "actor_user_id": current_user.id,
                        "actor_name": current_user.name,
                        "actor_role": current_user.rol,
                        "requested_channels": requested_channels,
                        "template_registry_id": payload.get(
                            "template_registry_id"
                        ),
                        "template_variables": (
                            payload.get("template_variables")
                            if "template_variables" in payload
                            else payload.get("content_variables")
                        ),
                        "emit_socket": True,
                    },
                    idempotency_key=reply_idempotency_key,
                    idempotency_tenant_id=tenant.id,
                    reply_actor=current_user,
                )
                ticket = reply_result["ticket"]
                recent_comment = reply_result["comment"]
                reply_event = dict(reply_result["event"])
                reply_record = reply_result.get("reply_record")
                reply_replayed = bool(reply_result.get("replayed"))
                timeline_updated = not reply_replayed
                aggregate_ref = str(reply_result.get("aggregate_ref") or "")
                if aggregate_ref:
                    reply_outbox_effects = DomainEffectOutbox.query.filter_by(
                        tenant_id=tenant.id,
                        aggregate_type="municipio_ticket_reply",
                        aggregate_ref=aggregate_ref,
                    ).all()
                    reply_outbox_effect_count = len(reply_outbox_effects)
                    reply_outbox_external_effect_count = sum(
                        1
                        for effect in reply_outbox_effects
                        if effect.channel != "realtime"
                    )
            else:
                existing_receipt = TicketDomainEffectReceipt.query.filter_by(
                    tenant_id=tenant.id,
                    idempotency_key=reply_idempotency_key,
                ).one_or_none()
                existing_comment = None
                if (
                    existing_receipt is not None
                    and existing_receipt.effect_kind == "ticket.comment.municipio"
                    and existing_receipt.resource_type == "ticket_comentario"
                ):
                    existing_comment = db.session.get(
                        TicketComentario,
                        existing_receipt.resource_id,
                    )

                current_status = str(ticket.estado or "")
                target_status = (
                    current_status
                    if reply_visibility == "internal"
                    else (
                        "en_proceso"
                        if current_status.lower() in {"nuevo", "open"}
                        else current_status
                    )
                )
                comment_status = (
                    str(existing_comment.estado_ticket or target_status)
                    if existing_comment is not None
                    else target_status
                )
                if existing_receipt is None:
                    ticket.estado = target_status
                recent_comment = ServicioTickets().crear_comentario(
                    ticket.id,
                    "municipio",
                    {
                        "comentario": body,
                        "user_id": current_user.id,
                        "es_admin": True,
                        "origen": (
                            "internal"
                            if reply_visibility == "internal"
                            else "admin_panel"
                        ),
                        "estado_ticket": comment_status,
                        "emit_notifications": bool(requested_channels),
                        "emit_socket": True,
                        "requested_channels": requested_channels,
                    },
                    idempotency_key=reply_idempotency_key,
                    idempotency_tenant_id=tenant.id,
                    legacy_effects_owned_by_caller=True,
                    reply_actor=current_user,
                )
                if recent_comment is None:
                    db.session.rollback()
                    return _error_response(
                        "No se pudo guardar la respuesta",
                        500,
                        "reply_persistence_failed",
                        "retry_with_same_idempotency_key",
                    )
                reply_replayed = existing_receipt is not None
                timeline_updated = not reply_replayed
                if reply_outbox_enabled:
                    reply_outbox_effects = DomainEffectOutbox.query.filter_by(
                        tenant_id=tenant.id,
                        aggregate_type="municipio_ticket_comment",
                        aggregate_ref=str(recent_comment.id),
                    ).all()
                    reply_outbox_effect_count = len(reply_outbox_effects)
                    reply_outbox_external_effect_count = sum(
                        1
                        for effect in reply_outbox_effects
                        if effect.channel != "realtime"
                    )
        except TicketReplyOwnershipError as exc:
            db.session.rollback()
            return _error_response(
                "Toma el ticket antes de responder"
                if exc.reason_code == "ticket_claim_required"
                else "El ticket esta asignado a otro operador",
                409,
                exc.reason_code,
                "claim_ticket"
                if exc.reason_code == "ticket_claim_required"
                else "refresh_inbox",
            )
        except TicketIdempotencyConflict:
            db.session.rollback()
            return _error_response(
                "La identidad idempotente ya fue usada con otra respuesta",
                409,
                "reply_idempotency_payload_conflict",
                "reuse_key_only_for_identical_payload",
            )
        except TicketIdempotencyValidationError:
            db.session.rollback()
            return _error_response(
                "La identidad idempotente o su alcance de tenant no es valido",
                400,
                "reply_idempotency_invalid",
                "send_stable_client_message_id",
            )
        except TicketIdempotencyReplayUnavailable:
            db.session.rollback()
            return _error_response(
                "La respuesta idempotente existe pero su comentario no esta disponible",
                409,
                "reply_idempotency_replay_unavailable",
                "refresh_inbox",
            )
        except TenantTicketReplyDeliveryError as exc:
            db.session.rollback()
            is_window_error = exc.code == "whatsapp_template_required_outside_24h"
            is_template_body_mismatch = exc.code == "whatsapp_template_body_mismatch"
            return _error_response(
                (
                    "La ventana de atención de 24 horas está cerrada. "
                    "Seleccioná una plantilla aprobada para responder por WhatsApp."
                    if is_window_error
                    else (
                        "El texto visible no coincide con la plantilla aprobada. "
                        "Volvé a generar la vista previa antes de enviarla."
                        if is_template_body_mismatch
                        else "La configuración de WhatsApp, la plantilla o sus variables no son válidas."
                    )
                ),
                409 if is_window_error or is_template_body_mismatch else 400,
                exc.code,
                "choose_approved_template"
                if is_window_error
                else (
                    "refresh_approved_template_preview"
                    if is_template_body_mismatch
                    else "review_whatsapp_delivery_fields"
                ),
            )
        except Exception as exc:
            db.session.rollback()
            if not enterprise_whatsapp_reply:
                raise
            current_app.logger.exception(
                "MunicipioTicket durable reply failed ticket=%s tenant=%s: %s",
                ticket.id,
                tenant.id,
                exc,
            )
            return _error_response(
                "No se pudo guardar la respuesta de forma durable",
                503,
                "reply_durability_unavailable",
                "retry_with_same_idempotency_key",
            )

    elif action == "close":
        target_status = _ticket_transition_status("close", payload.get("status"), default="cerrado")
        if target_status is None:
            return _error_response(
                "status no es compatible con la accion close",
                400,
                "ticket_action_status_conflict",
                "send_closed_status",
            )
        ticket.estado = target_status
        body = str(payload.get("body") or "Reclamo cerrado desde la bandeja operativa").strip()
        db.session.add(
            TicketComentario(
                municipio_ticket_id=ticket.id,
                comentario=body,
                user_id=current_user.id,
                es_admin=True,
                origen="admin_panel",
                estado_ticket=ticket.estado,
            )
        )
        timeline_updated = True

    elif action == "reopen":
        target_status = _ticket_transition_status("reopen", payload.get("status"), default="en_proceso")
        if target_status is None:
            return _error_response(
                "status no es compatible con la accion reopen",
                400,
                "ticket_action_status_conflict",
                "send_active_status",
            )
        ticket.estado = target_status
        body = str(payload.get("body") or "Reclamo reabierto desde la bandeja operativa").strip()
        db.session.add(
            TicketComentario(
                municipio_ticket_id=ticket.id,
                comentario=body,
                user_id=current_user.id,
                es_admin=True,
                origen="admin_panel",
                estado_ticket=ticket.estado,
            )
        )
        timeline_updated = True

    if handoff_event_body is not None:
        ticket.datos_extra = extra
        flag_modified(ticket, "datos_extra")
        db.session.add(
            TicketComentario(
                municipio_ticket_id=ticket.id,
                comentario=handoff_event_body,
                user_id=current_user.id,
                es_admin=True,
                origen="internal",
                estado_ticket=action,
            )
        )
        timeline_updated = True

    if action != "reply":
        if not (
            (action == "claim" and claim_idempotent)
            or (action == "assign" and assignment_idempotent)
        ):
            ticket.ultima_actividad = now
            db.session.add(ticket)
        db.session.commit()

    if action not in {"handoff", "accept_handoff", "resume_ai"} and not (
        action == "reply" and (reply_outbox_enabled or reply_replayed)
    ) and not (action == "claim" and claim_idempotent) and not (
        action == "assign" and assignment_idempotent
    ):
        realtime_state_events = _emit_legacy_claim_realtime_state(
            ticket,
            action=action,
            previous_status=previous_status,
        )

    if (
        action == "reply"
        and recent_comment is not None
        and not reply_outbox_enabled
        and not reply_replayed
    ):
        if requested_channels:
            delivery_results, dispatch_error_reason = _dispatch_legacy_claim_reply(
                ticket,
                body,
                recent_comment,
                requested_channels=requested_channels,
            )
        else:
            delivery_results = {"email": False, "sms": False, "whatsapp": False}
            dispatch_error_reason = (
                "legacy_whatsapp_enterprise_cutover_required"
                if legacy_whatsapp_cutover_blocked
                else "external_dispatch_no_channel_requested"
            )
        realtime_emitted = _emit_legacy_claim_realtime_reply(
            ticket,
            recent_comment,
            current_user,
            visibility=reply_visibility,
        )

    external_dispatch = bool(delivery_results and any(delivery_results.values()))
    delivery_reason = None
    if action == "reply":
        if legacy_whatsapp_cutover_blocked:
            delivery_reason = "legacy_whatsapp_enterprise_cutover_required"
        elif reply_outbox_enabled:
            delivery_reason = (
                "idempotent_replay_domain_effects_preserved"
                if reply_replayed and reply_outbox_effect_count
                else (
                    "idempotent_replay_no_redispatch"
                    if reply_replayed
                    else "domain_effects_durably_staged"
                )
            )
        elif reply_replayed:
            delivery_reason = "idempotent_replay_no_redispatch"
        else:
            delivery_reason = (
                "acceptance_unverified"
                if external_dispatch
                else (dispatch_error_reason or "external_dispatch_no_channel_confirmed")
            )
    delivery = _inbox_action_delivery_payload(
        action=action,
        channel=(
            "crm"
            if action in {"claim", "handoff", "accept_handoff", "resume_ai"}
            or (action == "reply" and not requested_channels)
            else _ticket_delivery_channel(delivery_results, ticket.canal_ingreso or "whatsapp")
        ),
        timeline_updated=timeline_updated,
        source_model="MunicipioTicket",
        status=(
            "already_owned"
            if action == "claim" and claim_idempotent
            else (
                "claimed"
                if action == "claim"
                else (
                    "already_assigned"
                    if action == "assign" and assignment_idempotent
                    else ("assigned" if action == "assign" else None)
                )
            )
        ),
        reason=(
            "claim_idempotent_same_operator"
            if action == "claim" and claim_idempotent
            else (
                "claim_acquired"
                if action == "claim"
                else (
                    "assignment_idempotent_same_target"
                    if action == "assign" and assignment_idempotent
                    else ("assignment_applied" if action == "assign" else delivery_reason)
                )
            )
        ),
        external_dispatch=external_dispatch,
        delivery_results=delivery_results,
        requested_channels=(
            [*(requested_channels or []), "realtime"]
            if action == "reply" and reply_outbox_effect_count
            else (requested_channels if action == "reply" else None)
        ),
        durably_staged=(
            action == "reply"
            and bool(reply_outbox_external_effect_count)
            and not reply_replayed
        ),
        idempotent_replay=(
            (action == "reply" and reply_replayed)
            or (action == "assign" and assignment_idempotent)
        ),
        delivery_skipped=(
            {"whatsapp": "legacy_whatsapp_enterprise_cutover_required"}
            if action == "reply" and legacy_whatsapp_cutover_blocked
            else None
        ),
    )
    if action == "claim":
        delivery.update(
            _claim_action_evidence(
                source_model="MunicipioTicket",
                replayed=claim_idempotent,
            )
        )
    if action == "assign":
        delivery["assignment"] = {
            "contract_version": "inbox.assignment_cas.v1",
            "source_model": "MunicipioTicket",
            "ticket_id": ticket.id,
            "expected_assignee_id": assignment_expected_id,
            "assignee_id": assignment_target_id,
            "replayed": assignment_idempotent,
        }
    if action in {"accept_handoff", "resume_ai"}:
        delivery["ledger"] = {
            "contract_version": "municipio_ticket.handoff_follow_up.v1",
            "normalized_event": False,
            "mode": "legacy_projection_only",
            "idempotency_supported": False,
            "external_dispatch": False,
        }
    reply_realtime_event = (
        "ticket_update"
        if action == "reply" and reply_visibility == "internal"
        else "new_chat_message"
    )
    delivery["realtime"] = {
        "emitted": bool(realtime_emitted or realtime_state_events),
        "event": reply_realtime_event if realtime_emitted else (realtime_state_events[0] if realtime_state_events else None),
        "events": ([reply_realtime_event] if realtime_emitted else []) + realtime_state_events,
        "room": (
            f"tenant_{tenant.id}"
            if action == "reply" and reply_visibility == "internal"
            else f"ticket_municipio_{ticket.id}"
        ),
        "fallback": "http_polling",
    }
    if action == "reply":
        if reply_record is not None and "whatsapp" in (requested_channels or []):
            from services.municipio_ticket_reply_delivery import (
                serialize_reply_delivery as serialize_municipio_reply_delivery,
            )

            delivery["final_delivery"] = serialize_municipio_reply_delivery(
                reply_record,
                session=db.session,
            )
            delivery["reply_event_id"] = reply_record.event_id
            delivery["evidence_stage"] = delivery["final_delivery"]["status"]
            delivery["realtime"] = {
                "contract_version": "municipio_ticket.reply.realtime.v1",
                "emitted": False,
                "queued": bool(reply_outbox_effect_count and not reply_replayed),
                "event": "ticket_update" if reply_outbox_effect_count else None,
                "events": (
                    [
                        "ticket_update",
                        *(
                            ["ticket.reply.delivery.updated"]
                            if reply_outbox_external_effect_count
                            else []
                        ),
                    ]
                    if reply_outbox_effect_count
                    else []
                ),
                "delivery_status_event": (
                    "ticket.reply.delivery.updated"
                    if reply_outbox_external_effect_count
                    else None
                ),
                "room": f"tenant_{tenant.id}",
                "scope": "authenticated_tenant_operators",
                "fallback": "http_polling",
                "polling": {
                    "method": "GET",
                    "href": (
                        f"/api/v2/inbox/omnichannel/{ticket.id}"
                        "?source_model=MunicipioTicket"
                    ),
                },
            }
        delivery["idempotency"] = {
            "contract_version": "inbox.reply_idempotency.v1",
            "replayed": reply_replayed,
            "source": reply_idempotency_source,
            "raw_value_persisted": False,
        }
    if action == "reply" and reply_outbox_enabled:
        delivery["outbox"] = {
            "durably_staged": bool(reply_outbox_external_effect_count),
            "effect_count": reply_outbox_effect_count,
            "external_effect_count": reply_outbox_external_effect_count,
            "worker_authoritative": bool(reply_outbox_effect_count),
            "direct_dispatch_performed": False,
        }
    if action == "reply" and reply_replayed:
        durable_extra = _ticket_extra(ticket)
        delivery["receipt_persisted"] = bool(
            reply_record is not None
            or durable_extra.get("reply_delivery_latest_evidence")
            or durable_extra.get("reply_delivery_history")
        )
        if not delivery["receipt_persisted"]:
            delivery["receipt_persistence_reason"] = "original_delivery_receipt_unavailable"
    if action == "reply" and not reply_replayed:
        try:
            delivery["receipt_persisted"] = True
            ticket = _record_ticket_reply_delivery(
                ticket,
                tenant=tenant,
                source_model="MunicipioTicket",
                delivery=delivery,
                actor=current_user,
            )
            db.session.commit()
        except Exception as exc:  # pragma: no cover - reply timeline remains durable
            db.session.rollback()
            delivery["receipt_persisted"] = False
            delivery["receipt_persistence_reason"] = "delivery_receipt_persistence_failed"
            current_app.logger.exception(
                "Error recording MunicipioTicket reply delivery audit ticket=%s: %s",
                ticket.id,
                exc,
            )
    live_chat_status = _tenant_inbox_live_chat_status(tenant)
    ticket_payload = _legacy_claim_inbox_payload(ticket, tenant=tenant, live_chat_status=live_chat_status, actor=current_user)

    return _json_response(
        {
            "ok": True,
            "contract_version": "inbox.omnichannel.action.v1",
            "tenant": _tenant_ref(tenant),
            "action": action,
            "delivery": delivery,
            "live_chat": ticket_payload.get("live_chat"),
            "ticket": ticket_payload,
        }
    )


@v2_saas_bp.route("/inbox/omnichannel/<int:ticket_id>/actions", methods=["POST"])
@v2_saas_bp.route("/inbox/omnichannel/actions", methods=["POST"])
@token_requerido
@require_role("admin", "empleado", "supervisor", "manager", "super_admin")
def omnichannel_inbox_action_v2(current_user, ticket_id: int | None = None):
    payload = _omnichannel_action_json_payload()
    requested_action = str(payload.get("action") or payload.get("type") or "").strip().lower()

    tenant, error = _resolve_tenant_or_error(current_user)
    if error:
        return error

    raw_source_model = payload.get("source_model")
    if raw_source_model in (None, ""):
        return _error_response(
            "source_model es obligatorio para mutar un caso",
            400,
            "source_model_required",
            "send_exact_ticket_identity",
        )
    raw_source_models = [raw_source_model]
    if payload.get("legacy_model") not in (None, ""):
        raw_source_models.append(payload.get("legacy_model"))
    normalized_sources: list[str] = []
    for raw_source_model in raw_source_models:
        if not isinstance(raw_source_model, str) or not raw_source_model.strip():
            return _error_response(
                "source_model no es compatible con este inbox",
                400,
                "unsupported_inbox_source_model",
                "send_tenantticket_or_municipioticket",
            )
        normalized_source_model = str(raw_source_model).strip().lower()
        if normalized_source_model in {"tenantticket", "tenant_ticket", "tenant"}:
            normalized_sources.append("TenantTicket")
        elif normalized_source_model in {"municipioticket", "municipio_ticket", "municipio"}:
            normalized_sources.append("MunicipioTicket")
        else:
            return _error_response(
                "source_model no es compatible con este inbox",
                400,
                "unsupported_inbox_source_model",
                "send_tenantticket_or_municipioticket",
            )
    if len(set(normalized_sources)) > 1:
        return _error_response(
            "source_model y legacy_model deben identificar el mismo origen",
            409,
            "ticket_identity_conflict",
            "refresh_ticket_identity",
        )
    source_model = normalized_sources[0]

    body_ids: list[int] = []
    for key in ("legacy_id", "ticket_id", "id"):
        if payload.get(key) in (None, ""):
            continue
        parsed_id = _coerce_inbox_ticket_id(payload.get(key))
        if parsed_id is None:
            return _error_response("ticket_id no es valido", 400, "ticket_id_invalid", "send_ticket_id")
        body_ids.append(parsed_id)
    if len(set(body_ids)) > 1 or (ticket_id is not None and body_ids and any(item != ticket_id for item in body_ids)):
        return _error_response(
            "source_model y ticket_id deben identificar el mismo caso",
            409,
            "ticket_identity_conflict",
            "refresh_ticket_identity",
        )

    raw_ticket_id = ticket_id if ticket_id is not None else (body_ids[0] if body_ids else None)
    resolved_ticket_id = _coerce_inbox_ticket_id(raw_ticket_id)
    if resolved_ticket_id is None:
        if raw_ticket_id is not None:
            return _error_response("ticket_id no es valido", 400, "ticket_id_invalid", "send_ticket_id")
        return _error_response("ticket_id es obligatorio", 400, "ticket_id_required", "send_ticket_id")

    if source_model == "MunicipioTicket":
        return _omnichannel_legacy_claim_action_v2(current_user, tenant, resolved_ticket_id, payload)

    ticket = (
        TenantTicket.query.filter_by(id=resolved_ticket_id, tenant_id=tenant.id)
        .with_for_update()
        .first()
    )
    if not ticket or not employee_ticket_category_access_allows(current_user, ticket):
        return _error_response("Ticket no encontrado", 404, "ticket_not_found", "refresh_inbox")

    action = requested_action
    if action not in {
        "claim",
        "assign",
        "reply",
        "handoff",
        "accept_handoff",
        "resume_ai",
        "close",
        "reopen",
        "set_priority",
        "attach_file",
        "share_location",
        "send_form",
    }:
        return _error_response("Accion de inbox no soportada", 400, "unsupported_inbox_action", "send_supported_action")

    extra = deepcopy(_ticket_extra(ticket))
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    handoff_state = _handoff_lifecycle_state(
        extra.get("handoff") if isinstance(extra.get("handoff"), Mapping) else None
    )
    transition_error = _validate_handoff_transition(action, handoff_state)
    if transition_error is not None:
        return transition_error

    event_body = ""
    timeline_updated = False
    reply_visibility = "public"
    requested_channels: list[str] | None = None
    reply_event: dict[str, Any] | None = None
    reply_record: TenantTicketReplyEvent | None = None
    reply_replayed = False
    reply_idempotency_source: str | None = None
    reply_outbox_effect_count = 0
    reply_outbox_external_effect_count = 0
    realtime_emitted = False
    claim_idempotent = False
    assignment_idempotent = False
    assignment_expected_id: int | None = None
    assignment_target_id: int | None = None

    if action in _OPERATIONAL_OWNERSHIP_ACTIONS:
        ownership_error = _operational_ownership_error(current_user, extra.get("assignee_id"))
        if ownership_error is not None:
            return ownership_error

    if action in _CRM_ARTIFACT_ACTIONS:
        if str(ticket.estado or "").lower() in _CLOSED_TICKET_STATES:
            return _error_response(
                "El ticket esta cerrado. Reabrilo antes de agregar recursos.", 403,
                "ticket_closed", "reopen_ticket",
            )
        return _crm_artifact_action_response(
            payload=payload, tenant=tenant, actor=current_user,
            source_model="TenantTicket", ticket=ticket, action=action,
        )

    if action == "claim":
        raw_current_assignee_id = extra.get("assignee_id")
        current_assignee_id = _coerce_inbox_ticket_id(raw_current_assignee_id)
        if raw_current_assignee_id is not None and raw_current_assignee_id != "":
            if current_assignee_id != current_user.id:
                return _error_response(
                    "El ticket ya fue tomado por otro operador",
                    409,
                    "already_claimed",
                    "refresh_inbox",
                )
            claim_idempotent = True
            event_body = f"Ticket ya estaba tomado por {current_user.name}"
        else:
            if not ticket_assignee_is_compatible(current_user, ticket):
                return _error_response(
                    "El operador no tiene acceso a la categoria del ticket",
                    404,
                    "ticket_not_found",
                    "refresh_inbox",
                )
            extra["assignee_id"] = current_user.id
            extra["assignee_name"] = current_user.name
            extra["assignee_email"] = current_user.email
            if str(ticket.estado or "").lower() in {"nuevo", "open"}:
                ticket.estado = "en_proceso"
            event_body = f"Ticket tomado por {current_user.name}"

    elif action == "assign":
        assignee_id, assignee_error = _resolve_aliased_inbox_id(
            payload,
            keys=("assignee_id", "user_id"),
            required_message="assignee_id es obligatorio",
            required_reason="assignee_required",
            conflict_message="assignee_id y user_id deben identificar el mismo empleado",
            conflict_reason="assignee_identity_conflict",
        )
        if assignee_error is not None:
            return assignee_error
        assignment_target_id = assignee_id
        try:
            transition = assignment_transition(
                actor=current_user,
                payload=payload,
                current_assignee_id=extra.get("assignee_id"),
                target_assignee_id=assignee_id,
            )
        except TicketAssignmentPolicyError as exc:
            return _assignment_policy_error(exc)
        assignment_expected_id = transition.expected_assignee_id
        assignee = User.query.filter_by(id=assignee_id, tenant_id=tenant.id).first()
        if not ticket_assignee_is_operational(assignee):
            return _error_response("Empleado no encontrado para este tenant", 404, "assignee_not_found", "choose_valid_assignee")
        if not ticket_assignee_is_compatible(assignee, ticket):
            return _error_response(
                "El agente no tiene acceso a la categoria del ticket",
                409,
                "assignee_category_scope_mismatch",
                "choose_compatible_assignee",
            )
        if transition.replayed:
            assignment_idempotent = True
            event_body = f"El ticket ya estaba asignado a {assignee.name}"
        else:
            extra["assignee_id"] = assignee.id
            extra["assignee_name"] = assignee.name
            extra["assignee_email"] = assignee.email
            if ticket.estado in {"nuevo", "open"}:
                ticket.estado = "en_proceso"
            event_body = f"Asignado a {assignee.name}"

    elif action == "handoff":
        channel = _normalize_handoff_channel(payload.get("channel") or payload.get("target_channel"))
        if channel is None:
            return _error_response(
                "El canal de handoff no es valido",
                400,
                "invalid_handoff_channel",
                "choose_supported_handoff_channel",
            )
        _apply_handoff_transition(
            extra,
            action="handoff",
            actor=current_user,
            occurred_at=now_iso,
            channel=channel,
            reason=payload.get("reason"),
        )
        if str(ticket.estado or "").lower() in {"nuevo", "open"}:
            ticket.estado = "en_proceso"
        event_body = f"Handoff solicitado al equipo ({channel})"

    elif action == "accept_handoff":
        current_assignee_id = _coerce_inbox_ticket_id(extra.get("assignee_id"))
        if not ticket_assignee_is_compatible(current_user, ticket):
            return _error_response(
                "El operador no tiene acceso a la categoria del ticket",
                404,
                "ticket_not_found",
                "refresh_inbox",
            )
        recipient_error = _validate_handoff_recipient(
            extra,
            actor=current_user,
            current_assignee_id=current_assignee_id,
        )
        if recipient_error is not None:
            return recipient_error
        transferred_from = (
            {
                "id": current_assignee_id,
                "name": extra.get("assignee_name"),
                "email": extra.get("assignee_email"),
            }
            if current_assignee_id is not None and current_assignee_id != current_user.id
            else None
        )
        _apply_handoff_transition(
            extra,
            action="accept_handoff",
            actor=current_user,
            occurred_at=now_iso,
            transferred_from=transferred_from,
        )
        extra["assignee_id"] = current_user.id
        extra["assignee_name"] = current_user.name
        extra["assignee_email"] = current_user.email
        if str(ticket.estado or "").lower() in {"nuevo", "open"}:
            ticket.estado = "en_proceso"
        event_body = f"Conversación tomada por {current_user.name}"

    elif action == "resume_ai":
        _apply_handoff_transition(extra, action="resume_ai", actor=current_user, occurred_at=now_iso)
        event_body = f"Conversación devuelta a IA por {current_user.name}"

    elif action == "reply":
        if str(ticket.estado or "").lower() in _CLOSED_TICKET_STATES:
            return _error_response(
                "El ticket esta cerrado. Reabrilo antes de responder.",
                403,
                "ticket_closed",
                "reopen_ticket",
            )
        body, body_error = _omnichannel_reply_body(payload)
        if body_error is not None:
            return body_error
        raw_visibility = payload["visibility"] if "visibility" in payload else "public"
        if not isinstance(raw_visibility, str):
            return _error_response(
                "La visibilidad de la respuesta no es válida.",
                400,
                "reply_visibility_invalid",
                "choose_public_or_internal_visibility",
            )
        visibility = raw_visibility.strip().lower()
        if visibility not in {"public", "internal"}:
            return _error_response(
                "La visibilidad de la respuesta no es válida.",
                400,
                "reply_visibility_invalid",
                "choose_public_or_internal_visibility",
            )
        reply_visibility = visibility
        event_body = body
        reply_idempotency_key, reply_idempotency_source, idempotency_error = (
            _omnichannel_reply_idempotency_identity(payload, tenant_id=tenant.id)
        )
        if idempotency_error is not None:
            return idempotency_error
        requested_channels, channel_contract_error = _tenant_ticket_delivery_channels(
            ticket,
            payload,
            visibility=reply_visibility,
        )
        if channel_contract_error is not None:
            return _error_response(
                "La configuración de entrega de la respuesta no es válida.",
                400,
                channel_contract_error,
                "review_reply_delivery_fields",
            )
        source_channel = _ticket_channel(ticket)
        source_is_whatsapp = source_channel in {
            "whatsapp", "wa", "twilio", "whatsapp_business"
        }
        source_is_email = source_channel in {"email", "mail", "correo"}
        if "whatsapp" in requested_channels and not source_is_whatsapp:
            return _error_response(
                "Este expediente no se originó en WhatsApp; no se habilitó una salida externa por ese canal.",
                409,
                "reply_channel_source_mismatch",
                "use_original_channel_or_internal_note",
            )
        if "email" in requested_channels and not source_is_email:
            return _error_response(
                "Este expediente no se originó por email; no se habilitó una salida externa por ese canal.",
                409,
                "reply_channel_source_mismatch",
                "use_original_channel_or_internal_note",
            )
        if "template_variables" in payload and "content_variables" in payload:
            return _error_response(
                "Usá un solo campo para las variables de la plantilla.",
                400,
                "whatsapp_template_variable_alias_conflict",
                "send_template_variables_only",
            )
        from services.domain_effect_gate import resolve_domain_effect_outbox_policy

        tenant_outbox_enabled = resolve_domain_effect_outbox_policy(
            current_app.config, tenant_id=int(tenant.id)
        ).enabled
        persisted_delivery_channels = [
            channel
            for channel in requested_channels
            if channel != "whatsapp" or tenant_outbox_enabled
        ]

        from services.ticket_service import (
            ServicioTickets,
            TicketIdempotencyConflict,
            TicketIdempotencyReplayUnavailable,
            TicketIdempotencyValidationError,
            TicketReplyOwnershipError,
        )
        from services.tenant_ticket_reply_delivery import (
            TenantTicketReplyDeliveryError,
        )

        try:
            reply_result = ServicioTickets().crear_respuesta_tenant(
                ticket,
                {
                    "body": body,
                    "visibility": reply_visibility,
                    "actor_user_id": current_user.id,
                    "actor_name": current_user.name,
                    "actor_role": current_user.rol,
                    "requested_channels": persisted_delivery_channels,
                    "template_registry_id": (
                        payload.get("template_registry_id")
                        if "whatsapp" in persisted_delivery_channels
                        else None
                    ),
                    "template_variables": (
                        (
                            payload.get("template_variables")
                            if "template_variables" in payload
                            else payload.get("content_variables")
                        )
                        if "whatsapp" in persisted_delivery_channels
                        else None
                    ),
                    "emit_socket": True,
                },
                idempotency_key=reply_idempotency_key,
                idempotency_tenant_id=tenant.id,
                reply_actor=current_user,
            )
        except TicketReplyOwnershipError as exc:
            db.session.rollback()
            return _error_response(
                "Toma el ticket antes de responder"
                if exc.reason_code == "ticket_claim_required"
                else "El ticket esta asignado a otro operador",
                409,
                exc.reason_code,
                "claim_ticket"
                if exc.reason_code == "ticket_claim_required"
                else "refresh_inbox",
            )
        except TicketIdempotencyConflict:
            db.session.rollback()
            return _error_response(
                "La identidad idempotente ya fue usada con otra respuesta",
                409,
                "reply_idempotency_payload_conflict",
                "reuse_key_only_for_identical_payload",
            )
        except TicketIdempotencyValidationError:
            db.session.rollback()
            return _error_response(
                "La identidad idempotente o su alcance de tenant no es valido",
                400,
                "reply_idempotency_invalid",
                "send_stable_client_message_id",
            )
        except TicketIdempotencyReplayUnavailable:
            db.session.rollback()
            return _error_response(
                "La respuesta idempotente existe pero su ticket no esta disponible",
                409,
                "reply_idempotency_replay_unavailable",
                "refresh_inbox",
            )
        except TenantTicketReplyDeliveryError as exc:
            db.session.rollback()
            is_window_error = exc.code == "whatsapp_template_required_outside_24h"
            is_template_body_mismatch = exc.code == "whatsapp_template_body_mismatch"
            return _error_response(
                (
                    "La ventana de atención de 24 horas está cerrada. "
                    "Seleccioná una plantilla aprobada para responder por WhatsApp."
                    if is_window_error
                    else (
                        "El texto visible no coincide con la plantilla aprobada. "
                        "Volvé a generar la vista previa antes de enviarla."
                        if is_template_body_mismatch
                        else "La configuración de WhatsApp, la plantilla o sus variables no son válidas."
                    )
                ),
                409 if is_window_error or is_template_body_mismatch else 400,
                exc.code,
                "choose_approved_template"
                if is_window_error
                else (
                    "refresh_approved_template_preview"
                    if is_template_body_mismatch
                    else "review_whatsapp_delivery_fields"
                ),
            )
        except Exception as exc:
            db.session.rollback()
            current_app.logger.exception(
                "TenantTicket durable reply failed ticket=%s tenant=%s: %s",
                ticket.id,
                tenant.id,
                exc,
            )
            return _error_response(
                "No se pudo guardar la respuesta de forma durable",
                503,
                "reply_durability_unavailable",
                "retry_with_same_idempotency_key",
            )

        ticket = reply_result["ticket"]
        reply_event = dict(reply_result["event"])
        reply_record = reply_result.get("reply_record")
        reply_replayed = bool(reply_result.get("replayed"))
        timeline_updated = not reply_replayed
        aggregate_ref = str(reply_result.get("aggregate_ref") or "")
        if aggregate_ref:
            reply_outbox_effects = DomainEffectOutbox.query.filter_by(
                tenant_id=tenant.id,
                aggregate_type="tenant_ticket_reply",
                aggregate_ref=aggregate_ref,
            ).all()
            reply_outbox_effect_count = len(reply_outbox_effects)
            reply_outbox_external_effect_count = sum(
                1 for effect in reply_outbox_effects if effect.channel != "realtime"
            )

    elif action == "close":
        target_status = _ticket_transition_status("close", payload.get("status"), default="cerrado")
        if target_status is None:
            return _error_response(
                "status no es compatible con la accion close",
                400,
                "ticket_action_status_conflict",
                "send_closed_status",
            )
        ticket.estado = target_status
        extra["closed_at"] = now_iso
        extra["closed_by"] = {"id": current_user.id, "name": current_user.name}
        event_body = payload.get("body") or "Ticket cerrado"

    elif action == "reopen":
        target_status = _ticket_transition_status("reopen", payload.get("status"), default="nuevo")
        if target_status is None:
            return _error_response(
                "status no es compatible con la accion reopen",
                400,
                "ticket_action_status_conflict",
                "send_active_status",
            )
        ticket.estado = target_status
        extra["reopened_at"] = now_iso
        extra["reopened_by"] = {"id": current_user.id, "name": current_user.name}
        event_body = payload.get("body") or "Ticket reabierto"

    elif action == "set_priority":
        priority = str(payload.get("priority") or "").strip().lower()
        if priority not in {"low", "medium", "high", "urgent"}:
            return _error_response(
                "priority debe ser low, medium, high o urgent",
                400,
                "priority_invalid",
                "send_supported_priority",
            )
        extra["priority"] = priority
        event_body = f"Prioridad actualizada: {priority}"

    if action != "reply" and not (
        (action == "claim" and claim_idempotent)
        or (action == "assign" and assignment_idempotent)
    ):
        _append_ticket_event(extra, action=action, actor=current_user, body=str(event_body or action), visibility="internal")
        timeline_updated = True

    if action in {"close", "reopen", "set_priority"}:
        ticket.datos_extra = extra
        if action == "close":
            apply_resolution_sla(ticket, occurred_at=now)
        else:
            policies = get_policies_for_tenant(tenant)
            if action == "reopen":
                apply_reopen_sla(ticket, policies, occurred_at=now)
            else:
                apply_priority_change_sla(ticket, policies, occurred_at=now)
        extra = deepcopy(_ticket_extra(ticket))

    if action != "reply":
        if not (
            (action == "claim" and claim_idempotent)
            or (action == "assign" and assignment_idempotent)
        ):
            ticket.datos_extra = extra
            flag_modified(ticket, "datos_extra")
            ticket.updated_at = now
            db.session.add(ticket)
        db.session.commit()

    delivery_results: dict[str, bool] | None = None
    dispatch_reason: str | None = None
    delivery_skipped: dict[str, str] | None = None
    external_dispatch = False
    if (
        action == "reply"
        and not reply_replayed
        and not reply_outbox_effect_count
    ):
        legacy_dispatch_channels = [
            channel
            for channel in (requested_channels or [])
            if channel != "whatsapp"
        ]
        if legacy_dispatch_channels:
            delivery_results, dispatch_reason, delivery_skipped = (
                _dispatch_tenant_ticket_reply(
                    tenant=tenant,
                    ticket=ticket,
                    body=event_body,
                    requested_channels=legacy_dispatch_channels,
                    reply_record=reply_record,
                )
            )
            if "whatsapp" in (requested_channels or []):
                delivery_skipped["whatsapp"] = "whatsapp_outbox_cutover_required"
        elif "whatsapp" in (requested_channels or []):
            delivery_results = {"email": False, "sms": False, "whatsapp": False}
            dispatch_reason = "whatsapp_outbox_cutover_required"
            delivery_skipped = {
                "whatsapp": "whatsapp_outbox_cutover_required"
            }
        else:
            delivery_results, dispatch_reason, delivery_skipped = (
                _dispatch_tenant_ticket_reply(
                    tenant=tenant,
                    ticket=ticket,
                    body=event_body,
                    requested_channels=[],
                    reply_record=reply_record,
                )
            )
        external_dispatch = any(delivery_results.values())
        if reply_event is not None:
            realtime_emitted = _emit_tenant_ticket_realtime_reply(ticket, reply_event)

    delivery_reason = dispatch_reason
    if action == "reply":
        if reply_replayed:
            delivery_reason = (
                "idempotent_replay_domain_effects_preserved"
                if reply_outbox_external_effect_count
                else "idempotent_replay_no_redispatch"
            )
        elif reply_outbox_external_effect_count:
            delivery_reason = "domain_effects_durably_staged"
        elif external_dispatch:
            delivery_reason = "acceptance_unverified"

    delivery = _inbox_action_delivery_payload(
        action=action,
        channel=_ticket_delivery_channel(
            delivery_results,
            (
                requested_channels[0]
                if requested_channels
                else (
                    "crm"
                    if action in {"claim", "handoff", "accept_handoff", "resume_ai"}
                    else _ticket_channel(ticket)
                )
            ),
        ),
        timeline_updated=timeline_updated,
        source_model="TenantTicket",
        status=(
            "already_owned"
            if action == "claim" and claim_idempotent
            else (
                "claimed"
                if action == "claim"
                else (
                    "already_assigned"
                    if action == "assign" and assignment_idempotent
                    else ("assigned" if action == "assign" else None)
                )
            )
        ),
        reason=(
            "claim_idempotent_same_operator"
            if action == "claim" and claim_idempotent
            else (
                "claim_acquired"
                if action == "claim"
                else (
                    "assignment_idempotent_same_target"
                    if action == "assign" and assignment_idempotent
                    else ("assignment_applied" if action == "assign" else delivery_reason)
                )
            )
        ),
        external_dispatch=external_dispatch,
        delivery_results=delivery_results,
        requested_channels=requested_channels,
        delivery_skipped=delivery_skipped,
        durably_staged=(
            action == "reply"
            and bool(reply_outbox_external_effect_count)
            and not reply_replayed
        ),
        idempotent_replay=(
            (action == "reply" and reply_replayed)
            or (action == "assign" and assignment_idempotent)
        ),
    )
    if action == "claim":
        delivery.update(
            _claim_action_evidence(
                source_model="TenantTicket",
                replayed=claim_idempotent,
            )
        )
    if action == "assign":
        delivery["assignment"] = {
            "contract_version": "inbox.assignment_cas.v1",
            "source_model": "TenantTicket",
            "ticket_id": ticket.id,
            "expected_assignee_id": assignment_expected_id,
            "assignee_id": assignment_target_id,
            "replayed": assignment_idempotent,
        }
    if action == "reply":
        if reply_record is not None and "whatsapp" in (requested_channels or []):
            from services.tenant_ticket_reply_delivery import serialize_reply_delivery

            delivery["final_delivery"] = serialize_reply_delivery(
                reply_record, session=db.session
            )
            delivery["reply_event_id"] = reply_record.event_id
            delivery["evidence_stage"] = delivery["final_delivery"]["status"]
        delivery["idempotency"] = {
            "contract_version": "inbox.reply_idempotency.v1",
            "replayed": reply_replayed,
            "source": reply_idempotency_source,
            "raw_value_persisted": False,
        }
        delivery["realtime"] = {
            "contract_version": "tenant_ticket.reply.realtime.v1",
            "emitted": realtime_emitted,
            "queued": bool(reply_outbox_effect_count and not reply_replayed),
            "event": "ticket_update" if (realtime_emitted or reply_outbox_effect_count) else None,
            "events": (
                [
                    "ticket_update",
                    *(
                        ["ticket.reply.delivery.updated"]
                        if reply_outbox_external_effect_count
                        else []
                    ),
                ]
                if (realtime_emitted or reply_outbox_effect_count)
                else []
            ),
            "delivery_status_event": (
                "ticket.reply.delivery.updated"
                if reply_outbox_external_effect_count
                else None
            ),
            "room": f"tenant_{tenant.id}",
            "scope": "authenticated_tenant_operators",
            "fallback": "http_polling",
            "polling": {
                "method": "GET",
                "href": f"/api/v2/inbox/omnichannel/{ticket.id}",
            },
        }
        if reply_outbox_effect_count:
            delivery["outbox"] = {
                "durably_staged": bool(reply_outbox_external_effect_count),
                "effect_count": reply_outbox_effect_count,
                "external_effect_count": reply_outbox_external_effect_count,
                "worker_authoritative": True,
                "direct_dispatch_performed": False,
            }
        if reply_replayed:
            durable_extra = _ticket_extra(ticket)
            delivery["receipt_persisted"] = bool(
                durable_extra.get("reply_delivery_latest_evidence")
                or durable_extra.get("reply_delivery_history")
            )
            if not delivery["receipt_persisted"]:
                delivery["receipt_persistence_reason"] = "original_delivery_receipt_unavailable"
        if not reply_replayed:
            try:
                delivery["receipt_persisted"] = True
                ticket = _record_ticket_reply_delivery(
                    ticket,
                    tenant=tenant,
                    source_model="TenantTicket",
                    delivery=delivery,
                    actor=current_user,
                )
                db.session.commit()
            except Exception as exc:  # pragma: no cover - reply is already durable in the timeline
                db.session.rollback()
                delivery["receipt_persisted"] = False
                delivery["receipt_persistence_reason"] = "delivery_receipt_persistence_failed"
                current_app.logger.exception(
                    "Error recording TenantTicket reply delivery audit ticket=%s: %s",
                    ticket.id,
                    exc,
                )
    live_chat_status = _tenant_inbox_live_chat_status(tenant)
    ticket_payload = _inbox_ticket_payload(ticket, tenant=tenant, live_chat_status=live_chat_status, actor=current_user)

    return _json_response(
        {
            "ok": True,
            "contract_version": "inbox.omnichannel.action.v1",
            "tenant": _tenant_ref(tenant),
            "action": action,
            "delivery": delivery,
            "live_chat": ticket_payload.get("live_chat"),
            "ticket": ticket_payload,
        }
    )
