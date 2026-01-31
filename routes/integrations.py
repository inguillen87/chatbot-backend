from flask import Blueprint, request, jsonify, g, current_app
from services.integrations.mercadolibre_service import MercadoLibreService
from services.integrations.tiendanube_service import TiendaNubeService
from utils.auth_helpers import token_requerido
from models import IntegrationAccount, db, TenantProfile

integrations_bp = Blueprint('integrations', __name__)


def _plan_allows_integrations(tenant: TenantProfile) -> bool:
    plan_key = (tenant.plan or "").strip().lower()
    return plan_key in ("pro", "full")


@integrations_bp.route('/<provider>/connect', methods=['POST'])
@token_requerido
def connect(user, provider):
    """Generates OAuth URL."""
    tenant = g.tenant_profile
    if not tenant:
        return jsonify({"error": "Tenant required"}), 400
    if not _plan_allows_integrations(tenant):
        return (
            jsonify(
                {
                    "error": "plan_required",
                    "message": "Integraciones disponibles para planes Pro/Full.",
                }
            ),
            403,
        )

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
