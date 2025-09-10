import logging
import os
import hashlib
import shutil
import re

logger = logging.getLogger(__name__)


def sanitize_for_tts(raw: str) -> str:
    """Prepare text so synthesized audio is clear and accessible.

    The sanitizer:
    - Removes URLs, emojis and other non standard symbols.
    - Normalizes numbered options like ``1)`` to ``Opción 1:``.
    - Expands common time abbreviations such as ``hs``/``hrs`` to "horas".
    """

    cleaned = re.sub(r"https?://\S+", "", raw)  # strip URLs
    cleaned = cleaned.replace("*", "")
    # remove emojis and nonstandard symbols
    cleaned = re.sub(r"[^\w\s.,;:0-9áéíóúÁÉÍÓÚñÑüÜ-]", "", cleaned)
    # Normalize numbered options like "1.", "1)" or "1-" to "Opción 1:"
    cleaned = re.sub(r"(?m)^\s*(\d+)[\.)-]\s*", r"Opción \1: ", cleaned)
    # Expand time abbreviations (e.g., "18 hs" -> "18 horas", "24hrs" -> "24 horas")
    cleaned = re.sub(
        r"(?i)\b(\d{1,2}(?:[:.]\d{2})?)\s*(hs|hrs)\b",
        r"\1 horas",
        cleaned,
    )
    cleaned = re.sub(r"(?i)\b(hs|hrs)\b", "horas", cleaned)
    return " ".join(cleaned.split())


def generar_audio_con_fallback(text: str, channel: str | None = None) -> str | None:
    """
    Generates audio from text using a fallback mechanism.
    It tries OpenAI first, luego Cohere y finalmente Google.

    Args:
        text (str): The text to synthesize.

    Returns:
        str: The public URL path to the generated audio file, or None if all providers fail.
    """
    if channel == "whatsapp" and os.getenv("WHATSAPP_TTS_DEFAULT", "false").lower() != "true":
        return None

    text = sanitize_for_tts(text)
    logger.info(f"TTS Orchestrator: Attempting to generate audio for text: '{text[:50]}...'")

    cache_dir = "static/audio_cache"
    os.makedirs(cache_dir, exist_ok=True)
    text_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
    cached_rel_path = os.path.join(cache_dir, f"{text_hash}.mp3")
    if os.path.exists(cached_rel_path):
        logger.info("TTS Orchestrator: Returning cached audio.")
        return f"/{cached_rel_path}"

    def cache_and_return(audio_url: str | None) -> str | None:
        if not audio_url:
            return None
        generated_path = audio_url.lstrip("/")
        try:
            if os.path.exists(generated_path):
                shutil.copyfile(generated_path, cached_rel_path)
                logger.info(f"TTS Orchestrator: Cached audio at {cached_rel_path}")
                return f"/{cached_rel_path}"
        except Exception as e:
            logger.warning(f"TTS Orchestrator: Failed to cache audio: {e}")
        return audio_url

    # Import providers lazily to avoid heavy dependencies at module import time
    from services.openai_tts_bridge import generar_audio_openai
    from services.cohere_tts_bridge import generar_audio_cohere
    from services.google_text_to_speech import TextToSpeechService

    # 1. Try OpenAI
    try:
        speed_env = os.getenv("TTS_SPEECH_SPEED", "0.9")
        try:
            speech_speed = float(speed_env)
        except ValueError:
            speech_speed = 0.9
        logger.info("TTS Orchestrator: Trying OpenAI...")
        audio_url = generar_audio_openai(text, speed=speech_speed)
        if audio_url:
            logger.info("TTS Orchestrator: OpenAI successful.")
            return cache_and_return(audio_url)
        logger.warning("TTS Orchestrator: OpenAI returned None, but did not raise an exception.")
    except Exception as e:
        logger.error(f"TTS Orchestrator: OpenAI failed with an exception: {e}", exc_info=True)

    # 2. Try Cohere
    try:
        logger.info("TTS Orchestrator: Trying Cohere...")
        audio_url = generar_audio_cohere(text)
        if audio_url:
            logger.info("TTS Orchestrator: Cohere successful.")
            return cache_and_return(audio_url)
        logger.warning("TTS Orchestrator: Cohere returned None, but did not raise an exception.")
    except Exception as e:
        logger.error(f"TTS Orchestrator: Cohere failed with an exception: {e}", exc_info=True)

    # 3. Fallback to Google
    try:
        logger.warning("TTS Orchestrator: Falling back to Google...")
        google_tts = TextToSpeechService()
        audio_url = google_tts.synthesize_speech(text)
        if audio_url:
            logger.info("TTS Orchestrator: Google successful.")
            return cache_and_return(audio_url)
        logger.error("TTS Orchestrator: Google returned None.")
    except Exception as e:
        logger.error(f"TTS Orchestrator: Google failed with an exception: {e}", exc_info=True)

    logger.error("TTS Orchestrator: All TTS providers failed.")
    return None
