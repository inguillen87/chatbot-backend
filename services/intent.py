import json
import logging
import os

from .spacy_loader import get_spacy_model

logger = logging.getLogger(__name__)
nlp = get_spacy_model()

file_path = os.path.join(os.path.dirname(__file__), "../data/intents.json")
try:
    with open(file_path, "r", encoding="utf-8") as f:
        INTENTS = json.load(f)
except Exception as e:
    logger.error("❌ No se pudo cargar intents.json: %s", e)
    INTENTS = {}


def buscar_en_intents(pregunta_usuario: str, rubro_nombre: str, threshold: float = 0.70):
    if not pregunta_usuario or not rubro_nombre or nlp is None:
        return None

    doc_user = nlp(pregunta_usuario.lower())
    if not getattr(doc_user, "vector_norm", 0):
        return None

    rubro_data = INTENTS.get(rubro_nombre.lower())
    if not rubro_data:
        return None

    mejor_intent = None
    mejor_score = 0.0

    for intent in rubro_data:
        for ejemplo in intent.get("ejemplos", []):
            doc_ejemplo = nlp(ejemplo.lower())
            if not getattr(doc_ejemplo, "vector_norm", 0):
                continue
            score = doc_user.similarity(doc_ejemplo)
            if score > mejor_score:
                mejor_score = score
                mejor_intent = intent

    if mejor_intent and mejor_score >= threshold:
        return mejor_intent["respuesta"]

    return None
