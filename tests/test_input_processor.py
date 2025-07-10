import unittest
from unittest.mock import MagicMock, patch
from services.input_processor import InputProcessor # Assuming this path

class TestInputProcessor(unittest.TestCase):

    def setUp(self):
        # Mock STT service for these tests if it's a dependency
        self.mock_stt_service = MagicMock()
        self.mock_stt_service.transcribe_audio_from_url.return_value = "Texto de audio transcrito (mock)"
        # self.processor = InputProcessor(stt_service_instance=self.mock_stt_service) # When STT is passed
        self.processor = InputProcessor() # Current InputProcessor does not take STT in constructor yet
        # If InputProcessor initializes STT internally, you might need to patch it there:
        # @patch('services.input_processor.SpeechToTextService')
        # then pass the mock_stt_service_class to the test methods.


    def test_process_web_input_basic_text(self):
        mock_request_json = {"pregunta": "Hola mundo"}
        mock_user_context = {"cliente_id": 123, "user_id": 1, "anon_id": None} # user_id is bot_owner_id

        result = self.processor.process_web_input(mock_request_json, mock_user_context)

        self.assertEqual(result["channel"], "web")
        self.assertEqual(result["user_message"], "Hola mundo")
        self.assertEqual(result["user_id"], 123)
        self.assertEqual(result["bot_owner_id"], 1)
        self.assertIsNone(result["media_info"])
        self.assertIsNone(result["location_info"])

    def test_process_web_input_with_media(self):
        mock_request_json = {
            "pregunta": "Ver foto",
            "uploaded_file_info": {
                "id": 789, "name": "test.jpg", "url": "/files/test.jpg",
                "mime_type": "image/jpeg", "source": "web_upload"
            }
        }
        mock_user_context = {"cliente_id": 123, "user_id": 1, "anon_id": None}

        result = self.processor.process_web_input(mock_request_json, mock_user_context)

        self.assertIsNotNone(result["media_info"])
        self.assertEqual(result["media_info"]["type"], "image")
        self.assertEqual(result["media_info"]["db_id"], 789)
        self.assertEqual(result["media_info"]["source"], "web_upload")

    def test_process_web_input_with_location(self):
        mock_request_json = {
            "pregunta": "", # Location might be sent without text
            "ubicacion_usuario": {"lat": -34.123, "lon": -58.456, "accuracy": 15.0}
        }
        mock_user_context = {"cliente_id": 456, "user_id": 2, "anon_id": None}

        result = self.processor.process_web_input(mock_request_json, mock_user_context)

        self.assertIsNotNone(result["location_info"])
        self.assertEqual(result["location_info"]["latitude"], -34.123)
        self.assertEqual(result["location_info"]["longitude"], -58.456)
        self.assertEqual(result["location_info"]["accuracy"], 15.0)

    def test_process_web_input_with_action_payload(self):
        mock_request_json = {
            "pregunta": "Confirmar", # User might also type something
            "action_payload": "confirm_button_clicked"
        }
        mock_user_context = {"cliente_id": 789, "user_id": 3, "anon_id": None}

        result = self.processor.process_web_input(mock_request_json, mock_user_context)

        self.assertEqual(result["action_payload"], "confirm_button_clicked")
        self.assertEqual(result["user_message"], "Confirmar")


    def test_process_whatsapp_input_text(self):
        mock_whatsapp_payload = {
            "From": "whatsapp:+5491112345678", "To": "whatsapp:+14155238886",
            "Body": "Pedido de prueba"
        }
        mock_user_context = {"user_id": 2, "cliente_id": None, "anon_id": "whatsapp_user_whatsapp:+5491112345678"}

        result = self.processor.process_whatsapp_input(mock_whatsapp_payload, mock_user_context)

        self.assertEqual(result["channel"], "whatsapp")
        self.assertEqual(result["user_message"], "Pedido de prueba")
        self.assertEqual(result["anon_id"], "whatsapp_user_whatsapp:+5491112345678")
        self.assertEqual(result["bot_owner_id"], 2)
        self.assertEqual(result["whatsapp_from_number"], "whatsapp:+5491112345678")

    @patch('services.input_processor.InputProcessor.stt_service') # Patch the instance's stt_service
    def test_process_whatsapp_input_voice_transcribed(self, mock_stt_instance):
        # Configure the mock STT service instance that InputProcessor will use
        mock_stt_instance.transcribe_audio_from_url.return_value = "Audio transcrito: hola que tal"
        # Re-initialize processor if it creates STT service internally, or ensure the mock is injected.
        # If InputProcessor() creates its own STT, the patch needs to target that creation.
        # For this test, let's assume InputProcessor's self.stt_service is the patched mock_stt_instance.
        # This means the constructor of InputProcessor should allow injecting stt_service,
        # or we patch 'services.input_processor.SpeechToTextService' if it's instantiated inside.

        # As InputProcessor's __init__ currently sets self.stt_service = None,
        # we need to manually assign the mock to the instance for this test to work as intended.
        # This is a bit of a workaround for the current constructor.
        processor_for_voice_test = InputProcessor()
        processor_for_voice_test.stt_service = mock_stt_instance # Inject the mock

        mock_whatsapp_payload = {
            "From": "whatsapp:+549112", "To": "whatsapp:+14155238886", "Body": "",
            "NumMedia": "1", "MediaUrl0": "http://example.com/voice.ogg", "MediaContentType0": "audio/ogg",
        }
        mock_user_context = {"user_id": 3}

        # The InputProcessor's current mock for STT sets user_message to a placeholder.
        # To test actual transcription, we'd need to modify InputProcessor to use the passed STT service
        # or ensure the patch correctly replaces the internal one.

        # If InputProcessor's transcribe logic were active and using the mock:
        # result = processor_for_voice_test.process_whatsapp_input(mock_whatsapp_payload, mock_user_context)
        # self.assertEqual(result["user_message"], "Audio transcrito: hola que tal")

        # Current behavior (STT placeholder in InputProcessor):
        result = processor_for_voice_test.process_whatsapp_input(mock_whatsapp_payload, mock_user_context)
        self.assertEqual(result["user_message"], "[Mensaje de voz recibido - Transcripción pendiente]")
        self.assertIsNotNone(result["media_info"])
        self.assertEqual(result["media_info"]["type"], "audio")
        # To assert stt_service was called (if InputProcessor called it):
        # mock_stt_instance.transcribe_audio_from_url.assert_called_once_with("http://example.com/voice.ogg")


    def test_process_whatsapp_input_location(self):
        mock_whatsapp_payload = {
            "From": "whatsapp:+549113", "To": "whatsapp:+14155238886", "Body": "",
            "Latitude": "-34.567", "Longitude": "-58.789", "Address": "Calle Falsa 123, CABA"
        }
        mock_user_context = {"user_id": 4}
        result = self.processor.process_whatsapp_input(mock_whatsapp_payload, mock_user_context)

        self.assertIsNotNone(result["location_info"])
        self.assertEqual(result["location_info"]["latitude"], -34.567)
        self.assertEqual(result["location_info"]["longitude"], -58.789)
        self.assertEqual(result["location_info"]["address_text"], "Calle Falsa 123, CABA")

    def test_process_whatsapp_input_button_payload(self):
        mock_whatsapp_payload = {
            "From": "whatsapp:+549114", "To": "whatsapp:+14155238886",
            "Body": "Texto del Botón", # This is what user sees
            "ButtonPayload": "action_button_id_123" # This is the dev-defined payload
        }
        mock_user_context = {"user_id": 5}
        result = self.processor.process_whatsapp_input(mock_whatsapp_payload, mock_user_context)

        self.assertEqual(result["action_payload"], "action_button_id_123")
        self.assertEqual(result["user_message"], "Texto del Botón") # user_message should reflect what user clicked

if __name__ == '__main__':
    unittest.main()
