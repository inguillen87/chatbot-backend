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
INTENTS = {
    "iniciar_reclamo": ["r", "re", "reclamo", "reclamos", "reportar"],
    "consultar_tramites": ["t", "tr", "tramite", "tramites"],
    "solicitar_turnos": ["tu", "turno", "turnos"],
    "consultar_estado_ticket": ["estado", "ticket"],
    "derivar_humano": ["agente", "humano", "persona"],
    "mostrar_menu": ["menu", "menu", "volver", "inicio"],
    "agradecer": ["gracias", "ok", "bueno", "dale"],
    "finalizar": ["finalizar", "cerrar", "chau", "adios"]
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
