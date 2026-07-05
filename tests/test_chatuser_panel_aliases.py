import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "https://example.com")

from app import create_app
from config import TestingConfig
from models import db


@unittest.skipIf(create_app is None, "Flask not available")
class ChatUserPanelAliasTests(unittest.TestCase):
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

    def test_chatuser_register_alias_returns_json_error(self):
        resp = self.client.post(
            "/api/chatuserregisterpanel", headers={"Origin": "https://example.com"}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get("error"), "Falta empresa_token")

    def test_chatuser_register_alias_root_path(self):
        resp = self.client.post(
            "/chatuserregisterpanel", headers={"Origin": "https://example.com"}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get("error"), "Falta empresa_token")

    def test_chatuser_login_alias_returns_json_error(self):
        resp = self.client.post(
            "/api/chatuserloginpanel", headers={"Origin": "https://example.com"}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get("error"), "Falta empresa_token")

    def test_chatuser_login_alias_root_path(self):
        resp = self.client.post(
            "/chatuserloginpanel", headers={"Origin": "https://example.com"}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get("error"), "Falta empresa_token")

    def test_chatuser_alias_options_return_ok(self):
        for path in [
            "/api/chatuserregisterpanel",
            "/api/chatuserloginpanel",
            "/chatuserregisterpanel",
            "/chatuserloginpanel",
            "/api/google-login",
            "/api/v2/auth/google",
            "/google-login",
            "/api/google-client-id",
            "/google-client-id",
        ]:
            resp = self.client.options(path, headers={"Origin": "https://example.com"})
            self.assertEqual(resp.status_code, 200)
            payload = resp.get_json()
            self.assertEqual(payload.get("ok"), True)

    def test_google_login_aliases_require_id_token(self):
        for path in ["/api/google-login", "/api/v2/auth/google", "/google-login"]:
            resp = self.client.post(path, headers={"Origin": "https://example.com"})
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.get_json().get("error"), "id_token requerido")

    def test_google_client_id_aliases_return_payload(self):
        for path in ["/api/google-client-id", "/google-client-id"]:
            resp = self.client.get(path, headers={"Origin": "https://example.com"})
            self.assertEqual(resp.status_code, 200)
            self.assertIn("client_id", resp.get_json())


if __name__ == "__main__":
    unittest.main()
