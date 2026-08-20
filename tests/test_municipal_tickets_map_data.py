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


if __name__ == '__main__':
    unittest.main()
