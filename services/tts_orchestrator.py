import logging
import os
import hashlib
import shutil
import re
from services.openai_tts_bridge import generar_audio_openai
from services.cohere_tts_bridge import generar_audio_cohere
from services.google_text_to_speech import TextToSpeechService

logger = logging.getLogger(__name__)

def generar_audio_con_fallback(text: str) -> str | None:
    """
    Generates audio from text using a fallback mechanism.
    It tries OpenAI first, luego Cohere y finalmente Google.

    Args:
        text (str): The text to synthesize.

    Returns:
        str: The public URL path to the generated audio file, or None if all providers fail.
    """
    def sanitize_for_tts(raw: str) -> str:
        # Remove URLs and markdown characters so the audio doesn't read them
        cleaned = re.sub(r"https?://\S+", "", raw)
        cleaned = cleaned.replace("*", "")
        # Replace numbered lists with a more speech-friendly format
        cleaned = re.sub(r"(?m)^\s*(\d+)\.\s*", r"Opción \1: ", cleaned)
        return " ".join(cleaned.split())

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

    # 1. Try OpenAI
    try:
        logger.info("TTS Orchestrator: Trying OpenAI...")
        audio_url = generar_audio_openai(text, speed=1.25)
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
