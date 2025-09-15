# services/elevenlabs_bridge.py
import os
import elevenlabs
import logging
import uuid
from cachetools import TTLCache

logger = logging.getLogger(__name__)

_TTS_CACHE: TTLCache[str, str] = TTLCache(maxsize=128, ttl=3600)

def clear_elevenlabs_cache() -> None:
    """Utility mainly for tests to clear the local TTS cache."""
    _TTS_CACHE.clear()

def generar_audio_elevenlabs(text: str, speed: float = 1.0) -> str | None:
    """
    Generates audio from text using ElevenLabs's Text-to-Speech API.

    Args:
        text (str): The text to synthesize.
        speed (float): The speed of the speech.

    Returns:
        str: The public URL path to the generated audio file, or None if synthesis fails.
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        logger.warning("ELEVENLABS_API_KEY not found in environment variables.")
        return None

    cache_key = f"{text}|{speed}"
    if cache_key in _TTS_CACHE:
        return _TTS_CACHE[cache_key]

    try:
        client = elevenlabs.ElevenLabs(api_key=api_key)

        logger.info(f"Requesting ElevenLabs speech synthesis for text: '{text[:50]}...'")

        # Voice ID for a male Argentinian voice. This can be changed to other available voices.
        # A good one for Argentinian Spanish is "yoZ06aHiZ5aJE25DAmWc" (Rachel)
        # or find a custom one. For now, I'll use a pre-made one.
        # Let's use a standard, high-quality voice for now.
        # "21m00Tcm4TlvDq8ikWAM" is "Rachel"
        # "29vD33N1CtxCmqQRPO9t" is "Drew"
        # "5Q0t7uMcjvnagumLfvZi" is "Clyde"
        # I will use "Rachel" as it's a good quality voice.
        # The user can later specify a custom voice if they have one cloned.
        audio = client.generate(
            text=text,
            voice="Rachel",
            model="eleven_multilingual_v2",
        )

        filename = f"{uuid.uuid4()}.mp3"
        output_dir = "static/audio_responses"
        output_path = os.path.join(output_dir, filename)
        public_url_path = f"/{output_dir}/{filename}"
        os.makedirs(output_dir, exist_ok=True)

        elevenlabs.save(audio, output_path)
        logger.info(f"Audio content written to file: {output_path}")

        _TTS_CACHE[cache_key] = public_url_path

        return public_url_path

    except Exception as e:
        logger.error(f"An error occurred during ElevenLabs speech synthesis: {e}", exc_info=True)
        return None
