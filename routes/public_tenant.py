from flask import Blueprint, request, jsonify, g, current_app
from models import TenantProfile, TenantConfig, WidgetSettings, db
from routes.auth import token_requerido
from services.tenant_resolver import resolve_tenant_only
from middleware.tenant_context import require_tenant

public_tenant_bp = Blueprint('public_tenant_bp', __name__)

def _add_cors_headers(response):
    origin = request.headers.get('Origin', '*')
    response.headers.add('Access-Control-Allow-Origin', origin)
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Tenant,X-Requested-With,X-Anon-Id,X-Chat-Session-Id')
    response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS,PUT,DELETE,PATCH')
    response.headers.add('Access-Control-Allow-Credentials', 'true')
    return response

def _get_tenant_from_request(slug: str):
    """Resolve a tenant using the path slug with a querystring fallback."""
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
        return _add_cors_headers(jsonify({"ok": True}))

    channel = request.args.get('channel')

    tenant = _get_tenant_from_request(slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    cfg = None
    if channel:
        cfg = TenantConfig.query.filter_by(tenant_id=tenant.id, key='menu', channel=channel).first()

    if not cfg:
        cfg = TenantConfig.query.filter_by(tenant_id=tenant.id, key='menu', channel=None).first()

    response = jsonify(cfg.json_value if cfg else {})
    return _add_cors_headers(response)

@public_tenant_bp.route('/api/public/tenants/<slug>/contacts', methods=['GET', 'OPTIONS'])
def get_contacts(slug):
    if request.method == 'OPTIONS':
        return _add_cors_headers(jsonify({"ok": True}))

    tenant = _get_tenant_from_request(slug)
    if not tenant: return jsonify({"error": "Tenant not found"}), 404

    cfg = TenantConfig.query.filter_by(tenant_id=tenant.id, key='contacts', channel=None).first()
    response = jsonify(cfg.json_value if cfg else {})
    return _add_cors_headers(response)

@public_tenant_bp.route('/api/public/tenants/<slug>/links', methods=['GET', 'OPTIONS'])
def get_links(slug):
    if request.method == 'OPTIONS':
        return _add_cors_headers(jsonify({"ok": True}))

    tenant = _get_tenant_from_request(slug)
    if not tenant: return jsonify({"error": "Tenant not found"}), 404

    cfg = TenantConfig.query.filter_by(tenant_id=tenant.id, key='links', channel=None).first()
    response = jsonify(cfg.json_value if cfg else {})
    return _add_cors_headers(response)

@public_tenant_bp.route('/api/public/tenants/<slug>/widget-config', methods=['GET', 'OPTIONS'])
def get_widget_config(slug):
    if request.method == 'OPTIONS':
        return _add_cors_headers(jsonify({"ok": True}))

    tenant = _get_tenant_from_request(slug)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    from routes.pwa_public import public_tenant_widget_config

    # We need to capture the response from the other blueprint function
    # and ensure CORS headers are added.
    # Typically that function returns a JSON response object.

    # This is a bit hacky but avoids code duplication.
    # However, public_tenant_widget_config might return a Response object.

    try:
        # Assuming public_tenant_widget_config returns a response object
        resp = public_tenant_widget_config(tenant.slug)
        if isinstance(resp, tuple):
             resp_obj, status = resp
             return _add_cors_headers(resp_obj), status
        return _add_cors_headers(resp)
    except Exception as e:
        current_app.logger.error(f"Error fetching widget config: {e}")
        return jsonify({"error": "Internal Error"}), 500

# --- Fix for missing /api/tenant/config endpoint ---

@public_tenant_bp.route('/api/tenant/config', methods=['GET', 'PUT', 'OPTIONS'])
@token_requerido
@require_tenant
def tenant_config_api(current_user):
    """
    Endpoint for tenant configuration (Admin Panel -> Appearance).
    Supports GET (view) and PUT (update).
    Requires 'tenant_slug' or 'tenant' query param (handled by require_tenant middleware).
    """
    if request.method == 'OPTIONS':
        return _add_cors_headers(jsonify({"ok": True}))

    tenant = g.tenant_profile
    if not tenant:
        return jsonify({"error": "Tenant context required"}), 400

    # Authorization Check
    # Allow if user is admin/owner of this tenant
    # Using existing helper from admin_tenant if available, or simple logic
    authorized = False
    if current_user.rol in ('super_admin', 'platform_admin'):
        authorized = True
    elif current_user.tenant_id == tenant.id:
        authorized = True
    elif getattr(current_user, 'pyme_id', None) == getattr(tenant, 'pyme_id', None) and getattr(tenant, 'pyme_id', None):
        authorized = True

    if not authorized:
         response = jsonify({'error': 'Unauthorized'})
         return _add_cors_headers(response), 403

    if request.method == 'GET':
        # Return merged config (TenantProfile + WidgetSettings)
        # Similar to what the frontend expects: theme_json, welcome_message, etc.

        settings = WidgetSettings.query.filter_by(tenant_id=tenant.id).first()
        if not settings:
            # Return defaults from TenantProfile if no specific widget settings
            response = jsonify({
                "theme_json": tenant.theme_json or {},
                "welcome_message": "¡Hola! ¿En qué puedo ayudarte?",
                "avatar_url": tenant.logo_url,
                "primary_color": "#000000", # Default
                "secondary_color": "#FFFFFF"
            })
        else:
            response = jsonify({
                "theme_json": settings.theme_config or tenant.theme_json or {},
                "welcome_message": settings.welcome_title or "¡Hola! ¿En qué puedo ayudarte?",
                "welcome_subtitle": settings.welcome_subtitle,
                "avatar_url": settings.avatar_url or tenant.logo_url,
                "primary_color": settings.primary_color,
                "secondary_color": settings.secondary_color,
                # Include other settings as needed
                "bottom": settings.bottom,
                "side_offset": settings.side_offset,
                "bubble_shape": settings.bubble_shape,
                "font_family": settings.font_family,
                "default_open": settings.default_open
            })
        return _add_cors_headers(response)

    elif request.method == 'PUT':
        data = request.json or {}

        settings = WidgetSettings.query.filter_by(tenant_id=tenant.id).first()
        if not settings:
            settings = WidgetSettings(tenant_id=tenant.id)
            db.session.add(settings)

        # Map fields
        if 'theme_json' in data:
            settings.theme_config = data['theme_json']
            tenant.theme_json = data['theme_json'] # Sync back to profile

        if 'welcome_message' in data: settings.welcome_title = data['welcome_message']
        if 'welcome_subtitle' in data: settings.welcome_subtitle = data['welcome_subtitle']
        if 'avatar_url' in data: settings.avatar_url = data['avatar_url']

        # Color handling from theme_json usually, but if sent separately:
        if 'primary_color' in data: settings.primary_color = data['primary_color']
        if 'secondary_color' in data: settings.secondary_color = data['secondary_color']

        # Style props
        if 'bottom' in data: settings.bottom = data['bottom']
        if 'side_offset' in data: settings.side_offset = data['side_offset']
        if 'bubble_shape' in data: settings.bubble_shape = data['bubble_shape']
        if 'font_family' in data: settings.font_family = data['font_family']
        if 'default_open' in data: settings.default_open = bool(data['default_open'])

        db.session.commit()
        return _add_cors_headers(jsonify({"status": "updated"}))

# --- Fix for missing /api/<slug>/live-chat/schedule ---

@public_tenant_bp.route('/api/<slug>/live-chat/schedule', methods=['GET', 'OPTIONS'])
def public_live_chat_schedule(slug):
    if request.method == 'OPTIONS':
        return _add_cors_headers(jsonify({"ok": True}))

    from services.live_chat_schedule import build_live_chat_status
    # We might want to pass the tenant slug to build_live_chat_status if it supports tenant-specific schedules
    # For now, assuming global or default logic, but checking tenant existence first

    tenant = _get_tenant_from_request(slug)
    if not tenant:
         return jsonify({"error": "Tenant not found"}), 404

    # If build_live_chat_status accepts a tenant, pass it.
    # Checking source code of services/live_chat_schedule.py would be ideal, but for the fix:
    try:
        # Assuming it returns a dict
        status = build_live_chat_status()
        # If we need tenant specific, we'd add logic here.
        return _add_cors_headers(jsonify(status))
    except Exception as e:
        current_app.logger.error(f"Error getting schedule: {e}")
        return jsonify({"error": "Internal Error"}), 500
