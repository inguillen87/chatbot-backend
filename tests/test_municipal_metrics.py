import unittest
from types import SimpleNamespace
from unittest.mock import patch
import sys
import os

# Añadir el directorio raíz del proyecto al sys.path
project_root_municipal_metrics = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_municipal_metrics not in sys.path:
    sys.path.insert(0, project_root_municipal_metrics)

from routes.municipal_legacy import municipal_metrics

class DummySession:
    def __init__(self):
        self.calls = 0
    def execute(self, stmt, params=None):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(scalar=lambda: 5)
        if self.calls == 2:
            return SimpleNamespace(scalar=lambda: 10)
        if self.calls == 3:
            return SimpleNamespace(scalar=lambda: 20)
        return SimpleNamespace(scalar=lambda: 0)

def make_db():
    return SimpleNamespace(session=DummySession(), text=lambda q: q)

def make_user():
    return SimpleNamespace(id=1, empresa_id=None, municipio_id=2, rol='admin')

class MunicipalMetricsTests(unittest.TestCase):
    def test_basic_counts(self):
        db_mock = make_db()
        with patch('routes.municipal_legacy.db', db_mock), \
             patch('routes.municipal_legacy.jsonify', lambda x: x):
            resp = municipal_metrics.__wrapped__(make_user())
        self.assertEqual(resp[0]['value'], 5)
        self.assertEqual(resp[1]['value'], 10)
        self.assertEqual(resp[2]['value'], 20)
        self.assertEqual(db_mock.session.calls, 3)

if __name__ == '__main__':
    unittest.main()
