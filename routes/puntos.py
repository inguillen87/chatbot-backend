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


@puntos_bp.route("/saldo", methods=["GET", "OPTIONS"], strict_slashes=False)
@cross_origin(**_cors_kwargs(["GET", "OPTIONS"]))
def saldo():
    if request.method == "OPTIONS":
        return "", 204

    tenant_arg = (
        request.headers.get("X-Tenant")
        or request.args.get("tenant")
        or request.args.get("tenant_slug")
    )
    tenant_id = request.headers.get("X-Tenant-Id") or request.args.get("tenant_id")
    widget_token = request.headers.get("X-Widget-Token") or request.args.get("widget_token")
    has_hint = bool(tenant_arg or tenant_id or widget_token or request.headers.get("X-Whatsapp-Dst"))
    if not has_hint:
        return jsonify({"error": "Tenant requerido"}), 400
    try:
        tenant, user, _ = resolve_tenant_and_user(
            whatsapp_destination_number=request.headers.get("X-Whatsapp-Dst"),
            widget_token=widget_token,
            tenant_slug=tenant_arg,
            tenant_id=tenant_id,
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
    limit = request.args.get("limit", type=int) or 5
    include_history = request.args.get("include_history") in {"1", "true", "True"}
    movimientos = []
    if include_history:
        for tx in recompensas_service().historial(user)[:limit]:
            movimientos.append(
                {
                    "tipo": tx.tipo,
                    "delta": tx.delta,
                    "saldo_final": tx.saldo_final,
                    "timestamp": tx.created_at.isoformat() if tx.created_at else None,
                }
            )

    return jsonify(
        {
            "tenant_id": tenant.id,
            "saldo": saldo_actual,
            "anonId": getattr(user, "anon_id", None),
            "movimientos": movimientos,
        }
    )


@puntos_public_bp.route("/saldo", methods=["GET", "OPTIONS"], strict_slashes=False)
@cross_origin(**_cors_kwargs(["GET", "OPTIONS"]))
def saldo_public():
    """Alias público para /api/puntos/saldo."""

    return saldo()


@puntos_bp.route("/historial", methods=["GET", "OPTIONS"], strict_slashes=False)
@cross_origin(**_cors_kwargs(["GET", "OPTIONS"]))
def historial():
    if request.method == "OPTIONS":
        return "", 204

    tenant_arg = (
        request.headers.get("X-Tenant")
        or request.args.get("tenant")
        or request.args.get("tenant_slug")
    )
    tenant_id = request.headers.get("X-Tenant-Id") or request.args.get("tenant_id")
    widget_token = request.headers.get("X-Widget-Token") or request.args.get("widget_token")
    has_hint = bool(tenant_arg or tenant_id or widget_token or request.headers.get("X-Whatsapp-Dst"))
    if not has_hint:
        return jsonify({"error": "Tenant requerido"}), 400
    try:
        tenant, user, _ = resolve_tenant_and_user(
            whatsapp_destination_number=request.headers.get("X-Whatsapp-Dst"),
            widget_token=widget_token,
            tenant_slug=tenant_arg,
            tenant_id=tenant_id,
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

    limit = request.args.get("limit", type=int) or 50
    page = request.args.get("page", type=int) or 1
    offset = max(page - 1, 0) * limit

    query = recompensas_service().historial_query(user)
    total_registros = query.count()
    historial_registros = []
    for tx in query.offset(offset).limit(limit).all():
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
                "motivo": (tx.metadata_payload or {}).get("motivo"),
                "saldo_final": tx.saldo_final,
                "timestamp": timestamp_iso,
                "timestamp_humano": timestamp_local,
            }
        )
    return jsonify(
        {
            "tenant_id": tenant.id,
            "historial": historial_registros,
            "page": page,
            "per_page": limit,
            "total": total_registros,
        }
    )


@puntos_public_bp.route("/historial", methods=["GET", "OPTIONS"], strict_slashes=False)
@cross_origin(**_cors_kwargs(["GET", "OPTIONS"]))
def historial_public():
    """Alias público para /api/puntos/historial."""

    return historial()


@puntos_bp.route("/movimientos", methods=["GET", "OPTIONS"], strict_slashes=False)
@cross_origin(**_cors_kwargs(["GET", "OPTIONS"]))
def movimientos():
    """Alias más descriptivo para historial de puntos con límite configurable."""

    return historial()


@puntos_public_bp.route("/movimientos", methods=["GET", "OPTIONS"], strict_slashes=False)
@cross_origin(**_cors_kwargs(["GET", "OPTIONS"]))
def movimientos_public():
    """Alias público para /api/puntos/movimientos."""

    return historial()

