import sys
import os
import logging
import uuid
from flask import current_app
from models import db
from services.pymes import responder_pyme
from services.municipio_responder import responder_municipio
from .herramientas_municipio import normalizar_texto
from .common_utils import es_rubro_publico, normalizar_rubro

logger = logging.getLogger(__name__)

def responder_chatboc(
    pregunta,
    owner_user=None,
    viewer_user=None,
    rubro_obj=None,
    chat_db_context=None,
    rubro_nombre_frontend=None,
    tipo_chat=None,
    anon_id=None,
    chat_session_uuid=None,
    channel: str = "web",
    **kwargs,
):
    """
    Dispatcher principal. Determina si la consulta es para una pyme o municipio
    y la delega al handler correspondiente.
    """
    if 'current_user' in kwargs and viewer_user is None:
        viewer_user = kwargs.pop('current_user')
    if 'viewer_user' in kwargs:
        if viewer_user is None:
            viewer_user = kwargs.pop('viewer_user')
        else:
            kwargs.pop('viewer_user')
    if 'owner_user' in kwargs:
        if owner_user is None:
            owner_user = kwargs.pop('owner_user')
        else:
            kwargs.pop('owner_user')

    rubro_nombre = None
    authoritative_rubro_source = rubro_obj or (owner_user.rubro if owner_user and hasattr(owner_user, 'rubro') else None)

    if authoritative_rubro_source:
        rubro_nombre = getattr(authoritative_rubro_source, 'nombre', None) or getattr(authoritative_rubro_source, 'clave', None)

    if not rubro_nombre:
        rubro_nombre = rubro_nombre_frontend

    final_tipo_chat = None
    if rubro_nombre:
        final_tipo_chat = "municipio" if es_rubro_publico(rubro_nombre) else "pyme"
    else:
        final_tipo_chat = tipo_chat

    if not final_tipo_chat:
        logger.error("[responder_chatboc] No se pudo determinar el tipo de chat (pyme/municipio).")
        return {"message_body": "Error de configuración del bot.", "fuente": "error_configuracion_tipo_chat"}

    if isinstance(pregunta, dict) and "media_url" in pregunta:
        if viewer_user and chat_db_context and not viewer_user.prefers_audio:
            audio_message_count = chat_db_context.context_data.get('audio_input_count', 0) + 1
            chat_db_context.context_data['audio_input_count'] = audio_message_count
            if audio_message_count >= 2:
                viewer_user.prefers_audio = True
                db.session.add(viewer_user)

    if final_tipo_chat == "municipio":
        response_data = responder_municipio(
            pregunta_original=pregunta, owner_user=owner_user,
            rubro_obj=rubro_obj, viewer_user=viewer_user,
            chat_db_context=chat_db_context, anon_id=anon_id,
            chat_session_uuid=chat_session_uuid, channel=channel, **kwargs
        )
    elif final_tipo_chat == "pyme":
        response_data = responder_pyme(
            pregunta_original=pregunta, owner_user=owner_user,
            rubro_obj=rubro_obj, viewer_user=viewer_user,
            chat_db_context=chat_db_context, anon_id=anon_id,
            chat_session_uuid=chat_session_uuid, channel=channel, **kwargs
        )
    else:
        logger.error(f"Error crítico: tipo_chat '{final_tipo_chat}' no es válido.")
        response_data = {"respuesta": "Error interno: tipo de chat no configurado.", "fuente": "sistema_error"}

    logger.info(f"DEBUG: response_data from handler: {response_data}")
    should_generate_audio = (viewer_user and viewer_user.prefers_audio) or \
                            (chat_db_context and chat_db_context.context_data.get('source_is_audio'))
    logger.info(f"DEBUG: should_generate_audio: {should_generate_audio}")

    if response_data and should_generate_audio:
        response_data['generar_audio'] = True

    if response_data and response_data.get('generar_audio') and not response_data.get('audio_url'):
        text_to_speak = response_data.get('message_body')
        if text_to_speak:
            logger.info(f"DEBUG: Generating audio for text: {text_to_speak}")
            from services.google_text_to_speech import generate_audio_url
            context_data = chat_db_context.context_data if chat_db_context else None
            audio_url = generate_audio_url(text_to_speak, context_data, viewer_user)
            if audio_url:
                response_data['audio_url'] = audio_url
                logger.info(f"Generated audio response at {audio_url}")

    if chat_db_context and chat_db_context.context_data:
        chat_db_context.context_data.pop('source_is_audio', None)
    if response_data:
        response_data.pop('generar_audio', None)

    return response_data
