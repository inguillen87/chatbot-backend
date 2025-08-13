"""
Deterministic NLU router to recognize commands and atajos before hitting the LLM.
"""
import unicodedata
import logging
import re

logger = logging.getLogger(__name__)

def normalize_text(text: str) -> str:
    """Removes accents, punctuation, and converts to lowercase, keeping spaces."""
    if not text:
        return ""
    # Normalize to separate accents from letters and convert to lowercase
    nfkd_form = unicodedata.normalize('NFKD', text.lower())
    # Keep only alphanumeric characters and spaces
    return "".join([c for c in nfkd_form if unicodedata.isalnum(c) or c.isspace()]).strip()

# --- Intent Definitions ---
# This dictionary maps keywords and action_ids to a canonical intent name.
INTENTS = {
    # General & Navigation
    "mostrar_menu": ["menu", "volver", "inicio", "principal"],
    "agradecer": ["gracias", "ok", "bueno", "dale"],
    "finalizar": ["finalizar", "cerrar", "chau", "adios"],
    "derivar_humano": ["agente", "humano", "persona", "hablar con alguien"],

    # Main Menu Options (from web and whatsapp)
    "iniciar_reclamo": [
        "r", "re", "reclamo", "reclamos", "reportar", "iniciar un reclamo",
        "hacer un reclamo", "show_reclamos_menu", "iniciar_reclamo"
    ],
    "realizar_denuncia": [
        "denuncia", "denuncias", "realizar una denuncia", "hacer_denuncia"
    ],
    "tramite_licencia": [
        "licencia", "licencia de conducir", "carnet", "licencia_de_conducir"
    ],
    "pagar_tasas": [
        "tasas", "pagar tasas", "impuestos", "pago_de_tasas_vigentes"
    ],
    "consultar_tramites": [
        "t", "tr", "tramite", "tramites", "consultar otros tramites",
        "consultar tramites", "consultar_otros_tramites"
    ],
    "veterinaria_bromatologia": [
        "veterinaria", "bromatologia", "mascotas", "animales",
        "veterinaria y bromatologia", "veterinaria_y_bromatologia"
    ],
    "solicitar_turnos": [
        "tu", "turno", "turnos", "solicitar turnos", "solicitar_turnos"
    ],
    "agenda_cultural": [
        "agenda", "cultura", "turismo", "agenda cultural", "agenda cultural y turistica",
        "agenda_cultural_y_turistica"
    ],
    "ultimas_novedades": [
        "noticias", "novedades", "ultimas novedades", "ultimas_novedades"
    ],
    "defensa_consumidor": [
        "consumidor", "defensa del consumidor", "defensa_del_consumidor"
    ],

    # Other specific intents
    "consultar_estado_ticket": ["estado", "ticket", "mi reclamo", "ver estado"],
}

def route(text: str) -> str | None:
    """
    Recognizes simple commands and maps them to an intent.
    Returns the intent name or None if no match.
    """
    if not text:
        return None

    normalized_input = normalize_text(text)
    logger.info(f"NLU router received normalized text: '{normalized_input}'")

    # Check for numeric selection first
    if normalized_input.isdigit():
        num = int(normalized_input)
        # Allow for more than 9 options if needed in the future
        if 1 <= num <= 20:
            intent = f"seleccion_numero_{num}"
            logger.info(f"NLU router matched intent: {intent}")
            return intent

    # Create a flat list of (keyword, intent) tuples
    all_keywords = []
    for intent, keywords in INTENTS.items():
        for keyword in keywords:
            all_keywords.append((normalize_text(keyword), intent))

    # Sort by the number of words in the keyword, descending, to prioritize longer matches
    all_keywords.sort(key=lambda x: len(x[0].split()), reverse=True)

    # Use a set of words from the input for efficient checking
    input_words = set(normalized_input.split())

    for keyword, intent in all_keywords:
        keyword_words = keyword.split()
        # Check if all words from the keyword are present in the input text
        if all(word in input_words for word in keyword_words):
            logger.info(f"NLU router matched intent: {intent} (keyword: '{keyword}')")
            return intent

    logger.info("NLU router found no specific intent, returning None.")
    return None
