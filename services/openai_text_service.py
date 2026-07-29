"""Production-safe OpenAI text compatibility for legacy extractors."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
from typing import Any

import httpx
from openai import OpenAI


logger = logging.getLogger(__name__)

DEFAULT_ROBUST_CHAT_MODEL = "gpt-5.6-sol"
_CLIENT_LOCK = threading.Lock()
_OPENAI_CLIENT: OpenAI | None = None
_OPENAI_CLIENT_KEY_DIGEST: str | None = None


def _configured_model(explicit_model: object = None) -> str:
    requested = str(explicit_model or "").strip()
    return (
        requested
        or os.getenv("OPENAI_ROBUST_CHAT_MODEL")
        or os.getenv("OPENAI_CHAT_MODEL_EXTRACTION")
        or os.getenv("OPENAI_CHAT_MODEL_DEFAULT")
        or DEFAULT_ROBUST_CHAT_MODEL
    )


def _get_openai_client() -> OpenAI:
    """Create the client lazily, after app/dotenv configuration is loaded."""

    api_key = str(os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    key_digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    try:
        timeout_seconds = max(
            1.0,
            min(float(os.getenv("OPENAI_TEXT_TIMEOUT_SECONDS", "45")), 120.0),
        )
    except (TypeError, ValueError):
        timeout_seconds = 45.0
    global _OPENAI_CLIENT, _OPENAI_CLIENT_KEY_DIGEST
    with _CLIENT_LOCK:
        if _OPENAI_CLIENT is None or _OPENAI_CLIENT_KEY_DIGEST != key_digest:
            http_client = httpx.Client(proxy=None, trust_env=False)
            _OPENAI_CLIENT = OpenAI(
                api_key=api_key,
                http_client=http_client,
                max_retries=0,
                timeout=timeout_seconds,
            )
            _OPENAI_CLIENT_KEY_DIGEST = key_digest
    return _OPENAI_CLIENT


def _privacy_safe_identifier(user_id: object) -> str | None:
    """Return a stable opaque identifier without sending tenant PII upstream."""

    raw_user_id = str(user_id or "").strip()
    secret = str(
        os.getenv("OPENAI_SAFETY_IDENTIFIER_SECRET")
        or os.getenv("AI_SAFETY_IDENTIFIER_SECRET")
        or ""
    ).strip()
    if not raw_user_id or not secret:
        return None
    return hmac.new(
        secret.encode("utf-8"),
        raw_user_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _response_output_text(response: Any) -> str:
    direct = getattr(response, "output_text", None)
    if direct:
        return str(direct).strip()

    parts: list[str] = []
    for output_item in getattr(response, "output", None) or []:
        for content_item in getattr(output_item, "content", None) or []:
            text = getattr(content_item, "text", None)
            if text:
                parts.append(str(text))
    return "".join(parts).strip()


def robust_chat(message: str, **kwargs: Any) -> str:
    """Generate plain text without ever falling back to a data-producing mock.

    Responses is used when the installed official SDK exposes it. Older SDKs
    use Chat Completions only as a compatibility path; API failures are not
    retried against a second endpoint.
    """

    prompt = str(message or "").strip()
    if not prompt:
        return ""

    client = _get_openai_client()
    model = _configured_model(kwargs.get("model_override") or kwargs.get("model"))
    max_output_tokens = int(os.getenv("OPENAI_ROBUST_CHAT_MAX_OUTPUT_TOKENS", "2048"))
    safety_identifier = _privacy_safe_identifier(kwargs.get("user_id"))

    if hasattr(client, "responses"):
        request: dict[str, Any] = {
            "model": model,
            "instructions": (
                "Sos un componente interno de Chatboc. Segui exactamente el formato "
                "pedido por el mensaje, conserva nombres y direcciones, no inventes datos "
                "y devolve solo el resultado solicitado."
            ),
            "input": prompt,
            "max_output_tokens": max_output_tokens,
            "store": False,
        }
        if safety_identifier:
            request["safety_identifier"] = safety_identifier
        response = client.responses.create(**request)
        result = _response_output_text(response)
    else:  # pragma: no cover - compatibility until every runtime uses SDK 2.x
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Sos un componente interno de Chatboc. Segui exactamente el formato "
                        "pedido, no inventes datos y devolve solo el resultado solicitado."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_output_tokens,
            temperature=0,
        )
        result = str(completion.choices[0].message.content or "").strip()

    if not result:
        raise RuntimeError("OpenAI returned an empty response")

    logger.info("OpenAI legacy text task completed model=%s", model)
    return result
