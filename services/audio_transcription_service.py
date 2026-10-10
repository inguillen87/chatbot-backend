"""Utility functions for transcribing audio clips."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import time
from threading import Lock

import re
import requests
from typing import Any

from collections import OrderedDict

from services.bounded_media import MediaDownloadTooLarge, read_bounded_response_body
from services.llm_provider_network_policy import llm_provider_network_allowed
from services.outbox_execution_budget import outbox_io_timeout_seconds
from utils.lazy_module import LazyModule


httpx = LazyModule("httpx")

logger = logging.getLogger(__name__)

# Keep these module-level names for backwards-compatible test injection, but build
# the real client only when STT is first used. This is important because app.py may
# load dotenv after this module has been imported by another entrypoint.
http_client: httpx.Client | None = None
openai_client: Any | None = None
_OPENAI_CLIENT_LOCK = Lock()


def OpenAI(*args: Any, **kwargs: Any) -> Any:
    """Compatibility constructor that defers importing the provider SDK."""

    from openai import OpenAI as OpenAIClient

    return OpenAIClient(*args, **kwargs)

# Current OpenAI guidance recommends gpt-4o-transcribe for completed recordings.
# Keep OPENAI_STT_MODEL as an explicit rollout/rollback boundary per deployment.
DEFAULT_STT_MODEL = "gpt-4o-transcribe"
DEFAULT_AUDIO_DOWNLOAD_TIMEOUT_SECONDS = 12
# OpenAI's file-transcription API documents a 25 MB upload ceiling. Deployments may
# choose a lower operational limit, but never raise it above the provider contract.
OPENAI_TRANSCRIPTION_UPLOAD_LIMIT_BYTES = 25 * 1024 * 1024
DEFAULT_STT_MAX_AUDIO_BYTES = OPENAI_TRANSCRIPTION_UPLOAD_LIMIT_BYTES
AUTO_LANGUAGE_MARKERS = {"", "auto", "detect", "none", "null"}
SUPPORTED_TRANSLATION_LANGUAGES = ("es", "en", "pt")
_TRUTHY_VALUES = {"1", "true", "yes", "on"}
_FALSEY_VALUES = {"0", "false", "no", "off"}
_STT_CACHE: OrderedDict[str, tuple[float, str]] = OrderedDict()

_MIME_EXTENSION_MAP = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/m4a": "m4a",
    "audio/mpga": "mpga",
    "audio/wav": "wav",
    "audio/wave": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
    "video/webm": "webm",
    # WhatsApp voice notes use Ogg/Opus. The API already accepts these in the
    # deployed flow; retaining the real container extension is safer than naming
    # Ogg bytes as WebM merely to match a documentation allow-list.
    "audio/ogg": "ogg",
    "application/ogg": "ogg",
    "audio/opus": "ogg",
}


def clear_transcription_cache() -> None:
    """Clear the in-memory STT cache. Primarily used by tests."""

    _STT_CACHE.clear()


def _runtime_setting(name: str, default=None):
    """Resolve a setting from Flask config first, then the process environment.

    Importing Flask lazily keeps this service usable from scripts and workers that
    do not have an application context.
    """

    try:
        from flask import current_app, has_app_context

        if has_app_context() and name in current_app.config:
            return current_app.config.get(name)
    except (ImportError, RuntimeError):
        pass

    value = os.getenv(name)
    return default if value is None else value


def _openai_api_key() -> str | None:
    value = _runtime_setting("OPENAI_API_KEY")
    normalized = str(value or "").strip()
    return normalized or None


def _get_openai_client() -> Any | None:
    """Return the shared OpenAI client, creating it only on first real use."""

    global http_client, openai_client

    if not llm_provider_network_allowed("openai"):
        logger.info("OpenAI STT unavailable reason=test_network_disabled")
        return None

    # This fast path also preserves the existing test seam where callers patch
    # ``openai_client`` with a mock.
    if openai_client is not None:
        return openai_client

    api_key = _openai_api_key()
    if not api_key:
        logger.warning("OpenAI STT unavailable reason=missing_api_key")
        return None

    with _OPENAI_CLIENT_LOCK:
        if openai_client is not None:
            return openai_client

        http_client = httpx.Client(proxy=None, trust_env=False)
        openai_client = OpenAI(api_key=api_key, http_client=http_client)
        return openai_client


def _canonical_mime_type(mime_type: str | None) -> str:
    """Strip MIME parameters and normalize case for cache/file handling."""

    return str(mime_type or "").split(";", 1)[0].strip().lower()


def _stt_max_audio_bytes() -> int:
    configured = _runtime_setting(
        "STT_MAX_AUDIO_BYTES",
        _runtime_setting("OPENAI_STT_MAX_AUDIO_BYTES", DEFAULT_STT_MAX_AUDIO_BYTES),
    )
    try:
        parsed = int(configured)
    except (TypeError, ValueError):
        parsed = DEFAULT_STT_MAX_AUDIO_BYTES

    return min(
        OPENAI_TRANSCRIPTION_UPLOAD_LIMIT_BYTES,
        max(1, parsed),
    )


def _safe_error_status(exc: Exception) -> str:
    """Extract only a non-sensitive provider/HTTP status for operational logs."""

    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)

    if isinstance(status, int):
        return str(status)
    if isinstance(status, str) and status.isdigit():
        return status
    return "unknown"


def _log_transcription_failure(provider: str, exc: Exception) -> None:
    logger.warning(
        "Audio transcription provider failed provider=%s error_type=%s status_code=%s",
        provider,
        type(exc).__name__,
        _safe_error_status(exc),
    )


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
            str(_runtime_setting("OPENAI_STT_MODEL", DEFAULT_STT_MODEL)),
            str(resolve_transcription_language() or "auto"),
            str(_runtime_setting("TRANSLATION_TARGET_LANGUAGE", "es")),
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
        return max(
            1.0,
            float(
                _runtime_setting(
                    "STT_AUDIO_DOWNLOAD_TIMEOUT_SECONDS",
                    str(DEFAULT_AUDIO_DOWNLOAD_TIMEOUT_SECONDS),
                )
            ),
        )
    except (TypeError, ValueError):
        return float(DEFAULT_AUDIO_DOWNLOAD_TIMEOUT_SECONDS)


def _safe_audio_filename(mime_type: str | None) -> str:
    canonical_mime = _canonical_mime_type(mime_type)
    mapped_extension = _MIME_EXTENSION_MAP.get(canonical_mime)
    if mapped_extension:
        return f"audio.{mapped_extension}"

    extension = canonical_mime.split("/")[-1] if "/" in canonical_mime else "audio"
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

    language = str(_runtime_setting("OPENAI_STT_LANGUAGE", "auto") or "auto").strip().lower()
    if language in AUTO_LANGUAGE_MARKERS:
        return None
    return language


def audio_translation_capabilities() -> dict:
    """Public contract fragment for audio-note translation support."""

    return {
        "enabled": True,
        "mode": "transcribe_detect_translate_for_reasoning",
        "transcription_model": str(_runtime_setting("OPENAI_STT_MODEL", DEFAULT_STT_MODEL)),
        "language_detection": resolve_transcription_language() is None,
        "supported_languages": list(SUPPORTED_TRANSLATION_LANGUAGES),
        "target_language": str(_runtime_setting("TRANSLATION_TARGET_LANGUAGE", "es")),
        "preserve_original_transcript": True,
        "max_audio_bytes": _stt_max_audio_bytes(),
        "providers": _stt_provider_order(),
        "cache": {
            "enabled": _stt_cache_enabled(),
            "ttl_seconds": _stt_cache_ttl_seconds(),
            "max_items": _stt_cache_max_items(),
        },
    }


def _transcribe_with_openai(audio_bytes: bytes, filename: str) -> str | None:
    language = resolve_transcription_language()
    model = str(_runtime_setting("OPENAI_STT_MODEL", DEFAULT_STT_MODEL))
    client = _get_openai_client()
    if client is None:
        return None
    bounded_timeout = outbox_io_timeout_seconds()
    if bounded_timeout is not None:
        client = client.with_options(
            timeout=bounded_timeout,
            max_retries=0,
        )

    with io.BytesIO(audio_bytes) as audio_file:
        audio_file.name = filename
        payload = {
            "model": model,
            "file": audio_file,
        }
        if language:
            payload["language"] = language
        transcription = client.audio.transcriptions.create(**payload)

    return getattr(transcription, "text", None)


class PrivateAudioTranscriptionError(RuntimeError):
    """A fixed error code, never an SDK response, recording or transcript."""


def transcribe_private_audio_bytes(audio_bytes: bytes, mime_type: str, *,
                                  max_bytes: int, timeout_seconds: float) -> str:
    """One bounded official OpenAI request; no cache, disk, fallback or retries.

    The existing generic STT path intentionally caches transcriptions. An
    institutional accessibility pilot must not put sensitive voice notes in
    that shared cache, and must pin the provider rather than inherit a proxy or
    user-configured gateway. The caller has already verified the Meta media.
    The deadline checks admission/chunks and discards late results; HTTPX
    timeouts are per I/O operation, so this is not hard wall-clock cancellation.
    """
    import json
    if (not isinstance(audio_bytes, bytes) or not 0 < len(audio_bytes) <= max_bytes
            or not 0 < timeout_seconds <= 25):
        raise PrivateAudioTranscriptionError("private_audio_invalid")
    api_key = _openai_api_key()
    model = str(_runtime_setting("OPENAI_STT_MODEL", DEFAULT_STT_MODEL))
    if (not api_key or not llm_provider_network_allowed("openai") or model not in {
            "gpt-4o-transcribe", "gpt-4o-mini-transcribe", "gpt-4o-mini-transcribe-2025-12-15",
            "gpt-transcribe", "whisper-1"}):
        raise PrivateAudioTranscriptionError("private_audio_unavailable")
    mime_type = _canonical_mime_type(mime_type)
    deadline = time.monotonic() + timeout_seconds
    timeout = httpx.Timeout(timeout_seconds, connect=min(3.0, timeout_seconds),
                            read=min(5.0, timeout_seconds), write=min(3.0, timeout_seconds),
                            pool=min(1.0, timeout_seconds))

    def validate_response(response):
        # The SDK reads error bodies before entering our streaming context.
        # Reject them in the transport hook without consuming their contents.
        size = response.headers.get("Content-Length")
        if (time.monotonic() >= deadline or response.status_code != 200
                or response.headers.get("Content-Encoding", "identity") != "identity"
                or (size is not None and (not size.isascii() or not size.isdigit() or int(size) > 32 * 1024))):
            response.close()
            raise PrivateAudioTranscriptionError("private_audio_failed")

    try:
        with httpx.Client(proxy=None, trust_env=False, timeout=timeout,
                          follow_redirects=False, headers={"Accept-Encoding": "identity"},
                          event_hooks={"response": [validate_response]}) as transport:
            with OpenAI(api_key=api_key, base_url="https://api.openai.com/v1",
                        http_client=transport, timeout=timeout, max_retries=0) as client:
                with io.BytesIO(audio_bytes) as audio_file:
                    with client.audio.transcriptions.with_streaming_response.create(
                            model=model, file=(_safe_audio_filename(mime_type), audio_file, mime_type),
                            response_format="json") as response:
                        body = bytearray()
                        for chunk in response.iter_bytes(chunk_size=8192):
                            if time.monotonic() >= deadline or len(body) + len(chunk) > 32 * 1024:
                                raise PrivateAudioTranscriptionError("private_audio_failed")
                            body.extend(chunk)
                        if time.monotonic() >= deadline:
                            raise PrivateAudioTranscriptionError("private_audio_failed")
                        document = json.loads(body)
                        text = document.get("text") if isinstance(document, dict) else None
                        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                            raise PrivateAudioTranscriptionError("private_audio_unintelligible")
                        return text.strip()
    except PrivateAudioTranscriptionError:
        raise
    except Exception:
        raise PrivateAudioTranscriptionError("private_audio_failed") from None


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

    max_audio_bytes = _stt_max_audio_bytes()
    if len(audio_bytes) > max_audio_bytes:
        logger.warning(
            "Audio transcription rejected reason=file_too_large size_bytes=%s max_bytes=%s",
            len(audio_bytes),
            max_audio_bytes,
        )
        return None

    mime_type = _canonical_mime_type(mime_type)
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
            except Exception as exc:  # pragma: no cover - provider-dependent errors
                _log_transcription_failure("openai", exc)
                text = None
        elif provider == "cohere" and _cohere_stt_enabled():
            try:
                from services.cohere_stt_bridge import transcribir_audio_cohere

                text = transcribir_audio_cohere(audio_bytes, mime_type)
            except Exception as exc:  # pragma: no cover - provider-dependent errors
                _log_transcription_failure("cohere", exc)
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
        mime_type = _canonical_mime_type(mime_type)
        from services.attachment_delivery import _sensitive_reference, read_authorized_attachment_bytes
        if _sensitive_reference(url):
            # Authorize even when an earlier request populated a transcript
            # cache. Private storage references never reach HTTP/provider logs.
            data = read_authorized_attachment_bytes(url, mime_type,
                                                    max_bytes=min(_stt_max_audio_bytes(), 15 * 1024 * 1024))
            return transcribe_audio_bytes(data, mime_type)
        url_cache_key = _stt_url_cache_key(url, mime_type)
        cached_text = _stt_cache_get(url_cache_key)
        if cached_text:
            return cached_text

        auth = (account_sid, auth_token) if account_sid and auth_token else None
        max_audio_bytes = _stt_max_audio_bytes()
        audio_response = requests.get(
            url,
            auth=auth,
            stream=True,
            timeout=_audio_download_timeout_seconds(),
        )
        try:
            audio_response.raise_for_status()
            try:
                audio_bytes = read_bounded_response_body(
                    audio_response,
                    max_bytes=max_audio_bytes,
                )
            except MediaDownloadTooLarge:
                audio_bytes = None
        finally:
            audio_response.close()

        if audio_bytes is None:
            logger.warning(
                "Audio transcription rejected reason=file_too_large max_bytes=%s",
                max_audio_bytes,
            )
            return None

        return transcribe_audio_bytes(audio_bytes, mime_type, cache_url=url)

    except requests.exceptions.RequestException as exc:
        logger.warning(
            "Audio download failed error_type=%s status_code=%s",
            type(exc).__name__,
            _safe_error_status(exc),
        )
        return None
    except Exception as exc:
        logger.warning(
            "Audio transcription pipeline failed error_type=%s status_code=%s",
            type(exc).__name__,
            _safe_error_status(exc),
        )
        return None
