from __future__ import annotations

from typing import Any
import uuid

from flask import Blueprint, jsonify, request

from models import TenantProfile, User
from routes.v2.tenants import V2TenantResolutionError, resolve_tenant_v2
from services.commerce_contracts import (
    PaymentGatewayError,
    as_int,
    build_mercadopago_preference_payload,
    build_payment_status_payload,
    contact_ready_for_checkout,
    create_mercadopago_preference,
    find_payment_resources,
    normalize_checkout_preview_totals,
    payment_capabilities,
    tenant_config,
    tenant_ref,
)
from services.plan_access import integration_plan_required_payload
from services.rewards import recompensas_service
from utils.auth_helpers import token_requerido
from utils.permissions import require_role

v2_commerce_bp = Blueprint("v2_commerce", __name__, url_prefix="/api/v2")


def _request_id() -> str:
    incoming = (request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id") or "").strip()
    return incoming or uuid.uuid4().hex


def _json_response(payload: dict[str, Any], status: int = 200):
    body = dict(payload)
    request_id = str(body.get("request_id") or _request_id())
    body.setdefault("request_id", request_id)
    response = jsonify(body)
    response.status_code = status
    response.headers["X-Request-Id"] = request_id
    return response


def _error_response(message: str, status_code: int, reason_code: str, action_hint: str, *, retryable: bool = False, extra: dict[str, Any] | None = None):
    payload = {
        "contract_version": "shared.error.v1",
        "status_code": status_code,
        "reason_code": reason_code,
        "retryable": retryable,
        "action_hint": action_hint,
        "error": {"code": status_code, "message": message},
        "message": message,
    }
    if extra:
        payload.update(extra)
    return _json_response(payload, status_code)


def _tenant_slug_from_request(path_slug: str | None = None) -> str:
    return (
        path_slug
        or request.headers.get("X-Tenant-Slug")
        or request.headers.get("X-Tenant")
        or request.args.get("tenant_slug")
        or request.args.get("tenant")
        or ""
    ).strip()


def _user_can_access_tenant(user: User, tenant: TenantProfile) -> bool:
    role = str(getattr(user, "rol", "") or "").lower()
    if role == "super_admin":
        return True
    if str(getattr(user, "tenant_id", "") or "") == str(tenant.id):
        return True
    if (getattr(user, "tenant_slug", "") or "").strip().lower() == (tenant.slug or "").strip().lower():
        return True
    owner_ids = {getattr(tenant, "pyme_id", None), getattr(tenant, "municipio_id", None)}
    return getattr(user, "id", None) in owner_ids


def _resolve_tenant_or_error(current_user: User, path_slug: str | None = None):
    slug = _tenant_slug_from_request(path_slug)
    try:
        tenant = resolve_tenant_v2(required=True, explicit_slug=slug or None)
    except V2TenantResolutionError as exc:
        return None, _error_response(exc.message, exc.status_code, "tenant_resolution_failed", "send_valid_tenant")

    if not _user_can_access_tenant(current_user, tenant):
        return None, _error_response("Permisos insuficientes para este tenant", 403, "forbidden_tenant", "switch_tenant")
    return tenant, None


def _idempotency_key(payload: dict[str, Any]) -> str | None:
    value = request.headers.get("Idempotency-Key") or payload.get("idempotency_key")
    value = str(value or "").strip()
    return value or None


@v2_commerce_bp.route("/payments/checkout-status", methods=["GET"])
@v2_commerce_bp.route("/payments/capabilities", methods=["GET"])
@v2_commerce_bp.route("/tenants/<string:tenant_slug>/payments/checkout-status", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def payment_checkout_status_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error
    return _json_response({"contract_version": "payments.checkout_status.v1", "tenant": tenant_ref(tenant), **payment_capabilities(tenant)})


@v2_commerce_bp.route("/payments/checkout-preview", methods=["POST"])
@v2_commerce_bp.route("/tenants/<string:tenant_slug>/payments/checkout-preview", methods=["POST"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def payment_checkout_preview_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    totals = normalize_checkout_preview_totals(payload)
    payment = payment_capabilities(tenant)
    integration_access = payment.get("integration_access") or {}
    checkout_experience = payment.get("checkout_experience") or {}
    payment_required = totals["total_monetary"] > 0
    contact_ready = contact_ready_for_checkout(payload, current_user)
    checkout_status = (
        "ready"
        if ((not payment_required or payment["payment_ready"]) and contact_ready)
        else "needs_contact"
        if not contact_ready
        else "locked"
        if payment_required and not integration_access.get("enabled")
        else "needs_gateway"
    )
    next_steps = [
        {"id": "confirm_contact", "label": "Confirmar contacto", "status": "ready" if contact_ready else "required"},
        {
            "id": "complete_payment",
            "label": "Completar pago",
            "status": (
                "ready"
                if payment_required and payment["payment_ready"]
                else "not_required"
                if not payment_required
                else "locked"
                if not integration_access.get("enabled")
                else "pending_configuration"
            ),
        },
        {"id": "track_order", "label": "Seguir pedido", "status": "ready"},
    ]
    return _json_response(
        {
            "contract_version": "payments.checkout_preview.v1",
            "tenant": tenant_ref(tenant),
            "status": checkout_status,
            "summary": totals,
            "payment_required": payment_required,
            "payment_ready": (not payment_required) or payment["payment_ready"],
            "contact_ready": contact_ready,
            "integration_access": integration_access,
            "checkout_experience": checkout_experience,
            "security_policy": checkout_experience.get("policy") or {},
            "checkout_options": {
                "payment_required": payment_required,
                "payment_ready": (not payment_required) or payment["payment_ready"],
                "requires_contact_or_auth": not contact_ready,
                "gateway": payment["gateway"],
                "gateway_hint": payment["gateway_hint"],
                "mercadopago_ready": payment["mercadopago_ready"],
                "preference_id": None,
                "init_point": None,
            },
            "next_steps": next_steps,
            "idempotency_key": _idempotency_key(payload),
        }
    )


@v2_commerce_bp.route("/payments/checkout-session", methods=["POST"])
@v2_commerce_bp.route("/payments/preference", methods=["POST"])
@v2_commerce_bp.route("/tenants/<string:tenant_slug>/payments/checkout-session", methods=["POST"])
@token_requerido
def payment_checkout_session_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    totals = normalize_checkout_preview_totals(payload)
    if float(totals.get("total_monetary") or 0) <= 0:
        return _error_response("El checkout no requiere pago monetario", 400, "payment_not_required", "confirm_without_gateway")

    payment = payment_capabilities(tenant)
    integration_access = payment.get("integration_access") or {}
    checkout_experience = payment.get("checkout_experience") or {}
    if not integration_access.get("enabled"):
        return _json_response(
            integration_plan_required_payload(
                tenant,
                "mercadopago_checkout",
                contract_version="payments.checkout_session.v1",
                action_hint="upgrade_full_plan",
                render_as="integration_locked",
                extra={
                    "message": "Plan Full requerido para crear sesiones de pago productivas",
                    "error": {"code": 403, "message": "Plan Full requerido para crear sesiones de pago productivas"},
                    "retryable": False,
                    "checkout_options": {
                        "payment_required": True,
                        "payment_ready": False,
                        "requires_contact_or_auth": False,
                        "gateway": payment["gateway"],
                        "gateway_hint": payment["gateway_hint"],
                        "mercadopago_ready": payment["mercadopago_ready"],
                        "preference_id": None,
                        "init_point": None,
                    },
                    "integration_access": integration_access,
                    "checkout_experience": checkout_experience,
                },
            ),
            403,
        )

    access_token = tenant_config(tenant).get("mercadopago_access_token")
    if not access_token:
        return _error_response(
            "Mercado Pago no configurado para este tenant",
            503,
            "payment_gateway_not_configured",
            "configure_mercadopago_access_token",
            retryable=True,
            extra={
                "integration_access": integration_access,
                "checkout_experience": checkout_experience,
            },
        )

    key = _idempotency_key(payload)
    request_id = _request_id()
    preference_payload, external_reference = build_mercadopago_preference_payload(
        tenant=tenant,
        totals=totals,
        payload=payload,
        idempotency_key=key,
        request_id=request_id,
    )
    try:
        mp_data = create_mercadopago_preference(access_token, preference_payload)
    except PaymentGatewayError as exc:
        return _error_response(exc.message, exc.status_code, exc.reason_code, exc.action_hint, retryable=exc.retryable)

    preference_id = mp_data.get("id")
    init_point = mp_data.get("init_point") or mp_data.get("sandbox_init_point")
    return _json_response(
        {
            "ok": True,
            "contract_version": "payments.checkout_session.v1",
            "request_id": request_id,
            "tenant": tenant_ref(tenant),
            "gateway": payment["gateway"],
            "status": "pending_payment",
            "integration_access": integration_access,
            "checkout_experience": checkout_experience,
            "external_reference": external_reference,
            "preference_id": preference_id,
            "init_point": init_point,
            "sandbox_init_point": mp_data.get("sandbox_init_point"),
            "summary": totals,
            "checkout_options": {
                "payment_required": True,
                "payment_ready": bool(preference_id and init_point),
                "gateway": payment["gateway"],
                "gateway_hint": payment["gateway_hint"],
                "mercadopago_ready": True,
                "preference_id": preference_id,
                "init_point": init_point,
            },
            "idempotency_key": key,
        }
    )


@v2_commerce_bp.route("/payments/status", methods=["GET", "POST"])
@v2_commerce_bp.route("/tenants/<string:tenant_slug>/payments/status", methods=["GET", "POST"])
@token_requerido
def payment_status_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    body = request.get_json(silent=True) or {}
    filters = {
        "pedido_id": request.args.get("pedido_id") or body.get("pedido_id"),
        "order_id": request.args.get("order_id") or body.get("order_id"),
        "market_order_id": request.args.get("market_order_id") or body.get("market_order_id"),
        "preference_id": request.args.get("preference_id") or body.get("preference_id"),
        "mp_preference_id": request.args.get("mp_preference_id") or body.get("mp_preference_id"),
        "external_reference": request.args.get("external_reference") or body.get("external_reference"),
    }
    if not any(filters.values()):
        return _error_response("Se requiere un identificador de pago u orden", 400, "payment_reference_required", "send_payment_reference")

    pedido, market_order = find_payment_resources(tenant, filters)
    if pedido is None and market_order is None:
        return _error_response("Pago u orden no encontrados", 404, "payment_not_found", "refresh_payment_status")

    return _json_response(build_payment_status_payload(tenant, pedido, market_order))


@v2_commerce_bp.route("/rewards/profile", methods=["GET"])
@v2_commerce_bp.route("/tenants/<string:tenant_slug>/rewards/profile", methods=["GET"])
@token_requerido
def rewards_profile_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    pending_points = as_int(request.args.get("pending_points") or request.args.get("puntos_en_carrito"))
    profile = recompensas_service().perfil_usuario(current_user, tenant, pending_points)
    return _json_response(
        {
            "contract_version": "rewards.profile.v1",
            "tenant": tenant_ref(tenant),
            "user": {"id": current_user.id, "name": current_user.name},
            **profile,
        }
    )


@v2_commerce_bp.route("/rewards/redeem", methods=["POST"])
@v2_commerce_bp.route("/tenants/<string:tenant_slug>/rewards/redeem", methods=["POST"])
@token_requerido
def rewards_redeem_v2(current_user, tenant_slug: str | None = None):
    tenant, error = _resolve_tenant_or_error(current_user, tenant_slug)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    reward_id = str(payload.get("reward_id") or payload.get("benefit_id") or "").strip()
    result = recompensas_service().canjear_beneficio(
        current_user,
        tenant,
        reward_id,
        idempotency_key=_idempotency_key(payload),
    )
    if not result.get("ok"):
        reason_code = result.get("reason_code") or "reward_redeem_failed"
        action_hint = "choose_valid_reward" if reason_code == "reward_not_found" else "earn_more_points"
        return _error_response(
            result.get("message") or "No pudimos canjear el beneficio",
            int(result.get("status_code") or 400),
            reason_code,
            action_hint,
            extra={k: v for k, v in result.items() if k not in {"ok", "message", "status_code", "reason_code"}},
        )

    return _json_response(
        {
            "ok": True,
            "contract_version": "rewards.redeem.v1",
            "tenant": tenant_ref(tenant),
            **result,
        }
    )
