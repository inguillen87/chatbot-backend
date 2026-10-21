from __future__ import annotations

from typing import Any
import uuid

from flask import Blueprint, g, jsonify, request

from extensions import db
from routes.v2.tenants import V2TenantResolutionError, resolve_tenant_v2
from services.v2.sla_service import (
    SlaPolicyValidationError,
    detect_sla_breaches_for_tenant,
    get_policies_for_tenant,
    save_policies_for_tenant,
)
from utils.auth_decorators import _is_authorized_for_tenant
from utils.roles import ROLE_EMPLEADO, ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, canonical_role

v2_sla_bp = Blueprint("v2_sla", __name__, url_prefix="/api/v2/sla")


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


def _operator_error():
    role = canonical_role(getattr(_viewer(), "rol", None))
    if role in {ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, ROLE_EMPLEADO}:
        return None
    return _error_response("Permisos insuficientes para operar SLA", 403, "operator_required", "ask_operator")


def _policy_admin_error():
    role = canonical_role(getattr(_viewer(), "rol", None))
    if role in {ROLE_SUPERADMIN, ROLE_TENANT_ADMIN}:
        return None
    return _error_response(
        "Solo un administrador del tenant puede modificar politicas SLA",
        403,
        "sla_policy_admin_required",
        "ask_tenant_admin",
    )


def _resolve_tenant_or_error():
    explicit_slug = (request.headers.get("X-Tenant-Slug") or request.args.get("tenant_slug") or "").strip()
    if not explicit_slug:
        return None, _error_response("X-Tenant-Slug es obligatorio en SLA v2", 400, "missing_tenant", "send_tenant_slug")
    try:
        return resolve_tenant_v2(required=True, explicit_slug=explicit_slug), None
    except V2TenantResolutionError as exc:
        return None, _error_response(exc.message, exc.status_code, "tenant_resolution_failed", "check_tenant_slug")


@v2_sla_bp.route("/policies", methods=["GET"])
def get_sla_policies_v2():
    tenant, error = _resolve_tenant_or_error()
    if error:
        return error
    access_error = _tenant_access_error(tenant)
    if access_error:
        return access_error
    role_error = _operator_error()
    if role_error:
        return role_error
    return _json_response({"contract_version": "sla.v2.policies", "policies": get_policies_for_tenant(tenant)})


@v2_sla_bp.route("/policies", methods=["POST"])
def save_sla_policies_v2():
    tenant, error = _resolve_tenant_or_error()
    if error:
        return error
    access_error = _tenant_access_error(tenant)
    if access_error:
        return access_error
    role_error = _policy_admin_error()
    if role_error:
        return role_error

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}
    policy_payload = payload.get("policies") if "policies" in payload else payload
    try:
        policies = save_policies_for_tenant(tenant, policy_payload)
    except SlaPolicyValidationError as exc:
        return _json_response(
            {
                "contract_version": "shared.error.v1",
                "status_code": 422,
                "reason_code": "invalid_sla_policy",
                "retryable": False,
                "action_hint": "fix_invalid_fields",
                "error": {
                    "code": 422,
                    "message": "Politicas SLA invalidas",
                    "fields": exc.errors,
                },
                "message": "Politicas SLA invalidas",
                "field_errors": exc.errors,
            },
            422,
        )
    db.session.commit()
    return _json_response({"contract_version": "sla.v2.policies", "policies": policies})


@v2_sla_bp.route("/breaches", methods=["GET"])
def list_sla_breaches_v2():
    tenant, error = _resolve_tenant_or_error()
    if error:
        return error
    access_error = _tenant_access_error(tenant)
    if access_error:
        return access_error
    role_error = _operator_error()
    if role_error:
        return role_error

    breaches = detect_sla_breaches_for_tenant(
        tenant,
        actor_user=getattr(g, "viewer", None),
        materialize=False,
    )
    return _json_response(
        {
            "contract_version": "sla.v2.breaches",
            "items": breaches,
            "total": len(breaches),
            "read_only": True,
        }
    )
