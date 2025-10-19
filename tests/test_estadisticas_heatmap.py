import unittest
from unittest.mock import patch
import importlib
import os
import sys
from types import SimpleNamespace, ModuleType

from flask import Flask


project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from config import TestConfig


class EstadisticasHeatmapRouteTest(unittest.TestCase):
    def setUp(self):
        self.original_auth_helpers = sys.modules.get('utils.auth_helpers')
        if self.original_auth_helpers is None:
            stub = ModuleType('utils.auth_helpers')
            stub.token_requerido = lambda f: f
            stub.admin_o_empleado_requerido = lambda f: f
            stub.anon_o_token_requerido = lambda f: f
            sys.modules['utils.auth_helpers'] = stub
            self._stubbed_auth_helpers = True
        else:
            self._stubbed_auth_helpers = False

        # Bypass authentication decorators for testing by patching source module
        self.token_patcher = patch('utils.auth_helpers.token_requerido', lambda f: f)
        self.admin_patcher = patch('utils.auth_helpers.admin_o_empleado_requerido', lambda f: f)
        self.session_patcher = patch(
            'flask_session.Session',
            lambda *args, **kwargs: SimpleNamespace(init_app=lambda app: None),
        )
        self.token_patcher.start()
        self.admin_patcher.start()
        self.session_patcher.start()

        import routes.estadisticas as estats
        importlib.reload(estats)
        self.app = Flask(__name__)
        self.app.config.from_object(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.token_patcher.stop()
        self.admin_patcher.stop()
        self.session_patcher.stop()
        self.app_context.pop()
        if self._stubbed_auth_helpers:
            sys.modules.pop('utils.auth_helpers', None)
        elif self.original_auth_helpers is not None:
            sys.modules['utils.auth_helpers'] = self.original_auth_helpers

    @patch('routes.estadisticas.servicio_tickets')
    def test_mapa_calor_datos(self, mock_servicio):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = [
            {"location": {"lat": 1, "lng": 2}, "weight": 3, "categoria": None}
        ]
        import routes.estadisticas as estats
        with self.app.test_request_context('/estadisticas/mapa_calor/datos?tipo_ticket=municipio'):
            response = estats.mapa_calor_datos(current_user=None)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIsInstance(payload, dict)
        self.assertEqual(len(payload.get("heatmap", [])), 1)
        point = payload["heatmap"][0]
        self.assertEqual(point["location"], {"lat": 1, "lng": 2})
        self.assertEqual(point["weight"], 3.0)
        self.assertIn("feature", point)
        self.assertIn("coordinates", point)
        self.assertIn("heatmap_geojson", payload)
        self.assertIn("heatmap_google", payload)
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.assert_called_once_with(
            tipo_ticket='municipio',
            municipio_id=None,
            rubro_id=None,
            fecha_inicio=None,
            fecha_fin=None,
            categoria=None,
            distrito=None,
            estado=None,
            satisfactorio=None,
        )

    @patch('routes.estadisticas.servicio_tickets')
    def test_mapa_calor_multiple_estados(self, mock_servicio):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = []
        import routes.estadisticas as estats
        with self.app.test_request_context(
            '/estadisticas/mapa_calor/datos?tipo_ticket=municipio&estado=nuevo&estado=en_vivo'
        ):
            response = estats.mapa_calor_datos(current_user=None)
        self.assertEqual(response.status_code, 200)
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.assert_called_once_with(
            tipo_ticket='municipio',
            municipio_id=None,
            rubro_id=None,
            fecha_inicio=None,
            fecha_fin=None,
            categoria=None,
            distrito=None,
            estado=['nuevo', 'en_vivo'],
            satisfactorio=None,
        )

    @patch('routes.estadisticas.servicio_tickets')
    def test_mapa_calor_acepta_varias_categorias(self, mock_servicio):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = []
        import routes.estadisticas as estats
        with self.app.test_request_context(
            '/estadisticas/mapa_calor/datos?tipo_ticket=municipio&categoria=Arbol&categoria=Luminaria'
        ):
            response = estats.mapa_calor_datos(current_user=None)
        self.assertEqual(response.status_code, 200)
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.assert_called_once_with(
            tipo_ticket='municipio',
            municipio_id=None,
            rubro_id=None,
            fecha_inicio=None,
            fecha_fin=None,
            categoria=['Arbol', 'Luminaria'],
            distrito=None,
            estado=None,
            satisfactorio=None,
        )


if __name__ == '__main__':
    unittest.main()
