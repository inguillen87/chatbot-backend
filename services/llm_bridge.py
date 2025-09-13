import json
import logging
from typing import Any, Dict, Tuple

from services.logging_config import log_text_block

from cachetools import TTLCache

from services.openai_bridge import llamar_openai
from services.cohere_bridge import llamar_cohere

logger = logging.getLogger(__name__)

# Simple in-memory cache to avoid repeated LLM calls for identical queries
LLM_CACHE = TTLCache(maxsize=128, ttl=3600)


def clear_llm_cache() -> None:
    """Utility function primarily for tests to clear the cache."""
    LLM_CACHE.clear()


def _build_cache_key(
    mensaje_usuario: Any,
    chat_session_id: str | None = None,
    historial: list | None = None,
) -> str:
    """Build a cache key that includes session and history context."""
    key_obj: Dict[str, Any] = {"mensaje": mensaje_usuario, "session": chat_session_id}
    if historial:
        try:
            key_obj["history"] = json.dumps(historial, sort_keys=True)
        except Exception:
            key_obj["history"] = str(historial)
    try:
        return json.dumps(key_obj, sort_keys=True)
    except Exception:
        return str(key_obj)


def llamar_llm(
    app,
    mensaje_usuario: Any = None,
    usuario: Dict[str, Any] | None = None,
    historial: list | None = None,
    mensaje: Any = None,
    chat_session_id: str | None = None,
    timeout_seconds: int = 20,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Generic LLM wrapper using OpenAI with Cohere as fallback.

    This function keeps the previous signature for backwards compatibility.
    It first tries OpenAI and falls back to Cohere. Responses are
    cached in-memory to minimize repeated calls.
    """
    user_msg = mensaje_usuario if mensaje_usuario is not None else mensaje
    cache_key = _build_cache_key(user_msg, chat_session_id, historial)

    if cache_key in LLM_CACHE:
        logger.info("llamar_llm: returning cached response")
        return LLM_CACHE[cache_key]

    try:
        respuesta = llamar_openai(app, user_msg, usuario or {}, historial or [], chat_session_id)
        log_text_block(logger, "LLM OpenAI response", respuesta)
    except Exception as e:
        logger.error(f"OpenAI call failed: {e}; trying Cohere", exc_info=True)
        try:
            respuesta = llamar_cohere(app, user_msg, usuario or {}, historial or [], chat_session_id)
            log_text_block(logger, "LLM Cohere response", respuesta)
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


def llamar_llm_para_generacion_texto(
    system_prompt_especifico: str,
    user_prompt: str,
    temperature: float = 0.7,
    json_output: bool = False,
) -> str:
    """Generate text using OpenAI (with optional JSON formatting)."""
    try:
        from services.openai_bridge import client as openai_client

        if not openai_client:
            raise ConnectionError("OpenAI client is not initialized. Check API key.")

        messages = []
        if system_prompt_especifico:
            messages.append({"role": "system", "content": system_prompt_especifico})
        messages.append({"role": "user", "content": user_prompt})

        kwargs = {"model": "gpt-4o-mini", "messages": messages, "temperature": temperature}
        if json_output:
            kwargs["response_format"] = {"type": "json_object"}

        response = openai_client.chat.completions.create(**kwargs)
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Error en llamar_llm_para_generacion_texto: {e}", exc_info=True)
        return ""
