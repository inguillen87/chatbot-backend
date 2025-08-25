import logging
from services.openai_tts_bridge import generar_audio_openai
from services.google_text_to_speech import TextToSpeechService

logger = logging.getLogger(__name__)

def generar_audio_con_fallback(text: str) -> str | None:
    """
    Generates audio from text using a fallback mechanism.
    It tries OpenAI first, then falls back to Google.

    Args:
        text (str): The text to synthesize.

    Returns:
        str: The public URL path to the generated audio file, or None if all providers fail.
    """
    logger.info(f"TTS Orchestrator: Attempting to generate audio for text: '{text[:50]}...'")

    # 1. Try OpenAI
    try:
        logger.info("TTS Orchestrator: Trying OpenAI...")
        audio_url = generar_audio_openai(text)
        if audio_url:
            logger.info("TTS Orchestrator: OpenAI successful.")
            return audio_url
        logger.warning("TTS Orchestrator: OpenAI returned None, but did not raise an exception.")
    except Exception as e:
        logger.error(f"TTS Orchestrator: OpenAI failed with an exception: {e}", exc_info=True)

    # 2. Fallback to Google
    try:
        logger.warning("TTS Orchestrator: Falling back to Google...")
        google_tts = TextToSpeechService()
        audio_url = google_tts.synthesize_speech(text)
        if audio_url:
            logger.info("TTS Orchestrator: Google successful.")
            return audio_url
        logger.error("TTS Orchestrator: Google returned None.")
    except Exception as e:
        logger.error(f"TTS Orchestrator: Google failed with an exception: {e}", exc_info=True)

    logger.error("TTS Orchestrator: All TTS providers failed.")
    return None
