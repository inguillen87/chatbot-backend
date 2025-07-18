import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
import sys
import os

# Añadir el directorio raíz del proyecto al sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from routes.ticket import _get_tickets_del_usuario_logic

class DummyQuery(list):
    def filter_by(self, **kwargs):
        # This is a simplified mock. A real implementation might need to handle different filters.
        return self

    def filter(self, *args, **kwargs):
        # This is a simplified mock. A real implementation might need to handle different filters.
        return self

    def order_by(self, *args):
        return self

    def offset(self, *args):
        return self

    def limit(self, *args):
        return self

    def all(self):
        return list(self)

class DummyColumn:
    def desc(self):
        return self

class TicketsEndpointTest(unittest.TestCase):
    def test_get_tickets_del_usuario_municipio(self):
        # Mock current_user for a municipality
        user = SimpleNamespace(
            id=1,
            rol='admin',
            municipio_id=10,
            rubro=SimpleNamespace(nombre='municipios'),
            ticket_categorias=None
        )

        # Mock MunicipioTicket
        t1 = SimpleNamespace(
            id=1,
            nro_ticket=101,
            asunto='Test Ticket 1',
            estado='nuevo',
            fecha=MagicMock(),
            categoria='Plazas y parques',
            direccion='Calle Falsa 123',
            latitud=None,
            longitud=None
        )
        t1.fecha.isoformat.return_value = '2023-01-01T12:00:00'

        # Mock the query object
        mock_query = DummyQuery([t1])

        # Mock the models
        muni_model = SimpleNamespace(query=mock_query, fecha=DummyColumn(), categoria=DummyColumn(), municipio_id=10)

        # Mock Flask's request and jsonify
        with patch('routes.ticket.MunicipioTicket', muni_model), \
             patch('routes.ticket.jsonify', lambda x: x), \
             patch('routes.ticket.request', SimpleNamespace(args={})), \
             patch('routes.ticket.current_app', SimpleNamespace(config=MagicMock(), logger=MagicMock())):

            # Call the logic function directly
            resp = _get_tickets_del_usuario_logic(user)

            # Assertions
            self.assertIsInstance(resp, list)
            self.assertEqual(len(resp), 1)
            self.assertEqual(resp[0]['id'], 1)
            self.assertEqual(resp[0]['nro_ticket'], 101)
            self.assertEqual(resp[0]['asunto'], 'Test Ticket 1')

if __name__ == '__main__':
    unittest.main()
