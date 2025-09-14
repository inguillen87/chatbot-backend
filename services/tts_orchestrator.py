import logging
import os
import hashlib
import shutil
import re
import textwrap

logger = logging.getLogger(__name__)


def _summarize_if_long(text: str, max_chars: int = 500) -> str:
    """Truncate long strings so speech does not become overwhelming."""

    if len(text) <= max_chars:
        return text
    return textwrap.shorten(text, width=max_chars, placeholder=" ...")


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
    cleaned = re.sub(r"[^\w\s.,;:0-9áéíóúÁÉÍÓÚñÑüÜ-]", "", cleaned)
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
    cleaned = " ".join(cleaned.split())
    return _summarize_if_long(cleaned)


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
        return f"/{cached_rel_path}"

    def cache_and_return(audio_url: str | None) -> str | None:
        if not audio_url:
            return None
        generated_path = audio_url.lstrip("/")
        try:
            if os.path.exists(generated_path):
                shutil.copyfile(generated_path, cached_rel_path)
                logger.info(f"TTS Service: Cached audio at {cached_rel_path}")
                return f"/{cached_rel_path}"
        except Exception as e:
            logger.warning(f"TTS Service: Failed to cache audio file from {generated_path}: {e}")
        return audio_url

    # Import the OpenAI provider lazily to avoid heavy dependencies at module import time
    from services.openai_tts_bridge import generar_audio_openai

    # Try OpenAI
    try:
        speed_env = os.getenv("TTS_SPEECH_SPEED", "0.85")
        try:
            speech_speed = float(speed_env)
        except (ValueError, TypeError):
            speech_speed = 0.85
        logger.info("TTS Service: Trying OpenAI...")
        audio_url = generar_audio_openai(text, speed=speech_speed)
        if audio_url:
            logger.info("TTS Service: OpenAI successful.")
            return cache_and_return(audio_url)

        logger.warning("TTS Service: OpenAI returned None without raising an exception.")
        return None
    except Exception as e:
        logger.error(f"TTS Service: OpenAI failed with an exception: {e}", exc_info=True)
        return None
