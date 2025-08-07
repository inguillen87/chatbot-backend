import unittest
from unittest.mock import patch, MagicMock
import os
import sys
from types import SimpleNamespace

# Add project root to sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from app import create_app
from models import db, ChatSessionContext
from services.logic import responder_chatboc
from services.municipio_responder import CONTEXTO_MUNICIPIO, ConversationState
from services.response_formatter import build_interactive_response

class TestAccessibilityAndMedia(unittest.TestCase):

    def setUp(self):
        self.app = create_app('config.TestingConfig')
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        # Use SimpleNamespace for mock objects to ensure they are JSON serializable
        self.owner_user = SimpleNamespace(id=1, rubro=SimpleNamespace(clave="municipio"), tipo_chat="municipio", datos_interpretados_archivo=None)
        self.viewer_user = SimpleNamespace(id=2)


    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('services.logic.responder_municipio')
    @patch('services.google_text_to_speech.TextToSpeechService.synthesize_speech')
    def test_audio_response_is_generated_for_audio_input(self, mock_synthesize_speech, mock_responder_municipio):
        """
        Tests if an audio response is generated when the input was audio.
        """
        # --- Setup ---
        fake_audio_url = "/static/audio/test_audio.mp3"
        mock_synthesize_speech.return_value = fake_audio_url
        mock_responder_municipio.return_value = {
            "message_body": "Esta es una respuesta de prueba.",
            "options_list": []
        }

        chat_session = ChatSessionContext(
            chat_session_id='audio_test_session',
            user_id=self.owner_user.id,
            context_data={
                'source_is_audio': True
            }
        )
        db.session.add(chat_session)
        db.session.commit()

        # --- Act ---
        response_dict = responder_chatboc(
            pregunta="test",
            owner_user=self.owner_user,
            current_user=self.viewer_user,
            rubro_obj=self.owner_user.rubro,
            chat_db_context=chat_session
        )

        # --- Assert ---
        # 1. Check that the speech synthesis was called with the correct text
        mock_synthesize_speech.assert_called_once_with("Esta es una respuesta de prueba.")

        # 2. Check that the final response dictionary includes the audio URL
        self.assertIn('audio_url', response_dict)
        self.assertEqual(response_dict['audio_url'], fake_audio_url)

        # 3. Check that the context flag was removed
        self.assertNotIn('source_is_audio', chat_session.context_data)

    def test_button_fallback_formats_options_as_text_list(self):
        """
        Tests that the response formatter creates a text list when the 'botones' key is present.
        """
        # --- Setup ---
        bot_response_with_botones = {
            "message_body": "Por favor, elige una opción:",
            "botones": [
                {"texto": "Opción 1", "action_id": "op1"},
                {"texto": "Opción 2", "action_id": "op2"}
            ],
            "message_type": "text" # This would be set by the webhook logic
        }

        # --- Act ---
        formatted_payload = build_interactive_response(
            options=[], # options_list would be empty
            body_text=bot_response_with_botones["message_body"],
            channel="whatsapp",
            message_type=bot_response_with_botones["message_type"],
            original_bot_response=bot_response_with_botones
        )

        # --- Assert ---
        self.assertEqual(formatted_payload['type'], 'text')
        expected_body = (
            "Por favor, elige una opción:\n\n"
            "*1*. Opción 1\n"
            "*2*. Opción 2\n\n"
            "Responde con el número de la opción que necesites."
        )
        self.assertEqual(formatted_payload['text']['body'], expected_body)

    @patch('services.municipio_responder.llamar_gemini')
    def test_finalizar_tramite_action_resets_context(self, mock_llamar_gemini):
        """
        Tests if the 'finalizar_tramite' action correctly resets the conversation context.
        """
        # --- Setup ---
        mock_llamar_gemini.return_value = {
            "respuesta_usuario": "De nada. ¡Hasta luego!",
            "accion_backend": "finalizar_tramite",
            "datos_estructura": {"target": "municipio"},
            "pedir_info": None,
            "botones": []
        }

        chat_session = ChatSessionContext(
            chat_session_id='context_reset_test_session',
            user_id=self.owner_user.id,
            context_data={
                CONTEXTO_MUNICIPIO: {
                    'estado_conversacion': ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name,
                    'datos_parciales_llm_reclamo': {'categoria': 'Arreglo de calle', 'descripcion': 'Bache'},
                    'historial_llm_reclamo': [{'pregunta': '...', 'respuesta': '...'}]
                }
            }
        )
        db.session.add(chat_session)
        db.session.commit()

        # --- Act ---
        from services.municipio_responder import responder_municipio
        responder_municipio(
            pregunta_original="gracias, chau",
            owner_user=self.owner_user,
            rubro_obj=self.owner_user.rubro,
            viewer_user=self.viewer_user,
            chat_db_context=chat_session
        )

        # --- Assert ---
        final_context = chat_session.context_data.get(CONTEXTO_MUNICIPIO, {})

        # Check that claim-specific data is cleared
        self.assertEqual(final_context.get('datos_parciales_llm_reclamo'), {})
        self.assertEqual(final_context.get('historial_llm_reclamo'), [])
        self.assertNotIn('esperando_info_llm_reclamo', final_context)

        # Check that the state is now general conversation
        self.assertEqual(final_context.get('estado_conversacion'), ConversationState.CONVERSACION_GENERAL_LLM.name)

    @patch('services.logic.responder_municipio')
    @patch('services.interpretacion_imagen_service.interpretar_imagen_para_chat')
    @patch('requests.get')
    def test_media_and_location_data_is_passed_to_handler(self, mock_requests_get, mock_interpretar_imagen, mock_responder_municipio):
        """
        Tests that location and interpreted image data are correctly passed to the final handler.
        """
        # --- Setup ---
        # Mock the download of the image
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.content = b'fake_image_bytes'
        mock_requests_get.return_value = mock_response

        # Simulate that the image interpreter returns some data
        mock_interpretar_imagen.return_value = {'texto_extraido': 'Imagen de un bache'}
        mock_responder_municipio.return_value = {"message_body": "OK"}

        location_data = {"latitude": "-33.123", "longitude": "-68.456"}
        image_data = {"url": "http://example.com/bache.jpg", "mime_type": "image/jpeg", "source": "whatsapp"}

        chat_session = ChatSessionContext(chat_session_id='media_test_session', user_id=self.owner_user.id)
        db.session.add(chat_session)
        db.session.commit()

        # --- Act ---
        responder_chatboc(
            pregunta="miren esto",
            owner_user=self.owner_user,
            current_user=self.viewer_user,
            rubro_obj=self.owner_user.rubro,
            chat_db_context=chat_session,
            # Kwargs that would come from the webhook
            location_info=location_data,
            uploaded_file_info=image_data
        )

        # --- Assert ---
        # 1. Assert that the image interpreter was called correctly
        mock_interpretar_imagen.assert_called_once_with(
            archivo_adjunto=image_data,
            tipo_interpretacion="reclamo_auto_descripcion_categoria"
        )

        # 2. Assert that the final handler was called
        mock_responder_municipio.assert_called_once()

        # 3. Inspect the kwargs passed to the handler
        _, called_kwargs = mock_responder_municipio.call_args
        self.assertIn('datos_interpretados_archivo', called_kwargs)
        self.assertEqual(called_kwargs['datos_interpretados_archivo'], {'texto_extraido': 'Imagen de un bache'})


if __name__ == '__main__':
    unittest.main()
