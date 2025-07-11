import unittest
from types import SimpleNamespace
from unittest.mock import patch
import sys
import os

# Añadir el directorio raíz del proyecto al sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from routes.empleados import crear_empleado

class DummyQuery:
    def __init__(self, result=None):
        self._result = result
    def filter_by(self, **kwargs):
        return self
    def first(self):
        return self._result

class DummySession:
    def __init__(self):
        self.added = []
    def add(self, obj):
        self.added.append(obj)
    def commit(self):
        pass
    def rollback(self):
        pass

class DummyUser(SimpleNamespace):
    def set_password(self, p):
        self.password = p

class EmpleadosRouteTests(unittest.TestCase):
    def test_crear_empleado_email_existente(self):
        data = {"name": "Emp", "email": "emp@e.com", "password": "123"}
        with patch('routes.empleados.request', SimpleNamespace(get_json=lambda silent=True: data)), \
             patch('routes.empleados.User', SimpleNamespace(query=DummyQuery(DummyUser()))), \
             patch('routes.empleados.db', SimpleNamespace(session=DummySession())):
            resp = crear_empleado(SimpleNamespace(id=1))
            self.assertEqual(resp[1], 400)

    def test_crear_empleado_con_categorias(self):
        data = {
            "name": "Nuevo",
            "email": "nuevo@e.com",
            "password": "123",
            "categorias": ["A", "B"],
        }

        class NewDummyUser(DummyUser):
            query = DummyQuery(None)

        session = DummySession()
        with patch('routes.empleados.request', SimpleNamespace(get_json=lambda silent=True: data)), \
             patch('routes.empleados.User', NewDummyUser), \
             patch('routes.empleados.db', SimpleNamespace(session=session)):
            resp = crear_empleado(SimpleNamespace(id=1))
            self.assertEqual(resp[1], 201)
            created = session.added[0]
            self.assertEqual(created.ticket_categorias, 'A,B')

if __name__ == '__main__':
    unittest.main()
