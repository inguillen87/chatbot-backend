import unittest
import jwt

try:
    from app import create_app
    from models import db, User
except Exception:
    create_app = None


@unittest.skipIf(create_app is None, "Flask not available")
class WidgetTokenEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.user = User(
            name="Demo",
            email="demo@example.com",
            password_hash="hash",
            token="owner-token",
            rol="admin",
            tipo_chat="municipio",
        )
        db.session.add(self.user)
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_widget_token_returns_jwt(self):
        resp = self.client.post(
            "/auth/widget-token",
            headers={"Authorization": self.user.token, "Origin": "https://example.com"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("token", data)
        decoded = jwt.decode(
            data["token"], self.app.config["SECRET_KEY"], algorithms=["HS256"]
        )
        self.assertEqual(decoded["user_id"], self.user.id)
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "https://example.com")

    def test_widget_token_preflight(self):
        resp = self.client.options(
            "/auth/widget-token", headers={"Origin": "https://example.com"}
        )
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "https://example.com")


if __name__ == "__main__":
    unittest.main()
