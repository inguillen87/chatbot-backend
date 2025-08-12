import logging
import os
import uuid
from google.cloud import texttospeech

logger = logging.getLogger(__name__)

class TextToSpeechService:
    """
    A service to synthesize text into speech using Google Cloud Text-to-Speech.
    """

    def __init__(self, credentials_path=None):
        """
        Initializes the Text-to-Speech client.
        Args:
            credentials_path (str, optional): Path to the Google Cloud credentials JSON file.
                                              If None, relies on Application Default Credentials (ADC).
        """
        if credentials_path:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path

        try:
            self.client = texttospeech.TextToSpeechClient()
            logger.info("Google Text-to-Speech client initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize Google Text-to-Speech client: {e}", exc_info=True)
            self.client = None

    def synthesize_speech(self, text: str) -> str | None:
        """
        Synthesizes text into speech and saves it as an MP3 file.

        Args:
            text (str): The text to synthesize.

        Returns:
            str: The public URL path to the generated audio file, or None if synthesis fails.
        """
        if not self.client:
            logger.error("Text-to-Speech client is not available. Cannot synthesize speech.")
            return None

        try:
            synthesis_input = texttospeech.SynthesisInput(text=text)

            # Voice selection for JuniA (female voice)
            # A good quality standard female voice for es-US.
            voice = texttospeech.VoiceSelectionParams(
                language_code="es-US",
                name="es-US-Standard-A",
                ssml_gender=texttospeech.SsmlVoiceGender.FEMALE
            )

            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3
            )

            logger.info(f"Requesting speech synthesis for text: '{text[:50]}...'")
            response = self.client.synthesize_speech(
                input=synthesis_input, voice=voice, audio_config=audio_config
            )
            logger.info("Received synthesized speech response from Google API.")

            # Generate a unique filename
            filename = f"{uuid.uuid4()}.mp3"
            output_dir = "static/audio_responses"
            # The full path to save the file
            output_path = os.path.join(output_dir, filename)

            # The public URL path for the client to access
            public_url_path = f"/{output_dir}/{filename}"

            # Ensure the output directory exists
            os.makedirs(output_dir, exist_ok=True)

            # Write the audio content to the file
            with open(output_path, "wb") as out:
                out.write(response.audio_content)
                logger.info(f"Audio content written to file: {output_path}")

            return public_url_path

        except Exception as e:
            logger.error(f"An unexpected error occurred during speech synthesis: {e}", exc_info=True)
            return None

if __name__ == '__main__':
    # This is for local testing of the service.
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')

    # IMPORTANT: Ensure GOOGLE_APPLICATION_CREDENTIALS is set in your environment for this test.
    tts_service = TextToSpeechService()

    if tts_service.client:
        logger.info("--- Running Test Speech Synthesis ---")
        test_text = "Hola, soy JuniA, tu asistente virtual. ¿En qué puedo ayudarte hoy?"
        audio_url = tts_service.synthesize_speech(test_text)

        if audio_url:
            logger.info(f"Test successful. Audio file available at: {audio_url}")
            # In a real web app, you would return this URL to the frontend.
            # You can manually open the file in the `static/audio_responses` directory to listen to it.
        else:
            logger.error("Test failed: Speech synthesis did not return a URL.")
    else:
        logger.error("Cannot run test: Text-to-Speech client failed to initialize.")
    logger.info("--- Test Finished ---")
