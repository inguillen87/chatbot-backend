import logging
import json
from flask import current_app, has_app_context
from sqlalchemy.orm.attributes import flag_modified

# Utiliza el orquestador de LLMs (OpenAI → Cohere) en lugar de una
# llamada directa a un único proveedor.
from services.llm_orchestrator import llamar_llm_con_fallback
from services.conversation_state import ConversationState
from services.actions.municipio_actions import CrearReclamoActionHandler
from services.municipio_responder import (
    GreetingHandler,
    _get_main_menu_payload,
    _normalize_pedir_info_fields,
)
from services.municipio_responder import _get_reclamos_menu
from services.herramientas_municipio import TOOL_REGISTRY
from services.municipio_responder import es_consulta_general
from services.llm_utils import extract_multiple_contact_details_llm
from utils.response_utils import normalize_response_payload

logger = logging.getLogger(__name__)

CONTEXTO_MUNICIPIO = "contexto_municipio_v2"

# These might need to be passed or imported from a central config
PALABRAS_CLAVE_CONFIRMACION = {
    "confirmar_reclamo", "confirmar", "confirmo", "confirmado",
    "si", "sí", "afirmativo", "dale", "ok", "proceder", "aceptar",
    "confirmar_reclamo_final", "si, confirmar reclamo", "sí, confirmar reclamo",
    "yes"
}

PEDIR_INFO_TO_STATE = {
    "ubicacion": ConversationState.ESPERANDO_DIRECCION_RECLAMO,
    "direccion": ConversationState.ESPERANDO_DIRECCION_RECLAMO,
    "categoria": ConversationState.ESPERANDO_CATEGORIA_RECLAMO,
    "descripcion": ConversationState.ESPERANDO_DESCRIPCION_RECLAMO,
    "descripcion_mas_detallada": ConversationState.ESPERANDO_DESCRIPCION_RECLAMO,
    "nombre_completo": ConversationState.ESPERANDO_NOMBRE_VECINO,
    "nombre": ConversationState.ESPERANDO_NOMBRE_VECINO,
    "telefono": ConversationState.ESPERANDO_TELEFONO_VECINO,
    "email": ConversationState.ESPERANDO_EMAIL_VECINO,
    "id_reclamo": ConversationState.ESPERANDO_NUMERO_TICKET,
    "id_ticket": ConversationState.ESPERANDO_NUMERO_TICKET,
    "confirmacion": ConversationState.ESPERANDO_CONFIRMACION_RECLAMO,
    "adjuntos": ConversationState.ESPERANDO_ADJUNTOS_RECLAMO,
}

