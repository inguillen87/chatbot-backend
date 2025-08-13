import sys
import os
import logging

# Add project root to sys.path for this service file
project_root_logic = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_logic not in sys.path:
    sys.path.insert(0, project_root_logic)

from flask import current_app
from models import db
from services.interpretacion_service import interpretacion_service
from services.archivo_service import archivo_service
# servicio_tickets se importa/usa en los handlers específicos (municipios.py, pymes.py)

logger = logging.getLogger(__name__)

# Rubros que deben usar la lógica de municipio/ente público
RUBROS_PUBLICOS = {
    "municipio",
    "municipios",
    "ong",
    "gobierno",
    "hospital_publico",
    "entidad_publica",
    "municipal",
    "publico",
    # Agregá acá los que consideres públicos
}

def normalizar_rubro(rubro) -> str:
    """Devuelve el nombre del rubro en minúsculas."""
    if not rubro:
        return ""
    if isinstance(rubro, str):
        return rubro.strip().lower()
    if hasattr(rubro, "nombre") and getattr(rubro, "nombre"):
        return str(rubro.nombre).strip().lower()
    if hasattr(rubro, "clave") and getattr(rubro, "clave"):
        return str(rubro.clave).strip().lower()
    return str(rubro).strip().lower()


from .herramientas_municipio import normalizar_texto

def es_rubro_publico(rubro) -> bool:
    """Indica si un rubro pertenece a ``RUBROS_PUBLICOS``."""
    return normalizar_rubro(rubro) in RUBROS_PUBLICOS


from services.llm_utils import clasificar_entidad_con_llm
from services.municipio_responder import responder_municipio
from services.pymes import responder_pyme
from services.common_utils import clean_text_for_tts

# PROMPT_CLASIFICACION_INTENCION y _clasificar_intencion_con_llm han sido eliminados.
# La clasificación de intención ahora es responsabilidad de llamar_gemini con JULES_SYSTEM_PROMPT.

