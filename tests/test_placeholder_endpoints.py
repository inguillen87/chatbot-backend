import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "https://example.com")

from app import create_app
from config import TestingConfig
from models import db


@unittest.skipIf(create_app is None, "Flask not available")
class PlaceholderEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(TestingConfig)
        cls.ctx = cls.app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def test_municipal_whatsapp_placeholder(self):
        resp = self.client.get("/municipal/whatsapp", headers={"Origin": "https://www.chatboc.ar"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json().get("integraciones"), [])
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "https://www.chatboc.ar")

    def test_municipal_whatsapp_preflight(self):
        resp = self.client.options("/municipal/whatsapp", headers={"Origin": "https://www.chatboc.ar"})
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "https://www.chatboc.ar")
        self.assertIn("PATCH", resp.headers.get("Access-Control-Allow-Methods", ""))

    def test_municipal_integrations_placeholder(self):
        resp = self.client.get("/municipal/integrations", headers={"Origin": "https://www.chatboc.ar"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json().get("integraciones"), [])
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "https://www.chatboc.ar")

    def test_municipal_integrations_preflight(self):
        resp = self.client.options(
            "/municipal/integrations", headers={"Origin": "https://www.chatboc.ar"}
        )
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "https://www.chatboc.ar")
        self.assertIn("PATCH", resp.headers.get("Access-Control-Allow-Methods", ""))

    def test_public_tenant_profile_preflight(self):
        resp = self.client.options(
            "/api/public/tenant-profile", headers={"Origin": "https://example.com"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True})
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "https://example.com")


if __name__ == "__main__":
    unittest.main()
