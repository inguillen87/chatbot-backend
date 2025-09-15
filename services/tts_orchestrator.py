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


def _deletrear_numeros(texto: str) -> str:
    """Expande números en una cadena a su forma hablada, deletreando dígitos."""
    def reemplazo(match):
        return ' '.join(list(match.group(0)))

    # Deletrea números de 5 o más dígitos para IDs de ticket, etc.
    texto = re.sub(r'\b\d{5,}\b', reemplazo, texto)
    # Deletrea "M-12345" como "eme guión uno dos tres..."
    texto = re.sub(r'\b[A-Za-z]-\d+\b', lambda m: ' '.join(list(m.group(0).replace('-', ' guión '))), texto)
    return texto

def sanitize_for_tts(raw: str) -> str:
    """Prepare text so synthesized audio is clear and accessible."""
    cleaned = re.sub(r"https?://\S+", "", raw)
    cleaned = cleaned.replace("*", "")
    cleaned = re.sub(r"[^\w\s.,;:0-9áéíóúÁÉÍÓÚñÑüÜ-]", "", cleaned)
    cleaned = re.sub(r"(?m)^\s*(\d+)[\.)-]\s*", r"Opción \1: ", cleaned)
    cleaned = re.sub(r"(?m)^\s*[\-•]\s*", "Punto: ", cleaned)
    cleaned = re.sub(r"(?i)\b(\d{1,2}(?:[:.]\d{2})?)\s*(hs|hrs)\b", r"\1 horas", cleaned)
    cleaned = re.sub(r"(?i)\b(hs|hrs)\b", "horas", cleaned)

    # Deletrear números de ticket y otros códigos largos
    cleaned = _deletrear_numeros(cleaned)

    # Mejorar el ritmo y la entonación
    cleaned = cleaned.replace("\n\n", ". ") # Doble salto de línea como pausa mayor
    cleaned = cleaned.replace("\n", ". ") # Salto de línea simple como pausa
    cleaned = re.sub(r'\s*\.+\s*', '. ', cleaned) # Normalizar múltiples puntos
    cleaned = " ".join(cleaned.split())

    # Pausa después del saludo
    if cleaned.lower().startswith("hola"):
        cleaned = cleaned.replace("Hola", "Hola, ", 1)

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
