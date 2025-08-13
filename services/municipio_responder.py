import os
from flask import jsonify
import pandas as pd
from geopy.geocoders import GoogleV3
from geopy.exc import GeocoderTimedOut, GeocoderServiceError
from dotenv import load_dotenv
import sys
import logging
import re
import json
from enum import Enum, auto
import unicodedata
import difflib
from flask import current_app, has_app_context, session as flask_session
from sqlalchemy.orm.attributes import flag_modified
from models import MunicipioTicket, TicketComentario, db, SitioWebInfo, Conversacion
from services.ticket_service import servicio_tickets
from twilio.rest import Client
from datetime import datetime, timedelta
from services.utils_placeholders import (
    reemplazar_placeholders,
    obtener_respuesta_municipio,
)
from services.config_loader import cargar_configuracion_municipio
from .actions.municipio_actions import (
    CrearReclamoActionHandler,
)
from .herramientas_municipio import (
    consultar_recoleccion_por_direccion,
    categorizar_reclamo_por_palabra_clave,
    sugerir_categorias_relevantes,
    parse_direccion_completa,
    normalizar_texto,
    direccion_es_valida,
    TOOL_REGISTRY,
    KEYWORD_TO_CATEGORY_MAP,
)
from .categorias_municipio import CATEGORIAS_RECLAMO, categorias_normalizadas
from .common_utils import (
    validar_email,
    validar_telefono,
    formatear_telefono_e164,
    construir_respuesta_sugerir_registro
)
from .llm_utils import extract_complaint_details_llm, extract_multiple_contact_details_llm
import math
from services.tasks import process_image_for_chat_task
from services.nlu.router import route as nlu_route
from services.flows import reclamos as reclamos_flow, tramites, noticias, menu, smalltalk
from services.ui import render_whatsapp
from services.google_text_to_speech import TextToSpeechService

load_dotenv()

logger = logging.getLogger(__name__)

CONTEXTO_MUNICIPIO = "contexto_municipio_v2"

CONTEXTO_MUNICIPIO = "contexto_municipio_v2"

from enum import Enum, auto

class ConversationState(Enum):
    ESPERANDO_CONFIRMACION_CIERRE = auto()
    ESPERANDO_CALIFICACION = auto()
    ESPERANDO_NUMERO_TICKET = auto()
    ESPERANDO_PARAM_RECOLECCION = auto()
    ESPERANDO_CATEGORIA_RECLAMO = auto()
    ESPERANDO_DIRECCION_RECLAMO = auto()
    ESPERANDO_NOMBRE_VECINO = auto()
    ESPERANDO_TELEFONO_VECINO = auto()
    ESPERANDO_EMAIL_VECINO = auto()
    ESPERANDO_DESCRIPCION_RECLAMO = auto()
    ESPERANDO_ADJUNTOS_RECLAMO = auto()
    ESPERANDO_CONFIRMACION_RECLAMO = auto()
    ESPERANDO_SELECCION_TRAMITE = auto()
    ESPERANDO_PREGUNTA_CURSO_LICENCIA = auto()
    ESPERANDO_TEXTO_SUGERENCIA = auto()
    ESPERANDO_PRODUCTO_PARA_CONSULTA = auto()
    MOSTRANDO_PRODUCTOS = auto()
    ESPERANDO_CONFIRMACION_AGREGAR_CARRITO = auto()
    ESPERANDO_OPCION_CARRITO = auto()
    ESPERANDO_DETALLES_CHECKOUT = auto()
    ESPERANDO_CONFIRMACION_PEDIDO = auto()
    ESPERANDO_UBICACION_PANICO = auto()
    ESPERANDO_INFO_RECLAMO_LLM = auto()
    CONVERSACION_GENERAL_LLM = auto()
    ESPERANDO_CONFIRMACION_INICIAR_RECLAMO = auto()
    ESPERANDO_CREACION_TICKET = auto()
    ESPERANDO_CONFIRMACION_UBICACION = auto()
    ESPERANDO_CONSULTA_GENERAL = auto()
    ESPERANDO_SELECCION_MENU_PRINCIPAL = auto()
    ESPERANDO_SELECCION_MENU_RECLAMOS = auto()
    ESPERANDO_SELECCION_DE_LISTA = auto()
    ESPERANDO_UBICACION_GENERAL = auto()
    ESPERANDO_CONFIRMACION_STT = auto()

