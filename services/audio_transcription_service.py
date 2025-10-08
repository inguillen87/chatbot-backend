"""Utility functions for transcribing audio clips."""

import io
import os

import re
import requests
import httpx
from openai import OpenAI

from collections import OrderedDict

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


def _stt_provider_order() -> list[str]:
    raw = os.getenv("STT_PROVIDER_ORDER", "openai,cohere")
    normalized = [entry.strip().lower() for entry in raw.split(",") if entry.strip()]
    if not normalized:
        normalized = ["openai", "cohere"]
    return list(OrderedDict.fromkeys(normalized))


def _transcribe_with_openai(audio_bytes: bytes, filename: str) -> str | None:
    language = os.getenv("OPENAI_STT_LANGUAGE", "es")
    model = os.getenv("OPENAI_STT_MODEL", "whisper-1")

    with io.BytesIO(audio_bytes) as audio_file:
        audio_file.name = filename
        transcription = openai_client.audio.transcriptions.create(
            model=model,
            file=audio_file,
            language=language,
        )

    return getattr(transcription, "text", None)

def transcribe_audio_from_url(url: str, mime_type: str, account_sid: str = None, auth_token: str = None) -> str | None:
    """Download an audio file and transcribe it using OpenAI Whisper.

    Parameters
    ----------
    url:
        Direct URL to the audio file.
    mime_type:
        The MIME type of the audio file (e.g., 'audio/webm', 'audio/ogg').
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

        # Determine a safe filename with a proper extension
        extension = mime_type.split('/')[-1] if '/' in mime_type else 'audio'
        safe_extension = re.sub(r'[^a-zA-Z0-9]', '', extension)
        filename = f"audio.{safe_extension}"

        for provider in _stt_provider_order():
            if provider == "openai":
                try:
                    text = _transcribe_with_openai(audio_bytes, filename)
                except Exception as exc:  # pragma: no cover - defensive logging
                    print(f"OpenAI STT error: {exc}")
                    text = None
            elif provider == "cohere":
                try:
                    from services.cohere_stt_bridge import transcribir_audio_cohere

                    text = transcribir_audio_cohere(audio_bytes, mime_type)
                except Exception as exc:  # pragma: no cover - defensive logging
                    print(f"Cohere STT error: {exc}")
                    text = None
            else:
                text = None

            if text:
                return normalize_spanish_transcription(text)

        return None

    except requests.exceptions.RequestException as e:
        print(f"Error downloading audio file: {e}")
        return None
    except Exception as e:
        print(f"Error during audio transcription: {e}")
        return None
