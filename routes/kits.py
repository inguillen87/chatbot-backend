from flask import Blueprint, jsonify, request, g

from services.kits import listar_kits, sugerir_kits_para_carrito
from services.tenant_resolver import TenantResolutionError, resolve_tenant_and_user

kits_bp = Blueprint("kits_bp", __name__, url_prefix="/api/pwa/kits")


@kits_bp.route("", methods=["GET"])
def listar():
    try:
        tenant, _, _ = resolve_tenant_and_user(
            tenant_slug=request.headers.get("X-Tenant"), current_user=getattr(g, "user", None)
        )
    except TenantResolutionError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"kits": listar_kits(tenant)})


@kits_bp.route("/sugerencias", methods=["POST"])
def sugerencias():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        tenant, _, _ = resolve_tenant_and_user(
            tenant_slug=request.headers.get("X-Tenant"), current_user=getattr(g, "user", None)
        )
    except TenantResolutionError as exc:
        return jsonify({"error": str(exc)}), 404

    productos = payload.get("productos") or []
    # Para esta versión, solo aleatorizamos sobre kits existentes
    sugeridos = sugerir_kits_para_carrito(tenant, productos)
    return jsonify({"kits": sugeridos})

