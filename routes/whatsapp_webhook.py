from flask import Blueprint, request, jsonify, abort, current_app, g  # Basic Flask components
from twilio.request_validator import RequestValidator  # For validating Twilio requests
from twilio.rest import Client  # For sending messages via Twilio
import os  # For accessing environment variables
import requests
import io
import json
import threading
from werkzeug.datastructures import FileStorage
from models import WhatsappNumero, User, ChatSessionContext, ArchivoAdjunto  # Import necessary models
from extensions import db  # Import db instance for database operations
import uuid
from services.logic import responder_chatboc  # Import the correct chatbot logic processor
from sqlalchemy.orm import joinedload  # To potentially eager load User.rubro
from utils.db_utils import safe_flag_modified
from services.gcs_service import upload_to_gcs
from services.attachment_service import create_attachment_with_thumbnail
from services.llm_utils import extract_multiple_contact_details_llm
from services.user_service import update_user_profile
from services.media_classifier import clasificar_adjunto_whatsapp
from utils.maps_utils import extraer_coordenadas_de_url_google_maps
from services.openai_maps_service import geocodificar_inversa_llm
from services.municipio_responder import CONTEXTO_MUNICIPIO

# Define the blueprint for WhatsApp webhooks
webhook_bp = Blueprint('whatsapp_webhook', __name__)

# Twilio imposes a 1600 character limit on message bodies. When the bot
# generates very long responses (e.g. large contact lists) the request can
# fail with `HTTP 400: The concatenated message body exceeds the 1600 character
# limit`.  To prevent this we define a helper that splits long texts into
# chunks that comply with Twilio's limits and send them sequentially.

MAX_TWILIO_BODY_LENGTH = 1600


def _split_message(text: str, limit: int = MAX_TWILIO_BODY_LENGTH) -> list[str]:
    """Split `text` into chunks no longer than `limit` characters.

    Preference is given to splitting on newlines or spaces to avoid breaking
    words when possible.
    """
    parts: list[str] = []
    while len(text) > limit:
        split_idx = text.rfind("\n", 0, limit)
        if split_idx == -1:
            split_idx = text.rfind(" ", 0, limit)
        if split_idx == -1:
            split_idx = limit
        parts.append(text[:split_idx])
        text = text[split_idx:].lstrip()
    parts.append(text)
    return parts


def _send_delayed_payload(client, to_number: str, from_number: str, payload: dict, delay: int, app):
    """Send a payload via WhatsApp after a delay using a background thread."""

    def _send():
        with app.app_context():
            from services.response_formatter import build_interactive_response

            formatted = build_interactive_response(
                options=payload.get("options_list", []),
                body_text=payload.get("message_body", ""),
                channel="whatsapp",
                message_type=payload.get("message_type", "text"),
                original_bot_response=payload,
            )

            params = {"from_": to_number, "to": from_number}
            if formatted.get("type") == "interactive":
                interactive = formatted.get("interactive")
                params["body"] = interactive.get("body", {}).get("text", "")
                params["persistent_action"] = [f"whatsapp:{json.dumps(interactive)}"]
            else:
                params["body"] = formatted.get("text", {}).get("body", "")

            image_url = formatted.get("image_url")
            if image_url and "persistent_action" not in params:
                params["media_url"] = [image_url]

            try:
                client.messages.create(**params)
            except Exception as e:
                app.logger.error(f"Error sending delayed message: {e}")

            audio_url = None
            audio_payload = formatted.get("audio") if isinstance(formatted, dict) else None
            if isinstance(audio_payload, dict):
                audio_url = audio_payload.get("link")
            if not audio_url:
                audio_url = payload.get("audio_url")

            if audio_url:
                absolute_audio_url = audio_url
                if audio_url.startswith("/"):
                    base_url = app.config.get("APP_BASE_URL")
                    if base_url:
                        absolute_audio_url = f"{base_url.rstrip('/')}{audio_url}"
                    else:
                        app.logger.warning(
                            "[WELCOME][DELAYED_AUDIO] APP_BASE_URL is not configured; sending relative audio URL."
                        )

                try:
                    client.messages.create(
                        from_=to_number,
                        to=from_number,
                        media_url=[absolute_audio_url],
                    )
                except Exception as e:
                    app.logger.error(f"Error sending delayed audio message: {e}")

    if client:
        timer = threading.Timer(delay, _send)
        timer.daemon = True
        timer.start()


