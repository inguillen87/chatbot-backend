"""
Deterministic NLU router to recognize commands and atajos before hitting the LLM.
"""
import unicodedata

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
    "iniciar_reclamo": ["r", "re", "reclamo", "reclamos"],
    "consultar_tramites": ["t", "tr", "tramite", "tramites"],
    "solicitar_turnos": ["tu", "turno", "turnos"],
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

    # Direct match for simple commands
    for intent, keywords in INTENTS.items():
        if normalized in keywords:
            return intent

    # Check for numeric selection (will need context of which menu is active)
    # For now, this is a placeholder.
    if normalized.isdigit():
        num = int(normalized)
        if 1 <= num <= 9:
            return f"seleccion_numero_{num}"

    # More complex matching can be added here if needed, e.g., regex.

    return None
