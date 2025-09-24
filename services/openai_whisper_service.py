import logging
import openai
import requests
import tempfile
import os

logger = logging.getLogger(__name__)

# The client is initialized in openai_bridge.py, we can reuse it.
try:
    from .openai_bridge import client as openai_client
except ImportError:
    openai_client = None

class OpenAIWhisperService:
    def __init__(self):
        if openai_client is None:
            logger.error("OpenAI client not initialized. Cannot use Whisper service.")
            self.client = None
        else:
            self.client = openai_client

    def transcribe_audio_url(self, audio_url: str) -> str:
        """
        Downloads an audio file from a URL and transcribes it using OpenAI Whisper.
        """
        if not self.client:
            logger.error("Whisper service cannot transcribe: OpenAI client not available.")
            return ""

        if not audio_url:
            logger.warning("No audio URL provided to transcribe.")
            return ""

        try:
            # Download the audio file
            response = requests.get(audio_url, timeout=20)
            response.raise_for_status()
            audio_content = response.content

            # Save to a temporary file because the OpenAI library needs a file path
            with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as temp_audio_file:
                temp_audio_file.write(audio_content)
                temp_file_path = temp_audio_file.name

            # Transcribe using Whisper
            with open(temp_file_path, "rb") as audio_file_obj:
                transcription = self.client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file_obj
                )

            # Clean up the temporary file
            os.remove(temp_file_path)

            logger.info(f"Successfully transcribed audio from URL: {audio_url}")
            return transcription.text

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to download audio file from {audio_url}: {e}", exc_info=True)
            return ""
        except openai.APIError as e:
            logger.error(f"OpenAI API error during transcription: {e}", exc_info=True)
            return ""
        except Exception as e:
            logger.error(f"An unexpected error occurred during audio transcription: {e}", exc_info=True)
            if 'temp_file_path' in locals() and os.path.exists(temp_file_path):
                os.remove(temp_file_path) # Ensure cleanup on error
            return ""

# Create a singleton instance of the service
whisper_service = OpenAIWhisperService()