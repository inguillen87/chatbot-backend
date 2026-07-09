import logging
import os
import hashlib
import shutil
import re
import textwrap
from collections import OrderedDict
from threading import Lock
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from config import BACKEND_URL
from services.media_cache_policy import IMMUTABLE_PUBLIC_CACHE

ProviderCallable = Callable[[str], str | None]

logger = logging.getLogger(__name__)
_FALSEY_VALUES = {"0", "false", "no", "off"}
_TTS_CACHE_METRIC_KEYS = (
    "requests",
    "cache_hits",
    "cache_misses",
    "cache_disabled",
    "cache_writes",
    "cache_write_failures",
    "provider_successes",
    "provider_failures",
    "generation_failures",
    "warmup_requests",
    "warmup_successes",
    "warmup_failures",
)
_TTS_CACHE_METRICS = {key: 0 for key in _TTS_CACHE_METRIC_KEYS}
_TTS_CACHE_METRICS_LOCK = Lock()


def _increment_metric(name: str, amount: int = 1) -> None:
    if name not in _TTS_CACHE_METRICS:
        return
    with _TTS_CACHE_METRICS_LOCK:
        _TTS_CACHE_METRICS[name] += amount


def get_tts_cache_metrics() -> dict[str, int]:
    """Return in-process TTS cache counters without exposing text content."""

    with _TTS_CACHE_METRICS_LOCK:
        return dict(_TTS_CACHE_METRICS)


def reset_tts_cache_metrics() -> None:
    """Reset counters. Intended for tests and local diagnostics."""

    with _TTS_CACHE_METRICS_LOCK:
        for key in _TTS_CACHE_METRICS:
            _TTS_CACHE_METRICS[key] = 0


def _summarize_if_long(text: str, max_chars: int = 500) -> str:
    """Truncate long strings so speech does not become overwhelming."""

    if len(text) <= max_chars:
        return text
    return textwrap.shorten(text, width=max_chars, placeholder=" ...")


def _normalize_punctuation(cleaned: str) -> str:
    """Ensure punctuation produces natural pauses when read aloud."""

    cleaned = re.sub(r"\s*([.,;:])\s*", r"\1 ", cleaned)
    cleaned = re.sub(r"\s+\?", "? ", cleaned)
    cleaned = re.sub(r"\s+!", "! ", cleaned)

    return " ".join(cleaned.split())


def sanitize_for_tts(raw: str) -> str:
    """Prepare text so synthesized audio is clear and accessible.

    The sanitizer:
    - Removes URLs, emojis and other non standard symbols.
    - Normalizes numbered options like ``1)`` to ``Opción 1:``.
    - Expands common time abbreviations such as ``hs``/``hrs`` to "horas".
    - Converts bullet points to explicit "Punto" markers.
    - Shortens overly long texts.
    """

    cleaned = re.sub(r"https?://\S+", "", raw)  # strip URLs
    cleaned = cleaned.replace("*", "")
    # remove emojis and nonstandard symbols
    cleaned = re.sub(r"[^\w\s.,;:¿¡0-9áéíóúÁÉÍÓÚñÑüÜ-]", "", cleaned)
    # Normalize numbered options like "1.", "1)" or "1-" to "Opción 1:"
    cleaned = re.sub(r"(?m)^\s*(\d+)[\.)-]\s*", r"Opción \1: ", cleaned)
    # Convert bullet points to explicit prompts
    cleaned = re.sub(r"(?m)^\s*[\-•]\s*", "Punto: ", cleaned)
    # Expand time abbreviations (e.g., "18 hs" -> "18 horas", "24hrs" -> "24 horas")
    cleaned = re.sub(
        r"(?i)\b(\d{1,2}(?:[:.]\d{2})?)\s*(hs|hrs)\b",
        r"\1 horas",
        cleaned,
    )
    cleaned = re.sub(r"(?i)\b(hs|hrs)\b", "horas", cleaned)
    cleaned = re.sub(r"\s*\n+\s*", ". ", cleaned)
    cleaned = _normalize_punctuation(cleaned)

    regionalisms = {
        "av.": "avenida",
        "av": "avenida",
        "caba": "ciudad de buenos aires",
        "uds": "ustedes",
        "sres": "señores",
    }
    for short, expanded in regionalisms.items():
        cleaned = re.sub(rf"\b{short}\b", expanded, cleaned, flags=re.IGNORECASE)

    return _summarize_if_long(cleaned)