# ... otras funciones que ya tengas en logic.py (como responder_chatboc)
def responder_chatboc(
    pregunta,
    owner_user=None,
    current_user=None,
    rubro_obj=None,
    # session_obj=None, # <<< REPLACED by chat_db_context
    chat_db_context=None, # <<< NEW: To pass the ChatSessionContext object
    rubro_nombre_frontend=None,
    tipo_chat=None,
    anon_id=None,
    chat_session_uuid=None,
    channel: str = "web", # Add channel parameter
    **kwargs,
):
    """Envía la consulta al handler correcto según el rubro y tipo de chat."""
    logger.debug(f"[responder_chatboc] START - Args: pregunta='{pregunta}', owner_user_id='{getattr(owner_user, 'id', 'N/A')}', current_user_id='{getattr(current_user, 'id', 'N/A')}', anon_id='{anon_id}', tipo_chat_inicial='{tipo_chat}', rubro_obj_id='{getattr(rubro_obj, 'id', 'N/A')}', chat_session_uuid='{chat_session_uuid}', channel='{channel}'")

    # 1. Determinar el 'effective_owner_user' (la entidad o bot dueño)
    effective_owner_user = owner_user
    if not effective_owner_user and current_user and current_user.empresa_id:
        from models import User # Local import
        logger.debug(f"[responder_chatboc] No owner_user arg, but current_user ({current_user.id}) has empresa_id ({current_user.empresa_id}). Attempting to load owner.")
        potential_owner = User.query.get(current_user.empresa_id)
        if potential_owner:
            effective_owner_user = potential_owner
            logger.info(f"[responder_chatboc] Set effective_owner_user to User ID {effective_owner_user.id} (Name: {effective_owner_user.nombre_empresa}) via current_user.empresa_id.")
        else: #pragma: no cover
            logger.warning(f"[responder_chatboc] current_user.empresa_id ({current_user.empresa_id}) did not resolve to a valid User. effective_owner_user remains None.")

    if not effective_owner_user:
        logger.warning(f"[responder_chatboc] 'effective_owner_user' could not be determined. This is critical for context-specific logic (e.g., for /ask/municipio). Check if a valid entity token is being passed for the bot instance.")

    # 2. Detectar nombre de rubro (universal)
    rubro_nombre = ""
    fuente = ""
    if rubro_obj:
        if getattr(rubro_obj, "nombre", None):
            rubro_nombre = str(rubro_obj.nombre).strip().lower()
            fuente = "rubro_obj.nombre"
        elif getattr(rubro_obj, "clave", None):
            rubro_nombre = str(rubro_obj.clave).strip().lower()
            fuente = "rubro_obj.clave"
    elif owner_user and getattr(owner_user, "rubro", None):
        rubro_value = owner_user.rubro
        if hasattr(rubro_value, "nombre") and rubro_value.nombre:
            rubro_nombre = str(rubro_value.nombre).strip().lower()
            fuente = "owner_user.rubro.nombre"
        elif hasattr(rubro_value, "clave") and rubro_value.clave:
            rubro_nombre = str(rubro_value.clave).strip().lower()
            fuente = "owner_user.rubro.clave"
        else:
            rubro_nombre = str(rubro_value).strip().lower()
            fuente = "owner_user.rubro (str)"
    elif rubro_nombre_frontend:
        rubro_nombre = str(rubro_nombre_frontend).strip().lower()
        fuente = "rubro_nombre_frontend"
    else:
        rubro_nombre = ""
        fuente = "no_encontrado"

    # logger.info(  # Comentado para reducir verbosidad, la siguiente línea es más completa.
    #     f"[LOGIC] Usando rubro: '{rubro_nombre}' (fuente: {fuente}, user: {getattr(owner_user, 'id', None)})"
    # )

    # Si el rubro indica un tipo específico de lógica, lo usamos siempre
    if rubro_nombre:
        tipo_chat = "municipio" if es_rubro_publico(rubro_nombre) else "pyme"
    elif tipo_chat not in ("municipio", "pyme"):
        raise ValueError(f"Tipo de chat inválido: {tipo_chat}")

    # --- INICIO: Manejo de confusión Pyme/Municipio ---
    if tipo_chat == "pyme":
        from services.municipio_responder import MENU_KEYWORDS as MUNICIPIO_MENU_KEYWORDS
        pregunta_norm = normalizar_texto(pregunta)
        # Check for municipal keywords in the user's query
        for action, keywords in MUNICIPIO_MENU_KEYWORDS.items():
            if any(keyword in pregunta_norm for keyword in keywords):
                pyme_name = getattr(effective_owner_user, "nombre_empresa", "este comercio")
                return {
                    "message_body": f"Parece que estás consultando sobre un trámite municipal, pero te encuentras en el chat de {pyme_name}. ¿Querés que te dirija al asistente del municipio?",
                    "options_list": [
                        # This would need frontend logic to handle a redirection.
                        # For now, we just guide the user.
                        {"texto": "Ir al Chat del Municipio", "action_id": "redirect_municipio"},
                        {"texto": "Quedarme aquí", "action_id": "stay_pyme"}
                    ],
                    "message_type": "interactive_buttons",
                    "fuente": "pyme_municipio_confusion_handler"
                }
    # --- FIN: Manejo de confusión ---

    # --- Inicio: Lógica de manejo de archivo adjunto y su análisis ---
    uploaded_file_info = kwargs.get("uploaded_file_info")
    datos_interpretados_de_archivo = kwargs.get("interpretacion_imagen_data")
    archivo_id_para_asociar_al_ticket = None
    procesamiento_archivo_en_curso = False # Nueva bandera

    if uploaded_file_info and isinstance(uploaded_file_info, dict):
        logger.info(f"DEBUG: Processing uploaded_file_info in responder_chatboc: {uploaded_file_info}")
        if uploaded_file_info.get("id"):
            archivo_id = uploaded_file_info.get("id")
            current_app.logger.info(f"[LOGIC] Procesando uploaded_file_info para ArchivoAdjunto ID: {archivo_id}")
            # El resto de la lógica para archivos subidos desde el frontend va aquí
        elif uploaded_file_info.get("source") == "whatsapp":
            from services.document_processing_service import document_processing_service
            from services.interpretacion_imagen_service import interpretar_imagen_para_chat
            import requests

            media_url = uploaded_file_info.get("url")
            media_content_type = uploaded_file_info.get("mime_type")

            try:
                response = requests.get(media_url, auth=(current_app.config.get("TWILIO_ACCOUNT_SID"), current_app.config.get("TWILIO_AUTH_TOKEN")))
                response.raise_for_status()
                file_content = response.content

                if media_content_type.startswith("image/"):
                    datos_interpretados_de_archivo = interpretar_imagen_para_chat(
                        archivo_adjunto=uploaded_file_info,
                        tipo_interpretacion="reclamo_auto_descripcion_categoria"
                    )
                else:
                    doc_ai_result = document_processing_service.process_document(file_content, media_content_type)
                    if doc_ai_result:
                        # Aquí puedes procesar el resultado de Document AI
                        # Por ahora, solo extraemos el texto
                        datos_interpretados_de_archivo = {"texto_extraido": doc_ai_result.text}
                    else:
                        datos_interpretados_de_archivo = {"error": "No se pudo procesar el documento."}
            except requests.exceptions.RequestException as e:
                current_app.logger.error(f"Error descargando archivo de WhatsApp: {e}")
                datos_interpretados_de_archivo = {"error": "No se pudo descargar el archivo."}

    # Actualizar kwargs para pasar la información a los handlers específicos
    kwargs["datos_interpretados_archivo"] = datos_interpretados_de_archivo
    kwargs["archivo_id_para_asociar"] = archivo_id_para_asociar_al_ticket
    kwargs["procesamiento_archivo_en_curso"] = procesamiento_archivo_en_curso

    if "uploaded_file_info" in kwargs: # Limpiar para no pasarlo si ya se usó.
        del kwargs["uploaded_file_info"]
    # --- Fin: Lógica de manejo de archivo adjunto ---

    # Si un archivo está en proceso, no pasamos al LLM de intención general ni small talk.
    # La respuesta ya se dio arriba.
    # No, esto no es correcto. Si el archivo se está procesando, ya retornamos.
    # Si llegamos aquí, o no hubo archivo, o el análisis se completó (y datos_interpretados_archivo está poblado o es None).



    # Pasar el contexto_previo correcto al handler
    # El contexto_previo que llega a kwargs es el genérico.
    # Los handlers esperan "contexto_pyme" o "contexto_municipio".
    # El session_obj (Flask session) se usa para almacenar el contexto entre llamadas.
    # El `contexto_previo` en kwargs debería ser el específico del tipo_chat.

    # La variable `session_obj` que se pasa a los handlers es la sesión de Flask.
    # Los handlers (responder_municipio, responder_pyme) son responsables de cargar/guardar
    # su propio contexto desde/hacia chat_db_context.context_data usando chat_session_uuid como posible sub-key si es necesario.

    response_data = None
    if tipo_chat == "municipio":
        # Añadir datos interpretados al contexto del usuario para el LLM
        if datos_interpretados_de_archivo:
            if not owner_user.datos_interpretados_archivo:
                owner_user.datos_interpretados_archivo = {}
            owner_user.datos_interpretados_archivo.update(datos_interpretados_de_archivo)

        response_data = responder_municipio(
            pregunta_original=pregunta, # La pregunta original del usuario
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=current_user,
            chat_db_context=chat_db_context, # Pasar el contexto de DB
            anon_id=anon_id,
            chat_session_uuid=chat_session_uuid,
            channel=channel, # Pass channel
            **kwargs, # Contiene datos_interpretados_archivo y archivo_id_para_asociar
        )
    elif tipo_chat == "pyme":
        # Añadir datos interpretados al contexto del usuario para el LLM
        if datos_interpretados_de_archivo:
            if not owner_user.datos_interpretados_archivo:
                owner_user.datos_interpretados_archivo = {}
            owner_user.datos_interpretados_archivo.update(datos_interpretados_de_archivo)

        response_data = responder_pyme(
            pregunta_original=pregunta,
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=current_user,
            chat_db_context=chat_db_context, # Pasar el contexto de DB
            anon_id=anon_id,
            chat_session_uuid=chat_session_uuid,
            channel=channel, # Pass channel
            **kwargs,
        )
    else:
        # Esto no debería ocurrir debido a las validaciones previas de tipo_chat
        logger.error(f"Error crítico: tipo_chat '{tipo_chat}' no es ni 'municipio' ni 'pyme' en la parte final de responder_chatboc.")
        response_data = {"respuesta": "Error interno: tipo de chat no configurado correctamente.", "fuente": "sistema_error"}

    # --- Audio Response Generation ---
    generate_audio = False
    # Check if the user has indicated a preference for audio (by sending an audio message)
    if chat_db_context and chat_db_context.context_data and chat_db_context.context_data.get('prefers_audio'):
        generate_audio = True

    # Also check if the handler specifically requested audio generation for this specific response
    if response_data and response_data.get('generar_audio'):
        generate_audio = True

    if generate_audio:
        text_to_speak = response_data.get('message_body')
        if text_to_speak:
            cleaned_text = clean_text_for_tts(text_to_speak)
            from services.google_text_to_speech import TextToSpeechService
            tts_service = TextToSpeechService()
            audio_url = tts_service.synthesize_speech(cleaned_text)
            if audio_url:
                response_data['audio_url'] = audio_url
                logger.info(f"Generated audio response at {audio_url}")

        # Clean up the single-use 'generar_audio' flag from the response
        if response_data:
            response_data.pop('generar_audio', None)
        # The 'prefers_audio' and 'source_is_audio' flags are now managed in the webhook
        # based on the type of the next incoming message.

    return response_data
