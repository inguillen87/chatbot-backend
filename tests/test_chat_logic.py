import unittest
from unittest.mock import patch, MagicMock
from flask import jsonify
from app import create_app, db
from models import User

class ChatLogicTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update({
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"
        })
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

    def test_authenticated_user_location(self):
        """
        Prueba que la ubicación de un usuario autenticado se usa automáticamente.
        """
        self.user.latitud = -34.6037
        self.user.longitud = -58.3816
        db.session.commit()

        with self.client:
            with patch('models.User.query') as mock_query:
                mock_query.filter_by.return_value.first.return_value = self.user
                with patch('routes.chat.responder_chatboc') as mock_responder:
                    mock_responder.return_value = {'respuesta': 'Ubicación recibida'}
                    response = self.client.post(
                        '/ask',
                        json={'pregunta': 'Necesito un taxi', 'tipo_chat': 'municipio'},
                        headers={'Authorization': f'Bearer {self.user.token}'}
                    )

                    self.assertEqual(response.status_code, 200)
                    # Get the call arguments from the mock
                    args, kwargs = mock_responder.call_args
                    # Assert that the location is in the kwargs
                    self.assertIn('location', kwargs)
                    self.assertEqual(kwargs['location'], {'lat': -34.6037, 'lon': -58.3816})

    def test_anonymous_user_location_request(self):
        """
        Prueba que se solicita la ubicación a un usuario anónimo cuando es necesario.
        """
        with self.client:
            with patch('routes.chat._procesar_chat') as mock_procesar_chat:
                mock_procesar_chat.return_value = (jsonify({'respuesta': 'Necesito tu ubicación', 'solicitar_ubicacion': True}), 200)
                response = self.client.post(
                    '/ask',
                    json={'pregunta': 'Necesito un taxi', 'tipo_chat': 'municipio'},
                    headers={'X-Anon-Id': 'test-anon-id'}
                )

                self.assertEqual(response.status_code, 200)
                json_data = response.get_json()
                self.assertTrue(json_data.get('solicitar_ubicacion'))

    def test_anonymous_user_creation(self):
        """
        Prueba que se crea un usuario para un usuario anónimo que proporciona sus datos.
        """
        with self.client:
            with patch('services.pymes.get_or_create_pyme_user_by_token') as mock_get_or_create:
                mock_get_or_create.return_value = self.user
                response = self.client.post(
                    '/auth/chatuserregisterpanel',
                    json={
                        'empresa_token': 'some_token',
                        'name': 'Anonymous User',
                        'email': 'anon@example.com'
                    }
                )

                self.assertEqual(response.status_code, 201)
                new_user = User.query.filter_by(email='anon@example.com').first()
                self.assertIsNotNone(new_user)
                self.assertEqual(new_user.rol, 'lead')

    @patch('routes.whatsapp_webhook.responder_chatboc')
    def test_whatsapp_user_creation(self, mock_responder):
        """
        Prueba que se crea un usuario para un usuario de WhatsApp.
        """
        mock_responder.return_value = {'respuesta': 'Hola'}
        with self.client:
            with patch('routes.whatsapp_webhook.validator') as mock_validator:
                mock_validator.validate.return_value = True
                with patch('models.WhatsappNumero.query') as mock_query:
                    mock_whatsapp_numero = MagicMock()
                    mock_whatsapp_numero.user = self.user
                    mock_query.options.return_value.filter_by.return_value.first.return_value = mock_whatsapp_numero
                    response = self.client.post(
                        '/webhook/whatsapp',
                        data={
                            'From': 'whatsapp:+1234567890',
                            'To': 'whatsapp:+0987654321',
                            'Body': 'Hola'
                        }
                    )
                    self.assertEqual(response.status_code, 200)
                    new_user = User.query.filter_by(telefono='+1234567890').first()
                    self.assertIsNotNone(new_user)
                    self.assertEqual(new_user.rol, 'usuario')

    @patch('services.municipios.llamar_gemini')
    @patch('services.herramientas_municipio.tts_service.synthesize_speech')
    def test_audio_response_flow(self, mock_synthesize_speech, mock_llamar_gemini):
        """
        Tests the full flow when the LLM decides to respond with audio.
        """
        # 1. Mock LLM response to use the audio tool
        mock_llm_response = {
            "respuesta_usuario": "Aquí está la información que pediste.",
            "accion_backend": "ejecutar_herramienta",
            "datos_estructura": {
                "nombre_herramienta": "generar_respuesta_audio",
                "parametros_herramienta": {
                    "texto_para_audio": "Aquí está la información que pediste."
                }
            }
        }
        mock_llamar_gemini.return_value = mock_llm_response

        # 2. Mock TTS service to return a fake URL
        mock_synthesize_speech.return_value = "/static/audio/test_audio.mp3"

        # 3. Call the main logic function (responder_chatboc)
        from services.logic import responder_chatboc
        from models import ChatSessionContext

        # Create a dummy context object for the test
        chat_context = ChatSessionContext(chat_session_id="test-session", context_data={'source_is_audio': True})

        response = responder_chatboc(
            pregunta="Quiero saber sobre los impuestos",
            owner_user=self.user,
            chat_db_context=chat_context,
            channel="web"
        )

        # 4. Assertions
        # Verify that the TTS service was called
        mock_synthesize_speech.assert_called_once_with(text="Aquí está la información que pediste.")

        # Verify that the final response contains the audio_url
        self.assertIn("audio_url", response)
        self.assertEqual(response["audio_url"], "/static/audio/test_audio.mp3")

        # Verify that the text response is also present
        self.assertEqual(response["message_body"], "Aquí está la información que pediste.")


if __name__ == '__main__':
    unittest.main()
