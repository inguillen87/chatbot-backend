import importlib
import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask, g

from services.municipal_stats import StatsFilters

# Añadir el directorio raíz del proyecto al sys.path
project_root_municipal_stats = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..')
)
if project_root_municipal_stats not in sys.path:
    sys.path.insert(0, project_root_municipal_stats)


def make_user():
    return SimpleNamespace(id=1, municipio_id=1, empresa_id=None, rol='admin')


class MunicipalStatsTests(unittest.TestCase):
    def setUp(self):
        self.original_auth_helpers = sys.modules.get('utils.auth_helpers')
        stub = types.ModuleType('utils.auth_helpers')
        stub.token_requerido = lambda f: f
        stub.admin_o_empleado_requerido = lambda f: f
        stub.anon_o_token_requerido = lambda f: f
        sys.modules['utils.auth_helpers'] = stub

        import routes.municipal_legacy as municipal_module
        importlib.reload(municipal_module)
        self.module = municipal_module

    def tearDown(self):
        if self.original_auth_helpers is not None:
            sys.modules['utils.auth_helpers'] = self.original_auth_helpers
        else:
            sys.modules.pop('utils.auth_helpers', None)
        sys.modules.pop('routes.municipal_legacy', None)

    def test_basic_stats(self):
        expected_payload = {
            'resumen': {
                'total': 20,
                'abiertos': 5,
                'cerrados': 15,
                'sin_resolver': 5,
                'nuevos': 2,
                'en_proceso': 1,
                'en_vivo': 1,
                'esperando': 1,
                'resueltos': 15,
                'expirados': 0,
                'promedio_satisfaccion': 4.7,
                'respuestas_satisfaccion': 3,
                'tasa_resolucion': 75.0,
                'sla_24h': 80.0,
            },
            'estados': [{'estado': 'cerrado', 'total': 15, 'porcentaje': 75.0}],
            'por_categoria': [{
                'categoria': 'Baches',
                'total': 5,
                'abiertos': 1,
                'cerrados': 4,
                'satisfaccion': {'promedio': 4.7, 'respuestas': 3}
            }],
            'por_distrito': [{
                'distrito': 'Centro',
                'total': 5,
                'abiertos': 1,
                'cerrados': 4
            }],
            'por_canal': [{'canal': 'WhatsApp', 'total': 10}],
            'tendencia_mensual': [{
                'label': '2023-01',
                'total': 2,
                'abiertos': 1,
                'cerrados': 1
            }],
            'tendencia_semanal': [{
                'label': '2023-01-01',
                'total': 1,
                'abiertos': 1,
                'cerrados': 0
            }],
            'tiempos_respuesta': {
                'promedio_horas': 2.0,
                'mediana_horas': 2.0,
                'minimo_horas': 2.0,
                'maximo_horas': 2.0,
                'porcentaje_24h': 80.0
            },
            'tiempos_cierre': {
                'promedio_horas': 48.0,
                'mediana_horas': 48.0,
                'minimo_horas': 48.0,
                'maximo_horas': 48.0,
                'porcentaje_24h': 0.0
            },
            'backlog': {'menos_72h': 2, 'entre_3_y_7_dias': 1, 'mas_7_dias': 1},
            'geolocalizacion': {
                'con_coordenadas': 3,
                'por_categoria': [{'categoria': 'Baches', 'total': 3}]
            },
            'satisfaccion': {
                'promedio': 4.7,
                'respuestas': 3,
                'distribucion': [{'puntuacion': 5, 'total': 2}]
            },
            'sugerencias': {
                'total': 2,
                'por_estado': [{'estado': 'nueva', 'total': 1}],
                'por_categoria': [{'categoria': 'Transporte', 'total': 1}],
                'tendencia_mensual': [{'label': '2023-01', 'total': 2}],
            },
        }

        build_mock = MagicMock(return_value=expected_payload)
        with patch('routes.municipal_legacy.jsonify', lambda x: x), \
             patch('routes.municipal_legacy.build_stats_for_municipio', build_mock):
            view = getattr(self.module.municipal_stats, '__wrapped__', self.module.municipal_stats)
            app = Flask(__name__)
            with app.test_request_context('/municipal/stats'):
                resp = view(make_user())

        build_mock.assert_called_once()
        args, kwargs = build_mock.call_args
        self.assertEqual(args[0], 1)
        self.assertNotIn('filters', kwargs)
        self.assertEqual(resp, expected_payload)

    def test_stats_with_filters_query(self):
        build_mock = MagicMock(return_value={"resumen": {}})

        with patch('routes.municipal_legacy.jsonify', lambda x: x), \
             patch('routes.municipal_legacy.build_stats_for_municipio', build_mock):
            view = getattr(self.module.municipal_stats, '__wrapped__', self.module.municipal_stats)
            app = Flask(__name__)
            with app.test_request_context(
                '/municipal/stats?estado=cerrado&categoria=Alumbrado&fecha_inicio=2024-01-01&fecha_fin=2024-01-31'
            ):
                resp = view(make_user())

        args, kwargs = build_mock.call_args
        self.assertEqual(args[0], 1)
        filtros: StatsFilters = kwargs.get('filters')
        self.assertIsInstance(filtros, StatsFilters)
        self.assertEqual(filtros.estados, ('cerrado',))
        self.assertEqual(filtros.categorias, ('Alumbrado',))
        self.assertIsNotNone(filtros.fecha_inicio)
        self.assertIsNotNone(filtros.fecha_fin)
        self.assertEqual(resp, {"resumen": {}})

    @patch('routes.municipal_legacy._build_stats_excel', return_value=b'EXCEL_BYTES')
    @patch('routes.municipal_legacy.build_stats_for_municipio', return_value={"resumen": {}})
    def test_municipal_stats_export_excel(self, mock_build_stats, mock_build_excel):
        with patch('routes.municipal_legacy._format_filters_for_export', return_value=[('Filtros', 'Ninguno')]):
            view = getattr(self.module.municipal_stats_export, '__wrapped__', self.module.municipal_stats_export)
            app = Flask(__name__)
            with app.test_request_context('/municipal/stats/export/excel'):
                response = view(make_user(), 'excel')

        mock_build_stats.assert_called_once()
        mock_build_excel.assert_called_once()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.mimetype,
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        self.assertIn('attachment; filename=', response.headers.get('Content-Disposition', ''))
        response.direct_passthrough = False
        self.assertEqual(response.get_data(), b'EXCEL_BYTES')

    @patch('routes.municipal_legacy._build_stats_pdf', return_value=b'PDF_BYTES')
    @patch('routes.municipal_legacy.build_stats_for_municipio', return_value={"resumen": {}})
    def test_municipal_stats_export_pdf(self, mock_build_stats, mock_build_pdf):
        with patch('routes.municipal_legacy._format_filters_for_export', return_value=[('Filtros', 'Ninguno')]):
            view = getattr(self.module.municipal_stats_export, '__wrapped__', self.module.municipal_stats_export)
            app = Flask(__name__)
            with app.test_request_context('/municipal/stats/export/pdf'):
                response = view(make_user(), 'pdf')

        mock_build_stats.assert_called_once()
        mock_build_pdf.assert_called_once()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'application/pdf')
        self.assertIn('attachment; filename=', response.headers.get('Content-Disposition', ''))
        response.direct_passthrough = False
        self.assertEqual(response.get_data(), b'PDF_BYTES')

    @patch('routes.municipal_legacy._build_stats_excel', return_value=b'EXCEL_BYTES')
    @patch('routes.municipal_legacy._municipal_message_metrics', return_value={'cards': [], 'summary': {}})
    @patch('routes.municipal_legacy.build_stats_for_municipio', return_value={"resumen": {}})
    def test_municipal_analytics_export_excel(self, mock_build_stats, mock_metrics, mock_build_excel):
        with patch('routes.municipal_legacy._format_filters_for_export', return_value=[('Filtros', 'Ninguno')]):
            view = getattr(self.module.municipal_analytics_export, '__wrapped__', self.module.municipal_analytics_export)
            app = Flask(__name__)
            with app.test_request_context('/municipal/analytics/export/excel'):
                response = view(make_user(), 'excel')

        mock_build_stats.assert_called_once()
        mock_metrics.assert_called_once()
        mock_build_excel.assert_called_once()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.mimetype,
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        self.assertIn('attachment; filename=', response.headers.get('Content-Disposition', ''))

    def test_municipal_stats_export_invalid_format(self):
        view = getattr(self.module.municipal_stats_export, '__wrapped__', self.module.municipal_stats_export)
        app = Flask(__name__)
        with patch('routes.municipal_legacy.build_stats_for_municipio', return_value={"resumen": {}}), \
             patch('routes.municipal_legacy._format_filters_for_export', return_value=[('Filtros', 'Ninguno')]):
            with app.test_request_context('/municipal/stats/export/csv'):
                raw_response = view(make_user(), 'csv')

        response = app.make_response(raw_response)
        self.assertEqual(response.status_code, 400)

    def test_municipal_analytics_combines_stats_and_metrics(self):
        stats_payload = {'resumen': {'total': 0}}
        metrics_payload = {'cards': [], 'summary': {}}

        with patch('routes.municipal_legacy.jsonify', lambda x: x), \
             patch('routes.municipal_legacy.build_stats_for_municipio', return_value=stats_payload) as build_mock, \
             patch('routes.municipal_legacy._municipal_message_metrics', return_value=metrics_payload) as metrics_mock:
            view = getattr(self.module.municipal_analytics, '__wrapped__', self.module.municipal_analytics)
            app = Flask(__name__)
            with app.test_request_context('/municipal/analytics?estado=nuevo'):
                resp = view(make_user())

        args, kwargs = build_mock.call_args
        self.assertEqual(args[0], 1)
        filtros = kwargs.get('filters')
        self.assertIsInstance(filtros, StatsFilters)
        self.assertEqual(filtros.estados, ('nuevo',))

        metrics_args, metrics_kwargs = metrics_mock.call_args
        self.assertEqual(metrics_args[0], 1)
        # fecha_inicio/fin may be None when not provided explicitly
        self.assertIn('fecha_inicio', metrics_kwargs)
        self.assertIn('fecha_fin', metrics_kwargs)

        self.assertEqual(resp["stats"], stats_payload)
        self.assertEqual(resp["metrics"], metrics_payload)
        self.assertEqual(resp["cards"], metrics_payload["cards"])
        self.assertEqual(resp["summary"], metrics_payload["summary"])

    def test_stats_filters_defaults_when_no_municipio(self):
        with patch('routes.municipal_legacy.jsonify', lambda x: x):
            view = getattr(self.module.municipal_stats_filters, '__wrapped__', self.module.municipal_stats_filters)
            app = Flask(__name__)
            with app.test_request_context('/municipal/stats/filters'):
                resp = view(SimpleNamespace(municipio_id=None))

        self.assertEqual(resp['categorias'], [])
        self.assertIn('estados', resp)
        self.assertIn('rangos', resp)
        self.assertEqual(resp['agentes'], [])

    def test_stats_filters_dynamic_values(self):
        class QueryStub:
            def __init__(self, values, wrap_tuple: bool = True):
                if wrap_tuple:
                    self._values = [(value,) for value in values]
                else:
                    self._values = list(values)

            def filter(self, *args, **kwargs):
                return self

            def distinct(self):
                return self

            def all(self):
                return list(self._values)

            def __iter__(self):
                return iter(self._values)

        categories_stub = QueryStub(["Alumbrado", "Limpieza"])
        districts_stub = QueryStub(["Centro"])
        channels_stub = QueryStub(["WhatsApp", "Web"])
        agents_stub = QueryStub(
            [
                SimpleNamespace(id=10, name="Ana", email="ana@example.com", rol="admin"),
                SimpleNamespace(
                    id=11, name="Luis", email="luis@example.com", rol="empleado"
                ),
            ],
            wrap_tuple=False,
        )

        with patch('routes.municipal_legacy.jsonify', lambda x: x), \
             patch('routes.municipal_legacy.db.session.query', side_effect=[categories_stub, districts_stub, channels_stub, agents_stub]):
            view = getattr(self.module.municipal_stats_filters, '__wrapped__', self.module.municipal_stats_filters)
            app = Flask(__name__)
            with app.test_request_context('/municipal/stats/filters'):
                resp = view(make_user())

        self.assertEqual(resp['categorias'], ['Alumbrado', 'Limpieza'])
        self.assertEqual(resp['distritos'], ['Centro'])
        self.assertEqual(resp['canales'], ['Web', 'WhatsApp'])
        self.assertIn('estados', resp)
        self.assertEqual(
            resp['agentes'],
            [
                {
                    'id': 10,
                    'label': 'Ana',
                    'name': 'Ana',
                    'rol': 'admin',
                    'value': 10,
                },
                {
                    'id': 11,
                    'label': 'Luis',
                    'name': 'Luis',
                    'rol': 'empleado',
                    'value': 11,
                },
            ],
        )

    def test_stats_for_employee_uses_empresa_id(self):
        employee = SimpleNamespace(
            id=99,
            municipio_id=None,
            empresa_id=77,
            rol='empleado',
            tipo_chat='municipio',
        )

        with patch('routes.municipal_legacy.jsonify', lambda x: x), \
             patch('routes.municipal_legacy.build_stats_for_municipio', return_value={"resumen": {}}) as build_mock:
            view = getattr(self.module.municipal_stats, '__wrapped__', self.module.municipal_stats)
            app = Flask(__name__)
            with app.test_request_context('/municipal/stats'):
                resp = view(employee)

        args, kwargs = build_mock.call_args
        self.assertEqual(args[0], 77)
        self.assertEqual(resp, {"resumen": {}})

    def test_resolve_current_municipio_id_prefers_owner_context(self):
        owner = SimpleNamespace(id=50, municipio_id=321, tipo_chat='municipio')
        employee = SimpleNamespace(id=3, municipio_id=None, empresa_id=None, tipo_chat='municipio')

        app = Flask(__name__)
        with app.app_context():
            g.owner_user = owner
            resolved = self.module._resolve_current_municipio_id(employee)
            g.pop('owner_user', None)

        self.assertEqual(resolved, 321)

    def test_resolve_current_municipio_id_falls_back_to_owner_id(self):
        owner = SimpleNamespace(id=88, municipio_id=None, tipo_chat='municipio')
        employee = SimpleNamespace(id=4, municipio_id=None, empresa_id=None, tipo_chat='municipio')

        app = Flask(__name__)
        with app.app_context():
            g.owner_user = owner
            resolved = self.module._resolve_current_municipio_id(employee)
            g.pop('owner_user', None)

        self.assertEqual(resolved, 88)


if __name__ == '__main__':
    unittest.main()
