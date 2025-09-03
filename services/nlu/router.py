"""
Deterministic NLU router to recognize commands and atajos before hitting the LLM.
"""
import unicodedata
import logging
import re

from fuzzywuzzy import fuzz

logger = logging.getLogger(__name__)

try:  # pragma: no cover - spaCy model might be missing in some environments
    import spacy

    _nlp = spacy.load("es_core_news_md", disable=["parser", "ner"])
except Exception:  # pragma: no cover - handled gracefully when model not present
    logger.warning("spaCy model 'es_core_news_md' not available. Lemmatization disabled.")
    _nlp = None


def normalize_text(text: str) -> str:
    """Removes accents and punctuation, lemmatizes (if spaCy available), and lowercases."""
    if not isinstance(text, str):
        return ""
    text = text.lower()
    if _nlp is not None:
        doc = _nlp(text)
        text = " ".join(token.lemma_ for token in doc)
    nfkd_form = unicodedata.normalize("NFKD", text)
    return "".join([c for c in nfkd_form if c.isalnum() or c.isspace()]).strip()

# --- Intent Definitions ---
INTENTS = {
    "mostrar_menu": ["menu", "volver", "inicio", "principal"],
    "iniciar_reclamo": [
        "reclamo", "reclamos", "reportar", "iniciar un reclamo",
        "hacer un reclamo", "show_reclamos_menu", "queja", "problema",
        "averia", "avería"
    ],
    "denuncias": [
        "denuncia", "denuncias", "denunciar", "hacer una denuncia", "realizar una denuncia"
    ],
    "enviar_sugerencia": [
        "sugerencia", "enviar sugerencia", "comentario", "opinion", "feedback", "idea"
    ],
    "consultar_estado_ticket": [
        "estado", "ticket", "mi reclamo", "ver estado",
        "estado reclamo", "seguimiento", "ver reclamo", "consultar reclamo"
    ],
    "contactos_utiles": [
        "contactos", "contacto", "telefonos", "telefono", "números", "numeros",
        "contactos utiles", "contacto del municipio"
    ],
    "tramite_licencia": [
        "licencia", "licencia de conducir", "carnet", "licencia_de_conducir",
        "registro", "registro de conducir", "sacar licencia"
    ],
    "pagar_tasas": [
        "tasas", "pagar tasas", "impuestos", "pago_de_tasas_vigentes",
        "pagar impuestos", "tasas municipales", "impuesto municipal",
        "tributo", "tributos"
    ],
    "consultar_tramites": [
        "tramite", "tramites", "consultar otros tramites", "consultar_otros_tramites",
        "hacer tramite", "otros tramites"
    ],
    "veterinaria_bromatologia": [
        "veterinaria", "bromatologia", "mascotas", "animales",
        "veterinaria y bromatologia", "veterinaria_y_bromatologia",
        "perros", "gatos", "vacunas", "castracion",
        "sanidad animal", "sanidad_animal"
    ],
    "solicitar_turnos": [
        "turno", "turnos", "solicitar turnos", "solicitar_turnos",
        "sacar turno", "pedir turno", "reservar turno"
    ],
    "agenda_cultural": [
        "agenda", "cultura", "turismo", "agenda cultural",
        "agenda cultural y turistica", "agenda_cultural_y_turistica",
        "agenda de eventos", "eventos", "eventos culturales",
        "agenda municipal", "actividades culturales", "que hay para hacer"
    ],
    "ultimas_novedades": [
        "noticias", "novedades", "ultimas novedades", "ultimas_novedades",
        "actualidad", "que hay de nuevo", "informacion",
        "informacion municipal", "noticias municipales", "lo ultimo", "lo nuevo"
    ],
    "defensa_consumidor": [
        "consumidor", "defensa del consumidor", "defensa_del_consumidor",
        "proteccion al consumidor", "reclamo consumidor"
    ],
    "buscar_estacionamiento": [
        "estacionamiento", "buscar estacionamiento", "parking",
        "donde estacionar", "estacionamiento libre"
    ],
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

    # Fallback to fuzzy matching for more flexible user input
    best_intent = None
    best_keyword = ""
    best_score = 0
    for keyword, intent in all_keywords:
        score = fuzz.partial_ratio(keyword, normalized_input)
        if score > best_score:
            best_score = score
            best_intent = intent
            best_keyword = keyword
    if best_score >= 80:
        logger.info(
            f"NLU router fuzzy matched intent: {best_intent} (keyword: '{best_keyword}', score: {best_score})"
        )
        return best_intent

    logger.info("NLU router found no specific intent, returning None.")
    return None
