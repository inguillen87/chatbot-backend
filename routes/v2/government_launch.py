from __future__ import annotations

import os
import re
import uuid

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import func

from extensions import db
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
from services.government_jurisdiction_readiness import (
    GovernmentJurisdictionError,
    jurisdiction_readiness,
    review_jurisdiction_evidence,
    submit_jurisdiction_evidence,
)
from services.tenant_blueprints import TenantBlueprintError
from utils.auth_helpers import auth_sin_escrituras_implicitas, token_requerido
from utils.roles import (
    ROLE_EMPLEADO,
    canonical_role,
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
_PLATFORM_REVIEWER_ROLES = frozenset({ROLE_EMPLEADO, "supervisor", "manager"})
_PLATFORM_REVIEWER_ALLOWLIST_CONFIG = "JURISDICTION_PLATFORM_REVIEWER_EMAILS"


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


def _handle_jurisdiction_error(exc: GovernmentJurisdictionError):
    safe_details = {
        key: value
        for key, value in exc.details.items()
        if key in {"current_submission_sha256"}
        and isinstance(value, (str, int, float, bool))
    }
    payload = {
        "contract_version": "government.jurisdiction.error.v1",
        "status_code": exc.status_code,
        "reason_code": exc.reason_code,
        "retryable": exc.status_code >= 500,
        "request_id": _request_id(),
        "error": {"code": exc.status_code, "message": exc.message},
        "next_action": exc.next_action,
    }
    if safe_details:
        payload["details"] = safe_details
    return _response(payload, exc.status_code)


def _jurisdiction_route_error(
    reason_code: str,
    message: str,
    status_code: int,
    *,
    next_action: str,
):
    """Keep validation and authorization failures on the jurisdiction contract."""

    return _handle_jurisdiction_error(
        GovernmentJurisdictionError(
            reason_code,
            message,
            status_code,
            next_action=next_action,
        )
    )


def _assert_tenant_selector_consistency(tenant_slug: str) -> str:
    """Keep the path authoritative and reject conflicting legacy selectors."""

    cleaned = normalize_tenant_slug(tenant_slug)
    selector_values = [
        value
        for key in ("tenant", "tenant_slug")
        for value in request.args.getlist(key)
        if str(value or "").strip()
    ]
    if any(normalize_tenant_slug(value) != cleaned for value in selector_values):
        raise GovernmentJurisdictionError(
            "tenant_selector_conflict",
            "El selector de tenant es ambiguo.",
            400,
            next_action="use_exact_tenant_path",
        )
    return cleaned


def _resolve_exact_tenant(tenant_slug: str) -> TenantProfile:
    cleaned = normalize_tenant_slug(tenant_slug)
    if not cleaned or is_generic_tenant_slug(cleaned):
        raise TenantBlueprintError("tenant_not_found", "Tenant no encontrado.", 404)
    tenant = TenantProfile.query.filter(func.lower(TenantProfile.slug) == cleaned).first()
    if tenant is None:
        raise TenantBlueprintError("tenant_not_found", "Tenant no encontrado.", 404)
    g.v2_tenant = tenant
    return tenant


def _resolve_exact_jurisdiction_tenant(tenant_slug: str) -> TenantProfile:
    cleaned = _assert_tenant_selector_consistency(tenant_slug)
    if not cleaned or is_generic_tenant_slug(cleaned):
        raise TenantBlueprintError("tenant_not_found", "Tenant no encontrado.", 404)
    matches = (
        TenantProfile.query.filter(func.lower(TenantProfile.slug) == cleaned)
        .order_by(TenantProfile.id.asc())
        .limit(2)
        .all()
    )
    if not matches:
        raise TenantBlueprintError("tenant_not_found", "Tenant no encontrado.", 404)
    if len(matches) != 1:
        raise GovernmentJurisdictionError(
            "tenant_selector_ambiguous",
            "El selector no identifica un unico tenant.",
            409,
            next_action="repair_tenant_identity",
        )
    tenant = matches[0]
    g.v2_tenant = tenant
    return tenant


def _configured_platform_reviewer_emails() -> set[str]:
    raw = current_app.config.get(_PLATFORM_REVIEWER_ALLOWLIST_CONFIG)
    if raw in (None, ""):
        raw = os.getenv(_PLATFORM_REVIEWER_ALLOWLIST_CONFIG, "")
    if isinstance(raw, (list, tuple, set, frozenset)):
        values = raw
    else:
        values = str(raw or "").split(",")
    return {str(value).strip().lower() for value in values if str(value).strip()}


def _is_authorized_platform_reviewer(current_user) -> bool:
    if is_authorized_superadmin_user(current_user):
        return True
    if canonical_role(getattr(current_user, "rol", None)) not in _PLATFORM_REVIEWER_ROLES:
        return False
    email = str(getattr(current_user, "email", None) or "").strip().lower()
    if not email or email not in _configured_platform_reviewer_emails():
        return False
    # A platform reviewer is unscoped. Tenant employees cannot turn an email
    # allowlist entry into cross-tenant control-plane authority.
    if any(
        getattr(current_user, field, None) not in (None, "")
        for field in ("tenant_id", "tenant_slug", "municipio_id", "pyme_id", "empresa_id")
    ):
        return False
    return True


def _require_platform_reviewer_and_strict_mfa(current_user):
    if not _is_authorized_platform_reviewer(current_user):
        return _jurisdiction_route_error(
            "jurisdiction_platform_reviewer_required",
            "La revision requiere un operador de plataforma autorizado.",
            403,
            next_action="assign_authorized_platform_reviewer",
        )
    try:
        require_request_auth_assurance(STRICT_MFA)
    except AuthAssuranceError as exc:
        payload = exc.to_payload()
        payload["request_id"] = _request_id()
        return _response(payload, 403)
    return None


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


@v2_government_launch_bp.route(
    "/tenants/<string:tenant_slug>/government-readiness/jurisdiction",
    methods=["GET"],
)
@token_requerido
def government_jurisdiction_readiness_v2(current_user, tenant_slug: str):
    try:
        tenant = _resolve_exact_jurisdiction_tenant(tenant_slug)
        if not (
            can_manage_tenant_control_plane(current_user, tenant)
            or _is_authorized_platform_reviewer(current_user)
        ):
            raise GovernmentJurisdictionError(
                "tenant_control_plane_forbidden",
                "No tenes acceso al plano de control de este tenant.",
                403,
                next_action="select_authorized_tenant",
            )
        return _response(jurisdiction_readiness(tenant))
    except GovernmentJurisdictionError as exc:
        return _handle_jurisdiction_error(exc)
    except TenantBlueprintError as exc:
        return _handle_launch_error(exc)


@v2_government_launch_bp.route(
    "/tenants/<string:tenant_slug>/government-readiness/jurisdiction/evidence",
    methods=["POST"],
)
@token_requerido
@auth_sin_escrituras_implicitas
def government_jurisdiction_evidence_v2(current_user, tenant_slug: str):
    payload = request.get_json(silent=True)
    allowed_fields = {"jurisdiction_ref", "evidence_ref", "evidence_sha256"}
    if not isinstance(payload, dict) or set(payload) != allowed_fields:
        return _jurisdiction_route_error(
            "jurisdiction_evidence_payload_invalid",
            "La evidencia requiere jurisdiction_ref, evidence_ref y evidence_sha256.",
            400,
            next_action="send_exact_evidence_payload",
        )
    idempotency_key = request.headers.get("Idempotency-Key")
    if idempotency_key is None:
        return _jurisdiction_route_error(
            "jurisdiction_idempotency_key_required",
            "Idempotency-Key es obligatorio.",
            400,
            next_action="send_valid_idempotency_key",
        )
    try:
        tenant = _resolve_exact_jurisdiction_tenant(tenant_slug)
        if is_authorized_superadmin_user(current_user) or not can_manage_tenant_control_plane(
            current_user, tenant
        ):
            raise GovernmentJurisdictionError(
                "jurisdiction_tenant_admin_required",
                "La evidencia debe ser presentada por un administrador del tenant.",
                403,
                next_action="sign_in_as_tenant_admin",
            )
        readiness, replayed = submit_jurisdiction_evidence(
            tenant,
            actor_user_id=int(current_user.id),
            jurisdiction_ref=payload.get("jurisdiction_ref"),
            evidence_ref=payload.get("evidence_ref"),
            evidence_sha256=payload.get("evidence_sha256"),
            idempotency_key=idempotency_key,
        )
        response = _response(
            {
                "contract_version": "government.jurisdiction.evidence_submission.v1",
                "replayed": replayed,
                "write_performed": not replayed,
                "verification_granted": False,
                "readiness": readiness,
            },
            200 if replayed else 201,
        )
        response.headers["X-Idempotency-Status"] = "replayed" if replayed else "created"
        response.headers["Idempotency-Replayed"] = "true" if replayed else "false"
        return response
    except GovernmentJurisdictionError as exc:
        db.session.rollback()
        return _handle_jurisdiction_error(exc)
    except TenantBlueprintError as exc:
        return _handle_launch_error(exc)


@v2_government_launch_bp.route(
    "/tenants/<string:tenant_slug>/government-readiness/jurisdiction/review",
    methods=["POST"],
)
@token_requerido
@auth_sin_escrituras_implicitas
def government_jurisdiction_review_v2(current_user, tenant_slug: str):
    denied = _require_platform_reviewer_and_strict_mfa(current_user)
    if denied is not None:
        return denied
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _jurisdiction_route_error(
            "jurisdiction_review_payload_invalid",
            "El cuerpo JSON no es valido.",
            400,
            next_action="send_exact_review_payload",
        )
    decision = str(payload.get("decision") or "").strip().lower()
    expected_fields = (
        {"decision", "expected_submission_sha256", "reason_code"}
        if decision == "reject"
        else {"decision", "expected_submission_sha256"}
    )
    if set(payload) != expected_fields:
        return _jurisdiction_route_error(
            "jurisdiction_review_payload_invalid",
            "La revision requiere decision y expected_submission_sha256; "
            "reason_code solo es obligatorio al rechazar.",
            400,
            next_action="send_exact_review_payload",
        )
    idempotency_key = request.headers.get("Idempotency-Key")
    if idempotency_key is None:
        return _jurisdiction_route_error(
            "jurisdiction_idempotency_key_required",
            "Idempotency-Key es obligatorio.",
            400,
            next_action="send_valid_idempotency_key",
        )
    try:
        tenant = _resolve_exact_jurisdiction_tenant(tenant_slug)
        readiness, replayed = review_jurisdiction_evidence(
            tenant,
            reviewer_user_id=int(current_user.id),
            decision=decision,
            expected_submission_sha256=payload.get("expected_submission_sha256"),
            reason_code=payload.get("reason_code"),
            idempotency_key=idempotency_key,
        )
        response = _response(
            {
                "contract_version": "government.jurisdiction.review.v1",
                "decision": decision,
                "replayed": replayed,
                "write_performed": not replayed,
                "readiness": readiness,
            },
            200 if replayed else 201,
        )
        response.headers["X-Idempotency-Status"] = "replayed" if replayed else "created"
        response.headers["Idempotency-Replayed"] = "true" if replayed else "false"
        return response
    except GovernmentJurisdictionError as exc:
        db.session.rollback()
        return _handle_jurisdiction_error(exc)
    except TenantBlueprintError as exc:
        return _handle_launch_error(exc)


__all__ = ["v2_government_launch_bp"]
