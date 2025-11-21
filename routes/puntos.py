from flask import Blueprint, jsonify, request, g
from flask_cors import cross_origin

from models import TenantProfile
from services.rewards import recompensas_service
from routes.productos import _resolve_public_owner
from services.tenant_resolver import TenantResolutionError, resolve_tenant_and_user
from config import ALLOWED_ORIGINS

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

puntos_bp = Blueprint("puntos_bp", __name__, url_prefix="/api/puntos")
# Alias sin el prefijo /api para compatibilidad con widgets antiguos
puntos_public_bp = Blueprint("puntos_public_bp", __name__, url_prefix="/puntos")


@puntos_bp.route("/saldo", methods=["GET", "OPTIONS"])
@cross_origin(**_cors_kwargs(["GET", "OPTIONS"]))
def saldo():
    if request.method == "OPTIONS":
        return "", 204

    tenant_arg = request.headers.get("X-Tenant") or request.args.get("tenant") or request.args.get("tenant_slug")
    widget_token = request.headers.get("X-Widget-Token") or request.args.get("widget_token")
    try:
        tenant, user, _ = resolve_tenant_and_user(
            whatsapp_destination_number=request.headers.get("X-Whatsapp-Dst"),
            widget_token=widget_token,
            tenant_slug=tenant_arg,
            current_user=getattr(g, "user", None),
        )
    except TenantResolutionError as exc:
        tenant, user = _resolve_public_owner()
        if tenant is None:
            tenant = TenantProfile.query.order_by(TenantProfile.id.asc()).first()
        if user is None:
            return (
                jsonify({"tenant_id": getattr(tenant, "id", None), "saldo": 0, "error": str(exc)}),
                200,
            )

    if tenant is None:
        return jsonify({"tenant_id": None, "saldo": 0, "anonId": getattr(user, "anon_id", None)})

    saldo_actual = recompensas_service().obtener_saldo(user)
    return jsonify({"tenant_id": tenant.id, "saldo": saldo_actual, "anonId": user.anon_id})


@puntos_public_bp.route("/saldo", methods=["GET", "OPTIONS"])
@cross_origin(**_cors_kwargs(["GET", "OPTIONS"]))
def saldo_public():
    """Alias público para /api/puntos/saldo."""

    return saldo()


@puntos_bp.route("/historial", methods=["GET", "OPTIONS"])
@cross_origin(**_cors_kwargs(["GET", "OPTIONS"]))
def historial():
    if request.method == "OPTIONS":
        return "", 204

    tenant_arg = request.headers.get("X-Tenant") or request.args.get("tenant") or request.args.get("tenant_slug")
    widget_token = request.headers.get("X-Widget-Token") or request.args.get("widget_token")
    try:
        tenant, user, _ = resolve_tenant_and_user(
            whatsapp_destination_number=request.headers.get("X-Whatsapp-Dst"),
            widget_token=widget_token,
            tenant_slug=tenant_arg,
            current_user=getattr(g, "user", None),
        )
    except TenantResolutionError as exc:
        tenant, user = _resolve_public_owner()
        if tenant is None:
            tenant = TenantProfile.query.order_by(TenantProfile.id.asc()).first()
        if user is None:
            return (
                jsonify({"tenant_id": getattr(tenant, "id", None), "historial": [], "error": str(exc)}),
                200,
            )

    if tenant is None:
        return jsonify({"tenant_id": None, "historial": [], "error": "Tenant no encontrado"}), 200

    historial_registros = []
    for tx in recompensas_service().historial(user):
        timestamp_iso = tx.created_at.isoformat() if tx.created_at else None
        timestamp_local = None
        if tx.created_at:
            try:
                timestamp_local = tx.created_at.astimezone().strftime("%d/%m/%Y %H:%M")
            except Exception:
                timestamp_local = tx.created_at.strftime("%Y-%m-%d %H:%M")

        historial_registros.append(
            {
                "tipo": tx.tipo,
                "delta": tx.delta,
                "saldo_final": tx.saldo_final,
                "timestamp": timestamp_iso,
                "timestamp_humano": timestamp_local,
            }
        )
    return jsonify({"tenant_id": tenant.id, "historial": historial_registros})


@puntos_public_bp.route("/historial", methods=["GET", "OPTIONS"])
@cross_origin(**_cors_kwargs(["GET", "OPTIONS"]))
def historial_public():
    """Alias público para /api/puntos/historial."""

    return historial()

