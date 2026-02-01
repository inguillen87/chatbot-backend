from flask import Blueprint, request, jsonify, g
from utils.auth_helpers import token_requerido
from middleware.tenant_context import require_tenant
from models import db, TenantProfile
from routes.admin_tenant import _resolve_admin_tenant, _is_authorized_for_tenant

admin_fulfillment_bp = Blueprint('admin_fulfillment_bp', __name__)

@admin_fulfillment_bp.route('/api/fulfillment-config', methods=['GET', 'PUT', 'OPTIONS'])
@token_requerido
@require_tenant
def fulfillment_config(current_user):
    """
    Get or update fulfillment configuration for the current tenant.
    Query Params: tenant_slug (required via require_tenant)
    """
    # Fix for OPTIONS requests
    if request.method == 'OPTIONS':
        return jsonify({'status': 'ok'}), 200

    # Resolve tenant
    tenant = getattr(g, 'tenant_profile', None)
    if not tenant:
        slug = request.args.get('tenant_slug')
        if slug:
            tenant = _resolve_admin_tenant(current_user, slug)

    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    # Authorization Check
    if not _is_authorized_for_tenant(current_user, tenant):
         return jsonify({'error': 'Unauthorized'}), 403

    if request.method == 'GET':
        return jsonify({
            "dispatch_email": getattr(tenant, "dispatch_email", "") or "",
            "dispatch_phone": getattr(tenant, "dispatch_phone", "") or "",
            "send_buyer_email": getattr(tenant, "send_buyer_email", False),
            "send_dispatch_email": getattr(tenant, "send_dispatch_email", False),
            "send_dispatch_whatsapp": getattr(tenant, "send_dispatch_whatsapp", False)
        })

    elif request.method == 'PUT':
        data = request.json or {}

        # Update allowed fields
        if 'dispatch_email' in data:
            tenant.dispatch_email = data['dispatch_email']

        if 'dispatch_phone' in data:
            tenant.dispatch_phone = data['dispatch_phone']

        if 'send_buyer_email' in data:
            tenant.send_buyer_email = bool(data['send_buyer_email'])

        if 'send_dispatch_email' in data:
            tenant.send_dispatch_email = bool(data['send_dispatch_email'])

        if 'send_dispatch_whatsapp' in data:
            tenant.send_dispatch_whatsapp = bool(data['send_dispatch_whatsapp'])

        try:
            db.session.commit()
            return jsonify({"message": "Fulfillment configuration updated successfully"})
        except Exception as e:
            db.session.rollback()
            return jsonify({"error": str(e)}), 500
