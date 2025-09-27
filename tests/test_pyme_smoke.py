import unittest
from unittest.mock import patch, MagicMock, AsyncMock

from app import create_app, db
from services.pymes import responder_pyme_v4
from config import Config

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False

class PymeSmokeTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.loop = self.app.loop

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch("services.pymes.ChatOrchestrator.execute_action", new_callable=AsyncMock)
    def test_pyme_smoke_text_to_quote_to_order(
        self, mock_execute_action
    ):
        async def run_test():
            # 1. User starts an order
            mock_execute_action.return_value = {"message_body": "OK, empecemos tu pedido."}
            response = await responder_pyme_v4(1, "comprar", "+1", "Test", {})
            self.assertIn("empecemos tu pedido", response["message_body"])
            mock_execute_action.assert_called_with('pyme_hacer_pedido', unittest.mock.ANY, unittest.mock.ANY)

            # 2. User asks for a quote (presupuesto)
            mock_execute_action.return_value = {"message_body": "Tu presupuesto es $100."}
            response = await responder_pyme_v4(1, "presupuesto", "+1", "Test", {})
            self.assertIn("Tu presupuesto es", response["message_body"])

            # 3. User confirms order
            mock_execute_action.return_value = {"message_body": "¡Gracias por tu compra!"}
            response = await responder_pyme_v4(1, "confirmar pedido", "+1", "Test", {})
            self.assertIn("¡Gracias por tu compra!", response["message_body"])

        with self.app.test_request_context():
            self.loop.run_until_complete(run_test())

    @patch("services.pymes.ChatOrchestrator.execute_action", new_callable=AsyncMock)
    def test_pyme_image_to_catalog_match(
        self, mock_execute_action
    ):
        async def run_test():
            mock_execute_action.return_value = {"message_body": "Basado en tu imagen, encontré Producto X"}
            response = await responder_pyme_v4(1, "cuanto cuesta esto?", "+1", "Test", {}, media_url="http://a.com/img.png")
            self.assertIn("Producto X", response["message_body"])

        with self.app.test_request_context():
            self.loop.run_until_complete(run_test())

    @patch("services.pymes.ChatOrchestrator.execute_action", new_callable=AsyncMock)
    def test_pyme_audio_to_intent_delivery(
        self, mock_execute_action
    ):
        async def run_test():
            # Assume transcription would result in "delivery"
            mock_execute_action.return_value = {"message_body": "Información de delivery..."}
            response = await responder_pyme_v4(1, "delivery", "+1", "Test", {}, media_url="http://a.com/audio.ogg")
            self.assertIn("Información de delivery", response["message_body"])
            mock_execute_action.assert_called_with('pyme_info_envio', unittest.mock.ANY, unittest.mock.ANY)

        with self.app.test_request_context():
            self.loop.run_until_complete(run_test())

    @patch("services.pymes.ChatOrchestrator.execute_action", new_callable=AsyncMock)
    def test_pyme_location_to_shipping_estimate(
        self, mock_execute_action
    ):
        async def run_test():
            mock_execute_action.return_value = {"message_body": "El envío a tu ubicación cuesta $150."}
            location_data = {"lat": -34.6, "lon": -58.4}
            response = await responder_pyme_v4(1, "", "+1", "Test", {}, location=location_data)
            self.assertIn("cuesta $150", response["message_body"])
            mock_execute_action.assert_called_with('pyme_info_envio', unittest.mock.ANY, unittest.mock.ANY)

        with self.app.test_request_context():
            self.loop.run_until_complete(run_test())

if __name__ == "__main__":
    unittest.main()