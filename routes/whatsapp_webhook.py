from flask import Blueprint, request, jsonify, abort # Basic Flask components
from twilio.request_validator import RequestValidator # For validating Twilio requests
from twilio.rest import Client # For sending messages via Twilio
import os # For accessing environment variables
from models import WhatsappNumero, User, ChatSessionContext # Import necessary models
from extensions import db # Import db instance for database operations
import uuid
# Import the main chatbot logic function used in the rest of the app
from services.logic import responder_chatboc

# Define the blueprint for WhatsApp webhooks
webhook_bp = Blueprint('whatsapp_webhook', __name__)

# Load environment variables for Twilio credentials
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")

# Initialize Twilio client and request validator
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    validator = RequestValidator(TWILIO_AUTH_TOKEN)
else:
    print("Warning: TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN environment variables not set. Twilio client and validator will not be initialized.")
    twilio_client = None
    validator = None

@webhook_bp.route("/webhook/whatsapp", methods=["POST"])
def whatsapp_webhook():
    if not validator:
        print("Error: Twilio RequestValidator not initialized. Ensure TWILIO_AUTH_TOKEN is set.")
        abort(500, "Twilio validator not configured")

    signature = request.headers.get("X-Twilio-Signature", "")
    url = request.url
    post_vars = request.form.to_dict()

    if not validator.validate(url, post_vars, signature):
        abort(403, "Invalid Twilio signature")

    to_number_raw = post_vars.get("To", "")
    from_number_raw = post_vars.get("From", "")
    message_body = post_vars.get("Body", "")

    to_number_cleaned = to_number_raw.replace("whatsapp:", "")
    from_number_cleaned = from_number_raw.replace("whatsapp:", "") # User's phone number

    print(f"Received WhatsApp message to: {to_number_cleaned}, from: {from_number_cleaned}, body: '{message_body}'")

    whatsapp_mapping = WhatsappNumero.query.filter_by(numero_whatsapp=to_number_cleaned, is_active=True).first()

    if not whatsapp_mapping:
        print(f"Error: WhatsApp number {to_number_cleaned} not found or inactive in database.")
        return "WhatsApp number not configured for any client.", 404

    client_user = whatsapp_mapping.user
    if not client_user:
        print(f"Error: No user associated with WhatsappNumero id {whatsapp_mapping.id} for number {to_number_cleaned}.")
        return "Internal configuration error: WhatsApp number mapped to non-existent user.", 500

    empresa_id = client_user.id
    client_name = client_user.nombre_empresa or client_user.name
    client_type = client_user.tipo_chat

    print(f"Mensaje para cliente: {client_name} (ID: {empresa_id}, Tipo: {client_type})")

    # --- Real Session Management using ChatSessionContext ---
    chat_session_id_internal = f"whatsapp_{empresa_id}_{from_number_cleaned}"
    session_context_db_entry = ChatSessionContext.query.filter_by(chat_session_id=chat_session_id_internal).first()

    current_session_data = {}  # This will be passed to and updated by responder_chatboc

    if session_context_db_entry:
        current_session_data = session_context_db_entry.context_data or {}
        # Ensure essential keys are present if loading an existing session
        current_session_data.setdefault("historial_chat", [])
        current_session_data.setdefault("estado_conversacion", "continuando") # Or derive from actual context
        print(f"Session found for {chat_session_id_internal}. Context: {current_session_data}")
    else:
        # For a new session, responder_chatboc will likely initialize the context_data structure
        # We still need to create the ChatSessionContext DB entry.
        current_session_data = {  # Minimal initial context if responder_chatboc doesn't create it entirely
            "historial_chat": [],
            "estado_conversacion": "inicio",
            "user_id_empresa": empresa_id,
            "telefono_usuario": from_number_cleaned,
            "canal_origen": "whatsapp"
        }
        session_context_db_entry = ChatSessionContext(
            chat_session_id=chat_session_id_internal,
            user_id=empresa_id,
            anon_id=from_number_cleaned,
            context_data=current_session_data # Initial context
        )
        db.session.add(session_context_db_entry)
        print(f"New session DB entry prepared for {chat_session_id_internal}.")
        # Note: responder_chatboc should ideally update the ChatSessionContext object in place.

    # --- Call Real Chatbot Logic ---
    respuesta_del_bot_text = "Lo siento, no pude procesar tu solicitud en este momento."  # Default error

    try:
        print(
            f"Calling responder_chatboc for session_id: {chat_session_id_internal}, owner_user_id: {empresa_id}"
        )

        raw_response = responder_chatboc(
            pregunta=message_body,
            owner_user=client_user,
            current_user=None,
            rubro_obj=None,
            chat_db_context=session_context_db_entry,
            rubro_nombre_frontend=None,
            tipo_chat=client_type,
            anon_id=from_number_cleaned,
            chat_session_uuid=chat_session_id_internal,
        )

        print(f"Raw response from responder_chatboc: {raw_response}")

        if isinstance(raw_response, dict):
            respuesta_del_bot_text = raw_response.get("respuesta", respuesta_del_bot_text)
        elif isinstance(raw_response, str):
            respuesta_del_bot_text = raw_response
        else:
            print(
                "Warning: responder_chatboc returned unexpected type; default response will be used"
            )

        print(f"Bot response text: '{respuesta_del_bot_text}'")

    except Exception as e:
        print(f"Error calling real chatbot logic (responder_chatboc): {e}")

    # --- Save Updated Session ---
    try:
        session_context_db_entry.last_updated = db.func.now()
        db.session.commit()
        print(f"Session saved for {chat_session_id_internal}.")
    except Exception as e:
        db.session.rollback()
        print(f"Error saving session for {chat_session_id_internal}: {e}")
        # If session saving fails, the user might get inconsistent state on next message.
        # The current response (respuesta_del_bot_text) will still be sent.

    # --- Send Response via Twilio ---
    if twilio_client:
        try:
            message = twilio_client.messages.create(
                from_=to_number_raw,
                to=from_number_raw,
                body=respuesta_del_bot_text # Use the response from real bot logic
            )
            print(f"Mensaje de respuesta enviado a {from_number_raw}, SID: {message.sid}")
        except Exception as e:
            print(f"Error al enviar mensaje de Twilio: {e}")
    else:
        print("Warning: Twilio client no inicializado. No se puede enviar respuesta por WhatsApp.")

    return "OK", 200
