import requests
from google.cloud import speech

def transcribe_audio_from_url(url: str, account_sid: str, auth_token: str) -> dict | None:
    """
    Downloads an audio file from a URL and transcribes it using Google Speech-to-Text.
    Returns a dictionary with 'transcript' and 'confidence'.
    """
    try:
        audio_response = requests.get(url, auth=(account_sid, auth_token))
        audio_response.raise_for_status()
        audio_content = audio_response.content

        client = speech.SpeechClient()

        audio = speech.RecognitionAudio(content=audio_content)
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
            sample_rate_hertz=16000,
            language_code="es-ES",
            enable_automatic_punctuation=True,
        )

        response = client.recognize(config=config, audio=audio)

        if response.results and response.results[0].alternatives:
            best_alternative = response.results[0].alternatives[0]
            return {
                "transcript": best_alternative.transcript,
                "confidence": best_alternative.confidence
            }
        else:
            return None
    except requests.exceptions.RequestException as e:
        print(f"Error downloading audio file: {e}")
        return None
    except Exception as e:
        print(f"Error during audio transcription: {e}")
        return None
