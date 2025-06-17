# services/intent_matcher.py
import json
import os
import logging
from typing import Optional, Dict, List, Any
import random # Para elegir una respuesta de una lista
from .utils import limpiar_texto_base
from .spacy_loader import get_spacy_model

logger = logging.getLogger(__name__)
NLP_SPACY_INTENT = None
INTENTS_DATA: Dict[str, List[Dict[str, Any]]] = {} 

def _cargar_recursos_intent():
    global NLP_SPACY_INTENT, INTENTS_DATA
    if NLP_SPACY_INTENT is None:
        NLP_SPACY_INTENT = get_spacy_model()
        if NLP_SPACY_INTENT is None:
            logger.error("❌ No se pudo cargar el modelo spaCy para Intent Matcher.")
    if not INTENTS_DATA:
        try:
            current_dir = os.path.dirname(os.path.abspath(__file__)); project_root = os.path.abspath(os.path.join(current_dir, "..")) 
            file_path = os.path.join(project_root, "data", "intents.json")
            if not os.path.exists(file_path): logger.error(f"❌ intents.json NO encontrado: {file_path}"); INTENTS_DATA = {}; return
            with open(file_path, "r", encoding="utf-8") as f: INTENTS_DATA = json.load(f)
            logger.info(f"✅ intents.json cargado ({len(INTENTS_DATA)} rubros) desde {file_path}.")
        except Exception as e_load: logger.error(f"❌ No se pudo cargar intents.json: {e_load}", exc_info=True); INTENTS_DATA = {}

def buscar_en_intents(pregunta_usuario: str, rubro_nombre: str, threshold: float = 0.70) -> Optional[str]:
    _cargar_recursos_intent()
    if NLP_SPACY_INTENT is None or not INTENTS_DATA: logger.error("[INTENT] Imposible buscar: spaCy o datos no cargados."); return None
    if not pregunta_usuario or not isinstance(pregunta_usuario, str) or not pregunta_usuario.strip(): logger.warning("[INTENT] Pregunta vacía."); return None
    if not rubro_nombre or not isinstance(rubro_nombre, str) or not rubro_nombre.strip(): logger.warning(f"[INTENT] Rubro vacío ('{rubro_nombre}')."); return None
    pregunta_limpia = limpiar_texto_base(pregunta_usuario)
    doc_user = NLP_SPACY_INTENT(pregunta_limpia)
    if not doc_user.has_vector or not doc_user.vector_norm: logger.warning(f"[INTENT] No vector para pregunta (Intent): '{pregunta_limpia}'."); return None
    rubro_key = rubro_nombre.lower().strip(); rubro_intent_data = INTENTS_DATA.get(rubro_key)
    if not rubro_intent_data:
        if rubro_key != "general": logger.info(f"[INTENT] No intents para '{rubro_key}'. Fallback a 'general'."); rubro_intent_data = INTENTS_DATA.get("general")
        if not rubro_intent_data: logger.info(f"[INTENT] No intents para '{rubro_key}' ni 'general'."); return None
    if not isinstance(rubro_intent_data, list): logger.warning(f"[INTENT] Datos para rubro '{rubro_key}' no es lista: {type(rubro_intent_data)}"); return None
    mejor_intent_respuesta: Optional[str] = None; mejor_score: float = -1.0
    for intent_obj in rubro_intent_data:
        if not isinstance(intent_obj, dict) or "ejemplos" not in intent_obj or "respuesta" not in intent_obj: logger.warning(f"[INTENT] Formato incorrecto en '{rubro_key}': {str(intent_obj)[:100]}"); continue
        for ejemplo in intent_obj.get("ejemplos", []):
            if not ejemplo or not isinstance(ejemplo, str) or not ejemplo.strip(): continue
            doc_ejemplo = NLP_SPACY_INTENT(limpiar_texto_base(ejemplo))
            if not doc_ejemplo.has_vector or not doc_ejemplo.vector_norm: continue
            try:
                score = doc_user.similarity(doc_ejemplo)
                if score > mejor_score: 
                    mejor_score = score; raw_respuesta = intent_obj["respuesta"]
                    if isinstance(raw_respuesta, list): mejor_intent_respuesta = random.choice(raw_respuesta) if raw_respuesta else None
                    elif isinstance(raw_respuesta, str): mejor_intent_respuesta = raw_respuesta
                    else: logger.warning(f"[INTENT] Respuesta no es string/lista: {raw_respuesta}"); mejor_intent_respuesta = None
            except Exception as e_sim: logger.error(f"[INTENT] Error similitud para '{ejemplo[:50]}...': {e_sim}", exc_info=True); continue
    if mejor_intent_respuesta and mejor_score >= threshold:
        logger.info(f"✅ [INTENT] Match para '{pregunta_limpia}' (Rubro '{rubro_key}'): Respuesta (parcial) '{str(mejor_intent_respuesta)[:50]}...' score {mejor_score:.3f}")
        return str(mejor_intent_respuesta)
    else:
        logger.info(f"📉 [INTENT] No match >= {threshold} para '{pregunta_limpia}' (Rubro '{rubro_key}'). Mejor score: {mejor_score:.3f}")
        return None