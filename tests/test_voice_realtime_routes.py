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
        self.assertIn("<Redirect>", body)
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

    @patch("routes.voice_routes.TWILIO_AUTH_TOKEN", None)
    def test_twilio_voice_alias_preserves_demo_query_params(self):
        response = self.client.post(
            "/twilio/voice?tenant=club-demo-ar&vertical=juni",
            data={
                "CallSid": "CA123",
                "From": "+5492613168608",
                "To": "+18564858589",
                "Direction": "inbound",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("<Connect>", body)
        self.assertIn("club-demo-ar", body)
        self.assertIn("juni", body)
        self.assertIn("demo_hub", body)
        self.assertIn("max_call_seconds", body)

    @patch("routes.voice_routes.TWILIO_AUTH_TOKEN", None)
    def test_twilio_voice_alias_preserves_requested_tenant_for_non_demo_number(self):
        response = self.client.post(
            "/twilio/voice?tenant=junin-1&vertical=municipio&intent=reclamos",
            data={
                "CallSid": "CA456",
                "From": "+5492613168608",
                "To": "+17432643718",
                "Direction": "inbound",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("<Connect>", body)
        self.assertIn("junin-1", body)
        self.assertIn("municipio", body)
        self.assertIn("reclamos", body)
        self.assertNotIn("demo_hub", body)
        self.assertIn("intent=reclamos", body)

    @patch("routes.voice_routes.TWILIO_AUTH_TOKEN", None)
    def test_voice_fallback_keeps_phone_demo_menu_alive(self):
        response = self.client.post("/voice/fallback", data=self._twilio_payload())

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("<Gather", body)
        self.assertIn('language="es-AR"', body)
        self.assertIn("/voice/demo/process", body)
        self.assertIn('voice="Polly.Lupe-Neural"', body)
        self.assertIn('language="es-US"', body)
        self.assertIn("municipios", body)
        self.assertIn("colegios", body)
        self.assertIn("empresas", body)

    @patch("routes.voice_routes.TWILIO_AUTH_TOKEN", None)
    def test_voice_fallback_uses_municipal_menu_for_junin_number(self):
        response = self.client.post(
            "/voice/fallback?tenant=junin-1&vertical=municipio&intent=reclamos",
            data=self._twilio_payload(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("<Gather", body)
        self.assertIn("asistente telefonico del municipio", body)
        self.assertIn("iniciar reclamo", body)
        self.assertIn("consultar estado", body)
        self.assertIn("intent=reclamos", body)

    @patch("routes.voice_routes.TWILIO_AUTH_TOKEN", None)
    def test_voice_demo_process_routes_municipal_reclamo_intent(self):
        response = self.client.post(
            "/voice/demo/process?tenant=junin-1&vertical=municipio&intent=reclamos",
            data={**self._twilio_payload(), "Digits": "1"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("reclamo municipal", body)
        self.assertIn("direccion", body)

    @patch("routes.voice_routes.TWILIO_AUTH_TOKEN", None)
    def test_voice_demo_process_routes_business_order_intent(self):
        response = self.client.post(
            "/voice/demo/process",
            data={**self._twilio_payload(), "SpeechResult": "quiero hacer un pedido", "Confidence": "0.9"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Demo empresas", body)
        self.assertIn("tomar un pedido", body)


if __name__ == "__main__":
    unittest.main()
