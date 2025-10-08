"""Helpers for invoking Cohere's speech-to-text API."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _build_request(
    audio_bytes: bytes,
    mime_type: str,
    *,
    model: str | None,
    language: str | None,
) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if model:
        data["model"] = model
    if language:
        data["language"] = language

    files = {
        "file": ("audio", audio_bytes, mime_type or "application/octet-stream"),
    }
    return {"data": data, "files": files}


def transcribir_audio_cohere(audio_bytes: bytes, mime_type: str) -> str | None:
    """Attempt to transcribe audio using Cohere.

    Returns ``None`` when the service is not configured or fails so callers can
    fall back to alternative providers.
    """

    api_key = os.getenv("COHERE_API_KEY")
    if not api_key:
        logger.debug("Skipping Cohere STT because COHERE_API_KEY is missing.")
        return None

    endpoint = os.getenv(
        "COHERE_STT_ENDPOINT", "https://api.cohere.ai/v1/audio/transcribe"
    )
    model = os.getenv("COHERE_STT_MODEL")
    language = os.getenv("COHERE_STT_LANGUAGE", "es")

    request_parts = _build_request(
        audio_bytes,
        mime_type,
        model=model,
        language=language,
    )

    headers = {"Authorization": f"Bearer {api_key}"}
    timeout = float(os.getenv("COHERE_STT_TIMEOUT", "60"))

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                endpoint,
                headers=headers,
                data=request_parts["data"],
                files=request_parts["files"],
            )
        response.raise_for_status()
        payload = response.json()
        text = payload.get("text") or payload.get("transcript")
        if text:
            return text.strip()
        logger.warning("Cohere STT response missing text field: %s", payload)
        return None
    except httpx.HTTPError as exc:
        logger.error("Cohere STT HTTP error: %s", exc, exc_info=True)
        return None
    except Exception as exc:  # pragma: no cover - defensive logging path
        logger.error("Unexpected Cohere STT error: %s", exc, exc_info=True)
        return None
