import unittest
from unittest.mock import patch
import importlib
import os
import sys
from types import SimpleNamespace

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from config import TestConfig


class EstadisticasTicketsRouteTest(unittest.TestCase):
    def setUp(self):
        self.token_patcher = patch('utils.auth_helpers.token_requerido', lambda f: f)
        self.admin_patcher = patch('utils.auth_helpers.admin_o_empleado_requerido', lambda f: f)
        self.session_patcher = patch('flask_session.Session', lambda *a, **k: SimpleNamespace(init_app=lambda app: None))
        self.token_patcher.start()
        self.admin_patcher.start()
        self.session_patcher.start()

        import routes.estadisticas as estats
        importlib.reload(estats)
        import app as app_module
        importlib.reload(app_module)
        self.app = app_module.create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.token_patcher.stop()
        self.admin_patcher.stop()
        self.session_patcher.stop()
        self.app_context.pop()

    @patch('routes.estadisticas.servicio_tickets')
    def test_estadisticas_tickets_returns_heatmap(self, mock_servicio):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = [
            {"location": {"lat": 1, "lng": 2}, "weight": 3}
        ]
        current_user = SimpleNamespace(municipio_id=1, rubro_id=None)
        import routes.estadisticas as estats
        with self.app.test_request_context('/estadisticas/tickets?tipo=municipio'):
            response = estats.estadisticas_tickets(current_user)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"heatmap": [{"location": {"lat": 1, "lng": 2}, "weight": 3}]},
        )
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.assert_called_once_with(
            tipo_ticket='municipio',
            municipio_id=1,
            rubro_id=None,
            fecha_inicio=None,
            fecha_fin=None,
            categoria=None,
            estado=None,
            satisfactorio=None,
            agrupar=True,
        )


if __name__ == '__main__':
    unittest.main()
