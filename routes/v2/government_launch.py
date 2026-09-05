from __future__ import annotations

import re
import uuid

from flask import Blueprint, g, jsonify, request
from sqlalchemy import func

from models import TenantProfile
from services.auth_assurance_service import (
    AuthAssuranceError,
    STRICT_MFA,
    require_request_auth_assurance,
)
from services.government_launch_journey import (
    apply_mesa_unica_launch,
    preview_mesa_unica_launch,
)
from services.tenant_blueprints import TenantBlueprintError
from utils.auth_helpers import auth_sin_escrituras_implicitas, token_requerido
from utils.roles import (
    is_authorized_superadmin_user,
    is_generic_tenant_slug,
    normalize_tenant_slug,
)
from utils.tenant_admin_access import can_manage_tenant_control_plane


v2_government_launch_bp = Blueprint(
    "v2_government_launch", __name__, url_prefix="/api/v2"
)
ERROR_CONTRACT_VERSION = "tenant.blueprint.launch.error.v1"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _request_id() -> str:
    candidate = (
        request.headers.get("X-Request-Id")
        or request.headers.get("X-Correlation-Id")
        or getattr(g, "request_id", None)
    )
    value = str(candidate or "")
    if not _REQUEST_ID_PATTERN.fullmatch(value):
        value = uuid.uuid4().hex
    g.request_id = value
    return value


def _response(payload: dict, status_code: int = 200):
    response = jsonify(payload)
    response.status_code = status_code
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Request-Id"] = _request_id()
    return response


def _error(reason_code: str, message: str, status_code: int, *, details=None):
    payload = {
        "contract_version": ERROR_CONTRACT_VERSION,
        "status_code": status_code,
        "reason_code": reason_code,
        "retryable": status_code >= 500,
        "request_id": _request_id(),
        "error": {"code": status_code, "message": message},
    }
    if details:
        payload["details"] = details
    return _response(payload, status_code)


def _handle_launch_error(exc: TenantBlueprintError):
    safe_details = {
        key: value
        for key, value in exc.details.items()
        if key == "conflict_count" and isinstance(value, int)
    }
    return _error(
        exc.reason_code,
        exc.message,
        exc.status_code,
        details=safe_details or None,
    )


def _resolve_exact_tenant(tenant_slug: str) -> TenantProfile:
    cleaned = normalize_tenant_slug(tenant_slug)
    if not cleaned or is_generic_tenant_slug(cleaned):
        raise TenantBlueprintError("tenant_not_found", "Tenant no encontrado.", 404)
    tenant = TenantProfile.query.filter(func.lower(TenantProfile.slug) == cleaned).first()
    if tenant is None:
        raise TenantBlueprintError("tenant_not_found", "Tenant no encontrado.", 404)
    g.v2_tenant = tenant
    return tenant


def _require_superadmin_and_strict_mfa(current_user):
    if not is_authorized_superadmin_user(current_user):
        return _error(
            "superadmin_required",
            "La preparacion operativa requiere un superadmin autorizado.",
            403,
        )
    try:
        require_request_auth_assurance(STRICT_MFA)
    except AuthAssuranceError as exc:
        payload = exc.to_payload()
        payload["request_id"] = _request_id()
        return _response(payload, 403)
    return None


@v2_government_launch_bp.route(
    "/tenants/<string:tenant_slug>/blueprints/government-core/launch/mesa-unica/preview",
    methods=["POST"],
)
@token_requerido
@auth_sin_escrituras_implicitas
def government_mesa_unica_preview_v2(current_user, tenant_slug: str):
    try:
        tenant = _resolve_exact_tenant(tenant_slug)
        if not can_manage_tenant_control_plane(current_user, tenant):
            raise TenantBlueprintError(
                "tenant_control_plane_forbidden",
                "No tenes acceso al plano de control de este tenant.",
                403,
            )
        return _response(preview_mesa_unica_launch(tenant))
    except TenantBlueprintError as exc:
        return _handle_launch_error(exc)


@v2_government_launch_bp.route(
    "/tenants/<string:tenant_slug>/blueprints/government-core/launch/mesa-unica/apply",
    methods=["POST"],
)
@token_requerido
@auth_sin_escrituras_implicitas
def government_mesa_unica_apply_v2(current_user, tenant_slug: str):
    denied = _require_superadmin_and_strict_mfa(current_user)
    if denied is not None:
        return denied

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("invalid_json", "El cuerpo JSON no es valido.", 400)
    if set(payload) != {"launch_digest"}:
        return _error(
            "invalid_launch_payload",
            "La aplicacion requiere unicamente launch_digest.",
            400,
        )
    idempotency_key = request.headers.get("Idempotency-Key")
    if idempotency_key is None:
        return _error(
            "idempotency_key_required",
            "Idempotency-Key es obligatorio.",
            400,
        )

    try:
        tenant = _resolve_exact_tenant(tenant_slug)
        result = apply_mesa_unica_launch(
            tenant,
            actor_user_id=current_user.id,
            expected_launch_digest=payload.get("launch_digest"),
            idempotency_key=idempotency_key,
        )
        response = _response(result, 200 if result["replayed"] else 201)
        response.headers["X-Idempotency-Status"] = (
            "replayed" if result["replayed"] else "created"
        )
        response.headers["Idempotency-Replayed"] = (
            "true" if result["replayed"] else "false"
        )
        return response
    except TenantBlueprintError as exc:
        return _handle_launch_error(exc)


__all__ = ["v2_government_launch_bp"]
