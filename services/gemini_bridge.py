import json
import logging
import os
from typing import Any, Dict, Tuple

from services.chatbot_prompts import get_system_prompt

logger = logging.getLogger(__name__)


def _env_first(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return default


def _gemini_api_key() -> str | None:
    return _env_first("GEMINI_API_KEY", "GOOGLE_GENAI_API_KEY")


def is_gemini_llm_configured() -> bool:
    return bool(_gemini_api_key())


def _gemini_chat_model() -> str:
    return _env_first(
        "GEMINI_CHAT_MODEL",
        "GEMINI_MODEL",
        default="gemini-2.5-flash",
    ) or "gemini-2.5-flash"


def _get_genai_modules():
    try:
        from google import genai
        from google.genai import types
    except Exception as exc:  # pragma: no cover - depends on deploy env
        raise ConnectionError(
            "Google GenAI SDK is not available. Check requirements installation."
        ) from exc
    return genai, types


def _extract_message_payload(mensaje_usuario: Any) -> tuple[str, str | None]:
    try:
        payload = json.loads(mensaje_usuario)
    except (json.JSONDecodeError, TypeError):
        return str(mensaje_usuario or ""), None

    if not isinstance(payload, dict):
        return str(payload), None

    text = payload.get("texto") or payload.get("message") or payload.get("body")
    return str(text or payload), payload.get("instruccion_canal")


def _history_to_gemini_contents(historial: list, types) -> list:
    contents = []
    for item in historial or []:
        role = item.get("role")
        if role == "assistant":
            role = "model"
        if role not in {"user", "model"}:
            continue

        text = item.get("parts", [{}])[0].get("text", "")
        if not text:
            continue

        contents.append(types.Content(role=role, parts=[types.Part(text=text)]))
    return contents


def _extract_response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()

    candidates = getattr(response, "candidates", None)
    if not candidates:
        return ""

    parts_text: list[str] = []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", []) or []:
            part_text = getattr(part, "text", None)
            if part_text:
                parts_text.append(str(part_text))
    return "".join(parts_text).strip()


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
        raise ValueError("Gemini response is not a JSON object.")

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


def llamar_gemini(
    app,
    mensaje_usuario: str,
    usuario: Dict[str, Any],
    historial: list,
    chat_session_id: str,
    model: str | None = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    api_key = _gemini_api_key()
    if not api_key:
        raise ConnectionError("GEMINI_API_KEY is not configured.")

    genai, types = _get_genai_modules()
    resolved_model = model or _gemini_chat_model()

    message, channel_instruction = _extract_message_payload(mensaje_usuario)
    system_prompt = get_system_prompt(usuario)
    if channel_instruction:
        system_prompt += f"\n\nCONTEXTO DEL CANAL: {channel_instruction}"

    contents = _history_to_gemini_contents(historial or [], types)
    contents.append(types.Content(role="user", parts=[types.Part(text=message)]))

    logger.info("Sending to Gemini. Message: %s... Model: %s", message[:100], resolved_model)

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=resolved_model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.3,
                response_mime_type="application/json",
            ),
        )
        raw_response_text = _extract_response_text(response)
        logger.info("Response from Gemini (raw): %s", raw_response_text)

        parsed_response = _parse_llm_json(raw_response_text, usuario=usuario)
        return parsed_response, {"model_used": resolved_model, "provider": "gemini"}

    except Exception as exc:
        logger.error("Error calling Gemini API: %s", exc, exc_info=True)
        raise