def _esperando_info_libre(municipio_ctx: dict) -> bool:
    """True if any LLM prompt awaits free-form user input.

    Both generic conversation fields and claim-specific flows use different
    context keys when asking the user for additional information. This helper
    centralizes the check so numeric shortcuts and other automated handlers
    can pause while the bot waits for a free-form response.
    """

    return (
        municipio_ctx.get("esperando_info_llm")
        or municipio_ctx.get("esperando_info_llm_reclamo")
    )


def _merge_nested_dicts(base: dict, updates: dict) -> dict:
    """Return a shallow copy of ``base`` merged with ``updates`` recursively."""

    if not updates:
        return base

    merged = dict(base or {})
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_nested_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged

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

    # Store the owner entity on ``flask.g`` so downstream helpers (like the
    # storage fallback) know which empresa/municipio owns this conversation.
    g.owner_user = client_user

    empresa_id = client_user.id
    from services.pymes import get_or_create_user_by_phone
    end_user = get_or_create_user_by_phone(from_number_cleaned, client_user)

    chat_session_id_internal = f"whatsapp_{empresa_id}_{from_number_cleaned}"
    session_context_db_entry = ChatSessionContext.query.filter_by(chat_session_id=chat_session_id_internal).first()

    if not session_context_db_entry:
        initial_session_data = {
            "historial_chat": [],
            "estado_conversacion": "inicio",
            "user_id_empresa": empresa_id,
            "telefono_usuario": from_number_cleaned,
            "canal_origen": "whatsapp",
            "mensajes_previos_llm_formato": []
        }
        session_context_db_entry = ChatSessionContext(
            chat_session_id=chat_session_id_internal, user_id=empresa_id,
            anon_id=from_number_cleaned, context_data=initial_session_data
        )
        db.session.add(session_context_db_entry)
        db.session.commit()

    # Ensure context_data is a dict
    if not isinstance(session_context_db_entry.context_data, dict):
        session_context_db_entry.context_data = {}

    # --- Boti-style Welcome Message Branch ---
    from services.municipio_responder import normalizar_texto
    from datetime import datetime

    button_payload = post_vars.get("ButtonPayload")
    list_id = post_vars.get("ListId")
    incoming_text = button_payload or list_id or post_vars.get("Body", "")
    normalized_input = normalizar_texto(incoming_text.strip())

    GREETING_KEYWORDS = {"hola", "buenas", "buenos dias", "buenas tardes", "buenas noches"}
    OVERRIDE_KEYWORDS = {"menu", "menu principal", "reiniciar", "resetear", "volver", "cancelar", "terminar"}

    is_greeting = normalized_input in GREETING_KEYWORDS
    is_override = normalized_input in OVERRIDE_KEYWORDS

    municipio_ctx = session_context_db_entry.context_data.get(CONTEXTO_MUNICIPIO, {})
    is_waiting_for_info = _esperando_info_libre(municipio_ctx)

    # Cooldown logic
    now = datetime.now().timestamp()
    last_welcome_ts = session_context_db_entry.context_data.get("last_welcome_ts", 0)
    is_rate_limited = (now - last_welcome_ts) < 15

    should_trigger_welcome = is_override or (is_greeting and not is_waiting_for_info)

    if should_trigger_welcome and not is_rate_limited:
        current_app.logger.info(f"[WELCOME] Triggering Boti-style welcome for user {from_number_cleaned}. Reason: '{normalized_input}'.")

        session_context_db_entry.context_data["last_welcome_ts"] = now
        safe_flag_modified(session_context_db_entry, "context_data")
        db.session.commit()

        if twilio_client:
            try:
                template_sid = current_app.config.get("WELCOME_TEMPLATE_SID")
                # Prioritize DB name, then WhatsApp profile name. Avoid generic
                # "vecino" fallback so the bot either personalizes or greets
                # without a name and lets downstream logic ask for it.
                user_name = getattr(end_user, "name", "") or (post_vars.get("ProfileName") or "").strip()
                if user_name.lower() in {"vecino", "vecina", "vecino/a"}:
                    user_name = ""

                if template_sid:
                    params = {
                        "from_": to_number_raw,
                        "to": from_number_raw,
                        "content_sid": template_sid,
                        # Always supply the template variables. WhatsApp requires
                        # all placeholders to be populated, so an empty string is
                        # safer than omitting the field and triggering a 400.
                        "content_variables": json.dumps({"1": user_name or ""}),
                    }
                    twilio_client.messages.create(**params)
                    current_app.logger.info(
                        f"[WELCOME] Template {template_sid} sent to {from_number_cleaned} with name: {user_name or '<unknown>'}."
                    )

                greeting = (
                    f"*¡Hola, {user_name}!* Acá *Juni* \U0001F44B"
                    if user_name
                    else "*¡Hola!* Soy *Juni* \U0001F44B ¿Cómo te llamás?"
                )
                twilio_client.messages.create(
                    from_=to_number_raw, to=from_number_raw, body=greeting
                )

                if not user_name:
                    session_context_db_entry.context_data["awaiting_user_name"] = True
                    safe_flag_modified(session_context_db_entry, "context_data")
                    db.session.commit()
                    return "OK", 200
            except Exception as e:
                current_app.logger.error(f"[WELCOME] Failed to send sticker/template: {e}")

            try:
                welcome_response_payload = responder_chatboc(
                    pregunta="hola", owner_user=client_user, current_user=end_user,
                    rubro_obj=client_user.rubro, chat_db_context=session_context_db_entry,
                    tipo_chat=client_user.tipo_chat, anon_id=from_number_cleaned,
                    chat_session_uuid=chat_session_id_internal, channel="whatsapp"
                )
                delay = current_app.config.get("WELCOME_MESSAGE_DELAY_SECONDS", 5)
                _send_delayed_payload(
                    client=twilio_client, to_number=to_number_raw, from_number=from_number_raw,
                    payload=welcome_response_payload, delay=delay, app=current_app._get_current_object()
                )
                # Persist any context modifications made during the welcome call
                safe_flag_modified(session_context_db_entry, "context_data")
                db.session.add(session_context_db_entry)
                db.session.commit()
                current_app.logger.info(f"[WELCOME] Scheduled delayed menu for {from_number_cleaned}.")
            except Exception as e:
                current_app.logger.error(f"[WELCOME] Failed to schedule delayed menu: {e}")

        return "OK", 200
    elif should_trigger_welcome and is_rate_limited:
        current_app.logger.info(f"[WELCOME] Welcome skipped for {from_number_cleaned} due to rate-limit.")
    elif is_greeting and is_waiting_for_info:
        current_app.logger.info(f"[WELCOME] Welcome skipped for {from_number_cleaned} because bot is waiting for info.")

    # Determine incoming text before any special handling (re-declaration to ensure it's available for the rest of the code)
    list_id = post_vars.get("ListId")
    incoming_text = button_payload or list_id or post_vars.get("Body", "")

    if session_context_db_entry.context_data.get("awaiting_user_name"):
        name_candidate = incoming_text.strip()
        if name_candidate:
            try:
                extracted = extract_multiple_contact_details_llm(name_candidate, ["nombre"])
            except Exception as e:
                current_app.logger.error(f"[WELCOME] Name extraction failed: {e}")
                extracted = {}
            new_name = extracted.get("nombre") or name_candidate
            update_user_profile(end_user, {"name": new_name})
            session_context_db_entry.context_data.pop("awaiting_user_name", None)
            safe_flag_modified(session_context_db_entry, "context_data")
            db.session.commit()
            if twilio_client:
                twilio_client.messages.create(
                    from_=to_number_raw,
                    to=from_number_raw,
                    body=f"¡Encantado, {new_name}! ¿En qué puedo ayudarte?",
                )
            try:
                welcome_response_payload = responder_chatboc(
                    pregunta="hola", owner_user=client_user, current_user=end_user,
                    rubro_obj=client_user.rubro, chat_db_context=session_context_db_entry,
                    tipo_chat=client_user.tipo_chat, anon_id=from_number_cleaned,
                    chat_session_uuid=chat_session_id_internal, channel="whatsapp",
                )
                delay = current_app.config.get("WELCOME_MESSAGE_DELAY_SECONDS", 5)
                _send_delayed_payload(
                    client=twilio_client,
                    to_number=to_number_raw,
                    from_number=from_number_raw,
                    payload=welcome_response_payload,
                    delay=delay,
                    app=current_app._get_current_object(),
                )
                # Persist any context updates from responder_chatboc
                safe_flag_modified(session_context_db_entry, "context_data")
                db.session.add(session_context_db_entry)
                db.session.commit()
                current_app.logger.info(
                    f"[WELCOME] Scheduled delayed menu for {from_number_cleaned}."
                )
            except Exception as e:
                current_app.logger.error(
                    f"[WELCOME] Failed to schedule delayed menu after name: {e}"
                )
            return "OK", 200

    # --- Handle pending paginated messages ---
    pending_chunks = session_context_db_entry.context_data.get("pending_chunks", [])
    if pending_chunks and incoming_text.strip().lower() in ["mas", "más", "mostrar mas", "mostrar más", "show_more"]:
        next_chunk = pending_chunks.pop(0)
        session_context_db_entry.context_data["pending_chunks"] = pending_chunks
        safe_flag_modified(session_context_db_entry, "context_data")
        db.session.add(session_context_db_entry)
        db.session.commit()
        if twilio_client:
            twilio_client.messages.create(
                from_=to_number_raw,
                to=from_number_raw,
                body=next_chunk,
            )
            if pending_chunks:
                more_payload = {
                    "type": "button",
                    "body": {"text": "¿Mostrar más resultados?"},
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {"id": "show_more", "title": "Mostrar más"}},
                            {"type": "reply", "reply": {"id": "menu_principal", "title": "Menú"}},
                        ]
                    },
                }
                twilio_client.messages.create(
                    from_=to_number_raw,
                    to=from_number_raw,
                    body="Seleccioná una opción",
                    persistent_action=[f"whatsapp:{json.dumps(more_payload)}"],
                )
        return "OK", 200

    # --- Profile confirmation flow ---
    if not session_context_db_entry.context_data.get("perfil_confirmado"):
        session_context_db_entry.context_data["perfil_confirmado"] = True
        session_context_db_entry.context_data.setdefault("estado_conversacion", "activo")
        safe_flag_modified(session_context_db_entry, "context_data")
        db.session.add(session_context_db_entry)
        db.session.commit()

    # --- Message and Media Handling SECOND ---
    media_url = post_vars.get("MediaUrl0")
    media_content_type = post_vars.get("MediaContentType0")
    uploaded_file_info = None
    skip_media_analysis = False
    message_body = incoming_text

    if media_url and media_content_type:
        try:
            # Download the file from Twilio's URL first
            auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            r = requests.get(media_url, auth=auth)
            r.raise_for_status()
            media_content = r.content

            # Create a FileStorage object to be compatible with our services
            file_stream = io.BytesIO(media_content)
            file_name = f"whatsapp_media_{uuid.uuid4().hex[:12]}"
            file_storage = FileStorage(
                stream=file_stream,
                filename=file_name,
                content_type=media_content_type
            )

            # Use the attachment service to save the file and create a thumbnail if applicable
            adjunto = create_attachment_with_thumbnail(
                file_storage=file_storage,
                user_id=end_user.id if end_user else None,
                session_id=chat_session_id_internal
            )

            if adjunto:
                thumb_url = None
                if getattr(adjunto, "analisis", None) and isinstance(adjunto.analisis.datos_estructurados, dict):
                    thumb_url = adjunto.analisis.datos_estructurados.get("url")
                # Prepare the info for the chatbot logic, which will be used for all media types
                uploaded_file_info = {
                    "id": adjunto.id,
                    "url": adjunto.url,
                    "mime_type": adjunto.mime,
                    "name": adjunto.nombre_original,
                    "source": "whatsapp"
                }
                if thumb_url:
                    uploaded_file_info["thumbnail_url"] = thumb_url
                current_app.logger.info(f"WhatsApp media processed and saved as ArchivoAdjunto ID: {adjunto.id}")
            else:
                current_app.logger.error("create_attachment_with_thumbnail failed to process the WhatsApp media")

            if media_content_type.startswith("audio/"):
                session_context_db_entry.context_data['source_is_audio'] = True
                from services.audio_transcription_service import transcribe_audio_from_url
                # We pass the direct URL to the transcription service
                transcribed_text = transcribe_audio_from_url(media_url, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                if transcribed_text:
                    message_body = transcribed_text
                    uploaded_file_info['transcribed_text'] = transcribed_text
                else:
                    current_app.logger.warning("Audio transcription failed or returned empty.")
            else:
                # If it's not audio, remove the source_is_audio flag
                session_context_db_entry.context_data.pop('source_is_audio', None)

        except requests.exceptions.RequestException as e:
            current_app.logger.error(f"Error downloading media from Twilio URL {media_url}: {e}")
        except Exception as e:
            current_app.logger.error(f"Error processing WhatsApp media file: {e}", exc_info=True)
            # Reset uploaded_file_info if processing fails
            uploaded_file_info = None
    else:
        # If no media, ensure the flag is not present
        session_context_db_entry.context_data.pop('source_is_audio', None)

    # --- Location Handling ---
    latitud = post_vars.get("Latitude")
    longitud = post_vars.get("Longitude")
    location_info = None
    if latitud and longitud:
        location_info = {"latitude": latitud, "longitude": longitud}
        address = post_vars.get("Address")
        label = post_vars.get("Label")
        if address:
            location_info["address"] = address
        else:
            try:
                addr = geocodificar_inversa_llm(latitud, longitud)
                if addr and addr.get("formatted_address"):
                    location_info["address"] = addr["formatted_address"]
            except Exception as e:
                current_app.logger.error(f"Error al geocodificar inversamente {latitud, longitud}: {e}")
        if label:
            location_info["label"] = label
        print(f"Received location data: {location_info}")
    else:
        coordenadas = extraer_coordenadas_de_url_google_maps(incoming_text)
        if coordenadas:
            latitud, longitud = coordenadas
            location_info = {"latitude": str(latitud), "longitude": str(longitud)}
            try:
                addr = geocodificar_inversa_llm(latitud, longitud)
                if addr and addr.get("formatted_address"):
                    location_info["address"] = addr["formatted_address"]
            except Exception as e:
                current_app.logger.error(
                    f"Error al geocodificar inversamente {coordenadas}: {e}"
                )
            # treat message as location input only
            incoming_text = ""
            message_body = ""

    # --- Human Chat Check ---
    if session_context_db_entry.context_data.get("human_chat_in_progress"):
        room = session_context_db_entry.context_data.get("room")
        if room:
            from socket_service import socketio
            socketio.emit('message', {'msg': message_body}, room=room)
            return "OK", 200

    # --- Numeric Menu Handling ---
    last_options = session_context_db_entry.context_data.get("last_options_sent")
    municipio_ctx = (
        session_context_db_entry.context_data.get(CONTEXTO_MUNICIPIO)
        or session_context_db_entry.context_data.get("contexto_municipio", {})
    )
    if isinstance(municipio_ctx, dict):
        flow_state = (municipio_ctx.get("reclamo_flow_v2") or {}).get("state")
        if flow_state:
            skip_media_analysis = True
    esperando_info = _esperando_info_libre(municipio_ctx)

    # Solo traducir números a acciones cuando no estamos esperando información libre.
    if message_body.isdigit() and last_options and not esperando_info:
        idx = int(message_body) - 1
        if 0 <= idx < len(last_options):
            selected = last_options[idx]
            message_body = (
                selected.get("id")
                or selected.get("action_id")
                or selected.get("category_name")
                or selected.get("id_accion")
                or selected.get("texto")
                or message_body
            )

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
            mime_type = uploaded_file_info.get("mime_type", "")
            if not skip_media_analysis and not mime_type.startswith("audio/"):
                interpretacion_media_data = clasificar_adjunto_whatsapp(uploaded_file_info, client_user)
        # Location info should not be treated as interpreted media.
        # It should be passed directly as location data.

        kwargs_for_bot = {"source_channel": "whatsapp"}
        if uploaded_file_info:
            kwargs_for_bot["uploaded_file_info"] = uploaded_file_info
            mime_type = uploaded_file_info.get("mime_type", "")
            if mime_type.startswith("image/"):
                # Also add the specific keys the old flow handler expects
                kwargs_for_bot["es_foto"] = True
                kwargs_for_bot["foto_url"] = uploaded_file_info.get("url")
            if skip_media_analysis:
                kwargs_for_bot["skip_media_analysis"] = True
        if location_info:
            # Pass location_info and mark it explicitly as a location payload
            kwargs_for_bot["location"] = location_info
            kwargs_for_bot["es_ubicacion"] = True
            kwargs_for_bot["ubicacion_usuario"] = location_info
        if interpretacion_media_data and not interpretacion_media_data.get("error"):
            # This will now only contain data from actual images/files, not locations.
            kwargs_for_bot["datos_interpretados_archivo"] = interpretacion_media_data

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
            potential_fields = ["nombre_cliente", "telefono_cliente", "email_cliente"]
            current_app.logger.debug(f"[CONTACT_EXTRACTION] Extracting {potential_fields} from: {message_body}")
            extracted_data = extract_multiple_contact_details_llm(message_body, potential_fields)
            current_app.logger.debug(f"[CONTACT_EXTRACTION] Extracted: {extracted_data}")

            # Actualizar datos del reclamo con la info extraída
            if extracted_data.get("nombre_cliente"):
                datos_reclamo["nombre_usuario_detectado"] = extracted_data["nombre_cliente"]
            if extracted_data.get("telefono_cliente"):
                datos_reclamo["telefono_detectado"] = extracted_data["telefono_cliente"]
            if extracted_data.get("email_cliente"):
                datos_reclamo["email_detectado"] = extracted_data["email_cliente"]

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
            merged_context = _merge_nested_dicts(db_context, updated_context)
        else:
            merged_context = db_context

        current_app.logger.info(f"[CONTEXT_WHATSAPP] Contexto fusionado para guardar: {merged_context}")


        # Save the merged context
        session_context_db_entry.context_data = merged_context
        safe_flag_modified(session_context_db_entry, "context_data")
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

            image_url = formatted_whatsapp_payload.get("image_url")
            if image_url and 'persistent_action' not in message_params:
                if image_url.startswith('/'):
                    base_url = request.url_root.rstrip('/')
                    image_url = f"{base_url}{image_url}"
                message_params['media_url'] = [image_url]

            current_app.logger.debug(f"Sending WhatsApp message params: {message_params}")

            # Send the main message. If the body exceeds Twilio's 1600 character
            # limit (and isn't an interactive payload), send the first chunk and
            # store the remainder so the user can request more with a button.
            body_text = message_params.get('body', '') or ''
            if 'persistent_action' not in message_params and len(body_text) > MAX_TWILIO_BODY_LENGTH:
                chunks = _split_message(body_text)
                session_context_db_entry.context_data['pending_chunks'] = chunks[1:]
                safe_flag_modified(session_context_db_entry, 'context_data')
                db.session.add(session_context_db_entry)
                db.session.commit()

                first_chunk_params = {
                    'from_': to_number_raw,
                    'to': from_number_raw,
                    'body': chunks[0],
                }
                main_message = twilio_client.messages.create(**first_chunk_params)
                print(f"Mensaje parte 1/{len(chunks)} enviado a {from_number_raw}, SID: {main_message.sid}")

                if session_context_db_entry.context_data['pending_chunks']:
                    more_payload = {
                        "type": "button",
                        "body": {"text": "¿Mostrar más resultados?"},
                        "action": {
                            "buttons": [
                                {"type": "reply", "reply": {"id": "show_more", "title": "Mostrar más"}},
                                {"type": "reply", "reply": {"id": "menu_principal", "title": "Menú"}},
                            ]
                        },
                    }
                    twilio_client.messages.create(
                        from_=to_number_raw,
                        to=from_number_raw,
                        body="Seleccioná una opción",
                        persistent_action=[f"whatsapp:{json.dumps(more_payload)}"],
                    )
            else:
                session_context_db_entry.context_data.pop('pending_chunks', None)
                safe_flag_modified(session_context_db_entry, 'context_data')
                db.session.add(session_context_db_entry)
                db.session.commit()
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

    # Schedule delayed menu or follow-up payload if requested
    if (
        twilio_client
        and bot_response_dict.get("delayed_payload")
        and bot_response_dict.get("delay_seconds")
    ):
        _send_delayed_payload(
            twilio_client,
            to_number_raw,
            from_number_raw,
            bot_response_dict["delayed_payload"],
            bot_response_dict["delay_seconds"],
            current_app._get_current_object()
        )

    return "OK", 200
