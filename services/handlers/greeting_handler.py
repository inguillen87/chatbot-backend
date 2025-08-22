import logging
from services.handlers.base_handler import BaseMunicipioHandler
from services.conversation_state import ConversationState

logger = logging.getLogger(__name__)

CONTEXTO_MUNICIPIO = "contexto_municipio_v2"

def _get_main_menu_payload(context: dict, welcome_message_override: str = None) -> dict:
    """
    Generates the main menu payload, allowing for a custom welcome message.
    This centralizes menu creation to be reused by GreetingHandler and error handlers.
    """
    viewer_user = context.get("viewer_user_obj")
    profile_name = context.get("profile_name")

    # Prioritize the fresh ProfileName from WhatsApp, then fallback to the database name.
    user_name = None
    if isinstance(profile_name, str) and profile_name.strip():
        user_name = profile_name.strip()
    elif viewer_user:
        user_name = getattr(viewer_user, "nombre", None) or getattr(viewer_user, "name", None)

    if welcome_message_override:
        welcome_message = welcome_message_override
    elif user_name:
        welcome_message = (
            f"¡Hola, {user_name}! 👋 Soy JUNI, tu Asistente Virtual de la Municipalidad de Junín. "
            "Estoy aquí para ayudarte de una forma más inteligente. Podés consultarme sobre trámites, "
            "reclamos, turnos, noticias y mucho más.\n\n"
            "¿Cómo te puedo ayudar hoy? Elegí una opción o escribí una palabra clave:"
        )
    else:
        # Fallback for when there is no user name available
        welcome_message = (
            "¡Hola, Vecino/a! 👋 Soy JUNI, tu Asistente Virtual de la Municipalidad de Junín. "
            "Estoy aquí para ayudarte de una forma más inteligente. Para empezar, podés escribirme, "
            "enviarme un audio, una foto de un problema o compartir tu ubicación.\n\n"
            "¿Cómo te puedo ayudar hoy? Elegí una opción o escribí una palabra clave:"
        )

    categorias = [
        {"titulo": "🛠️ Reclamos", "botones": [
            {"texto": "📝 Iniciar un Reclamo", "action_id": "mostrar_menu_reclamos"}
        ]},
        {"titulo": "📄 Trámites y Consultas", "botones": [
            {"texto": "🚗 Licencia de Conducir", "action_id": "licencia_de_conducir"},
            {"texto": "💵 Pagar Tasas", "action_id": "pago_de_tasas_vigentes"},
            {"texto": "❓ Consultar otros trámites", "action_id": "consultar_otros_tramites"}
        ]},
        {"titulo": "📅 Servicios y Turnos", "botones": [
            {"texto": "🐾 Veterinaria y Bromatología", "action_id": "veterinaria_y_bromatologia"},
            {"texto": "🗓️ Solicitar Turnos", "action_id": "solicitar_turnos"}
        ]},
        {"titulo": "🚗 Estacionamiento", "botones": [
            {"texto": "🚗 Estacionamiento", "action_id": "estacionamiento"}
        ]},
        {"titulo": "📰 Información y Novedades", "botones": [
            {"texto": "🎭 Agenda Cultural y Turística", "action_id": "agenda_cultural_y_turistica"},
            {"texto": "🗞️ Últimas Novedades", "action_id": "ultimas_novedades"},
            {"texto": "📞 Contactos Útiles", "action_id": "contactos_utiles"}
        ]}
    ]

    flat_buttons = []
    for categoria in categorias:
        for boton in categoria.get('botones', []):
            new_boton = boton.copy()
            new_boton['id'] = new_boton.get('action_id', new_boton['texto'])
            flat_buttons.append(new_boton)

    return {
        "message_body": welcome_message,
        "options_list": flat_buttons,
        "message_type": "interactive_list",
        "accion_backend": "responder_directamente",
        "fuente": "greeting_handler_universal_v5",
        "categorias": categorias,
        "generar_audio": True
    }


class GreetingHandler(BaseMunicipioHandler):
    def handle(self, payload: dict) -> dict | None:
        chat_db_context_data = self.context.get("chat_db_context_data")

        if not chat_db_context_data:
            logger.warning("[GreetingHandler] chat_db_context_data no encontrado. No se puede hacer un reseteo completo.")
            contexto_municipio_actual = {}
        else:
            logger.info("[GreetingHandler] Saludo detectado. Realizando reseteo completo del contexto del municipio.")
            # Guardar información del usuario si existe, para no perderla entre reseteos.
            contexto_municipio_viejo = chat_db_context_data.get(CONTEXTO_MUNICIPIO, {})
            user_info = contexto_municipio_viejo.get('user', {})

            # Crear un diccionario de contexto completamente nuevo y limpio.
            contexto_municipio_nuevo = {}
            if user_info:
                contexto_municipio_nuevo['user'] = user_info

            # Reemplazar el diccionario de contexto viejo con el nuevo.
            # Esto elimina todo estado de conversación, historiales, datos parciales, etc.
            chat_db_context_data[CONTEXTO_MUNICIPIO] = contexto_municipio_nuevo
            contexto_municipio_actual = contexto_municipio_nuevo

        # Establecer el estado para esperar una selección del menú principal en el próximo turno.
        contexto_municipio_actual['estado_conversacion'] = ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
        logger.info(f"[GreetingHandler] Nuevo estado de conversación: {contexto_municipio_actual['estado_conversacion']}")

        # Usar la función centralizada para obtener el payload del menú.
        return _get_main_menu_payload(self.context)
