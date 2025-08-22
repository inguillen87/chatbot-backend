from enum import Enum, auto
import re
from fuzzywuzzy import process
from services.herramientas_municipio import normalizar_texto
from services.common_utils import extract_multiple_contact_details_regex
from services.actions.municipio_actions import CrearReclamoActionHandler

class ReclamoState(Enum):
    ESPERANDO_CATEGORIA = auto()
    ESPERANDO_UBICACION = auto()
    ESPERANDO_DESCRIPCION = auto()
    ESPERANDO_DATOS_PERSONALES = auto()
    CONFIRMANDO_RECLAMO = auto()

RECLAMO_KEYWORDS = {
    "Luminaria": ["luminaria", "luz", "poste", "foco"],
    "Arbolado": ["arbolado", "arbol", "arboles", "rama", "ramas"],
    "Limpieza y riego": ["limpieza", "riego", "basura", "basural", "contenedor"],
    "Arreglo de calle": ["calle", "bache", "pozo", "asfalto", "vereda", "agujero", "hueco"],
    "Pérdida de agua": ["agua", "perdida", "caño", "cañeria"],
    "Otros": ["otros", "otro", "varios"]
}

def find_reclamo_category_by_input(user_input: str, reclamo_options: list) -> str | None:
    """
    Finds a reclamo category based on user input, checking for number, first letter, or keywords.
    """
    if not user_input or not reclamo_options:
        return None

    normalized_input = normalizar_texto(user_input.strip())

    # 1. Check for numeric selection
    try:
        selection_index = int(normalized_input) - 1
        if 0 <= selection_index < len(reclamo_options):
            return reclamo_options[selection_index].get('texto')
    except (ValueError, IndexError):
        pass

    # 2. Check for first letter match
    if len(normalized_input) == 1:
        for option in reclamo_options:
            if normalizar_texto(option.get("texto", "")).startswith(normalized_input):
                return option.get("texto")

    # 3. Check for keyword match within the input
    for category, keywords in RECLAMO_KEYWORDS.items():
        for keyword in keywords:
            if keyword in normalized_input:
                return category

    # 4. Fallback to fuzzy matching if no direct keyword was found
    all_keywords = {
        keyword: category
        for category, keywords in RECLAMO_KEYWORDS.items()
        for keyword in keywords
    }
    best_match, score = process.extractOne(normalized_input, all_keywords.keys())
    if score > 80:
        return all_keywords[best_match]

    return None

def _get_reclamos_menu():
    """Devuelve la estructura del menú de reclamos estandarizado, con íconos y negritas."""
    opciones = [
        {"texto": "*Volver al inicio*", "id_accion": "0", "category_name": "Volver al inicio"},
        {"texto": "💡 *Luminaria*", "id_accion": "1", "category_name": "Luminaria"},
        {"texto": "🌳 *Arbolado*", "id_accion": "2", "category_name": "Arbolado"},
        {"texto": "🗑️ *Limpieza y riego*", "id_accion": "3", "category_name": "Limpieza y riego"},
        {"texto": "🚧 *Arreglo de calle*", "id_accion": "4", "category_name": "Arreglo de calle"},
        {"texto": "💧 *Pérdida de agua*", "id_accion": "5", "category_name": "Pérdida de agua"},
        {"texto": "⚫ *Otros*", "id_accion": "6", "category_name": "Otros"},
    ]
    # El cuerpo del mensaje ahora instruye al usuario que puede responder con un número o seleccionar una opción.
    return {
        "message_body": "Elegí una opción para tu reclamo:",
        "message_type": "interactive_buttons",
        "options_list": opciones,
        "fuente": "submenu_reclamos_estandar_v4",
        "generar_audio": True
    }

