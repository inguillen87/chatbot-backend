from flask import Blueprint, request, jsonify, abort # Basic Flask components
from twilio.request_validator import RequestValidator # For validating Twilio requests
from twilio.rest import Client # For sending messages via Twilio
import os # For accessing environment variables
from models import WhatsappNumero, User, ChatSessionContext # Import necessary models
from extensions import db # Import db instance for database operations
import uuid
from services.logic import responder_chatboc # Import the correct chatbot logic processor
from sqlalchemy.orm import joinedload # To potentially eager load User.rubro

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

    # Eager load the 'user' and 'user.rubro' relationships to avoid separate queries later
    whatsapp_mapping = WhatsappNumero.query.options(
        joinedload(WhatsappNumero.user).joinedload(User.rubro)
    ).filter_by(numero_whatsapp=to_number_cleaned, is_active=True).first()

    if not whatsapp_mapping:
        print(f"Error: WhatsApp number {to_number_cleaned} not found or inactive in database.")
        return "WhatsApp number not configured for any client.", 404

    client_user = whatsapp_mapping.user # This is the User object for the company/municipality
    if not client_user:
        print(f"Error: No user associated with WhatsappNumero id {whatsapp_mapping.id} for number {to_number_cleaned}.")
        return "Internal configuration error: WhatsApp number mapped to non-existent user.", 500

    empresa_id = client_user.id
    client_name = client_user.nombre_empresa or client_user.name
    client_type = client_user.tipo_chat
    rubro_object = client_user.rubro # Access the Rubro object

    print(f"Mensaje para cliente: {client_name} (ID: {empresa_id}, Tipo: {client_type}, Rubro: {rubro_object.nombre if rubro_object else 'N/A'})")

    # --- Real Session Management using ChatSessionContext ---
    chat_session_id_internal = f"whatsapp_{empresa_id}_{from_number_cleaned}"
    session_context_db_entry = ChatSessionContext.query.filter_by(chat_session_id=chat_session_id_internal).first()

    initial_session_data_for_new_session = {
        "historial_chat": [],
        "estado_conversacion": "inicio", # responder_chatboc will manage this
        "user_id_empresa": empresa_id,
        "telefono_usuario": from_number_cleaned,
        "canal_origen": "whatsapp"
    }

    if session_context_db_entry:
        # Ensure context_data is a dict; if None or invalid, start fresh for safety
        if not isinstance(session_context_db_entry.context_data, dict):
            session_context_db_entry.context_data = initial_session_data_for_new_session
        # Ensure essential keys are present
        session_context_db_entry.context_data.setdefault("historial_chat", [])
        session_context_db_entry.context_data.setdefault("estado_conversacion", "continuando")
        print(f"Session found for {chat_session_id_internal}. Context: {session_context_db_entry.context_data}")
    else:
        session_context_db_entry = ChatSessionContext(
            chat_session_id=chat_session_id_internal,
            user_id=empresa_id,
            anon_id=from_number_cleaned, # WhatsApp user's phone number as anon identifier
            context_data=initial_session_data_for_new_session
        )
        db.session.add(session_context_db_entry)
        print(f"New session DB entry prepared for {chat_session_id_internal}.")

    # --- Call Real Chatbot Logic: responder_chatboc ---
    respuesta_del_bot_text = "Lo siento, no pude procesar tu solicitud en este momento." # Default error response

    # The context_data from session_context_db_entry will be passed to responder_chatboc
    # and it's expected that responder_chatboc might modify it directly or return a new context.

    try:
        print(f"Calling responder_chatboc for session_id: {chat_session_id_internal}, owner_user: {client_user.name}")

        # Parameters for responder_chatboc:
        # pregunta, owner_user=None, current_user=None, rubro_obj=None,
        # chat_db_context=None, rubro_nombre_frontend=None, tipo_chat=None,
        # anon_id=None, chat_session_uuid=None, **kwargs

        kwargs_for_bot = {
            # Add any other specific kwargs your responder_chatboc might need from WhatsApp channel
            "source_channel": "whatsapp"
        }

        bot_response_dict = responder_chatboc(
            pregunta=message_body,
            owner_user=client_user,
            current_user=None, # For WhatsApp, end-user is typically not a full "User" record initially
            rubro_obj=rubro_object,
            chat_db_context=session_context_db_entry, # Pass the whole ChatSessionContext object
            rubro_nombre_frontend=None, # Typically from web UI, not WhatsApp
            tipo_chat=client_type,
            anon_id=from_number_cleaned,
            chat_session_uuid=chat_session_id_internal,
            **kwargs_for_bot
        )

        print(f"Raw response from responder_chatboc: {bot_response_dict}")

        if isinstance(bot_response_dict, dict):
            respuesta_del_bot_text = bot_response_dict.get("respuesta", respuesta_del_bot_text)

            # Update session_context_db_entry.context_data based on what responder_chatboc returns
            # If responder_chatboc directly modifies chat_db_context.context_data, this might not be strictly needed
            # but it's safer to explicitly set it if a specific context key is returned.
            if "contexto_chat" in bot_response_dict:
                session_context_db_entry.context_data = bot_response_dict["contexto_chat"]
            elif client_type == "pyme" and "contexto_pyme" in bot_response_dict:
                session_context_db_entry.context_data = bot_response_dict["contexto_pyme"]
            elif client_type == "municipio" and "contexto_municipio" in bot_response_dict:
                session_context_db_entry.context_data = bot_response_dict["contexto_municipio"]
            # If no specific context key is returned, we assume chat_db_context.context_data was modified in place.
            # Ensure it's a dict for saving.
            if not isinstance(session_context_db_entry.context_data, dict):
                print(f"Warning: context_data is not a dict after responder_chatboc. Resetting to minimal error state. Data: {session_context_db_entry.context_data}")
                session_context_db_entry.context_data = {
                    "historial_chat": [{"role": "system", "content": "Context was reset due to invalid format from bot logic."}],
                    "estado_conversacion": "error_context"
                }

        else: # Should not happen if responder_chatboc always returns a dict
            print(f"Warning: responder_chatboc did not return a dictionary. Response: {bot_response_dict}")
            # respuesta_del_bot_text remains the default error message.
            # session_context_db_entry.context_data might be stale or un-updated.

        print(f"Bot response text: '{respuesta_del_bot_text}', Session context to save: {session_context_db_entry.context_data}")

    except Exception as e:
        print(f"Error calling real chatbot logic (responder_chatboc): {e}")
        import traceback
        traceback.print_exc() # Log full traceback for debugging

    # --- Save Updated Session ---
    try:
        session_context_db_entry.last_updated = db.func.now()
        db.session.commit()
        print(f"Session saved for {chat_session_id_internal}.")
    except Exception as e:
        db.session.rollback()
        print(f"Error saving session for {chat_session_id_internal}: {e}")
        import traceback
        traceback.print_exc()

    # --- Send Response via Twilio ---
    if twilio_client:
        try:
            message = twilio_client.messages.create(
                from_=to_number_raw,
                to=from_number_raw,
                body=respuesta_del_bot_text
            )
            print(f"Mensaje de respuesta enviado a {from_number_raw}, SID: {message.sid}")
        except Exception as e:
            print(f"Error al enviar mensaje de Twilio: {e}")
    else:
        print("Warning: Twilio client no inicializado. No se puede enviar respuesta por WhatsApp.")

    return "OK", 200
