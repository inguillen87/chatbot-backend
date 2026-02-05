from flask import Blueprint, request, jsonify, g
from utils.auth_helpers import token_requerido
from models import TenantProfile, User
from services.widget_config_service import WidgetConfigService

widget_config_bp = Blueprint('widget_config_bp', __name__)

def _resolve_tenant_by_slug(slug):
    # Basic resolver, could use the robust one from admin_tenant if shared
    return TenantProfile.query.filter_by(slug=slug).first()

def _check_auth(current_user, tenant):
    """
    Checks if current_user has access to manage this tenant.
    """
    if current_user.rol in ("platform_admin", "super_admin"):
        return True

    # Direct Tenant ID match
    if current_user.tenant_id == tenant.id:
        return True

    # Legacy Ownership (Municipio/Pyme ID)
    if tenant.municipio_id and str(current_user.id) == str(tenant.municipio_id):
        return True
    if tenant.pyme_id and str(current_user.id) == str(tenant.pyme_id):
        return True

    return False

# --- Public Endpoints ---

@widget_config_bp.route('/api/public/tenants/<slug>/saas-config', methods=['GET', 'OPTIONS'])
def public_get_config(slug):
    if request.method == 'OPTIONS':
        return jsonify({"status": "ok"}), 200

    config = WidgetConfigService.get_public_config(slug)
    if not config:
        return jsonify({"error": "Tenant config not found"}), 404

    return jsonify(config)

# --- Admin Endpoints (SaaS) ---

@widget_config_bp.route('/api/admin/tenants/<slug>/widget-config', methods=['GET'])
@token_requerido
def admin_get_config(current_user, slug):
    tenant = _resolve_tenant_by_slug(slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    if not _check_auth(current_user, tenant):
        return jsonify({"error": "Unauthorized"}), 403

    config_bundle = WidgetConfigService.get_admin_config(slug)
    return jsonify(config_bundle)

@widget_config_bp.route('/api/admin/tenants/<slug>/widget-config', methods=['PUT'])
@token_requerido
def admin_update_draft(current_user, slug):
    tenant = _resolve_tenant_by_slug(slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    if not _check_auth(current_user, tenant):
        return jsonify({"error": "Unauthorized"}), 403

    payload = request.get_json() or {}
    try:
        updated_draft = WidgetConfigService.update_draft(slug, payload)
        return jsonify({"draft": updated_draft})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@widget_config_bp.route('/api/admin/tenants/<slug>/widget-config/publish', methods=['POST'])
@token_requerido
def admin_publish_config(current_user, slug):
    tenant = _resolve_tenant_by_slug(slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    if not _check_auth(current_user, tenant):
        return jsonify({"error": "Unauthorized"}), 403

    try:
        live_config = WidgetConfigService.publish_draft(slug, current_user.id)
        return jsonify({"status": "published", "live": live_config})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@widget_config_bp.route('/api/admin/tenants/<slug>/widget-config/preview', methods=['POST'])
@token_requerido
def admin_preview_config(current_user, slug):
    tenant = _resolve_tenant_by_slug(slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    if not _check_auth(current_user, tenant):
        return jsonify({"error": "Unauthorized"}), 403

    payload = request.get_json() or {}
    # Just sanitize and return, don't save
    preview = WidgetConfigService._validate_and_sanitize(payload)
    merged = WidgetConfigService._merge_with_defaults(preview)
    return jsonify(merged)
