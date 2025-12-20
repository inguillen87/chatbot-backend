from flask import Blueprint, request, jsonify, current_app
from models import IntegrationAccount, TenantProfile, db

integrations_bp = Blueprint('integrations_bp', __name__, url_prefix='/api/integrations')

@integrations_bp.route('/mercadolibre/webhook', methods=['POST'])
def mercadolibre_webhook():
    """
    Recibe notificaciones de MercadoLibre.
    """
    data = request.json or {}
    topic = data.get('topic')
    resource = data.get('resource')
    user_id = data.get('user_id') # ML user_id (seller)

    # Map ML user_id to tenant
    # In a real scenario, we'd query IntegrationAccount filtering by credentials->user_id
    # For now, we stub it.

    current_app.logger.info(f"[ML Webhook] Topic: {topic}, Resource: {resource}, User: {user_id}")

    return jsonify({"status": "received"}), 200
