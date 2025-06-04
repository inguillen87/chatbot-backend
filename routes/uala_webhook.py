# routes/uala_webhook.py
from flask import Blueprint, request, jsonify
from extensions import db
from models import User
import logging

uala_bp = Blueprint("uala_bp", __name__)

@uala_bp.route("/uala_webhook", methods=["POST"])
def uala_webhook():
    data = request.json
    logging.info(f"🟣 Webhook Ualá Bis: {data}")

    # Ajustá los campos a los datos que realmente recibís del webhook de Ualá
    email = data.get("payer", {}).get("email") or data.get("email") or data.get("customer_email")
    order = data.get("order", {})
    amount = float(order.get("total", 0)) if order else 0

    # Si vos querés validar por link, podés agregar un campo “order_id” y ver de dónde vino el pago
    plan = None
    if amount == 30000:
        plan = "pro"
    elif amount == 60000:
        plan = "full"

    if not email or not plan:
        return jsonify({"error": "Datos insuficientes"}), 400

    user = User.query.filter_by(email=email).first()
    if user:
        user.plan = plan
        db.session.commit()
        return jsonify({"ok": True, "msg": f"Upgrade exitoso a {plan}"})
    else:
        return jsonify({"error": "Usuario no encontrado"}), 404
