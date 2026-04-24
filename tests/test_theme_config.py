import unittest
from app import create_app, db
from config import TestConfig
from models import TenantProfile, WidgetSettings, User

class TestThemeConfig(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        # Helper to create owner
        self.owner = User(name="Owner", email="owner@test.com", password_hash="x", rol="admin", tipo_chat="pyme")
        db.session.add(self.owner)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_get_theme_config_defaults(self):
        tenant = TenantProfile(slug="test", nombre="Test", tipo="pyme", pyme_id=self.owner.id)
        db.session.add(tenant)
        db.session.commit()

        config = tenant.get_theme_config()
        self.assertEqual(config["mode"], "light")
        self.assertEqual(config["light"]["primary"], "#3B82F6") # Default

    def test_get_theme_config_from_legacy(self):
        tenant = TenantProfile(
            slug="test-legacy",
            nombre="Legacy",
            tipo="pyme",
            pyme_id=self.owner.id,
            tema={"primaryColor": "#FF0000", "secondaryColor": "#00FF00"}
        )
        db.session.add(tenant)
        db.session.commit()

        config = tenant.get_theme_config()
        self.assertEqual(config["light"]["primary"], "#FF0000")
        self.assertEqual(config["dark"]["primary"], "#FF0000")
        self.assertEqual(config["light"]["secondary"], "#00FF00")
        # Ensure dark secondary is NOT the light secondary (green), but default dark gray
        self.assertNotEqual(config["dark"]["secondary"], "#00FF00")
        self.assertEqual(config["dark"]["secondary"], "#1f2937")

    def test_get_theme_config_from_widget_settings(self):
        tenant = TenantProfile(slug="test-ws", nombre="WS", tipo="pyme", pyme_id=self.owner.id)
        db.session.add(tenant)
        db.session.commit()

        ws = WidgetSettings(
            tenant_id=tenant.id,
            primary_color="#123456",
            secondary_color="#654321"
        )
        db.session.add(ws)
        db.session.commit()

        # Refresh tenant
        tenant = TenantProfile.query.get(tenant.id)
        config = tenant.get_theme_config()
        self.assertEqual(config["light"]["primary"], "#123456")
        self.assertEqual(config["dark"]["primary"], "#123456")

    def test_get_theme_config_explicit(self):
        tenant = TenantProfile(slug="test-explicit", nombre="Explicit", tipo="pyme", pyme_id=self.owner.id)
        db.session.add(tenant)
        db.session.commit()

        ws = WidgetSettings(
            tenant_id=tenant.id,
            theme_config={
                "mode": "dark",
                "light": {"primary": "#AAAAAA"},
                "dark": {"primary": "#BBBBBB"}
            }
        )
        db.session.add(ws)
        db.session.commit()

        tenant = TenantProfile.query.get(tenant.id)
        config = tenant.get_theme_config()
        self.assertEqual(config["mode"], "dark")
        self.assertEqual(config["light"]["primary"], "#AAAAAA")
        self.assertEqual(config["dark"]["primary"], "#BBBBBB")
        # Defaults should persist for missing keys
        self.assertEqual(config["light"]["text"], "#000000")

if __name__ == "__main__":
    unittest.main()
