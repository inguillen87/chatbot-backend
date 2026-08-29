from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
from ast import literal_eval
from typing import Any, Dict, Optional

import httpx
from flask import current_app, has_app_context

from services.llm_provider_network_policy import llm_provider_network_allowed

logger = logging.getLogger(__name__)

DEFAULT_OPENAI_VISION_MODEL = "gpt-5.6-sol"
_CLIENT_LOCK = threading.Lock()
_OPENAI_CLIENT: Optional[Any] = None
_OPENAI_CLIENT_KEY_DIGEST: Optional[str] = None


def OpenAI(*args: Any, **kwargs: Any) -> Any:
    from openai import OpenAI as OpenAIClient

    return OpenAIClient(*args, **kwargs)


class OpenAIAmbiguousVisionFailure(RuntimeError):
    """The provider call may have been accepted; callers must not resubmit it."""


class OpenAIConfigurationError(RuntimeError):
    """OpenAI cannot be called because required local configuration is missing."""

TABLE_SCHEMA_ONLY = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "columns": {"type": "array", "items": {"type": "string"}},
        "rows": {
            "type": "array",
            "items": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "number"},
                        {"type": "null"},
                    ]
                },
            },
        },
    },
    "required": ["columns", "rows"],
}

VISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "labels": {"type": "array", "items": {"type": "string"}},
        "objects": {"type": "array", "items": {"type": "string"}},
        "text": {"type": "string"},
    },
    "required": ["labels", "objects", "text"],
}


def _truthy_env(*names: str) -> bool:
    truthy = {"1", "true", "yes", "on"}
    return any(str(os.getenv(name) or "").strip().lower() in truthy for name in names)


def _cohere_vision_enabled() -> bool:
    return _truthy_env("VISION_COHERE_ENABLED", "COHERE_ENABLED")


def _huggingface_vision_enabled() -> bool:
    return _truthy_env("VISION_HUGGINGFACE_ENABLED", "HUGGINGFACE_VISION_ENABLED")


def _configured_value(config_key: str, env_key: Optional[str] = None) -> Optional[str]:
    if has_app_context():
        value = current_app.config.get(config_key)
        if value not in (None, ""):
            return str(value).strip()
    value = os.getenv(env_key or config_key)
    return str(value).strip() if value not in (None, "") else None


def _openai_model(default_model: str = DEFAULT_OPENAI_VISION_MODEL) -> str:
    return (
        _configured_value("OPENAI_VISION_MODEL")
        or _configured_value("OPENAI_MODEL")
        or default_model
    )


def _get_openai_client() -> Any:
    """Build the SDK client lazily after Flask/dotenv configuration is loaded."""

    if not llm_provider_network_allowed("openai"):
        raise OpenAIConfigurationError("openai_test_network_disabled")

    api_key = _configured_value("OPENAI_API_KEY")
    if not api_key:
        raise OpenAIConfigurationError("OPENAI_API_KEY is not configured")

    key_digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    global _OPENAI_CLIENT, _OPENAI_CLIENT_KEY_DIGEST
    with _CLIENT_LOCK:
        if _OPENAI_CLIENT is None or _OPENAI_CLIENT_KEY_DIGEST != key_digest:
            http_client = httpx.Client(proxy=None, trust_env=False)
            timeout_raw = _configured_value("OPENAI_VISION_TIMEOUT_SECONDS") or "30"
            try:
                timeout_seconds = max(1.0, min(float(timeout_raw), 120.0))
            except (TypeError, ValueError):
                timeout_seconds = 30.0
            _OPENAI_CLIENT = OpenAI(
                api_key=api_key,
                http_client=http_client,
                max_retries=0,
                timeout=timeout_seconds,
            )
            _OPENAI_CLIENT_KEY_DIGEST = key_digest
    return _OPENAI_CLIENT


def _response_output_text(response: Any) -> str:
    direct = getattr(response, "output_text", None)
    if direct:
        return str(direct).strip()

    parts = []
    for output_item in getattr(response, "output", None) or []:
        content = (
            output_item.get("content")
            if isinstance(output_item, dict)
            else getattr(output_item, "content", None)
        )
        for content_item in content or []:
            content_text = (
                content_item.get("text")
                if isinstance(content_item, dict)
                else getattr(content_item, "text", None)
            )
            if content_text:
                parts.append(str(content_text))
    return "".join(parts).strip()


