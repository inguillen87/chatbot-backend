import logging
import os
import uuid
import hashlib
import unicodedata
import time
from google.cloud import texttospeech

logger = logging.getLogger(__name__)

CACHE = {}
CACHE_TTL = 7 * 24 * 3600  # 7 days

def _normalize_text(text):
    """Normalizes text for consistent hashing."""
    return unicodedata.normalize('NFKD', text.lower()).encode('ascii', 'ignore').decode('ascii').strip()

def _get_cache_key(text, voice_params):
    """Generates a cache key from text and voice parameters."""
    normalized_text = _normalize_text(text)
    key_str = f"{normalized_text}|{voice_params.language_code}|{voice_params.name}|{voice_params.ssml_gender}"
    return hashlib.md5(key_str.encode()).hexdigest()

class TextToSpeechService:
    def __init__(self, credentials_path=None):
        if credentials_path:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path
        try:
            self.client = texttospeech.TextToSpeechClient()
            logger.info("Google Text-to-Speech client initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize Google Text-to-Speech client: {e}", exc_info=True)
            self.client = None

    def is_cached(self, text: str) -> bool:
        """Checks if the audio for the given text is cached."""
        voice = texttospeech.VoiceSelectionParams(
            language_code="es-US", name="es-US-Standard-A",
            ssml_gender=texttospeech.SsmlVoiceGender.FEMALE
        )
        cache_key = _get_cache_key(text, voice)
        cached = CACHE.get(cache_key)
        return cached and (time.time() - cached['timestamp']) < CACHE_TTL

    def synthesize_speech(self, text: str) -> str | None:
        if not self.client:
            logger.error("Text-to-Speech client is not available.")
            return None

        voice = texttospeech.VoiceSelectionParams(
            language_code="es-US", name="es-US-Standard-A",
            ssml_gender=texttospeech.SsmlVoiceGender.FEMALE
        )

        cache_key = _get_cache_key(text, voice)

        # Check cache
        if self.is_cached(text):
            logger.info(f"TTS cache hit for key: {cache_key}")
            return CACHE[cache_key]['url']

        try:
            synthesis_input = texttospeech.SynthesisInput(text=text)
            audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3)

            logger.info(f"Requesting speech synthesis for text: '{text[:50]}...'")
            response = self.client.synthesize_speech(
                input=synthesis_input, voice=voice, audio_config=audio_config
            )
            logger.info("Received synthesized speech response from Google API.")

            filename = f"{uuid.uuid4()}.mp3"
            output_dir = "static/audio_responses"
            output_path = os.path.join(output_dir, filename)
            public_url_path = f"/{output_dir}/{filename}"
            os.makedirs(output_dir, exist_ok=True)

            with open(output_path, "wb") as out:
                out.write(response.audio_content)
                logger.info(f"Audio content written to file: {output_path}")

            # Store in cache
            CACHE[cache_key] = {'url': public_url_path, 'timestamp': time.time()}

            return public_url_path

        except Exception as e:
            logger.error(f"An unexpected error occurred during speech synthesis: {e}", exc_info=True)
            return None
