import unittest
from unittest.mock import patch

from flask import Flask

from routes.voice_routes import voice_bp


class VoiceRealtimeRoutesTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["TESTING"] = True
        self.app.config["BACKEND_URL"] = "https://api.chatboc.test"
        self.app.config["TWILIO_FALLBACK_SAY_LANGUAGE"] = "es-US"
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
            "/twilio/voice?tenant=chatboc-demo&vertical=ventas&intent=sales",
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
        self.assertIn("chatboc-platform", body)
        self.assertIn("ventas", body)
        self.assertIn("sales", body)
        self.assertIn("demo_hub", body)
        self.assertIn("max_call_seconds", body)

    @patch("routes.voice_routes.TWILIO_AUTH_TOKEN", None)
    def test_twilio_voice_alias_preserves_requested_tenant_for_non_demo_number(self):
        response = self.client.post(
            "/twilio/voice?tenant=junin&vertical=municipio&intent=reclamos",
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
        self.assertIn("junin", body)
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
        self.assertNotIn('language="es-US"', body)
        self.assertIn("municipios", body)
        self.assertIn("colegios", body)
        self.assertIn("empresas", body)

    @patch("routes.voice_routes.TWILIO_AUTH_TOKEN", None)
    def test_voice_fallback_uses_municipal_menu_for_junin_number(self):
        response = self.client.post(
            "/voice/fallback?tenant=junin&vertical=municipio&intent=reclamos",
            data=self._twilio_payload(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("<Gather", body)
        self.assertIn("asistente telefonico del municipio", body)
        self.assertIn("iniciar reclamo", body)
        self.assertIn("consultar estado", body)
        self.assertIn("/voice/process", body)
        self.assertNotIn("/voice/demo/process", body)
        self.assertIn("intent=reclamos", body)

    @patch("routes.voice_routes.TWILIO_AUTH_TOKEN", None)
    def test_voice_fallback_canonicalizes_legacy_junin_alias(self):
        response = self.client.post(
            "/voice/fallback?tenant=junin-1&vertical=juni&intent=reclamos",
            data=self._twilio_payload(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("tenant=junin-1", body)
        self.assertIn("vertical=municipio", body)
        self.assertNotIn("vertical=juni", body)
        self.assertIn("/voice/process", body)

    @patch("routes.voice_routes.TWILIO_AUTH_TOKEN", None)
    def test_voice_fallback_canonicalizes_old_club_demo_juni_alias_to_junin(self):
        response = self.client.post(
            "/voice/fallback?tenant=club-demo-ar&vertical=juni",
            data=self._twilio_payload(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("tenant=junin-1", body)
        self.assertIn("vertical=municipio", body)
        self.assertNotIn("club-demo-ar", body)
        self.assertIn("/voice/process", body)

    @patch("routes.voice_routes.TWILIO_AUTH_TOKEN", None)
    def test_voice_fallback_canonicalizes_chatboc_platform_alias(self):
        response = self.client.post(
            "/voice/fallback?tenant=chatboc-platform&vertical=sales&intent=sales",
            data=self._twilio_payload(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("tenant=chatboc-platform", body)
        self.assertIn("vertical=ventas", body)
        self.assertNotIn("chatboc-demo", body)
        self.assertIn("/voice/process", body)

    @patch("routes.voice_routes.TWILIO_AUTH_TOKEN", None)
    def test_voice_demo_process_routes_municipal_reclamo_intent(self):
        response = self.client.post(
            "/voice/demo/process?tenant=junin&vertical=municipio&intent=reclamos",
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

    @patch.dict("os.environ", {}, clear=True)
    @patch("routes.voice_routes.TWILIO_AUTH_TOKEN", None)
    def test_voice_webhook_fails_closed_without_auth_token_outside_testing(self):
        app = Flask("voice-production-guard")
        app.config.update(
            TESTING=False,
            ENV="production",
            BACKEND_URL="https://api.chatboc.test",
        )
        app.register_blueprint(voice_bp)

        response = app.test_client().post("/twilio/voice", data=self._twilio_payload())

        self.assertEqual(response.status_code, 403)

    @patch.dict("os.environ", {}, clear=True)
    @patch("routes.voice_routes.TWILIO_AUTH_TOKEN", None)
    def test_voice_transfer_fails_closed_without_twilio_signature(self):
        app = Flask("voice-transfer-production-guard")
        app.config.update(TESTING=False, ENV="production")
        app.register_blueprint(voice_bp)

        response = app.test_client().post(
            "/twilio/voice/transfer?target=%2B5492610000000",
            data=self._twilio_payload(),
        )

        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
