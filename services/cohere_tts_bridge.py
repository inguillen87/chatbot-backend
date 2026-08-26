"""Utilities for Cohere based text-to-speech synthesis."""

from __future__ import annotations

import base64
import logging
import os
import uuid
from typing import Any

from utils.lazy_module import LazyModule


httpx = LazyModule("httpx")

logger = logging.getLogger(__name__)


def _build_request_payload(
    text: str,
    *,
    voice: str | None,
    style: str | None,
    model: str | None,
    speed: float,
    language: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "text": text,
        "voice": voice or "argentina-female",
        "format": "mp3",
        "speed": speed,
        "language": language,
    }

    if style:
        payload["style"] = style
    if model:
        payload["model"] = model

    return payload


def _extract_audio_bytes(data: dict[str, Any]) -> bytes:
    """Return decoded audio bytes from the Cohere JSON payload."""

    if not isinstance(data, dict):
        return b""

    candidates = [
        data.get("audio_base64"),
        data.get("audio"),
        data.get("mp3_base64"),
    ]

    generations = data.get("generations")
    if isinstance(generations, list) and generations:
        generation = generations[0] or {}
        if isinstance(generation, dict):
            candidates.append(generation.get("audio_base64"))
            audio_entry = generation.get("audio")
            if isinstance(audio_entry, dict):
                candidates.append(audio_entry.get("mp3_base64"))
                candidates.append(audio_entry.get("base64"))

    audio_field = data.get("audio")
    if isinstance(audio_field, dict):
        candidates.append(audio_field.get("base64"))
        candidates.append(audio_field.get("mp3_base64"))

    for candidate in candidates:
        if not candidate:
            continue

        if isinstance(candidate, dict):
            base64_payload = candidate.get("base64") or candidate.get("mp3_base64")
        else:
            base64_payload = candidate

        if not base64_payload:
            continue

        try:
            return base64.b64decode(base64_payload)
        except Exception:
            continue

    return b""


def generar_audio_cohere(
    text: str,
    *,
    voice: str | None = None,
    style: str | None = None,
    model: str | None = None,
    speed: float = 0.85,
    language: str | None = None,
) -> str | None:
    """Generate audio using Cohere's experimental speech endpoint.

    The function is defensive: if Cohere credentials are not configured or if
    the API responds with an error we simply return ``None`` so the orchestrator
    can fall back to alternative providers.
    """

    api_key = os.getenv("COHERE_API_KEY")
    if not api_key:
        logger.debug("Cohere TTS skipped because COHERE_API_KEY is not set.")
        return None

    endpoint = os.getenv(
        "COHERE_TTS_ENDPOINT", "https://api.cohere.ai/v1/audio/generate"
    )
    language = language or os.getenv("COHERE_TTS_LANGUAGE", "es-AR")

    payload = _build_request_payload(
        text,
        voice=voice,
        style=style,
        model=model,
        speed=speed,
        language=language,
    )

    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        timeout = float(os.getenv("COHERE_TTS_TIMEOUT", "60"))
        with httpx.Client(timeout=timeout) as client:
            response = client.post(endpoint, json=payload, headers=headers)
        response.raise_for_status()

        # Cohere responses may either stream raw audio or return base64 encoded
        # content. Support both to make local testing easier.
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            data = response.json()
            audio_bytes = _extract_audio_bytes(data)
        else:
            audio_bytes = response.content

        if not audio_bytes:
            logger.warning("Cohere TTS did not return audio bytes.")
            return None

        output_dir = "static/audio_responses"
        os.makedirs(output_dir, exist_ok=True)
        filename = f"cohere-{uuid.uuid4()}.mp3"
        output_path = os.path.join(output_dir, filename)
        with open(output_path, "wb") as handler:
            handler.write(audio_bytes)

        logger.info("Cohere TTS generated audio at %s", output_path)
        return f"/{output_dir}/{filename}"

    except httpx.HTTPError as exc:
        logger.error("Cohere TTS HTTP error: %s", exc, exc_info=True)
        return None
    except Exception as exc:  # pragma: no cover - defensive logging path
        logger.error("Unexpected Cohere TTS error: %s", exc, exc_info=True)
        return None
