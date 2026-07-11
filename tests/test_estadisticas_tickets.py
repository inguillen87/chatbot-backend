import os
import sys
import types
import unittest
from unittest.mock import patch
import importlib
from types import SimpleNamespace

from flask import Flask

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from config import TestConfig
from services.municipal_stats import StatsFilters


class EstadisticasTicketsRouteTest(unittest.TestCase):
    def setUp(self):
        self.original_auth_helpers = sys.modules.get('utils.auth_helpers')
        stub = types.ModuleType('utils.auth_helpers')
        stub.token_requerido = lambda f: f
        stub.admin_o_empleado_requerido = lambda f: f
        stub.anon_o_token_requerido = lambda f: f
        sys.modules['utils.auth_helpers'] = stub

        os.environ.setdefault('OPENAI_API_KEY', 'test')

        self.session_patcher = patch(
            'flask_session.Session',
            lambda *args, **kwargs: SimpleNamespace(init_app=lambda app: None),
        )
        self.session_patcher.start()

        import routes.estadisticas as estats
        importlib.reload(estats)

        self.app = Flask(__name__)
        self.app.config.from_object(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.session_patcher.stop()
        if self.original_auth_helpers is not None:
            sys.modules['utils.auth_helpers'] = self.original_auth_helpers
        else:
            sys.modules.pop('utils.auth_helpers', None)
        self.app_context.pop()

    @patch('routes.estadisticas.build_stats_for_municipio')
    @patch('routes.estadisticas.servicio_tickets')
    def test_estadisticas_tickets_returns_heatmap_and_stats(self, mock_servicio, mock_stats):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = [
            {"location": {"lat": 1, "lng": 2}, "weight": 3}
        ]
        mock_stats.return_value = {
            "resumen": {
                "abiertos": 5,
                "en_proceso": 2,
                "resueltos": 7,
            },
            "estados": [],
        }
        current_user = SimpleNamespace(municipio_id=1, rubro_id=None)
        import routes.estadisticas as estats
        with self.app.test_request_context('/estadisticas/tickets?tipo=municipio'):
            response = estats.estadisticas_tickets(current_user)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(len(payload["heatmap"]), 1)
        point = payload["heatmap"][0]
        self.assertEqual(point["location"], {"lat": 1, "lng": 2})
        self.assertEqual(point["weight"], 3.0)
        self.assertIn("feature", point)
        self.assertIn("coordinates", point)
        self.assertIn("heatmap_geojson", payload)
        self.assertNotIn("heatmap_google", payload)
        self.assertIn("map_config", payload)
        self.assertIn("map_layers", payload)
        heatmap_layer = payload["map_layers"].get("heatmap")
        self.assertIsInstance(heatmap_layer, dict)
        self.assertEqual(heatmap_layer.get("preferred_format"), "geojson")
        self.assertIn("geojson", heatmap_layer.get("supported_formats", []))
        self.assertIn("source_keys", heatmap_layer)
        self.assertIn("heatmap_cells", payload)
        self.assertIn("heatmap_cells_geojson", payload)
        grid_layer = payload["map_layers"].get("heatmap_cells")
        self.assertIsInstance(grid_layer, dict)
        self.assertEqual(grid_layer.get("kind"), "grid")
        self.assertIn("geojson", grid_layer.get("supported_formats", []))
        self.assertIn("metadata", payload)
        self.assertIn("map", payload["metadata"])
        self.assertIn("heatmap", payload["metadata"]["map"])
        self.assertIn("style", payload["metadata"]["map"]["heatmap"])
        self.assertIn("filters", payload["metadata"])
        self.assertEqual(payload["stats"], mock_stats.return_value)
        self.assertEqual(payload["summary"], mock_stats.return_value["resumen"])
        self.assertEqual(
            payload["cards"],
            [
                {"label": "Reclamos Abiertos", "value": 5},
                {"label": "Reclamos en Proceso", "value": 2},
                {"label": "Reclamos Resueltos", "value": 7},
            ],
        )
        self.assertEqual(payload["filters"], {})
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.assert_called_once_with(
            tipo_ticket='municipio',
            municipio_id=1,
            rubro_id=None,
            tenant_id=None,
            fecha_inicio=None,
            fecha_fin=None,
            categoria=None,
            distrito=None,
            estado=None,
            satisfactorio=None,
        )

    @patch('routes.estadisticas.build_stats_for_municipio')
    @patch('routes.estadisticas.servicio_tickets')
    def test_estadisticas_tickets_accepts_multiple_estados(self, mock_servicio, mock_stats):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = []
        mock_stats.return_value = {"resumen": {}}
        current_user = SimpleNamespace(municipio_id=1, rubro_id=None)
        import routes.estadisticas as estats
        with self.app.test_request_context(
            '/estadisticas/tickets?tipo=municipio&estado=nuevo&estado=en_proceso'
        ):
            response = estats.estadisticas_tickets(current_user)
        self.assertEqual(response.status_code, 200)
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.assert_called_once_with(
            tipo_ticket='municipio',
            municipio_id=1,
            rubro_id=None,
            tenant_id=None,
            fecha_inicio=None,
            fecha_fin=None,
            categoria=None,
            distrito=None,
            estado=['nuevo', 'en_proceso'],
            satisfactorio=None,
        )
        filters = mock_stats.call_args.kwargs.get('filters')
        self.assertIsInstance(filters, (StatsFilters, type(None)))

    @patch('routes.estadisticas.build_stats_for_municipio')
    @patch('routes.estadisticas.servicio_tickets')
    def test_estadisticas_tickets_builds_filters(self, mock_servicio, mock_stats):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = []
        mock_stats.return_value = {"resumen": {}}
        current_user = SimpleNamespace(municipio_id=42, rubro_id=None)
        import routes.estadisticas as estats
        with self.app.test_request_context(
            '/estadisticas/tickets?tipo=municipio'
            '&estado=cerrado'
            '&categoria=Basura'
            '&distrito=Centro'
            '&canal=Web'
            '&agente_id=7&agente_id=8'
            '&fecha_inicio=2024-01-01'
            '&fecha_fin=2024-01-31'
        ):
            response = estats.estadisticas_tickets(current_user)

        self.assertEqual(response.status_code, 200)
        filters = mock_stats.call_args.kwargs.get('filters')
        self.assertIsInstance(filters, StatsFilters)
        self.assertEqual(filters.estados, ('cerrado',))
        self.assertEqual(filters.categorias, ('Basura',))
        self.assertEqual(filters.distritos, ('Centro',))
        self.assertEqual(filters.canales, ('Web',))
        self.assertEqual(filters.agentes, (7, 8))
        self.assertEqual(filters.fecha_inicio.isoformat(), '2024-01-01T00:00:00+00:00')
        self.assertEqual(filters.fecha_fin.isoformat(), '2024-02-01T00:00:00+00:00')

        payload = response.get_json()
        self.assertEqual(
            payload["filters"],
            {
                "fecha_inicio": '2024-01-01T00:00:00+00:00',
                "fecha_fin": '2024-02-01T00:00:00+00:00',
                "estados": ['cerrado'],
                "categorias": ['Basura'],
                "distritos": ['Centro'],
                "canales": ['Web'],
                "agentes": [7, 8],
            },
        )

    @patch('routes.estadisticas.build_stats_for_municipio')
    @patch('routes.estadisticas.servicio_tickets')
    def test_estadisticas_tickets_acepta_varias_categorias(self, mock_servicio, mock_stats):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = []
        mock_stats.return_value = {"resumen": {}}
        current_user = SimpleNamespace(municipio_id=42, rubro_id=None)
        import routes.estadisticas as estats
        with self.app.test_request_context(
            '/estadisticas/tickets?tipo=municipio&categoria=Arbol&categoria=Luminaria'
        ):
            response = estats.estadisticas_tickets(current_user)

        self.assertEqual(response.status_code, 200)
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.assert_called_once_with(
            tipo_ticket='municipio',
            municipio_id=42,
            rubro_id=None,
            tenant_id=None,
            fecha_inicio=None,
            fecha_fin=None,
            categoria=['Arbol', 'Luminaria'],
            distrito=None,
            estado=None,
            satisfactorio=None,
        )

    @patch('routes.estadisticas.build_stats_for_municipio')
    @patch('routes.estadisticas.servicio_tickets')
    def test_estadisticas_tickets_no_inventa_heatmap_si_no_hay_puntos(
        self,
        mock_servicio,
        mock_stats,
    ):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = []
        mock_stats.return_value = {"resumen": {}}
        current_user = SimpleNamespace(municipio_id=9, rubro_id=None)
        import routes.estadisticas as estats

        with self.app.test_request_context('/estadisticas/tickets?tipo=municipio'):
            response = estats.estadisticas_tickets(current_user)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["heatmap"], [])
        self.assertEqual(payload["heatmap_cells"], [])
        self.assertEqual(payload["render_contract"]["state"], "empty")
        self.assertFalse(payload["render_contract"]["can_render_heatmap"])
        self.assertEqual(payload["metadata"]["map"]["heatmap"]["empty_reason"], "no_real_geo_points")


if __name__ == '__main__':
    unittest.main()
