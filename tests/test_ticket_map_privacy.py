import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask


project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import routes.ticket as ticket_routes


def _unwrapped_map_handler():
    handler = ticket_routes.mapa_de_tickets
    while hasattr(handler, '__wrapped__'):
        handler = handler.__wrapped__
    return handler


class TicketMapPrivacyRouteTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.tenant = SimpleNamespace(id=9, municipio_id=3, pyme_id=None)

    def _call(self, viewer):
        with (
            self.app.test_request_context('/tickets/municipio/mapa'),
            patch.object(
                ticket_routes,
                '_resolve_tenant_scope',
                return_value=(self.tenant, 3, None),
            ),
            patch.object(ticket_routes, '_authorized_for_tenant_scope', return_value=True),
            patch.object(ticket_routes, 'servicio_tickets') as service,
        ):
            service.obtener_tickets_con_ubicacion_para_mapa.return_value = [
                {
                    'location': {'lat': -34.60011, 'lng': -68.30011},
                    'weight': 2,
                    'categoria': 'Nombre anterior',
                    'categoria_id': 17,
                },
                {
                    'location': {'lat': -34.60024, 'lng': -68.30024},
                    'weight': 3,
                    'categoria': 'Nombre actualizado',
                    'categoria_id': 17,
                },
                {
                    'location': {'lat': -34.61011, 'lng': -68.31011},
                    'weight': 1,
                    'categoria': 'Nombre anterior',
                    'categoria_id': 17,
                },
                {
                    'location': {'lat': -34.62011, 'lng': -68.32011},
                    'weight': 20,
                    'categoria': 'Restringida',
                    'categoria_id': 18,
                },
            ]
            response = _unwrapped_map_handler()(viewer, 'municipio')
        return response

    def test_employee_feature_collection_is_scoped_and_k_anonymous(self):
        employee = SimpleNamespace(
            id=42,
            rol='empleado',
            es_empleado=True,
            tipo_chat='municipio',
            ticket_categorias='',
            categorias_ticket=[SimpleNamespace(id=17, nombre='')],
            accesibilidad={},
        )

        response = self._call(employee)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['privacy']['mode'], 'employee_aggregated')
        self.assertEqual(payload['privacy']['k_min'], 5)
        self.assertEqual(len(payload['features']), 1)
        self.assertEqual(
            payload['features'][0]['geometry']['coordinates'],
            [-68.3, -34.6],
        )
        self.assertEqual(payload['features'][0]['properties']['weight'], 5)
        serialized = json.dumps(payload).lower()
        self.assertNotIn('restringida', serialized)
        self.assertNotIn('-34.60011', serialized)
        self.assertNotIn('-34.61011', serialized)

    def test_admin_feature_collection_keeps_exact_legacy_contract(self):
        admin = SimpleNamespace(
            id=7,
            rol='admin',
            es_empleado=False,
            tipo_chat='municipio',
        )

        response = self._call(admin)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertNotIn('privacy', payload)
        self.assertEqual(len(payload['features']), 4)
        self.assertEqual(
            payload['features'][0]['geometry']['coordinates'],
            [-68.30011, -34.60011],
        )


if __name__ == '__main__':
    unittest.main()
