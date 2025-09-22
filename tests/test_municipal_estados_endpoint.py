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
        self.assertEqual(
            response.headers.get('Access-Control-Allow-Origin'),
            'http://localhost:8080',
        )
        vary_header = response.headers.get('Vary', '')
        vary_values = {value.strip() for value in vary_header.split(',') if value.strip()}
        self.assertIn('Origin', vary_values)
        self.assertEqual(response.headers.get('Access-Control-Allow-Credentials'), 'true')

        allow_methods_header = response.headers.get('Access-Control-Allow-Methods', '')
        allow_methods = {m.strip().upper() for m in allow_methods_header.split(',') if m.strip()}
        self.assertIn('GET', allow_methods)
        self.assertIn('OPTIONS', allow_methods)

        allow_headers_header = response.headers.get('Access-Control-Allow-Headers', '')
        allow_headers = {h.strip() for h in allow_headers_header.split(',') if h.strip()}
        self.assertIn('Origin', allow_headers)
        self.assertIn('Content-Type', allow_headers)
        self.assertIn('X-Entity-Token', allow_headers)

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
        self.assertEqual(
            response.headers.get('Access-Control-Allow-Origin'),
            'http://localhost:8080',
        )
        vary_header = response.headers.get('Vary', '')
        vary_values = {value.strip() for value in vary_header.split(',') if value.strip()}
        self.assertIn('Origin', vary_values)
        self.assertEqual(response.headers.get('Access-Control-Allow-Credentials'), 'true')

        allow_methods_header = response.headers.get('Access-Control-Allow-Methods', '')
        allow_methods = {m.strip().upper() for m in allow_methods_header.split(',') if m.strip()}
        self.assertIn('GET', allow_methods)
        self.assertIn('OPTIONS', allow_methods)

        allow_headers_header = response.headers.get('Access-Control-Allow-Headers', '')
        allow_headers = {h.strip() for h in allow_headers_header.split(',') if h.strip()}
        self.assertIn('Origin', allow_headers)
        self.assertIn('Content-Type', allow_headers)
        self.assertIn('X-Entity-Token', allow_headers)

    def test_disallowed_origin_receives_no_cors_headers(self):
        response = self.client.get(
            '/municipal/estados',
            headers={'Origin': 'https://malicious.example'}
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get('Access-Control-Allow-Origin'))
        self.assertIsNone(response.headers.get('Access-Control-Allow-Credentials'))


if __name__ == '__main__':
    unittest.main()
