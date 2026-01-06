from flask import Blueprint, jsonify
from models import Categoria, TenantProfile
from routes.auth import token_requerido
from utils.tenant import get_current_tenant_profile

pyme_api_bp = Blueprint(
    "pyme_api",
    __name__,
    url_prefix="/api/pyme/<tenant_slug>",
)

def _categorias_para_tenant(tenant: TenantProfile) -> list[dict]:
    categorias = (
        Categoria.query.filter_by(tenant_id=tenant.id)
        .order_by(Categoria.nombre.asc())
        .all()
    )
    return [c.to_dict() for c in categorias]

@pyme_api_bp.route("/categorias", methods=["GET", "OPTIONS"])
@token_requerido
def listar_categorias_pyme(current_user, tenant_slug: str):
    if request.method == "OPTIONS":
        return "", 204

    tenant = get_current_tenant_profile()
    if not tenant:
        return jsonify({"error": "Tenant no encontrado"}), 404

    categorias = _categorias_para_tenant(tenant)
    return jsonify({"categorias": categorias, "categories": categorias})
