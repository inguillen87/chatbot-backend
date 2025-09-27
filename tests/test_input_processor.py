import unittest
from unittest.mock import MagicMock, patch
from services.input_processor import InputProcessor

class TestInputProcessor(unittest.TestCase):

    def setUp(self):
        # The processor will be instantiated in each test where it's needed.
        self.processor = InputProcessor()

    def test_process_web_input_basic_text(self):
        mock_payload = {"pregunta": "Hola mundo"}
        text_input, media_info, location_info = self.processor.process_input(mock_payload, "web")
        self.assertEqual(text_input, "Hola mundo")
        self.assertEqual(media_info, {})
        self.assertEqual(location_info, {})

    def test_process_web_input_with_media(self):
        mock_payload = {
            "pregunta": "Ver foto",
            "uploaded_file_info": {
                "id": 789, "name": "test.jpg", "url": "/files/test.jpg",
                "mime_type": "image/jpeg", "source": "web_upload"
            }
        }
        text_input, media_info, location_info = self.processor.process_input(mock_payload, "web")
        self.assertEqual(text_input, "Ver foto")
        self.assertIsNotNone(media_info)
        self.assertEqual(media_info.get("id"), 789)
        self.assertEqual(media_info.get("source"), "web_upload")
        self.assertEqual(location_info, {})

    def test_process_web_input_with_location(self):
        mock_payload = {
            "pregunta": "",
            "ubicacion_usuario": {"lat": -34.123, "lon": -58.456, "accuracy": 15.0}
        }
        text_input, media_info, location_info = self.processor.process_input(mock_payload, "web")
        self.assertEqual(text_input, "")
        self.assertEqual(media_info, {})
        self.assertIsNotNone(location_info)
        self.assertEqual(location_info["lat"], -34.123)
        self.assertEqual(location_info["lon"], -58.456)
        self.assertEqual(location_info["accuracy"], 15.0)

    def test_process_web_input_with_action_payload(self):
        mock_payload = {
            "pregunta": "Confirmar",
            "action_payload": "confirm_button_clicked"
        }
        text_input, media_info, location_info = self.processor.process_input(mock_payload, "web")
        self.assertEqual(text_input, "Confirmar")

    def test_process_whatsapp_input_text(self):
        mock_whatsapp_payload = {
            "From": "whatsapp:+5491112345678", "To": "whatsapp:+17432643718",
            "Body": "Pedido de prueba"
        }
        transformed_payload = {"pregunta": mock_whatsapp_payload["Body"], **mock_whatsapp_payload}
        text_input, media_info, location_info = self.processor.process_input(transformed_payload, "whatsapp")
        self.assertEqual(text_input, "Pedido de prueba")
        self.assertEqual(media_info, {})
        self.assertEqual(location_info, {})

    @patch('services.audio_transcription_service.transcribe_audio_from_url')
    def test_process_whatsapp_input_voice_transcribed(self, mock_transcribe):
        mock_transcribe.return_value = "Audio transcrito: hola que tal"
        processor = InputProcessor() # Uses the real (patched) function by default

        mock_whatsapp_payload = {
            "From": "whatsapp:+549112", "To": "whatsapp:+17432643718", "Body": "Audio adjunto.",
            "NumMedia": "1", "MediaUrl0": "http://example.com/voice.ogg", "MediaContentType0": "audio/ogg",
        }
        transformed_payload = {"pregunta": mock_whatsapp_payload["Body"], **mock_whatsapp_payload}
        text_input, media_info, location_info = processor.process_input(transformed_payload, "whatsapp")

        self.assertEqual(text_input, "Audio adjunto. Audio transcrito: hola que tal")
        self.assertIsNotNone(media_info)
        self.assertEqual(media_info["mime_type"], "audio/ogg")
        self.assertEqual(media_info["transcribed_text"], "Audio transcrito: hola que tal")
        mock_transcribe.assert_called_once_with("http://example.com/voice.ogg", "audio/ogg")

    def test_process_whatsapp_input_location(self):
        mock_whatsapp_payload = {
            "From": "whatsapp:+549113", "To": "whatsapp:+17432643718", "Body": "",
            "Latitude": "-34.567", "Longitude": "-58.789",
        }
        transformed_payload = {"pregunta": "", "ubicacion_usuario": {"lat": -34.567, "lon": -58.789}, **mock_whatsapp_payload}
        text_input, media_info, location_info = self.processor.process_input(transformed_payload, "whatsapp")
        self.assertEqual(text_input, "")
        self.assertEqual(media_info, {})
        self.assertIsNotNone(location_info)
        self.assertEqual(location_info.get("lat"), -34.567)
        self.assertEqual(location_info.get("lon"), -58.789)

    def test_process_whatsapp_input_button_payload(self):
        mock_whatsapp_payload = {
            "From": "whatsapp:+549114", "To": "whatsapp:+17432643718",
            "Body": "Texto del Botón",
            "ButtonPayload": "action_button_id_123"
        }
        transformed_payload = {"pregunta": mock_whatsapp_payload["Body"], **mock_whatsapp_payload}
        text_input, media_info, location_info = self.processor.process_input(transformed_payload, "whatsapp")
        self.assertEqual(text_input, "Texto del Botón")

if __name__ == '__main__':
    unittest.main()
