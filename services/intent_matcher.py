import json
import os
import spacy
import logging

# Cargar spaCy una sola vez
nlp = spacy.load("es_core_news_md")

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
    mejor_intent = None
    mejor_score = 0.0

    rubro_data = INTENTS.get(rubro_nombre.lower())
    if not rubro_data:
        logging.info(f"⚠️ No hay intents para el rubro: {rubro_nombre}")
        return None

    for intent in rubro_data:
        for ejemplo in intent.get("ejemplos", []):  # 🛡️ Usa .get() por si falta la clave
            doc_ejemplo = nlp(ejemplo.lower())
            score = doc_user.similarity(doc_ejemplo)
            if score > mejor_score:
                mejor_score = score
                mejor_intent = intent

    if mejor_intent and mejor_score >= threshold:
        logging.info(f"✅ Intent match: '{mejor_intent['respuesta']}' (score: {mejor_score:.2f})")
        return mejor_intent["respuesta"]

    logging.info(f"❌ No hubo match en intents (score máximo: {mejor_score:.2f})")
    return None
