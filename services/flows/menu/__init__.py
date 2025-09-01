"""
Handles the main menu flow.
"""
from enum import Enum, auto

# Simplified ConversationState for the menu flow
class MenuState(Enum):
    ESPERANDO_SELECCION_MENU_PRINCIPAL = auto()

def handle(msg, ctx):
    """
    Handles the main menu.
    """
    # For now, we just return the main menu.
    # In the future, this will have more logic, like context reset.

    welcome_message = (
        "¡Hola! 👋 Soy JUNI, tu Asistente Virtual de la Municipalidad de Junín. "
        "¿Cómo te puedo ayudar hoy?"
    )

    categorias = [
        {"titulo": "Reclamos y Denuncias 🛠️", "botones": [
            {"texto": "🛠️ Iniciar un Reclamo", "action_id": "iniciar_reclamo"},
            {"texto": "⚖️ Realizar una Denuncia", "action_id": "denuncias"}
        ]},
        {"titulo": "Trámites y Consultas 📄", "botones": [
            {"texto": "🚗 Licencia de Conducir", "action_id": "licencia_de_conducir"},
            {"texto": "💵 Pagar Tasas", "action_id": "pago_de_tasas_vigentes"},
            {"texto": "📋 Consultar otros trámites", "action_id": "consultar_otros_tramites"}
        ]},
        {"titulo": "Servicios y Turnos 📅", "botones": [
            {"texto": "🐾 Veterinaria y Bromatología", "action_id": "veterinaria_bromatologia"},
            {"texto": "📅 Solicitar Turnos", "action_id": "solicitar_turnos"}
        ]},
        {"titulo": "Información y Novedades 📰", "botones": [
            {"texto": "🎭 Agenda Cultural y Turística", "action_id": "agenda_cultural_y_turistica"},
            {"texto": "📰 Últimas Novedades", "action_id": "ultimas_novedades"},
            {"texto": "🛒 Defensa del Consumidor", "action_id": "defensa_del_consumidor"}
        ]}
    ]

    flat_buttons = [boton for categoria in categorias for boton in categoria.get('botones', [])]

    return {
        "message_body": welcome_message,
        "options_list": flat_buttons,
        "message_type": "interactive_list",
    }
