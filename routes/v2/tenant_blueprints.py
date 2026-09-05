from __future__ import annotations

import uuid
import re

from flask import Blueprint, g, jsonify, request

from routes.v2.tenants import V2TenantResolutionError, resolve_tenant_v2
from services.tenant_blueprints import (
    TenantBlueprintError,
    apply_blueprint,
    get_blueprint,
    list_blueprints,
    preview_blueprint,
)
from utils.auth_helpers import token_requerido
from utils.roles import (
    ROLE_SUPERADMIN,
    ROLE_TENANT_ADMIN,
    canonical_role,
    is_authorized_superadmin_user,
)
from utils.tenant_admin_access import can_manage_tenant_control_plane


v2_tenant_blueprints_bp = Blueprint(
    "v2_tenant_blueprints", __name__, url_prefix="/api/v2"
)
ERROR_CONTRACT_VERSION = "tenant.blueprint.error.v1"
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
    return str(value)


def _response(payload: dict, status_code: int = 200):
    response = jsonify(payload)
    response.status_code = status_code
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Request-Id"] = _request_id()
    return response


def _error(
    reason_code: str,
    message: str,
    status_code: int,
    *,
    details: dict | None = None,
):
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


def _handle_blueprint_error(exc: TenantBlueprintError):
    safe_details = {
        key: value
        for key, value in exc.details.items()
        if key in {"location"} and isinstance(value, (str, int, float, bool))
    }
    return _error(
        exc.reason_code,
        exc.message,
        exc.status_code,
        details=safe_details or None,
    )


def _resolve_exact_tenant(tenant_slug: str):
    try:
        return resolve_tenant_v2(explicit_slug=tenant_slug)
    except V2TenantResolutionError as exc:
        raise TenantBlueprintError(
            "tenant_not_found" if exc.status_code == 404 else "tenant_required",
            exc.message,
            exc.status_code,
        ) from exc


@v2_tenant_blueprints_bp.route("/tenant-blueprints", methods=["GET"])
@token_requerido
def tenant_blueprint_catalog_v2(current_user):
    role = canonical_role(getattr(current_user, "rol", None))
    if role not in {ROLE_TENANT_ADMIN, ROLE_SUPERADMIN}:
        return _error(
            "tenant_admin_required",
            "Se requiere administracion de tenant.",
            403,
        )
    if role == ROLE_SUPERADMIN and not is_authorized_superadmin_user(current_user):
        return _error(
            "superadmin_not_authorized",
            "El superadmin no esta autorizado.",
            403,
        )
    try:
        return _response(list_blueprints())
    except TenantBlueprintError as exc:
        return _handle_blueprint_error(exc)


@v2_tenant_blueprints_bp.route(
    "/tenants/<string:tenant_slug>/blueprints/<string:blueprint_id>",
    methods=["GET"],
)
@token_requerido
def tenant_blueprint_detail_v2(current_user, tenant_slug: str, blueprint_id: str):
    try:
        tenant = _resolve_exact_tenant(tenant_slug)
        if not can_manage_tenant_control_plane(current_user, tenant):
            raise TenantBlueprintError(
                "tenant_control_plane_forbidden",
                "No tenes acceso al plano de control de este tenant.",
                403,
            )
        return _response(get_blueprint(tenant, blueprint_id))
    except TenantBlueprintError as exc:
        return _handle_blueprint_error(exc)


@v2_tenant_blueprints_bp.route(
    "/tenants/<string:tenant_slug>/blueprints/<string:blueprint_id>/preview",
    methods=["POST"],
)
@token_requerido
def tenant_blueprint_preview_v2(current_user, tenant_slug: str, blueprint_id: str):
    try:
        tenant = _resolve_exact_tenant(tenant_slug)
        if not can_manage_tenant_control_plane(current_user, tenant):
            raise TenantBlueprintError(
                "tenant_control_plane_forbidden",
                "No tenes acceso al plano de control de este tenant.",
                403,
            )
        return _response(preview_blueprint(tenant, blueprint_id))
    except TenantBlueprintError as exc:
        return _handle_blueprint_error(exc)


@v2_tenant_blueprints_bp.route(
    "/tenants/<string:tenant_slug>/blueprints/<string:blueprint_id>/apply",
    methods=["POST"],
)
@token_requerido
def tenant_blueprint_apply_v2(current_user, tenant_slug: str, blueprint_id: str):
    if not is_authorized_superadmin_user(current_user):
        return _error(
            "superadmin_required",
            "La aplicacion inicial requiere un superadmin autorizado.",
            403,
        )

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("invalid_json", "El cuerpo JSON no es valido.", 400)
    if set(payload) != {"manifest_digest"}:
        return _error(
            "invalid_apply_payload",
            "La aplicacion requiere unicamente manifest_digest.",
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
        result = apply_blueprint(
            tenant,
            actor_user_id=current_user.id,
            blueprint_id=blueprint_id,
            expected_manifest_digest=payload.get("manifest_digest"),
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
        return _handle_blueprint_error(exc)
