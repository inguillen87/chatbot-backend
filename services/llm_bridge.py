import os
import json
import logging
from typing import Any, Dict, Tuple

from services.logging_config import log_text_block

from cachetools import TTLCache

from services.llm_orchestrator import llamar_llm_con_fallback
from utils.response_utils import normalize_response_payload

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


def _controlled_llm_error(error_detail: str = "llm_unavailable", message: str | None = None) -> Dict[str, Any]:
    payload = {
        "message_body": message
        or "El asistente IA esta tardando mas de lo normal en responder. Por favor, intenta de nuevo en unos momentos.",
        "accion_backend": "derivar_humano",
        "datos_estructura": {"error_detalle": error_detail},
        "pedir_info": None,
        "botones": [],
    }
    normalize_response_payload(payload)
    return payload


def _normalize_llm_bridge_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {"message_body": str(payload or ""), "accion_backend": "responder_directamente"}
    else:
        payload = dict(payload)

    normalize_response_payload(payload)

    if not isinstance(payload.get("datos_estructura"), dict):
        payload["datos_estructura"] = {}
    if not isinstance(payload.get("botones"), list):
        payload["botones"] = []
    if not isinstance(payload.get("options_list"), list):
        payload["options_list"] = payload["botones"]

    payload.setdefault("accion_backend", "responder_directamente")
    payload.setdefault("pedir_info", None)
    return payload


def llamar_llm(
    app,
    mensaje_usuario: Any = None,
    usuario: Dict[str, Any] | None = None,
    historial: list | None = None,
    mensaje: Any = None,
    chat_session_id: str | None = None,
    timeout_seconds: int = 20,
    model: str = "gpt-4o-mini",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Generic LLM wrapper using the configured multi-provider orchestrator.

    This function keeps the previous signature for backwards compatibility.
    Responses are cached in-memory to minimize repeated calls, and payloads are
    normalized to the legacy Chatboc response contract expected by municipal and
    PyME flows.
    """
    user_msg = mensaje_usuario if mensaje_usuario is not None else mensaje
    cache_key = _build_cache_key(user_msg, chat_session_id, historial)

    if cache_key in LLM_CACHE:
        logger.info("llamar_llm: returning cached response")
        return LLM_CACHE[cache_key]

    resolved_model = model or os.getenv("OPENAI_CHAT_MODEL_DEFAULT", "gpt-4o-mini")

    try:
        respuesta_payload, respuesta_context = llamar_llm_con_fallback(
            app,
            user_msg,
            usuario or {},
            historial or [],
            chat_session_id,
            model=resolved_model,
        )
        normalized_payload = _normalize_llm_bridge_payload(respuesta_payload)
        if normalized_payload.get("accion_backend") == "error_fatal_llm":
            normalized_payload = _controlled_llm_error(
                "llm_unavailable",
                normalized_payload.get("message_body"),
            )
        respuesta = (normalized_payload, respuesta_context or {})
        log_text_block(logger, "LLM orchestrator response", respuesta)
    except Exception as exc:
        logger.error("LLM orchestrator call failed: %s", exc, exc_info=True)
        respuesta = (_controlled_llm_error("llm_unavailable"), {})

    LLM_CACHE[cache_key] = respuesta
    return respuesta


def llamar_llm_para_generacion_texto(
    system_prompt_especifico: str,
    user_prompt: str,
    temperature: float = 0.7,
    json_output: bool = False,
    model: str = "gpt-4o-mini",
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

        kwargs = {"model": model, "messages": messages, "temperature": temperature}
        if json_output:
            kwargs["response_format"] = {"type": "json_object"}
            # Ensure the prompt contains the word "JSON" as required by OpenAI
            json_instruction = " Respond in JSON format."
            if messages and messages[0]["role"] == "system":
                messages[0]["content"] += json_instruction
            else:
                messages.insert(0, {"role": "system", "content": "You are a helpful assistant." + json_instruction})

        response = openai_client.chat.completions.create(**kwargs)
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Error en llamar_llm_para_generacion_texto: {e}", exc_info=True)
        return ""