def responder_municipio(
    pregunta_original,
    owner_user,
    rubro_obj,
    viewer_user=None,
    chat_db_context=None,
    anon_id=None,
    channel: str = "web",
    location=None,
    **kwargs
    ):

    estado_conversacion = None
    # 1. Get the raw text from the payload
    if chat_db_context and chat_db_context.context_data is None:
        chat_db_context.context_data = {}

    if isinstance(pregunta_original, dict) and pregunta_original.get('media_url'):
        from .audio_transcription_service import transcribe_audio_from_url
        transcription_result = transcribe_audio_from_url(
            pregunta_original['media_url'],
            os.getenv("TWILIO_ACCOUNT_SID"),
            os.getenv("TWILIO_AUTH_TOKEN")
        )
        if transcription_result and transcription_result.get('transcript'):
            if transcription_result.get('confidence', 1.0) < 0.8:
                # Low confidence, ask for confirmation
                chat_db_context.context_data['stt_transcript'] = transcription_result['transcript']
                chat_db_context.context_data['estado_conversacion'] = 'ESPERANDO_CONFIRMACION_STT'
                return {
                    "message_body": f"Escuché: \"{transcription_result['transcript']}\". ¿Es correcto?",
                    "options_list": [{"texto": "Sí"}, {"texto": "No"}],
                    "message_type": "interactive_buttons"
                }
            else:
                msg = transcription_result['transcript']
        else:
            msg = "" # Could not transcribe
    elif isinstance(pregunta_original, dict):
        msg = pregunta_original.get("pregunta", "")
    else:
        msg = pregunta_original

    # Auto-learn audio preference
    if msg.lower().strip() == 'solo texto':
        if viewer_user:
            viewer_user.prefers_audio = False
            db.session.commit()
        return {"message_body": "Entendido. A partir de ahora, solo te enviaré mensajes de texto."}

    is_audio_message = isinstance(pregunta_original, dict) and pregunta_original.get('media_url')

    if is_audio_message:
        audio_counter = chat_db_context.context_data.get('audio_counter', 0) + 1
        chat_db_context.context_data['audio_counter'] = audio_counter
        if audio_counter >= 2 and viewer_user and not viewer_user.prefers_audio:
            viewer_user.prefers_audio = True
            db.session.commit()
    else:
        chat_db_context.context_data['audio_counter'] = 0

    # Handle STT confirmation state
    if chat_db_context and chat_db_context.context_data.get('estado_conversacion') == 'ESPERANDO_CONFIRMACION_STT':
        if msg.lower() == 'sí' or msg.lower() == 'si':
            msg = chat_db_context.context_data.pop('stt_transcript', '')
            chat_db_context.context_data['estado_conversacion'] = None
        else:
            chat_db_context.context_data['estado_conversacion'] = None
            return {
                "message_body": "Entendido. Por favor, escribí tu consulta.",
                "message_type": "text",
                "options_list": []
            }

    # 2. Handle contextual input (e.g., replying to a menu)
    intent = None
    context_state = chat_db_context.context_data.get('estado_conversacion')

    if context_state == 'ESPERANDO_SELECCION_DE_LISTA' and msg.isdigit():
        num_seleccionado = int(msg)
        last_options = chat_db_context.context_data.get('last_options', [])
        if 0 < num_seleccionado <= len(last_options):
            # Map number to action_id
            intent = last_options[num_seleccionado - 1].get('key')
            logger.info(f"Intent resolved from context menu selection: '{intent}'")
            # Clear state after using it
            chat_db_context.context_data['estado_conversacion'] = None
            chat_db_context.context_data['last_options'] = None

    # 3. If no intent from context, use NLU router
    if not intent:
        intent = nlu_route(msg)
        logger.info(f"NLU intent: {intent}")

    # 4. Handle intent with flows
    flow_context = kwargs.copy()
    flow_context['phone'] = anon_id
    flow_context['profile_name'] = kwargs.get('profile_name')
    flow_context['viewer_user_obj'] = viewer_user
    flow_context['user_obj'] = owner_user
    flow_context['channel'] = channel


    # Route intent to the corresponding flow handler
    if intent == "iniciar_reclamo":
        payload = reclamos_flow.handle(msg, flow_context)
    elif intent in ["consultar_tramites", "tramite_licencia", "pagar_tasas", "veterinaria_bromatologia", "defensa_consumidor", "realizar_denuncia", "solicitar_turnos"]:
        # For now, route all procedural intents to the main tramites_flow
        # A more specific flow could be created for each one later.
        logger.info(f"Routing intent '{intent}' to tramites_flow.")
        payload = tramites.handle(msg, flow_context)
    elif intent in ["agenda_cultural", "ultimas_novedades"]:
        logger.info(f"Routing intent '{intent}' to noticias_flow.")
        payload = noticias.handle(msg, flow_context)
    elif intent == "mostrar_menu":
        payload = menu.handle(msg, flow_context)
    else:
        # Fallback to smalltalk/general LLM for unhandled intents
        logger.info(f"Intent '{intent}' not explicitly handled, using smalltalk/LLM fallback.")
        payload = smalltalk.handle(msg, flow_context)

    logger.info(f"Flow payload: {payload}")
    # 4. Post-flow processing: save context for menus
    if payload.get("type") == "menu" and payload.get("data", {}).get("items"):
        chat_db_context.context_data['last_options'] = payload["data"]["items"]
        chat_db_context.context_data['estado_conversacion'] = 'ESPERANDO_SELECCION_DE_LISTA'
        logger.info("Saved menu options to context and set state to ESPERANDO_SELECCION_DE_LISTA")

    # 5. Render the payload to a WhatsApp message
    response_text = render_whatsapp.render(payload)

    # 6. Generate audio response
    tts_service = TextToSpeechService()
    audio_url = None
    if (viewer_user and viewer_user.prefers_audio) or (isinstance(pregunta_original, dict) and pregunta_original.get('media_url')):
        audio_url = tts_service.synthesize_speech(response_text)

    final_response = {
        "message_body": response_text,
        "options_list": payload.get("options_list", []),
        "message_type": payload.get("message_type", "text"),
        "fuente": "new_pipeline"
    }

    if audio_url:
        if tts_service.is_cached(response_text):
            final_response["audio_url"] = audio_url
        else:
            final_response["action"] = "send_preliminary_message_then_audio"
            final_response["preliminary_message"] = "Enviando audio..."
            final_response["audio_url"] = audio_url

    return final_response
