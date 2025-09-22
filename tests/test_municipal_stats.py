import importlib
import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask

# Añadir el directorio raíz del proyecto al sys.path
project_root_municipal_stats = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..')
)
if project_root_municipal_stats not in sys.path:
    sys.path.insert(0, project_root_municipal_stats)


def make_user():
    return SimpleNamespace(municipio_id=1, empresa_id=None, rol='admin')


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

        build_mock.assert_called_once_with(1)
        self.assertEqual(resp, expected_payload)


if __name__ == '__main__':
    unittest.main()
