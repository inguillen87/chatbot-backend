from flask import Blueprint, request, jsonify, g, current_app
from services.integrations.mercadolibre_service import MercadoLibreService
from services.integrations.tiendanube_service import TiendaNubeService
from utils.auth_helpers import token_requerido
from models import IntegrationAccount, db, TenantProfile
from services.plan_access import integration_access_payload, plan_allows_full_integrations

integrations_bp = Blueprint('integrations', __name__)


def _plan_allows_integrations(tenant: TenantProfile) -> bool:
    return plan_allows_full_integrations(tenant)


def _integration_plan_required_response(tenant: TenantProfile):
    access = integration_access_payload(tenant)
    return (
        jsonify(
            {
                "error": "plan_required",
                "message": access["message"],
                "access": access,
            }
        ),
        403,
    )


@integrations_bp.route('/<provider>/connect', methods=['POST'])
@token_requerido
def connect(user, provider):
    """Generates OAuth URL."""
    tenant = g.tenant_profile
    if not tenant:
        return jsonify({"error": "Tenant required"}), 400
    if not _plan_allows_integrations(tenant):
        return _integration_plan_required_response(tenant)

    redirect_uri = f"{request.host_url}api/integrations/{provider}/callback"

    if provider == "mercadolibre":
        url = MercadoLibreService.get_auth_url(tenant.id, redirect_uri)
        return jsonify({"url": url})
    elif provider == "tiendanube":
        url = TiendaNubeService.get_auth_url(tenant.id, redirect_uri)
        return jsonify({"url": url})

    return jsonify({"error": "Provider not supported"}), 400

@integrations_bp.route('/<provider>/callback', methods=['GET'])
def callback(provider):
    """Handles OAuth callback."""
    code = request.args.get('code')
    state = request.args.get('state') # Used as tenant_id

    if not code or not state:
        return jsonify({"error": "Missing code or state"}), 400

    redirect_uri = f"{request.host_url}api/integrations/{provider}/callback"

    try:
        tenant = TenantProfile.query.get(state)
        if not tenant:
            return jsonify({"error": "Tenant not found"}), 404
        if not _plan_allows_integrations(tenant):
            return jsonify(integration_access_payload(tenant)), 403

        if provider == "mercadolibre":
            MercadoLibreService.handle_callback(state, code, redirect_uri)
        elif provider == "tiendanube":
            TiendaNubeService.handle_callback(state, code, redirect_uri)

        return "Conexión exitosa. Puede cerrar esta ventana."
    except Exception as e:
        current_app.logger.error(f"OAuth Error: {e}")
        return f"Error en conexión: {str(e)}", 500

@integrations_bp.route('/webhooks/<provider>', methods=['POST'])
def webhook(provider):
    """Receives external events."""
    # Note: Real implementation needs to securely identify tenant from the webhook payload or a unique URL.
    # For ML, we receive a global notification. Identifying the tenant is tricky without keeping a map of UserID -> TenantID.
    # For this MVP, we assume the query param ?tenant_id=X is set in the webhook URL registered in ML.

    tenant_id = request.args.get('tenant_id')
    payload = request.get_json(silent=True) or request.form.to_dict()

    try:
        if provider == "mercadolibre":
            # Pass tenant_id if available to help identify the account
            MercadoLibreService.process_webhook(payload, tenant_id)

        return jsonify({"status": "ok"}), 200
    except Exception as e:
        current_app.logger.error(f"Webhook Error: {e}")
        return jsonify({"error": str(e)}), 500


@integrations_bp.route('/<provider>/preview', methods=['GET'])
@token_requerido
def preview_integration(current_user, provider):
    """
    Returns a preview of the integration mapping status.
    This prevents 404s on the frontend and provides user feedback.
    """
    tenant = g.tenant_profile
    if not tenant:
        return jsonify({"error": "Tenant required"}), 400
    if not _plan_allows_integrations(tenant):
        return _integration_plan_required_response(tenant)

    # Validate provider
    if provider not in ['mercadolibre', 'tiendanube']:
        return jsonify({"error": "Provider not supported"}), 400

    # Check connection status
    account = IntegrationAccount.query.filter_by(
        tenant_id=tenant.id,
        type=provider,
        status="active"
    ).first()

    if not account:
        return jsonify({
            "status": "disconnected",
            "message": f"Conectá tu cuenta de {provider} para sincronizar productos.",
            "preview_data": [],
            "mapped_count": 0,
            "error_count": 0
        })

    # TODO: Implement real-time fetch of remote items for comparison
    # For now, return a 'connected' state with existing catalog stats to satisfy the preview UI

    # Simple heuristic: Count items with the provider's metadata/ID if stored,
    # or just total items eligible for sync.
    from models import CatalogoItem
    total_items = CatalogoItem.query.filter_by(tenant_id=tenant.id).count()

    # Mock preview data for the UI
    preview_sample = []
    if total_items > 0:
        item = CatalogoItem.query.filter_by(tenant_id=tenant.id).first()
        preview_sample.append({
            "local_sku": item.sku or "SKU-UNK",
            "local_title": item.nombre,
            "remote_status": "pending_sync", # Default state
            "message": "Listo para publicar"
        })

    return jsonify({
        "status": "connected",
        "message": f"Cuenta vinculada. {total_items} productos listos para sincronizar.",
        "preview_data": preview_sample,
        "mapped_count": 0, # To be implemented with real sync logic
        "pending_count": total_items,
        "error_count": 0
    })
