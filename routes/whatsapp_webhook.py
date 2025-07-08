from flask import Blueprint, request, jsonify, abort # Basic Flask components
from twilio.request_validator import RequestValidator # For validating Twilio requests
from twilio.rest import Client # For sending messages via Twilio
import os # For accessing environment variables
from models import WhatsappNumero, User, ChatSessionContext # Import necessary models
from extensions import db # Import db instance for database operations
import uuid
from services.logic import processar_interacao # Import the real chatbot logic processor

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

    current_session_data = {} # This will be passed to and updated by processar_interacao

    if session_context_db_entry:
        current_session_data = session_context_db_entry.context_data or {}
        # Ensure essential keys are present if loading an existing session
        current_session_data.setdefault("historial_chat", [])
        current_session_data.setdefault("estado_conversacion", "continuando") # Or derive from actual context
        print(f"Session found for {chat_session_id_internal}. Context: {current_session_data}")
    else:
        # For a new session, processar_interacao will likely initialize the context_data structure
        # We still need to create the ChatSessionContext DB entry.
        current_session_data = { # Minimal initial context if processar_interacao doesn't create it entirely
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
        # Note: processar_interacao should ideally return the full session data to be saved.

    # --- Call Real Chatbot Logic ---
    respuesta_del_bot_text = "Lo siento, no pude procesar tu solicitud en este momento." # Default error response
    session_data_actualizada = current_session_data # Default to current if error

    try:
        # Prepare parameters for processar_interacao
        datos_usuario_param = {
            "numero_whatsapp": from_number_cleaned,
            "source_channel": "whatsapp",
            "nombre_usuario_display": from_number_cleaned # Could be enhanced later if user name is known
        }
        metadata_chat_param = {
            "client_name": client_name,
            "client_type": client_type,
            # Pass the whole current_session_data as part of metadata if processar_interacao expects it there
            # or if it primarily works by mutating a passed session object.
            # Adjust based on processar_interacao's design.
            "current_context": current_session_data
        }

        print(f"Calling processar_interacao for session_id: {chat_session_id_internal}, owner_user_id: {empresa_id}")

        # Assuming processar_interacao takes current_session_data and mutates it or returns a new one
        # and returns a dictionary like {'respuesta': 'text', 'contexto_chat': session_dict}
        # The exact signature and data flow of processar_interacao is critical here.
        # For now, let's assume it might take the session data as part of metadata_chat or a separate param.
        # If processar_interacao expects to receive and return the full session object:
        raw_response = processar_interacao(
            owner_user_id=empresa_id,
            session_id=chat_session_id_internal, # Crucial for linking logs and context
            texto_mensaje=message_body,
            datos_usuario=datos_usuario_param,
            metadata_chat=metadata_chat_param, # Passing current_session_data within metadata
            # Pass current_session_data directly if that's the expected API for processar_interacao:
            # current_chat_context=current_session_data
        )

        print(f"Raw response from processar_interacao: {raw_response}")

        if isinstance(raw_response, dict):
            respuesta_del_bot_text = raw_response.get("respuesta", respuesta_del_bot_text)
            # processar_interacao should return the complete, updated session context
            session_data_actualizada = raw_response.get("contexto_chat", current_session_data)
        elif isinstance(raw_response, str): # If it only returns the text response
            respuesta_del_bot_text = raw_response
            # In this case, session_data_actualizada would rely on mutations if current_session_data was passed by reference
            # or we'd need another way to get the updated session. This is less ideal.
            # For robustness, ensure 'contexto_chat' is returned by processar_interacao.
            print("Warning: processar_interacao returned a string. Assuming session data needs to be handled from input or is not updated by this function call directly for output.")

        print(f"Bot response: '{respuesta_del_bot_text}', Updated session: {session_data_actualizada}")

    except Exception as e:
        print(f"Error calling real chatbot logic (processar_interacao): {e}")
        # Keep default error response and current session data
        # Potentially log the stack trace: import traceback; traceback.print_exc()

    # --- Save Updated Session ---
    try:
        session_context_db_entry.context_data = session_data_actualizada
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
