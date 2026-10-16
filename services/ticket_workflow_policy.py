"""Private legacy ticket workflow: one policy for publication and mutation.

This contract does not authorize TenantTicket, public tracking, or inbox actions.
It describes only the existing Municipal/Pyme PUT state route.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from flask import g, has_request_context, request
from sqlalchemy import func, select

from models import MunicipioTicket, PymeTicket, TenantProfile
from services.employee_ticket_access import employee_ticket_category_access_allows
from services.tenant_ticket_scope import municipio_ticket_belongs_to_tenant
from utils.roles import ROLE_EMPLEADO, ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, canonical_role, is_authorized_superadmin_user
from utils.tenant import TENANT_HEADER_KEYS, TENANT_QUERY_KEYS
from utils.tenant_admin_access import resolve_consistent_user_tenant

ALLOWED_STATES = ("nuevo", "en_proceso", "en_vivo", "esperando_agente_en_vivo", "cerrado")
ALLOWED_TRANSITIONS = {
    "nuevo": ("en_proceso", "cerrado"),
    "en_proceso": ("en_vivo", "esperando_agente_en_vivo", "cerrado"),
    "en_vivo": ("en_proceso", "cerrado"),
    "esperando_agente_en_vivo": ("en_vivo", "en_proceso", "cerrado"),
    "cerrado": (),
}


class TicketWorkflowError(ValueError):
    def __init__(self, code: str, status: int, message: str):
        super().__init__(message)
        self.code, self.status = code, status


def canonical_state(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    state = value.strip().lower()
    state = "cerrado" if state == "resuelto" else state
    return state if state in ALLOWED_STATES else None


def resolve_private_ticket_tenant(actor, *, body: Mapping[str, Any] | None = None):
    """Explicit selectors must all agree; never fall back after a denial."""
    from utils.auth_helpers import _explicit_admin_request_tenant

    selected, explicit = _explicit_admin_request_tenant() if has_request_context() else (None, False)
    slugs, ids = [], []
    if has_request_context():
        slugs.extend(value for key in TENANT_QUERY_KEYS for value in request.args.getlist(key))
        slugs.extend(request.headers[key] for key in TENANT_HEADER_KEYS if key in request.headers)
        ids.extend(request.args.getlist("tenant_id"))
        if "X-Tenant-Id" in request.headers:
            ids.append(request.headers["X-Tenant-Id"])
        view = request.view_args or {}
        slugs.extend(view[key] for key in ("tenant_slug", "tenant") if key in view)
        if "tenant_id" in view:
            ids.append(view["tenant_id"])
    if body is not None:
        slugs.extend(body[key] for key in TENANT_QUERY_KEYS if key in body)
        if "tenant_id" in body:
            ids.append(body["tenant_id"])
    explicit = explicit or bool(slugs or ids)
    if explicit:
        if any(not isinstance(value, str) or not value.strip() for value in slugs):
            raise TicketWorkflowError("invalid_tenant_selector", 400, "La organización solicitada no es válida.")
        normalized_slugs = {value.strip().lower() for value in slugs}
        normalized_ids = set()
        for value in ids:
            if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip().isdigit():
                raise TicketWorkflowError("invalid_tenant_selector", 400, "La organización solicitada no es válida.")
            normalized_ids.add(int(value))
        if len(normalized_slugs) > 1 or len(normalized_ids) > 1 or any(value <= 0 for value in normalized_ids):
            raise TicketWorkflowError("invalid_tenant_selector", 400, "Los selectores de organización no coinciden.")
        # The existing selector helper covers standard headers/query/path;
        # legacy aliases and JSON selectors are checked against the same row.
        if selected is None:
            if normalized_slugs:
                selected = TenantProfile.query.filter(func.lower(TenantProfile.slug) == next(iter(normalized_slugs))).one_or_none()
            elif normalized_ids:
                from models import db
                selected = db.session.get(TenantProfile, next(iter(normalized_ids)))
        if (selected is None
                or (normalized_slugs and normalized_slugs != {str(selected.slug).strip().lower()})
                or (normalized_ids and normalized_ids != {selected.id})):
            raise TicketWorkflowError("invalid_tenant_selector", 400, "La organización solicitada es desconocida o contradictoria.")
    else:
        selected = resolve_consistent_user_tenant(actor)
    if selected is None or getattr(selected, "is_active", True) is not True:
        raise TicketWorkflowError("tenant_scope_unavailable", 403, "No se pudo verificar una organización activa.")
    if not is_authorized_superadmin_user(actor):
        membership = resolve_consistent_user_tenant(actor)
        if membership is None or membership.id != selected.id:
            raise TicketWorkflowError("tenant_forbidden", 403, "No tenés acceso a la organización solicitada.")
    return selected


def ticket_belongs_to_workflow_tenant(ticket, ticket_type: str, tenant) -> bool:
    if ticket_type == "municipio" and isinstance(ticket, MunicipioTicket):
        return municipio_ticket_belongs_to_tenant(ticket, tenant)
    if ticket_type != "pyme" or not isinstance(ticket, PymeTicket) or tenant is None:
        return False
    if ticket.tenant_id is not None:
        return ticket.tenant_id == tenant.id
    # PymeTicket has no owner column. A shared rubro is not tenant provenance.
    return False


def _actor_block_reason(actor, tenant, *, membership_verified=False) -> str | None:
    from utils.auth_helpers import is_demo_user_account, is_user_auth_disabled

    if actor is None or is_user_auth_disabled(actor) or is_demo_user_account(actor):
        return "panel_actor_required"
    if has_request_context():
        payload = getattr(g, "token_payload", {}) or {}
        if getattr(g, "widget_session", False) or payload.get("session_kind") in {"widget", "demo"}:
            return "panel_actor_required"
    role = canonical_role(getattr(actor, "rol", None))
    if role not in {ROLE_TENANT_ADMIN, ROLE_EMPLEADO, ROLE_SUPERADMIN}:
        return "workflow_role_forbidden"
    if role == ROLE_SUPERADMIN:
        if not is_authorized_superadmin_user(actor):
            return "workflow_role_forbidden"
    elif not membership_verified:
        membership = resolve_consistent_user_tenant(actor)
        if membership is None or tenant is None or membership.id != tenant.id:
            return "tenant_forbidden"
    if tenant is None or getattr(tenant, "is_active", True) is not True:
        return "tenant_scope_unavailable"
    return None


@dataclass(frozen=True)
class VerifiedWorkflowContext:
    """Response-local verified selection; never stored globally or reused for PUT."""
    actor_id: int | None
    tenant: TenantProfile | None
    blocked_reason: str | None


def build_workflow_context(actor, *, tenant=None) -> VerifiedWorkflowContext:
    reason = None
    try:
        body = request.get_json(silent=True) if has_request_context() and request.is_json else None
        selected = resolve_private_ticket_tenant(actor, body=body if isinstance(body, Mapping) else None)
        if tenant is not None and selected.id != tenant.id:
            raise TicketWorkflowError("tenant_forbidden", 403, "Organización contradictoria.")
        tenant = selected
    except TicketWorkflowError as error:
        reason = error.code
    return VerifiedWorkflowContext(getattr(actor, "id", None), tenant,
        reason or _actor_block_reason(actor, tenant, membership_verified=True))


def workflow_block_reason(ticket, ticket_type: str, actor, tenant, *, membership_verified=False) -> str | None:
    reason = _actor_block_reason(actor, tenant, membership_verified=membership_verified)
    if reason:
        return reason
    role = canonical_role(getattr(actor, "rol", None))
    if not ticket_belongs_to_workflow_tenant(ticket, ticket_type, tenant):
        return "ticket_scope_forbidden"
    if not employee_ticket_category_access_allows(actor, ticket):
        return "employee_category_forbidden"
    if (role == ROLE_EMPLEADO or bool(getattr(actor, "es_empleado", False))) and ticket.asignado_a_id != actor.id:
        return "ticket_not_assigned_to_actor"
    if canonical_state(ticket.estado) is None:
        return "workflow_state_unavailable"
    return None


def build_workflow_instance(ticket, ticket_type: str, *, actor=None, tenant=None, context=None) -> dict:
    context = context or build_workflow_context(actor, tenant=tenant)
    reason = context.blocked_reason
    if context.actor_id != getattr(actor, "id", None) or (tenant is not None and context.tenant is not None and tenant.id != context.tenant.id):
        reason = "workflow_context_mismatch"
    tenant = context.tenant
    reason = reason or workflow_block_reason(ticket, ticket_type, actor, tenant, membership_verified=True)
    state = canonical_state(ticket.estado)
    final = state == "cerrado"
    next_states = list(ALLOWED_TRANSITIONS.get(state, ())) if reason is None else []
    return {
        "contract_version": "ticket.workflow.instance.v2",
        "identity": {"tenant_id": getattr(tenant, "id", None), "tenant_slug": getattr(tenant, "slug", None),
            "ticket_id": ticket.id, "source_model": "MunicipioTicket" if ticket_type == "municipio" else "PymeTicket"},
        "current_state": ticket.estado, "canonical_state": state,
        "next_states": next_states, "can_transition": bool(next_states), "final_state": final,
        "blocked_reason": reason or ("workflow_final_state" if final else None),
        "mutation": {"method": "PUT", "endpoint": f"/api/tickets/{ticket_type}/{ticket.id}/estado", "expected_estado": ticket.estado},
    }


def lock_and_validate_workflow_command(session, model, ticket_id: int, *, actor, tenant, ticket_type: str, data: Mapping[str, Any]):
    """Refresh and compare under the DB row lock, before any business effect."""
    ticket = session.execute(select(model).where(model.id == ticket_id).with_for_update()
        .execution_options(populate_existing=True)).scalar_one_or_none()
    if ticket is None:
        raise TicketWorkflowError("ticket_not_found", 404, "Ticket no encontrado.")
    reason = workflow_block_reason(ticket, ticket_type, actor, tenant)
    if reason:
        raise TicketWorkflowError(reason, 404 if reason in {"ticket_scope_forbidden", "employee_category_forbidden"} else 403, "No tenés permiso para cambiar este ticket.")
    expected = canonical_state(data.get("expected_estado"))
    destination = canonical_state(data.get("estado"))
    if expected is None:
        raise TicketWorkflowError("workflow_expected_state_invalid", 400, "Se requiere un estado esperado válido.")
    if destination is None:
        raise TicketWorkflowError("workflow_destination_invalid", 400, "El nuevo estado no es válido.")
    current = canonical_state(ticket.estado)
    if expected != current:
        raise TicketWorkflowError("stale_ticket_state", 409, "El estado del ticket cambió. Volvé a consultarlo.")
    if destination not in ALLOWED_TRANSITIONS.get(current, ()):
        raise TicketWorkflowError("workflow_transition_not_allowed", 422, "La transición solicitada no está permitida.")
    return ticket, destination
