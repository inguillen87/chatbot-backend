import json
import unittest
from unittest.mock import patch

from services.voice_stream_service import VoiceStreamService, _openai_realtime_headers


class _FakeSocket:
    def __init__(self):
        self.messages = []

    def send(self, payload):
        self.messages.append(json.loads(payload))


class VoiceStreamServiceMessageTests(unittest.TestCase):
    def test_audio_delta_forwards_media_to_twilio(self):
        for event_type in ("response.audio.delta", "response.output_audio.delta"):
            with self.subTest(event_type=event_type):
                twilio_ws = _FakeSocket()
                service = VoiceStreamService(twilio_ws)
                service.stream_sid = "stream-1"

                service.handle_openai_message({"type": event_type, "delta": "abc"})

                self.assertEqual(
                    twilio_ws.messages,
                    [{"event": "media", "streamSid": "stream-1", "media": {"payload": "abc"}}],
                )
                self.assertTrue(service.response_active)

    def test_barge_in_clears_twilio_and_cancels_openai_response(self):
        twilio_ws = _FakeSocket()
        openai_ws = _FakeSocket()
        service = VoiceStreamService(twilio_ws)
        service.stream_sid = "stream-2"
        service.openai_ws = openai_ws
        service.response_active = True
        service.response_id = "resp-1"

        service.handle_openai_message({"type": "input_audio_buffer.speech_started"})

        self.assertEqual(twilio_ws.messages, [{"event": "clear", "streamSid": "stream-2"}])
        self.assertEqual(openai_ws.messages, [{"type": "response.cancel", "response_id": "resp-1"}])
        self.assertFalse(service.response_active)
        self.assertTrue(service.cancel_pending)

    def test_demo_greeting_offers_requested_vertical_menu(self):
        service = VoiceStreamService(_FakeSocket())
        service.demo_hub = "chatboc"
        service.requested_vertical = "juni"

        greeting = service._build_chatboc_demo_greeting("Chatboc.ar Demo Hub", "Marcelo")

        self.assertIn("Hola Marcelo", greeting)
        self.assertIn("demo telefonica de municipios", greeting)
        self.assertIn("reclamo", greeting)

    def test_openai_realtime_headers_do_not_send_beta_by_default(self):
        with patch.dict("os.environ", {}, clear=False):
            headers = _openai_realtime_headers()

        self.assertIn("Authorization", headers)
        self.assertNotIn("OpenAI-Beta", headers)

    def test_openai_realtime_headers_allow_explicit_beta_override(self):
        with patch.dict(
            "os.environ",
            {"OPENAI_REALTIME_ALLOW_BETA_HEADER": "true", "OPENAI_REALTIME_BETA_HEADER": "realtime=test"},
            clear=False,
        ):
            headers = _openai_realtime_headers()

        self.assertEqual(headers["OpenAI-Beta"], "realtime=test")

    def test_initial_voice_greeting_for_municipio_has_menu(self):
        service = VoiceStreamService(_FakeSocket())
        service.requested_vertical = "municipio"
        service.voice_vertical = "municipio"

        greeting = service._build_initial_voice_greeting("Municipalidad de Junin", "Marcelo")

        self.assertIn("Hola Marcelo", greeting)
        self.assertIn("Municipalidad de Junin", greeting)
        self.assertIn("reclamo", greeting.lower())
        self.assertIn("tramites", greeting.lower())


if __name__ == "__main__":
    unittest.main()
