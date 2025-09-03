import json
import logging
from typing import Any, Dict, Tuple

from cachetools import TTLCache

from services.openai_bridge import llamar_openai
from services.cohere_bridge import llamar_cohere

logger = logging.getLogger(__name__)

# Simple in-memory cache to avoid repeated LLM calls for identical queries
LLM_CACHE = TTLCache(maxsize=128, ttl=3600)


def clear_llm_cache() -> None:
    """Utility function primarily for tests to clear the cache."""
    LLM_CACHE.clear()


def _build_cache_key(mensaje_usuario: Any) -> str:
    if isinstance(mensaje_usuario, dict):
        try:
            return json.dumps(mensaje_usuario, sort_keys=True)
        except Exception:
            return str(mensaje_usuario)
    return str(mensaje_usuario)


def llamar_gemini(
    app,
    mensaje_usuario: Any = None,
    usuario: Dict[str, Any] | None = None,
    historial: list | None = None,
    mensaje: Any = None,
    chat_session_id: str | None = None,
    timeout_seconds: int = 20,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Wrapper that uses OpenAI or Cohere instead of Gemini.

    This function keeps the old signature to avoid touching the rest of the codebase.
    It first tries OpenAI and falls back to Cohere. Responses are cached in-memory.
    """
    user_msg = mensaje_usuario if mensaje_usuario is not None else mensaje
    cache_key = _build_cache_key(user_msg)

    if cache_key in LLM_CACHE:
        logger.info("llamar_gemini: returning cached response")
        return LLM_CACHE[cache_key]

    try:
        respuesta = llamar_openai(app, user_msg, usuario or {}, historial or [], chat_session_id)
    except Exception as e:
        logger.error(f"OpenAI call failed: {e}; trying Cohere", exc_info=True)
        try:
            respuesta = llamar_cohere(app, user_msg, usuario or {}, historial or [], chat_session_id)
        except Exception as e2:
            logger.error(f"Cohere call failed: {e2}", exc_info=True)
            error_response = ({
                "message_body": "El asistente IA está tardando más de lo normal en responder. Por favor, intenta de nuevo en unos momentos.",
                "accion_backend": "derivar_humano",
                "datos_estructura": {"error_detalle": "llm_unavailable"},
                "pedir_info": None,
                "botones": [],
            }, {})
            LLM_CACHE[cache_key] = error_response
            return error_response

    LLM_CACHE[cache_key] = respuesta
    return respuesta


def llamar_gemini_para_generacion_texto(prompt: str) -> str:
    """Convenience wrapper for generating plain text via the main LLM call."""
    try:
        respuesta, _ = llamar_gemini(None, prompt, {}, [])
        return respuesta.get("message_body", "")
    except Exception as e:
        logger.error(f"Error en llamar_gemini_para_generacion_texto: {e}", exc_info=True)
        return ""
