import importlib
import logging
import os
from functools import lru_cache

_SPACY_IMPORT_ERROR: Exception | None = None
_SPACY_MODULE: object | None = None


class _BlankToken:
    def __init__(self, text: str) -> None:
        self.text = text
        self.lemma_ = text
        self.is_space = False


class _BlankDoc(list):
    has_vector = False
    vector_norm = 0

    def similarity(self, other: object) -> float:
        return 0.0


class _BlankVectors:
    shape = (0, 0)


class _BlankVocab:
    vectors = _BlankVectors()


class _BlankDefaults:
    stop_words = {
        "a",
        "al",
        "con",
        "de",
        "del",
        "el",
        "en",
        "la",
        "las",
        "los",
        "para",
        "por",
        "que",
        "un",
        "una",
        "y",
    }


class _BlankSpanishPipeline:
    Defaults = _BlankDefaults
    lang = "es"
    vocab = _BlankVocab()

    def __call__(self, text: str) -> _BlankDoc:
        return _BlankDoc(_BlankToken(token) for token in str(text or "").split())


class _UnavailableSpacyModule:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def load(self, model_name: str):
        raise RuntimeError(f"spaCy is not importable: {self._exc}") from self._exc

    def blank(self, language: str):
        return _BlankSpanishPipeline()


def _load_spacy_module():
    """Import spaCy only when an NLP feature is used for the first time."""
    global _SPACY_IMPORT_ERROR, _SPACY_MODULE

    if _SPACY_MODULE is not None:
        return _SPACY_MODULE

    try:
        _SPACY_MODULE = importlib.import_module("spacy")
    except Exception as exc:  # pragma: no cover - depends on local binary deps
        _SPACY_IMPORT_ERROR = exc
        _SPACY_MODULE = _UnavailableSpacyModule(exc)
    return _SPACY_MODULE


class _LazySpacyModule:
    """Compatibility proxy that preserves ``spacy.load``/``blank`` callers."""

    def load(self, model_name: str):
        return _load_spacy_module().load(model_name)

    def blank(self, language: str):
        return _load_spacy_module().blank(language)


if os.getenv("TESTING") == "1" or os.getenv("CHATBOC_DISABLE_SPACY") == "1":
    _SPACY_IMPORT_ERROR = RuntimeError("spaCy disabled for this process")
    spacy = _UnavailableSpacyModule(_SPACY_IMPORT_ERROR)  # type: ignore[assignment]
else:
    spacy = _LazySpacyModule()  # type: ignore[assignment]

logger = logging.getLogger(__name__)


@lru_cache(maxsize=None)
def get_spacy_model(model_name: str = "es_core_news_md"):
    """Load and reuse spaCy, falling back to a tiny Spanish-like pipeline."""
    if _SPACY_IMPORT_ERROR is not None:
        logger.warning("spaCy no esta disponible; se usa fallback liviano: %s", _SPACY_IMPORT_ERROR)
        return _BlankSpanishPipeline()
    try:
        logger.info("Cargando modelo spaCy '%s'...", model_name)
        nlp = spacy.load(model_name)
        logger.info("Modelo spaCy '%s' cargado.", model_name)
        if nlp.vocab.vectors.shape[0] == 0:
            logger.warning("El modelo spaCy '%s' se cargo pero no tiene vectores.", model_name)
        return nlp
    except OSError:
        logger.warning(
            "spaCy model '%s' not available; falling back to blank Spanish pipeline.",
            model_name,
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.error("Error inesperado al cargar spaCy '%s': %s", model_name, exc, exc_info=True)

    try:
        fallback = spacy.blank("es")
    except Exception:
        fallback = _BlankSpanishPipeline()
    logger.info("Fallback spaCy blank('es') cargado para '%s'.", model_name)
    return fallback
