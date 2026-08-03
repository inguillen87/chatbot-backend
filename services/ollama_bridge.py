import json
import logging
import os
from typing import Any, Dict, Tuple

import httpx
from openai import OpenAI

from services.chatbot_prompts import get_system_prompt
from services.llm_provider_network_policy import require_llm_provider_network

logger = logging.getLogger(__name__)


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_first(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return default


def is_ollama_llm_configured() -> bool:
    """Return true only when Ollama is explicitly enabled.

    Ollama can point to local `http://localhost:11434/v1` or to Ollama Cloud.
    We keep it opt-in so experimental/open-source models never enter critical
    WhatsApp flows by accident.
    """

    return _truthy(os.getenv("OLLAMA_ENABLED") or os.getenv("LLM_OLLAMA_ENABLED"))


def _ollama_base_url() -> str:
    return (
        _env_first("OLLAMA_BASE_URL", "OLLAMA_OPENAI_BASE_URL", default="http://localhost:11434/v1")
        or "http://localhost:11434/v1"
    ).rstrip("/")


def _ollama_api_key() -> str:
    return _env_first("OLLAMA_API_KEY", "OLLAMA_CLOUD_API_KEY", default="ollama") or "ollama"


def _ollama_chat_model() -> str:
    return _env_first("OLLAMA_CHAT_MODEL", "OLLAMA_MODEL", default="glm-5.2:cloud") or "glm-5.2:cloud"


def _extract_message_payload(mensaje_usuario: Any) -> tuple[str, str | None]:
    try:
        payload = json.loads(mensaje_usuario)
    except (json.JSONDecodeError, TypeError):
        return str(mensaje_usuario or ""), None

    if not isinstance(payload, dict):
        return str(payload), None

    text = payload.get("texto") or payload.get("message") or payload.get("body")
    return str(text or payload), payload.get("instruccion_canal")


def _history_to_messages(historial: list) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for item in historial or []:
        role = item.get("role")
        if role == "model":
            role = "assistant"
        if role not in {"assistant", "user", "system"}:
            continue

        text = item.get("parts", [{}])[0].get("text", "")
        if not text:
            continue
        messages.append({"role": role, "content": str(text)})
    return messages


def _parse_llm_json(raw_response_text: str, usuario: dict | None = None) -> dict:
    raw_response_text = (raw_response_text or "").strip()
    if raw_response_text.startswith("```json"):
        raw_response_text = raw_response_text[len("```json") :].strip()
    if raw_response_text.startswith("```"):
        raw_response_text = raw_response_text[len("```") :].strip()
    if raw_response_text.endswith("```"):
        raw_response_text = raw_response_text[: -len("```")].strip()

    parsed_response = json.loads(raw_response_text)
    if not isinstance(parsed_response, dict):
        raise ValueError("Ollama response is not a JSON object.")

    parsed_response.setdefault("message_body", parsed_response.get("respuesta_usuario", ""))
    parsed_response.setdefault("accion_backend", "responder_directamente")
    parsed_response.setdefault("pedir_info", None)

    if not isinstance(parsed_response.get("datos_estructura"), dict):
        parsed_response["datos_estructura"] = {}

    if usuario and isinstance(usuario, dict):
        tipo_entidad = usuario.get("tipo_entidad")
        if tipo_entidad in {"pyme", "municipio"}:
            parsed_response["datos_estructura"].setdefault("target", tipo_entidad)

    if not isinstance(parsed_response.get("botones"), list):
        parsed_response["botones"] = []

    return parsed_response


def llamar_ollama(
    app,
    mensaje_usuario: str,
    usuario: Dict[str, Any],
    historial: list,
    chat_session_id: str,
    model: str | None = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if not is_ollama_llm_configured():
        raise ConnectionError("OLLAMA_ENABLED is not enabled.")
    require_llm_provider_network("ollama", app)

    resolved_model = model if model and model != "gpt-4o-mini" else _ollama_chat_model()
    message, channel_instruction = _extract_message_payload(mensaje_usuario)
    system_prompt = get_system_prompt(usuario)
    if channel_instruction:
        system_prompt += f"\n\nCONTEXTO DEL CANAL: {channel_instruction}"
    if "json" not in system_prompt.lower():
        system_prompt += "\n\nIMPORTANTE: responde siempre como objeto JSON valido, sin Markdown."

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(_history_to_messages(historial or []))
    messages.append({"role": "user", "content": message})

    timeout = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "45"))
    base_url = _ollama_base_url()
    logger.info("LLM provider request started provider=ollama")

    try:
        http_client = httpx.Client(proxy=None, trust_env=False, timeout=timeout)
        client = OpenAI(api_key=_ollama_api_key(), base_url=base_url, http_client=http_client)
        response = client.chat.completions.create(
            model=resolved_model,
            messages=messages,
            temperature=float(os.getenv("OLLAMA_TEMPERATURE", "0.2")),
            response_format={"type": "json_object"},
        )

        raw_response_text = (response.choices[0].message.content or "").strip()
        logger.info("LLM provider response received provider=ollama")
        parsed_response = _parse_llm_json(raw_response_text, usuario=usuario)
        return parsed_response, {
            "model_used": resolved_model,
            "provider": "ollama",
            "base_url": base_url,
        }
    except Exception as exc:
        logger.error(
            "LLM provider request failed provider=ollama error_type=%s",
            type(exc).__name__,
        )
        raise
