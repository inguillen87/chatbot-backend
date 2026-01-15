import json
import logging
from typing import Any, Dict, List, Optional, Tuple, Union
from datetime import datetime
import threading
import uuid
from flask import current_app

from models import User, TenantProfile
from services.common_utils import (
    _get_main_menu_payload,
    _ensure_welcome_audio_payload,
    _esperando_info_libre,
    get_logger,
)
from services.response_formatter import render_audio_text
from services.tts_orchestrator import generar_audio
from services.openai_bridge import (
    detect_intent_municipio,
    extraer_datos_reclamo_llm,
    generar_respuesta_municipio_llm,
)
from services.intent_classifier import classify_intent
from services.actions.municipio_actions import (
    CrearReclamoActionHandler,
    ConsultarReclamoActionHandler,
    VerCatalogoActionHandler,
    HacerSugerenciaActionHandler,
    InfoLicenciaConducirActionHandler,
    SolicitarTurnoActionHandler,
    PagarTasasActionHandler,
    BuscarEstacionamientoActionHandler,
    AgendaCulturalActionHandler,
    VeterinariaBromatologiaActionHandler,
    ObrasActionHandler,
    ContactosUtilesActionHandler,
    PuntoLimpioActionHandler,
    AyudaActionHandler,
    EncuestasActionHandler,
    VerProductosActionHandler,
    CanjearPuntosActionHandler,
    ComprarProductosActionHandler,
    DonacionesActionHandler,
    UnknownIntentHandler,
)
from config.feature_flags import FEATURE_ENCUESTAS

# Constants
CONTEXTO_MUNICIPIO = "contexto_municipio_v2"
logger = get_logger()

class GreetingHandler:
    def execute(self, owner_user, current_user, context, text, channel="web", session_context=None):
        logger.info("[GreetingHandler] Saludo detectado. Realizando reseteo completo del contexto.")

        # Reset specific context fields related to flows
        keys_to_reset = [
            "reclamo_flow_v2", "estado_conversacion", "ubicacion_contextual",
            "ultima_consulta_poi", "consulta_pendiente_ubicacion", "menu_opciones",
            "datos_parciales_llm_reclamo", "accion_pendiente_tras_ubicacion"
        ]
        for key in keys_to_reset:
            context.pop(key, None)

        context["estado_conversacion"] = "ESPERANDO_SELECCION_MENU_PRINCIPAL"
        logger.info(f"[GreetingHandler] Nuevo estado de conversación: {context['estado_conversacion']}")

        # Construct the Main Menu
        menu_context = {
            "user_obj": owner_user,
            "viewer_user_obj": current_user,
            "chat_db_context_data": session_context.context_data if session_context else {},
            "channel": channel,
            # "municipio_config_actual": ... (would load config here)
        }

        # Use the existing util to generate the payload
        response = _get_main_menu_payload(menu_context, reduced=False)
        return response

def normalizar_texto(texto: str) -> str:
    """Normalize text for consistent matching."""
    if not texto:
        return ""
    import unicodedata
    # Remove accents
    s = unicodedata.normalize('NFKD', texto).encode('ASCII', 'ignore').decode('utf-8')
    return s.lower().strip()

def _build_submenu_response(title: str, buttons: List[Dict[str, str]], body: str) -> Dict[str, Any]:
    """Helper to build consistent submenu responses."""

    # Add navigation buttons
    buttons.append({"texto": "Menú", "action_id": "menu_principal"})
    buttons.append({"texto": "Cancelar", "action_id": "cancelar"})

    # Map IDs
    for btn in buttons:
        if 'id' not in btn:
            btn['id'] = btn.get('action_id')

    return {
        "message_body": body,
        "message_type": "interactive_buttons",
        "options_list": buttons,
        "fuente": "submenu_builder",
        "generar_audio": True
    }

