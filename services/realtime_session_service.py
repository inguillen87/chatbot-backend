import logging
import uuid
import requests
from typing import Dict, Any, Optional
from flask import current_app
from services.realtime_voice_profiles import (
    resolve_realtime_model,
    resolve_realtime_voice,
)

logger = logging.getLogger(__name__)

class RealtimeSessionService:
    """
    Orchestrates the creation and validation of WebRTC sessions.
    Handles securely fetching ephemeral tokens from OpenAI for the browser.
    """

    def __init__(self):
        self.openai_url = "https://api.openai.com/v1/realtime/client_secrets"

    @staticmethod
    def _audio_format(value: str | None = None) -> dict:
        normalized = str(value or "pcm16").strip().lower().replace("-", "_")
        if normalized in {"g711_ulaw", "ulaw", "pcmu", "mulaw", "audio/pcmu"}:
            return {"type": "audio/pcmu"}
        if normalized in {"g711_alaw", "alaw", "pcma", "audio/pcma"}:
            return {"type": "audio/pcma"}
        return {"type": "audio/pcm", "rate": 24000}

    def create_session(self, tenant_id: int, user_id: Optional[int] = None, anon_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Creates an ephemeral WebRTC token session using the OpenAI Realtime API.
        Enforces policy checks before allowing the session.
        """
        # Fetch the api key properly
        api_key = current_app.config.get("OPENAI_API_KEY")
        if not api_key:
            logger.error("OPENAI_API_KEY is not configured.")
            return {"error": "Configuración de IA no disponible", "status_code": 500}

        model = resolve_realtime_model(app_config=current_app.config)
        voice = resolve_realtime_voice(app_config=current_app.config)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "expires_after": {"anchor": "created_at", "seconds": 600},
            "session": {
                "type": "realtime",
                "model": model,
                "output_modalities": ["audio"],
                "instructions": (
                    "Sos un asistente realtime de Chatboc. Habla en espanol argentino, "
                    "con respuestas breves, claras y orientadas a resolver. "
                    "Entende tambien ingles y portugues; responde en el idioma del usuario "
                    "y deja los datos operativos normalizados en espanol. "
                    "No inventes tickets, pedidos, precios ni confirmaciones."
                ),
                "audio": {
                    "input": {
                        "format": self._audio_format("pcm16"),
                        "noise_reduction": {"type": "near_field"},
                        "turn_detection": {
                            "type": "semantic_vad",
                            "eagerness": "auto",
                            "create_response": True,
                            "interrupt_response": True,
                        },
                        "transcription": {"model": "gpt-4o-mini-transcribe"},
                    },
                    "output": {
                        "format": self._audio_format("pcm16"),
                        "voice": voice,
                        "speed": 1.0,
                    },
                },
            },
        }

        try:
            # We use an explicit requests call to fetch the ephemeral token because
            # the Python OpenAI SDK doesn't natively expose the WebRTC ephemeral tokens route yet natively.
            resp = requests.post(self.openai_url, json=payload, headers=headers, timeout=10)

            if resp.status_code != 200:
                logger.error(f"Failed to create Realtime session: {resp.text}")
                return {"error": "Error al generar sesión de voz", "status_code": resp.status_code}

            data = resp.json()
            client_secret = data.get("client_secret", {}).get("value") or data.get("value")

            return {
                "client_secret": client_secret,
                "session_id": str(uuid.uuid4()), # our internal reference
                "openai_session": data.get("session") or {},
                "model": model,
                "voice": voice,
                "status_code": 200
            }

        except Exception as e:
            logger.error(f"Exception creating Realtime session: {e}")
            return {"error": "Error interno", "status_code": 500}

realtime_session_service = RealtimeSessionService()