def _provider_order_from_env() -> Iterable[str]:
    raw_order = os.getenv("TTS_PROVIDER_ORDER", "openai")
    normalized = [provider.strip().lower() for provider in raw_order.split(",") if provider.strip()]
    if not normalized:
        normalized = ["openai"]

    return list(OrderedDict.fromkeys(normalized))


def _truthy_env(*names: str) -> bool:
    truthy = {"1", "true", "yes", "on"}
    return any(str(os.getenv(name) or "").strip().lower() in truthy for name in names)


def _cohere_tts_enabled() -> bool:
    return _truthy_env("TTS_COHERE_ENABLED", "COHERE_ENABLED")


def _tts_cache_enabled() -> bool:
    return str(os.getenv("TTS_CACHE_ENABLED", "true")).strip().lower() not in _FALSEY_VALUES


def _first_env_value(*names: str) -> str | None:
    for name in names:
        value = str(os.getenv(name) or "").strip()
        if value:
            return value

    return None


def _audio_cache_public_base_url() -> str | None:
    return _first_env_value(
        "TTS_AUDIO_CACHE_PUBLIC_BASE_URL",
        "CLOUDFLARE_AUDIO_CACHE_PUBLIC_BASE_URL",
    )


def _is_audio_cache_path(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/").lstrip("/")
    return normalized.startswith("static/audio_cache/")


def get_tts_audio_cache_public_config() -> dict[str, Any]:
    """Return cache publication settings without exposing spoken text."""

    public_base_url = _audio_cache_public_base_url()
    parsed = urlparse(public_base_url) if public_base_url else None
    host = parsed.netloc if parsed and parsed.netloc else None
    cdn_configured = bool(public_base_url)
    public_url_mode = "cdn" if cdn_configured else "backend_static"

    return {
        "contract_version": "tts.audio_cache_publication.v1",
        "provider": "cloudflare_or_backend_static",
        "recommended_provider": "cloudflare",
        "public_path": "/static/audio_cache",
        "public_url_mode": public_url_mode,
        "cdn_configured": cdn_configured,
        "cdn_host": host,
        "edge_ready": cdn_configured,
        "cache_control": IMMUTABLE_PUBLIC_CACHE,
        "headers": {
            "Cache-Control": IMMUTABLE_PUBLIC_CACHE,
            "X-Content-Type-Options": "nosniff",
        },
        "privacy": {
            "content_text_exposed": False,
            "pii_in_url": False,
            "filename_strategy": "sha256_audio_fingerprint",
        },
        "next_action": "monitor_hit_rate" if cdn_configured else "configure_cloudflare_audio_cache_public_base_url",
        "cdn_env_vars": [
            "TTS_AUDIO_CACHE_PUBLIC_BASE_URL",
            "CLOUDFLARE_AUDIO_CACHE_PUBLIC_BASE_URL",
        ],
        "required_env": [
            {
                "name": "CLOUDFLARE_AUDIO_CACHE_PUBLIC_BASE_URL",
                "target": "backend_render",
                "configured": bool(str(os.getenv("CLOUDFLARE_AUDIO_CACHE_PUBLIC_BASE_URL") or "").strip()),
            },
            {
                "name": "TTS_AUDIO_CACHE_PUBLIC_BASE_URL",
                "target": "backend_render",
                "configured": bool(str(os.getenv("TTS_AUDIO_CACHE_PUBLIC_BASE_URL") or "").strip()),
            },
        ],
    }


def _public_backend_url(relative_path: str) -> str:
    url_path = relative_path.replace("\\", "/")
    if _is_audio_cache_path(url_path):
        audio_cache_base_url = _audio_cache_public_base_url()
        if audio_cache_base_url:
            return f"{audio_cache_base_url.rstrip('/')}/{url_path.lstrip('/')}"

    return f"{BACKEND_URL}/{url_path.lstrip('/')}"


def _build_cache_fingerprint(
    *,
    cache_namespace: str,
    text: str,
    voice: str | None,
    model: str | None,
    style: str | None,
    speed: float | None,
) -> str:
    return "|".join(
        [
            cache_namespace,
            text,
            str(voice or ""),
            str(model or ""),
            str(style or ""),
            str(speed if speed is not None else ""),
        ]
    )


def generar_audio(
    text: str,
    *,
    voice: str | None = None,
    model: str | None = None,
    style: str | None = None,
    speed: float | None = None,
    cache_namespace: str | None = None,
) -> str | None:
    """
    Generates audio from text using OpenAI's Text-to-Speech API.
    It includes a caching mechanism to avoid re-generating audio for the same text.

    Args:
        text (str): The text to synthesize.

    Returns:
        str: The public URL path to the generated audio file, or None if synthesis fails.
    """
    _increment_metric("requests")
    text = sanitize_for_tts(text)
    if not text:
        _increment_metric("generation_failures")
        return None

    logger.info(f"TTS Service: Attempting to generate audio for text: '{text[:50]}...'")

    effective_speed = speed
    if effective_speed is None:
        speed_env = os.getenv("TTS_SPEECH_SPEED", "0.95")
        try:
            effective_speed = float(speed_env)
        except (ValueError, TypeError):
            effective_speed = 0.95

    effective_voice = (
        voice
        or os.getenv("OPENAI_TTS_VOICE")
        or os.getenv("OPENAI_TTS_DEFAULT_VOICE")
        or os.getenv("OPENAI_TTS_FALLBACK_VOICE")
        or "nova"
    )
    effective_model = model or os.getenv("OPENAI_TTS_MODEL")
    effective_style = style or os.getenv("OPENAI_TTS_STYLE")

    cache_enabled = _tts_cache_enabled()
    cache_dir = "static/audio_cache"
    if cache_enabled:
        os.makedirs(cache_dir, exist_ok=True)
    else:
        _increment_metric("cache_disabled")

    cache_fingerprint = _build_cache_fingerprint(
        cache_namespace=cache_namespace or os.getenv("TTS_CACHE_NAMESPACE") or "default",
        text=text,
        voice=effective_voice,
        model=effective_model,
        style=effective_style,
        speed=effective_speed,
    )
    text_hash = hashlib.sha256(cache_fingerprint.encode("utf-8")).hexdigest()
    cached_rel_path = os.path.join(cache_dir, f"{text_hash}.mp3")
    if cache_enabled and os.path.exists(cached_rel_path):
        logger.info("TTS Service: Returning cached audio.")
        _increment_metric("cache_hits")
        return _public_backend_url(cached_rel_path)
    if cache_enabled:
        _increment_metric("cache_misses")

    def cache_and_return(audio_url: str | None) -> str | None:
        if not audio_url:
            return None

        # Ensure audio_url is a relative path before proceeding
        # This handles cases where a full URL might be returned by a service
        if audio_url.startswith("http://") or audio_url.startswith("https://"):
             # If it's already a full URL, we can't cache it locally easily, return as is
            return audio_url

        generated_path = audio_url.lstrip("/")
        if not cache_enabled:
            return _public_backend_url(generated_path)

        try:
            if os.path.exists(generated_path):
                shutil.copyfile(generated_path, cached_rel_path)
                logger.info(f"TTS Service: Cached audio at {cached_rel_path}")
                _increment_metric("cache_writes")
                return _public_backend_url(cached_rel_path)
        except Exception as e:
            logger.warning(f"TTS Service: Failed to cache audio file from {generated_path}: {e}")
            _increment_metric("cache_write_failures")

        # Fallback to returning the original URL if caching fails but URL is valid
        return _public_backend_url(generated_path)

    def _provider_factory() -> dict[str, ProviderCallable]:
        providers: dict[str, ProviderCallable] = {}

        def _register(name: str, func: ProviderCallable) -> None:
            providers[name] = func

        from services.openai_tts_bridge import generar_audio_openai

        def _openai_provider(clean_text: str) -> str | None:
            return generar_audio_openai(
                clean_text,
                speed=effective_speed or 0.95,
                voice=effective_voice,
                model=effective_model,
                style=effective_style,
            )

        _register("openai", _openai_provider)

        if _cohere_tts_enabled():
            def _cohere_provider(clean_text: str) -> str | None:
                from services.cohere_tts_bridge import generar_audio_cohere

                voice = os.getenv("COHERE_TTS_VOICE", "argentina-female")
                style = os.getenv("COHERE_TTS_STYLE", "informative")
                model = os.getenv("COHERE_TTS_MODEL")
                speed_env = os.getenv("COHERE_TTS_SPEED", "0.85")
                try:
                    speech_speed = float(speed_env)
                except (ValueError, TypeError):
                    speech_speed = 0.85

                return generar_audio_cohere(
                    clean_text,
                    voice=voice,
                    style=style,
                    model=model,
                    speed=speech_speed,
                )

            _register("cohere", _cohere_provider)

        return providers

    providers = _provider_factory()

    for provider_name in _provider_order_from_env():
        provider = providers.get(provider_name)
        if not provider:
            logger.debug("TTS Service: Provider '%s' is not available", provider_name)
            continue

        try:
            logger.info("TTS Service: Trying %s provider...", provider_name.title())
            audio_url = provider(text)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error(
                "TTS Service: Provider %s raised an exception: %s",
                provider_name,
                exc,
                exc_info=True,
            )
            continue

        if audio_url:
            logger.info(
                "TTS Service: %s provider returned audio successfully.",
                provider_name.title(),
            )
            _increment_metric("provider_successes")
            return cache_and_return(audio_url)

        logger.warning(
            "TTS Service: %s provider returned no audio.", provider_name.title()
        )
        _increment_metric("provider_failures")

    _increment_metric("generation_failures")
    return None


def warm_tts_cache(entries: Iterable[dict[str, Any] | str]) -> dict[str, Any]:
    """Pre-generate fixed TTS audio and report only cache-safe metadata.

    Each dict entry accepts ``tts_cache_text``/``audio_text``/``text`` and
    ``tts_cache_namespace``/``cache_namespace`` plus optional voice/model/style/speed.
    The returned summary intentionally omits spoken text to avoid leaking PII in
    logs or diagnostics.
    """

    summary: dict[str, Any] = {
        "requested": 0,
        "ready": 0,
        "failed": 0,
        "items": [],
    }

    for entry in entries:
        summary["requested"] += 1
        _increment_metric("warmup_requests")

        if isinstance(entry, str):
            item: dict[str, Any] = {"text": entry}
        elif isinstance(entry, dict):
            item = entry
        else:
            summary["failed"] += 1
            _increment_metric("warmup_failures")
            summary["items"].append({"status": "failed", "reason": "invalid_entry"})
            continue

        text = item.get("tts_cache_text") or item.get("audio_text") or item.get("text")
        namespace = item.get("tts_cache_namespace") or item.get("cache_namespace")
        if not text or not namespace:
            summary["failed"] += 1
            _increment_metric("warmup_failures")
            summary["items"].append(
                {
                    "cache_namespace": namespace,
                    "status": "failed",
                    "reason": "missing_text_or_namespace",
                }
            )
            continue

        audio_url = generar_audio(
            str(text),
            voice=item.get("tts_voice") or item.get("voice"),
            model=item.get("tts_model") or item.get("model"),
            style=item.get("tts_style") or item.get("style"),
            speed=item.get("tts_speed") or item.get("speed"),
            cache_namespace=str(namespace),
        )
        if audio_url:
            summary["ready"] += 1
            _increment_metric("warmup_successes")
            summary["items"].append(
                {
                    "cache_namespace": str(namespace),
                    "status": "ready",
                    "audio_url": audio_url,
                }
            )
        else:
            summary["failed"] += 1
            _increment_metric("warmup_failures")
            summary["items"].append(
                {
                    "cache_namespace": str(namespace),
                    "status": "failed",
                    "reason": "generation_failed",
                }
            )

    return summary
