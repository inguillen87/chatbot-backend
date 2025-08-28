import unittest
from types import SimpleNamespace
from unittest.mock import patch
import sys
import os

# Añadir el directorio raíz del proyecto al sys.path
project_root_municipal_stats = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_municipal_stats not in sys.path:
    sys.path.insert(0, project_root_municipal_stats)

from routes.municipal_legacy import municipal_stats

class DummySession:
    def __init__(self):
        self.calls = 0
    def execute(self, *a, **k):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(scalar=lambda: 5)
        if self.calls == 2:
            return SimpleNamespace(scalar=lambda: 3)
        if self.calls == 3:
            rows = [
                SimpleNamespace(categoria='Baches', abiertos=2, cerrados=1),
                SimpleNamespace(categoria='Luces', abiertos=1, cerrados=2),
            ]
            return SimpleNamespace(fetchall=lambda: rows)
        if self.calls == 4:
            rows = [
                SimpleNamespace(distrito='Centro', total=3),
                SimpleNamespace(distrito='Sur', total=1),
            ]
            return SimpleNamespace(fetchall=lambda: rows)
        if self.calls == 5:
            rows = [
                SimpleNamespace(mes='2023-01', total=2),
                SimpleNamespace(mes='2023-02', total=3),
            ]
            return SimpleNamespace(fetchall=lambda: rows)
        if self.calls == 6:
            return SimpleNamespace(scalar=lambda: 120.0)
        return SimpleNamespace()

def make_user():
    return SimpleNamespace(municipio_id=1, empresa_id=None, rol='admin')

class MunicipalStatsTests(unittest.TestCase):
    def test_basic_stats(self):
        session = DummySession()
        with patch('routes.municipal_legacy.jsonify', lambda x: x), \
             patch('routes.municipal_legacy.db', SimpleNamespace(session=session)):
            resp = municipal_stats.__wrapped__(make_user())
        self.assertEqual(resp['totales']['abiertos'], 5)
        self.assertEqual(resp['totales']['cerrados'], 3)
        self.assertEqual(len(resp['por_categoria']), 2)
        self.assertEqual(len(resp['por_distrito']), 2)
        self.assertEqual(len(resp['por_mes']), 2)
        self.assertEqual(resp['tiempo_respuesta_promedio_segundos'], 120.0)

if __name__ == '__main__':
    unittest.main()
