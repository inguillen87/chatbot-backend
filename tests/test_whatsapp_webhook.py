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
# Moved model imports after app and config to ensure they are found via sys.path
# and to avoid potential issues if models.py itself tries to import app-context related things early.
# However, for direct use in tests, they are typically at the top. Let's try keeping them here.
from models import User, WhatsappNumero
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

        # Patch the twilio_client.messages.create
        self.twilio_client_patch = patch('routes.whatsapp_webhook.twilio_client.messages.create', MagicMock())
        self.mock_twilio_create = self.twilio_client_patch.start()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()
        self.validator_patch.stop()
        self.twilio_client_patch.stop()

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

        expected_bot_response = f"Eco de {self.client_name_for_test} (ID: {self.empresa_id_for_test}, Tipo: {self.client_type_for_test}): 'Hello Test'. Estado de sesión: inicio"
        self.mock_twilio_create.assert_called_once_with(
            from_=f"whatsapp:{self.test_whatsapp_number_str}",
            to=f"whatsapp:{self.test_user_number_str}",
            body=expected_bot_response
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
        self.assertIn("not found or inactive", response.data.decode())
        self.mock_twilio_create.assert_not_called()

if __name__ == "__main__":
    unittest.main()
