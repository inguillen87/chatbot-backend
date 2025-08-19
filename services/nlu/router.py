"""
Deterministic NLU router to recognize commands and atajos before hitting the LLM.
"""
import unicodedata
import logging
import re

logger = logging.getLogger(__name__)

def normalize_text(text: str) -> str:
    """Removes accents, punctuation, and converts to lowercase, keeping spaces."""
    if not isinstance(text, str):
        return ""
    nfkd_form = unicodedata.normalize('NFKD', text.lower())
    return "".join([c for c in nfkd_form if c.isalnum() or c.isspace()]).strip()

# --- Intent Definitions ---
INTENTS = {
    "mostrar_menu": ["menu", "volver", "inicio", "principal"],
    "iniciar_reclamo": [
        "reclamo", "reclamos", "reportar", "iniciar un reclamo",
        "hacer un reclamo", "show_reclamos_menu"
    ],
    "tramite_licencia": ["licencia", "licencia de conducir", "carnet", "licencia_de_conducir"],
    "pagar_tasas": ["tasas", "pagar tasas", "impuestos", "pago_de_tasas_vigentes"],
    "consultar_tramites": ["tramite", "tramites", "consultar otros tramites", "consultar_otros_tramites"],
    "veterinaria_bromatologia": ["veterinaria", "bromatologia", "mascotas", "animales", "veterinaria y bromatologia", "veterinaria_y_bromatologia"],
    "solicitar_turnos": ["turno", "turnos", "solicitar turnos", "solicitar_turnos"],
    "agenda_cultural": ["agenda", "cultura", "turismo", "agenda cultural", "agenda cultural y turistica", "agenda_cultural_y_turistica"],
    "ultimas_novedades": ["noticias", "novedades", "ultimas novedades", "ultimas_novedades"],
    "defensa_consumidor": ["consumidor", "defensa del consumidor", "defensa_del_consumidor"],
    "consultar_estado_ticket": ["estado", "ticket", "mi reclamo", "ver estado"],
    "agradecer": ["gracias", "ok", "bueno", "dale"],
    "finalizar": ["finalizar", "cerrar", "chau", "adios"],
    "derivar_humano": ["agente", "humano", "persona", "hablar con alguien"],
}

def route(text: str) -> str | None:
    """
    Recognizes simple commands and maps them to an intent.
    Returns the intent name or None if no match.
    """
    if not text or not isinstance(text, str):
        return None

    normalized_input = normalize_text(text)
    logger.info(f"NLU router received normalized text: '{normalized_input}'")

    if normalized_input.isdigit():
        num = int(normalized_input)
        if 1 <= num <= 20:
            return f"seleccion_numero_{num}"

    # Create a flat list of (keyword, intent) tuples
    all_keywords = []
    for intent, keywords in INTENTS.items():
        for keyword in keywords:
            all_keywords.append((keyword, intent))

    # Sort by length of keyword descending to match longer phrases first
    all_keywords.sort(key=lambda x: len(x[0]), reverse=True)

    for keyword, intent in all_keywords:
        # Match if the keyword is present in the normalized input.
        # The keywords are sorted by length, so longer phrases are checked first.
        if keyword in normalized_input:
            logger.info(f"NLU router matched intent: {intent} (keyword: '{keyword}')")
            return intent

    logger.info("NLU router found no specific intent, returning None.")
    return None