class ReclamoFlowHandler:
    def __init__(self, context):
        self.context = context
        # It's safer to use setdefault to ensure reclamo_en_progreso exists
        self.municipio_context = self.context.get("chat_db_context_data", {}).setdefault("contexto_municipio_v2", {})
        self.reclamo_context = self.municipio_context.setdefault("reclamo_en_progreso", {})

    def handle(self, user_input):
        # The state should be a string from the DB, so we convert it to an Enum member
        state_str = self.reclamo_context.get("state")
        state = ReclamoState[state_str] if state_str else None

        if state == ReclamoState.ESPERANDO_CATEGORIA:
            return self._handle_categoria(user_input)
        elif state == ReclamoState.ESPERANDO_UBICACION:
            return self._handle_ubicacion(user_input)
        elif state == ReclamoState.ESPERANDO_DESCRIPCION:
            return self._handle_descripcion(user_input)
        elif state == ReclamoState.ESPERANDO_DATOS_PERSONALES:
            return self._handle_datos_personales(user_input)
        elif state == ReclamoState.CONFIRMANDO_RECLAMO:
            return self._handle_confirmacion(user_input)
        else:
            return self.start_flow()

    def start_flow(self):
        # Initialize the claim context
        self.reclamo_context["state"] = ReclamoState.ESPERANDO_CATEGORIA.name
        self.municipio_context["estado_conversacion"] = "RECLAMO_EN_PROGRESO" # A general state for the main responder
        return _get_reclamos_menu()

    def _handle_categoria(self, user_input):
        # Extract the text from the payload if it's a dict
        if isinstance(user_input, dict):
            user_input = user_input.get("pregunta", "")

        # Use the existing function to find the category
        reclamo_options = _get_reclamos_menu().get("options_list", [])
        plain_text_options = [{"texto": opt.get("category_name")} for opt in reclamo_options]
        category = find_reclamo_category_by_input(user_input, plain_text_options)

        if category:
            self.reclamo_context["categoria"] = category
            self.reclamo_context["state"] = ReclamoState.ESPERANDO_UBICACION.name
            return {
                "message_body": "Por favor, indicame la ubicación del problema (calle y número, o intersección).",
                "message_type": "text",
                "fuente": "reclamo_pide_ubicacion"
            }
        else:
            # If the input is not a valid category, show the menu again with an error message.
            response = _get_reclamos_menu()
            response["message_body"] = "No entendí la categoría. Por favor, elegí una de las opciones:"
            return response

    def _handle_ubicacion(self, user_input):
        if isinstance(user_input, dict):
            user_input = user_input.get("pregunta", "")

        if user_input and len(user_input) > 5: # Basic validation
            self.reclamo_context["ubicacion"] = user_input
            self.reclamo_context["state"] = ReclamoState.ESPERANDO_DESCRIPCION.name
            return {
                "message_body": "Gracias. Ahora, por favor, describí el problema con más detalle.",
                "message_type": "text",
                "fuente": "reclamo_pide_descripcion"
            }
        else:
            return {
                "message_body": "No entendí la ubicación. Por favor, indicame la calle y número, o la intersección.",
                "message_type": "text",
                "fuente": "reclamo_pide_ubicacion_error"
            }

    def _handle_descripcion(self, user_input):
        if isinstance(user_input, dict):
            user_input = user_input.get("pregunta", "")

        if user_input:
            self.reclamo_context["descripcion"] = user_input
            self.reclamo_context["state"] = ReclamoState.ESPERANDO_DATOS_PERSONALES.name
            return {
                "message_body": "Por último, necesito tu nombre, teléfono y correo electrónico para registrar el reclamo.",
                "message_type": "text",
                "fuente": "reclamo_pide_datos_personales"
            }
        else:
            return {
                "message_body": "Por favor, describí el problema.",
                "message_type": "text",
                "fuente": "reclamo_pide_descripcion_error"
            }

    def _handle_datos_personales(self, user_input):
        if isinstance(user_input, dict):
            user_input = user_input.get("pregunta", "")

        datos_personales = extract_multiple_contact_details_regex(user_input, ["nombre", "telefono", "email"])

        self.reclamo_context.update(datos_personales)
        self.reclamo_context["state"] = ReclamoState.CONFIRMANDO_RECLAMO.name

        mensaje_confirmacion = (
            f"Por favor, confirmá si los datos para tu reclamo son correctos:\n"
            f"- **Categoría**: {self.reclamo_context.get('categoria')}\n"
            f"- **Ubicación**: {self.reclamo_context.get('ubicacion')}\n"
            f"- **Descripción**: {self.reclamo_context.get('descripcion')}\n"
            f"- **Nombre**: {self.reclamo_context.get('nombre')}\n"
            f"- **Teléfono**: {self.reclamo_context.get('telefono')}\n"
            f"- **Email**: {self.reclamo_context.get('email')}"
        )

        botones = [
            {"texto": "Sí, crear reclamo", "action_id": "confirmar_reclamo_si"},
            {"texto": "No, quiero editar", "action_id": "confirmar_reclamo_no"},
        ]

        return {
            "message_body": mensaje_confirmacion,
            "options_list": botones,
            "message_type": "interactive_buttons"
        }

    def _handle_confirmacion(self, user_input):
        if isinstance(user_input, dict):
            user_input = user_input.get("pregunta", "")

        if "si" in user_input.lower():
            # Create the ticket
            handler = CrearReclamoActionHandler(self.context)
            response = handler.execute(self.reclamo_context)

            # Clear the reclamo context
            self.municipio_context.pop("reclamo_en_progreso", None)
            self.municipio_context["estado_conversacion"] = None

            return response
        else:
            # Go back to the beginning of the flow
            return self.start_flow()
