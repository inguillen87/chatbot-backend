import logging
import os
import hashlib
import shutil
import re
import textwrap
from collections import OrderedDict
from typing import Callable, Iterable

from config import BACKEND_URL

ProviderCallable = Callable[[str], str | None]

logger = logging.getLogger(__name__)


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
    raw_order = os.getenv("TTS_PROVIDER_ORDER", "openai,cohere")
    normalized = [provider.strip().lower() for provider in raw_order.split(",") if provider.strip()]
    if not normalized:
        normalized = ["openai", "cohere"]

    return list(OrderedDict.fromkeys(normalized))


def generar_audio(text: str) -> str | None:
    """
    Generates audio from text using OpenAI's Text-to-Speech API.
    It includes a caching mechanism to avoid re-generating audio for the same text.

    Args:
        text (str): The text to synthesize.

    Returns:
        str: The public URL path to the generated audio file, or None if synthesis fails.
    """
    text = sanitize_for_tts(text)
    logger.info(f"TTS Service: Attempting to generate audio for text: '{text[:50]}...'")

    cache_dir = "static/audio_cache"
    os.makedirs(cache_dir, exist_ok=True)
    text_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
    cached_rel_path = os.path.join(cache_dir, f"{text_hash}.mp3")
    if os.path.exists(cached_rel_path):
        logger.info("TTS Service: Returning cached audio.")
        return f"{BACKEND_URL}/{cached_rel_path}"

    def cache_and_return(audio_url: str | None) -> str | None:
        if not audio_url:
            return None

        # Ensure audio_url is a relative path before proceeding
        # This handles cases where a full URL might be returned by a service
        if audio_url.startswith("http://") or audio_url.startswith("https://"):
             # If it's already a full URL, we can't cache it locally easily, return as is
            return audio_url

        generated_path = audio_url.lstrip("/")
        try:
            if os.path.exists(generated_path):
                shutil.copyfile(generated_path, cached_rel_path)
                logger.info(f"TTS Service: Cached audio at {cached_rel_path}")
                return f"{BACKEND_URL}/{cached_rel_path}"
        except Exception as e:
            logger.warning(f"TTS Service: Failed to cache audio file from {generated_path}: {e}")

        # Fallback to returning the original URL if caching fails but URL is valid
        return f"{BACKEND_URL}/{generated_path}"

    def _provider_factory() -> dict[str, ProviderCallable]:
        providers: dict[str, ProviderCallable] = {}

        def _register(name: str, func: ProviderCallable) -> None:
            providers[name] = func

        from services.openai_tts_bridge import generar_audio_openai

        def _openai_provider(clean_text: str) -> str | None:
            speed_env = os.getenv("TTS_SPEECH_SPEED", "0.8")
            try:
                speech_speed = float(speed_env)
            except (ValueError, TypeError):
                speech_speed = 0.8

            voice = os.getenv("OPENAI_TTS_VOICE")
            model = os.getenv("OPENAI_TTS_MODEL")
            style = os.getenv("OPENAI_TTS_STYLE")

            return generar_audio_openai(
                clean_text,
                speed=speech_speed,
                voice=voice,
                model=model,
                style=style,
            )

        _register("openai", _openai_provider)

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
            return cache_and_return(audio_url)

        logger.warning(
            "TTS Service: %s provider returned no audio.", provider_name.title()
        )

    return None
