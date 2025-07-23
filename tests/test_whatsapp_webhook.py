import unittest
from unittest.mock import patch
from app import create_app, db
from config import TestConfig
from models import User, Rubro

class WhatsAppWebhookTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        with self.app.app_context():
            db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
        self.app_context.pop()

    @patch('routes.whatsapp_webhook.validar_whatsapp_request', return_value=True)
    @patch('routes.whatsapp_webhook.procesar_mensaje_whatsapp.delay')
    def test_whatsapp_webhook_valid_request(self, mock_delay, mock_validar):
        response = self.client.post('/webhook/whatsapp', data={'Body': 'test', 'From': 'whatsapp:+1234567890'})
        self.assertEqual(response.status_code, 200)
        mock_delay.assert_called_once()

    @patch('routes.whatsapp_webhook.validar_whatsapp_request', return_value=False)
    def test_whatsapp_webhook_invalid_signature(self, mock_validar):
        response = self.client.post('/webhook/whatsapp', data={'Body': 'test', 'From': 'whatsapp:+1234567890'})
        self.assertEqual(response.status_code, 403)
