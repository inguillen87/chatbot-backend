import sys
import os
import logging
import re

project_root_municipios_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_municipios_svc not in sys.path:
    sys.path.insert(0, project_root_municipios_svc)
import json
import os
from enum import Enum, auto
import unicodedata
import difflib
from flask import current_app, has_app_context  # Ensure current_app is imported directly
from sqlalchemy.orm.attributes import flag_modified # Import for flag_modified
from models import MunicipioTicket, TicketComentario, db, SitioWebInfo, Conversacion # Added Conversacion
from services.cohere_ai import get_cohere_response
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

try:
    from flask import current_app, session as flask_session, has_app_context
except ImportError:
    current_app = None
    flask_session = {}

logger = logging.getLogger(__name__)

CONTEXTO_MUNICIPIO = "contexto_municipio_v2"

class ConversationState(Enum):
    ESPERANDO_INFO_RECLAMO_LLM = auto()
    CONVERSACION_GENERAL_LLM = auto()
    ESPERANDO_CONFIRMACION_UBICACION = auto()

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
    logger.info(f"--- START responder_municipio ---")
    logger.info(f"Pregunta: {pregunta_original}")
    logger.info(f"Owner user: {owner_user}")
    logger.info(f"Viewer user: {viewer_user}")
    logger.info(f"Anonymous ID: {anon_id}")
    logger.info(f"Channel: {channel}")
    logger.info(f"Location: {location}")

    contexto_municipio = chat_db_context.context_data.get(CONTEXTO_MUNICIPIO, {})

    if not isinstance(contexto_municipio, dict):
        contexto_municipio = {}

    estado_conversacion = contexto_municipio.get("estado_conversacion")
    logger.info(f"Estado de conversacion: {estado_conversacion}")

    if estado_conversacion == ConversationState.ESPERANDO_CONFIRMACION_UBICACION.name:
        if "si" in pregunta_original.lower():
            contexto_municipio["ubicacion_confirmada"] = True
            contexto_municipio["estado_conversacion"] = None
        else:
            contexto_municipio["ubicacion_confirmada"] = False
            contexto_municipio["estado_conversacion"] = ConversationState.ESPERANDO_DIRECCION_RECLAMO.name
            return {
                "message_body": "Por favor, decime la nueva ubicación.",
                "options_list": [],
                "message_type": "text",
                "fuente": "pedir_nueva_ubicacion"
            }

    # Lógica simplificada
    from services.gemini_bridge import llamar_gemini
    from .chat_orchestrator import ChatOrchestrator

    usuario_info_for_gemini = {
        "nombre": getattr(viewer_user, "name", "Vecino/a"),
        "tipo_entidad": "municipio",
    }

    # Añadir ubicación si se conoce
    loc_usuario_texto = getattr(viewer_user, "direccion", None) or contexto_municipio.get("direccion_reclamo")
    if loc_usuario_texto:
        usuario_info_for_gemini["ubicacion_conocida"] = loc_usuario_texto
        if not contexto_municipio.get("ubicacion_confirmada"):
            contexto_municipio["estado_conversacion"] = ConversationState.ESPERANDO_CONFIRMACION_UBICACION.name
            return {
                "message_body": f"Veo que tu ubicación registrada es {loc_usuario_texto}. ¿Querés que busque cerca de ahí?",
                "options_list": [{"texto": "Sí"}, {"texto": "No, usar otra ubicación"}],
                "message_type": "interactive_buttons",
                "fuente": "confirmacion_ubicacion"
            }

    historial_chat_para_gemini = contexto_municipio.get("historial_conversacion_general_llm", [])

    llm_response_structured = llamar_gemini(
        mensaje_usuario=pregunta_original,
        usuario=usuario_info_for_gemini,
        historial=historial_chat_para_gemini
    )

    contexto_municipio.setdefault("historial_conversacion_general_llm", []).append({"role": "user", "parts": [{"text": pregunta_original}]})
    contexto_municipio["historial_conversacion_general_llm"].append({"role": "model", "parts": [{"text": llm_response_structured.get("respuesta_usuario")}]})

    global_context_for_orchestrator = {
        CONTEXTO_MUNICIPIO: contexto_municipio,
        "user_obj": owner_user,
        "viewer_user_obj": viewer_user,
    }

    orchestrator = ChatOrchestrator(global_context=global_context_for_orchestrator)
    action_handler_result = orchestrator.execute_action(llm_response_structured)

    if action_handler_result is None:
        action_handler_result = {}

    respuesta_final_texto = action_handler_result.get("message_to_user") or llm_response_structured.get("respuesta_usuario", "No entendí, ¿podrías repetirlo?")
    opciones_finales = action_handler_result.get("options_list", [])

    final_response_dict = {
        "message_body": respuesta_final_texto,
        "options_list": opciones_finales,
        "message_type": "interactive_buttons" if opciones_finales else "text",
        "fuente": action_handler_result.get("fuente") or llm_response_structured.get("accion_backend", "municipio_general_v4"),
    }

    chat_db_context.context_data[CONTEXTO_MUNICIPIO] = contexto_municipio
    flag_modified(chat_db_context, "context_data")
    db.session.commit()

    logger.info(f"--- END responder_municipio ---")
    return final_response_dict