def _image_mime_type(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _image_data_url(image_bytes: bytes) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{_image_mime_type(image_bytes)};base64,{encoded}"


def _max_output_tokens() -> int:
    raw = _configured_value("OPENAI_VISION_MAX_OUTPUT_TOKENS") or "4096"
    try:
        return max(1, min(int(raw), 32768))
    except (TypeError, ValueError):
        return 4096


def _privacy_safe_identifier(subject: object) -> Optional[str]:
    """Return a stable HMAC identifier without sending source PII upstream."""

    raw_subject = str(subject or "").strip()
    secret = (
        _configured_value("OPENAI_SAFETY_IDENTIFIER_SECRET")
        or _configured_value("AI_SAFETY_IDENTIFIER_SECRET")
        or ""
    )
    if not raw_subject or not secret:
        return None
    return hmac.new(
        secret.encode("utf-8"),
        raw_subject.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _content_safety_subject(prefix: str, content: bytes | str) -> str:
    payload = content if isinstance(content, bytes) else content.encode("utf-8")
    return f"{prefix}:{hashlib.sha256(payload).hexdigest()}"

def _ensure_json_prompt(prompt: str) -> str:
    suffix = "\nResponde solo JSON válido sin texto adicional."
    if suffix.strip().lower() in prompt.lower():
        return prompt
    return f"{prompt}{suffix}"

def _safe_json_loads(text: str) -> Dict[str, Any]:
    def _strip_code_fences(payload: str) -> str:
        if not payload:
            return payload
        fenced = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL | re.IGNORECASE)
        match = fenced.search(payload)
        return match.group(1) if match else payload

    candidates = []
    if text:
        text = _strip_code_fences(text.strip())
        candidates.append(text)
        candidates.append(re.sub(r"[\x00-\x1f]", " ", text))
        obj_start = text.find("{")
        obj_end = text.rfind("}")
        if obj_start != -1 and obj_end != -1 and obj_end > obj_start:
            candidates.append(text[obj_start : obj_end + 1])
        arr_start = text.find("[")
        arr_end = text.rfind("]")
        if arr_start != -1 and arr_end != -1 and arr_end > arr_start:
            candidates.append(text[arr_start : arr_end + 1])

    def _attempt(payload: str) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(payload, strict=False)
        except json.JSONDecodeError:
            cleaned = re.sub(r"[\x00-\x1f]", " ", payload)
            cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
            try:
                return json.loads(cleaned, strict=False)
            except json.JSONDecodeError:
                cleaned = re.sub(r"\bnull\b", "None", cleaned, flags=re.IGNORECASE)
                cleaned = re.sub(r"\btrue\b", "True", cleaned, flags=re.IGNORECASE)
                cleaned = re.sub(r"\bfalse\b", "False", cleaned, flags=re.IGNORECASE)
                return literal_eval(cleaned)

    for candidate in candidates:
        try:
            parsed = _attempt(candidate)
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"rows": parsed}
        except Exception:
            continue

    logger.warning(
        "OpenAI structured output could not be parsed response_chars=%s",
        len(text or ""),
    )
    return {}


def _call_openai(
    image_bytes: bytes,
    custom_prompt: Optional[str] = None,
    schema: Optional[Dict[str, Any]] = None,
    *,
    schema_name: str = "vision_payload",
    safety_subject: object = None,
) -> Optional[Dict[str, Any]]:
    """Analyze an image once through Responses with strict structured output."""
    try:
        client = _get_openai_client()
    except OpenAIConfigurationError as exc:
        reason = (
            "test_network_disabled"
            if str(exc) == "openai_test_network_disabled"
            else "missing_api_key"
        )
        logger.warning("OpenAI vision skipped reason=%s", reason)
        return None

    prompt = _ensure_json_prompt(
        custom_prompt
        or (
            "Describe la imagen en español para un sistema de reclamos municipales. "
            "Devuelve un JSON con las claves: labels (lista de palabras clave en español), "
            "objects (lista de objetos principales en español) y text (cadena con cualquier texto encontrado en español)."
        )
    )
    model = _openai_model()
    request: Dict[str, Any] = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {
                        "type": "input_image",
                        "image_url": _image_data_url(image_bytes),
                        "detail": "auto",
                    },
                ],
            }
        ],
        "max_output_tokens": _max_output_tokens(),
        "store": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema or VISION_SCHEMA,
                "strict": True,
            }
        },
    }
    safety_identifier = _privacy_safe_identifier(
        safety_subject or _content_safety_subject("image", image_bytes)
    )
    if safety_identifier:
        request["safety_identifier"] = safety_identifier

    try:
        response = client.responses.create(**request)
    except Exception as exc:
        logger.warning(
            "OpenAI vision request failed model=%s error_type=%s fallback_suppressed=true",
            model,
            type(exc).__name__,
        )
        raise OpenAIAmbiguousVisionFailure("OpenAI vision request outcome is ambiguous") from exc

    response_text = _response_output_text(response)
    if not response_text:
        logger.warning("OpenAI vision returned empty output model=%s", model)
        return None
    result = _safe_json_loads(response_text)
    logger.info(
        "OpenAI vision completed model=%s response_chars=%s parsed=%s",
        model,
        len(response_text),
        bool(result),
    )
    return result or None