def responder_chatboc(
    pregunta: str,
    owner_user: User,
    current_user: Optional[User],
    rubro_obj: Any,
    chat_db_context: Any,
    rubro_nombre_frontend: Optional[str] = None,
    tipo_chat: str = "municipio",
    anon_id: Optional[str] = None,
    chat_session_uuid: Optional[str] = None,
    channel: str = "web",
    **kwargs
) -> Dict[str, Any]:
    """
    Main entry point for the chatbot logic.
    Coordinates intent classification, state management, and action execution.
    """
    logger.info(f"[RESPONDER_MUNICIPIO_START] ==================================================")
    logger.info(
        f"[RESPONDER_MUNICIPIO_START] Pregunta: '{pregunta}', UserMunicipio: {owner_user.id if owner_user else 'None'}, "
        f"ViewerCiudadano: {current_user.id if current_user else 'None'}, Anon: {anon_id}, "
        f"Channel: {channel}, ChatSessionUUID: {chat_session_uuid}"
    )

    # --- Initialize Context ---
    if not chat_db_context.context_data:
        chat_db_context.context_data = {}

    # Ensure inner dictionary exists
    municipio_ctx = chat_db_context.context_data.get(CONTEXTO_MUNICIPIO)
    if not isinstance(municipio_ctx, dict):
        municipio_ctx = {
            "estado_conversacion": None,
            "contacto_usuario": {},
            # Keep previous logic's keys if they exist, or init new
        }
        chat_db_context.context_data[CONTEXTO_MUNICIPIO] = municipio_ctx

    # Update contact info if available from current_user
    if current_user:
        contacto = municipio_ctx.get("contacto_usuario", {})
        if not contacto.get("nombre") and current_user.name:
            contacto["nombre"] = current_user.name
        if not contacto.get("telefono") and current_user.phone:
            contacto["telefono"] = current_user.phone
        if not contacto.get("email") and current_user.email:
            contacto["email"] = current_user.email
        municipio_ctx["contacto_usuario"] = contacto

    logger.info(f"[CONTEXTO_MUNICIPIO_LOAD_RAW] Contexto crudo para '{CONTEXTO_MUNICIPIO}' desde DB: {municipio_ctx}")

    # --- Check for Location Input ---
    es_ubicacion = kwargs.get("es_ubicacion", False)
    location_data = kwargs.get("location") or kwargs.get("ubicacion_usuario")

    if es_ubicacion and location_data:
        logger.info(f"Recibida ubicación: {location_data}")
        # Inject location into context for handlers to use
        municipio_ctx["ubicacion_usuario"] = location_data

        # Check if we were waiting for location
        pendiente = municipio_ctx.get("accion_pendiente_tras_ubicacion")
        if pendiente == "buscar_estacionamiento":
             handler = BuscarEstacionamientoActionHandler()
             return handler.execute(owner_user, current_user, municipio_ctx, "", channel=channel)
        elif pendiente == "crear_reclamo":
             # Resume claim flow with location
             # Update partial data
             municipio_ctx.setdefault("datos_parciales_llm_reclamo", {})["ubicacion"] = location_data.get("address") or f"{location_data.get('latitude')}, {location_data.get('longitude')}"
             municipio_ctx.setdefault("datos_parciales_llm_reclamo", {})["coordenadas"] = {"lat": location_data.get("latitude"), "lng": location_data.get("longitude")}
             handler = CrearReclamoActionHandler()
             return handler.execute(owner_user, current_user, municipio_ctx, "", channel=channel)

    # --- Pre-processing & Normalization ---
    texto_usuario = normalizar_texto(pregunta)

    # Check for direct action override (e.g. from button click)
    action_override = kwargs.get("action")
    if action_override:
        logger.info(f"Action override detectado: {action_override}")
        intent_data = {"intent": action_override, "confidence": 1.0, "source": "button_click"}
    else:
        # --- Intent Classification ---
        # 1. Try Regex/Keyword/Pattern Matcher (fast)
        intent_data = classify_intent(texto_usuario, municipio_ctx)
        logger.info(f"[IntentClassifier] Classified intent: {intent_data.get('intent')} with payload: {intent_data.get('confidence')}")

        # 2. If low confidence or None, try LLM (slower but smarter)
        if not intent_data.get("intent") or intent_data.get("confidence", 0) < 0.6:
            # Prepare minimal context for LLM
            # Call LLM Intent Detector
            llm_intent = detect_intent_municipio(texto_usuario) # This function needs to exist in openai_bridge
            if llm_intent:
                intent_data = {"intent": llm_intent, "confidence": 0.8, "source": "llm"}
                logger.info(f"[LLM Intent] LLM detected: {llm_intent}")

    detected_intent = intent_data.get("intent")

    # --- State Machine / Logic Routing ---
    estado_actual = municipio_ctx.get("estado_conversacion")
    logger.info(f"[CONTEXTO_MUNICIPIO] Estado de conversacion actual: {estado_actual}")

    response = None

    # Handle Global/Interrupting Intents (Menu, Cancel, Help)
    if detected_intent in ["menu_principal", "cancelar", "volver_inicio"]:
        handler = GreetingHandler() # Reset flow
        response = handler.execute(
            owner_user, current_user, municipio_ctx, texto_usuario,
            channel=channel, session_context=chat_db_context
        )
    elif detected_intent == "ayuda":
         handler = AyudaActionHandler()
         response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)

    # Handle Active Flows (if state is set)
    elif estado_actual and estado_actual != "ESPERANDO_SELECCION_MENU_PRINCIPAL" and not detected_intent:
         # Logic for in-progress flows (e.g., claiming steps)
         # Delegate to specific flow handlers based on state
         # If we are in a claim flow, we should continue filling data
         if "reclamo" in str(estado_actual).lower() or "esperando" in str(estado_actual).lower():
             # Assume text is input for the pending field
             # Update partial data
             datos = municipio_ctx.get("datos_parciales_llm_reclamo", {})
             # LLM Extraction for entities if needed
             extracted = extraer_datos_reclamo_llm(texto_usuario)
             if extracted:
                 for k, v in extracted.items():
                     if v: datos[k] = v

             # Also assume raw text might be description or specific answer
             if not datos.get("descripcion"):
                 datos["descripcion"] = texto_usuario

             municipio_ctx["datos_parciales_llm_reclamo"] = datos

             handler = CrearReclamoActionHandler()
             response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)

    # Handle New Intents
    if not response:
        if detected_intent == "iniciar_reclamo":
            handler = CrearReclamoActionHandler()
            response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)

        elif detected_intent == "consultar_estado_reclamo":
            handler = ConsultarReclamoActionHandler()
            response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)

        elif detected_intent == "ver_catalogo" or detected_intent == "mostrar_menu_catalogo":
             # Fix for the user reported issue: direct catalog access
            handler = VerCatalogoActionHandler()
            response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)

        elif detected_intent == "enviar_sugerencia":
            handler = HacerSugerenciaActionHandler()
            response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)

        elif detected_intent == "licencia_de_conducir":
            handler = InfoLicenciaConducirActionHandler()
            response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)

        elif detected_intent == "solicitar_turnos":
            handler = SolicitarTurnoActionHandler()
            response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)

        elif detected_intent == "pago_de_tasas_vigentes":
            handler = PagarTasasActionHandler()
            response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)

        elif detected_intent == "buscar_estacionamiento":
            handler = BuscarEstacionamientoActionHandler()
            response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)

        elif detected_intent == "agenda_y_noticias":
            handler = AgendaCulturalActionHandler()
            response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)

        elif detected_intent == "veterinaria_bromatologia":
            handler = VeterinariaBromatologiaActionHandler()
            response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)

        elif detected_intent == "obras":
            handler = ObrasActionHandler()
            response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)

        elif detected_intent == "contactos_utiles":
            handler = ContactosUtilesActionHandler()
            response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)

        elif detected_intent == "punto_limpio":
            handler = PuntoLimpioActionHandler()
            response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)

        elif detected_intent == "mostrar_menu_encuestas":
            handler = EncuestasActionHandler()
            response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)

        elif detected_intent == "catalogo_ver": # Sub-action of catalog
            handler = VerProductosActionHandler()
            response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)

        elif detected_intent == "catalogo_canje_puntos":
             handler = CanjearPuntosActionHandler()
             response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)

        elif detected_intent == "catalogo_compras":
            handler = ComprarProductosActionHandler()
            response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)

        elif detected_intent == "catalogo_donaciones":
            handler = DonacionesActionHandler()
            response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)

        # Main Menu Categories (Intermediate Steps)
        elif detected_intent == "mostrar_menu_reclamos":
             response = _build_submenu_response(
                 "Reclamos y Consultas",
                 [
                    {"texto": "📝 Iniciar un Reclamo", "action_id": "iniciar_reclamo"},
                    {"texto": "💡 Enviar una Sugerencia", "action_id": "enviar_sugerencia"},
                    {"texto": "🤔 Consultar Estado de Reclamo", "action_id": "consultar_estado_reclamo"},
                    {"texto": "📞 Contactos Útiles", "action_id": "contactos_utiles"},
                 ],
                 "Elegí una opción de reclamos:"
             )

        elif detected_intent == "mostrar_menu_tramites":
            response = _build_submenu_response(
                "Trámites y Turnos",
                [
                    {"texto": "🚗 Licencia de Conducir", "action_id": "licencia_de_conducir"},
                    {"texto": "🗓️ Solicitar Otros Turnos", "action_id": "solicitar_turnos"},
                    {"texto": "💵 Pagar Tasas Municipales", "action_id": "pago_de_tasas_vigentes"},
                ],
                "Elegí una opción:"
            )

        elif detected_intent == "mostrar_menu_informacion":
            response = _build_submenu_response(
                "Información del Municipio",
                [
                    {"texto": "🎭 Agenda Cultural y Noticias", "action_id": "agenda_y_noticias"},
                    {"texto": "🐾 Veterinaria y Bromatología", "action_id": "veterinaria_bromatologia"},
                    {"texto": "🏗️ Obras", "action_id": "obras"},
                    {"texto": "♻️ Punto Limpio", "action_id": "punto_limpio"},
                ],
                "Información disponible:"
            )

        elif detected_intent == "mostrar_menu_estacionamiento":
            response = _build_submenu_response(
                "Estacionamiento",
                [
                     {"texto": "🅿️ Buscar Estacionamiento Libre", "action_id": "buscar_estacionamiento"},
                ],
                "Opciones de estacionamiento:"
            )

        elif detected_intent == "mostrar_menu_ayuda":
             # Usually redirects to AyudaHandler but can be a submenu if needed
             handler = AyudaActionHandler()
             response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)

        elif detected_intent == "saludo":
             # Explicit greeting intent found
             handler = GreetingHandler()
             response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel, session_context=chat_db_context)

        else:
             # Fallback to LLM chat if no specific intent matched
             handler = UnknownIntentHandler()
             response = handler.execute(owner_user, current_user, municipio_ctx, texto_usuario, channel=channel)


    # --- Post-Processing ---
    # Generate Audio if needed
    _ensure_welcome_audio_payload(response)

    # Save Context Updates
    chat_db_context.context_data[CONTEXTO_MUNICIPIO] = municipio_ctx
    # The caller (webhook) saves the session to DB.

    return response
