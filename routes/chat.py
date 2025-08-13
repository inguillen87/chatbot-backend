from flask import Blueprint, request, jsonify, current_app, session
from flask_login import login_required, current_user
from services.logic import responder_chatboc
from services.session_manager import SessionManager
from models import db, User, Rubro, ArchivoAdjunto
import uuid
import logging
from extensions import socketio # Import socketio

chat_bp = Blueprint('chat', __name__)
logger = logging.getLogger(__name__)

def _procesar_chat(pregunta, tipo_chat, rubro_id=None, rubro_clave=None, actor_principal_id=None, owner_del_bot_id=None, viewer_obj_id=None, anon_id=None, archivo_adjunto_id=None, uploaded_file_info=None, session_chat_id=None):
    """
    Función interna para procesar una solicitud de chat.
    Extrae la lógica común entre la ruta HTTP y el evento de Socket.IO.
    """
    channel = "web"
    # Lógica de negocio para obtener la respuesta del bot
    # ... (código existente para determinar owner_del_bot, viewer_obj, etc.)

    # Placeholder for owner and viewer object retrieval logic
    owner_del_bot = None
    if owner_del_bot_id:
        owner_del_bot = User.query.get(owner_del_bot_id)

    viewer_obj = None
    if viewer_obj_id:
        viewer_obj = User.query.get(viewer_obj_id)

    # Placeholder for rubro object retrieval
    rubro_obj = None
    if rubro_id:
        rubro_obj = Rubro.query.get(rubro_id)
    elif rubro_clave:
        rubro_obj = Rubro.query.filter_by(clave=rubro_clave).first()

    # --- Session and Context Management ---
    chat_session_id = session_chat_id
    if not chat_session_id:
        # This part is tricky. For web, the session ID might come from the client.
        # Let's assume it's passed in or we create a new one.
        # For a real app, this needs a robust session management strategy.
        chat_session_id = f"web_{owner_del_bot_id}_{viewer_obj_id or anon_id}"

    chat_session_context = SessionManager.get_session(chat_session_id)
    if not chat_session_context:
        chat_session_context = SessionManager.create_session(
            session_id=chat_session_id,
            user_id=viewer_obj_id,
            owner_id=owner_del_bot_id,
            anon_id=anon_id,
            channel='web' # Explicitly set channel for new sessions
        )

    # --- File Handling ---
    if archivo_adjunto_id:
        adjunto = ArchivoAdjunto.query.get(archivo_adjunto_id)
        if adjunto:
            uploaded_file_info = {
                "id": adjunto.id,
                "url": adjunto.url,
                "mime_type": adjunto.mime,
                "name": adjunto.nombre_original,
                "source": "web_upload"
            }

    try:
        # Llamada a la lógica principal del bot
        bot_response_dict = responder_chatboc(
            pregunta=pregunta,
            owner_user=owner_del_bot,
            current_user=viewer_obj,
            rubro_obj=rubro_obj,
            chat_db_context=chat_session_context,
            rubro_nombre_frontend=rubro_clave,
            tipo_chat=tipo_chat,
            anon_id=anon_id,
            chat_session_uuid=chat_session_id,
            channel=channel, # Pass the channel
            uploaded_file_info=uploaded_file_info,
        )

        # After getting the response, save the updated context
        SessionManager.save_session(chat_session_context)
        
        # Emit the response via Socket.IO for real-time update
        chat_session_id_header = request.headers.get('X-Chat-Session-Id')
        if channel == "web" and chat_session_id_header:
            socketio.emit('bot_response', {
                'message_body': bot_response_dict.get('message_body'),
                'options': bot_response_dict.get('options_list', []),
                'message_type': bot_response_dict.get('message_type'),
                'audio_url': bot_response_dict.get('audio_url'),
                'fuente': bot_response_dict.get('fuente')
            }, room=chat_session_id_header)
            logger.info(f"Bot response emitted to Socket.IO room: {chat_session_id_header}")


        return bot_response_dict

    except Exception as e:
        logger.error(f"❌ Error crítico en _procesar_chat. Details: {request.json}. Exception: {e}", exc_info=True)
        # Return a generic error response
        return jsonify({"error": "Ocurrió un error interno en el servidor."}), 500


@chat_bp.route('/ask/municipio', methods=['POST'])
@login_required
def ask_municipio():
    data = request.get_json()
    if not data or 'pregunta' not in data:
        abort(400, description="Falta el campo 'pregunta' en el cuerpo de la solicitud.")

    pregunta = data.get('pregunta')

    # For web chat, the viewer is the logged-in user
    viewer_obj = current_user

    # The 'owner' of the bot is determined by the context.
    # For a municipal chat, this would be the municipality's User object.
    # This needs to be fetched based on some logic, e.g., a config or a user property.
    # Let's assume a function `get_municipio_bot_user()` exists.
    # For now, we'll hardcode it for the example if not available.
    owner_del_bot = User.query.filter_by(rol='municipio_bot_owner').first() # Example query
    if not owner_del_bot:
        # Fallback for testing - assuming user 1 is the bot owner
        owner_del_bot = User.query.get(1)
        if not owner_del_bot:
            return jsonify({"error": "No se pudo determinar el bot del municipio."}), 500

    # Get anonymous ID from header if present
    anon_id = request.headers.get('X-Anonymous-Id') or str(uuid.uuid4())

    # Get Chat Session ID from header
    chat_session_id_header = request.headers.get('X-Chat-Session-Id')

    # Call the processing function
    response = _procesar_chat(
        pregunta=pregunta,
        tipo_chat='municipio',
        owner_del_bot_id=owner_del_bot.id,
        viewer_obj_id=viewer_obj.id,
        anon_id=anon_id,
        session_chat_id=chat_session_id_header
    )

    return jsonify(response)

# ... (other routes like /ask/pyme if they exist)
