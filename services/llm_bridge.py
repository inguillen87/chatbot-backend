import json
import logging
import os
import time
from typing import Any, Dict, Tuple

from services.logging_config import log_text_block

from cachetools import TTLCache

from services.openai_bridge import llamar_openai
from services.cohere_bridge import llamar_cohere

logger = logging.getLogger(__name__)

# Simple in-memory cache to avoid repeated LLM calls for identical queries
LLM_CACHE = TTLCache(maxsize=128, ttl=3600)

# Circuit breaker timestamp for OpenAI (epoch seconds). If in the future,
# OpenAI is skipped and fallback is used directly.
OPENAI_CIRCUIT_BREAKER_UNTIL = 0.0


def clear_llm_cache() -> None:
    """Utility function primarily for tests to clear the cache and reset state."""
    global OPENAI_CIRCUIT_BREAKER_UNTIL
    LLM_CACHE.clear()
    OPENAI_CIRCUIT_BREAKER_UNTIL = 0.0


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
    """Generic LLM wrapper with provider selection, retries and circuit breaker.

    Tries the primary provider first and falls back if necessary. Responses are
    cached in-memory to minimize repeated calls.
    """

    user_msg = mensaje_usuario if mensaje_usuario is not None else mensaje
    cache_key = _build_cache_key(user_msg, chat_session_id, historial)

    if cache_key in LLM_CACHE:
        logger.info("llamar_llm: returning cached response")
        return LLM_CACHE[cache_key]

    # Helper to pull config from app or environment
    def _cfg(key: str, default: str) -> str:
        if app and getattr(app, "config", None) and key in app.config:
            return str(app.config.get(key, default))
        return os.getenv(key, default)

    primary = _cfg("AI_PRIMARY", "openai").lower()
    fallback = _cfg("AI_FALLBACK", "cohere").lower()
    providers = [p for p in [primary, fallback] if p]

    max_retries = int(_cfg("MAX_RETRIES_OPENAI", "2"))
    backoff_ms = int(_cfg("BACKOFF_MS", "500"))
    circuit_breaker_sec = int(_cfg("CIRCUIT_BREAKER_SEC", str(5 * 60)))

    last_exception: Exception | None = None

    for provider in providers:
        if provider == "openai":
            global OPENAI_CIRCUIT_BREAKER_UNTIL
            now = time.time()
            if now < OPENAI_CIRCUIT_BREAKER_UNTIL:
                logger.warning("OpenAI circuit breaker active; skipping OpenAI")
                continue
            for attempt in range(max_retries):
                try:
                    respuesta = llamar_openai(app, user_msg, usuario or {}, historial or [], chat_session_id)
                    log_text_block(logger, "LLM OpenAI response", respuesta)
                    LLM_CACHE[cache_key] = respuesta
                    return respuesta
                except Exception as e:
                    last_exception = e
                    logger.error(f"OpenAI call failed (attempt {attempt + 1}/{max_retries}): {e}")
                    if attempt < max_retries - 1:
                        sleep_ms = backoff_ms * (2 ** attempt)
                        time.sleep(sleep_ms / 1000)
            OPENAI_CIRCUIT_BREAKER_UNTIL = time.time() + circuit_breaker_sec
            logger.error("OpenAI circuit breaker enabled for %s seconds", circuit_breaker_sec)

        elif provider == "cohere":
            try:
                respuesta = llamar_cohere(app, user_msg, usuario or {}, historial or [], chat_session_id)
                log_text_block(logger, "LLM Cohere response", respuesta)
                LLM_CACHE[cache_key] = respuesta
                return respuesta
            except Exception as e:
                last_exception = e
                logger.error(f"Cohere call failed: {e}", exc_info=True)
        else:
            logger.error(f"Unknown LLM provider: {provider}")

    logger.error(f"All LLM providers failed: {last_exception}")
    error_response = ({
        "message_body": "El asistente IA está tardando más de lo normal en responder. Por favor, intenta de nuevo en unos momentos.",
        "accion_backend": "derivar_humano",
        "datos_estructura": {"error_detalle": "llm_unavailable"},
        "pedir_info": None,
        "botones": [],
    }, {})
    LLM_CACHE[cache_key] = error_response
    return error_response


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

        if json_output and not any("json" in (m.get("content", "").lower()) for m in messages):
            messages.insert(0, {"role": "system", "content": "Responde solo con un objeto JSON válido."})

        kwargs = {"model": "gpt-4o-mini", "messages": messages, "temperature": temperature}
        if json_output:
            kwargs["response_format"] = {"type": "json_object"}

        response = openai_client.chat.completions.create(**kwargs)
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Error en llamar_llm_para_generacion_texto: {e}", exc_info=True)
        return ""
