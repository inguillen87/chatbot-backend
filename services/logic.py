import sys
import os
import logging

# Add project root to sys.path for this service file
project_root_logic = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_logic not in sys.path:
    sys.path.insert(0, project_root_logic)

from flask import current_app
from extensions import db
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
    "municipalidad",
    # Agregá acá los que consideres públicos
}

def normalizar_rubro(rubro) -> str:
    """Devuelve el nombre del rubro en minúsculas."""
    if not rubro:
        return ""
    if isinstance(rubro, str):
        return rubro.strip().lower()
    if hasattr(rubro, "clave") and getattr(rubro, "clave"):
        return str(rubro.clave).strip().lower()
    if hasattr(rubro, "nombre") and getattr(rubro, "nombre"):
        return str(rubro.nombre).strip().lower()
    return str(rubro).strip().lower()


from .herramientas_municipio import normalizar_texto

def es_rubro_publico(rubro) -> bool:
    """Indica si un rubro pertenece a ``RUBROS_PUBLICOS``."""
    return normalizar_rubro(rubro) in RUBROS_PUBLICOS


from services.llm_utils import clasificar_entidad_con_llm
from services.municipio_responder import responder_municipio
from services.pymes import responder_pyme
from services.response_formatter import render_audio_text
from services import preferences

# PROMPT_CLASIFICACION_INTENCION y _clasificar_intencion_con_llm han sido eliminados.
# La clasificación de intención ahora es responsabilidad de llamar_llm_con_fallback con JULES_SYSTEM_PROMPT.

