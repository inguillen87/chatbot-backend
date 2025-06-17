import logging
from functools import lru_cache
import spacy

logger = logging.getLogger(__name__)

@lru_cache(maxsize=None)
def get_spacy_model(model_name: str = "es_core_news_md"):
    """Carga y reutiliza un modelo spaCy."""
    try:
        logger.info(f"Cargando modelo spaCy '{model_name}'...")
        nlp = spacy.load(model_name)
        logger.info(f"✅ Modelo spaCy '{model_name}' cargado.")
        if nlp.vocab.vectors.shape[0] == 0:
            logger.warning(f"⚠️ El modelo spaCy '{model_name}' se cargó pero no tiene vectores.")
        return nlp
    except OSError:
        logger.error(f"❌ Error al cargar spaCy '{model_name}': Modelo no encontrado.")
    except Exception as e:
        logger.error(f"❌ Error inesperado al cargar spaCy '{model_name}': {e}", exc_info=True)
    return None
