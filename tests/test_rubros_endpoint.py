import unittest
import os
import sys
from unittest.mock import patch

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from app import create_app, db
from config import Config
from models import Rubro
from services.demo_registry import DemoRubro


class TestRubrosEndpoint(unittest.TestCase):
    def setUp(self):
        class TestConfig(Config):
            TESTING = True
            SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
            WTF_CSRF_ENABLED = False
            SESSION_COOKIE_SECURE = False
            CELERY_TASK_ALWAYS_EAGER = True
            DEBUG = False

        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_rubros_endpoint_accepts_trailing_slash(self):
        resp1 = self.client.get('/rubros')
        resp2 = self.client.get('/rubros/')
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp1.get_json(), resp2.get_json())
        self.assertGreaterEqual(len(resp1.get_json()), 2)

    def test_rubros_endpoint_falls_back_to_demo_catalog_when_empty(self):
        # Simular base vacía para que el endpoint utilice los demos
        db.session.query(Rubro).delete()
        db.session.commit()
        demo = DemoRubro(
            key="demo-alimentacion",
            label="Demo Alimentación",
            descripcion="Catálogo demo",
            tipo_chat="pyme",
            owner_user_id=None,
            rubro_id=None,
            rubro_clave="demo-alimentacion",
        )

        with patch("routes.rubros.load_demo_rubros", return_value=[demo]):
            resp = self.client.get("/rubros/")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertGreaterEqual(len(data), 1)
        demo_entry = next((item for item in data if item.get("clave") == "demo-alimentacion"), None)
        self.assertIsNotNone(demo_entry)
        self.assertIn("demo", demo_entry)

    def test_hidden_rubro_with_demo_metadata_is_exposed(self):
        rubro = Rubro(nombre="Oculto", clave="oculto", descripcion="", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

        demo = DemoRubro(
            key="oculto-demo",
            label="Oculto Demo",
            descripcion="",
            tipo_chat="pyme",
            owner_user_id=None,
            rubro_id=rubro.id,
            rubro_clave=rubro.clave,
        )

        with patch("routes.rubros.load_demo_rubros", return_value=[demo]), patch(
            "services.demo_registry.load_demo_rubros", return_value=[demo]
        ):
            resp = self.client.get("/rubros")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        claves = [item.get("clave") for item in data]
        self.assertIn("oculto", claves)
        entry = next(item for item in data if item.get("clave") == "oculto")
        self.assertTrue(entry.get("demo"))


if __name__ == '__main__':
    unittest.main()
