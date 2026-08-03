"""OpenAI Responses adapter for Chatboc's municipal and PyME contracts.

Language understanding stays in the model; this module owns the provider
boundary, strict output shape, privacy controls and defensive parsing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from typing import Any, Dict, List

import httpx
from openai import OpenAI

from services.chatbot_prompts import get_system_prompt
from services.openai_model_defaults import (
    DEFAULT_OPENAI_SOL_MODEL,
    DEFAULT_OPENAI_TERRA_MODEL,
)
from services.llm_provider_network_policy import require_llm_provider_network
from services.openai_text_service import _privacy_safe_identifier

try:
    import tiktoken

    _TOKEN_ENCODER = tiktoken.get_encoding("o200k_base")
except Exception:  # pragma: no cover - optional dependency
    _TOKEN_ENCODER = None


logger = logging.getLogger(__name__)

DEFAULT_CHAT_MODEL = DEFAULT_OPENAI_SOL_MODEL
_LEGACY_DEFAULT_MODEL = "gpt-4o-mini"
_CLIENT_LOCK = threading.Lock()
_CLIENT_KEY_DIGEST: str | None = None
_OPENAI_CLIENT: Any | None = None
_CLIENT_IS_MANAGED = False  # Compatibility marker retained for older tests.


class _LazyOpenAIClientProxy:
    """Preserve old ``from openai_bridge import client`` imports lazily."""

    def __getattr__(self, name: str) -> Any:
        # Introspection must describe the proxy itself.  In particular,
        # ``unittest.mock`` probes ``__func__`` before patching an object; if
        # that probe reached the provider it would defeat lazy initialization
        # (and could require network permission merely to install a mock).
        # Python's coroutine inspection also probes private marker attributes.
        # None of these are OpenAI client API surfaces, while public SDK
        # attributes continue to delegate to the real client.
        if (
            name.startswith("__") and name.endswith("__")
        ) or name in {"_is_coroutine", "_is_coroutine_marker"}:
            raise AttributeError(name)
        return getattr(_get_openai_client(None), name)

    def __bool__(self) -> bool:
        return _OPENAI_CLIENT is not None or bool(
            str(os.getenv("OPENAI_API_KEY") or "").strip()
        )


_PUBLIC_CLIENT_PROXY = _LazyOpenAIClientProxy()

# Existing modules import this symbol directly. The stable proxy ensures those
# imports do not capture ``None`` before dotenv/Flask configuration is loaded.
client: Any = _PUBLIC_CLIENT_PROXY


def _safe_log_identifier(value: Any, default: str = "unknown") -> str:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 80:
        return default
    if any(not (char.isalnum() or char in "._-/:") for char in candidate):
        return default
    return candidate


class LLMProviderError(RuntimeError):
    """Provider failure carrying only a stable, non-sensitive error code."""

    safe_to_fallback = False

    def __init__(self, code: str):
        self.code = str(code or "provider_error")
        super().__init__(self.code)


class LLMProviderPreRequestError(LLMProviderError):
    """A failure proven to have happened before any provider request."""

    safe_to_fallback = True


class LLMProviderRequestUncertainError(LLMProviderError):
    """A request may have reached the provider; callers must not retry it."""


def _config_value(app: Any, name: str, default: Any = None) -> Any:
    config = getattr(app, "config", None)
    if config is not None and hasattr(config, "get"):
        value = config.get(name)
        if value not in (None, ""):
            return value
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _get_openai_client(app: Any = None) -> Any:
    """Return a zero-retry OpenAI client created only when it is needed."""

    global _OPENAI_CLIENT, _CLIENT_KEY_DIGEST, _CLIENT_IS_MANAGED

    # A test or an embedding application may deliberately inject a client.
    if client is not _PUBLIC_CLIENT_PROXY:
        return client

    api_key = str(_config_value(app, "OPENAI_API_KEY", "") or "").strip()
    if not api_key:
        raise LLMProviderPreRequestError("openai_not_configured")
    require_llm_provider_network("openai", app)

    base_url = str(_config_value(app, "OPENAI_BASE_URL", "") or "").strip()
    try:
        timeout_seconds = float(_config_value(app, "OPENAI_TIMEOUT_SECONDS", 25))
    except (TypeError, ValueError):
        raise LLMProviderPreRequestError("openai_timeout_invalid") from None
    if timeout_seconds <= 0:
        raise LLMProviderPreRequestError("openai_timeout_invalid")

    fingerprint_material = f"{api_key}\0{base_url}\0{timeout_seconds}"
    key_digest = hashlib.sha256(fingerprint_material.encode("utf-8")).hexdigest()
    with _CLIENT_LOCK:
        if _OPENAI_CLIENT is not None and _CLIENT_KEY_DIGEST == key_digest:
            return _OPENAI_CLIENT
        try:
            http_client = httpx.Client(
                proxy=None,
                trust_env=False,
                timeout=timeout_seconds,
            )
            kwargs: dict[str, Any] = {
                "api_key": api_key,
                "http_client": http_client,
                "max_retries": 0,
                "timeout": timeout_seconds,
            }
            if base_url:
                kwargs["base_url"] = base_url
            _OPENAI_CLIENT = OpenAI(**kwargs)
        except Exception:
            logger.error("OpenAI client initialization failed code=openai_client_init_failed")
            raise LLMProviderPreRequestError("openai_client_init_failed") from None
        _CLIENT_KEY_DIGEST = key_digest
        _CLIENT_IS_MANAGED = True
        return _OPENAI_CLIENT


def _reset_openai_client_for_tests() -> None:
    """Reset lazy-client state without ever reading or exposing credentials."""

    global client, _OPENAI_CLIENT, _CLIENT_KEY_DIGEST, _CLIENT_IS_MANAGED
    client = _PUBLIC_CLIENT_PROXY
    _OPENAI_CLIENT = None
    _CLIENT_KEY_DIGEST = None
    _CLIENT_IS_MANAGED = False


def _nullable_string(description: str = "") -> dict[str, Any]:
    field: dict[str, Any] = {"type": ["string", "null"]}
    if description:
        field["description"] = description
    return field


def _nullable_number() -> dict[str, Any]:
    return {"anyOf": [{"type": "number"}, {"type": "string"}, {"type": "null"}]}


def _nullable_string_or_list() -> dict[str, Any]:
    return {
        "anyOf": [
            {"type": "string"},
            {"type": "array", "items": {"type": "string"}},
            {"type": "null"},
        ]
    }


def _strict_object(properties: dict[str, Any], description: str = "") -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
    if description:
        schema["description"] = description
    return schema


_COMMON_AGENT_DATA_PROPERTIES: dict[str, Any] = {
    "channel": _nullable_string(),
    "intent": _nullable_string(),
    "confidence": {
        "type": ["string", "null"],
        "enum": ["alta", "media", "baja", "high", "medium", "low", None],
    },
    "missing_fields": _nullable_string_or_list(),
    "template_intent": _nullable_string(),
    "handoff_reason": _nullable_string(),
    "priority": _nullable_string(),
    "summary": _nullable_string(),
}


_COORDINATES_SCHEMA = {
    "anyOf": [
        _strict_object(
            {
                "lat": _nullable_number(),
                "lon": _nullable_number(),
            },
            "Coordenadas del incidente, si fueron provistas.",
        ),
        {"type": "null"},
    ]
}

_ORDER_ITEM_SCHEMA = _strict_object(
    {
        "producto": _nullable_string(),
        "nombre": _nullable_string(),
        "cantidad": _nullable_number(),
        "sku": _nullable_string(),
        "observacion": _nullable_string(),
        "precio": _nullable_number(),
    }
)

_TOOL_PARAMETERS_SCHEMA = {
    "anyOf": [
        _strict_object(
            {
                "direccion": _nullable_string(),
                "fecha": _nullable_string(),
                "rubro": _nullable_string(),
                "localidad": _nullable_string(),
                "text": _nullable_string(),
                "query": _nullable_string(),
                "lat": _nullable_number(),
                "lon": _nullable_number(),
                "id": _nullable_string(),
                "extra_json": _nullable_string(
                    "Objeto JSON serializado para parametros no listados."
                ),
            }
        ),
        {"type": "null"},
    ]
}

_MUNICIPIO_DATA_SCHEMA = _strict_object(
    {
        **_COMMON_AGENT_DATA_PROPERTIES,
        "_contract_kind": {"type": "string", "enum": ["municipio"]},
        "target": {"type": ["string", "null"], "enum": ["municipio", None]},
        "categoria": _nullable_string(),
        "descripcion": _nullable_string(),
        "ubicacion": _nullable_string(),
        "distrito": _nullable_string(),
        "localidad": _nullable_string(),
        "direccion": _nullable_string(),
        "coordenadas": _COORDINATES_SCHEMA,
        "nombre": _nullable_string(),
        "nombre_usuario_detectado": _nullable_string(),
        "telefono": _nullable_string(),
        "telefono_detectado": _nullable_string(),
        "email": _nullable_string(),
        "email_detectado": _nullable_string(),
        "dni": _nullable_string(),
        "prioridad": {
            "type": ["string", "null"],
            "enum": ["normal", "alta", "emergencia", None],
        },
        "solicita_llamada": {"type": ["boolean", "null"]},
        "motivo_llamada": _nullable_string(),
        "datos_extra_json": _nullable_string(
            "Objeto JSON serializado para campos de accion no listados."
        ),
    },
    "Datos de reclamo, sugerencia o gestion municipal.",
)

_PYME_DATA_SCHEMA = _strict_object(
    {
        **_COMMON_AGENT_DATA_PROPERTIES,
        "_contract_kind": {"type": "string", "enum": ["pyme"]},
        "target": {"type": ["string", "null"], "enum": ["pyme", None]},
        "categoria": _nullable_string(),
        "category": _nullable_string(),
        "descripcion": _nullable_string(),
        "pregunta": _nullable_string(),
        "producto": _nullable_string(),
        "cantidad": _nullable_number(),
        "items": {
            "anyOf": [
                {"type": "array", "items": _ORDER_ITEM_SCHEMA},
                {"type": "null"},
            ]
        },
        "ubicacion": _nullable_string(),
        "direccion": _nullable_string(),
        "nombre": _nullable_string(),
        "telefono": _nullable_string(),
        "telefono_detectado": _nullable_string(),
        "email": _nullable_string(),
        "email_detectado": _nullable_string(),
        "fecha_turno": _nullable_string(),
        "especialidad": _nullable_string(),
        "vertical": _nullable_string(),
        "school_intent": _nullable_string(),
        "pedido_id": _nullable_string(),
        "metodo_entrega": _nullable_string(),
        "observaciones": _nullable_string(),
        "datos_extra_json": _nullable_string(
            "Objeto JSON serializado para campos de accion no listados."
        ),
    },
    "Datos comerciales, educativos, de turno o pedido PyME.",
)

_CORRECTION_DATA_SCHEMA = _strict_object(
    {
        **_COMMON_AGENT_DATA_PROPERTIES,
        "_contract_kind": {"type": "string", "enum": ["correccion"]},
        "target": {
            "type": ["string", "null"],
            "enum": ["municipio", "pyme", "ambos", None],
        },
        "campo_a_corregir": _nullable_string(),
        "nuevo_valor": _nullable_string(),
        "datos_extra_json": _nullable_string(
            "Objeto JSON serializado con correcciones adicionales."
        ),
    },
    "Correccion explicita de datos existentes.",
)

_TOOL_DATA_SCHEMA = _strict_object(
    {
        **_COMMON_AGENT_DATA_PROPERTIES,
        "_contract_kind": {"type": "string", "enum": ["herramienta"]},
        "target": {
            "type": ["string", "null"],
            "enum": ["municipio", "pyme", "ambos", None],
        },
        "nombre_herramienta": _nullable_string(),
        "parametros_herramienta": _TOOL_PARAMETERS_SCHEMA,
        "faltan_parametros_herramienta": _nullable_string_or_list(),
        "datos_extra_json": _nullable_string(
            "Objeto JSON serializado para campos de accion no listados."
        ),
    },
    "Solicitud para ejecutar una herramienta registrada.",
)

_GENERIC_DATA_SCHEMA = _strict_object(
    {
        **_COMMON_AGENT_DATA_PROPERTIES,
        "_contract_kind": {"type": "string", "enum": ["generico"]},
        "target": {
            "type": ["string", "null"],
            "enum": ["municipio", "pyme", "ambos", None],
        },
        "pregunta": _nullable_string(),
        "id_reclamo": _nullable_string(),
        "id_ticket": _nullable_string(),
        "pedido_id": _nullable_string(),
        "fecha_turno": _nullable_string(),
        "especialidad": _nullable_string(),
        "vertical": _nullable_string(),
        "school_intent": _nullable_string(),
        "finance_flow": _nullable_string(),
        "datos_extra_json": _nullable_string(
            "Objeto JSON serializado para campos de accion no listados."
        ),
    },
    "Consulta, estado, menu u otra gestion sin payload especializado.",
)

_EMPTY_DATA_SCHEMA = _strict_object(
    {
        **_COMMON_AGENT_DATA_PROPERTIES,
        "_contract_kind": {"type": "string", "enum": ["vacio"]},
    },
    "Accion que no necesita datos estructurados.",
)

CHATBOC_RESPONSE_SCHEMA: dict[str, Any] = _strict_object(
    {
        "message_body": {
            "type": "string",
            "description": "Respuesta profesional, directa y lista para el usuario final.",
        },
        "accion_backend": {
            "type": "string",
            "description": "Accion que Python debe validar y ejecutar.",
        },
        "datos_estructura": {
            "anyOf": [
                _MUNICIPIO_DATA_SCHEMA,
                _PYME_DATA_SCHEMA,
                _CORRECTION_DATA_SCHEMA,
                _TOOL_DATA_SCHEMA,
                _GENERIC_DATA_SCHEMA,
                _EMPTY_DATA_SCHEMA,
            ]
        },
        "pedir_info": _nullable_string_or_list(),
        "botones": {
            "type": "array",
            "items": _strict_object(
                {
                    "texto": {"type": "string"},
                    "action_id": _nullable_string(),
                }
            ),
        },
    }
)

CHATBOC_TEXT_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "name": "chatboc_response",
    "description": "Contrato de decision para asistentes municipales y PyME de Chatboc.",
    "strict": True,
    "schema": CHATBOC_RESPONSE_SCHEMA,
}


def _resolve_chat_model(
    explicit_model: str,
    usuario: dict,
    raw_user_message: str,
    historial: list | None = None,
) -> str:
    """Resolve explicit, workload and channel-specific model configuration."""

    default_model = os.getenv("OPENAI_CHAT_MODEL_DEFAULT") or DEFAULT_CHAT_MODEL
    if explicit_model and explicit_model not in {DEFAULT_CHAT_MODEL, _LEGACY_DEFAULT_MODEL}:
        return explicit_model

    channel = ""
    parsed_message: dict[str, Any] | None = None
    if isinstance(usuario, dict):
        channel = str(usuario.get("channel") or usuario.get("canal") or "").strip().lower()

    try:
        parsed = json.loads(raw_user_message)
        if isinstance(parsed, dict):
            parsed_message = parsed
            if not channel:
                channel = str(parsed.get("channel") or parsed.get("canal") or "").strip().lower()
    except (json.JSONDecodeError, TypeError):
        parsed_message = None

    try:
        complexity_min_chars = int(os.getenv("OPENAI_CHAT_COMPLEXITY_MIN_CHARS", "600"))
        complexity_min_turns = int(os.getenv("OPENAI_CHAT_COMPLEXITY_MIN_TURNS", "6"))
    except ValueError:
        complexity_min_chars, complexity_min_turns = 600, 6

    user_text = raw_user_message or ""
    if parsed_message:
        user_text = str(parsed_message.get("texto") or parsed_message.get("message") or user_text)

    is_high_complexity = (
        len(user_text) >= complexity_min_chars
        or len(historial or []) >= complexity_min_turns
    )
    is_premium_channel = channel == "whatsapp" or "widget" in channel or channel == "web"
    if is_high_complexity and is_premium_channel:
        return os.getenv("OPENAI_CHAT_MODEL_HIGH_COMPLEXITY") or os.getenv(
            "OPENAI_CHAT_MODEL_WHATSAPP", default_model
        )
    if channel == "whatsapp":
        return os.getenv("OPENAI_CHAT_MODEL_WHATSAPP", default_model)
    if "widget" in channel or channel == "web":
        return os.getenv("OPENAI_CHAT_MODEL_WIDGET", default_model)
    return default_model


def _estimate_tokens(text: str) -> int:
    if _TOKEN_ENCODER:
        return len(_TOKEN_ENCODER.encode(text))
    return max(1, len(text.split()))


def _history_text(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    content = item.get("content")
    if isinstance(content, str):
        return content
    parts = item.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) and part.get("text"):
                return str(part["text"])
            if isinstance(part, str) and part:
                return part
    return ""


def _prune_messages(messages: List[Dict[str, str]], limit: int) -> List[Dict[str, str]]:
    """Keep instructions and current turn, pruning only oldest history."""

    if len(messages) <= 2:
        return messages
    system_message, current_message = messages[0], messages[-1]
    total = _estimate_tokens(system_message["content"]) + _estimate_tokens(
        current_message["content"]
    )
    retained_history: list[Dict[str, str]] = []
    for history_message in reversed(messages[1:-1]):
        cost = _estimate_tokens(history_message.get("content", ""))
        if total + cost > limit:
            break
        retained_history.append(history_message)
        total += cost
    retained_history.reverse()
    return [system_message, *retained_history, current_message]


def _response_output_text(response: Any) -> str:
    direct = getattr(response, "output_text", None)
    if direct:
        return str(direct).strip()
    parts: list[str] = []
    for output_item in getattr(response, "output", None) or []:
        for content_item in getattr(output_item, "content", None) or []:
            if getattr(content_item, "type", None) == "refusal":
                raise LLMProviderRequestUncertainError("openai_refusal")
            text = getattr(content_item, "text", None)
            if text:
                parts.append(str(text))
    return "".join(parts).strip()


def _validate_completed_response(response: Any) -> None:
    status = str(getattr(response, "status", "completed") or "completed")
    if status == "completed":
        return
    details = getattr(response, "incomplete_details", None)
    reason = str(getattr(details, "reason", "") or "")
    if reason == "max_output_tokens":
        code = "openai_output_incomplete"
    elif reason == "content_filter":
        code = "openai_content_filtered"
    else:
        code = "openai_response_incomplete"
    raise LLMProviderRequestUncertainError(code)


def _usage_dict(response: Any) -> dict[str, Any] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    try:
        raw = usage.to_dict()
    except Exception:
        raw = {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        }
    if not isinstance(raw, dict):
        return None
    raw.setdefault("prompt_tokens", raw.get("input_tokens"))
    raw.setdefault("completion_tokens", raw.get("output_tokens"))
    return raw


def _merge_json_extension(target: dict[str, Any], field: str) -> None:
    raw = target.pop(field, None)
    if not isinstance(raw, str) or not raw.strip():
        return
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return
    if isinstance(parsed, dict):
        for key, value in parsed.items():
            if isinstance(key, str) and key not in target:
                target[key] = value


def _remove_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if item is None:
                continue
            cleaned[key] = _remove_nulls(item)
        return cleaned
    if isinstance(value, list):
        return [_remove_nulls(item) for item in value if item is not None]
    return value


def _normalize_chatboc_payload(parsed_response: Any, usuario: dict) -> dict[str, Any]:
    if not isinstance(parsed_response, dict):
        raise LLMProviderRequestUncertainError("openai_contract_invalid")

    message_body = parsed_response.get("message_body")
    accion_backend = parsed_response.get("accion_backend")
    datos = parsed_response.get("datos_estructura")
    botones = parsed_response.get("botones")
    if not isinstance(message_body, str) or not isinstance(accion_backend, str):
        raise LLMProviderRequestUncertainError("openai_contract_invalid")
    if not isinstance(datos, dict) or not isinstance(botones, list):
        raise LLMProviderRequestUncertainError("openai_contract_invalid")

    datos = dict(datos)
    datos.pop("_contract_kind", None)
    _merge_json_extension(datos, "datos_extra_json")
    parametros = datos.get("parametros_herramienta")
    if isinstance(parametros, dict):
        parametros = dict(parametros)
        _merge_json_extension(parametros, "extra_json")
        datos["parametros_herramienta"] = parametros
    datos = _remove_nulls(datos)

    tipo_entidad = usuario.get("tipo_entidad") if isinstance(usuario, dict) else None
    if tipo_entidad in {"pyme", "municipio"}:
        datos.setdefault("target", tipo_entidad)

    normalized_buttons: list[dict[str, Any]] = []
    for button in botones:
        if not isinstance(button, dict) or not isinstance(button.get("texto"), str):
            continue
        normalized_button = {"texto": button["texto"]}
        if button.get("action_id"):
            normalized_button["action_id"] = str(button["action_id"])
        normalized_buttons.append(normalized_button)

    return {
        "message_body": message_body,
        "respuesta_usuario": message_body,
        "accion_backend": accion_backend,
        "datos_estructura": datos,
        "pedir_info": parsed_response.get("pedir_info"),
        "botones": normalized_buttons,
    }


def _reasoning_for_model(model: str) -> dict[str, str] | None:
    if not str(model or "").lower().startswith("gpt-5.6"):
        return None
    effort = str(os.getenv("OPENAI_CHAT_REASONING_EFFORT") or "none").strip().lower()
    if effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
        effort = "none"
    return {"effort": effort}


def llamar_openai(
    app,
    mensaje_usuario: str,
    usuario: dict,
    historial: list,
    chat_session_id: str,
    model: str = DEFAULT_CHAT_MODEL,
) -> tuple[dict, dict]:
    """Call Responses once and return the validated Chatboc decision contract."""

    openai_client = _get_openai_client(app)
    responses_api = getattr(openai_client, "responses", None)
    if responses_api is None or not hasattr(responses_api, "create"):
        raise LLMProviderPreRequestError("openai_sdk_responses_unavailable")

    system_prompt = get_system_prompt(usuario or {})
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    for item in historial or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role == "model":
            role = "assistant"
        if role not in {"assistant", "user", "system", "developer"}:
            continue
        text = _history_text(item)
        if text:
            messages.append({"role": role, "content": text})

    message = ""
    try:
        message_data = json.loads(mensaje_usuario)
        if isinstance(message_data, dict):
            message = str(message_data.get("texto") or message_data.get("message") or message_data)
            channel_instruction = message_data.get("instruccion_canal")
            if channel_instruction:
                messages[0]["content"] += (
                    "\n\nCONTEXTO DEL CANAL (solo formato y accesibilidad): "
                    + str(channel_instruction)
                )
        else:
            message = str(message_data)
    except (json.JSONDecodeError, TypeError):
        message = str(mensaje_usuario or "")
    messages.append({"role": "user", "content": message})

    contract_instruction = (
        "\n\nCONTRATO ESTRUCTURADO: elegi la variante de datos_estructura que "
        "corresponda. Usa null para campos de esa variante que no apliquen. Para "
        "un campo de accion no listado, serializalo como objeto JSON en "
        "datos_extra_json. No afirmes que una accion fue ejecutada: Python valida "
        "y ejecuta despues de esta respuesta."
    )
    messages[0]["content"] += contract_instruction

    try:
        input_limit = int(os.getenv("OPENAI_CHAT_MAX_INPUT_TOKENS", "12000"))
        max_output_tokens = int(os.getenv("OPENAI_CHAT_MAX_OUTPUT_TOKENS", "4096"))
    except ValueError:
        raise LLMProviderPreRequestError("openai_token_limit_invalid") from None
    messages = _prune_messages(messages, max(1000, input_limit))
    total_prompt_tokens = sum(_estimate_tokens(item["content"]) for item in messages)

    resolved_model = _resolve_chat_model(
        model,
        usuario or {},
        str(mensaje_usuario),
        historial=historial,
    )
    request: dict[str, Any] = {
        "model": resolved_model,
        "input": messages,
        "text": {"format": CHATBOC_TEXT_FORMAT},
        "max_output_tokens": max(256, max_output_tokens),
        "store": False,
    }
    reasoning = _reasoning_for_model(resolved_model)
    if reasoning:
        request["reasoning"] = reasoning
    safety_identifier = _privacy_safe_identifier(
        chat_session_id
        or (usuario or {}).get("id")
        or (usuario or {}).get("user_id")
    )
    if safety_identifier:
        request["safety_identifier"] = safety_identifier

    logger.info(
        "OpenAI request started model=%s prompt_tokens_estimate=%s history_turns=%s",
        _safe_log_identifier(resolved_model),
        total_prompt_tokens,
        len(historial or []),
    )
    try:
        response = responses_api.create(**request)
    except LLMProviderError:
        raise
    except Exception:
        logger.error(
            "OpenAI request failed code=openai_request_failed model=%s",
            _safe_log_identifier(resolved_model),
        )
        # The SDK may fail after bytes were sent; switching endpoints or providers
        # could duplicate a semantic/transactional decision, so fail closed.
        raise LLMProviderRequestUncertainError("openai_request_failed") from None

    _validate_completed_response(response)
    raw_response_text = _response_output_text(response)
    if not raw_response_text:
        raise LLMProviderRequestUncertainError("openai_empty_response")
    try:
        parsed_response = json.loads(raw_response_text)
    except (TypeError, ValueError):
        raise LLMProviderRequestUncertainError("openai_contract_invalid") from None
    normalized = _normalize_chatboc_payload(parsed_response, usuario or {})
    usage = _usage_dict(response)
    actual_model = str(getattr(response, "model", "") or resolved_model)
    logger.info(
        "OpenAI request completed model=%s contract_valid=true total_tokens=%s",
        _safe_log_identifier(actual_model),
        (usage or {}).get("total_tokens"),
    )
    return normalized, {
        "usage": usage,
        "prompt_tokens_estimate": total_prompt_tokens,
        "model_used": actual_model,
        "provider": "openai",
    }


def _call_structured_response(
    *,
    app: Any,
    model: str,
    system_prompt: str,
    user_prompt: str,
    schema_name: str,
    schema: dict[str, Any],
    max_output_tokens: int = 2048,
    safety_subject: Any = None,
) -> dict[str, Any]:
    openai_client = _get_openai_client(app)
    responses_api = getattr(openai_client, "responses", None)
    if responses_api is None or not hasattr(responses_api, "create"):
        raise LLMProviderPreRequestError("openai_sdk_responses_unavailable")
    request: dict[str, Any] = {
        "model": model,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": schema,
            }
        },
        "max_output_tokens": max_output_tokens,
        "store": False,
    }
    reasoning = _reasoning_for_model(model)
    if reasoning:
        request["reasoning"] = reasoning
    safety_identifier = _privacy_safe_identifier(safety_subject)
    if safety_identifier:
        request["safety_identifier"] = safety_identifier
    try:
        response = responses_api.create(**request)
    except Exception:
        raise LLMProviderRequestUncertainError("openai_request_failed") from None
    _validate_completed_response(response)
    text = _response_output_text(response)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        raise LLMProviderRequestUncertainError("openai_contract_invalid") from None
    if not isinstance(parsed, dict):
        raise LLMProviderRequestUncertainError("openai_contract_invalid")
    return parsed


_ANALYTICS_REPORT_SCHEMA = _strict_object(
    {
        "summary": {"type": "string"},
        "opportunities": {"type": "array", "items": {"type": "string"}},
        "threats": {"type": "array", "items": {"type": "string"}},
        "tone": {"type": "string"},
    }
)


def generate_analytics_report(stats: dict, tenant_type: str = "pyme") -> dict:
    """Generate a schema-constrained municipal or PyME analytics report."""

    if tenant_type == "municipio":
        system_prompt = (
            "Sos consultor senior de ciudades inteligentes y gestion publica. "
            "Analiza las estadisticas sin inventar datos. Resume eficiencia y "
            "satisfaccion; propone tres acciones y tres riesgos concretos."
        )
    else:
        system_prompt = (
            "Sos consultor senior de PyMEs y analista comercial. Analiza las "
            "estadisticas sin inventar datos. Resume rendimiento; propone tres "
            "oportunidades y tres riesgos concretos."
        )
    try:
        return _call_structured_response(
            app=None,
            model=os.getenv("OPENAI_ANALYTICS_MODEL", DEFAULT_OPENAI_TERRA_MODEL),
            system_prompt=system_prompt,
            user_prompt=json.dumps(stats, default=str, ensure_ascii=False),
            schema_name="chatboc_analytics_report",
            schema=_ANALYTICS_REPORT_SCHEMA,
        )
    except Exception as exc:
        logger.error(
            "OpenAI analytics unavailable error_type=%s",
            type(exc).__name__,
        )
        return {
            "summary": "Could not generate report due to an error.",
            "opportunities": [],
            "threats": [],
            "tone": "Error",
        }


_SENTIMENT_SCHEMA = _strict_object(
    {
        "sentiment_score": {"type": "number", "minimum": -1, "maximum": 1},
        "keywords": {
            "type": "array",
            "items": _strict_object(
                {"word": {"type": "string"}, "count": {"type": "integer"}}
            ),
        },
    }
)


def analyze_sentiment(texts: List[str]) -> dict:
    """Analyze up to 50 survey answers with a constrained output contract."""

    if not texts:
        return {"sentiment_score": 0.0, "keywords": []}
    sample = [str(text) for text in texts[:50] if str(text).strip()]
    try:
        return _call_structured_response(
            app=None,
            model=os.getenv("OPENAI_SENTIMENT_MODEL", DEFAULT_OPENAI_TERRA_MODEL),
            system_prompt=(
                "Analiza el sentimiento de opiniones. Devuelve un puntaje entre "
                "-1 y 1 y hasta cinco temas recurrentes, sin inventar frecuencia."
            ),
            user_prompt="\n".join(f"- {text}" for text in sample),
            schema_name="chatboc_sentiment",
            schema=_SENTIMENT_SCHEMA,
            max_output_tokens=1024,
        )
    except Exception as exc:
        logger.error(
            "OpenAI sentiment unavailable error_type=%s",
            type(exc).__name__,
        )
        return {"sentiment_score": 0.0, "keywords": []}


_TICKET_SUMMARY_SCHEMA = _strict_object(
    {
        "summary": {"type": "string"},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "confidence": {
            "type": "string",
            "enum": ["low", "medium", "high"],
        },
    }
)


def generate_ticket_summary(ticket_data: dict) -> dict:
    """Generate a concise, non-invented summary for a ticket timeline."""

    timeline = ticket_data.get("timeline") or []
    try:
        return _call_structured_response(
            app=None,
            model=os.getenv("OPENAI_TICKET_SUMMARY_MODEL", DEFAULT_OPENAI_TERRA_MODEL),
            system_prompt=(
                "Sos analista senior de operaciones. Resume el historial sin "
                "inventar hechos y propone tres proximos pasos concretos."
            ),
            user_prompt=json.dumps(ticket_data, default=str, ensure_ascii=False),
            schema_name="chatboc_ticket_summary",
            schema=_TICKET_SUMMARY_SCHEMA,
            safety_subject=ticket_data.get("id") or ticket_data.get("numero_ticket"),
        )
    except Exception as exc:
        logger.error(
            "OpenAI ticket summary unavailable error_type=%s",
            type(exc).__name__,
        )
        base = ticket_data.get("pregunta") or ticket_data.get("asunto") or "Caso sin descripcion"
        last_state = ticket_data.get("estado") or "sin estado"
        return {
            "summary": (
                f"Caso: {base}. Estado actual: {last_state}. "
                f"Historial analizado: {len(timeline)} eventos."
            ),
            "next_steps": [
                "Confirmar prioridad y responsable",
                "Actualizar al usuario con estado actual",
                "Definir criterio de cierre y seguimiento",
            ],
            "confidence": "medium",
        }
