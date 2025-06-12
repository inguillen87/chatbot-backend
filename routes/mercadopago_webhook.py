# routes/mercadopago_webhook.py
from flask import Blueprint, request, jsonify
from extensions import db
from models import User
import logging
import requests

mp_bp = Blueprint("mp_bp", __name__)

ACCESS_TOKEN = "TEST-1688111541735106-061215-d58dd42a5db75ad361985176634393ee-2474247593"

PLANES = {
    "2c9380849764e81a01976585767f0040": "pro",   # PRO
    "2c9380849763daeb0197658791ee00b1": "full",  # FULL
}

@mp_bp.route("/mercadopago_webhook", methods=["POST"])
def mercadopago_webhook():
    data = request.json
    logging.info(f"🔵 Webhook Mercado Pago recibido: {data}")

    topic = data.get("type")
    action = data.get("action")
    preapproval_id = data.get("data", {}).get("id")  # El ID de la suscripción (preapproval_id)

    # Si el webhook es por suscripción (preapproval)
    if topic == "preapproval":
        # Consultamos detalles de la suscripción a la API de Mercado Pago
        url = f"https://api.mercadopago.com/preapproval/{preapproval_id}"
        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            logging.warning(f"No se pudo consultar preapproval: {preapproval_id}")
            return jsonify({"error": "No se pudo consultar preapproval"}), 400
        info = resp.json()

        # Extraer mail del usuario y plan_id
        email = info.get("payer_email")
        plan_id = info.get("preapproval_plan_id")
        status = info.get("status")  # authorized, cancelled, paused, etc.

        if not email or not plan_id:
            logging.warning(f"Faltan datos: {info}")
            return jsonify({"error": "Faltan datos"}), 400

        plan = PLANES.get(plan_id)
        if not plan:
            logging.warning(f"Plan no reconocido: {plan_id}")
            return jsonify({"error": "Plan desconocido"}), 400

        user = User.query.filter_by(email=email).first()
        if not user:
            # Si no existe, podrías crearlo o loguear y abortar
            logging.warning(f"Usuario no encontrado para email: {email}")
            return jsonify({"error": "Usuario no encontrado"}), 404

        # Actualizá datos del usuario
        user.plan = plan
        user.preapproval_id = preapproval_id
        user.plan_status = status
        db.session.commit()
        logging.info(f"Usuario {email} actualizado a plan {plan}, status {status}")

        return jsonify({"ok": True, "msg": f"Upgrade exitoso a {plan} ({status})"})

    return jsonify({"ok": False, "msg": "Evento ignorado"}), 200
