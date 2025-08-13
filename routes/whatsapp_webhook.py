import sys
import os
from flask import Blueprint, request, current_app, session
from twilio.twiml.messaging_response import MessagingResponse
from services.logic import responder_chatboc
import logging
from models import db, ChatSession, User, Rubro, load_user
from services.session_manager import SessionManager
from datetime import datetime

# Add project root to sys.path for this route file
project_root_routes = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_routes not in sys.path:
    sys.path.insert(0, project_root_routes)

whatsapp_webhook_bp = Blueprint('whatsapp_webhook', __name__)

logger = logging.getLogger(__name__)

@whatsapp_webhook_bp.route("/webhook/whatsapp", methods=['POST'])
def whatsapp_webhook():
    """
    Webhook para recibir mensajes de WhatsApp a través de Twilio.
    Identifica al usuario, obtiene el contexto de la sesión, llama a la lógica del bot,
    y envía la respuesta de vuelta a Twilio.
    """
    logger.info("Whatsapp webhook called!")
    logger.info("Request form: %s", request.form)

    # Extraer datos del request de Twilio
    incoming_msg = request.values.get('Body', '').strip()
    from_number = request.values.get('From', '').replace('whatsapp:', '')
    to_number = request.values.get('To', '').replace('whatsapp:', '')
    profile_name = request.values.get('ProfileName', '')

    # --- Identificación del Bot (User Owner) ---
    # En Twilio, el 'To' number es el número del bot.
    # Buscamos en la DB a qué User (empresa/municipio) pertenece este número.
    owner_user = User.query.filter_by(telefono=to_number).first()
    if not owner_user:
        logger.error(f"CRITICAL: No se encontró un usuario dueño para el número de bot '{to_number}'. No se puede continuar.")
        # Podríamos enviar una respuesta genérica, pero es mejor que falle para debug.
        # En producción, podrías querer enviar un SMS de error al admin.
        return "Error: Bot no configurado.", 500

    session_id = f"whatsapp_{owner_user.id}_{from_number}"
    logger.info(f"Calling responder_chatboc for session_id: {session_id}, owner_user: {owner_user.nombre_empresa}")

    # --- Identificación del Usuario Final (Viewer) ---
    # El 'From' number es el del usuario final.
    # Buscamos si ya existe un usuario (ciudadano/cliente) con ese número asociado al bot.
    viewer_user = User.query.filter_by(telefono=from_number, empresa_id=owner_user.id).first()
    if not viewer_user:
        # Si no existe, lo creamos como un nuevo 'ciudadano' o 'cliente'
        # Asumimos 'ciudadano' para rubros públicos, 'cliente' para el resto.
        from services.logic import es_rubro_publico
        rol_viewer = 'ciudadano' if es_rubro_publico(owner_user.rubro) else 'cliente'

        viewer_user = User(
            telefono=from_number,
            nombre_display_whatsapp=profile_name, # Guardamos el nombre de perfil de WA
            empresa_id=owner_user.id, # Asociado al bot/municipio
            rol=rol_viewer,
            activo=True
        )
        db.session.add(viewer_user)
        db.session.commit()
        logger.info(f"Nuevo usuario final '{profile_name}' (ID: {viewer_user.id}) creado con rol '{rol_viewer}' para el bot '{owner_user.nombre_empresa}'.")
    elif viewer_user.nombre_display_whatsapp != profile_name:
        # Si el usuario ya existe, actualizamos su nombre de perfil de WA si cambió.
        viewer_user.nombre_display_whatsapp = profile_name
        db.session.commit()
        logger.info(f"Nombre de perfil de WA actualizado para el usuario {from_number} a '{profile_name}'.")

    # --- Manejo de Sesión y Contexto ---
    # Usamos un session_id único para la combinación bot-usuario.
    chat_session_context = SessionManager.get_session(session_id)

    # Si no hay contexto, creamos uno nuevo.
    if not chat_session_context:
        chat_session_context = SessionManager.create_session(
            session_id=session_id,
            user_id=viewer_user.id, # El ID del usuario final
            owner_id=owner_user.id, # El ID del dueño del bot
            channel='whatsapp'
        )

    pregunta = incoming_msg

    try:
        # --- Audio handling ---
        if 'MediaContentType0' in request.form and request.form['MediaContentType0'].startswith('audio/'):
            media_url = request.form['MediaUrl0']

            # Set a persistent preference for audio responses in this session
            if chat_session_context and chat_session_context.context_data is not None:
                chat_session_context.context_data['prefers_audio'] = True
                logger.info("User preference for audio responses has been set for this session.")

            # Transcribe audio to text
            from services.audio_transcription_service import AudioTranscriptionService
            transcription_service = AudioTranscriptionService()
            try:
                transcribed_text = transcription_service.transcribe_audio_from_url(media_url)
                if transcribed_text:
                    pregunta = transcribed_text
                    logger.info(f"Audio transcrito exitosamente: '{pregunta}'")
                else:
                    pregunta = "el usuario envió un audio que no pudo ser transcrito"
                    logger.warning("La transcripción de audio no devolvió texto.")
            except Exception as e:
                pregunta = "el usuario envió un audio que no pudo ser procesado"
                logger.error(f"Error al transcribir el audio: {e}")
        elif chat_session_context and chat_session_context.context_data is not None:
            # If the incoming message is text, unset the audio preference
            if 'prefers_audio' in chat_session_context.context_data:
                logger.info("User sent a text message, unsetting audio preference for subsequent responses.")
                chat_session_context.context_data.pop('prefers_audio', None)
        # --- End Audio handling ---

        bot_response_dict = responder_chatboc(
            pregunta=pregunta,
            owner_user=owner_user,
            current_user=viewer_user, # Pasamos el usuario final como 'current_user'
            rubro_obj=owner_user.rubro,
            chat_db_context=chat_session_context,
            anon_id=from_number, # En WA, el número es el identificador anónimo
            chat_session_uuid=session_id,
            channel='whatsapp',
            # Pasamos el nombre de perfil de WA para usarlo en saludos, etc.
            whatsapp_profile_name=profile_name
        )
        logger.info(f"Raw response from responder_chatboc: {bot_response_dict}")
    except Exception as e:
        logger.error(f"Error calling real chatbot logic (responder_chatboc): {e}", exc_info=True)
        # Reseteamos el contexto a un estado seguro en caso de error.
        chat_session_context.reset_context()
        bot_response_dict = {
            "message_body": "Lo siento, no pude procesar tu solicitud en este momento.",
            "message_type": "text",
            "fuente": "error_handler"
        }

    # Guardar el contexto actualizado en la DB
    SessionManager.save_session(chat_session_context)
    logger.info(f"Session saved for {session_id}. Context: {chat_session_context.context_data}")

    # --- Construcción de la Respuesta para Twilio ---
    response = MessagingResponse()
    msg = response.message()

    main_message = bot_response_dict.get("message_body", "No se encontró respuesta.")

    # Log the response text for easier debugging
    logger.info(f"Bot response text for logging: '{main_message}', Session context to save: {chat_session_context.context_data}")

    # Enviar mensaje principal
    msg.body(main_message)
    logger.info(f"Mensaje principal enviado a {from_number}, SID: {msg.sid}")

    # Enviar audio si existe
    if bot_response_dict.get("audio_url"):
        audio_msg = response.message()
        # La URL debe ser accesible públicamente por Twilio
        full_audio_url = request.host_url.rstrip('/') + bot_response_dict["audio_url"]
        audio_msg.media(full_audio_url)
        logger.info(f"Mensaje de audio enviado a {from_number}, SID: {audio_msg.sid}")

    # Convertir botones/opciones al formato de Twilio (si existen)
    # Esta parte necesita ser implementada según cómo Twilio maneja botones interactivos.
    # Por ahora, se asume que la respuesta principal ya incluye el texto de las opciones.
    # Ejemplo básico para botones de WhatsApp:
    options = bot_response_dict.get("options_list")
    message_type = bot_response_dict.get("message_type")

    # Twilio no soporta botones directamente en el TwiML de la misma forma que otros.
    # Se deben enviar como mensajes separados usando la API REST.
    # Aquí, simplemente formateamos el texto para que el usuario pueda responder.

    if options:
        if message_type == 'interactive_list':
            # Formato para listas interactivas
            formatted_options = "\n\n"
            for i, option in enumerate(options, 1):
                formatted_options += f"{i}. {option['texto']}\n"
            formatted_options += "\n➡️ Responde con el número de la opción que necesites."
            main_message += formatted_options
        elif message_type == 'interactive_buttons':
            # Formato para botones simples
            formatted_options = "\n\n"
            for i, option in enumerate(options, 1):
                formatted_options += f"{i}. {option.get('texto') or option.get('text')}\n" # Compatible con ambos formatos
            formatted_options += "\n➡️ Responde con el número de la opción que necesites."
            main_message += formatted_options

    # Re-enviar el mensaje completo si se modificó con opciones
    # Esto es un workaround. La forma correcta es usar la API de Twilio para mensajes interactivos.
    # Por simplicidad del ejemplo, modificamos el cuerpo del mensaje.
    # response = MessagingResponse()
    # response.message(main_message) # Esto sobreescribiría el mensaje anterior.
    # La lógica actual ya añade el texto al `main_message` y lo envía.
    # La limitación es que los botones no son "clickeables" sino texto.
    # Para botones reales, se necesita una llamada a la API de Twilio, no TwiML.

    return str(response)
