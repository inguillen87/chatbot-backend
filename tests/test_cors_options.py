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
            'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'Content-Type, Idempotency-Key',
        })
        self.assertEqual(resp.status_code, 204)
        self.assertIn('Access-Control-Allow-Origin', resp.headers)
        self.assertIn(
            'Idempotency-Key',
            resp.headers.get('Access-Control-Allow-Headers', ''),
        )

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

    def test_local_vite_origin_allowed_for_pwa_tenant_info(self):
        origin = 'http://127.0.0.1:4174'
        resp = self.client.options('/api/pwa/public/tenant-info?tenant=junin', headers={
            'Origin': origin,
            'Access-Control-Request-Method': 'GET',
            'Access-Control-Request-Headers': 'x-tenant,x-tenant-slug'
        })
        self.assertIn(resp.status_code, {200, 204})
        self.assertEqual(resp.headers.get('Access-Control-Allow-Origin'), origin)
        self.assertEqual(resp.headers.getlist('Access-Control-Allow-Origin'), [origin])

    def test_api_me_cors_origin_is_not_duplicated(self):
        origin = 'http://127.0.0.1:4174'
        resp = self.client.options('/api/me?tenant_slug=junin&tenant=junin', headers={
            'Origin': origin,
            'Access-Control-Request-Method': 'GET',
            'Access-Control-Request-Headers': 'authorization,x-tenant,x-tenant-slug'
        })
        self.assertIn(resp.status_code, {200, 204})
        self.assertEqual(resp.headers.get('Access-Control-Allow-Origin'), origin)
        self.assertEqual(resp.headers.getlist('Access-Control-Allow-Origin'), [origin])

if __name__ == '__main__':
    unittest.main()
