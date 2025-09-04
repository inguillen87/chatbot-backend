import sys
import os
import unittest

project_root_cors = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_cors not in sys.path:
    sys.path.insert(0, project_root_cors)

from app import create_app
from config import TestConfig

class CorsOptionsTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.client = self.app.test_client()

    def test_historial_options(self):
        resp = self.client.options('/historial', headers={
            'Origin': 'http://localhost:8080',
            'Access-Control-Request-Method': 'GET'
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Access-Control-Allow-Origin', resp.headers)

    def test_notifications_options(self):
        resp = self.client.options('/notifications', headers={
            'Origin': 'http://localhost:8080',
            'Access-Control-Request-Method': 'GET'
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Access-Control-Allow-Origin', resp.headers)

    def test_ask_municipio_options(self):
        resp = self.client.options('/ask/municipio', headers={
            'Origin': 'http://localhost:8080',
            'Access-Control-Request-Method': 'POST'
        })
        self.assertEqual(resp.status_code, 204)
        self.assertIn('Access-Control-Allow-Origin', resp.headers)

    def test_options_allows_anon_id_header(self):
        resp = self.client.options('/perfil', headers={
            'Origin': 'http://localhost:8080',
            'Access-Control-Request-Method': 'GET',
            'Access-Control-Request-Headers': 'Anon-Id'
        })
        self.assertEqual(resp.status_code, 204)
        allow_headers = resp.headers.get('Access-Control-Allow-Headers', '')
        self.assertIn('Anon-Id', allow_headers)

    def test_options_allows_token_header(self):
        resp = self.client.options('/perfil', headers={
            'Origin': 'http://localhost:8080',
            'Access-Control-Request-Method': 'GET',
            'Access-Control-Request-Headers': 'token'
        })
        self.assertEqual(resp.status_code, 204)
        allow_headers = resp.headers.get('Access-Control-Allow-Headers', '')
        self.assertIn('token', allow_headers.lower())

    def test_perfil_options(self):
        resp = self.client.options('/auth/perfil', headers={
            'Origin': 'http://localhost:8080',
            'Access-Control-Request-Method': 'PUT'
        })
        self.assertEqual(resp.status_code, 204)
        self.assertIn('Access-Control-Allow-Origin', resp.headers)

    def test_legacy_perfil_get_options(self):
        resp = self.client.options('/perfil', headers={
            'Origin': 'http://localhost:8080',
            'Access-Control-Request-Method': 'GET'
        })
        self.assertEqual(resp.status_code, 204)
        self.assertIn('Access-Control-Allow-Origin', resp.headers)

    def test_legacy_me_get_options(self):
        resp = self.client.options('/me', headers={
            'Origin': 'http://localhost:8080',
            'Access-Control-Request-Method': 'GET'
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Access-Control-Allow-Origin', resp.headers)

if __name__ == '__main__':
    unittest.main()
