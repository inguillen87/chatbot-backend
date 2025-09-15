import unittest

try:
    from app import create_app
    from models import db, User
except Exception:
    create_app = None

def _create_user():
    user = User(
        name="Demo",
        email="demo@example.com",
        password_hash="hash",
        token="demo-token",
        nombre_empresa="Demo Corp",
        logo_url="http://example.com/logo.png",
        color_primario="#112233",
        color_secundario="#334455",
        badge_tipo="empresa",
    )
    db.session.add(user)
    db.session.commit()
    return user

from config import TestConfig

@unittest.skipIf(create_app is None, "Flask not available")
class WidgetConfigEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.user = _create_user()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_widget_config_returns_branding(self):
        resp = self.client.get("/widget/config", headers={"Authorization": self.user.token})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["logo_url"], self.user.logo_url)
        self.assertEqual(data["nombre_empresa"], self.user.nombre_empresa)

    def test_widget_config_provides_fallbacks(self):
        self.user.nombre_empresa = None
        self.user.logo_url = None
        self.user.color_primario = None
        self.user.color_secundario = None
        self.user.badge_tipo = None
        self.user.widget_icon_url = None
        self.user.widget_animation = None
        db.session.commit()

        resp = self.client.get("/widget/config", headers={"Authorization": self.user.token})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["nombre_empresa"], self.user.name)
        self.assertEqual(data["logo_url"], "")
        self.assertEqual(data["color_primario"], "#000000")
        self.assertEqual(data["color_secundario"], "#FFFFFF")
        self.assertEqual(data["badge_tipo"], "")
        self.assertEqual(data["widget_icon_url"], "")
        self.assertEqual(data["widget_animation"], "")

    def test_widget_config_includes_customization_for_full_plan(self):
        self.user.plan = "full"
        self.user.widget_icon_url = "http://example.com/icon.png"
        self.user.widget_animation = "bounce"
        db.session.commit()

        resp = self.client.get("/widget/config", headers={"Authorization": self.user.token})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["widget_icon_url"], self.user.widget_icon_url)
        self.assertEqual(data["widget_animation"], self.user.widget_animation)

if __name__ == "__main__":
    unittest.main()
