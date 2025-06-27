import unittest
from types import SimpleNamespace
from unittest.mock import patch

from routes.estadisticas import estadisticas_reclamos

class DummySession:
    def __init__(self, results):
        self._results = list(results)
        self.calls = []
    def execute(self, query, params=None):
        self.calls.append(params)
        return self._results.pop(0)

class DummyResult:
    def __init__(self, fetch=None, scalar=None):
        self._fetch = fetch
        self._scalar = scalar
    def fetchall(self):
        return self._fetch
    def scalar(self):
        return self._scalar

class EstadisticasRouteTests(unittest.TestCase):
    def test_pyme_filters_by_rubro(self):
        rows = [SimpleNamespace(rubro='bodega', total=2)]
        session = DummySession([
            DummyResult(fetch=rows),
            DummyResult(scalar=5),
            DummyResult(scalar=120.0),
        ])
        user = SimpleNamespace(
            rubro=SimpleNamespace(nombre='bodega'),
            rubro_id=7,
            municipio_id=None,
            empresa_id=None,
            rol='admin',
        )
        with patch('routes.estadisticas.jsonify', lambda x: x), \
             patch('routes.estadisticas.db', SimpleNamespace(session=session)):
            resp = estadisticas_reclamos.__wrapped__.__wrapped__(user)
        self.assertEqual(resp['por_rubro'][0]['total'], 2)
        self.assertEqual(resp['por_tipo'][0]['total'], 5)
        self.assertEqual(resp['tiempo_respuesta_promedio_segundos']['pyme'], 120.0)
        for params in session.calls:
            if params:
                self.assertEqual(params.get('rid'), 7)

    def test_municipio_filters_by_id(self):
        session = DummySession([
            DummyResult(scalar=4),
            DummyResult(scalar=60.0),
        ])
        user = SimpleNamespace(
            rubro=SimpleNamespace(nombre='municipios'),
            rubro_id=1,
            municipio_id=3,
            empresa_id=None,
            rol='admin',
        )
        with patch('routes.estadisticas.jsonify', lambda x: x), \
             patch('routes.estadisticas.db', SimpleNamespace(session=session)):
            resp = estadisticas_reclamos.__wrapped__.__wrapped__(user)
        self.assertEqual(resp['por_rubro'], [])
        self.assertEqual(resp['por_tipo'][0]['total'], 4)
        self.assertEqual(resp['tiempo_respuesta_promedio_segundos']['municipio'], 60.0)
        for params in session.calls:
            if params:
                self.assertEqual(params.get('mid'), 3)

if __name__ == '__main__':
    unittest.main()
