# services/intent_matcher.py
import json
import os
import spacy
import logging
# models no se usa aquí directamente, QA era un error de copia/pega en tu original.

logger = logging.getLogger(__name__)
NLP_SPACY_INTENT = None
INTENTS_DATA = {} # Cargar los intents una sola vez

def _cargar_recursos_intent():
    """Carga el modelo spaCy y los intents si aún no están cargados."""
    global NLP_SPACY_INTENT, INTENTS_DATA
    
    if NLP_SPACY_INTENT is None:
        try:
            NLP_SPACY_INTENT = spacy.load("es_core_news_md")
            logger.info("✅ Modelo spaCy 'es_core_news_md' cargado exitosamente para Intent Matcher.")
            if not NLP_SPACY_INTENT.vocab.vectors.length:
                 logger.warning("⚠️ El modelo spaCy 'es_core_news_md' (Intent) se cargó pero parece no tener vectores.")
        except OSError:
            logger.error("❌ Error al cargar spaCy 'es_core_news_md' (Intent): Modelo no encontrado. "
                         "Descárgalo con: python -m spacy download es_core_news_md")
        except Exception as e:
            logger.error(f"❌ Error inesperado al cargar spaCy (Intent): {e}", exc_info=True)

    if not INTENTS_DATA: # Cargar solo si está vacío
        try:
            # La ruta a intents.json es relativa a este archivo (intent_matcher.py)
            # Si services/intent_matcher.py y data/intents.json, entonces:
            # ../data/intents.json
            current_dir = os.path.dirname(os.path.abspath(__file__))
            file_path = os.path.join(current_dir, "..", "data", "intents.json") # Sube un nivel y luego a data/
            
            if not os.path.exists(file_path):
                logger.error(f"❌ Archivo intents.json NO encontrado en la ruta esperada: {file_path}")
                INTENTS_DATA = {} # Mantener vacío para evitar errores
                return

            with open(file_path, "r", encoding="utf-8") as f:
                INTENTS_DATA = json.load(f)
            logger.info(f"✅ Archivo intents.json cargado exitosamente desde {file_path}.")
        except FileNotFoundError:
            logger.error(f"❌ Archivo intents.json no encontrado en {file_path}.")
            INTENTS_DATA = {}
        except json.JSONDecodeError as e_json:
            logger.error(f"❌ Error al parsear intents.json desde {file_path}: {e_json}")
            INTENTS_DATA = {}
        except Exception as e_load:
            logger.error(f"❌ No se pudo cargar intents.json desde {file_path}: {e_load}", exc_info=True)
            INTENTS_DATA = {}


def buscar_en_intents(pregunta_usuario: str, rubro_nombre: str, threshold: float = 0.75) -> str | None: # Ajustado threshold
    """Busca la respuesta de un intent que coincida con la pregunta del usuario para un rubro."""
    _cargar_recursos_intent() # Asegurar que modelo y datos estén cargados
    
    if NLP_SPACY_INTENT is None or not INTENTS_DATA:
        logger.error("Imposible buscar en Intents: modelo spaCy o datos de intents no están cargados.")
        return None
    if not pregunta_usuario or not isinstance(pregunta_usuario, str) or not pregunta_usuario.strip():
        logger.warning("⚠️ Pregunta de usuario vacía o inválida para búsqueda en intents.")
        return None
    if not rubro_nombre or not isinstance(rubro_nombre, str) or not rubro_nombre.strip():
        logger.warning(f"⚠️ Nombre de rubro vacío o inválido ('{rubro_nombre}') para búsqueda en intents.")
        return None # No se puede buscar sin rubro

    pregunta_limpia = limpiar_texto_base(pregunta_usuario) # Usar la misma limpieza que en otros lados
    doc_user = NLP_SPACY_INTENT(pregunta_limpia)

    if not doc_user.has_vector or not doc_user.vector_norm:
        logger.warning(f"⚠️ No se pudo generar un vector para la pregunta del usuario (Intent): '{pregunta_limpia}'.")
        return None

    rubro_key = rubro_nombre.lower().strip()
    rubro_intent_data = INTENTS_DATA.get(rubro_key)
    if not rubro_intent_data:
        # Fallback a "general" si el rubro específico no tiene intents
        if rubro_key != "general":
            logger.info(f"ℹ️ No hay intents para el rubro '{rubro_key}'. Intentando con rubro 'general'.")
            rubro_intent_data = INTENTS_DATA.get("general")
        
        if not rubro_intent_data:
            logger.info(f"ℹ️ No hay intents para el rubro '{rubro_key}' ni para 'general'.")
            return None

    mejor_intent_respuesta = None
    mejor_score = -1.0

    for intent_obj in rubro_intent_data: # Asumiendo que rubro_intent_data es una lista de objetos intent
        if not isinstance(intent_obj, dict) or "ejemplos" not in intent_obj or "respuesta" not in intent_obj:
            logger.warning(f"Intent con formato incorrecto en rubro '{rubro_key}': {intent_obj}")
            continue

        for ejemplo in intent_obj.get("ejemplos", []):
            if not ejemplo or not isinstance(ejemplo, str) or not ejemplo.strip():
                continue
            
            doc_ejemplo = NLP_SPACY_INTENT(limpiar_texto_base(ejemplo))
            if not doc_ejemplo.has_vector or not doc_ejemplo.vector_norm:
                # logger.debug(f"Ejemplo de intent ('{ejemplo[:50]}...') no tiene vector, se omite.")
                continue
            
            try:
                score = doc_user.similarity(doc_ejemplo)
                # logger.debug(f"Comparando '{pregunta_limpia}' con Ejemplo Intent '{ejemplo[:50]}...' (Rubro '{rubro_key}'): Score {score:.3f}")
                if score > mejor_score:
                    mejor_score = score
                    mejor_intent_respuesta = intent_obj["respuesta"] # Guardar la respuesta del mejor intent
            except Exception as e_sim_intent:
                 logger.error(f"Error calculando similitud para Ejemplo Intent '{ejemplo[:50]}...': {e_sim_intent}", exc_info=True)
                 continue


    if mejor_intent_respuesta and mejor_score >= threshold:
        logger.info(f"✅ Intent encontrado para '{pregunta_limpia}' (Rubro '{rubro_key}'): Respuesta (primeros 50 chars) '{str(mejor_intent_respuesta)[:50]}...' con score {mejor_score:.3f}")
        return str(mejor_intent_respuesta) # Asegurar que la respuesta sea string
    else:
        logger.info(f"📉 No se encontró intent con similitud suficiente para '{pregunta_limpia}' (Rubro '{rubro_key}'). Mejor score: {mejor_score:.3f}, Umbral: {threshold}")
        return None