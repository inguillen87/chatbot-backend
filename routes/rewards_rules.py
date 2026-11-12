from __future__ import annotations

from flask import Blueprint, jsonify, request, g
from flask_cors import cross_origin
from cutover_writer_fence import cutover_writer_view

from config import ALLOWED_ORIGINS
from database import db
from models import TenantProfile, User
from services.tenant_resolver import TenantResolutionError, resolve_tenant_and_user

_CORS_ALLOWED_HEADERS = [
    "Content-Type",
    "Authorization",
    "X-Chatboc-Token",
    "X-Entity-Token",
    "X-Chat-Session-Id",
    "X-Anon-Id",
    "Anon-Id",
    "Cache-Control",
    "token",
    "X-Tenant",
    "X-Tenant-Id",
    "X-Widget-Token",
    "X-Whatsapp-Dst",
]


def _cors_kwargs(methods: list[str]) -> dict:
    return {
        "origins": ALLOWED_ORIGINS,
        "supports_credentials": True,
        "allow_headers": _CORS_ALLOWED_HEADERS,
        "methods": methods,
    }


rewards_rules_bp = Blueprint("rewards_rules_bp", __name__, url_prefix="/api/rewards")


def _is_tenant_admin(user: User, tenant: TenantProfile) -> bool:
    if user.rol == "admin":
        return True
    return user.id in {getattr(tenant, "municipio_id", None), getattr(tenant, "pyme_id", None)}


def _resolve_tenant_user():
    tenant_arg = request.headers.get("X-Tenant") or request.args.get("tenant") or request.args.get("tenant_slug")
    tenant_id = request.headers.get("X-Tenant-Id") or request.args.get("tenant_id")
    widget_token = request.headers.get("X-Widget-Token") or request.args.get("widget_token")
    has_hint = bool(tenant_arg or tenant_id or widget_token or request.headers.get("X-Whatsapp-Dst"))
    if not has_hint:
        raise TenantResolutionError("Tenant requerido")

    tenant, user, _ = resolve_tenant_and_user(
        tenant_slug=tenant_arg,
        tenant_id=tenant_id,
        widget_token=widget_token,
        whatsapp_destination_number=request.headers.get("X-Whatsapp-Dst"),
        current_user=getattr(g, "user", None),
    )
    if tenant is None:
        raise TenantResolutionError("Tenant no encontrado")
    if user is None:
        raise TenantResolutionError("Autenticación requerida")
    return tenant, user


@rewards_rules_bp.route("/rules", methods=["GET", "OPTIONS"], strict_slashes=False)
@cutover_writer_view
@cross_origin(**_cors_kwargs(["GET", "OPTIONS"]))
def get_rules():
    if request.method == "OPTIONS":
        return "", 204

    try:
        tenant, user = _resolve_tenant_user()
    except TenantResolutionError as exc:
        return jsonify({"error": str(exc)}), 400

    if not _is_tenant_admin(user, tenant):
        return jsonify({"error": "Solo administradores pueden ver las reglas"}), 403

    rules = {}
    if isinstance(tenant.configuracion, dict):
        rules = tenant.configuracion.get("rewards_rules") or {}

    return jsonify({"tenant_id": tenant.id, "rules": rules})


@rewards_rules_bp.route("/rules", methods=["PUT", "OPTIONS"], strict_slashes=False)
@cross_origin(**_cors_kwargs(["PUT", "OPTIONS"]))
def update_rules():
    if request.method == "OPTIONS":
        return "", 204

    try:
        tenant, user = _resolve_tenant_user()
    except TenantResolutionError as exc:
        return jsonify({"error": str(exc)}), 400

    if not _is_tenant_admin(user, tenant):
        return jsonify({"error": "Solo administradores pueden actualizar las reglas"}), 403

    payload = request.get_json(silent=True) or {}
    rules = payload.get("rules") if isinstance(payload, dict) else None
    if rules is None or not isinstance(rules, dict):
        return jsonify({"error": "Se requiere objeto 'rules'"}), 400

    configuracion = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    configuracion["rewards_rules"] = rules
    tenant.configuracion = configuracion
    db.session.add(tenant)
    db.session.commit()

    return jsonify({"tenant_id": tenant.id, "rules": rules})
