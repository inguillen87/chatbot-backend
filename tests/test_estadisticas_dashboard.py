import os
import sys
import types
import unittest
from unittest.mock import patch, MagicMock
import importlib
from types import SimpleNamespace

from flask import Flask

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from config import TestConfig


class EstadisticasDashboardRouteTest(unittest.TestCase):
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

        self.estadisticas_module = estats

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
    def test_dashboard_municipio_payload(self, mock_servicio, mock_build_stats):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = [
            {"location": {"lat": -34.6, "lng": -58.4}, "weight": 5}
        ]
        mock_build_stats.return_value = {
            "resumen": {
                "abiertos": 10,
                "en_proceso": 4,
                "resueltos": 7,
            },
            "estados": [
                {"estado": "nuevo", "total": 10},
            ],
        }

        current_user = SimpleNamespace(municipio_id=3, rubro_id=None)

        with self.app.test_request_context(
            '/estadisticas/dashboard?tipo=municipio'
            '&estado=nuevo&categoria=Limpieza&distrito=Centro'
            '&satisfactorio=true'
        ):
            response = self.estadisticas_module.estadisticas_dashboard(current_user)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()

        self.assertEqual(payload['tipo'], 'municipio')
        self.assertEqual(len(payload['heatmap']), 1)
        heat_point = payload['heatmap'][0]
        self.assertEqual(heat_point['location'], {"lat": -34.6, "lng": -58.4})
        self.assertEqual(heat_point['weight'], 5.0)
        self.assertIn('feature', heat_point)
        self.assertIn('coordinates', heat_point)
        self.assertIn('heatmap_geojson', payload)
        self.assertNotIn('heatmap_google', payload)
        self.assertIn('map_config', payload)
        self.assertIn('map_layers', payload)
        heatmap_layer = payload['map_layers'].get('heatmap')
        self.assertIsInstance(heatmap_layer, dict)
        self.assertEqual(heatmap_layer.get('preferred_format'), 'geojson')
        self.assertIn('geojson', heatmap_layer.get('supported_formats', []))
        self.assertIn('source_keys', heatmap_layer)
        self.assertIn('heatmap_cells', payload)
        self.assertIn('heatmap_cells_geojson', payload)
        grid_layer = payload['map_layers'].get('heatmap_cells')
        self.assertIsInstance(grid_layer, dict)
        self.assertEqual(grid_layer.get('kind'), 'grid')
        self.assertIn('geojson', grid_layer.get('supported_formats', []))
        self.assertIn('metadata', payload)
        self.assertIn('map', payload['metadata'])
        self.assertIn('heatmap', payload['metadata']['map'])
        self.assertEqual(payload['stats'], mock_build_stats.return_value)
        self.assertEqual(payload['summary'], mock_build_stats.return_value['resumen'])
        self.assertEqual(
            payload['filters'],
            {
                'fecha_inicio': None,
                'fecha_fin': None,
                'estados': ['nuevo'],
                'categorias': ['Limpieza'],
                'distritos': ['Centro'],
                'canales': [],
                'agentes': [],
            },
        )
        self.assertEqual(
            payload['cards'],
            [
                {"label": "Reclamos Abiertos", "value": 10},
                {"label": "Reclamos en Proceso", "value": 4},
                {"label": "Reclamos Resueltos", "value": 7},
            ],
        )
        self.assertIn('metadata', payload)
        self.assertEqual(payload['metadata']['municipio_id'], 3)
        self.assertIsNone(payload['metadata'].get('pyme_id'))
        self.assertIn('last_updated', payload['metadata'])

        self.assertEqual(
            payload['applied_filters'],
            {
                'estados': ['nuevo'],
                'categorias': ['Limpieza'],
                'distrito': 'Centro',
                'fecha_inicio': None,
                'fecha_fin': None,
                'satisfactorio': True,
                'municipio_id': 3,
                'rubro_id': None,
            }
        )

        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.assert_called_once_with(
            tipo_ticket='municipio',
            municipio_id=3,
            rubro_id=None,
            fecha_inicio=None,
            fecha_fin=None,
            categoria=['Limpieza'],
            distrito='Centro',
            estado='nuevo',
            satisfactorio=True,
        )

    @patch('routes.estadisticas.build_stats_for_municipio')
    @patch('routes.estadisticas.MetricasService')
    @patch('routes.estadisticas.servicio_tickets')
    def test_dashboard_pyme_payload(self, mock_servicio, mock_metricas_cls, mock_build_stats):
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.return_value = []
        mock_build_stats.return_value = {}

        metricas_instance = MagicMock()
        metricas_instance.get_total_ingresos.return_value = 1250
        metricas_instance.get_total_pedidos.return_value = 18
        metricas_instance.get_new_customers.return_value = 11
        metricas_instance.get_conversion_rate.return_value = 1.64
        metricas_instance.get_kpis.return_value = {"growth": 0.2}
        metricas_instance.get_sales_over_time.return_value = [
            {"date": "2024-01-01", "sales": 100}
        ]
        metricas_instance.get_top_products.return_value = [
            {"product": "Plan Premium", "sales": 10}
        ]
        metricas_instance.get_sales_by_region.return_value = [
            {"region": "Centro", "sales": 500}
        ]
        mock_metricas_cls.return_value = metricas_instance

        current_user = SimpleNamespace(id=77, municipio_id=None, rubro_id=9, pyme_id=None)

        with self.app.test_request_context('/estadisticas/dashboard?tipo=pyme'):
            response = self.estadisticas_module.estadisticas_dashboard(current_user)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()

        self.assertEqual(payload['tipo'], 'pyme')
        self.assertTrue(payload['heatmap'])
        first_point = payload['heatmap'][0]
        self.assertIn('location', first_point)
        self.assertEqual(first_point.get('fuente'), 'demo')
        self.assertIn('feature', first_point)
        self.assertIn('coordinates', first_point)
        self.assertIn('heatmap_geojson', payload)
        self.assertNotIn('heatmap_google', payload)
        self.assertIn('map_config', payload)
        self.assertIn('map_layers', payload)
        pyme_heatmap_layer = payload['map_layers'].get('heatmap')
        self.assertIsInstance(pyme_heatmap_layer, dict)
        self.assertEqual(pyme_heatmap_layer.get('preferred_format'), 'geojson')
        self.assertIn('geojson', pyme_heatmap_layer.get('supported_formats', []))
        self.assertIn('heatmap_cells', payload)
        self.assertIn('heatmap_cells_geojson', payload)
        pyme_grid_layer = payload['map_layers'].get('heatmap_cells')
        self.assertIsInstance(pyme_grid_layer, dict)
        self.assertEqual(pyme_grid_layer.get('kind'), 'grid')
        self.assertIn('geojson', pyme_grid_layer.get('supported_formats', []))
        self.assertEqual(payload['filters'], {})
        self.assertEqual(
            payload['summary'],
            {
                'total_ventas': 1250,
                'total_pedidos': 18,
                'clientes_unicos': 11,
                'tasa_conversion': 1.64,
            }
        )
        self.assertEqual(
            payload['cards'],
            [
                {"label": "Ingresos Totales", "value": 1250},
                {"label": "Pedidos", "value": 18},
                {"label": "Clientes Únicos", "value": 11},
                {"label": "Tasa de Conversión", "value": 1.64},
            ],
        )
        self.assertEqual(
            payload['stats'],
            {
                'kpis': {"growth": 0.2},
                'ventas_over_time': [{"date": "2024-01-01", "sales": 100}],
                'top_productos': [{"product": "Plan Premium", "sales": 10}],
                'ventas_por_region': [{"region": "Centro", "sales": 500}],
            }
        )
        self.assertEqual(payload['metadata']['rubro_id'], 9)
        self.assertEqual(payload['metadata']['pyme_id'], 77)

        mock_metricas_cls.assert_called_once_with(77)
        mock_build_stats.assert_not_called()
        mock_servicio.obtener_tickets_con_ubicacion_para_mapa.assert_called_once_with(
            tipo_ticket='pyme',
            municipio_id=None,
            rubro_id=9,
            fecha_inicio=None,
            fecha_fin=None,
            categoria=None,
            distrito=None,
            estado=None,
            satisfactorio=None,
        )


if __name__ == '__main__':
    unittest.main()
