"""Utility functions for transcribing audio clips."""

import io
import os

import re
import requests
import httpx
from openai import OpenAI

# Initialize a shared OpenAI client once at import time so it can be mocked in tests.
# If no API key is configured, fall back to a dummy key so unit tests can run without
# external credentials. Use a custom HTTP client that ignores proxy env vars (common
# in CI) to avoid initialization errors.
http_client = httpx.Client(proxy=None, trust_env=False)
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", "test"), http_client=http_client)


def normalize_spanish_transcription(text: str) -> str:
    """Expand common abbreviations and regionalisms for clearer understanding.

    This lightly normalizes transcriptions so downstream prompts can rely on
    full words.  It currently focuses on a few frequent shortcuts used in
    Spanish chats and voice notes.
    """

    replacements = {
        "xq": "porque",
        "pq": "porque",
        "dnd": "donde",
        "sr": "señor",
        "sra": "señora",
        "uds": "ustedes",
    }
    for short, full in replacements.items():
        text = re.sub(rf"\b{short}\b", full, text, flags=re.IGNORECASE)
    return text

def transcribe_audio_from_url(url: str, account_sid: str = None, auth_token: str = None) -> str | None:
    """Download an audio file and transcribe it using OpenAI Whisper.

    Parameters
    ----------
    url:
        Direct URL to the audio file.
    account_sid:
        Optional Twilio Account SID for authentication.
    auth_token:
        Optional Twilio Auth Token for authentication.

    Returns
    -------
    str | None
        The transcribed text if successful, otherwise ``None``.
    """

    try:
        # Download the audio file, using auth only if provided
        auth = (account_sid, auth_token) if account_sid and auth_token else None
        audio_response = requests.get(url, auth=auth)
        audio_response.raise_for_status()

        audio_bytes = audio_response.content

        # Send the audio to OpenAI's transcription endpoint
        with io.BytesIO(audio_bytes) as audio_file:
            # give the BytesIO object a name so the client knows the mimetype
            audio_file.name = "audio.ogg"
            transcription = openai_client.audio.transcriptions.create(
                model="gpt-4o-transcribe", file=audio_file
            )

        text = getattr(transcription, "text", None)
        if text:
            text = normalize_spanish_transcription(text)
        return text or None

    except requests.exceptions.RequestException as e:
        print(f"Error downloading audio file: {e}")
        return None
    except Exception as e:
        print(f"Error during audio transcription: {e}")
        return None
