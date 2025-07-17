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
    print("Whatsapp webhook called!")
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

    # Check for interactive message replies from Twilio
    button_payload = post_vars.get("ButtonPayload") # For Button Reply ID
    list_id = post_vars.get("ListId") # For List Reply ID

    if button_payload:
        message_body = button_payload # Use the ID from the button
        print(f"Received WhatsApp Button Reply. Using ID: '{button_payload}' as message_body.")
    elif list_id:
        message_body = list_id # Use the ID from the list item
        print(f"Received WhatsApp List Reply. Using ID: '{list_id}' as message_body.")
    else:
        message_body = post_vars.get("Body", "") # Standard text message
        print(f"Received WhatsApp standard text message. Body: '{message_body}'")

    # --- Manejo de Archivos Adjuntos de WhatsApp ---
    media_url = post_vars.get("MediaUrl0")
    media_content_type = post_vars.get("MediaContentType0")
    uploaded_file_info_whatsapp = None

    if media_url and media_content_type:
        print(f"Received media from WhatsApp: URL='{media_url}', ContentType='{media_content_type}'")
        # Procesar imágenes y PDFs básicos.
        if media_content_type.startswith("image/"):
            uploaded_file_info_whatsapp = {"url": media_url, "mime_type": media_content_type, "name": f"whatsapp_image_{uuid.uuid4().hex[:8]}.jpg", "source": "whatsapp"}
            post_vars["uploaded_file_info_whatsapp"] = uploaded_file_info_whatsapp
            print(f"Prepared 'uploaded_file_info_whatsapp' for responder_chatboc: {uploaded_file_info_whatsapp}")
        elif media_content_type == "application/pdf":
            uploaded_file_info_whatsapp = {"url": media_url, "mime_type": media_content_type, "name": f"whatsapp_doc_{uuid.uuid4().hex[:8]}.pdf", "source": "whatsapp"}
            post_vars["uploaded_file_info_whatsapp"] = uploaded_file_info_whatsapp
            print(f"Prepared 'uploaded_file_info_whatsapp' for responder_chatboc: {uploaded_file_info_whatsapp}")
        else:
            print(f"Media type {media_content_type} from WhatsApp not currently processed for automatic analysis.")
    # --- Fin Manejo de Archivos Adjuntos de WhatsApp ---

    to_number_cleaned = to_number_raw.replace("whatsapp:", "")
    from_number_cleaned = from_number_raw.replace("whatsapp:", "") # User's phone number

    log_message_parts = [
        f"Received WhatsApp message to: {to_number_cleaned}",
        f"from: {from_number_cleaned}",
        f"body: '{message_body}'"
    ]
    if uploaded_file_info_whatsapp:
        log_message_parts.append(f"with media: {uploaded_file_info_whatsapp['mime_type']}")
    print(", ".join(log_message_parts))

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
        if uploaded_file_info_whatsapp:
            kwargs_for_bot["uploaded_file_info_whatsapp"] = uploaded_file_info_whatsapp

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
            channel="whatsapp", # Set channel to whatsapp
            **kwargs_for_bot
        )

        print(f"Raw response from responder_chatboc: {bot_response_dict}")

        if isinstance(bot_response_dict, dict):
            respuesta_del_bot_text = bot_response_dict.get("respuesta", respuesta_del_bot_text)

            # Update session_context_db_entry.context_data based on what responder_chatboc returns
            # If responder_chatboc directly modifies chat_db_context.context_data, this might not be strictly needed
            # but it's safer to explicitly set it if a specific context key is returned.

            # COMENTADO: responder_municipio (alias de responder_chatboc) ya modifica
            # session_context_db_entry.context_data directamente y lo serializa.
            # Esta reasignación aquí es redundante y potencialmente podría introducir errores
            # si la estructura de bot_response_dict o las claves de contexto cambian.
            # El contexto ya está correctamente serializado y actualizado en session_context_db_entry.context_data
            # por la llamada a responder_municipio.

            # if "contexto_chat" in bot_response_dict:
            #     session_context_db_entry.context_data = bot_response_dict["contexto_chat"]
            # elif client_type == "pyme" and "contexto_pyme" in bot_response_dict:
            #     session_context_db_entry.context_data = bot_response_dict["contexto_pyme"]
            # elif client_type == "municipio" and "contexto_municipio" in bot_response_dict:
            #     session_context_db_entry.context_data = bot_response_dict["contexto_municipio"]

            # If no specific context key is returned, we assume chat_db_context.context_data was modified in place.
            # Ensure it's a dict for saving. (This check is still valid)
            if not isinstance(session_context_db_entry.context_data, dict):
                print(f"Warning: context_data in session_context_db_entry is not a dict after responder_chatboc. Resetting to minimal error state. Data: {session_context_db_entry.context_data}")
                session_context_db_entry.context_data = {
                    "historial_chat": [{"role": "system", "content": "Context was reset due to invalid format from bot logic."}],
                    "estado_conversacion": "error_context"
                }

        else: # Should not happen if responder_chatboc always returns a dict
            print(f"Warning: responder_chatboc did not return a dictionary. Response: {bot_response_dict}")
            # respuesta_del_bot_text remains the default error message.
            # session_context_db_entry.context_data might be stale or un-updated, ensure it's at least a dict
            if not isinstance(session_context_db_entry.context_data, dict):
                 session_context_db_entry.context_data = {
                    "historial_chat": [{"role": "system", "content": "Context was reset due to invalid format from bot logic (non-dict response)."}],
                    "estado_conversacion": "error_context_non_dict"
                }


        # Actualizar respuesta_del_bot_text para el logging DESPUÉS de obtenerla de bot_response_dict
        if isinstance(bot_response_dict, dict) and "message_body" in bot_response_dict:
            respuesta_del_bot_text = bot_response_dict["message_body"]
        elif isinstance(bot_response_dict, dict) and "respuesta" in bot_response_dict: # Fallback por si acaso
            respuesta_del_bot_text = bot_response_dict["respuesta"]

        print(f"Bot response text for logging: '{respuesta_del_bot_text}', Session context to save: {session_context_db_entry.context_data}")

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
            from services.response_formatter import build_interactive_response
            import json

            # Use new structured keys from bot_response_dict
            body_for_formatter = bot_response_dict.get('message_body', respuesta_del_bot_text) # Fallback to old 'respuesta' if new key not present
            options_for_formatter = bot_response_dict.get('options_list', [])
            message_type_for_formatter = bot_response_dict.get('message_type', 'text')
            header_for_formatter = bot_response_dict.get('header_text')
            footer_for_formatter = bot_response_dict.get('footer_text')

            # Ensure respuesta_del_bot_text (used for logging) is also from the new key if available
            respuesta_del_bot_text = body_for_formatter

            # The formatter now returns the direct payload for the WhatsApp API.
            # No need to look for 'main_body' or 'interactive_object'.
            formatted_whatsapp_payload = build_interactive_response(
                options=options_for_formatter,
                body_text=body_for_formatter,
                channel='whatsapp',
                message_type=message_type_for_formatter,
                original_bot_response=bot_response_dict,
                header_text=header_for_formatter,
                footer_text=footer_for_formatter
            )

            # The `body` is the main text, used as fallback by Twilio if the rich message can't be delivered.
            message_params = {
                'from_': to_number_raw,
                'to': from_number_raw,
                'body': body_for_formatter,  # Fallback text
            }

            # For interactive messages, the actual content is sent via 'PersistentAction'
            # The payload for PersistentAction should be a JSON string of the interactive object.
            # The formatter now returns the full object including the "type": "interactive" wrapper.
            if formatted_whatsapp_payload.get("type") == "interactive":
                # The `PersistentAction` expects a list of strings, where each string is
                # 'channel:JSON_payload'.
                persistent_action_payload = f"whatsapp:{json.dumps(formatted_whatsapp_payload)}"
                message_params['persistent_action'] = [persistent_action_payload]
                print(f"Preparing to send WhatsApp interactive message with PersistentAction: {persistent_action_payload[:250]}...")
            elif formatted_whatsapp_payload.get("type") == "text":
                # For plain text, the body is already set and no PersistentAction is needed.
                # We just update the body to be sure it's from the formatted payload.
                message_params['body'] = formatted_whatsapp_payload.get("text", {}).get("body", body_for_formatter)
            else:
                # Fallback for any other message type, just send the plain text body.
                print(f"Formatted payload type is not 'interactive' or 'text', sending as plain text. Type: {formatted_whatsapp_payload.get('type')}")


            message = twilio_client.messages.create(**message_params)
            print(f"Mensaje de respuesta enviado a {from_number_raw}, SID: {message.sid}")
        except Exception as e:
            print(f"Error al enviar mensaje de Twilio: {e}")
            import traceback
            traceback.print_exc()
    else:
        print("Warning: Twilio client no inicializado. No se puede enviar respuesta por WhatsApp.")

    return "OK", 200
