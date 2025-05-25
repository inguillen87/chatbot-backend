import json
import os
import spacy
import logging

try:
    nlp = spacy.load("es_core_news_md")
    print("✅ spaCy cargado correctamente en intent.py")
except Exception as e:
    logging.error(f"❌ Error al cargar spaCy: {e}")
    raise

file_path = os.path.join(os.path.dirname(__file__), "../data/intents.json")
try:
    with open(file_path, "r", encoding="utf-8") as f:
        INTENTS = json.load(f)
except Exception as e:
    logging.error(f"❌ No se pudo cargar intents.json: {e}")
    INTENTS = {}

def buscar_en_intents(pregunta_usuario: str, rubro_nombre: str, threshold: float = 0.70):
    if not pregunta_usuario or not rubro_nombre:
        return None

    doc_user = nlp(pregunta_usuario.lower())
    if not doc_user.vector_norm:
        return None

    rubro_data = INTENTS.get(rubro_nombre.lower())
    if not rubro_data:
        return None

    mejor_intent = None
    mejor_score = 0.0

    for intent in rubro_data:
        for ejemplo in intent.get("ejemplos", []):
            doc_ejemplo = nlp(ejemplo.lower())
            if not doc_ejemplo.vector_norm:
                continue
            score = doc_user.similarity(doc_ejemplo)
            if score > mejor_score:
                mejor_score = score
                mejor_intent = intent

    if mejor_intent and mejor_score >= threshold:
        return mejor_intent["respuesta"]

    return None
