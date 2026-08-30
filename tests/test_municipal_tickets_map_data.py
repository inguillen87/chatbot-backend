import unittest
from unittest.mock import patch
import importlib
import os
import sys
from types import SimpleNamespace

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app import create_app
from config import TestConfig


class MunicipalTicketsMapDataRouteTest(unittest.TestCase):
    def setUp(self):
        self.token_patcher = patch('utils.auth_helpers.token_requerido', lambda f: f)
        self.admin_patcher = patch('utils.auth_helpers.admin_o_empleado_requerido', lambda f: f)
        self.token_patcher.start()
        self.admin_patcher.start()

        import routes.municipal_legacy as muni
        importlib.reload(muni)
        self.muni = muni
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()
        self.token_patcher.stop()
        self.admin_patcher.stop()

        # This test reloads the route module while its authentication decorators
        # are patched so the view can be exercised directly. Restore the module
        # after stopping the patches; otherwise later tests would inherit the
        # undecorated route functions from this test process.
        import routes.municipal_legacy as muni
        importlib.reload(muni)

    def test_test_app_keeps_rotatable_server_side_sessions(self):
        import app as app_module
        from flask_session import Session

        self.assertIs(app_module.Session, Session)
        self.assertTrue(callable(getattr(self.app.session_interface, 'regenerate', None)))

    @patch('services.ticket_service.servicio_tickets')
    def test_estado_param_optional(self, mock_servicio):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = []
        user = SimpleNamespace(municipio_id=1)
        with self.app.test_request_context('/municipal/tickets/map_data'):
            resp = self.muni.municipal_tickets_map_data(user)
        self.assertEqual(resp.status_code, 200)
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.assert_called_once_with(
            tipo_ticket='municipio', actor=user, municipio_id=1, estado=None
        )

    @patch('services.ticket_service.servicio_tickets')
    def test_estado_param_passed(self, mock_servicio):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = []
        user = SimpleNamespace(municipio_id=1)
        with self.app.test_request_context('/municipal/tickets/map_data?estado=cerrado'):
            resp = self.muni.municipal_tickets_map_data(user)
        self.assertEqual(resp.status_code, 200)
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.assert_called_once_with(
            tipo_ticket='municipio', actor=user, municipio_id=1, estado='cerrado'
        )

    @patch('services.ticket_service.servicio_tickets')
    def test_employee_response_is_category_scoped_and_k_anonymous(self, mock_servicio):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = [
            {
                'location': {'lat': -34.60011, 'lng': -68.30011},
                'weight': 4,
                'categoria': 'Luminarias',
            },
            {
                'location': {'lat': -34.60024, 'lng': -68.30024},
                'weight': 1,
                'categoria': 'Luminarias',
            },
            {
                'location': {'lat': -34.61011, 'lng': -68.31011},
                'weight': 1,
                'categoria': 'Luminarias',
            },
            {
                'location': {'lat': -34.62011, 'lng': -68.32011},
                'weight': 20,
                'categoria': 'Baches',
            },
        ]
        employee = SimpleNamespace(
            id=41,
            municipio_id=1,
            rol='empleado',
            es_empleado=True,
            ticket_categorias='Luminarias',
            categorias_ticket=[],
            accesibilidad={},
        )

        with self.app.test_request_context('/municipal/tickets/map_data'):
            response = self.muni.municipal_tickets_map_data(employee)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]['location'], {'lat': -34.6, 'lng': -68.3})
        self.assertEqual(payload[0]['weight'], 5.0)
        self.assertEqual(payload[0]['privacy_mode'], 'employee_aggregated')
        serialized = str(payload).lower()
        self.assertNotIn('baches', serialized)
        self.assertNotIn('-34.60011', serialized)
        self.assertNotIn('-34.61011', serialized)

    @patch('services.ticket_service.servicio_tickets')
    def test_locations_employee_response_never_returns_singleton_coordinates(self, mock_servicio):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = [
            {
                'location': {'lat': -33.08111, 'lng': -68.48111},
                'weight': 4,
                'categoria': 'Luminarias',
                'categoria_id': 17,
            },
            {
                'location': {'lat': -33.08124, 'lng': -68.48124},
                'weight': 1,
                'categoria': 'Alumbrado publico',
                'categoria_id': 17,
            },
            {
                'location': {'lat': -33.09111, 'lng': -68.49111},
                'weight': 1,
                'categoria': 'Luminarias',
                'categoria_id': 17,
            },
            {
                'location': {'lat': -33.07111, 'lng': -68.47111},
                'weight': 20,
                'categoria': 'Baches',
                'categoria_id': 22,
            },
        ]
        employee = SimpleNamespace(
            id=42,
            municipio_id=1,
            rol='empleado',
            es_empleado=True,
            ticket_categorias='',
            categorias_ticket=[SimpleNamespace(id=17, nombre='Luminarias')],
            accesibilidad={},
        )

        with self.app.test_request_context('/municipal/tickets/locations'):
            response = self.muni.municipal_tickets_locations(employee)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(
            payload,
            [{
                'lat': -33.081,
                'lng': -68.481,
                'weight': 5,
                'count': 5,
                'privacy_mode': 'employee_aggregated',
                'k_min': 5,
            }],
        )
        mock_servicio.obtener_locations_de_tickets.assert_not_called()
        serialized = str(payload)
        self.assertNotIn('-33.08111', serialized)
        self.assertNotIn('-33.09111', serialized)

    @patch('services.ticket_service.servicio_tickets')
    def test_locations_admin_contract_remains_exact_and_unchanged(self, mock_servicio):
        exact_locations = [{'lat': -33.08111, 'lng': -68.48111}]
        mock_servicio.obtener_locations_de_tickets.return_value = exact_locations
        admin = SimpleNamespace(
            id=1,
            municipio_id=1,
            rol='admin',
            es_empleado=False,
        )

        with self.app.test_request_context('/municipal/tickets/locations'):
            response = self.muni.municipal_tickets_locations(admin)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), exact_locations)
        mock_servicio.obtener_locations_de_tickets.assert_called_once_with(
            municipio_id=1,
            actor=admin,
        )
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.assert_not_called()


if __name__ == '__main__':
    unittest.main()
