import unittest
from unittest.mock import patch
from app import create_app, db
from config import Config
from services.multimodal_ticket_service import multimodal_ticket_service
from schemas.ai_contracts import GatewayResponse, UsageMetrics

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"

class TestMultimodalTicketService(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('services.multimodal_ticket_service.ai_gateway.execute')
    def test_auto_create_eligible_when_confident(self, mock_execute):
        # Mock a high confidence, non-ambiguous response
        mock_execute.return_value = GatewayResponse(
            request_id="req1",
            model="gpt-4o",
            status="completed",
            structured_output={
                "category": "bache",
                "priority": "alta",
                "confidence": 0.95,
                "summary": "Bache en calle",
                "needs_human_review": False,
                "evidence_tags": []
            }
        )

        res = multimodal_ticket_service.analyze_image_for_ticket(1, "http://image.jpg")

        self.assertFalse(res["error"])
        self.assertTrue(res["draft"]["auto_create_eligible"])
        self.assertNotIn("reason", res["draft"])

    @patch('services.multimodal_ticket_service.ai_gateway.execute')
    def test_requires_human_review_flag(self, mock_execute):
        # Mock an ambiguous response
        mock_execute.return_value = GatewayResponse(
            request_id="req2",
            model="gpt-4o",
            status="completed",
            structured_output={
                "category": "bache",
                "priority": "alta",
                "confidence": 0.90,
                "summary": "Bache o sombra",
                "needs_human_review": True,
                "evidence_tags": []
            }
        )

        res = multimodal_ticket_service.analyze_image_for_ticket(1, "http://image.jpg")

        self.assertFalse(res["error"])
        self.assertFalse(res["draft"]["auto_create_eligible"])
        self.assertEqual(res["draft"]["reason"], "Requiere revisión humana debido a ambigüedad o baja confianza.")

    @patch('services.multimodal_ticket_service.ai_gateway.execute')
    def test_low_confidence_blocks_auto_create(self, mock_execute):
        # Mock a low confidence response
        mock_execute.return_value = GatewayResponse(
            request_id="req3",
            model="gpt-4o",
            status="completed",
            structured_output={
                "category": "bache",
                "priority": "alta",
                "confidence": 0.60,
                "summary": "Posible bache",
                "needs_human_review": False,
                "evidence_tags": []
            }
        )

        res = multimodal_ticket_service.analyze_image_for_ticket(1, "http://image.jpg")

        self.assertFalse(res["error"])
        self.assertFalse(res["draft"]["auto_create_eligible"])

if __name__ == "__main__":
    unittest.main()
