import unittest
from types import SimpleNamespace
from unittest.mock import patch

from routes.auth import dashboard_info

class DashboardRouteTests(unittest.TestCase):
    def test_admin_municipio_panels(self):
        user = SimpleNamespace(
            id=1,
            rol='admin',
            municipio_id=5,
            rubro=SimpleNamespace(nombre='municipios'),
            tipo_chat=None
        )
        with patch('routes.auth.es_rubro_publico', lambda r: True), \
             patch('routes.auth.jsonify', lambda x: x):
            resp = dashboard_info.__wrapped__(user)
        self.assertIn('municipio', resp['panels'])
        self.assertIn('crm', resp['panels'])
        self.assertEqual(resp['tipo_chat'], 'municipio')

if __name__ == '__main__':
    unittest.main()
