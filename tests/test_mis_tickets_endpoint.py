import unittest
from types import SimpleNamespace
from unittest.mock import patch
import sys
import os

# Añadir el directorio raíz del proyecto al sys.path
project_root_mis_tickets = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_mis_tickets not in sys.path:
    sys.path.insert(0, project_root_mis_tickets)

from app import create_app, db
from routes.ticket import get_mis_tickets
from config import Config

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False

class DummyQuery(list):
    def filter_by(self, **kwargs):
        return self
    def filter(self, *args, **kwargs):
        return self
    def order_by(self, *args):
        return self
    def all(self):
        return list(self)
    def offset(self, *args):
        return self
    def limit(self, *args):
        return self

class DummyColumn:
    def desc(self):
        return self

class MisTicketsEndpointTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_returns_object_with_tickets(self):
        user = SimpleNamespace(id=1)
        fecha_col = DummyColumn()
        t1 = SimpleNamespace(id=1, nro_ticket=1, asunto='A', estado='nuevo', fecha=SimpleNamespace(isoformat=lambda: 'f1'), direccion=None, latitud=None, longitud=None, categoria='bache')
        t2 = SimpleNamespace(id=2, nro_ticket=2, asunto='B', estado='nuevo', fecha=SimpleNamespace(isoformat=lambda: 'f2'), telefono=None, email=None, dni=None, estado_cliente=None, direccion=None, latitud=None, longitud=None, categoria='limpieza')
        muni_model = SimpleNamespace(query=DummyQuery([t1]), fecha=fecha_col, categoria=DummyColumn())
        pyme_model = SimpleNamespace(query=DummyQuery([t2]), fecha=fecha_col, categoria=DummyColumn())

        with self.app.test_request_context(), patch('routes.ticket.MunicipioTicket', muni_model), \
             patch('routes.ticket.PymeTicket', pyme_model), \
             patch('routes.ticket.jsonify', lambda x: x), \
             patch('routes.ticket.request', SimpleNamespace(args={})):
            resp = get_mis_tickets.__wrapped__(user)

        self.assertIsInstance(resp, dict)
        self.assertIn('tickets', resp)
        self.assertEqual(len(resp['tickets']), 2)

if __name__ == '__main__':
    unittest.main()
