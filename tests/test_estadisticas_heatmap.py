import unittest
from unittest.mock import patch
import importlib
import os
import sys

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from config import TestConfig


class EstadisticasHeatmapRouteTest(unittest.TestCase):
    def setUp(self):
        # Bypass authentication decorators for testing by patching source module
        self.token_patcher = patch('utils.auth_helpers.token_requerido', lambda f: f)
        self.admin_patcher = patch('utils.auth_helpers.admin_o_empleado_requerido', lambda f: f)
        self.token_patcher.start()
        self.admin_patcher.start()

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
        self.app_context.pop()

    @patch('routes.estadisticas.servicio_tickets')
    def test_mapa_calor_datos(self, mock_servicio):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = [
            {"location": {"lat": 1, "lng": 2}, "weight": 3, "categoria": None, "direccion": "dir", "distrito": "barrio"}
        ]
        import routes.estadisticas as estats
        with self.app.test_request_context('/estadisticas/mapa_calor/datos?tipo_ticket=municipio'):
            response = estats.mapa_calor_datos(current_user=None)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            [{"location": {"lat": 1, "lng": 2}, "weight": 3, "categoria": None, "direccion": "dir", "distrito": "barrio"}],
        )


if __name__ == '__main__':
    unittest.main()
