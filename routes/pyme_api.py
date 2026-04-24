from flask import Blueprint, jsonify, request, g, current_app, abort
from flask_cors import cross_origin
from models import CategoriaTicket, TenantProfile
from routes.ticket import TICKET_ALLOWED_STATES
from routes.auth import token_requerido
from utils.tenant import get_current_tenant, get_current_tenant_profile
from services.tenant_resolver import apply_tenant_alias, resolve_tenant_only, TenantResolutionError
from config import ALLOWED_ORIGINS

pyme_api_bp = Blueprint('pyme_api_bp', __name__, url_prefix='/api/pyme')

def _resolve_tenant_or_404(tenant_slug: str) -> TenantProfile:
    if tenant_slug:
        g.current_tenant = tenant_slug
        g.tenant_slug = tenant_slug

    normalized_slug = (tenant_slug or "").strip() or None
    mapped_slug = apply_tenant_alias(normalized_slug) or normalized_slug
    fallback_slug = current_app.config.get("PUBLIC_CATALOG_DEFAULT_TENANT")

    tenant: TenantProfile | None = None
    for candidate in dict.fromkeys(
        [mapped_slug, normalized_slug, fallback_slug, get_current_tenant()]
    ):
        if not candidate:
            continue
        tenant = get_current_tenant_profile(candidate)
        if tenant:
            break
        try:
            tenant = resolve_tenant_only(
                tenant_slug=candidate,
                require_explicit_slug=False,
            )
        except TenantResolutionError:
            tenant = None
        if tenant:
            break

    if not tenant and fallback_slug:
        tenant = get_current_tenant_profile(fallback_slug)

    if not tenant:
        abort(404, "Tenant no encontrado")

    g.tenant = tenant
    g.tenant_profile = tenant
    return tenant

def _add_cors_headers(response):
    origin = request.headers.get('Origin', '*')
    response.headers.add('Access-Control-Allow-Origin', origin)
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Tenant,X-Requested-With,X-Anon-Id,X-Tenant-Slug')
    response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS,PUT,DELETE')
    response.headers.add('Access-Control-Allow-Credentials', 'true')
    return response

@pyme_api_bp.route('/estados', methods=['GET', 'OPTIONS'])
def listar_estados():
    if request.method == 'OPTIONS':
         response = jsonify({'status': 'ok'})
         return _add_cors_headers(response)

    tenant_slug = request.args.get('tenant_slug')
    _resolve_tenant_or_404(tenant_slug)

    response = jsonify({"estados": sorted(TICKET_ALLOWED_STATES)})
    return _add_cors_headers(response)

@pyme_api_bp.route('/categorias', methods=['GET', 'OPTIONS'])
def listar_categorias():
    if request.method == 'OPTIONS':
         response = jsonify({'status': 'ok'})
         return _add_cors_headers(response)

    tenant_slug = request.args.get('tenant_slug')
    tenant = _resolve_tenant_or_404(tenant_slug)

    categorias = (
        CategoriaTicket.query.filter_by(tenant_id=tenant.id)
        .order_by(CategoriaTicket.nombre.asc())
        .all()
    )
    payload = [c.to_dict() for c in categorias]

    response = jsonify({"categorias": payload, "categories": payload})
    return _add_cors_headers(response)
