import os
import unittest

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}


class AuthDemoLoginTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_demo_login_returns_token_and_demo_mode(self):
        resp = self.client.post("/auth/demo", json={"rubro": "municipio"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload.get("demo_mode"))
        self.assertIn("token", payload)
        self.assertIn("tenant_slug", payload)

        decoded = jwt.decode(payload["token"], self.app.config["SECRET_KEY"], algorithms=["HS256"])
        self.assertTrue(decoded.get("demo_mode"))
        self.assertEqual(decoded.get("tenant_slug"), payload.get("tenant_slug"))

    def test_demo_login_rejects_unknown_rubro(self):
        resp = self.client.post("/auth/demo", json={"rubro": "no-existe-xyz"})
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
