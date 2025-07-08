from flask import Blueprint, request, jsonify, abort # Basic Flask components
from twilio.request_validator import RequestValidator # For validating Twilio requests
from twilio.rest import Client # For sending messages via Twilio
import os # For accessing environment variables
# JSON import is no longer needed as we're moving to DB based mapping
# import json
from models import WhatsappNumero, User # Import the new WhatsappNumero model and User for relationship
from extensions import db # Import db instance for database operations


# Define the blueprint for WhatsApp webhooks
webhook_bp = Blueprint('whatsapp_webhook', __name__)

# Load environment variables for Twilio credentials
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")

# Initialize Twilio client and request validator
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    validator = RequestValidator(TWILIO_AUTH_TOKEN) # Used to validate incoming webhook signatures
else:
    # Log a warning if Twilio credentials are not set
    print("Warning: TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN environment variables not set. Twilio client and validator will not be initialized.")
    twilio_client = None
    validator = None

@webhook_bp.route("/webhook/whatsapp", methods=["POST"])
def whatsapp_webhook():
    """
    Handles incoming WhatsApp messages from Twilio.
    Validates the request, identifies the client via database lookup,
    processes the message (stubbed), and sends a response back (stubbed).
    """
    if not validator:
        # Abort if the validator wasn't initialized (due to missing auth token)
        print("Error: Twilio RequestValidator not initialized. Ensure TWILIO_AUTH_TOKEN is set.")
        abort(500, "Twilio validator not configured")

    # 1. Validar autenticidad del webhook Twilio
    signature = request.headers.get("X-Twilio-Signature", "")
    url = request.url
    post_vars = request.form.to_dict()

    if not validator.validate(url, post_vars, signature):
        abort(403, "Invalid Twilio signature")

    # 2. Obtener info básica del mensaje
    to_number_raw = post_vars.get("To", "")      # Our Twilio number (e.g., "whatsapp:+14155238886")
    from_number_raw = post_vars.get("From", "")  # The end-user's WhatsApp number
    message_body = post_vars.get("Body", "")     # The content of the user's message

    # Clean the "whatsapp:" prefix from numbers for internal use and database lookup
    # The number stored in DB should be in E.164 format e.g. +14155238886
    to_number_cleaned = to_number_raw.replace("whatsapp:", "")
    from_number_cleaned = from_number_raw.replace("whatsapp:", "")

    print(f"Received WhatsApp message to: {to_number_cleaned}, from: {from_number_cleaned}, body: '{message_body}'")

    # 3. Identificar a qué cliente corresponde el número de destino (via Database)
    whatsapp_mapping = WhatsappNumero.query.filter_by(numero_whatsapp=to_number_cleaned, is_active=True).first()

    if not whatsapp_mapping:
        print(f"Error: WhatsApp number {to_number_cleaned} not found or inactive in database.")
        # Optional: Notify admin about unknown number
        # send_admin_notification(f"Unknown WhatsApp number received: {to_number_cleaned}")
        return "WhatsApp number not configured for any client.", 404

    # Retrieve associated User (company/municipality)
    client_user = whatsapp_mapping.user
    if not client_user:
        # This case should ideally not happen if DB constraints are set up correctly (user_id is not nullable)
        print(f"Error: No user associated with WhatsappNumero id {whatsapp_mapping.id} for number {to_number_cleaned}.")
        return "Internal configuration error: WhatsApp number mapped to non-existent user.", 500

    empresa_id = client_user.id # This is the User.id of the company/municipality
    client_name = client_user.nombre_empresa or client_user.name
    client_type = client_user.tipo_chat # e.g., 'pyme', 'municipio'

    print(f"Mensaje para cliente: {client_name} (ID: {empresa_id}, Tipo: {client_type})")

    # 4. Manejo de sesión/contexto por usuario (STUBBED)
    user_session_id = f"{empresa_id}_{from_number_cleaned}"

    # STUBBED IMPLEMENTATION for session retrieval:
    def recuperar_sesion(session_id):
        print(f"STUB: Intentando recuperar sesión para {session_id}")
        return {"historial_chat": [], "estado_conversacion": "inicio"}

    # STUBBED IMPLEMENTATION for session saving:
    def guardar_sesion(session_id, data):
        print(f"STUB: Guardando sesión para {session_id} con datos: {data}")
        pass

    session_data = recuperar_sesion(user_session_id)
    print(f"STUB: Sesión recuperada para {user_session_id}: {session_data}")

    # 5. Llamar a la lógica del bot (STUBBED)
    # Pass empresa_id (User.id), client_name, client_type if your bot logic needs them
    def responder_chatboc(e_id, u_number, msg, sess, c_name=None, c_type=None):
        print(f"STUB: Llamando a responder_chatboc para empresa ID {e_id} ({c_name}/{c_type}), usuario {u_number}, mensaje '{msg}'")
        respuesta_bot = f"Eco de {c_name} (ID: {e_id}, Tipo: {c_type}): '{msg}'. Estado de sesión: {sess.get('estado_conversacion')}"
        sess["historial_chat"].append({"usuario": msg, "bot": respuesta_bot})
        sess["estado_conversacion"] = "continuando"
        return respuesta_bot, sess

    respuesta_del_bot, session_data_actualizada = responder_chatboc(
        e_id=empresa_id,
        u_number=from_number_cleaned,
        msg=message_body,
        sess=session_data,
        c_name=client_name,
        c_type=client_type
    )
    print(f"STUB: Respuesta del bot: '{respuesta_del_bot}', Sesión actualizada: {session_data_actualizada}")

    # 6. Guardar el contexto actualizado (STUBBED)
    guardar_sesion(user_session_id, session_data_actualizada)

    # 7. Enviar respuesta por WhatsApp
    if twilio_client:
        try:
            message = twilio_client.messages.create(
                from_=to_number_raw, # Use the raw number with "whatsapp:" prefix for Twilio
                to=from_number_raw,   # Use the raw number with "whatsapp:" prefix for Twilio
                body=respuesta_del_bot
            )
            print(f"Mensaje de respuesta enviado a {from_number_raw}, SID: {message.sid}")
        except Exception as e:
            print(f"Error al enviar mensaje de Twilio: {e}")
    else:
        print("Warning: Twilio client no inicializado. No se puede enviar respuesta por WhatsApp.")

    return "OK", 200
