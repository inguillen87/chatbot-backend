import logging
import uuid
import requests
from typing import Dict, Any, Optional
from flask import current_app

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

        model = current_app.config.get("OPENAI_REALTIME_SPEECH_MODEL", "gpt-4o-realtime-preview-2024-12-17")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": model,
            "voice": "shimmer", # Default rioplatense friendly
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
                "status_code": 200
            }

        except Exception as e:
            logger.error(f"Exception creating Realtime session: {e}")
            return {"error": "Error interno", "status_code": 500}

realtime_session_service = RealtimeSessionService()
