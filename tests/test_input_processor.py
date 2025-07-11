import unittest
from unittest.mock import MagicMock, patch
from services.input_processor import InputProcessor # Assuming this path

class TestInputProcessor(unittest.TestCase):

    def setUp(self):
        # Mock STT service for these tests if it's a dependency
        self.mock_stt_service = MagicMock()
        self.mock_stt_service.transcribe_audio_from_url.return_value = "Texto de audio transcrito (mock)"
        # self.processor = InputProcessor(stt_service_instance=self.mock_stt_service) # Old
        self.processor = InputProcessor(speech_to_text_service=self.mock_stt_service) # Inject mock STT
        # If InputProcessor initializes STT internally, you might need to patch it there:
        # @patch('services.input_processor.SpeechToTextService')
        # then pass the mock_stt_service_class to the test methods.


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
        # The InputProcessor currently does not explicitly handle 'action_payload'.
        # This test might need adjustment based on whether 'action_payload' should be part of its output.
        # For now, it will be ignored by process_input.
        mock_payload = {
            "pregunta": "Confirmar",
            "action_payload": "confirm_button_clicked" # This key is not used by current process_input
        }

        text_input, media_info, location_info = self.processor.process_input(mock_payload, "web")

        self.assertEqual(text_input, "Confirmar")
        # self.assertEqual(result["action_payload"], "confirm_button_clicked") # No longer returned directly


    def test_process_whatsapp_input_text(self):
        mock_whatsapp_payload = {
            "From": "whatsapp:+5491112345678", "To": "whatsapp:+14155238886",
            "Body": "Pedido de prueba" # 'Body' is used as 'pregunta' by WhatsApp handler typically
        }
        # Simulate how the payload might look after initial transformation by a webhook handler
        transformed_payload = {"pregunta": mock_whatsapp_payload["Body"], **mock_whatsapp_payload}

        text_input, media_info, location_info = self.processor.process_input(transformed_payload, "whatsapp")

        self.assertEqual(text_input, "Pedido de prueba")
        self.assertEqual(media_info, {})
        self.assertEqual(location_info, {})
        # Other assertions like whatsapp_from_number would be part of higher-level processing

    # No longer patching the attribute directly on the class. STT service is injected.
    def test_process_whatsapp_input_voice_transcribed(self):
        # self.mock_stt_service is already injected into self.processor in setUp
        self.mock_stt_service.transcribe_audio_url.return_value = "Audio transcrito: hola que tal"

        mock_whatsapp_payload = {
            "From": "whatsapp:+549112", "To": "whatsapp:+14155238886", "Body": "Audio adjunto.", # User might type this
            "NumMedia": "1", "MediaUrl0": "http://example.com/voice.ogg", "MediaContentType0": "audio/ogg",
        }
        transformed_payload = {"pregunta": mock_whatsapp_payload["Body"], **mock_whatsapp_payload}

        text_input, media_info, location_info = self.processor.process_input(transformed_payload, "whatsapp")

        self.assertEqual(text_input, "Audio adjunto. Audio transcrito: hola que tal")
        self.assertIsNotNone(media_info)
        self.assertEqual(media_info["mime_type"], "audio/ogg")
        self.assertEqual(media_info["transcribed_text"], "Audio transcrito: hola que tal")
        self.mock_stt_service.transcribe_audio_url.assert_called_once_with("http://example.com/voice.ogg", "audio/ogg")


    def test_process_whatsapp_input_location(self):
        mock_whatsapp_payload = {
            "From": "whatsapp:+549113", "To": "whatsapp:+14155238886", "Body": "",
            # WhatsApp location data is typically in these fields in the Twilio payload
            "Latitude": "-34.567", "Longitude": "-58.789",
            # "Address" field from WhatsApp is usually the venue name/address if it's a Venue message, not raw geocoding.
            # For raw location, it's just Lat/Lon.
        }
        # Simulate payload structure that process_input expects for location
        transformed_payload = {"ubicacion_usuario": {"lat": mock_whatsapp_payload["Latitude"], "lon": mock_whatsapp_payload["Longitude"]}, **mock_whatsapp_payload}

        text_input, media_info, location_info = self.processor.process_input(transformed_payload, "whatsapp")

        self.assertEqual(text_input, "") # Body was empty
        self.assertEqual(media_info, {})
        self.assertIsNotNone(location_info)
        self.assertEqual(location_info["lat"], -34.567)
        self.assertEqual(location_info["lon"], -58.789)
        # self.assertEqual(location_info["address_text"], "Calle Falsa 123, CABA") # Not provided this way

    def test_process_whatsapp_input_button_payload(self):
        # WhatsApp button clicks often send 'Body' as the button text and 'ButtonPayload'
        mock_whatsapp_payload = {
            "From": "whatsapp:+549114", "To": "whatsapp:+14155238886",
            "Body": "Texto del Botón",
            "ButtonPayload": "action_button_id_123"
        }
        # InputProcessor's current `process_input` doesn't explicitly handle ButtonPayload.
        # It would typically be handled by the calling layer (Orchestrator or specific WhatsApp handler)
        # which would then decide if 'Body' or 'ButtonPayload' is the primary user message/action.
        # For this test, let's assume 'Body' is treated as 'pregunta'.
        transformed_payload = {"pregunta": mock_whatsapp_payload["Body"], **mock_whatsapp_payload}

        text_input, media_info, location_info = self.processor.process_input(transformed_payload, "whatsapp")

        self.assertEqual(text_input, "Texto del Botón")
        # self.assertEqual(result["action_payload"], "action_button_id_123") # Not part of process_input's direct output

if __name__ == '__main__':
    unittest.main()
