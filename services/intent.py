import json
import os
import logging
from .spacy_loader import get_spacy_model

try:
    nlp = get_spacy_model()
    if nlp is None:
        raise RuntimeError("Modelo spaCy no cargado")
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

import random
from typing import Optional, List

def buscar_en_intents(pregunta_usuario: str, rubro_nombre: str, threshold: float = 0.70) -> Optional[List[str]]:
    if not pregunta_usuario or not rubro_nombre:
        return None

    doc_user = nlp(pregunta_usuario.lower())
    if not doc_user.vector_norm:
        return None

    rubro_data = INTENTS.get(rubro_nombre.lower())
    if not rubro_data:
        return None

    posibles_respuestas = []
    for intent in rubro_data:
        for ejemplo in intent.get("ejemplos", []):
            doc_ejemplo = nlp(ejemplo.lower())
            if not doc_ejemplo.vector_norm:
                continue
            score = doc_user.similarity(doc_ejemplo)
            if score >= threshold:
                raw_respuesta = intent["respuesta"]
                if isinstance(raw_respuesta, list):
                    posibles_respuestas.extend(raw_respuesta)
                elif isinstance(raw_respuesta, str):
                    posibles_respuestas.append(raw_respuesta)

    if posibles_respuestas:
        return list(set(posibles_respuestas))

    return None
