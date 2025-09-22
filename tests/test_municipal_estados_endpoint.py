import os
import sys
import unittest

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)


from app import create_app
from config import TestConfig
from routes.ticket import TICKET_ALLOWED_STATES


class MunicipalEstadosEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.client = self.app.test_client()

    def test_get_estados_returns_allowed_states(self):
        response = self.client.get(
            '/municipal/estados',
            headers={'Origin': 'http://localhost:8080'}
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('Access-Control-Allow-Origin', response.headers)

        payload = response.get_json()
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload.get('estados'), TICKET_ALLOWED_STATES)

    def test_options_preflight_includes_cors_headers(self):
        response = self.client.options(
            '/municipal/estados',
            headers={
                'Origin': 'http://localhost:8080',
                'Access-Control-Request-Method': 'GET',
            }
        )

        self.assertEqual(response.status_code, 204)
        self.assertIn('Access-Control-Allow-Origin', response.headers)


if __name__ == '__main__':
    unittest.main()
