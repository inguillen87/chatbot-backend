import logging
import os
import tempfile
import requests
from pydub import AudioSegment
from google.cloud import speech

logger = logging.getLogger(__name__)

class SpeechToTextService:
    """
    A service to transcribe audio files using Google Cloud Speech-to-Text.
    """

    def __init__(self, credentials_path=None):
        """
        Initializes the Speech-to-Text client.
        Args:
            credentials_path (str, optional): Path to the Google Cloud credentials JSON file.
                                              If None, relies on Application Default Credentials (ADC).
        """
        if credentials_path:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path
        
        try:
            self.client = speech.SpeechClient()
            logger.info("Google Speech-to-Text client initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize Google Speech-to-Text client: {e}", exc_info=True)
            self.client = None

    def transcribe_audio_file(self, file_path: str, mime_type: str) -> str:
        """
        Reads an audio file from a local path, converts it, and transcribes it.
        """
        if not self.client:
            logger.error("Speech-to-Text client is not available. Cannot transcribe.")
            return ""

        try:
            # 1. Convert audio to WAV format
            audio = AudioSegment.from_file(file_path)
            wav_path = file_path + ".wav"
            audio.export(wav_path, format="wav", parameters=["-ar", "16000", "-ac", "1"])
            logger.info(f"Audio converted to WAV at {wav_path}")

            # 2. Read the converted WAV file and send to API
            with open(wav_path, "rb") as audio_file:
                content = audio_file.read()

            recognition_audio = speech.RecognitionAudio(content=content)
            config = speech.RecognitionConfig(
                encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=16000,
                language_code="es-AR",
                model="default",
            )

            response = self.client.recognize(config=config, audio=recognition_audio)

            if response.results and response.results[0].alternatives:
                return response.results[0].alternatives[0].transcript
            return ""
        except Exception as e:
            logger.error(f"An unexpected error occurred during file transcription: {e}", exc_info=True)
            return ""
        finally:
            if 'wav_path' in locals() and os.path.exists(wav_path):
                os.remove(wav_path)

    def transcribe_audio_url(self, url: str, mime_type: str) -> str:
        """
        Downloads an audio file from a URL, converts it to a compatible format,
        and transcribes it using Google Speech-to-Text.

        Args:
            url (str): The URL of the audio file.
            mime_type (str): The MIME type of the audio file (e.g., 'audio/ogg').

        Returns:
            str: The transcribed text, or an empty string if transcription fails.
        """
        if not self.client:
            logger.error("Speech-to-Text client is not available. Cannot transcribe.")
            return ""

        try:
            # 1. Download the audio file from the URL
            response = requests.get(url, stream=True)
            response.raise_for_status()

            # Use a temporary file to save the downloaded audio
            with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as temp_audio_file:
                for chunk in response.iter_content(chunk_size=8192):
                    temp_audio_file.write(chunk)
                original_audio_path = temp_audio_file.name
            
            logger.info(f"Audio file downloaded from URL to {original_audio_path}")

            # 2. Convert audio to WAV format (if necessary)
            # Google Speech-to-Text has good support for many formats, but WAV is standard.
            # WhatsApp often sends 'audio/ogg' with opus codec.
            try:
                audio = AudioSegment.from_file(original_audio_path)
                # Export to WAV, 16kHz sample rate, single channel (mono) for best results
                wav_path = original_audio_path + ".wav"
                audio.export(wav_path, format="wav", parameters=["-ar", "16000", "-ac", "1"])
                logger.info(f"Audio converted to WAV at {wav_path}")
            except Exception as e:
                logger.error(f"Failed to convert audio file at {original_audio_path}: {e}")
                os.remove(original_audio_path) # Clean up original file
                return ""

            # 3. Read the converted WAV file and send to Speech-to-Text API
            with open(wav_path, "rb") as audio_file:
                content = audio_file.read()

            recognition_audio = speech.RecognitionAudio(content=content)
            
            # Configure the recognition request
            config = speech.RecognitionConfig(
                encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=16000,
                language_code="es-AR",  # Use a broad Spanish language code
                model="default", # Or specify other models like "telephony" or "medical" if applicable
            )

            logger.info("Sending audio to Google Speech-to-Text API for transcription...")
            response = self.client.recognize(config=config, audio=recognition_audio)
            logger.info("Received response from Speech-to-Text API.")

            # 4. Process the response
            if response.results and response.results[0].alternatives:
                transcript = response.results[0].alternatives[0].transcript
                logger.info(f"Transcription successful. Transcript: '{transcript}'")
                return transcript
            else:
                logger.warning("Speech-to-Text API returned no transcription.")
                return ""

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to download audio from URL {url}: {e}")
            return ""
        except Exception as e:
            logger.error(f"An unexpected error occurred during transcription: {e}", exc_info=True)
            return ""
        finally:
            # 5. Clean up temporary files
            if 'original_audio_path' in locals() and os.path.exists(original_audio_path):
                os.remove(original_audio_path)
            if 'wav_path' in locals() and os.path.exists(wav_path):
                os.remove(wav_path)
            logger.info("Temporary audio files cleaned up.")

if __name__ == '__main__':
    # This is for local testing of the service.
    # To run this, you need:
    # 1. A valid Google Cloud service account JSON file.
    # 2. The `pydub`, `requests`, and `google-cloud-speech` libraries installed.
    # 3. An audio file URL to test with.
    
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')

    # IMPORTANT: Replace with your actual credentials file path for local testing.
    # In a real app, this should be handled by environment variables or a config service.
    CREDENTIALS_PATH = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    
    if not CREDENTIALS_PATH:
        logger.warning("GOOGLE_APPLICATION_CREDENTIALS environment variable not set. Local test might fail.")
        # Create a dummy service to avoid crashing if run without creds
        stt_service = SpeechToTextService()
    else:
        stt_service = SpeechToTextService(credentials_path=CREDENTIALS_PATH)

    # Example URL (replace with a real audio URL for a proper test)
    # This is a dummy URL and will fail the download step.
    TEST_URL = "http://www.example.com/audio.ogg"
    TEST_MIME_TYPE = "audio/ogg"

    logger.info(f"--- Running Test Transcription ---")
    if stt_service.client:
        # Since the URL is fake, we expect a download error.
        transcript = stt_service.transcribe_audio_url(TEST_URL, TEST_MIME_TYPE)
        if not transcript:
            logger.info("Test finished as expected (failed to download dummy URL).")
        else:
            logger.error(f"Test failed: A transcript was returned for a dummy URL: '{transcript}'")
    else:
        logger.error("Cannot run test: Speech-to-Text client failed to initialize.")
    logger.info("--- Test Finished ---")
