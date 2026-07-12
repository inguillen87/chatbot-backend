"""Google TTS compatibility adapter backed by the canonical audio cache.

New response flows should call :func:`generate_audio_url`, which delegates to
``tts_orchestrator``. ``TextToSpeechService`` remains available for legacy
callers and explicit Google-provider diagnostics.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from pathlib import Path
from threading import Lock
from typing import Any

from google.cloud import texttospeech

logger = logging.getLogger(__name__)

_GOOGLE_TTS_LOCKS = tuple(Lock() for _ in range(32))


def _cache_lock(cache_key: str) -> Lock:
    return _GOOGLE_TTS_LOCKS[int(cache_key[:8], 16) % len(_GOOGLE_TTS_LOCKS)]


def _safe_namespace(rubro_obj: Any = None) -> str:
    raw = str(
        getattr(rubro_obj, "clave", None)
        or getattr(rubro_obj, "slug", None)
        or "generic"
    ).strip().lower()
    token = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-") or "generic"
    return f"response:{token}:v1"


class TextToSpeechService:
    """Small, failure-tolerant Google Cloud TTS client with disk caching."""

    def __init__(self, client: Any = None, output_dir: str = "static/audio_responses"):
        self.output_dir = Path(output_dir)
        self.language_code = os.getenv("GOOGLE_TTS_LANGUAGE_CODE", "es-US")
        self.voice_name = os.getenv("GOOGLE_TTS_VOICE_NAME", "es-US-Standard-A")
        if client is not None:
            self.client = client
            return

        try:
            self.client = texttospeech.TextToSpeechClient()
        except Exception as exc:  # pragma: no cover - depends on deployment credentials
            logger.warning("Google TTS client is unavailable: %s", exc)
            self.client = None

    def synthesize_speech(self, text: str) -> str | None:
        normalized_text = str(text or "").strip()
        if not normalized_text or self.client is None:
            return None

        fingerprint = "|".join(
            [self.language_code, self.voice_name, normalized_text]
        )
        cache_key = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        output_path = self.output_dir / f"{cache_key}.mp3"
        public_url = "/" + output_path.as_posix().lstrip("/")

        if output_path.exists():
            return public_url

        with _cache_lock(cache_key):
            if output_path.exists():
                return public_url

            try:
                response = self.client.synthesize_speech(
                    input=texttospeech.SynthesisInput(text=normalized_text),
                    voice=texttospeech.VoiceSelectionParams(
                        language_code=self.language_code,
                        name=self.voice_name,
                        ssml_gender=texttospeech.SsmlVoiceGender.FEMALE,
                    ),
                    audio_config=texttospeech.AudioConfig(
                        audio_encoding=texttospeech.AudioEncoding.MP3
                    ),
                )
                audio_content = bytes(response.audio_content or b"")
                if not audio_content:
                    return None

                self.output_dir.mkdir(parents=True, exist_ok=True)
                temp_path: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        dir=self.output_dir,
                        prefix=f".{cache_key}.",
                        suffix=".tmp",
                        delete=False,
                    ) as temp_file:
                        temp_path = Path(temp_file.name)
                        temp_file.write(audio_content)
                        temp_file.flush()
                        os.fsync(temp_file.fileno())
                    os.replace(temp_path, output_path)
                finally:
                    if temp_path and temp_path.exists():
                        temp_path.unlink(missing_ok=True)

                return public_url
            except Exception as exc:
                logger.error("Google TTS synthesis failed: %s", exc, exc_info=True)
                return None


def generate_audio_url(
    text: str,
    rubro_obj: Any = None,
    current_user: Any = None,
    *,
    voice: str | None = None,
    model: str | None = None,
    style: str | None = None,
    speed: float | None = None,
    cache_namespace: str | None = None,
) -> str | None:
    """Generate an audio URL through the configured provider and shared cache.

    ``current_user`` is intentionally not included in the cache key or logs. It
    remains in the signature only for compatibility with older callers.
    """

    del current_user
    from services.tts_orchestrator import generar_audio

    return generar_audio(
        text,
        voice=voice,
        model=model,
        style=style,
        speed=speed,
        cache_namespace=cache_namespace or _safe_namespace(rubro_obj),
    )

