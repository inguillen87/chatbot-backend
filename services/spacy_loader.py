import logging
from functools import lru_cache

import spacy

logger = logging.getLogger(__name__)


@lru_cache(maxsize=None)
def get_spacy_model(model_name: str = "es_core_news_md"):
    """Carga y reutiliza un modelo spaCy con fallback seguro a un pipeline vacío."""
    try:
        logger.info("Cargando modelo spaCy '%s'...", model_name)
        nlp = spacy.load(model_name)
        logger.info("✅ Modelo spaCy '%s' cargado.", model_name)
        if nlp.vocab.vectors.shape[0] == 0:
            logger.warning("⚠️ El modelo spaCy '%s' se cargó pero no tiene vectores.", model_name)
        return nlp
    except OSError:
        logger.warning(
            "spaCy model '%s' not available; falling back to blank Spanish pipeline.",
            model_name,
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.error("❌ Error inesperado al cargar spaCy '%s': %s", model_name, exc, exc_info=True)

    fallback = spacy.blank("es")
    logger.info("✅ Fallback spaCy blank('es') cargado para '%s'.", model_name)
    return fallback
