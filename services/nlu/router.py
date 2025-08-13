"""
Deterministic NLU router to recognize commands and atajos before hitting the LLM.
"""
import unicodedata
import logging
import difflib

logger = logging.getLogger(__name__)

def normalize_text(text: str) -> str:
    """Removes accents, punctuation, and converts to lowercase."""
    if not text:
        return ""
    # Normalize to separate accents from letters
    nfkd_form = unicodedata.normalize('NFKD', text.lower())
    # Keep only non-accent characters
    return "".join([c for c in nfkd_form if not unicodedata.combining(c)])

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

    normalized = normalize_text(text.strip())
    logger.info(f"NLU router received normalized text: '{normalized}'")

    # Check for numeric selection first
    if normalized.isdigit():
        num = int(normalized)
        if 1 <= num <= 9:
            intent = f"seleccion_numero_{num}"
            logger.info(f"NLU router matched intent: {intent}")
            return intent

    # Exact match for simple commands
    for intent, keywords in INTENTS.items():
        if normalized in keywords:
            logger.info(f"NLU router matched intent: {intent}")
            return intent

    logger.info("NLU router found no specific intent, returning None.")
    return None
