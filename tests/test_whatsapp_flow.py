import os
os.environ['FLASK_ENV'] = 'testing'
os.environ['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'test.db'))
import unittest
import json
from unittest.mock import patch, MagicMock

# Since we are in the tests/ directory, we need to add the project root to the path
# to be able to import the app and other modules.
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app, db
from models import WhatsappNumero, User
from config import TestingConfig

class TestWhatsAppFlow(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestingConfig)
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            # Create a user and a whatsapp number for the bot
            owner_user = User(name="Test Owner", email="owner@test.com", tipo_chat="municipio")
            owner_user.set_password("password")
            db.session.add(owner_user)
            db.session.commit()

            whatsapp_mapping = WhatsappNumero(numero_whatsapp="17432643718", user_id=owner_user.id, is_active=True)
            db.session.add(whatsapp_mapping)
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    @patch('routes.whatsapp_webhook.twilio_client')
    def test_greeting_and_main_menu(self, mock_twilio_client):
        """Test that the bot responds to a greeting with a welcome message and a menu."""
        # Mock the create message method
        mock_create = MagicMock()
        mock_twilio_client.messages.create = mock_create

        # Simulate a WhatsApp message from a user
        response = self.client.post(
            "/webhook/whatsapp",
            data={
                "From": "whatsapp:+5492613168608",
                "To": "whatsapp:+17432643718",
                "Body": "hola",
                "ProfileName": "Marcelo",
                "SmsMessageSid": "SMxxxx",
                "NumMedia": "0",
                "SmsSid": "SMxxxx",
                "WaId": "5492613168608",
                "SmsStatus": "received",
                "MessagingServiceSid": "MGxxxx",
                "NumSegments": "1",
                "MessageSid": "SMxxxx",
                "AccountSid": "ACxxxx",
                "ApiVersion": "2010-04-01",
            },
        )

        self.assertEqual(response.status_code, 200)

        # Check that the Twilio client was called to send a message
        mock_create.assert_called()

        # Get the arguments passed to the create method
        call_args = mock_create.call_args

        # The body of the message should contain the welcome message and the menu
        sent_body = call_args.kwargs.get("body")
        self.assertIn("¡Hola, Marcelo!", sent_body)
        self.assertIn("Iniciar un Reclamo", sent_body)
        self.assertIn("Licencia de Conducir", sent_body)
        self.assertIn("Pagar Tasas", sent_body)

if __name__ == '__main__':
    unittest.main()
