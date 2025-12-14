import unittest

from app import create_app, db
from config import Config
from models import Rubro


class RubrosFallbackConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    CELERY_TASK_ALWAYS_EAGER = True
    SESSION_COOKIE_SECURE = False
    DEMO_RUBROS = [
        {
            "key": "municipio",
            "nombre": "Demo Municipio",
            "tipo_chat": "municipio",
            "rubro_clave": "municipio",
            "segment": "Gobiernos",
            "subsegment": "Municipios y ciudades",
            "welcome_message": "Demo gobierno",
        },
        {
            "key": "ferreteria",
            "nombre": "Demo Ferretería",
            "tipo_chat": "pyme",
            "rubro_clave": "ferreteria",
            "segment": "Empresas",
            "subsegment": "Construcción y hogar",
            "welcome_message": "Demo comercio",
        },
    ]


class RubrosEndpointTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(RubrosFallbackConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.drop_all()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_returns_demo_payload_when_no_rubros_exist(self):
        response = self.client.get("/rubros/")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertGreaterEqual(len(payload), 2)
        demo_segments = {item.get("demo", {}).get("segment") for item in payload}
        self.assertIn("Gobiernos", demo_segments)
        self.assertIn("Empresas", demo_segments)

    def test_injects_demo_metadata_even_without_owner(self):
        rubro = Rubro(nombre="Ferretería", clave="ferreteria", es_publico=True)
        db.session.add(rubro)
        db.session.commit()

        response = self.client.get("/rubros/")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        ferre_items = [item for item in payload if item.get("clave") == "ferreteria"]
        self.assertTrue(ferre_items)
        self.assertIn("demo", ferre_items[0])
        self.assertEqual(ferre_items[0]["demo"].get("segment"), "Empresas")


if __name__ == "__main__":
    unittest.main()
