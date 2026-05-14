import unittest
from unittest.mock import patch

from flask import Flask

from routes.voice_routes import voice_bp


class VoiceRealtimeRoutesTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["BACKEND_URL"] = "https://api.chatboc.test"
        self.app.register_blueprint(voice_bp)
        self.client = self.app.test_client()

    def _twilio_payload(self):
        return {
            "CallSid": "CA123",
            "From": "+15415550100",
            "To": "+17432643718",
            "Direction": "inbound",
        }

    @patch("routes.voice_routes.TWILIO_AUTH_TOKEN", None)
    @patch("routes.voice_routes.generar_audio")
    def test_voice_welcome_defaults_to_realtime_stream_without_tts(self, mock_generar_audio):
        response = self.client.post("/voice/welcome", data=self._twilio_payload())

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("<Connect>", body)
        self.assertIn("wss://api.chatboc.test/twilio/voice/stream", body)
        self.assertIn("from_number", body)
        mock_generar_audio.assert_not_called()

    @patch("routes.voice_routes.TWILIO_AUTH_TOKEN", None)
    def test_twilio_voice_inbound_uses_realtime_stream(self):
        response = self.client.post(
            "/twilio/voice/inbound?chat_session_id=session-123",
            data=self._twilio_payload(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("<Connect>", body)
        self.assertIn("wss://api.chatboc.test/twilio/voice/stream", body)
        self.assertIn("session-123", body)


if __name__ == "__main__":
    unittest.main()
