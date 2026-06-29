"""Utility functions for transcribing audio clips."""

import hashlib
import io
import os
import time

import re
import requests
import httpx
from openai import OpenAI

from collections import OrderedDict

# Initialize a shared OpenAI client once at import time so it can be mocked in tests.
# If no API key is configured, fall back to a dummy key so unit tests can run without
# external credentials. Use a custom HTTP client that ignores proxy env vars (common
# in CI) to avoid initialization errors.
http_client = httpx.Client(proxy=None, trust_env=False)
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", "test"), http_client=http_client)
DEFAULT_STT_MODEL = "gpt-4o-mini-transcribe"
DEFAULT_AUDIO_DOWNLOAD_TIMEOUT_SECONDS = 12
AUTO_LANGUAGE_MARKERS = {"", "auto", "detect", "none", "null"}
SUPPORTED_TRANSLATION_LANGUAGES = ("es", "en", "pt")
_TRUTHY_VALUES = {"1", "true", "yes", "on"}
_FALSEY_VALUES = {"0", "false", "no", "off"}
_STT_CACHE: OrderedDict[str, tuple[float, str]] = OrderedDict()


def clear_transcription_cache() -> None:
    """Clear the in-memory STT cache. Primarily used by tests."""

    _STT_CACHE.clear()


def _truthy_env(*names: str) -> bool:
    return any(str(os.getenv(name) or "").strip().lower() in _TRUTHY_VALUES for name in names)


def _cohere_stt_enabled() -> bool:
    return _truthy_env("STT_COHERE_ENABLED", "COHERE_ENABLED")


def _stt_cache_enabled() -> bool:
    return str(os.getenv("STT_CACHE_ENABLED", "true")).strip().lower() not in _FALSEY_VALUES


def _stt_cache_ttl_seconds() -> int:
    try:
        return max(0, int(os.getenv("STT_CACHE_TTL_SECONDS", "86400")))
    except (TypeError, ValueError):
        return 86400


def _stt_cache_max_items() -> int:
    try:
        return max(1, int(os.getenv("STT_CACHE_MAX_ITEMS", "256")))
    except (TypeError, ValueError):
        return 256


def _stt_config_fingerprint() -> str:
    return "|".join(
        [
            ",".join(_stt_provider_order()),
            os.getenv("OPENAI_STT_MODEL", DEFAULT_STT_MODEL),
            str(resolve_transcription_language() or "auto"),
            os.getenv("TRANSLATION_TARGET_LANGUAGE", "es"),
        ]
    )


def _stt_cache_get(key: str) -> str | None:
    if not _stt_cache_enabled():
        return None

    cached = _STT_CACHE.get(key)
    if not cached:
        return None

    created_at, value = cached
    ttl = _stt_cache_ttl_seconds()
    if ttl and (time.time() - created_at) > ttl:
        _STT_CACHE.pop(key, None)
        return None

    _STT_CACHE.move_to_end(key)
    return value


def _stt_cache_set(key: str, value: str) -> None:
    if not _stt_cache_enabled() or not value:
        return

    _STT_CACHE[key] = (time.time(), value)
    _STT_CACHE.move_to_end(key)

    max_items = _stt_cache_max_items()
    while len(_STT_CACHE) > max_items:
        _STT_CACHE.popitem(last=False)


def _stt_url_cache_key(url: str, mime_type: str) -> str:
    raw = f"url|{url}|{mime_type}|{_stt_config_fingerprint()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _stt_content_cache_key(audio_bytes: bytes, mime_type: str) -> str:
    audio_hash = hashlib.sha256(audio_bytes).hexdigest()
    raw = f"bytes|{audio_hash}|{mime_type}|{_stt_config_fingerprint()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _audio_download_timeout_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("STT_AUDIO_DOWNLOAD_TIMEOUT_SECONDS", str(DEFAULT_AUDIO_DOWNLOAD_TIMEOUT_SECONDS))))
    except (TypeError, ValueError):
        return float(DEFAULT_AUDIO_DOWNLOAD_TIMEOUT_SECONDS)


def _safe_audio_filename(mime_type: str) -> str:
    extension = mime_type.split("/")[-1] if "/" in mime_type else "audio"
    safe_extension = re.sub(r"[^a-zA-Z0-9]", "", extension) or "audio"
    return f"audio.{safe_extension}"


def normalize_spanish_transcription(text: str) -> str:
    """Expand common abbreviations and regionalisms for clearer understanding.

    This lightly normalizes transcriptions so downstream prompts can rely on
    full words.  It currently focuses on a few frequent shortcuts used in
    Spanish chats and voice notes.
    """

    replacements = {
        "xq": "porque",
        "pq": "porque",
        "dnd": "donde",
        "sr": "señor",
        "sra": "señora",
        "uds": "ustedes",
    }
    for short, full in replacements.items():
        text = re.sub(rf"\b{short}\b", full, text, flags=re.IGNORECASE)
    return text


