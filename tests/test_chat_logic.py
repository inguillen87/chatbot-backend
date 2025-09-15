import unittest
from unittest.mock import patch, MagicMock
from flask import jsonify
from app import create_app, db
from models import User, ChatSessionContext
from types import SimpleNamespace
from services.logic import responder_chatboc

class ChatLogicTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app('config.TestingConfig')
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        # Crear un usuario de prueba
        self.user = User(
            email='test@example.com',
            name='Test User',
            rol='admin',
            tipo_chat='pyme'
        )
        self.user.set_password('password')
        db.session.add(self.user)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('routes.chat.responder_chatboc')
    def test_authenticated_user_no_info_request(self, mock_responder):
        """
        Prueba que el chatbot no solicita información a un usuario autenticado.
        """
        mock_responder.return_value = {'respuesta': 'Hola'}
        with self.client:
            response = self.client.post(
                '/ask',
                json={'pregunta': 'Necesito ayuda', 'tipo_chat': 'pyme'},
                headers={'Authorization': f'Bearer {self.user.token}'}
            )

            self.assertEqual(response.status_code, 200)
            json_data = response.get_json()
            self.assertNotIn('pedir_info', json_data)
            self.assertIn('respuesta', json_data)

    @patch('routes.chat.responder_chatboc')
    def test_anonymous_user_info_request(self, mock_responder):
        """
        Prueba que el chatbot solicita información a un usuario anónimo.
        """
        mock_responder.return_value = {'respuesta': 'Hola', 'pedir_info': 'nombre'}
        with self.client:
            response = self.client.post(
                '/ask',
                json={'pregunta': 'Hola', 'tipo_chat': 'municipio'},
                headers={'X-Anon-Id': 'test-anon-id'}
            )

            self.assertEqual(response.status_code, 200)
            json_data = response.get_json()
            self.assertIn('pedir_info', json_data)

    @patch('services.logic.responder_municipio')
    @patch('services.tts_orchestrator.generar_audio')
    def test_audio_response_is_generated_for_audio_input(self, mock_generar_audio, mock_responder_municipio):
        """
        Tests if an audio response is generated when the input was audio.
        """
        # --- Setup ---
        fake_audio_url = "/static/audio/test_audio.mp3"
        mock_generar_audio.return_value = fake_audio_url
        mock_responder_municipio.return_value = {
            "message_body": "Esta es una respuesta de prueba.",
            "options_list": [],
            "generar_audio": True,
        }

        owner_user = SimpleNamespace(id=1, rubro=SimpleNamespace(clave="municipio"), tipo_chat="municipio")
        viewer_user = SimpleNamespace(id=2, prefers_audio=True)

        chat_session = ChatSessionContext(
            chat_session_id='audio_test_session',
            user_id=owner_user.id,
            context_data={
                'source_is_audio': True
            }
        )
        db.session.add(chat_session)
        db.session.commit()

        # --- Act ---
        response_dict = responder_chatboc(
            pregunta="test",
            owner_user=owner_user,
            current_user=viewer_user,
            rubro_obj=owner_user.rubro,
            chat_db_context=chat_session
        )

        # --- Assert ---
        mock_generar_audio.assert_called_once_with("Esta es una respuesta de prueba.")
        self.assertIn('audio_url', response_dict)
        self.assertEqual(response_dict['audio_url'], fake_audio_url)
        self.assertNotIn('source_is_audio', chat_session.context_data)

    @patch('routes.chat.responder_chatboc')
    @patch('socket_service.socketio.emit')
    def test_web_chat_uses_socketio(self, mock_emit, mock_responder):
        """Ensure web channel emits responses via Socket.IO without errors."""
        mock_responder.return_value = {"message_body": "Hola"}

        response = self.client.post(
            '/ask/municipio',
            json={'pregunta': 'Hola'}
        )

        self.assertEqual(response.status_code, 200)
        mock_emit.assert_called_once()

    @patch('services.municipio_responder.handle_llm_interaction')
    def test_redundant_url_is_removed_from_message_body(self, mock_handle_llm_interaction):
        """
        Tests that a URL in the message body is removed if it's also in a button.
        """
        # --- Setup ---
        test_url = "https://example.com/turno"
        # Simulate the response from the LLM interaction handler
        mock_handle_llm_interaction.return_value = (
            {
                "message_body": f"Para solicitar un turno, por favor ingresá al siguiente enlace: {test_url}. ¿Necesitás algo más?",
                "options_list": [
                    {"texto": "Ir a Turnos Online", "url": test_url}
                ],
                "fuente": "llm_respuesta_directa"
            },
            {} # Empty context dict
        )

        owner_user = SimpleNamespace(id=1, rubro=SimpleNamespace(clave="municipio"), tipo_chat="municipio")
        viewer_user = SimpleNamespace(id=2)
        chat_session = ChatSessionContext(
            chat_session_id='url_removal_test_session',
            user_id=owner_user.id,
            context_data={}
        )
        db.session.add(chat_session)
        db.session.commit()

        # --- Act ---
        # We call responder_chatboc, which will internally call our mocked responder_municipio
        from services.municipio_responder import responder_municipio
        response_dict = responder_municipio(
            pregunta_original="Solicitar turno",
            owner_user=owner_user,
            rubro_obj=owner_user.rubro,
            viewer_user=viewer_user,
            chat_db_context=chat_session,
            # We need to simulate the feature flag being on
            **{'USAR_LLM_PARA_RECLAMOS': True}
        )

        # --- Assert ---
        # The URL should be gone from the final message body
        self.assertNotIn(test_url, response_dict.get('message_body', ''))
        # The text around it should also be cleaned up
        self.assertNotIn("ingresá al siguiente enlace", response_dict.get('message_body', ''))
        self.assertIn("Para solicitar un turno", response_dict.get('message_body', ''))
        # Ensure the button is still there
        self.assertIn('options_list', response_dict)
        self.assertEqual(len(response_dict['options_list']), 1)
        self.assertEqual(response_dict['options_list'][0]['url'], test_url)


if __name__ == '__main__':
    unittest.main()
