import unittest
import importlib
import inspect
from types import SimpleNamespace
from unittest.mock import patch
import sys
import os

# Añadir el directorio raíz del proyecto al sys.path
project_root_municipal_metrics = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_municipal_metrics not in sys.path:
    sys.path.insert(0, project_root_municipal_metrics)

class DummySession:
    def __init__(self):
        self.calls = 0
        self.values = iter((5, 10, 20, 35, 12))

    def query(self, *args, **kwargs):
        return DummyQuery(self)


class DummyQuery:
    def __init__(self, session):
        self.session = session

    def select_from(self, *args, **kwargs):
        return self

    def join(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def scalar(self):
        self.session.calls += 1
        return next(self.session.values)

def make_db():
    return SimpleNamespace(session=DummySession(), text=lambda q: q)

def make_user():
    return SimpleNamespace(id=1, empresa_id=None, municipio_id=2, rol='admin')

class MunicipalMetricsTests(unittest.TestCase):
    def test_basic_counts(self):
        # Resolve the live module at execution time.  Other legacy-route tests
        # intentionally reload and evict this module; retaining a function
        # imported during collection would make these patches target a
        # different module instance and leak a real database dependency.
        municipal_module = importlib.import_module('routes.municipal_legacy')
        db_mock = make_db()
        with patch.object(municipal_module, 'db', db_mock), \
             patch.object(municipal_module, 'jsonify', lambda x: x):
            resp = inspect.unwrap(municipal_module.municipal_metrics)(make_user())
        self.assertEqual(resp['cards'][0]['value'], 5)
        self.assertEqual(resp['cards'][1]['value'], 10)
        self.assertEqual(resp['cards'][2]['value'], 20)
        self.assertEqual(resp['summary']['total'], 35)
        self.assertEqual(resp['summary']['filtered_total'], 12)
        self.assertEqual(db_mock.session.calls, 5)

if __name__ == '__main__':
    unittest.main()
