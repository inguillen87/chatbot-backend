import unittest
from unittest.mock import patch
from app import create_app, db
from config import Config
from services.realtime_session_service import realtime_session_service

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"

class TestRealtimeSessionService(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app.config["OPENAI_API_KEY"] = "mock-key"
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('services.realtime_session_service.requests.post')
    def test_create_session_success(self, mock_post):
        class MockResponse:
            status_code = 200
            text = "ok"
            def json(self):
                return {"client_secret": {"value": "ek_mock_secret"}}

        mock_post.return_value = MockResponse()

        result = realtime_session_service.create_session(tenant_id=1)
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["client_secret"], "ek_mock_secret")
        self.assertIn("session_id", result)

    @patch('services.realtime_session_service.requests.post')
    def test_create_session_failure(self, mock_post):
        class MockResponse:
            status_code = 401
            text = "Unauthorized"
            def json(self):
                return {"error": {"message": "Invalid key"}}

        mock_post.return_value = MockResponse()

        result = realtime_session_service.create_session(tenant_id=1)
        self.assertEqual(result["status_code"], 401)
        self.assertIn("error", result)

if __name__ == "__main__":
    unittest.main()
