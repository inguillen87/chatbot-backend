from flask import Blueprint, request, jsonify, abort, current_app  # Basic Flask components
from twilio.request_validator import RequestValidator  # For validating Twilio requests
from twilio.rest import Client  # For sending messages via Twilio
import os  # For accessing environment variables
import requests
import io
import json
from werkzeug.datastructures import FileStorage
from models import WhatsappNumero, User, ChatSessionContext, ArchivoAdjunto  # Import necessary models
from extensions import db  # Import db instance for database operations
import uuid
from services.logic import responder_chatboc  # Import the correct chatbot logic processor
from sqlalchemy.orm import joinedload  # To potentially eager load User.rubro
from sqlalchemy.orm.attributes import flag_modified
from services.notifications import enviar_bienvenida_whatsapp
from services.gcs_service import upload_to_gcs # Import the GCS service
from services.llm_utils import extract_multiple_contact_details_llm
from services.user_service import update_user_profile

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
    print(f"Request form: {request.form}")
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
    to_number_cleaned = to_number_raw.replace("whatsapp:", "")
    from_number_cleaned = from_number_raw.replace("whatsapp:", "")

    # --- Session Management FIRST ---
    whatsapp_mapping = WhatsappNumero.query.options(
        joinedload(WhatsappNumero.user).joinedload(User.rubro)
    ).filter_by(numero_whatsapp=to_number_cleaned, is_active=True).first()

    if not whatsapp_mapping:
        print(f"Error: WhatsApp number {to_number_cleaned} not found or inactive in database.")
        return "WhatsApp number not configured for any client.", 404

    client_user = whatsapp_mapping.user
    if not client_user:
        print(f"Error: No user associated with WhatsappNumero id {whatsapp_mapping.id} for number {to_number_cleaned}.")
        return "Internal configuration error: WhatsApp number mapped to non-existent user.", 500

    empresa_id = client_user.id
    from services.pymes import get_or_create_user_by_phone
    end_user = get_or_create_user_by_phone(from_number_cleaned, client_user)

    chat_session_id_internal = f"whatsapp_{empresa_id}_{from_number_cleaned}"
    session_context_db_entry = ChatSessionContext.query.filter_by(chat_session_id=chat_session_id_internal).first()

    if not session_context_db_entry:
        initial_session_data = {
            "historial_chat": [], "estado_conversacion": "inicio",
            "user_id_empresa": empresa_id, "telefono_usuario": from_number_cleaned,
            "canal_origen": "whatsapp",
            "mensajes_previos_gemini_formato": []
        }
        session_context_db_entry = ChatSessionContext(
            chat_session_id=chat_session_id_internal, user_id=empresa_id,
            anon_id=from_number_cleaned, context_data=initial_session_data
        )
        db.session.add(session_context_db_entry)
        print(f"New session DB entry prepared for {chat_session_id_internal}.")
        try:
            nombre_destino = getattr(end_user, "name", "") or ""
            enviar_bienvenida_whatsapp(from_number_cleaned, nombre_destino)
        except Exception as e:
            print(f"Error sending welcome template: {e}")

    # Ensure context_data is a dict
    if not isinstance(session_context_db_entry.context_data, dict):
        session_context_db_entry.context_data = {}

    # Determine incoming text before any special handling
    button_payload = post_vars.get("ButtonPayload")
    list_id = post_vars.get("ListId")
    incoming_text = button_payload or list_id or post_vars.get("Body", "")

    # --- Profile confirmation flow ---
    if not session_context_db_entry.context_data.get("perfil_confirmado"):
        perfil = session_context_db_entry.context_data.get("perfil_en_revision") or {
            "nombre": getattr(end_user, "name", ""),
            "dni": session_context_db_entry.context_data.get("dni", ""),
            "email": getattr(end_user, "email", ""),
            "telefono": getattr(end_user, "telefono", from_number_cleaned),
        }

        if session_context_db_entry.context_data.get("estado_conversacion") == "esperando_confirmacion_perfil":
            texto = incoming_text.strip()
            updated = False
            if texto.lower() not in ["si", "sí", "s", "ok", "correcto"]:
                nuevos = extract_multiple_contact_details_llm(texto)
                for key in ["nombre", "dni", "email", "telefono"]:
                    if nuevos.get(key):
                        perfil[key] = nuevos[key]
                        updated = True
                update_payload = {}
                if perfil.get("nombre"):
                    update_payload["name"] = perfil["nombre"]
                if perfil.get("telefono"):
                    update_payload["telefono"] = perfil["telefono"]
                if perfil.get("email"):
                    update_payload["email"] = perfil["email"]
                if update_payload:
                    update_user_profile(end_user, update_payload)
                if perfil.get("dni"):
                    session_context_db_entry.context_data["dni"] = perfil["dni"]

            resumen = []
            if perfil.get("nombre"): resumen.append(f"Nombre: {perfil['nombre']}")
            if perfil.get("dni"): resumen.append(f"DNI: {perfil['dni']}")
            if perfil.get("email"): resumen.append(f"Email: {perfil['email']}")
            if perfil.get("telefono"): resumen.append(f"Teléfono: {perfil['telefono']}")

            session_context_db_entry.context_data["perfil_confirmado"] = True
            session_context_db_entry.context_data["estado_conversacion"] = "activo"
            session_context_db_entry.context_data["perfil_en_revision"] = perfil
            flag_modified(session_context_db_entry, "context_data")
            db.session.add(session_context_db_entry)
            db.session.commit()

            if twilio_client:
                twilio_client.messages.create(
                    from_=to_number_raw,
                    to=from_number_raw,
                    body="Gracias, tus datos fueron " + ("actualizados" if updated else "confirmados") + ".\n" + "\n".join(resumen)
                )
            return "OK", 200
        else:
            resumen = []
            if perfil.get("nombre"): resumen.append(f"Nombre: {perfil['nombre']}")
            if perfil.get("dni"): resumen.append(f"DNI: {perfil['dni']}")
            if perfil.get("email"): resumen.append(f"Email: {perfil['email']}")
            if perfil.get("telefono"): resumen.append(f"Teléfono: {perfil['telefono']}")
            mensaje = (
                "Estos son los datos que tengo registrados:\n" +
                "\n".join(resumen) +
                "\n¿Son correctos? Responde 'sí' para confirmar o envía los correctos."
            )
            session_context_db_entry.context_data["estado_conversacion"] = "esperando_confirmacion_perfil"
            session_context_db_entry.context_data["perfil_en_revision"] = perfil
            flag_modified(session_context_db_entry, "context_data")
            db.session.add(session_context_db_entry)
            db.session.commit()

            if twilio_client:
                twilio_client.messages.create(
                    from_=to_number_raw,
                    to=from_number_raw,
                    body=mensaje
                )
            return "OK", 200

    # --- Message and Media Handling SECOND ---
    media_url = post_vars.get("MediaUrl0")
    media_content_type = post_vars.get("MediaContentType0")
    uploaded_file_info = None
    message_body = incoming_text

    if media_url and media_content_type:
        if media_content_type.startswith("audio/"):
            session_context_db_entry.context_data['source_is_audio'] = True
            from services.audio_transcription_service import transcribe_audio_from_url
            transcribed_text = transcribe_audio_from_url(media_url, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            if transcribed_text:
                message_body = transcribed_text
            else:
                print("Audio transcription failed or returned empty.")
        else:
            session_context_db_entry.context_data.pop('source_is_audio', None)

        file_extension = ".bin"
        if media_content_type.startswith("image/"): file_extension = ".jpg"
        elif media_content_type.startswith("audio/"): file_extension = ".ogg"

        file_name = f"whatsapp_file_{uuid.uuid4().hex[:8]}{file_extension}"

        try:
            # Download the file from Twilio's URL
            auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            r = requests.get(media_url, auth=auth)
            r.raise_for_status()

            # Create an in-memory file-like object
            file_stream = io.BytesIO(r.content)

            # Wrap it in a FileStorage object to be compatible with our service
            file_storage = FileStorage(
                stream=file_stream,
                filename=file_name,
                content_type=media_content_type
            )

            # Upload to GCS
            upload_result = upload_to_gcs(file_storage)

            if upload_result:
                # Create ArchivoAdjunto record
                nuevo_adjunto = ArchivoAdjunto(
                    user_id=end_user.id if end_user else None,
                    session_id=chat_session_id_internal,
                    filename=upload_result['unique_name'],
                    nombre_original=upload_result['original_name'],
                    mime=upload_result['mimetype'],
                    tamano=upload_result['size'],
                    tipo='whatsapp_adjunto',
                    url=upload_result['public_url']
                )
                db.session.add(nuevo_adjunto)
                db.session.commit()

                uploaded_file_info = {
                    "id": nuevo_adjunto.id,
                    "url": nuevo_adjunto.url,
                    "mime_type": nuevo_adjunto.mime,
                    "name": nuevo_adjunto.nombre_original,
                    "source": "whatsapp"
                }
                if media_content_type.startswith("audio/") and message_body:
                    uploaded_file_info["transcribed_text"] = message_body
                post_vars["uploaded_file_info"] = uploaded_file_info
                current_app.logger.info(f"WhatsApp media saved as ArchivoAdjunto ID: {nuevo_adjunto.id}")

            else:
                current_app.logger.error(f"Failed to upload WhatsApp media to GCS from URL: {media_url}")

        except requests.exceptions.RequestException as e:
            current_app.logger.error(f"Error downloading media from Twilio URL {media_url}: {e}")
        except Exception as e:
            current_app.logger.error(f"Error processing WhatsApp media file: {e}", exc_info=True)
            db.session.rollback()

    else:
        session_context_db_entry.context_data.pop('source_is_audio', None)

    # --- Location Handling ---
    latitude = post_vars.get("Latitude")
    longitude = post_vars.get("Longitude")
    location_info = None
    if latitude and longitude:
        location_info = {"latitude": latitude, "longitude": longitude}
        address = post_vars.get("Address")
        label = post_vars.get("Label")
        if address:
            location_info["address"] = address
        if label:
            location_info["label"] = label
        print(f"Received location data: {location_info}")

    # --- Human Chat Check ---
    if session_context_db_entry.context_data.get("human_chat_in_progress"):
        room = session_context_db_entry.context_data.get("room")
        if room:
            from socket_service import socketio
            socketio.emit('message', {'msg': message_body}, room=room)
            return "OK", 200

    # --- Call Real Chatbot Logic: responder_chatboc ---
    # Initialize with a default error response
    bot_response_dict = {
        'message_body': "Lo siento, no pude procesar tu solicitud en este momento.",
        'options_list': [],
        'message_type': 'text',
        'fuente': 'error_handler_whatsapp'
    }
    respuesta_del_bot_text = bot_response_dict['message_body']

    # The context_data from session_context_db_entry will be passed to responder_chatboc
    # and it's expected that responder_chatboc might modify it directly or return a new context.

    try:
        print(f"Calling responder_chatboc for session_id: {chat_session_id_internal}, owner_user: {client_user.name}")

        interpretacion_media_data = None
        if uploaded_file_info:
            interpretacion_media_data = clasificar_adjunto_whatsapp(uploaded_file_info, client_user)
        elif location_info:
            interpretacion_media_data = {
                "categoria_sugerida": "ubicacion",
                "ubicacion": location_info,
            }

        kwargs_for_bot = {"source_channel": "whatsapp"}
        if uploaded_file_info:
            kwargs_for_bot["uploaded_file_info"] = uploaded_file_info
        if location_info:
            kwargs_for_bot["location_info"] = location_info
        if interpretacion_media_data and not interpretacion_media_data.get("error"):
            kwargs_for_bot["interpretacion_imagen_data"] = interpretacion_media_data

        profile_name = post_vars.get("ProfileName")
        if profile_name:
            kwargs_for_bot["profile_name"] = profile_name

        # The actual call that might raise an exception
        bot_response_dict = responder_chatboc(
            pregunta=message_body,
            owner_user=client_user,
            current_user=end_user,
            rubro_obj=client_user.rubro,
            chat_db_context=session_context_db_entry,
            rubro_nombre_frontend=None,
            tipo_chat=client_user.tipo_chat,
            anon_id=from_number_cleaned,
            chat_session_uuid=chat_session_id_internal,
            channel="whatsapp",
            **kwargs_for_bot
        )

        # Si el usuario es anónimo y la acción requiere datos personales, pedirlos
        if not end_user and bot_response_dict.get("accion_backend") in ["crear_reclamo", "iniciar_reclamo"]:
            contexto_actual = session_context_db_entry.context_data.get("contexto_municipio", {})
            datos_reclamo = contexto_actual.get("datos_parciales_llm_reclamo", {})

            # Extraer info del mensaje actual del usuario
            extracted_data = extract_multiple_contact_details_llm(message_body)

            # Actualizar datos del reclamo con la info extraída
            if extracted_data.get("nombre"):
                datos_reclamo["nombre_usuario_detectado"] = extracted_data["nombre"]
            if extracted_data.get("telefono"):
                datos_reclamo["telefono_detectado"] = extracted_data["telefono"]
            if extracted_data.get("email"):
                datos_reclamo["email_detectado"] = extracted_data["email"]

            # Guardar datos actualizados en el contexto
            contexto_actual["datos_parciales_llm_reclamo"] = datos_reclamo
            session_context_db_entry.context_data["contexto_municipio"] = contexto_actual

            # Verificar si ya tenemos toda la info
            if not (datos_reclamo.get("nombre_usuario_detectado") and datos_reclamo.get("telefono_detectado") and datos_reclamo.get("email_detectado")):
                # Si falta info, volver a pedirla
                bot_response_dict = {
                    "message_body": "Para poder registrar tu reclamo, necesito que me indiques tu nombre, tu número de teléfono y tu correo electrónico.",
                    "pedir_info": ["nombre", "telefono", "email"]
                }

        print(f"Raw response from responder_chatboc: {bot_response_dict}")

        # Validate the response from the bot logic
        if not isinstance(bot_response_dict, dict):
            print(f"Warning: responder_chatboc did not return a dictionary. Response: {bot_response_dict}")
            # Keep the default error response initialized earlier
            bot_response_dict = {
                'message_body': "Lo siento, hubo un error interno al procesar tu mensaje.",
                'options_list': [], 'message_type': 'text', 'fuente': 'error_handler_non_dict_response'
            }

        # Ensure context_data is a dict for saving
        if not isinstance(session_context_db_entry.context_data, dict):
            print(f"Warning: context_data in session_context_db_entry is not a dict. Resetting. Data: {session_context_db_entry.context_data}")
            session_context_db_entry.context_data = {
                "historial_chat": [{"role": "system", "content": "Context was reset due to invalid format."}],
                "estado_conversacion": "error_context"
            }

    except Exception as e:
        print(f"Error calling real chatbot logic (responder_chatboc): {e}")
        import traceback
        traceback.print_exc() # Log full traceback for debugging
        # bot_response_dict is already set to a default error message, so we just log and continue

    # Update respuesta_del_bot_text for logging from the final bot_response_dict
    respuesta_del_bot_text = bot_response_dict.get('message_body', "Error: message_body no encontrado en la respuesta del bot.")
    print(f"Bot response text for logging: '{respuesta_del_bot_text}', Session context to save: {session_context_db_entry.context_data}")

    # --- Format Response and Save Session ---
    formatted_whatsapp_payload = {}
    try:
        from services.response_formatter import build_interactive_response

        body_text = bot_response_dict.get('message_body') or bot_response_dict.get('message_to_user', "Error de formato.")

        # This call will modify bot_response_dict to include context for the numeric menu
        formatted_whatsapp_payload = build_interactive_response(
            options=bot_response_dict.get('options_list', []),
            body_text=body_text,
            channel='whatsapp',
            message_type=bot_response_dict.get('message_type', 'text'),
            original_bot_response=bot_response_dict,
            header_text=bot_response_dict.get('header_text'),
            footer_text=bot_response_dict.get('footer_text'),
            audio_url=bot_response_dict.get('audio_url')
        )

        # After formatting, the context might be updated (e.g., with last_options_sent).
        # We need to merge this updated context back into our main session object before saving.
        updated_context = formatted_whatsapp_payload.get('contexto_actualizado')

        # The existing context from the database
        db_context = session_context_db_entry.context_data or {}
        current_app.logger.info(f"[CONTEXT_WHATSAPP] Contexto de la base de datos: {db_context}")
        current_app.logger.info(f"[CONTEXT_WHATSAPP] Contexto actualizado del turno actual: {updated_context}")


        # Merge the contexts
        if updated_context:
            merged_context = {**db_context, **updated_context}
        else:
            merged_context = db_context

        current_app.logger.info(f"[CONTEXT_WHATSAPP] Contexto fusionado para guardar: {merged_context}")


        # Save the merged context
        session_context_db_entry.context_data = merged_context
        flag_modified(session_context_db_entry, "context_data")
        db.session.add(session_context_db_entry)
        db.session.commit()
        print(f"Session saved for {chat_session_id_internal}. Context: {session_context_db_entry.context_data}")

    except Exception as e:
        db.session.rollback()
        print(f"Error formatting response or saving session for {chat_session_id_internal}: {e}")
        import traceback
        traceback.print_exc()

    # --- Send Response via Twilio ---
    if twilio_client:
        try:
            # El formateador ahora devuelve un diccionario con 'type' y los datos.
            # Si es de tipo 'text', usamos el cuerpo directamente.
            message_params = {
                'from_': to_number_raw,
                'to': from_number_raw,
            }

            if formatted_whatsapp_payload.get("type") == "interactive":
                interactive_payload = formatted_whatsapp_payload.get("interactive")
                # The body is required, it's the fallback for notifications and older clients
                message_params['body'] = interactive_payload.get("body", {}).get("text", "Por favor, mirá las opciones.")
                # The PersistentAction is what actually sends the interactive message
                # It needs to be a list of strings, with the format "channel:payload"
                # For WhatsApp, the payload is a JSON string of the interactive object.
                message_params['persistent_action'] = [f"whatsapp:{json.dumps(interactive_payload)}"]
            else: # Text message
                message_params['body'] = formatted_whatsapp_payload.get("text", {}).get("body", "No se pudo generar una respuesta.")
            current_app.logger.debug(f"Sending WhatsApp message params: {message_params}")

            # Send the main message (text or interactive)
            main_message = twilio_client.messages.create(**message_params)
            print(f"Mensaje principal enviado a {from_number_raw}, SID: {main_message.sid}")

            # Second, if there is an audio URL, send it as a separate media message.
            audio_url = bot_response_dict.get('audio_url')
            if audio_url:
                # Ensure the URL is absolute
                if audio_url.startswith('/'):
                    base_url = request.url_root.rstrip('/')
                    absolute_audio_url = f"{base_url}{audio_url}"
                else:
                    absolute_audio_url = audio_url

                audio_message_params = {
                    'from_': to_number_raw,
                    'to': from_number_raw,
                    'media_url': [absolute_audio_url]
                }
                current_app.logger.debug(f"Sending WhatsApp audio params: {audio_message_params}")
                audio_message = twilio_client.messages.create(**audio_message_params)
                print(f"Mensaje de audio enviado a {from_number_raw}, SID: {audio_message.sid}")

        except Exception as e:
            print(f"Error al enviar mensaje de Twilio: {e}")
            import traceback
            traceback.print_exc()
    else:
        print("Warning: Twilio client no inicializado. No se puede enviar respuesta por WhatsApp.")

    return "OK", 200
