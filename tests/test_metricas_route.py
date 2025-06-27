import unittest
from types import SimpleNamespace
from unittest.mock import patch

from routes.metricas import obtener_metricas, obtener_metricas_pyme

class DummyResult:
    def __init__(self, value):
        self._value = value
    def scalar(self):
        return self._value

class DummySession:
    def __init__(self, value):
        self.value = value
        self.calls = []
    def execute(self, stmt, params=None):
        self.calls.append(params)
        return DummyResult(self.value)

def _dummy_db(value):
    return SimpleNamespace(session=DummySession(value), text=lambda q: q)

class MetricasRouteTests(unittest.TestCase):
    def test_pyme_alias_returns_same_metrics(self):
        user = SimpleNamespace(preguntas_usadas=10, id=3)
        db_mock = _dummy_db(5)
        with patch('routes.metricas.db', db_mock), \
             patch('routes.metricas.jsonify', lambda x: x):
            normal = obtener_metricas.__wrapped__(user)
            alias = obtener_metricas_pyme.__wrapped__(user)
        self.assertEqual(normal, alias)
        self.assertEqual(normal[0]['value'], 10)
        self.assertEqual(normal[1]['value'], 5)
        self.assertEqual(db_mock.session.calls[0]['uid'], 3)

if __name__ == '__main__':
    unittest.main()
