import unittest
from unittest.mock import patch, MagicMock
import os
from app import create_app, db
from config import Config
import json

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False  # Deshabilitar CSRF para pruebas de formularios si los hubiera
    TWILIO_ACCOUNT_SID = "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" # Mock SID
    TWILIO_AUTH_TOKEN = "your_auth_token" # Mock Token
    TWILIO_NUMEROS_JSON = "test_numeros_whatsapp.json" # Mock numeros mapping

class WhatsAppWebhookTestCase(unittest.TestCase):

    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        # Create a mock numeros_whatsapp.json for testing
        self.test_numeros_path = TestConfig.TWILIO_NUMEROS_JSON
        with open(self.test_numeros_path, "w") as f:
            json.dump({
                "+15551234567": {"empresa_id": 99, "nombre": "TestEmpresa", "tipo": "pyme"}
            }, f)

        # Patch the RequestValidator globally for all tests in this class
        # The validator is instantiated at module load time in whatsapp_webhook.py
        # So we need to patch where it's looked up.
        self.validator_patch = patch('routes.whatsapp_webhook.validator', MagicMock())
        self.mock_validator = self.validator_patch.start()

        # Patch the twilio_client.messages.create
        self.twilio_client_patch = patch('routes.whatsapp_webhook.twilio_client.messages.create', MagicMock())
        self.mock_twilio_create = self.twilio_client_patch.start()


    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()
        if os.path.exists(self.test_numeros_path):
            os.remove(self.test_numeros_path)
        self.validator_patch.stop()
        self.twilio_client_patch.stop()

    def test_whatsapp_webhook_valid_signature(self):
        # Arrange
        self.mock_validator.validate.return_value = True

        # Mock Twilio message sending
        mock_message = MagicMock()
        mock_message.sid = "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        self.mock_twilio_create.return_value = mock_message

        payload = {
            "To": "whatsapp:+15551234567",
            "From": "whatsapp:+15557654321",
            "Body": "Hello Test"
        }
        headers = {
            "X-Twilio-Signature": "dummy_signature_valid"
        }

        # Act
        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        # Assert
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.decode(), "OK")
        self.mock_validator.validate.assert_called_once()
        self.mock_twilio_create.assert_called_once_with(
            from_="whatsapp:+15551234567", # This comes from payload['To']
            to="whatsapp:+15557654321",     # This comes from payload['From']
            body="Eco de empresa 99: 'Hello Test'. Estado de sesión: inicio" # Based on stubbed responder_chatboc
        )


    def test_whatsapp_webhook_invalid_signature(self):
        # Arrange
        self.mock_validator.validate.return_value = False
        payload = {
            "To": "whatsapp:+15551234567",
            "From": "whatsapp:+15557654321",
            "Body": "Hello Test"
        }
        headers = {
            "X-Twilio-Signature": "dummy_signature_invalid"
        }

        # Act
        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        # Assert
        self.assertEqual(response.status_code, 403)
        self.mock_validator.validate.assert_called_once()
        self.mock_twilio_create.assert_not_called() # Message should not be sent

    def test_whatsapp_webhook_client_not_found(self):
        # Arrange
        self.mock_validator.validate.return_value = True
        payload = {
            "To": "whatsapp:+15550000000", # Number not in test_numeros_whatsapp.json
            "From": "whatsapp:+15557654321",
            "Body": "Hello Test"
        }
        headers = {
            "X-Twilio-Signature": "dummy_signature_valid"
        }

        # Act
        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        # Assert
        self.assertEqual(response.status_code, 404)
        self.assertIn("Cliente no encontrado", response.data.decode())
        self.mock_twilio_create.assert_not_called()

if __name__ == "__main__":
    unittest.main()
