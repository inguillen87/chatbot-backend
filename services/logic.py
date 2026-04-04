import sys
import os
import logging

# Add project root to sys.path for this service file
project_root_logic = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_logic not in sys.path:
    sys.path.insert(0, project_root_logic)

from flask import current_app
from models import db, ArchivoAdjunto
from services.interpretacion_service import interpretacion_service
from services.archivo_service import archivo_service
# servicio_tickets se importa/usa en los handlers específicos (municipios.py, pymes.py)
logger = logging.getLogger(__name__)

# Rubros que deben usar la lógica de municipio/ente público
RUBROS_PUBLICOS = {
    "municipio",
    "municipios",
    "municipio inteligente",
    "ong",
    "gobierno",
    "hospital_publico",
    "entidad_publica",
    "municipal",
    "publico",
    "municipalidad",
    # Agregá acá los que consideres públicos
}

MENU_KEYWORDS = {
    "ver_estado_reclamo": {"estado", "reclamo", "seguimiento"},
    "iniciar_reclamo": {"iniciar", "nuevo", "hacer"},
    "cancelar_reclamo": {"cancelar", "anular"},
    "hablar_con_agente": {"agente", "hablar", "asesor", "representante", "humano"},
    "consultar_otro_reclamo": {"otro", "consultar"},
    "finalizar_conversacion": {"finalizar", "terminar", "chau", "adios"},
    "menu_principal": {"menu", "principal", "inicio"},
    "consultar_deuda": {"deuda", "pagar", "factura"},
    "consultar_licencia": {"licencia", "conducir", "registro"},
    "consultar_transporte": {"transporte", "colectivo", "sube"},
    "consultar_eventos": {"eventos", "agenda", "actividades"},
    "consultar_noticias": {"noticias", "novedades", "informacion"},
    "consultar_tramites": {"tramites", "tramite", "gestiones"},
    "consultar_servicios": {"servicios", "servicio"},
    "consultar_turismo": {"turismo", "visitar", "pasear"},
    "consultar_salud": {"salud", "hospital", "emergencia"},
    "consultar_educacion": {"educacion", "escuelas", "cursos"},
    "consultar_trabajo": {"trabajo", "empleo", "buscar"},
    "consultar_mascotas": {"mascotas", "perros", "gatos"},
    "consultar_ambiente": {"ambiente", "verde", "ecologia"},
    "consultar_cultura": {"cultura", "arte", "museos"},
    "consultar_deportes": {"deportes", "ejercicio", "gimnasio"},
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


from services.demo_response_engine import maybe_handle_demo_interaction
from services.llm_utils import clasificar_entidad_con_llm
from services.response_formatter import render_audio_text
from services.constants import CONTEXTO_MUNICIPIO

# PROMPT_CLASIFICACION_INTENCION y _clasificar_intencion_con_llm han sido eliminados.
# La clasificación de intención ahora es responsabilidad de llamar_llm_con_fallback con JULES_SYSTEM_PROMPT.

# ... otras funciones que ya tengas en logic.py (como responder_chatboc)
from utils.db_utils import safe_flag_modified
from utils.response_utils import normalize_response_payload
from services.catalog_share import maybe_handle_catalog_share

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

    catalog_share_response = maybe_handle_catalog_share(
        pregunta=pregunta,
        owner_user=effective_owner_user,
        channel=channel,
    )
    if catalog_share_response:
        return catalog_share_response

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

    # Si el rubro indica un tipo específico de lógica, lo usamos para inferir cuando no viene explícito
    if rubro_nombre and tipo_chat not in ("municipio", "pyme"):
        tipo_chat = "municipio" if es_rubro_publico(rubro_obj or rubro_nombre) else "pyme"
    elif not rubro_nombre and tipo_chat not in ("municipio", "pyme"):
        raise ValueError(f"Tipo de chat inválido: {tipo_chat}")

    # --- INICIO: Manejo de confusión Pyme/Municipio ---
    pregunta_text_check = pregunta if isinstance(pregunta, str) else pregunta.get("pregunta", "")
    pregunta_norm_check = normalizar_texto(pregunta_text_check)
    skip_confusion_check = any(k in pregunta_norm_check for k in ["catalogo", "catálogo", "carrito", "comprar", "pedido", "producto", "precio"])

    if tipo_chat == "pyme" and not kwargs.get("demo_metadata") and not skip_confusion_check:
        # Check for municipal keywords in the user's query
        for action, keywords in MENU_KEYWORDS.items():
            if any(keyword in pregunta_norm_check for keyword in keywords):
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
    skip_image_analysis = kwargs.pop("skip_media_analysis", False)
    archivo_id_para_asociar_al_ticket = None
    procesamiento_archivo_en_curso = False # Nueva bandera

    if uploaded_file_info and isinstance(uploaded_file_info, dict):
        logger.info(
            f"DEBUG: Processing uploaded_file_info in responder_chatboc: {uploaded_file_info}"
        )
        if uploaded_file_info.get("id"):
            archivo_id = uploaded_file_info.get("id")
            current_app.logger.info(
                f"[LOGIC] Procesando uploaded_file_info para ArchivoAdjunto ID: {archivo_id}"
            )
            archivo_id_para_asociar_al_ticket = archivo_id

            mime_type = uploaded_file_info.get("mime_type") or uploaded_file_info.get(
                "mimeType"
            )
            mime_type = str(mime_type) if mime_type else ""
            file_url = uploaded_file_info.get("url")

            archivo_obj = None
            if archivo_id:
                archivo_obj = db.session.get(ArchivoAdjunto, archivo_id)
                if not archivo_obj:
                    current_app.logger.warning(
                        "[LOGIC] ArchivoAdjunto ID %s no encontrado en la base de datos.",
                        archivo_id,
                    )
                else:
                    if not file_url:
                        file_url = archivo_obj.url
                    if not mime_type and archivo_obj.mime:
                        mime_type = str(archivo_obj.mime)

            if file_url:
                kwargs.setdefault("es_archivo", True)
                kwargs.setdefault("archivo_url", file_url)

            if file_url and mime_type.startswith("image/"):
                kwargs.setdefault("es_foto", True)
                kwargs.setdefault("foto_url", file_url)
                if not skip_image_analysis:
                    try:
                        from services.interpretacion_imagen_service import (
                            interpretar_imagen_para_chat,
                        )

                        analisis_objetivo = archivo_obj or uploaded_file_info
                        analisis_resultado = interpretar_imagen_para_chat(
                            archivo_adjunto=analisis_objetivo,
                            tipo_interpretacion="reclamo_auto_descripcion_categoria",
                        )
                        if isinstance(analisis_resultado, dict):
                            if not isinstance(datos_interpretados_de_archivo, dict):
                                datos_interpretados_de_archivo = {}
                            datos_interpretados_de_archivo.update(analisis_resultado)
                        else:
                            datos_interpretados_de_archivo = analisis_resultado
                    except Exception as exc:  # pragma: no cover - logged for observability
                        current_app.logger.error(
                            "Error interpretando imagen para ArchivoAdjunto ID %s: %s",
                            archivo_id,
                            exc,
                            exc_info=True,
                        )
            elif file_url and mime_type.startswith("audio/"):
                kwargs.setdefault("es_audio", True)
                from services.audio_transcription_service import transcribe_audio_from_url

                # Check if we already have the transcript (e.g. from WhatsApp/Twilio metadata)
                transcript = uploaded_file_info.get("transcribed_text")

                if not transcript:
                    try:
                        transcript = transcribe_audio_from_url(file_url, mime_type)
                    except Exception as e:
                        logger.error(f"Error inesperado durante la transcripción de audio web: {e}", exc_info=True)

                if transcript:
                    # This is the key change: pass the transcript in the same way WhatsApp does,
                    # not just by prepending it to the 'pregunta'.
                    # The 'pregunta' for an audio message should be the transcript itself.
                    pregunta = transcript

                    # Ensure the info dictionary is correctly populated for downstream processing
                    uploaded_file_info["transcribed_text"] = transcript
                    if not isinstance(datos_interpretados_de_archivo, dict):
                        datos_interpretados_de_archivo = {}
                    datos_interpretados_de_archivo["texto_transcrito"] = transcript

                    current_app.logger.info(
                        "Transcripción de audio web completada para ArchivoAdjunto ID %s. Texto: '%s'",
                        archivo_id,
                        transcript[:100]
                    )
                else:
                    current_app.logger.warning(
                        "La transcripción de audio web no produjo texto para ArchivoAdjunto ID %s. Se enviará un mensaje de error.",
                        archivo_id,
                    )
                    # Return a user-friendly error if transcription fails for any reason
                    return {
                        "message_body": "No pude entender el audio que enviaste. ¿Podrías intentarlo de nuevo o escribir tu consulta?",
                        "fuente": "web_transcription_failed",
                    }

        elif uploaded_file_info.get("source") == "whatsapp":
            from services.document_processing_service import document_processing_service
            from services.interpretacion_imagen_service import interpretar_imagen_para_chat
            import requests

            media_url = uploaded_file_info.get("url")
            media_content_type = uploaded_file_info.get("mime_type")

            # Expose basic photo metadata downstream so municipal handlers know a
            # picture was already provided. This allows the claim flow to reuse the
            # initial image instead of prompting for another one after location is
            # sent.
            skip_image_analysis = False
            if media_content_type and media_content_type.startswith("image/"):
                kwargs["es_foto"] = True
                stored_url = uploaded_file_info.get("url") if uploaded_file_info else None
                kwargs["foto_url"] = stored_url or media_url
                if (
                    not skip_image_analysis
                    and chat_db_context
                    and chat_db_context.context_data
                ):
                    muni_ctx = chat_db_context.context_data.get(CONTEXTO_MUNICIPIO, {})
                    if muni_ctx.get("reclamo_flow_v2", {}).get("state"):
                        skip_image_analysis = True

            if not skip_image_analysis:
                try:
                    response = requests.get(
                        media_url,
                        auth=(
                            current_app.config.get("TWILIO_ACCOUNT_SID"),
                            current_app.config.get("TWILIO_AUTH_TOKEN"),
                        ),
                    )
                    response.raise_for_status()
                    file_content = response.content

                    if media_content_type.startswith("image/"):
                        datos_interpretados_de_archivo = interpretar_imagen_para_chat(
                            archivo_adjunto=uploaded_file_info,
                            tipo_interpretacion="reclamo_auto_descripcion_categoria",
                        )
                    else:
                        doc_ai_result = document_processing_service.process_document(
                            file_content, media_content_type
                        )
                        if doc_ai_result:
                            # Aquí puedes procesar el resultado de Document AI
                            # Por ahora, solo extraemos el texto
                            datos_interpretados_de_archivo = {"texto_extraido": doc_ai_result.text}
                        else:
                            datos_interpretados_de_archivo = {"error": "No se pudo procesar el documento."}
                except requests.exceptions.RequestException as e:
                    current_app.logger.error(f"Error descargando archivo de WhatsApp: {e}")
                    datos_interpretados_de_archivo = {"error": "No se pudo descargar el archivo."}
            else:
                datos_interpretados_de_archivo = {}

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

    demo_metadata = kwargs.get("demo_metadata")

    response_data = None
    if demo_metadata:
        action_from_payload = None
        if isinstance(pregunta, dict):
            action_from_payload = pregunta.get("action") or pregunta.get("action_id")

        response_data = maybe_handle_demo_interaction(
            pregunta=pregunta,
            tipo_chat=tipo_chat,
            demo_metadata=demo_metadata,
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            channel=channel,
            chat_db_context=chat_db_context,
            action_id=action_from_payload,
        )

        if response_data is None:
            quick_actions = demo_metadata.get("quick_actions") or []
            message_type = "interactive_list" if len(quick_actions) > 3 else "interactive_buttons"
            response_data = {
                "message_body": demo_metadata.get(
                    "welcome_message",
                )
                or "Estás en la demo interactiva. Elegí una opción para continuar.",
                "options_list": quick_actions,
                "botones": quick_actions,
                "message_type": message_type,
                "fuente": "demo_offline",
                "skip_audio_generation": True,
            }

    if demo_metadata and tipo_chat == "municipio" and response_data is not None:
        return response_data

    if tipo_chat == "municipio":
        if response_data is None:
            from services import municipio_responder as municipio_responder_module

            response_data = municipio_responder_module.responder_municipio(
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
        if response_data is None:
            from services import pymes as pymes_module

            response_data = pymes_module.responder_pyme(
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

    if (
        isinstance(response_data, dict)
        and response_data.get("fuente") == "pyme_municipio_confusion_handler"
    ):
        quick_actions = (demo_metadata or {}).get("quick_actions") or []
        message_type = "interactive_list" if len(quick_actions) > 3 else "interactive_buttons"
        response_data = {
            "message_body": (demo_metadata or {}).get("welcome_message")
            or "Estás en la demo interactiva. Elegí una opción para continuar.",
            "options_list": quick_actions,
            "botones": quick_actions,
            "message_type": message_type,
            "fuente": "demo_offline",
            "skip_audio_generation": True,
        }

    if isinstance(response_data, dict):
        fuente_val = str(response_data.get("fuente") or "").strip().lower()
        if fuente_val.startswith("demo_") or response_data.get("demo_selector_mode"):
            response_data["skip_audio_generation"] = True

    # Always enable audio responses for accessibility
    context_data = chat_db_context.context_data if chat_db_context else {}
    if (
        isinstance(response_data, dict)
        and not response_data.get('generar_audio')
        and not response_data.get('skip_audio_generation')
    ):
        response_data['generar_audio'] = True

    # --- Audio Response Generation ---
    if (
        response_data
        and response_data.get('generar_audio')
        and not response_data.get('audio_url')
        and not response_data.get('skip_audio_generation')
    ):
        text_to_speak = response_data.get('audio_text')
        if not text_to_speak:
            options_for_audio = (
                response_data.get('options_list')
                or response_data.get('botones')
            )
            text_to_speak = render_audio_text(
                response_data.get('message_body', ''),
                options=options_for_audio,
                categorias=response_data.get('categorias'),
                datos=response_data.get('datos_estructura'),
                accion=response_data.get('accion_backend'),
            )
        if text_to_speak:
            from services.tts_orchestrator import generar_audio
            tts_speed = response_data.get("tts_speed")
            try:
                tts_speed = float(tts_speed) if tts_speed is not None else None
            except (TypeError, ValueError):
                tts_speed = None

            audio_url = generar_audio(
                text_to_speak,
                voice=response_data.get("tts_voice"),
                model=response_data.get("tts_model"),
                style=response_data.get("tts_style"),
                speed=tts_speed,
                cache_namespace=response_data.get("tts_cache_namespace"),
            )
            if audio_url:
                response_data['audio_url'] = audio_url
                logger.info(f"Generated audio response at {audio_url}")

    if isinstance(response_data, dict) and response_data.get("fuente") == "pyme_municipio_confusion_handler":
        quick_actions = (demo_metadata or {}).get("quick_actions") or []
        message_type = "interactive_list" if len(quick_actions) > 3 else "interactive_buttons"
        response_data = {
            "message_body": (demo_metadata or {}).get("welcome_message")
            or "Estás en la demo interactiva. Elegí una opción para continuar.",
            "options_list": quick_actions,
            "botones": quick_actions,
            "message_type": message_type,
            "fuente": "demo_offline",
            "skip_audio_generation": True,
        }

    if chat_db_context and chat_db_context.context_data:
        chat_db_context.context_data.pop('source_is_audio', None)
    if response_data:
        response_data.pop('generar_audio', None)
        response_data.pop('skip_audio_generation', None)

    if demo_metadata and tipo_chat == "municipio":
        response_data = response_data or {
            "message_body": (demo_metadata or {}).get("welcome_message")
            or "Estás en la demo interactiva. Elegí una opción para continuar.",
            "options_list": (demo_metadata or {}).get("quick_actions") or [],
            "botones": (demo_metadata or {}).get("quick_actions") or [],
            "message_type": "interactive_buttons",
        }
        if isinstance(response_data, dict):
            response_data["fuente"] = "demo_offline"


    if isinstance(response_data, dict):
        normalize_response_payload(response_data)

    return response_data
