import unittest
import jwt
import os

os.environ.setdefault("CORS_ALLOWED_ORIGINS", "https://example.com")

try:
    from app import create_app
    from models import TenantProfile, db, User
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
        db.session.flush()
        cls.tenant = TenantProfile(
            slug="demo-tenant",
            nombre="Demo Tenant",
            tipo="municipio",
            municipio_id=cls.user.id,
            configuracion={"widget_tokens": ["tenant-widget-token"]},
        )
        db.session.add(cls.tenant)
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
        self.assertEqual(data.get("contract_version"), "auth.widget_token.v1")
        decoded = jwt.decode(
            data["token"], self.app.config["SECRET_KEY"], algorithms=["HS256"]
        )
        self.assertEqual(decoded["user_id"], self.user.id)
        self.assertEqual(decoded.get("session_kind"), "widget")
        self.assertIn("renew_until", decoded)
        self.assertIn(
            resp.headers.get("Access-Control-Allow-Origin"),
            {"*", origin},
        )

    def test_widget_token_nested_alias_matches_docs(self):
        resp = self.client.post(
            "/auth/widget/token",
            headers={"Authorization": self.user.token, "Origin": "https://example.com"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json().get("contract_version"), "auth.widget_token.v1")

    def test_widget_token_preflight(self):
        origin = "https://example.com"
        resp = self.client.options(
            "/auth/widget-token", headers={"Origin": origin}
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
        payload = resp.get_json()
        self.assertIn("token", payload)
        self.assertEqual(payload.get("contract_version"), "auth.widget_token.v1")
        self.assertEqual(
            resp.headers.get("Access-Control-Allow-Origin"), "https://example.com"
        )

    def test_widget_token_cors_wildcard(self):
        os.environ["CORS_ALLOWED_ORIGINS"] = "*"
        resp = self.client.options(
            "/auth/widget-token", headers={"Origin": "https://foo.com"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")
        os.environ["CORS_ALLOWED_ORIGINS"] = "https://example.com"

    def test_widget_refresh_returns_new_jwt(self):
        mint_resp = self.client.post(
            "/auth/widget-token",
            headers={"Authorization": self.user.token, "Origin": "https://example.com"},
        )
        token = mint_resp.get_json()["token"]
        resp = self.client.post(
            "/auth/widget-refresh",
            json={"token": token},
            headers={"Origin": "https://example.com"},
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertIn("token", payload)
        self.assertEqual(payload.get("contract_version"), "auth.widget_token.v1")
        refreshed = jwt.decode(
            payload["token"],
            self.app.config["SECRET_KEY"],
            algorithms=["HS256"],
        )
        self.assertEqual(refreshed.get("session_kind"), "widget")
        self.assertEqual(
            resp.headers.get("Access-Control-Allow-Origin"), "https://example.com"
        )

    def test_widget_refresh_nested_alias_matches_docs(self):
        token = self.client.post(
            "/auth/widget-token",
            headers={"Authorization": self.user.token, "Origin": "https://example.com"},
        ).get_json()["token"]
        resp = self.client.post(
            "/auth/widget/refresh",
            json={"token": token},
            headers={"Origin": "https://example.com"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json().get("contract_version"), "auth.widget_token.v1")

    def test_widget_refresh_preflight_trailing_slash(self):
        resp = self.client.options(
            "/auth/widget-refresh/", headers={"Origin": "https://example.com"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.headers.get("Access-Control-Allow-Origin"), "https://example.com"
        )

    def test_widget_refresh_post_trailing_slash(self):
        token = self.client.post(
            "/auth/widget-token",
            headers={"Authorization": self.user.token, "Origin": "https://example.com"},
        ).get_json()["token"]
        resp = self.client.post(
            "/auth/widget-refresh/",
            json={"token": token},
            headers={"Origin": "https://example.com"},
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertIn("token", payload)
        self.assertEqual(payload.get("contract_version"), "auth.widget_token.v1")
        self.assertEqual(
            resp.headers.get("Access-Control-Allow-Origin"), "https://example.com"
        )

    def test_widget_token_from_tenant_profile_allows_profile_fetch(self):
        resp = self.client.get(
            "/auth/perfil",
            headers={"Authorization": "tenant-widget-token", "Origin": "https://example.com"},
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload.get("id"), self.user.id)


if __name__ == "__main__":
    unittest.main()
