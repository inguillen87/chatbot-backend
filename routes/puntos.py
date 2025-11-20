from flask import Blueprint, jsonify, request, g

from services.rewards import recompensas_service
from services.tenant_resolver import TenantResolutionError, resolve_tenant_and_user

puntos_bp = Blueprint("puntos_bp", __name__, url_prefix="/api/puntos")


@puntos_bp.route("/saldo", methods=["GET"])
def saldo():
    try:
        tenant, user, _ = resolve_tenant_and_user(
            whatsapp_destination_number=request.headers.get("X-Whatsapp-Dst"),
            widget_token=request.headers.get("X-Widget-Token"),
            tenant_slug=request.headers.get("X-Tenant"),
            current_user=getattr(g, "user", None),
        )
    except TenantResolutionError as exc:
        return jsonify({"error": str(exc)}), 404

    saldo_actual = recompensas_service().obtener_saldo(user)
    return jsonify({"tenant_id": tenant.id, "saldo": saldo_actual, "anonId": user.anon_id})


@puntos_bp.route("/historial", methods=["GET"])
def historial():
    try:
        tenant, user, _ = resolve_tenant_and_user(
            whatsapp_destination_number=request.headers.get("X-Whatsapp-Dst"),
            widget_token=request.headers.get("X-Widget-Token"),
            tenant_slug=request.headers.get("X-Tenant"),
            current_user=getattr(g, "user", None),
        )
    except TenantResolutionError as exc:
        return jsonify({"error": str(exc)}), 404

    historial_registros = [
        {
            "tipo": tx.tipo,
            "delta": tx.delta,
            "saldo_final": tx.saldo_final,
            "timestamp": tx.created_at.isoformat() if tx.created_at else None,
        }
        for tx in recompensas_service().historial(user)
    ]
    return jsonify({"tenant_id": tenant.id, "historial": historial_registros})

