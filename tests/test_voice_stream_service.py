import json
import unittest

from services.voice_stream_service import VoiceStreamService


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


if __name__ == "__main__":
    unittest.main()
