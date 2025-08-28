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

@unittest.skipIf(create_app is None, "Flask not available")
class WidgetConfigEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
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

if __name__ == "__main__":
    unittest.main()
