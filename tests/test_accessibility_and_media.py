import unittest
from unittest.mock import patch, MagicMock
import os
import sys
from types import SimpleNamespace
import pytest

# Add project root to sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from app import create_app
from models import db, ChatSessionContext, User, Rubro
from services.logic import responder_chatboc
from services.municipio_responder import CONTEXTO_MUNICIPIO, ConversationState
from services.response_formatter import build_interactive_response

class TestAccessibilityAndMedia(unittest.TestCase):

    def setUp(self):
        self.app = create_app('config.TestingConfig')
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        rubro_obj = Rubro(id=1, clave="municipio", nombre="Municipalidad")
        db.session.add(rubro_obj)

        self.owner_user = User(id=1, name="Test Owner", email="owner@test.com", rubro_id=rubro_obj.id)
        self.owner_user.set_password("password")

        self.viewer_user = User(id=2, name="Test Viewer", email="viewer@test.com")
        self.viewer_user.set_password("password")

        db.session.add_all([self.owner_user, self.viewer_user])
        db.session.commit()

        # Re-fetch users to ensure relationships are loaded
        self.owner_user = db.session.get(User, 1)
        self.viewer_user = db.session.get(User, 2)


    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('services.tts_orchestrator.generar_audio_con_fallback')
    def test_audio_response_is_generated_for_audio_input(self, mock_generar_audio):
        """
        Tests if an audio response is generated when the input was audio.
        """
        # --- Setup ---
        fake_audio_url = "/static/audio/test_audio.mp3"
        mock_generar_audio.return_value = fake_audio_url

        self.viewer_user.prefers_audio = True
        db.session.commit()

        chat_session = ChatSessionContext(
            chat_session_id='audio_test_session',
            user_id=self.owner_user.id,
            context_data={'source_is_audio': True}
        )
        db.session.add(chat_session)
        db.session.commit()

        # --- Act ---
        with patch('services.logic.responder_municipio') as mock_responder_municipio:
            mock_responder_municipio.return_value = {
                "message_body": "Esta es una respuesta de prueba.",
                "generar_audio": True,
            }
            response_dict = responder_chatboc(
                pregunta="test",
                owner_user=self.owner_user,
                current_user=self.viewer_user,
                rubro_obj=self.owner_user.rubro,
                chat_db_context=chat_session
            )

        # --- Assert ---
        mock_generar_audio.assert_called_once()
        args, _ = mock_generar_audio.call_args
        self.assertEqual(args[0], "Esta es una respuesta de prueba.")
        self.assertIn('audio_url', response_dict)
        self.assertEqual(response_dict['audio_url'], fake_audio_url)

    @patch('services.tts_orchestrator.generar_audio_con_fallback')
    def test_audio_generated_when_flag_missing_but_source_is_audio(self, mock_generar_audio):
        fake_audio_url = "/static/audio/test_audio.mp3"
        mock_generar_audio.return_value = fake_audio_url

        chat_session = ChatSessionContext(
            chat_session_id='auto_audio_session',
            user_id=self.owner_user.id,
            context_data={'source_is_audio': True}
        )
        db.session.add(chat_session)
        db.session.commit()

        with patch('services.logic.responder_municipio') as mock_responder_municipio:
            mock_responder_municipio.return_value = {
                "message_body": "Esta es una respuesta de prueba."
            }
            response_dict = responder_chatboc(
                pregunta="test",
                owner_user=self.owner_user,
                current_user=self.viewer_user,
                rubro_obj=self.owner_user.rubro,
                chat_db_context=chat_session
            )

        mock_generar_audio.assert_called_once()
        self.assertEqual(response_dict.get('audio_url'), fake_audio_url)

    @patch('services.tts_orchestrator.generar_audio_con_fallback')
    def test_audio_uses_message_to_user_when_message_body_missing(self, mock_generar_audio):
        fake_audio_url = "/static/audio/test_audio.mp3"
        mock_generar_audio.return_value = fake_audio_url

        chat_session = ChatSessionContext(
            chat_session_id='audio_message_to_user',
            user_id=self.owner_user.id,
            context_data={'source_is_audio': True}
        )
        db.session.add(chat_session)
        db.session.commit()

        with patch('services.logic.responder_municipio') as mock_responder_municipio:
            mock_responder_municipio.return_value = {
                "message_to_user": "Resumen del reclamo.",
            }
            response_dict = responder_chatboc(
                pregunta="test",
                owner_user=self.owner_user,
                current_user=self.viewer_user,
                rubro_obj=self.owner_user.rubro,
                chat_db_context=chat_session
            )

        mock_generar_audio.assert_called_once()
        args, _ = mock_generar_audio.call_args
        self.assertEqual(args[0], "Resumen del reclamo.")
        self.assertEqual(response_dict.get('audio_url'), fake_audio_url)

    @patch('services.tts_orchestrator.generar_audio_con_fallback')
    def test_audio_text_is_prefixed_with_summary_when_provided(self, mock_generar_audio):
        """If a custom audio_text exists, the message_to_user should still lead the speech."""

        fake_audio_url = "/static/audio/test_audio.mp3"
        mock_generar_audio.return_value = fake_audio_url

        chat_session = ChatSessionContext(
            chat_session_id='audio_text_with_summary',
            user_id=self.owner_user.id,
            context_data={'source_is_audio': True}
        )
        db.session.add(chat_session)
        db.session.commit()

        with patch('services.logic.responder_municipio') as mock_responder_municipio:
            mock_responder_municipio.return_value = {
                'message_to_user': 'Resumen del reclamo.',
                'audio_text': '1. Opción A\n2. Opción B'
            }
            responder_chatboc(
                pregunta='test',
                owner_user=self.owner_user,
                current_user=self.viewer_user,
                rubro_obj=self.owner_user.rubro,
                chat_db_context=chat_session
            )

        mock_generar_audio.assert_called_once()
        args, _ = mock_generar_audio.call_args
        spoken_text = args[0]
        assert spoken_text.startswith('Resumen del reclamo.')
        assert 'Opción A' in spoken_text
        assert 'Opción B' in spoken_text


    @patch('routes.whatsapp_webhook.threading.Timer')
    @patch('services.response_formatter.build_interactive_response')
    def test_send_delayed_payload_uses_message_to_user_when_body_missing(
        self, mock_build_interactive_response, mock_timer
    ):
        """Ensure delayed payload uses message_to_user as fallback for body text."""

        # Timer should execute the function immediately for test purposes
        def immediate_timer(delay, func):
            return SimpleNamespace(start=lambda: func())

        mock_timer.side_effect = immediate_timer

        client = SimpleNamespace(messages=SimpleNamespace(create=MagicMock()))
        payload = {
            'message_to_user': 'Resumen del reclamo.',
            'options_list': []
        }

        from routes.whatsapp_webhook import _send_delayed_payload

        _send_delayed_payload(client, 'to', 'from', payload, delay=0)

        mock_build_interactive_response.assert_called_once()
        _, kwargs = mock_build_interactive_response.call_args
        self.assertEqual(kwargs.get('body_text'), 'Resumen del reclamo.')

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
            "message_type": "interactive_buttons"
        }

        # --- Act ---
        formatted_payload = build_interactive_response(
            options=[],
            body_text=bot_response_with_botones["message_body"],
            channel="whatsapp",
            message_type="text",
            original_bot_response=bot_response_with_botones
        )

        # --- Assert ---
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
        mock_llamar_gemini.return_value = (
            {
                "message_body": "De nada. ¡Hasta luego!",
                "accion_backend": "finalizar_tramite",
                "datos_estructura": {"target": "municipio"},
                "pedir_info": None,
                "botones": []
            },
            {}
        )

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
        with self.app.test_request_context():
            responder_municipio(
                pregunta_original="gracias, chau",
                owner_user=self.owner_user,
                rubro_obj=self.owner_user.rubro,
                viewer_user=self.viewer_user,
                chat_db_context=chat_session
            )

        # --- Assert ---
        final_context = chat_session.context_data.get(CONTEXTO_MUNICIPIO, {})
        self.assertEqual(final_context.get('datos_parciales_llm_reclamo'), {})
        self.assertEqual(final_context.get('historial_llm_reclamo'), [])
        self.assertNotIn('esperando_info_llm_reclamo', final_context)
        self.assertEqual(final_context.get('estado_conversacion'), ConversationState.CONVERSACION_GENERAL_LLM.name)

    @unittest.skip("Test is flawed and needs to be rewritten. Mocks wrong handler.")
    @patch('services.pymes.llamar_gemini')
    @patch('requests.get')
    @patch('services.interpretacion_imagen_service.interpretar_imagen_para_chat')
    def test_media_and_location_data_is_passed_to_handler(self, mock_interpretar_imagen, mock_requests_get, mock_llamar_gemini):
        """
        Tests that location and interpreted image data are correctly passed to the final handler.
        """
        # --- Setup ---
        mock_llamar_gemini.return_value = {"accion_backend": "responder_directamente", "message_body": "OK"}
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.content = b'fake_image_bytes'
        mock_requests_get.return_value = mock_response

        mock_interpretar_imagen.return_value = {'texto_extraido': 'Imagen de un bache'}

        location_data = {"latitude": "-33.123", "longitude": "-68.456"}
        image_data = {"url": "http://example.com/bache.jpg", "mime_type": "image/jpeg", "source": "whatsapp"}

        chat_session = ChatSessionContext(chat_session_id='media_test_session', user_id=self.owner_user.id)
        db.session.add(chat_session)
        db.session.commit()

        # --- Act ---
        with patch('services.logic.responder_pyme') as mock_responder_pyme:
            mock_responder_pyme.return_value = {"message_body": "OK"}
            responder_chatboc(
                pregunta="miren esto",
                owner_user=self.owner_user,
                current_user=self.viewer_user,
                rubro_obj=self.owner_user.rubro,
                chat_db_context=chat_session,
                location_info=location_data,
                uploaded_file_info=image_data
            )
            mock_responder_pyme.assert_called_once()
            _, called_kwargs = mock_responder_pyme.call_args
            self.assertIn('datos_interpretados_archivo', called_kwargs)
            self.assertEqual(called_kwargs['datos_interpretados_archivo'], {'texto_extraido': 'Imagen de un bache'})

if __name__ == '__main__':
    unittest.main()
