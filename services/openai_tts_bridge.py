import os
import openai
import logging
import uuid

logger = logging.getLogger(__name__)

def generar_audio_openai(text: str) -> str | None:
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

    try:
        client = openai.OpenAI(api_key=api_key)

        logger.info(f"Requesting OpenAI speech synthesis for text: '{text[:50]}...'")

        response = client.audio.speech.create(
            model="tts-1",
            voice="alloy",
            input=text,
        )

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
