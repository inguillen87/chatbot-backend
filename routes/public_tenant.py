from flask import Blueprint, request, jsonify
from models import TenantProfile, TenantConfig

public_tenant_bp = Blueprint('public_tenant_bp', __name__)

@public_tenant_bp.route('/api/public/tenants/<slug>/menu', methods=['GET'])
def get_menu(slug):
    channel = request.args.get('channel')

    tenant = TenantProfile.query.filter_by(slug=slug).first()
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    cfg = None
    if channel:
        cfg = TenantConfig.query.filter_by(tenant_id=tenant.id, key='menu', channel=channel).first()

    if not cfg:
        cfg = TenantConfig.query.filter_by(tenant_id=tenant.id, key='menu', channel=None).first()

    return jsonify(cfg.json_value if cfg else {})

@public_tenant_bp.route('/api/public/tenants/<slug>/contacts', methods=['GET'])
def get_contacts(slug):
    tenant = TenantProfile.query.filter_by(slug=slug).first()
    if not tenant: return jsonify({"error": "Tenant not found"}), 404

    cfg = TenantConfig.query.filter_by(tenant_id=tenant.id, key='contacts', channel=None).first()
    return jsonify(cfg.json_value if cfg else {})

@public_tenant_bp.route('/api/public/tenants/<slug>/links', methods=['GET'])
def get_links(slug):
    tenant = TenantProfile.query.filter_by(slug=slug).first()
    if not tenant: return jsonify({"error": "Tenant not found"}), 404

    cfg = TenantConfig.query.filter_by(tenant_id=tenant.id, key='links', channel=None).first()
    return jsonify(cfg.json_value if cfg else {})

@public_tenant_bp.route('/api/public/tenants/<slug>/widget-config', methods=['GET'])
def get_widget_config(slug):
    tenant = TenantProfile.query.filter_by(slug=slug).first()
    if not tenant: return jsonify({"error": "Tenant not found"}), 404

    cfg = TenantConfig.query.filter_by(tenant_id=tenant.id, key='widget', channel=None).first()
    return jsonify(cfg.json_value if cfg else {})
