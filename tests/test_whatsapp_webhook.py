import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Añadir el directorio raíz del proyecto al sys.path
project_root_whatsapp = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_whatsapp not in sys.path:
    sys.path.insert(0, project_root_whatsapp)

from app import create_app, db
from config import Config
from models import User, Rubro, WhatsappNumero, ChatSessionContext
import models
import json

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    TWILIO_ACCOUNT_SID = "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxx_test"
    TWILIO_AUTH_TOKEN = "your_auth_token_test"

class WhatsAppWebhookTestCase(unittest.TestCase):

    def setUp(self):
        os.environ["TWILIO_ACCOUNT_SID"] = TestConfig.TWILIO_ACCOUNT_SID
        os.environ["TWILIO_AUTH_TOKEN"] = TestConfig.TWILIO_AUTH_TOKEN

        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        self.test_whatsapp_number_str = "+15551234567"
        self.test_user_number_str = "+15557654321"

        mock_client_user = User(
            id=1,
            name="TestEmpresa",
            email="testempresa@example.com",
            rol="empresa",
            tipo_chat="pyme",
            pyme_id=1,
            nombre_empresa="TestEmpresaName"
        )
        mock_client_user.set_password("testpassword")
        db.session.add(mock_client_user)
        db.session.commit() # Commit here to have the user available in the next session

        self.empresa_id_for_test = mock_client_user.id

        mock_whatsapp_mapping = WhatsappNumero(
            numero_whatsapp=self.test_whatsapp_number_str,
            user_id=mock_client_user.id,
            is_active=True
        )
        db.session.add(mock_whatsapp_mapping)
        db.session.commit() # Commit here as well

        self.validator_patch = patch('routes.whatsapp_webhook.validator', MagicMock())
        self.mock_validator = self.validator_patch.start()

        self.twilio_client_patch = patch('routes.whatsapp_webhook.twilio_client', MagicMock())
        self.mock_twilio_client = self.twilio_client_patch.start()
        self.mock_twilio_create = self.mock_twilio_client.messages.create

        self.welcome_patch = patch('routes.whatsapp_webhook.enviar_bienvenida_whatsapp', MagicMock())
        self.mock_welcome = self.welcome_patch.start()

    def _create_confirmed_session(self):
        # Re-fetch user to ensure it's in the current session
        owner_user = db.session.get(User, self.empresa_id_for_test)
        session_context = ChatSessionContext(
            chat_session_id=f"whatsapp_{owner_user.id}_{self.test_user_number_str}",
            user_id=owner_user.id,
            anon_id=self.test_user_number_str,
            context_data={"perfil_confirmado": True},
        )
        db.session.add(session_context)
        db.session.flush()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()
        self.validator_patch.stop()
        self.twilio_client_patch.stop()
        self.welcome_patch.stop()

    @patch('routes.whatsapp_webhook.requests.get')
    def test_whatsapp_webhook_docx_attachment(self, mock_requests_get):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.content = b'fake-docx-content'
        mock_requests_get.return_value = mock_response
        self.mock_validator.validate.return_value = True
        self.mock_twilio_create.return_value = MagicMock(sid="SM_docx_test")
        self._create_confirmed_session()

        with patch('routes.whatsapp_webhook.responder_chatboc') as mock_bot, \
             patch('routes.whatsapp_webhook.create_attachment_with_thumbnail') as mock_create_attachment, \
             patch('routes.whatsapp_webhook.clasificar_adjunto_whatsapp') as mock_classifier:

            mock_bot.return_value = {"message_body": "Ok"}
            mock_adjunto = MagicMock(id=124, url='http://example.com/test.docx', mime='application/vnd.openxmlformats-officedocument.wordprocessingml.document', nombre_original='test.docx')
            mock_create_attachment.return_value = mock_adjunto
            mock_classifier.return_value = {"categoria_sugerida": "documentacion"}

            payload = {
                "To": f"whatsapp:{self.test_whatsapp_number_str}",
                "From": f"whatsapp:{self.test_user_number_str}",
                "Body": "Archivo",
                "MediaUrl0": "http://example.com/test.docx",
                "MediaContentType0": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            }
            headers = {"X-Twilio-Signature": "dummy_signature_valid"}

            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

            self.assertEqual(response.status_code, 200)
            mock_bot.assert_called_once()
            _, kwargs = mock_bot.call_args
            self.assertIn("uploaded_file_info", kwargs)
            self.assertIn("datos_interpretados_archivo", kwargs)
            self.assertEqual(kwargs["datos_interpretados_archivo"], {"categoria_sugerida": "documentacion"})

if __name__ == "__main__":
    unittest.main()
