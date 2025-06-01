# services/intent_matcher.py
import json
import os
import spacy
import logging
from .utils import limpiar_texto_base # <--- IMPORTACIÓN AÑADIDA

logger = logging.getLogger(__name__)
NLP_SPACY_INTENT = None
INTENTS_DATA = {} 

def _cargar_recursos_intent():
    global NLP_SPACY_INTENT, INTENTS_DATA
    
    if NLP_SPACY_INTENT is None:
        try:
            NLP_SPACY_INTENT = spacy.load("es_core_news_md")
            logger.info("✅ Modelo spaCy 'es_core_news_md' cargado para Intent Matcher.")
            if NLP_SPACY_INTENT.vocab.vectors.shape[0] == 0: # CORREGIDO: Usar .shape[0]
                 logger.warning("⚠️ El modelo spaCy 'es_core_news_md' (Intent) se cargó pero no tiene vectores.")
        except OSError:
            logger.error("❌ Error al cargar spaCy 'es_core_news_md' (Intent): Modelo no encontrado.")
        except Exception as e:
            logger.error(f"❌ Error inesperado al cargar spaCy (Intent): {e}", exc_info=True)

    if not INTENTS_DATA:
        try:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            file_path = os.path.join(current_dir, "..", "data", "intents.json")
            
            if not os.path.exists(file_path):
                logger.error(f"❌ Archivo intents.json NO encontrado en: {file_path}")
                INTENTS_DATA = {} 
                return

            with open(file_path, "r", encoding="utf-8") as f:
                INTENTS_DATA = json.load(f)
            logger.info(f"✅ Archivo intents.json cargado desde {file_path}.")
        except Exception as e_load:
            logger.error(f"❌ No se pudo cargar o parsear intents.json desde {file_path}: {e_load}", exc_info=True)
            INTENTS_DATA = {}

def buscar_en_intents(pregunta_usuario: str, rubro_nombre: str, threshold: float = 0.75) -> str | None:
    _cargar_recursos_intent()
    
    if NLP_SPACY_INTENT is None or not INTENTS_DATA:
        logger.error("[INTENT] Imposible buscar: spaCy o datos de intents no cargados.")
        return None
    if not pregunta_usuario or not isinstance(pregunta_usuario, str) or not pregunta_usuario.strip():
        logger.warning("[INTENT] Pregunta de usuario vacía o inválida.")
        return None
    if not rubro_nombre or not isinstance(rubro_nombre, str) or not rubro_nombre.strip():
        logger.warning(f"[INTENT] Nombre de rubro vacío o inválido ('{rubro_nombre}').")
        return None

    pregunta_limpia = limpiar_texto_base(pregunta_usuario) # CORREGIDO: Usa la función importada
    doc_user = NLP_SPACY_INTENT(pregunta_limpia)

    if not doc_user.has_vector or not doc_user.vector_norm:
        logger.warning(f"[INTENT] No se pudo generar vector para pregunta (Intent): '{pregunta_limpia}'.")
        return None

    rubro_key = rubro_nombre.lower().strip()
    rubro_intent_data = INTENTS_DATA.get(rubro_key)
    
    if not rubro_intent_data:
        if rubro_key != "general":
            logger.info(f"[INTENT] No hay intents para rubro '{rubro_key}'. Intentando con 'general'.")
            rubro_intent_data = INTENTS_DATA.get("general")
        if not rubro_intent_data:
            logger.info(f"[INTENT] No hay intents para rubro '{rubro_key}' ni para 'general'.")
            return None

    mejor_intent_respuesta: Optional[str] = None
    mejor_score: float = -1.0

    for intent_obj in rubro_intent_data:
        if not isinstance(intent_obj, dict) or "ejemplos" not in intent_obj or "respuesta" not in intent_obj:
            logger.warning(f"[INTENT] Formato incorrecto de intent en rubro '{rubro_key}': {intent_obj}")
            continue

        for ejemplo in intent_obj.get("ejemplos", []):
            if not ejemplo or not isinstance(ejemplo, str) or not ejemplo.strip():
                continue
            
            doc_ejemplo = NLP_SPACY_INTENT(limpiar_texto_base(ejemplo))
            if not doc_ejemplo.has_vector or not doc_ejemplo.vector_norm:
                continue
            
            try:
                score = doc_user.similarity(doc_ejemplo)
                if score > mejor_score:
                    mejor_score = score
                    mejor_intent_respuesta = intent_obj["respuesta"]
            except Exception as e_sim_intent:
                 logger.error(f"[INTENT] Error calculando similitud para Ejemplo Intent '{ejemplo[:50]}...': {e_sim_intent}", exc_info=True)
                 continue

    if mejor_intent_respuesta and mejor_score >= threshold:
        logger.info(f"✅ [INTENT] Match encontrado para '{pregunta_limpia}' (Rubro '{rubro_key}'): Respuesta (parcial) '{str(mejor_intent_respuesta)[:50]}...' con score {mejor_score:.3f}")
        return str(mejor_intent_respuesta)
    else:
        logger.info(f"📉 [INTENT] No se encontró intent con similitud >= {threshold} para '{pregunta_limpia}' (Rubro '{rubro_key}'). Mejor score: {mejor_score:.3f}")
        return None