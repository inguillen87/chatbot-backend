import os
import openai
import logging
import uuid
import httpx
from cachetools import TTLCache

logger = logging.getLogger(__name__)

_TTS_CACHE: TTLCache[str, str] = TTLCache(maxsize=128, ttl=3600)

_OPENAI_SUPPORTED_VOICES = {
    "alloy",
    "ash",
    "coral",
    "echo",
    "fable",
    "onyx",
    "sage",
    "shimmer",
    "nova",
}

_VOICE_ALIASES = {
    "sol": "shimmer",
    "latina": "shimmer",
    "latin": "shimmer",
    "rioplatense": "shimmer",
    "rioplatense-femenina": "shimmer",
    "rioplatense-masculina": "alloy",
}


def _normalize_voice(requested_voice: str | None) -> str:
    """Return a voice accepted by OpenAI, applying aliases and fallbacks."""

    fallback_env = os.getenv("OPENAI_TTS_FALLBACK_VOICE", "nova")
    fallback_normalized = _VOICE_ALIASES.get(
        fallback_env.strip().lower(), fallback_env.strip().lower()
    )
    fallback = (
        fallback_normalized
        if fallback_normalized in _OPENAI_SUPPORTED_VOICES
        else "alloy"
    )

    if requested_voice:
        candidate = _VOICE_ALIASES.get(
            requested_voice.strip().lower(), requested_voice.strip().lower()
        )
        if candidate in _OPENAI_SUPPORTED_VOICES:
            return candidate

        logger.warning(
            "OpenAI TTS voice '%s' is not supported. Falling back to '%s'.",
            requested_voice,
            fallback,
        )

    default_env = os.getenv("OPENAI_TTS_DEFAULT_VOICE")
    if default_env:
        candidate = _VOICE_ALIASES.get(
            default_env.strip().lower(), default_env.strip().lower()
        )
        if candidate in _OPENAI_SUPPORTED_VOICES:
            return candidate

    return fallback

def clear_tts_cache() -> None:
    """Utility mainly for tests to clear the local TTS cache."""
    _TTS_CACHE.clear()

def generar_audio_openai(
    text: str,
    *,
    speed: float = 0.8,
    voice: str | None = None,
    model: str | None = None,
    style: str | None = None,
) -> str | None:
    """
    Generates audio from text using OpenAI's Text-to-Speech API.

    Args:
        text (str): The text to synthesize.

    Returns:
        str: The public URL path to the generated audio file, or None if synthesis fails.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY not found in environment variables.")
        return None

    selected_voice = _normalize_voice(voice)

    cache_key = "|".join(
        [
            text,
            str(speed),
            selected_voice,
            model or "",
            style or "",
        ]
    )
    if cache_key in _TTS_CACHE:
        return _TTS_CACHE[cache_key]

    try:
        # Disable reading proxy settings from the environment to avoid passing
        # unsupported `proxies` arguments into the OpenAI client. Some
        # environments (like CI) define `http_proxy`/`https_proxy` which would
        # otherwise cause `openai.OpenAI` to fail during initialization.
        http_client = httpx.Client(proxy=None, trust_env=False)
        client = openai.OpenAI(api_key=api_key, http_client=http_client)

        logger.info(f"Requesting OpenAI speech synthesis for text: '{text[:50]}...'")

        request_payload = {
            "model": model or os.getenv("OPENAI_TTS_MODEL", "tts-1"),
            "voice": selected_voice,
            "input": text,
            "speed": speed,
        }

        if style:
            request_payload["style"] = style

        response = client.audio.speech.create(**request_payload)

        # Generate a unique filename
        filename = f"{uuid.uuid4()}.mp3"
        output_dir = "static/audio_responses"
        # The full path to save the file
        output_path = os.path.join(output_dir, filename)

        # The public URL path for the client to access
        public_url_path = f"/{output_dir}/{filename}"

        # Ensure the output directory exists
        os.makedirs(output_dir, exist_ok=True)

        # Stream the response content to the file
        response.stream_to_file(output_path)
        logger.info(f"Audio content written to file: {output_path}")

        _TTS_CACHE[cache_key] = public_url_path

        return public_url_path

    except Exception as e:
        logger.error(f"An error occurred during OpenAI speech synthesis: {e}", exc_info=True)
        return None

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    # IMPORTANT: Ensure OPENAI_API_KEY is set in your environment for this test.
    test_text = "Hola, este es un audio de prueba generado por OpenAI."
    audio_path = generar_audio_openai(test_text)
    if audio_path:
        logger.info(f"Audio generado con éxito en: {audio_path}")
    else:
        logger.error("Falló la generación de audio con OpenAI.")
