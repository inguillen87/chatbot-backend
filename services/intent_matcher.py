import json
import os
import spacy
import logging
from models import QA

# Cargar spaCy una sola vez con validación de vectores
try:
    nlp = spacy.load("es_core_news_md")
    if not nlp.vocab.vectors:
        raise ValueError("❌ El modelo spaCy no tiene vectores cargados.")
except Exception as e:
    logging.error(f"❌ Error al cargar spaCy: {e}")
    raise

# Cargar intents desde archivo JSON
file_path = os.path.join(os.path.dirname(__file__), "../data/intents.json")
try:
    with open(file_path, "r", encoding="utf-8") as f:
        INTENTS = json.load(f)
except Exception as e:
    logging.error(f"❌ No se pudo cargar intents.json: {e}")
    INTENTS = {}

def buscar_en_intents(pregunta_usuario: str, rubro_nombre: str, threshold: float = 0.70):
    if not pregunta_usuario or not rubro_nombre:
        logging.warning("⚠️ Entrada inválida para búsqueda en intents.")
        return None

    doc_user = nlp(pregunta_usuario.lower())
    if not doc_user.vector_norm:
        logging.warning("⚠️ Vectores del usuario vacíos. Pregunta no procesable.")
        return None

    rubro_data = INTENTS.get(rubro_nombre.lower())
    if not rubro_data:
        logging.info(f"⚠️ No hay intents para el rubro: {rubro_nombre}")
        return None

    mejor_intent = None
    mejor_score = 0.0

    for intent in rubro_data:
        for ejemplo in intent.get("ejemplos", []):
            doc_ejemplo = nlp(ejemplo.lower())
            if not doc_ejemplo.vector_norm:
                continue
            score = doc_user.similarity(doc_ejemplo)
            logging.debug(f"🔍 Comparando con: '{ejemplo}' → Score: {score:.3f}")
            if score > mejor_score:
                mejor_score = score
                mejor_intent = intent

    if mejor_intent:
        if mejor_score >= threshold:
            logging.info(f"✅ Intent match fuerte: '{mejor_intent['respuesta']}' (score: {mejor_score:.3f})")
        else:
            logging.warning(f"⚠️ Coincidencia débil en intents (score: {mejor_score:.3f}). Se usa igual.")
        return mejor_intent["respuesta"]

    logging.info("❌ No se encontró ningún intent válido para la pregunta.")
    return None