def _call_openai_image_text(
    image_bytes: bytes,
    custom_prompt: Optional[str] = None,
    *,
    safety_subject: object = None,
) -> Optional[str]:
    """Extract raw image text once through Responses."""
    try:
        client = _get_openai_client()
    except OpenAIConfigurationError as exc:
        reason = (
            "test_network_disabled"
            if str(exc) == "openai_test_network_disabled"
            else "missing_api_key"
        )
        logger.warning("OpenAI OCR skipped reason=%s", reason)
        return None

    prompt = custom_prompt or (
        "Extrae TODO el texto visible de la imagen respetando saltos de línea. "
        "No agregues explicaciones."
    )
    model = _openai_model()
    request: Dict[str, Any] = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {
                        "type": "input_image",
                        "image_url": _image_data_url(image_bytes),
                        "detail": "auto",
                    },
                ],
            }
        ],
        "max_output_tokens": _max_output_tokens(),
        "store": False,
    }
    safety_identifier = _privacy_safe_identifier(
        safety_subject or _content_safety_subject("image", image_bytes)
    )
    if safety_identifier:
        request["safety_identifier"] = safety_identifier
    try:
        response = client.responses.create(**request)
    except Exception as exc:
        logger.warning(
            "OpenAI OCR request failed model=%s error_type=%s fallback_suppressed=true",
            model,
            type(exc).__name__,
        )
        raise OpenAIAmbiguousVisionFailure("OpenAI OCR request outcome is ambiguous") from exc
    return _response_output_text(response) or None


def _call_openai_text(
    text: str,
    custom_prompt: Optional[str] = None,
    schema: Optional[Dict[str, Any]] = None,
    *,
    safety_subject: object = None,
) -> Optional[Dict[str, Any]]:
    """Analyze text once through Responses and return structured JSON."""
    try:
        client = _get_openai_client()
    except OpenAIConfigurationError as exc:
        reason = (
            "test_network_disabled"
            if str(exc) == "openai_test_network_disabled"
            else "missing_api_key"
        )
        logger.warning("OpenAI text analysis skipped reason=%s", reason)
        return None

    prompt = _ensure_json_prompt(
        custom_prompt
        or (
            "Extrae la tabla del catálogo en JSON con claves "
            "'columns' (lista de strings) y 'rows' (lista de listas ordenadas según columns). "
            "No inventes datos, deja vacío si no se ve."
        )
    )
    model = _openai_model()
    request: Dict[str, Any] = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": f"{prompt}\n\n{str(text)}"}
                ],
            }
        ],
        "max_output_tokens": _max_output_tokens(),
        "store": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "catalog_table",
                "schema": schema or TABLE_SCHEMA_ONLY,
                "strict": True,
            }
        },
    }
    safety_identifier = _privacy_safe_identifier(
        safety_subject or _content_safety_subject("text", text)
    )
    if safety_identifier:
        request["safety_identifier"] = safety_identifier
    try:
        response = client.responses.create(**request)
    except Exception as exc:
        logger.warning(
            "OpenAI text analysis request failed model=%s error_type=%s fallback_suppressed=true",
            model,
            type(exc).__name__,
        )
        raise OpenAIAmbiguousVisionFailure("OpenAI text request outcome is ambiguous") from exc

    response_text = _response_output_text(response)
    if not response_text:
        logger.warning("OpenAI text analysis returned empty output model=%s", model)
        return None
    return _safe_json_loads(response_text) or None