def _stt_provider_order() -> list[str]:
    raw = os.getenv("STT_PROVIDER_ORDER", "openai")
    normalized = [entry.strip().lower() for entry in raw.split(",") if entry.strip()]
    if not normalized:
        normalized = ["openai"]

    ordered: list[str] = []
    for provider in OrderedDict.fromkeys(normalized):
        if provider == "cohere" and not _cohere_stt_enabled():
            continue
        ordered.append(provider)

    return ordered or ["openai"]


def resolve_transcription_language() -> str | None:
    """Return the configured STT language or None for automatic detection."""

    language = os.getenv("OPENAI_STT_LANGUAGE", "auto").strip().lower()
    if language in AUTO_LANGUAGE_MARKERS:
        return None
    return language


def audio_translation_capabilities() -> dict:
    """Public contract fragment for audio-note translation support."""

    return {
        "enabled": True,
        "mode": "transcribe_detect_translate_for_reasoning",
        "transcription_model": os.getenv("OPENAI_STT_MODEL", DEFAULT_STT_MODEL),
        "language_detection": resolve_transcription_language() is None,
        "supported_languages": list(SUPPORTED_TRANSLATION_LANGUAGES),
        "target_language": os.getenv("TRANSLATION_TARGET_LANGUAGE", "es"),
        "preserve_original_transcript": True,
        "providers": _stt_provider_order(),
        "cache": {
            "enabled": _stt_cache_enabled(),
            "ttl_seconds": _stt_cache_ttl_seconds(),
            "max_items": _stt_cache_max_items(),
        },
    }


def _transcribe_with_openai(audio_bytes: bytes, filename: str) -> str | None:
    language = resolve_transcription_language()
    model = os.getenv("OPENAI_STT_MODEL", DEFAULT_STT_MODEL)

    with io.BytesIO(audio_bytes) as audio_file:
        audio_file.name = filename
        payload = {
            "model": model,
            "file": audio_file,
        }
        if language:
            payload["language"] = language
        transcription = openai_client.audio.transcriptions.create(**payload)

    return getattr(transcription, "text", None)


def transcribe_audio_bytes(
    audio_bytes: bytes,
    mime_type: str,
    *,
    cache_url: str | None = None,
) -> str | None:
    """Transcribe an already-downloaded audio payload.

    WhatsApp/Twilio webhooks already download media to persist the attachment.
    This helper lets that path reuse the same bytes instead of downloading the
    audio a second time only for STT.
    """

    if not audio_bytes:
        return None

    content_cache_key = _stt_content_cache_key(audio_bytes, mime_type)
    cached_text = _stt_cache_get(content_cache_key)
    if cached_text:
        if cache_url:
            _stt_cache_set(_stt_url_cache_key(cache_url, mime_type), cached_text)
        return cached_text

    url_cache_key = _stt_url_cache_key(cache_url, mime_type) if cache_url else None
    if url_cache_key:
        cached_text = _stt_cache_get(url_cache_key)
        if cached_text:
            _stt_cache_set(content_cache_key, cached_text)
            return cached_text

    filename = _safe_audio_filename(mime_type)
    for provider in _stt_provider_order():
        if provider == "openai":
            try:
                text = _transcribe_with_openai(audio_bytes, filename)
            except Exception as exc:  # pragma: no cover - defensive logging
                print(f"OpenAI STT error: {exc}")
                text = None
        elif provider == "cohere" and _cohere_stt_enabled():
            try:
                from services.cohere_stt_bridge import transcribir_audio_cohere

                text = transcribir_audio_cohere(audio_bytes, mime_type)
            except Exception as exc:  # pragma: no cover - defensive logging
                print(f"Cohere STT error: {exc}")
                text = None
        else:
            text = None

        if text:
            language = resolve_transcription_language()
            if language == "es":
                text = normalize_spanish_transcription(text)
            _stt_cache_set(content_cache_key, text)
            if url_cache_key:
                _stt_cache_set(url_cache_key, text)
            return text

    return None

def transcribe_audio_from_url(url: str, mime_type: str, account_sid: str = None, auth_token: str = None) -> str | None:
    """Download an audio file and transcribe it using OpenAI Whisper.

    Parameters
    ----------
    url:
        Direct URL to the audio file.
    mime_type:
        The MIME type of the audio file (e.g., 'audio/webm', 'audio/ogg').
    account_sid:
        Optional Twilio Account SID for authentication.
    auth_token:
        Optional Twilio Auth Token for authentication.

    Returns
    -------
    str | None
        The transcribed text if successful, otherwise ``None``.
    """

    try:
        url_cache_key = _stt_url_cache_key(url, mime_type)
        cached_text = _stt_cache_get(url_cache_key)
        if cached_text:
            return cached_text

        auth = (account_sid, auth_token) if account_sid and auth_token else None
        audio_response = requests.get(url, auth=auth, timeout=_audio_download_timeout_seconds())
        audio_response.raise_for_status()

        return transcribe_audio_bytes(audio_response.content, mime_type, cache_url=url)

    except requests.exceptions.RequestException as e:
        print(f"Error downloading audio file: {e}")
        return None
    except Exception as e:
        print(f"Error during audio transcription: {e}")
        return None
