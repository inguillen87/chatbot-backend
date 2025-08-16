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
from models import User, Rubro, WhatsappNumero
# Moved model imports after app and config to ensure they are found via sys.path
# and to avoid potential issues if models.py itself tries to import app-context related things early.
# However, for direct use in tests, they are typically at the top. Let's try keeping them here.
import models
# JSON import is no longer needed
# import json

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:' # Use in-memory SQLite for tests
    WTF_CSRF_ENABLED = False
    TWILIO_ACCOUNT_SID = "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxx_test" # Mock SID
    TWILIO_AUTH_TOKEN = "your_auth_token_test" # Mock Token
    # TWILIO_NUMEROS_JSON is no longer used

class WhatsAppWebhookTestCase(unittest.TestCase):

    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all() # Create all tables, including whatsapp_numero
        self.client = self.app.test_client()

        # Test data
        self.test_whatsapp_number_str = "+15551234567"
        self.test_user_number_str = "+15557654321"

        # Create a mock User (company/municipality)
        self.mock_client_user = User(
            name="TestEmpresa",
            email="testempresa@example.com",
            rol="empresa", # or 'municipio'
            tipo_chat="pyme", # or 'municipio'
            pyme_id=1,
            nombre_empresa="TestEmpresaName"
        )
        self.mock_client_user.set_password("testpassword")
        db.session.add(self.mock_client_user)
        db.session.commit() # Commit to get an ID for mock_client_user

        self.empresa_id_for_test = self.mock_client_user.id
        self.client_name_for_test = self.mock_client_user.nombre_empresa
        self.client_type_for_test = self.mock_client_user.tipo_chat

        # Create a mock WhatsappNumero mapping
        self.mock_whatsapp_mapping = WhatsappNumero(
            numero_whatsapp=self.test_whatsapp_number_str,
            user_id=self.mock_client_user.id,
            is_active=True
        )
        db.session.add(self.mock_whatsapp_mapping)
        db.session.commit()

        # Patch the RequestValidator (globally for all tests in this class)
        self.validator_patch = patch('routes.whatsapp_webhook.validator', MagicMock())
        self.mock_validator = self.validator_patch.start()

        # Patch the twilio_client
        self.twilio_client_patch = patch('routes.whatsapp_webhook.twilio_client', MagicMock())
        self.mock_twilio_client = self.twilio_client_patch.start()
        self.mock_twilio_create = self.mock_twilio_client.messages.create

        self.welcome_patch = patch('routes.whatsapp_webhook.enviar_bienvenida_whatsapp', MagicMock())
        self.mock_welcome = self.welcome_patch.start()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()
        self.validator_patch.stop()
        self.twilio_client_patch.stop()
        self.welcome_patch.stop()

    def test_whatsapp_webhook_valid_request(self):
        # Arrange
        self.mock_validator.validate.return_value = True

        mock_twilio_message = MagicMock()
        mock_twilio_message.sid = "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxx_test_sid"
        self.mock_twilio_create.return_value = mock_twilio_message

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "Hello Test"
        }
        headers = { "X-Twilio-Signature": "dummy_signature_valid" }

        # Act
        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        # Assert
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.decode(), "OK")
        self.mock_validator.validate.assert_called_once()

        # The bot should now derive to human, so we don't expect an echo.
        # We just check that the webhook returns OK and that the bot was called.
        # The response to the user is handled by the bot logic, which is mocked here.
        # In a real scenario, the bot would send a message like "Connecting you to an agent..."
        # and the test for that would be in the bot logic tests, not the webhook test.
        self.mock_twilio_create.assert_called_once()
        self.mock_welcome.assert_called_once_with(
            self.test_user_number_str,
            "Usuario de WhatsApp 4321"
        )
    def test_whatsapp_webhook_uses_message_to_user_when_missing_message_body(self):
        """Ensure webhook falls back to 'message_to_user' when 'message_body' is absent."""
        self.mock_validator.validate.return_value = True

        mock_twilio_message = MagicMock()
        mock_twilio_message.sid = "SM_message_to_user_test"
        self.mock_twilio_create.return_value = mock_twilio_message

        with patch('routes.whatsapp_webhook.responder_chatboc') as mock_bot:
            mock_bot.return_value = {
                'success': False,
                'message_to_user': '¿Sobre qué trámite necesitas información?',
                'pedir_info': 'nombre_tramite'
            }

            payload = {
                "To": f"whatsapp:{self.test_whatsapp_number_str}",
                "From": f"whatsapp:{self.test_user_number_str}",
                "Body": "tramites"
            }
            headers = {"X-Twilio-Signature": "dummy_signature_valid"}

            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

            self.assertEqual(response.status_code, 200)
            self.mock_twilio_create.assert_called_once_with(
                from_=f"whatsapp:{self.test_whatsapp_number_str}",
                to=f"whatsapp:{self.test_user_number_str}",
                body="¿Sobre qué trámite necesitas información?"
            )

    def test_whatsapp_webhook_invalid_signature(self):
        # Arrange
        self.mock_validator.validate.return_value = False # Simulate invalid signature
        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "Hello Test"
        }
        headers = { "X-Twilio-Signature": "dummy_signature_invalid" }

        # Act
        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        # Assert
        self.assertEqual(response.status_code, 403) # Expect Forbidden
        self.mock_validator.validate.assert_called_once()
        self.mock_twilio_create.assert_not_called() # Message should not be sent
        self.mock_welcome.assert_not_called()

    def test_whatsapp_webhook_number_not_found_in_db(self):
        # Arrange
        self.mock_validator.validate.return_value = True
        unknown_twilio_number = "+15550000000" # A number not in our mock DB
        payload = {
            "To": f"whatsapp:{unknown_twilio_number}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "Hello Test"
        }
        headers = { "X-Twilio-Signature": "dummy_signature_valid" }

        # Act
        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        # Assert
        self.assertEqual(response.status_code, 404) # Expect Not Found
        self.assertIn("WhatsApp number not configured", response.data.decode())
        self.mock_twilio_create.assert_not_called()
        self.mock_welcome.assert_not_called()

    def test_whatsapp_webhook_number_inactive_in_db(self):
        # Arrange
        self.mock_validator.validate.return_value = True

        # Deactivate the existing mapping
        inactive_mapping = WhatsappNumero.query.filter_by(numero_whatsapp=self.test_whatsapp_number_str).first()
        if inactive_mapping:
            inactive_mapping.is_active = False
            db.session.add(inactive_mapping)
            db.session.commit()

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}", # Number is now inactive
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "Hello Test"
        }
        headers = { "X-Twilio-Signature": "dummy_signature_valid" }

        # Act
        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        # Assert
        self.assertEqual(response.status_code, 404) # Expect Not Found (as if not configured)
        self.assertIn("WhatsApp number not configured", response.data.decode())
        self.mock_twilio_create.assert_not_called()
        self.mock_welcome.assert_not_called()

    @patch('routes.whatsapp_webhook.requests.get')
    def test_whatsapp_webhook_pdf_attachment(self, mock_requests_get):
        # Mock the download response
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.content = b'fake-pdf-content'
        mock_requests_get.return_value = mock_response

        self.mock_validator.validate.return_value = True

        mock_twilio_message = MagicMock()
        mock_twilio_message.sid = "SM_pdf_test"
        self.mock_twilio_create.return_value = mock_twilio_message

        with patch('routes.whatsapp_webhook.responder_chatboc') as mock_bot, \
             patch('routes.whatsapp_webhook.upload_to_gcs') as mock_upload_gcs:
            mock_bot.return_value = {"message_body": "Ok"}
            mock_upload_gcs.return_value = {
                'public_url': 'http://gcs.example.com/test.pdf',
                'unique_name': 'unique_test.pdf',
                'original_name': 'test.pdf',
                'mimetype': 'application/pdf',
                'size': len(b'fake-pdf-content')
            }

            payload = {
                "To": f"whatsapp:{self.test_whatsapp_number_str}",
                "From": f"whatsapp:{self.test_user_number_str}",
                "Body": "Archivo",
                "MediaUrl0": "http://example.com/test.pdf",
                "MediaContentType0": "application/pdf"
            }
            headers = {"X-Twilio-Signature": "dummy_signature_valid"}

            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data.decode(), "OK")

            mock_bot.assert_called_once()
            _, kwargs = mock_bot.call_args
            self.assertIn("uploaded_file_info", kwargs)
            self.assertEqual(kwargs["uploaded_file_info"]["mime_type"], "application/pdf")

            self.mock_twilio_create.assert_called_once_with(
                from_=f"whatsapp:{self.test_whatsapp_number_str}",
                to=f"whatsapp:{self.test_user_number_str}",
                body="Ok"
            )
            self.mock_welcome.assert_called_once()

    @patch('routes.whatsapp_webhook.requests.get')
    def test_whatsapp_webhook_docx_attachment(self, mock_requests_get):
        # Mock the download response
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.content = b'fake-docx-content'
        mock_requests_get.return_value = mock_response

        self.mock_validator.validate.return_value = True

        mock_twilio_message = MagicMock()
        mock_twilio_message.sid = "SM_docx_test"
        self.mock_twilio_create.return_value = mock_twilio_message

        with patch('routes.whatsapp_webhook.responder_chatboc') as mock_bot, \
             patch('routes.whatsapp_webhook.upload_to_gcs') as mock_upload_gcs:
            mock_bot.return_value = {"message_body": "Ok"}
            mock_upload_gcs.return_value = {
                'public_url': 'http://gcs.example.com/test.docx',
                'unique_name': 'unique_test.docx',
                'original_name': 'test.docx',
                'mimetype': "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                'size': len(b'fake-docx-content')
            }

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
            self.assertEqual(response.data.decode(), "OK")

            mock_bot.assert_called_once()
            _, kwargs = mock_bot.call_args
            self.assertIn("uploaded_file_info", kwargs)
            self.assertEqual(kwargs["uploaded_file_info"]["mime_type"], "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

            self.mock_twilio_create.assert_called_once_with(
                from_=f"whatsapp:{self.test_whatsapp_number_str}",
                to=f"whatsapp:{self.test_user_number_str}",
                body="Ok"
            )
            self.mock_welcome.assert_called_once()

    @patch('routes.whatsapp_webhook.requests.get')
    def test_whatsapp_webhook_image_attachment(self, mock_requests_get):
        # Mock the download response
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.content = b'fake-image-content'
        mock_requests_get.return_value = mock_response

        self.mock_validator.validate.return_value = True

        mock_twilio_message = MagicMock()
        mock_twilio_message.sid = "SM_image_test"
        self.mock_twilio_create.return_value = mock_twilio_message

        with patch('routes.whatsapp_webhook.responder_chatboc') as mock_bot, \
             patch('routes.whatsapp_webhook.upload_to_gcs') as mock_upload_gcs:
            mock_bot.return_value = {"message_body": "Ok"}
            mock_upload_gcs.return_value = {
                'public_url': 'http://gcs.example.com/test.jpg',
                'unique_name': 'unique_test.jpg',
                'original_name': 'test.jpg',
                'mimetype': 'image/jpeg',
                'size': len(b'fake-image-content')
            }

            payload = {
                "To": f"whatsapp:{self.test_whatsapp_number_str}",
                "From": f"whatsapp:{self.test_user_number_str}",
                "Body": "Archivo",
                "MediaUrl0": "http://example.com/test.jpg",
                "MediaContentType0": "image/jpeg"
            }
            headers = {"X-Twilio-Signature": "dummy_signature_valid"}

            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data.decode(), "OK")

            mock_bot.assert_called_once()
            _, kwargs = mock_bot.call_args
            self.assertIn("uploaded_file_info", kwargs)
            self.assertEqual(kwargs["uploaded_file_info"]["mime_type"], "image/jpeg")

            self.mock_twilio_create.assert_called_once_with(
                from_=f"whatsapp:{self.test_whatsapp_number_str}",
                to=f"whatsapp:{self.test_user_number_str}",
                body="Ok"
            )
            self.mock_welcome.assert_called_once()

if __name__ == "__main__":
    unittest.main()
