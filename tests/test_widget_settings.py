import unittest

try:
    from app import create_app
    from config import Config
    from models import TenantProfile, User, WidgetSettings, db
    from utils.auth_helpers import generar_token
except Exception:
    create_app = None


class _TestConfig(Config):
    TESTING = True
    SESSION_TYPE = "filesystem"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"


def _create_tenant_with_owner():
    owner = User(
        name="Admin",
        email="admin@example.com",
        password_hash="hash",
        rol="admin",
    )
    db.session.add(owner)
    db.session.flush()
    owner.token = generar_token(owner.id, owner.rol, None, None, None)

    tenant = TenantProfile(
        slug="demo",
        nombre="Demo Municipio",
        tipo="municipio",
        municipio=owner,
    )
    db.session.add(tenant)
    db.session.commit()
    return owner, tenant


@unittest.skipIf(create_app is None, "Flask not available")
class WidgetSettingsTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.owner, self.tenant = _create_tenant_with_owner()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_widget_settings_defaults_and_embed(self):
        resp = self.client.get(
            "/widget-settings",
            headers={"Authorization": self.owner.token},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("embed_code", data)
        self.assertIn("data-singleton", data["embed_code"])
        self.assertIn(self.tenant.slug, data["embed_code"])

    def test_widget_settings_update_and_widget_config_merge(self):
        payload = {
            "primary_color": "#ff0000",
            "secondary_color": "#00ff00",
            "avatar_url": "http://example.com/avatar.png",
            "font_family": "Inter",
            "bubble_shape": "square",
            "default_open": True,
        }
        put = self.client.put(
            "/widget-settings",
            json=payload,
            headers={"Authorization": self.owner.token},
        )
        self.assertEqual(put.status_code, 200)

        widget_resp = self.client.get(
            f"/api/public/widget-config?tenant={self.tenant.slug}",
        )
        self.assertEqual(widget_resp.status_code, 200)
        widget_data = widget_resp.get_json()
        attrs = widget_data["widget"]["attributes"]
        self.assertEqual(attrs["data-primary-color"], payload["primary_color"])
        self.assertEqual(attrs["data-accent-color"], payload["secondary_color"])
        self.assertEqual(attrs["data-logo-url"], payload["avatar_url"])
        self.assertEqual(attrs["data-font-family"], payload["font_family"])
        self.assertEqual(attrs["data-bubble-shape"], payload["bubble_shape"])
        self.assertEqual(attrs["data-default-open"], "true")
        self.assertIn("theme_config", widget_data["widget"])
        self.assertIn("cta_messages", widget_data["widget"])
        self.assertIsInstance(widget_data["widget"]["cta_messages"], list)

    def test_public_widget_config_allows_querystring_tenant_fallback(self):
        resp = self.client.get(
            "/api/public/tenants/perfil/widget-config",
            query_string={"tenant": self.tenant.slug, "tenant_slug": self.tenant.slug},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.get_json(), dict)


if __name__ == "__main__":
    unittest.main()
