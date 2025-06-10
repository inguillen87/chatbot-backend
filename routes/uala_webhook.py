# routes/uala_webhook.py
from flask import Blueprint, request, jsonify
from extensions import db
from models import User
import logging

uala_bp = Blueprint("uala_bp", __name__)

@uala_bp.route("/uala_webhook", methods=["POST"])
def uala_webhook():
    data = request.json
    logging.info(f"🟣 Webhook Ualá Bis recibido: {data}")

    # Campos típicos que podrías recibir
    email = data.get("payer", {}).get("email") or data.get("email") or data.get("customer_email")
    order_id = data.get("order", {}).get("id") or data.get("order_id")
    amount = float(data.get("order", {}).get("total", 0)) if data.get("order") else float(data.get("amount", 0))

    # Lógica de planes
    plan = None
    if amount == 30000:
        plan = "pro"
    elif amount == 60000:
        plan = "full"

    # Validación mínima
    if not email or not plan:
        logging.warning(f"Webhook con datos insuficientes: {data}")
        return jsonify({"error": "Datos insuficientes"}), 400

    user = User.query.filter_by(email=email).first()
    if not user:
        logging.warning(f"Usuario no encontrado para email: {email}")
        return jsonify({"error": "Usuario no encontrado"}), 404

    # Control de duplicados (opcional)
    if hasattr(user, "last_webhook_id") and user.last_webhook_id == order_id:
        logging.info(f"Webhook duplicado detectado: {order_id}")
        return jsonify({"ok": True, "msg": "Duplicado, ya procesado"})

    user.plan = plan
    if hasattr(user, "last_webhook_id"):
        user.last_webhook_id = order_id  # guardá el último procesado
    db.session.commit()
    logging.info(f"Plan actualizado a {plan} para usuario {email}")

    return jsonify({"ok": True, "msg": f"Upgrade exitoso a {plan}"})
