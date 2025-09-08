import unittest
import jwt
import os

os.environ.setdefault("CORS_ALLOWED_ORIGINS", "https://example.com")

try:
    from app import create_app
    from models import db, User
    from config import TestingConfig
except Exception:
    create_app = None


@unittest.skipIf(create_app is None, "Flask not available")
class WidgetTokenEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(TestingConfig)
        cls.ctx = cls.app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.user = User(
            name="Demo",
            email="demo@example.com",
            password_hash="hash",
            token="owner-token",
            rol="admin",
            tipo_chat="municipio",
        )
        db.session.add(cls.user)
        db.session.commit()
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def test_widget_token_returns_jwt(self):
        origin = "https://example.com"
        resp = self.client.post(
            "/auth/widget-token",
            headers={"Authorization": self.user.token, "Origin": origin},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("token", data)
        decoded = jwt.decode(
            data["token"], self.app.config["SECRET_KEY"], algorithms=["HS256"]
        )
        self.assertEqual(decoded["user_id"], self.user.id)
        self.assertIn(
            resp.headers.get("Access-Control-Allow-Origin"),
            {"*", origin},
        )

    def test_widget_token_preflight(self):
        origin = "https://example.com"
        resp = self.client.options(
            "/auth/widget-token", headers={"Origin": origin}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            resp.headers.get("Access-Control-Allow-Origin"),
            {"*", origin},
        )

    def test_widget_token_preflight_trailing_slash(self):
        origin = "https://example.com"
        resp = self.client.options(
            "/auth/widget-token/", headers={"Origin": origin}
        )
        self.assertEqual(resp.status_code, 200)

        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "https://example.com")

    def test_widget_token_preflight_trailing_slash(self):
        resp = self.client.options(
            "/auth/widget-token/", headers={"Origin": "https://example.com"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.headers.get("Access-Control-Allow-Origin"), "https://example.com"
        )

    def test_widget_token_post_trailing_slash(self):
        resp = self.client.post(
            "/auth/widget-token/",
            headers={"Authorization": self.user.token, "Origin": "https://example.com"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("token", resp.get_json())
        self.assertEqual(
            resp.headers.get("Access-Control-Allow-Origin"), "https://example.com"
        )


if __name__ == "__main__":
    unittest.main()