def _call_cohere(image_bytes: bytes, custom_prompt: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Analyze an image using Cohere's multimodal API."""
    if not _cohere_vision_enabled():
        logger.info("Cohere vision fallback is disabled.")
        return None
    if not llm_provider_network_allowed("cohere"):
        logger.info("Cohere vision fallback blocked reason=test_network_disabled")
        return None

    api_key = os.getenv("COHERE_API_KEY")
    if not api_key:
        return None
    try:
        import cohere

        b64 = base64.b64encode(image_bytes).decode("utf-8")
        co = cohere.Client(api_key)
        prompt = custom_prompt or (
            "Describe the image for a municipal complaint system. "
            "Return JSON with keys: labels, objects, text."
        )
        try:
            resp = co.chat(
                model="command-r-plus",
                message=prompt,
                images=[{"data": b64, "mime_type": "image/jpeg"}],
            )
            text = resp.text
        except TypeError:
            # Older SDKs may not support the ``images`` parameter; fall back to generate()
            resp = co.generate(
                model="command-r-plus",
                prompt=prompt,
                image_url=f"data:image/jpeg;base64,{b64}",
            )
            text = resp.generations[0].text
        return json.loads(text)
    except Exception as exc:
        logger.warning("Cohere vision failed error_type=%s", type(exc).__name__)
        return None


def _call_huggingface(image_bytes: bytes) -> Optional[Dict[str, Any]]:
    if not _huggingface_vision_enabled():
        return None
    try:
        from services.huggingface_inference_service import analyze_image_for_chatboc

        return analyze_image_for_chatboc(image_bytes)
    except Exception as exc:
        logger.warning("Hugging Face vision failed error_type=%s", type(exc).__name__)
        return None


def _normalize_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Convert provider-agnostic result into the format used in the app."""
    labels = ["" if l is None else l for l in result.get("labels", [])]
    objects = ["" if o is None else o for o in result.get("objects", [])]
    normalized: Dict[str, Any] = {
        "labels": [{"description": lbl} for lbl in labels if lbl],
        "objects": [{"name": obj} for obj in objects if obj],
    }
    text = result.get("text") or ""
    if text:
        normalized["full_text_annotation"] = {"description": text}
    return normalized


def analyze_image_smart(image_bytes: bytes, prompt: Optional[str] = None) -> Dict[str, Any]:
    """Analyze image bytes using OpenAI, with optional Cohere fallback."""
    try:
        result = _call_openai(image_bytes, custom_prompt=prompt, schema=VISION_SCHEMA)
    except OpenAIAmbiguousVisionFailure:
        logger.warning("Vision provider fallback suppressed reason=ambiguous_openai_outcome")
        return {"labels": [], "objects": []}
    if result:
        return _normalize_result(result)
    if _cohere_vision_enabled():
        logger.warning("Falling back to explicitly enabled Cohere vision...")
        result = _call_cohere(image_bytes, custom_prompt=prompt)
        if result:
            return _normalize_result(result)
    else:
        logger.warning("OpenAI vision failed and Cohere vision fallback is disabled.")
    if _huggingface_vision_enabled():
        logger.warning("Falling back to explicitly enabled Hugging Face vision...")
        result = _call_huggingface(image_bytes)
        if result:
            return _normalize_result(result)
    logger.error("All vision providers failed")
    return {"labels": [], "objects": []}


def analyze_image_structured(image_bytes: bytes, prompt: str) -> Optional[Dict[str, Any]]:
    """Analyze image bytes and return provider JSON without normalization."""
    try:
        result = _call_openai(image_bytes, custom_prompt=prompt, schema=TABLE_SCHEMA_ONLY)
    except OpenAIAmbiguousVisionFailure:
        logger.warning("Structured vision fallback suppressed reason=ambiguous_openai_outcome")
        return None
    if result:
        return result
    logger.warning("Structured vision failed for OpenAI; skipping Cohere structured fallback.")
    logger.error("All structured vision providers failed")
    return None


def analyze_image_text(image_bytes: bytes, prompt: Optional[str] = None) -> Optional[str]:
    """Extract raw text from image bytes."""
    try:
        return _call_openai_image_text(image_bytes, custom_prompt=prompt)
    except OpenAIAmbiguousVisionFailure:
        logger.warning("OCR fallback suppressed reason=ambiguous_openai_outcome")
        return None


def analyze_text_structured(text: str, prompt: str) -> Optional[Dict[str, Any]]:
    """Analyze text and return provider JSON without normalization."""
    try:
        result = _call_openai_text(text, custom_prompt=prompt, schema=TABLE_SCHEMA_ONLY)
    except OpenAIAmbiguousVisionFailure:
        logger.warning("Structured text fallback suppressed reason=ambiguous_openai_outcome")
        return None
    if result:
        return result
    logger.error("Structured text analysis failed")
    return None
