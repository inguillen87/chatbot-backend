import unittest
from flask import Flask
from unittest.mock import patch

from routes.auth import obtener_token
from utils.auth_helpers import _demo_session_allowed, _widget_session_allowed

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

    def test_bearer_on_protected_json_route_does_not_parse_domain_body(self):
        with self.app.test_request_context(
            '/api/v2/tenants/junin/whatsapp/workflow-studio/drafts',
            method='POST',
            headers={'Authorization': 'Bearer abc123'},
            data='{"draft":{"large":"domain payload"}}',
            content_type='application/json',
        ):
            with patch(
                'flask.wrappers.Request.get_json',
                side_effect=AssertionError('auth_must_not_parse_domain_json'),
            ):
                self.assertEqual(obtener_token(), 'abc123')

    def test_legacy_json_token_fallback_is_size_bounded(self):
        previous_limit = self.app.config.get('AUTH_TOKEN_JSON_BODY_MAX_BYTES')
        self.app.config['AUTH_TOKEN_JSON_BODY_MAX_BYTES'] = 1024
        try:
            with self.app.test_request_context(
                '/api/public/tenant',
                method='POST',
                data='{"token":"abc123","padding":"' + ('x' * 2048) + '"}',
                content_type='application/json',
            ):
                with patch(
                    'flask.wrappers.Request.get_json',
                    side_effect=AssertionError('oversized_auth_json_must_not_parse'),
                ):
                    self.assertIsNone(obtener_token())
        finally:
            if previous_limit is None:
                self.app.config.pop('AUTH_TOKEN_JSON_BODY_MAX_BYTES', None)
            else:
                self.app.config['AUTH_TOKEN_JSON_BODY_MAX_BYTES'] = previous_limit

    def test_small_legacy_json_token_fallback_remains_compatible(self):
        with self.app.test_request_context(
            '/api/public/tenant',
            method='POST',
            json={'token': 'abc123'},
        ):
            self.assertEqual(obtener_token(), 'abc123')

    def test_protected_route_without_auth_does_not_parse_domain_json(self):
        with self.app.test_request_context(
            '/api/v2/tenants/junin/whatsapp/workflow-studio/drafts',
            method='POST',
            data='{"draft":{"large":"domain payload"}}',
            content_type='application/json',
        ):
            with patch(
                'flask.wrappers.Request.get_json',
                side_effect=AssertionError('auth_must_not_parse_domain_json'),
            ):
                self.assertIsNone(obtener_token())

    def test_protected_route_without_auth_does_not_parse_form(self):
        with self.app.test_request_context(
            '/api/v2/tenants/junin/whatsapp/workflow-studio/drafts',
            method='POST',
            data={'token': 'must-not-be-read'},
        ):
            with patch(
                'flask.wrappers.Request._load_form_data',
                side_effect=AssertionError('auth_must_not_parse_domain_form'),
            ):
                self.assertIsNone(obtener_token())

    def test_auth_body_path_matching_requires_a_segment_boundary(self):
        with self.app.test_request_context(
            '/api/askevil',
            method='POST',
            data='{"entityToken":"must-not-be-read"}',
            content_type='application/json',
        ):
            with patch(
                'flask.wrappers.Request.get_json',
                side_effect=AssertionError('near_prefix_must_not_parse_json'),
            ):
                self.assertIsNone(obtener_token())

    def test_widget_and_demo_scope_prefixes_require_segment_boundaries(self):
        self.assertTrue(_widget_session_allowed('/api/ask/municipio', 'POST'))
        self.assertTrue(_widget_session_allowed('/api/market/demo/cart', 'GET'))
        self.assertFalse(_widget_session_allowed('/api/askevil', 'POST'))
        self.assertFalse(_widget_session_allowed('/api/marketplace', 'GET'))
        self.assertFalse(_widget_session_allowed('/api/widget-evil', 'GET'))

        self.assertTrue(_demo_session_allowed('/api/demo/onboarding', 'POST'))
        self.assertTrue(_demo_session_allowed('/api/public/tenant', 'GET'))
        self.assertFalse(_demo_session_allowed('/api/demolition', 'POST'))
        self.assertFalse(_demo_session_allowed('/api/publicity', 'GET'))

    def test_protected_pwa_app_route_does_not_parse_domain_json(self):
        with self.app.test_request_context(
            '/api/pwa/app/tickets',
            method='POST',
            data='{"entityToken":"must-not-be-read"}',
            content_type='application/json',
        ):
            with patch(
                'flask.wrappers.Request.get_json',
                side_effect=AssertionError('protected_pwa_json_must_remain_lazy'),
            ):
                self.assertIsNone(obtener_token())

    def test_invalid_auth_body_limits_fail_closed(self):
        previous_limit = self.app.config.get('AUTH_TOKEN_JSON_BODY_MAX_BYTES')
        try:
            for invalid_limit in (0, -1, 1024 * 1024 + 1, 'invalid'):
                with self.subTest(limit=invalid_limit):
                    self.app.config['AUTH_TOKEN_JSON_BODY_MAX_BYTES'] = invalid_limit
                    with self.app.test_request_context(
                        '/api/public/tenant',
                        method='POST',
                        data='{"token":"must-not-be-read"}',
                        content_type='application/json',
                    ):
                        with patch(
                            'flask.wrappers.Request.get_json',
                            side_effect=AssertionError('invalid_limit_must_fail_closed'),
                        ):
                            self.assertIsNone(obtener_token())
        finally:
            if previous_limit is None:
                self.app.config.pop('AUTH_TOKEN_JSON_BODY_MAX_BYTES', None)
            else:
                self.app.config['AUTH_TOKEN_JSON_BODY_MAX_BYTES'] = previous_limit

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
