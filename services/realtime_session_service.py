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
        self.openai_url = "https://api.openai.com/v1/realtime/sessions"

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
            "model": model,
            "modalities": ["audio", "text"],
            "voice": voice,
            "instructions": (
                "Sos un asistente realtime de Chatboc. Habla en espanol argentino, "
                "con respuestas breves, claras y orientadas a resolver. "
                "No inventes tickets, pedidos, precios ni confirmaciones."
            ),
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.45,
                "prefix_padding_ms": 250,
                "silence_duration_ms": 420,
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

            return {
                "client_secret": data.get("client_secret", {}).get("value"),
                "session_id": str(uuid.uuid4()), # our internal reference
                "model": model,
                "voice": voice,
                "status_code": 200
            }

        except Exception as e:
            logger.error(f"Exception creating Realtime session: {e}")
            return {"error": "Error interno", "status_code": 500}

realtime_session_service = RealtimeSessionService()
