"""Compatibility facade for Chatboc's provider orchestrator."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Dict, Tuple

from cachetools import TTLCache

from services.llm_orchestrator import llamar_llm_con_fallback
from utils.response_utils import normalize_response_payload


logger = logging.getLogger(__name__)

DEFAULT_CHAT_MODEL = "gpt-5.6-sol"

# Short-lived process-local cache. Keys are hashes, not message/PII strings.
LLM_CACHE = TTLCache(maxsize=128, ttl=3600)


def _safe_log_identifier(value: Any, default: str = "unknown") -> str:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 80:
        return default
    if any(not (char.isalnum() or char in "._-/:") for char in candidate):
        return default
    return candidate


def clear_llm_cache() -> None:
    """Utility primarily used by tests and configuration reloads."""

    LLM_CACHE.clear()


def _build_cache_key(
    mensaje_usuario: Any,
    chat_session_id: str | None = None,
    historial: list | None = None,
    usuario: Dict[str, Any] | None = None,
) -> str:
    """Build an opaque cache key scoped to the conversation and tenant context."""

    user_scope = usuario or {}
    key_obj: Dict[str, Any] = {
        "mensaje": mensaje_usuario,
        "session": chat_session_id,
        "scope": {
            "tipo_entidad": user_scope.get("tipo_entidad"),
            "tenant_id": user_scope.get("tenant_id"),
            "municipio_id": user_scope.get("municipio_id"),
            "user_id": user_scope.get("user_id") or user_scope.get("id"),
            "channel": user_scope.get("channel") or user_scope.get("canal"),
        },
    }
    if historial:
        key_obj["history"] = historial
    try:
        canonical = json.dumps(
            key_obj,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
    except Exception:
        canonical = repr(key_obj)
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()


def _controlled_llm_error(
    error_detail: str = "llm_unavailable",
    message: str | None = None,
) -> Dict[str, Any]:
    payload = {
        "message_body": message
        or (
            "El asistente IA esta tardando mas de lo normal en responder. "
            "Por favor, intenta de nuevo en unos momentos."
        ),
        "accion_backend": "derivar_humano",
        "datos_estructura": {"error_detalle": error_detail},
        "pedir_info": None,
        "botones": [],
    }
    normalize_response_payload(payload)
    return payload


def _normalize_llm_bridge_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {
            "message_body": str(payload or ""),
            "accion_backend": "responder_directamente",
        }
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
    model: str = DEFAULT_CHAT_MODEL,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Call the configured orchestrator and normalize the legacy app contract.

    The public signature is preserved. ``timeout_seconds`` remains available to
    callers while the provider client's timeout is centrally configured.
    """

    del timeout_seconds  # Provider timeouts are configured once on the lazy client.
    user_context = usuario or {}
    conversation_history = historial or []
    user_msg = mensaje_usuario if mensaje_usuario is not None else mensaje
    cache_key = _build_cache_key(
        user_msg,
        chat_session_id,
        conversation_history,
        user_context,
    )
    if cache_key in LLM_CACHE:
        logger.info("LLM cache hit")
        return LLM_CACHE[cache_key]

    resolved_model = (
        str(model or "").strip()
        or os.getenv("OPENAI_CHAT_MODEL_DEFAULT")
        or DEFAULT_CHAT_MODEL
    )
    should_cache = False
    try:
        respuesta_payload, respuesta_context = llamar_llm_con_fallback(
            app,
            user_msg,
            user_context,
            conversation_history,
            chat_session_id,
            model=resolved_model,
        )
        normalized_payload = _normalize_llm_bridge_payload(respuesta_payload)
        if normalized_payload.get("accion_backend") == "error_fatal_llm":
            normalized_payload = _controlled_llm_error(
                "llm_unavailable",
                normalized_payload.get("message_body"),
            )
        else:
            should_cache = True
        response_context = respuesta_context if isinstance(respuesta_context, dict) else {}
        respuesta = (normalized_payload, response_context)
        provider = str(response_context.get("provider") or "").strip().lower()
        if provider not in {"openai", "gemini", "ollama", "cohere"}:
            provider = "unknown"
        logger.info(
            "LLM orchestrator completed provider=%s contract_valid=true",
            provider,
        )
    except Exception as exc:
        logger.error(
            "LLM orchestrator unavailable error_type=%s",
            type(exc).__name__,
        )
        respuesta = (_controlled_llm_error("llm_unavailable"), {})

    # Never cache provider failures or controlled handoff errors for an hour.
    if should_cache:
        LLM_CACHE[cache_key] = respuesta
    return respuesta


def llamar_llm_para_generacion_texto(
    system_prompt_especifico: str,
    user_prompt: str,
    temperature: float = 0.7,
    json_output: bool = False,
    model: str = DEFAULT_CHAT_MODEL,
) -> str:
    """Generate text through Responses without retrying another API endpoint."""

    from services.openai_bridge import (
        LLMProviderPreRequestError,
        LLMProviderRequestUncertainError,
        _get_openai_responses_client,
        _model_for_openai_transport,
        _reasoning_for_model,
        _response_output_text,
        _validate_completed_response,
    )

    resolved_model = (
        str(model or "").strip()
        or os.getenv("OPENAI_CHAT_MODEL_DEFAULT")
        or DEFAULT_CHAT_MODEL
    )
    try:
        openai_client = _get_openai_responses_client(None)
        responses_api = getattr(openai_client, "responses", None)
        if responses_api is None or not hasattr(responses_api, "create"):
            raise LLMProviderPreRequestError("openai_sdk_responses_unavailable")

        provider_model = _model_for_openai_transport(resolved_model)
        request: dict[str, Any] = {
            "model": provider_model,
            "input": [
                {
                    "role": "system",
                    "content": system_prompt_especifico or "Sos un asistente util.",
                },
                {"role": "user", "content": str(user_prompt or "")},
            ],
            "max_output_tokens": int(
                os.getenv("OPENAI_TEXT_MAX_OUTPUT_TOKENS", "4096")
            ),
            "store": False,
        }
        if json_output:
            request["text"] = {"format": {"type": "json_object"}}
            request["input"][0]["content"] += " Devuelve un unico objeto JSON valido."
        reasoning = _reasoning_for_model(provider_model)
        if reasoning:
            request["reasoning"] = reasoning
        elif temperature is not None:
            request["temperature"] = float(temperature)

        try:
            response = responses_api.create(**request)
        except Exception:
            raise LLMProviderRequestUncertainError("openai_request_failed") from None
        _validate_completed_response(response)
        result = _response_output_text(response)
        if not result:
            raise LLMProviderRequestUncertainError("openai_empty_response")
        logger.info(
            "OpenAI text generation completed model=%s",
            _safe_log_identifier(resolved_model),
        )
        return result
    except Exception as exc:
        logger.error(
            "OpenAI text generation unavailable error_type=%s",
            type(exc).__name__,
        )
        return ""
