import requests
from google.cloud import speech

def transcribe_audio_from_url(url: str) -> str | None:
    """
    Downloads an audio file from a URL and transcribes it using Google Speech-to-Text.
    """
    try:
        # Download the audio file
        audio_response = requests.get(url)
        audio_response.raise_for_status()
        audio_content = audio_response.content

        # Initialize the Speech-to-Text client
        client = speech.SpeechClient()

        # Prepare the audio and recognition config
        audio = speech.RecognitionAudio(content=audio_content)
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
            sample_rate_hertz=16000,
            language_code="es-ES",  # Spanish
        )

        # Perform the transcription
        response = client.recognize(config=config, audio=audio)

        # Return the most likely transcript
        if response.results:
            return response.results[0].alternatives[0].transcript
        else:
            return None
    except requests.exceptions.RequestException as e:
        print(f"Error downloading audio file: {e}")
        return None
    except Exception as e:
        print(f"Error during audio transcription: {e}")
        return None
