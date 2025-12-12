import unittest
import os
import sys

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from app import create_app, db
from config import Config
from models import Rubro


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
        db.session.add(Rubro(nombre='Municipio', clave='municipio'))
        db.session.add(Rubro(nombre='Pyme', clave='pyme'))
        db.session.commit()
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
        self.assertEqual(len(resp1.get_json()), 2)


if __name__ == '__main__':
    unittest.main()