def handle_llm_interaction(app, pregunta_str, context, viewer_user, owner_user, chat_db_context, contexto_municipio_actual):
    from services.municipio_responder import extract_description_and_check_confirmation, _handle_ticket_creation

    logger_actual = app.logger if app else (current_app.logger if has_app_context() else logging.getLogger(__name__))
    datos_actuales = {}

    logger_actual.info(
        f"[HANDLE_LLM_START] pregunta='{pregunta_str}' estado_previo='{contexto_municipio_actual.get('estado_conversacion')}'"
    )

    estado_conversacion_para_llm = contexto_municipio_actual.get("estado_conversacion")
    invocar_llm = False

    if (
        estado_conversacion_para_llm == ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
        and es_consulta_general(pregunta_str)
    ):
        logger_actual.info(
            "[HANDLE_LLM] Cambio de tema detectado. Reseteando contexto a conversacion general."
        )
        contexto_municipio_actual.pop("historial_llm_reclamo", None)
        contexto_municipio_actual.pop("datos_parciales_llm_reclamo", None)
        contexto_municipio_actual.pop("esperando_info_llm_reclamo", None)
        contexto_municipio_actual.pop("esperando_info_llm", None)
        contexto_municipio_actual["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM.name
        estado_conversacion_para_llm = ConversationState.CONVERSACION_GENERAL_LLM.name

    if estado_conversacion_para_llm in [
        ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name,
        ConversationState.CONVERSACION_GENERAL_LLM.name,
        ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
    ]:
        invocar_llm = True
    elif not estado_conversacion_para_llm or contexto_municipio_actual.get("saludo_detectado_en_largo_mensaje"):
        if len(pregunta_str.strip().split()) > 1 or (context.get("es_foto") and not pregunta_str.strip()):
            invocar_llm = True

    if not invocar_llm:
        return None, contexto_municipio_actual

    logger.info(f"[HANDLE_LLM] Invocando LLM. Estado: {estado_conversacion_para_llm}")

    datos_reclamo = contexto_municipio_actual.get("datos_parciales_llm_reclamo", {})
    usuario_info_llm = {
        "nombre": datos_reclamo.get("nombre_usuario_detectado") or getattr(viewer_user, "nombre", "Vecino/a") if viewer_user else "Vecino/a",
        "tipo_entidad": "municipio",
        "ubicacion": datos_reclamo.get("ubicacion") or getattr(viewer_user, "direccion", None) if viewer_user else None,
        "contacto": {
            "telefono": datos_reclamo.get("telefono_detectado") or getattr(viewer_user, "telefono", None) if viewer_user else None,
            "email": datos_reclamo.get("email_detectado") or getattr(viewer_user, "email", None) if viewer_user else None
        },
        "datos_reclamo_actuales": datos_reclamo
    }

    historial_para_llm = (
        contexto_municipio_actual.get("historial_llm_reclamo", [])
        if estado_conversacion_para_llm == ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
        else contexto_municipio_actual.get("historial_conversacion_general_llm", [])
    )

    historial_formateado = []
    if historial_para_llm and isinstance(historial_para_llm, list) and historial_para_llm and "pregunta_usuario" in historial_para_llm[0]:
        for turno in historial_para_llm:
            pregunta = turno.get("pregunta_usuario")
            respuesta = turno.get("respuesta_ia")
            if pregunta:
                historial_formateado.append({"role": "user", "parts": [{"text": pregunta}]})
            if respuesta:
                historial_formateado.append({"role": "model", "parts": [{"text": respuesta}]})
    else:
        historial_formateado = historial_para_llm or []

    try:
        mensaje_completo_para_llm = {"texto": pregunta_str}
        if context.get("es_foto") and context.get("foto_url"):
            mensaje_completo_para_llm["imagen_url"] = context.get("foto_url")

        mensaje_para_llm = json.dumps(mensaje_completo_para_llm)

        # --- Context Injection ---
        # Add key links and identity info to the user context passed to the LLM
        tenant_name = getattr(owner_user, "nombre", "Municipalidad")
        # Assuming frontend URL can be derived or is in config
        base_url = current_app.config.get("WIDGET_URL") or current_app.config.get("PANEL_URL") or "https://chatboc.ar"
        tenant_slug = getattr(owner_user, "tenant_slug", "") or "municipio"

        usuario_info_llm["contexto_tenant"] = {
            "nombre": tenant_name,
            "links": {
                "portal_usuario": f"{base_url}/portal/{tenant_slug}",
                "catalogo": f"{base_url}/{tenant_slug}/productos",
                "perfil": f"{base_url}/portal/{tenant_slug}/perfil"
            }
        }

        respuesta_llm_dict, context_dict = llamar_llm_con_fallback(
            app=app,
            mensaje_usuario=mensaje_para_llm,
            usuario=usuario_info_llm,
            historial=historial_formateado,
            chat_session_id=context.get("chat_session_uuid")
        )

        if isinstance(respuesta_llm_dict, dict):
            normalize_response_payload(respuesta_llm_dict)

        if isinstance(context_dict, dict) and chat_db_context:
            chat_db_context.context_data.update(context_dict)

        respuesta_usuario_llm = respuesta_llm_dict.get("message_body")
        accion_backend_llm = respuesta_llm_dict.get("accion_backend")
        datos_estructura_llm = respuesta_llm_dict.get("datos_estructura")
        pedir_info_llm = respuesta_llm_dict.get("pedir_info")
        botones_llm = respuesta_llm_dict.get("botones", [])

        if not respuesta_usuario_llm and accion_backend_llm not in ["crear_reclamo", "ejecutar_herramienta"]:
             logger_actual.warning("[HANDLE_LLM] LLM response did not contain a 'message_body'.")
             return None, contexto_municipio_actual

        nuevo_turno_historial = {"pregunta_usuario": pregunta_str, "respuesta_ia": respuesta_usuario_llm}

        if accion_backend_llm == "saludar":
            handler = GreetingHandler(context)
            return handler.handle({}), contexto_municipio_actual

        if accion_backend_llm == "mostrar_menu_catalogo":
             from services.municipio_responder import _get_catalogo_menu
             # Delegate to the standard catalog menu responder
             response = _get_catalogo_menu(context)
             if respuesta_usuario_llm:
                 response["message_body"] = respuesta_usuario_llm # Override or prepend LLM text if desired
             return response, contexto_municipio_actual

        if accion_backend_llm in ["crear_reclamo", "iniciar_reclamo"] and datos_estructura_llm and datos_estructura_llm.get("target") == "municipio":
            contexto_municipio_actual.setdefault("historial_llm_reclamo", []).append(nuevo_turno_historial)
            datos_actuales = contexto_municipio_actual.setdefault("datos_parciales_llm_reclamo", {})
            datos_actuales.update({k: v for k, v in datos_estructura_llm.items() if v is not None})
            if not datos_actuales.get("categoria"):
                datos_actuales["categoria"] = "otros"

            if not pedir_info_llm:
                handler = CrearReclamoActionHandler(context)
                return handler.execute(datos_actuales), contexto_municipio_actual
            else:
                contexto_municipio_actual["estado_conversacion"] = ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
                pending_fields = _normalize_pedir_info_fields(pedir_info_llm)
                if pending_fields:
                    contexto_municipio_actual["expected_fields_llm_reclamo"] = pending_fields
                    contexto_municipio_actual["esperando_info_llm_reclamo"] = pending_fields[0]
                else:
                    contexto_municipio_actual["esperando_info_llm_reclamo"] = pedir_info_llm
                return {"message_body": respuesta_usuario_llm, "options_list": botones_llm, "message_type": "interactive_buttons" if botones_llm else "text"}, contexto_municipio_actual

        # Other actions... (this is a simplified version of the logic)

        else: # Fallback for general conversation
            contexto_municipio_actual.setdefault("historial_conversacion_general_llm", []).append(nuevo_turno_historial)
            contexto_municipio_actual["estado_conversacion"] = ConversationState.CONVERSACION_GENERAL_LLM.name
            return {"message_body": respuesta_usuario_llm, "options_list": botones_llm, "message_type": "interactive_buttons" if botones_llm else "text"}, contexto_municipio_actual

    except Exception as e_llm:
        logger.error(f"[HANDLE_LLM] Error: {e_llm}", exc_info=True)
        return None, contexto_municipio_actual
