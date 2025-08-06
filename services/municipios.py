import logging
from enum import Enum, auto
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timedelta
import json
import os
from services.google_maps_service import obtener_direccion_de_coordenadas
from services.logging_config import get_logger
from services.gemini_bridge import llamar_gemini
from services.herramientas_municipio import TOOL_REGISTRY
from services.chat_orchestrator import ChatOrchestrator, GreetingHandler, ConsultarInfoTramiteActionHandler, MenuPrincipalActionHandler, ErrorActionHandler
from models import User, MunicipioTicket, TicketComentario, SitioWebInfo, Conversacion, db
from sqlalchemy.orm.attributes import flag_modified
from utils.nlp_utils import encontrar_saludo, validar_telefono
import services.ticket_service as servicio_tickets

# (El resto del código que ya tienes)

# -- Adaptador de Respuesta --
def _adaptar_respuesta_para_compatibilidad(respuesta_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    Adapta la nueva estructura de respuesta a la antigua para mantener la compatibilidad
    con los tests y otros módulos que esperan 'respuesta_usuario'.
    """
    if not isinstance(respuesta_dict, dict):
        return {"respuesta_usuario": str(respuesta_dict)}

    if "respuesta_usuario" in respuesta_dict:
        return respuesta_dict

    # Nueva estructura esperada: {'success': bool, 'message_to_user': str, 'buttons': list, ...}
    # Antigua estructura: {'respuesta_usuario': str, 'botones': list, ...}

    # Si la respuesta ya tiene el formato antiguo, no la toques
    if 'message_to_user' not in respuesta_dict and 'message_body' not in respuesta_dict:
        # Asumimos que es un formato que no necesita adaptación o un error
        return respuesta_dict

    respuesta_adaptada = respuesta_dict.copy()

    # Mapeo principal
    respuesta_adaptada["respuesta_usuario"] = respuesta_dict.get("message_to_user") or respuesta_dict.get("message_body")

    # Mapeo de botones/opciones
    if "buttons" in respuesta_dict:
        respuesta_adaptada["botones"] = respuesta_dict["buttons"]
    elif "options_list" in respuesta_dict:
        respuesta_adaptada["botones"] = respuesta_dict["options_list"]

    # Eliminar las claves nuevas para no generar confusión en los módulos antiguos
    respuesta_adaptada.pop("message_to_user", None)
    respuesta_adaptada.pop("message_body", None)
    respuesta_adaptada.pop("success", None)
    respuesta_adaptada.pop("buttons", None)
    respuesta_adaptada.pop("options_list", None)

    return respuesta_adaptada

# (El resto de la función responder_municipio, pero al final, antes del return, se adapta la respuesta)

def responder_municipio(pregunta_original: any, owner_user: User, rubro_obj: Any, viewer_user: Optional[User], chat_db_context: Any, anon_id: str = None, chat_session_uuid: str = None, **kwargs) -> Dict[str, Any]:
    # ... (toda la lógica existente de la función)

    # Al final de la función, justo antes de cada `return`
    # Ejemplo de un return que ahora necesita adaptación:
    # return resultado_del_handler # Este es un dict con el nuevo formato

    # Lo envolvemos en el adaptador:
    # return _adaptar_respuesta_para_compatibilidad(resultado_del_handler)
    
    # Aplicando esto a toda la función...
    logger = get_logger(__name__)
    # ... (resto del código de la función)

    # Ejemplo de cómo se vería un return al final:
    # final_response = orchestrator.handle_message(...)
    # return _adaptar_respuesta_para_compatibilidad(final_response)

    # --- Código real de la función ---
    logger.info(f"[RESPONDER_MUNICIPIO_START] {'='*50}")
    # ... (el código sigue)

    # Y al final...
    # Supongamos que `final_result` es el diccionario que la función va a devolver
    final_result = {} # Reemplazar con la lógica real que genera la respuesta

    # --- Mock de la lógica para el ejemplo ---
    pregunta_str = pregunta_original.get("pregunta", "") if isinstance(pregunta_original, dict) else str(pregunta_original)

    if "hola" in pregunta_str:
        final_result = GreetingHandler(db.session).handle()
    elif "tramite" in pregunta_str:
        final_result = MenuPrincipalActionHandler(db.session).handle()
    else:
        # LLM Call
        llm_response = llamar_gemini(mensaje_usuario=pregunta_str)
        orchestrator = ChatOrchestrator(db.session)
        final_result = orchestrator.route_action(llm_response)



    return _adaptar_respuesta_para_compatibilidad(final_result)
