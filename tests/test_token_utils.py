import unittest
from flask import Flask
from unittest.mock import patch

from routes.auth import obtener_token

class TokenExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="testing-secret",
            AUTH_TOKEN_COOKIE_NAME="auth_token",
            WIDGET_TOKEN_COOKIE_NAME="widget_token",
        )
        cls.static_owner_lookup = patch(
            'utils.auth_helpers._lookup_owner_for_static_token',
            return_value=None,
        )
        cls.static_owner_lookup.start()

    @classmethod
    def tearDownClass(cls):
        cls.static_owner_lookup.stop()

    def test_authorization_without_bearer(self):
        with self.app.test_request_context('/', headers={'Authorization': 'abc123'}):
            self.assertEqual(obtener_token(), 'abc123')

    def test_authorization_with_bearer(self):
        with self.app.test_request_context('/', headers={'Authorization': 'Bearer abc123'}):
            self.assertEqual(obtener_token(), 'abc123')

    def test_token_from_cookie(self):
        cookie_name = self.app.config.get('AUTH_TOKEN_COOKIE_NAME', 'auth_token')
        headers = {'Cookie': f'{cookie_name}=abc123'}
        with self.app.test_request_context('/', headers=headers):
            self.assertEqual(obtener_token(), 'abc123')

    def test_token_from_widget_cookie(self):
        widget_cookie = self.app.config.get('WIDGET_TOKEN_COOKIE_NAME', 'widget_token')
        headers = {'Cookie': f'{widget_cookie}=widget123'}
        with self.app.test_request_context('/', headers=headers):
            self.assertEqual(obtener_token(), 'widget123')

    def test_panel_cookie_is_available_on_protected_profile_from_public_site(self):
        headers = {
            'Cookie': 'auth_token=panel-session',
            'Origin': 'https://www.chatboc.ar',
        }
        with self.app.test_request_context('/api/me', headers=headers):
            self.assertEqual(obtener_token(), 'panel-session')

    def test_panel_cookie_is_ignored_on_all_public_widget_aliases(self):
        paths = (
            '/ask/municipio',
            '/api/ask/municipio',
            '/public/tenant',
            '/api/public/tenant',
            '/widget/config',
            '/api/widget/config',
            '/auth/widget-token',
            '/api/auth/widget/token',
            '/pwa/tenant-info',
            '/api/pwa/tenant-info',
            '/api/pwa/anon-id',
            '/api/pwa/public/junin/catalog',
            '/api/pwa/kits/junin',
        )
        for path in paths:
            with self.subTest(path=path):
                with self.app.test_request_context(
                    path,
                    headers={
                        'Cookie': 'auth_token=panel-session',
                        'Origin': 'https://www.chatboc.ar',
                    },
                ):
                    self.assertIsNone(obtener_token())

    def test_panel_cookie_is_available_on_protected_widget_and_pwa_routes(self):
        paths = (
            '/widget-settings',
            '/api/pwa/app/me/tenants',
            '/api/pwa/app/tickets',
        )
        for path in paths:
            with self.subTest(path=path):
                with self.app.test_request_context(
                    path,
                    headers={'Cookie': 'auth_token=panel-session'},
                ):
                    self.assertEqual(obtener_token(), 'panel-session')

    def test_public_widget_uses_explicit_entity_token_instead_of_panel_cookie(self):
        headers = {
            'Cookie': 'auth_token=panel-session',
            'X-Entity-Token': 'tenant-widget-token',
        }
        with self.app.test_request_context('/api/public/tenant', headers=headers):
            self.assertEqual(obtener_token(), 'tenant-widget-token')

    def test_public_widget_keeps_separate_widget_cookie(self):
        headers = {
            'Cookie': 'auth_token=panel-session; widget_token=widget-session',
        }
        with self.app.test_request_context('/widget/config', headers=headers):
            self.assertEqual(obtener_token(), 'widget-session')

if __name__ == '__main__':
    unittest.main()
