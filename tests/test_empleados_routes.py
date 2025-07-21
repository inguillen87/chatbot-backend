import unittest
from types import SimpleNamespace
from unittest.mock import patch
import sys
import os

# Añadir el directorio raíz del proyecto al sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app import create_app, db
from models import User
from routes.empleados import crear_empleado
from config import TestConfig as TestingConfig

class EmpleadosRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestingConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_crear_empleado_email_existente(self):
        user = User(name='test', email='emp@e.com', password_hash='test')
        db.session.add(user)
        db.session.commit()
        data = {"name": "Emp", "email": "emp@e.com", "password": "123"}
        with self.app.test_request_context(json=data):
            resp = crear_empleado(SimpleNamespace(id=1))
            self.assertEqual(resp.status_code, 400)

    def test_crear_empleado_con_categorias(self):
        data = {
            "name": "Nuevo",
            "email": "nuevo@e.com",
            "password": "123",
            "categorias": ["A", "B"],
        }

        with self.app.test_request_context(json=data):
            resp = crear_empleado(SimpleNamespace(id=1))
            self.assertEqual(resp.status_code, 201)
            created_user = User.query.filter_by(email="nuevo@e.com").first()
            self.assertIsNotNone(created_user)
            self.assertEqual(created_user.ticket_categorias, 'A,B')

if __name__ == '__main__':
    unittest.main()