# ... otras funciones que ya tengas en logic.py (como responder_chatboc)
from utils.db_utils import safe_flag_modified

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

    # --- Low-confidence STT handling ---
    if isinstance(pregunta, dict) and 'confidence' in pregunta and 'transcript' in pregunta:
        confidence = pregunta.get('confidence', 1.0)
        transcript = pregunta.get('transcript', '')
        if confidence < 0.8 and transcript:
            if chat_db_context:
                chat_db_context.context_data['stt_pending_confirmation'] = transcript
                safe_flag_modified(chat_db_context, "context_data")
            return {
                "message_body": f"Escuché: \"{transcript}\". ¿Es correcto?",
                "options_list": [
                    {"texto": "Sí, es correcto", "action_id": "confirmar_stt_si"},
                    {"texto": "No, intentar de nuevo", "action_id": "confirmar_stt_no"}
                ],
                "message_type": "interactive_buttons",
                "fuente": "confirmacion_stt_baja_confianza"
            }
        pregunta = transcript # Use the transcript as the question

    if pregunta == "confirmar_stt_si":
        if chat_db_context and 'stt_pending_confirmation' in chat_db_context.context_data:
            pregunta = chat_db_context.context_data.pop('stt_pending_confirmation')
            safe_flag_modified(chat_db_context, "context_data")
        else:
            return {"message_body": "Hubo un error, no recuerdo qué estábamos confirmando. Por favor, inténtalo de nuevo."}

    if pregunta == "confirmar_stt_no":
        if chat_db_context:
            chat_db_context.context_data.pop('stt_pending_confirmation', None)
            safe_flag_modified(chat_db_context, "context_data")
        return {"message_body": "Entendido. Por favor, envía tu mensaje de nuevo."}

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
        pregunta_text = pregunta if isinstance(pregunta, str) else pregunta.get("pregunta", "")
        pregunta_norm = normalizar_texto(pregunta_text)
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
    datos_interpretados_de_archivo = kwargs.get("datos_interpretados_archivo")
    procesamiento_archivo_en_curso = False  # Nueva bandera

    ids_archivos_para_asociar = []
    if chat_db_context and chat_db_context.context_data is not None:
        ids_archivos_para_asociar = list(
            chat_db_context.context_data.get("ids_archivos_para_asociar", [])
        )

    if uploaded_file_info and isinstance(uploaded_file_info, dict):
        logger.info(
            f"DEBUG: Processing uploaded_file_info in responder_chatboc: {uploaded_file_info}"
        )
        archivo_id = uploaded_file_info.get("id")
        if archivo_id:
            if archivo_id not in ids_archivos_para_asociar:
                ids_archivos_para_asociar.append(archivo_id)
                if chat_db_context and chat_db_context.context_data is not None:
                    chat_db_context.context_data["ids_archivos_para_asociar"] = (
                        ids_archivos_para_asociar
                    )
                    safe_flag_modified(chat_db_context, "context_data")
            current_app.logger.info(
                f"[LOGIC] Procesando uploaded_file_info para ArchivoAdjunto ID: {archivo_id}"
            )
        if uploaded_file_info.get("source") == "whatsapp":
            from services.document_processing_service import document_processing_service
            from services.interpretacion_imagen_service import interpretar_imagen_para_chat
            import requests
            import os

            media_url = uploaded_file_info.get("public_url") or uploaded_file_info.get("url")
            media_content_type = uploaded_file_info.get("mime_type")

            try:
                file_content = None
                final_url = media_url

                if media_url and media_url.startswith(("http://", "https://")):
                    response = requests.get(
                        media_url,
                        auth=(
                            current_app.config.get("TWILIO_ACCOUNT_SID"),
                            current_app.config.get("TWILIO_AUTH_TOKEN"),
                        ),
                    )
                    response.raise_for_status()
                    file_content = response.content
                elif media_url:
                    local_path = os.path.join(
                        current_app.root_path, media_url.lstrip("/")
                    )
                    if os.path.exists(local_path):
                        with open(local_path, "rb") as f:
                            file_content = f.read()
                        base = current_app.config.get("APP_PUBLIC_BASE_URL")
                        if base and base.startswith(("http://", "https://")):
                            final_url = base.rstrip("/") + media_url
                            uploaded_file_info.setdefault("public_url", final_url)
                    else:
                        base = current_app.config.get("APP_PUBLIC_BASE_URL")
                        if base and base.startswith(("http://", "https://")):
                            final_url = base.rstrip("/") + media_url
                            response = requests.get(
                                final_url,
                                auth=(
                                    current_app.config.get("TWILIO_ACCOUNT_SID"),
                                    current_app.config.get("TWILIO_AUTH_TOKEN"),
                                ),
                            )
                            response.raise_for_status()
                            file_content = response.content
                            uploaded_file_info["public_url"] = final_url
                        else:
                            raise FileNotFoundError(local_path)
                else:
                    raise ValueError("Media URL no válida")

                filename = uploaded_file_info.get("filename", "").lower()
                document_mime_types = {
                    "application/pdf",
                    "application/msword",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "text/plain",
                }

                if media_content_type and media_content_type.startswith("audio/"):
                    transcribed_text = uploaded_file_info.get("transcribed_text")
                    if not transcribed_text and final_url:
                        from services.audio_transcription_service import (
                            transcribe_audio_from_url,
                        )

                        transcribed_text = transcribe_audio_from_url(
                            final_url,
                            current_app.config.get("TWILIO_ACCOUNT_SID"),
                            current_app.config.get("TWILIO_AUTH_TOKEN"),
                        )
                        if transcribed_text:
                            uploaded_file_info["transcribed_text"] = transcribed_text
                    datos_interpretados_de_archivo = {
                        "transcribed_text": transcribed_text or ""
                    }
                elif media_content_type and media_content_type.startswith("image/"):
                    kwargs["es_foto"] = True
                    kwargs["foto_url"] = final_url
                    datos_interpretados_de_archivo = interpretar_imagen_para_chat(
                        archivo_adjunto=uploaded_file_info,
                        tipo_interpretacion="reclamo_auto_descripcion_categoria",
                    )
                elif (
                    (media_content_type in document_mime_types)
                    or filename.endswith((".pdf", ".doc", ".docx", ".txt", ".rtf"))
                ):
                    doc_ai_result = document_processing_service.process_document(
                        file_content, media_content_type
                    )
                    if doc_ai_result.get("success"):
                        datos_interpretados_de_archivo = {
                            "texto_extraido": doc_ai_result.get("text", "")
                        }
                    else:
                        datos_interpretados_de_archivo = {
                            "error": "No se pudo procesar el documento."
                        }
                else:
                    datos_interpretados_de_archivo = {
                        "error": "Tipo de archivo no soportado."
                    }
            except (requests.exceptions.RequestException, OSError, ValueError) as e:
                current_app.logger.error(
                    f"Error descargando archivo de WhatsApp: {e}"
                )
                datos_interpretados_de_archivo = {
                    "error": "No se pudo descargar el archivo."
                }

    archivo_id_para_asociar_al_ticket = (
        ids_archivos_para_asociar[-1] if ids_archivos_para_asociar else None
    )

    # Actualizar kwargs para pasar la información a los handlers específicos
    kwargs["datos_interpretados_archivo"] = datos_interpretados_de_archivo
    kwargs["archivo_id_para_asociar"] = archivo_id_para_asociar_al_ticket
    kwargs["ids_archivos_para_asociar"] = ids_archivos_para_asociar
    kwargs["procesamiento_archivo_en_curso"] = procesamiento_archivo_en_curso

    if "uploaded_file_info" in kwargs:  # Limpiar para no pasarlo si ya se usó.
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

    response = None
    if tipo_chat == "municipio":
        response = responder_municipio(
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
        response = responder_pyme(
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
        response = {"respuesta": "Error interno: tipo de chat no configurado correctamente.", "fuente": "sistema_error"}

    # Some handlers may return ``(payload, extras)`` or similar sequences.
    # Normalize to separate the payload (dict) from optional extras so the rest
    # of the function can safely operate on ``response_data`` without errors.
    if isinstance(response, (tuple, list)):
        response_data = response[0] or {}
        extras = response[1] if len(response) > 1 else {}
    else:
        response_data = response or {}
        extras = {}
    if not isinstance(response_data, dict):
        response_data = {}

    context_data = chat_db_context.context_data if chat_db_context else {}
    audio_requested = False
    if channel == "whatsapp":
        normalized_q = normalizar_texto(str(pregunta))
        if normalized_q == "audio on":
            if chat_db_context:
                preferences.set_audio_enabled(context_data, True)
                safe_flag_modified(chat_db_context, "context_data")
            if current_user is not None:
                current_user.prefers_audio = True
                db.session.add(current_user)
                db.session.commit()
            return {"message_body": "🔊 Activé los audios.", "message_type": "text"}
        if normalized_q == "audio off":
            if chat_db_context:
                preferences.set_audio_enabled(context_data, False)
                safe_flag_modified(chat_db_context, "context_data")
            if current_user is not None:
                current_user.prefers_audio = False
                db.session.add(current_user)
                db.session.commit()
            return {"message_body": "📝 Desactivé los audios.", "message_type": "text"}
        if any(
            phrase in normalized_q
            for phrase in ["mandamelo en audio", "manda en audio", "no puedo leer"]
        ):
            audio_requested = True
            if chat_db_context:
                preferences.set_audio_enabled(context_data, True)
                safe_flag_modified(chat_db_context, "context_data")
        elif "audio" in normalized_q or "escuchar" in normalized_q:
            audio_requested = True
    tts_forced = context_data.get("tts_forced")
    policy = os.getenv("WHATSAPP_TTS_POLICY", "auto").lower()
    generar_audio = False
    reason = None
    if isinstance(response_data, dict):
        long_msg = len(
            (response_data.get('message_body') or '')
            + (response_data.get('message_to_user') or '')
        ) > 150
        is_menu = response_data.get('message_type') in {'interactive_buttons', 'options_list'}
        pref_audio = preferences.is_audio_enabled(context_data, user=current_user)
        if policy == 'always':
            generar_audio = True
            reason = 'policy'
        elif policy == 'off':
            if audio_requested or tts_forced:
                generar_audio = True
                reason = 'pedido_usuario' if audio_requested else 'forzado'
        else:  # auto
            if channel == 'whatsapp':
                if audio_requested or tts_forced:
                    generar_audio = True
                    reason = 'pedido_usuario' if audio_requested else 'forzado'
            else:
                if tts_forced:
                    generar_audio = True
                    reason = 'forzado'
                elif audio_requested:
                    generar_audio = True
                    reason = 'pedido_usuario'
                elif pref_audio:
                    generar_audio = True
                    reason = 'preferencia'
                elif long_msg and not is_menu:
                    generar_audio = True
                    reason = 'largo'
                elif response_data.get('es_confirmacion_final'):
                    generar_audio = True
                    reason = 'confirmacion'
        if is_menu and not pref_audio and reason not in {'pedido_usuario', 'forzado'}:
            generar_audio = False
            reason = None
        response_data['generar_audio'] = generar_audio
        if generar_audio:
            logger.info(f"tts_sent=true reason={reason}")
        else:
            logger.info("tts_sent=false")

    # --- Audio Response Generation ---
    if isinstance(response_data, dict) and response_data.get('generar_audio'):
        text_to_speak = response_data.get('audio_text')
        base_text = response_data.get('message_body') or response_data.get('message_to_user', '')
        if not text_to_speak:
            base_text = response_data.get('message_body') or response_data.get('message_to_user', '')
            text_to_speak = render_audio_text(
                base_text,
                response_data.get('options_list'),
                response_data.get('categorias'),
            )
        elif base_text:
            # Ensure provided audio_text is prefixed with the conversational summary
            text_to_speak = f"{base_text}\n{text_to_speak}"
        if text_to_speak:
            from services.tts_orchestrator import generar_audio_con_fallback
            audio_url = generar_audio_con_fallback(
                text_to_speak,
                channel=channel,
                allow_if_policy_off=policy == 'off',
            )
            if audio_url:
                response_data['audio_url'] = audio_url
                logger.info(f"Generated audio response at {audio_url}")

    if chat_db_context and chat_db_context.context_data:
        chat_db_context.context_data.pop('source_is_audio', None)
    if response_data:
        response_data.pop('generar_audio', None)

    return response_data
