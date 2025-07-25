import unittest
from types import SimpleNamespace
from unittest.mock import patch
import sys
import os

# Añadir el directorio raíz del proyecto al sys.path
project_root_mis_tickets = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_mis_tickets not in sys.path:
    sys.path.insert(0, project_root_mis_tickets)

from routes.ticket import get_mis_tickets

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
    def test_returns_object_with_tickets(self):
        user = SimpleNamespace(id=1)
        fecha_col = DummyColumn()
        t1 = SimpleNamespace(id=1, nro_ticket=1, asunto='A', estado='nuevo', fecha=SimpleNamespace(isoformat=lambda: 'f1'), direccion=None, latitud=None, longitud=None)
        t2 = SimpleNamespace(id=2, nro_ticket=2, asunto='B', estado='nuevo', fecha=SimpleNamespace(isoformat=lambda: 'f2'), telefono=None, email=None, dni=None, estado_cliente=None, direccion=None, latitud=None, longitud=None)
        muni_model = SimpleNamespace(query=DummyQuery([t1]), fecha=fecha_col, categoria=DummyColumn())
        pyme_model = SimpleNamespace(query=DummyQuery([t2]), fecha=fecha_col, categoria=DummyColumn())

        with patch('routes.ticket.MunicipioTicket', muni_model), \
             patch('routes.ticket.PymeTicket', pyme_model), \
             patch('routes.ticket.jsonify', lambda x: x), \
             patch('routes.ticket.request', SimpleNamespace(args={})): \
            resp = get_mis_tickets.__wrapped__(user)

        self.assertIsInstance(resp, dict)
        self.assertIn('tickets', resp)
        self.assertEqual(len(resp['tickets']), 2)

if __name__ == '__main__':
    unittest.main()
