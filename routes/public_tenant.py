from flask import Blueprint, request, jsonify
from models import TenantProfile, TenantConfig

public_tenant_bp = Blueprint('public_tenant_bp', __name__)


def _get_tenant_from_request(slug: str):
    """Resolve a tenant using the path slug with a querystring fallback.

    Some clients historically call these endpoints with an incorrect path slug
    but include the real tenant slug in either ``tenant`` or ``tenant_slug``
    query parameters. We try to honor that to avoid a hard 404 when the path
    slug is merely a placeholder like ``perfil``.
    """

    tenant = TenantProfile.query.filter_by(slug=slug).first()
    if tenant:
        return tenant

    fallback_slug = request.args.get("tenant") or request.args.get("tenant_slug")
    if fallback_slug and fallback_slug != slug:
        tenant = TenantProfile.query.filter_by(slug=fallback_slug).first()

    return tenant

@public_tenant_bp.route('/api/public/tenants/<slug>/menu', methods=['GET', 'OPTIONS'])
def get_menu(slug):
    if request.method == 'OPTIONS':
        return jsonify({"ok": True})

    channel = request.args.get('channel')

    tenant = _get_tenant_from_request(slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    cfg = None
    if channel:
        cfg = TenantConfig.query.filter_by(tenant_id=tenant.id, key='menu', channel=channel).first()

    if not cfg:
        cfg = TenantConfig.query.filter_by(tenant_id=tenant.id, key='menu', channel=None).first()

    return jsonify(cfg.json_value if cfg else {})

@public_tenant_bp.route('/api/public/tenants/<slug>/contacts', methods=['GET', 'OPTIONS'])
def get_contacts(slug):
    if request.method == 'OPTIONS':
        return jsonify({"ok": True})

    tenant = _get_tenant_from_request(slug)
    if not tenant: return jsonify({"error": "Tenant not found"}), 404

    cfg = TenantConfig.query.filter_by(tenant_id=tenant.id, key='contacts', channel=None).first()
    return jsonify(cfg.json_value if cfg else {})

@public_tenant_bp.route('/api/public/tenants/<slug>/links', methods=['GET', 'OPTIONS'])
def get_links(slug):
    if request.method == 'OPTIONS':
        return jsonify({"ok": True})

    tenant = _get_tenant_from_request(slug)
    if not tenant: return jsonify({"error": "Tenant not found"}), 404

    cfg = TenantConfig.query.filter_by(tenant_id=tenant.id, key='links', channel=None).first()
    return jsonify(cfg.json_value if cfg else {})

@public_tenant_bp.route('/api/public/tenants/<slug>/widget-config', methods=['GET', 'OPTIONS'])
def get_widget_config(slug):
    if request.method == 'OPTIONS':
        return jsonify({"ok": True})

    tenant = _get_tenant_from_request(slug)
    if not tenant: return jsonify({"error": "Tenant not found"}), 404

    cfg = TenantConfig.query.filter_by(tenant_id=tenant.id, key='widget', channel=None).first()
    return jsonify(cfg.json_value if cfg else {})
