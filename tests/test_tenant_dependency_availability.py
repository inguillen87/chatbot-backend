"""Real Flask/blueprint/middleware boundary; database failure is controlled."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('FLASK_SKIP_GLOBAL_APP', '1')

from flask import Flask, g
from sqlalchemy.exc import DisconnectionError, OperationalError, PendingRollbackError, TimeoutError
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

import middleware.tenant_context as tenant_context
import routes.auth as auth_routes
from routes.auth import auth_api_bp, auth_bp


class TenantDependencyAvailabilityTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY='synthetic-test-key', WIDGET_JWT_ALG='HS256')
        self.seen_context = []

        @self.app.before_request
        def seed_retired_context():
            for name in ('tenant_profile', 'tenant_profile_slug', 'current_tenant',
                         'current_tenant_slug', 'tenant_slug', 'tenant'):
                setattr(g, name, 'retired-tenant')

        tenant_context.tenant_middleware(self.app)
        self.app.register_blueprint(auth_bp)
        self.app.register_blueprint(auth_api_bp)

        @self.app.after_request
        def observe_context(response):
            self.seen_context.append({name: getattr(g, name, None) for name in
                                      ('tenant_profile', 'tenant_profile_slug', 'current_tenant',
                                       'current_tenant_slug', 'tenant_slug', 'tenant')})
            return response

        self.client = self.app.test_client()

    def dependency_response(self, error, path='/auth/widget/bootstrap', request_id='qa-request-availability'):
        with patch.object(tenant_context, '_find_tenant_by_slug', side_effect=error) as lookup, \
                patch.object(tenant_context, '_fallback_default_tenant') as fallback:
            response = self.client.get(path, headers={'X-Tenant': 'qa-a', 'X-Request-Id': request_id})
        lookup.assert_called_once_with('qa-a')
        fallback.assert_not_called()
        self.assertTrue(all(value is None for value in self.seen_context[-1].values()))
        return response

    def assert_unavailable(self, response, request_id='qa-request-availability'):
        self.assertEqual(response.status_code, 503)
        payload = response.get_json()
        self.assertEqual(payload['contract_version'], 'shared.error.v1')
        self.assertEqual(payload['status_code'], 503)
        self.assertEqual(payload['reason_code'], 'tenant_context_unavailable')
        # An OperationalError alone cannot justify automated retries or a quota reset time.
        self.assertIs(payload['retryable'], False)
        self.assertEqual(payload['action_hint'], 'retry_later')
        self.assertEqual(payload['request_id'], request_id)
        self.assertEqual(response.headers['X-Request-Id'], request_id)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertNotIn('Retry-After', response.headers)
        self.assertNotIn('X-Chatboc-Bootstrap', response.headers)
        self.assertNotIn('request_dispatched', payload)
        self.assertNotIn('PRIVATE_DEPENDENCY_DIAGNOSTIC', response.get_data(as_text=True))
        self.assertNotIn('tenant', payload)

    def test_database_failure_is_availability_not_missing_tenant(self):
        error = OperationalError('SELECT synthetic', {}, Exception('PRIVATE_DEPENDENCY_DIAGNOSTIC'))
        self.assert_unavailable(self.dependency_response(error))

    def test_api_alias_preserves_the_same_availability_contract(self):
        error = OperationalError('SELECT synthetic', {}, Exception('PRIVATE_DEPENDENCY_DIAGNOSTIC'))
        self.assert_unavailable(self.dependency_response(error, '/api/auth/widget/bootstrap'))

    def test_pool_timeout_is_an_availability_failure(self):
        self.assert_unavailable(self.dependency_response(TimeoutError('PRIVATE_DEPENDENCY_DIAGNOSTIC')))

    def test_disconnection_is_an_availability_failure(self):
        self.assert_unavailable(self.dependency_response(DisconnectionError('PRIVATE_DEPENDENCY_DIAGNOSTIC')))

    def test_genuinely_unresolved_explicit_tenant_keeps_400(self):
        with patch.object(tenant_context, '_find_tenant_by_slug', return_value=None) as lookup, \
                patch.object(tenant_context, '_fallback_default_tenant') as fallback:
            response = self.client.get('/auth/widget/bootstrap', headers={'X-Tenant': 'missing'})
        lookup.assert_called_once_with('missing')
        fallback.assert_not_called()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {'error': 'tenant requerido'})
        self.assertTrue(all(value is None for value in self.seen_context[-1].values()))

    def test_a_later_resolved_request_recovers_without_reusing_retired_tenant(self):
        self.assert_unavailable(self.dependency_response(TimeoutError('synthetic timeout')))
        tenant = SimpleNamespace(id=1, slug='qa-b', nombre='Synthetic B', tipo='pyme',
                                 configuracion={}, dominio=None,
                                 to_public_dict=lambda: {'id': 1, 'slug': 'qa-b'})
        with patch.object(tenant_context, '_find_tenant_by_slug', return_value=tenant):
            response = self.client.get('/auth/widget/bootstrap', headers={'X-Tenant': 'qa-b'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['tenant']['slug'], 'qa-b')
        self.assertEqual(response.get_json()['contract_version'], 'auth.widget_bootstrap.v1')
        self.assertIs(self.seen_context[-1]['tenant_profile'], tenant)
        self.assertEqual(self.seen_context[-1]['current_tenant'], 'qa-b')

    def test_request_ids_are_per_request_when_none_was_supplied(self):
        first = self.dependency_response(TimeoutError('synthetic timeout'), request_id='')
        second = self.dependency_response(TimeoutError('synthetic timeout'), request_id='')
        self.assertEqual(first.status_code, 503)
        self.assertEqual(second.status_code, 503)
        self.assertTrue(first.get_json()['request_id'])
        self.assertNotEqual(first.get_json()['request_id'], second.get_json()['request_id'])

    def test_liveness_exemption_does_not_resolve_dependencies(self):
        self.app.add_url_rule('/health', endpoint='health', view_func=lambda: {'status': 'ok'})
        with patch.object(tenant_context, '_resolve_tenant_profile', side_effect=TimeoutError('synthetic')) as lookup:
            response = self.client.get('/health')
        self.assertEqual(response.status_code, 200)
        lookup.assert_not_called()

    def test_bootstrap_cors_preflight_does_not_require_database_availability(self):
        with patch.object(tenant_context, '_resolve_tenant_profile', side_effect=TimeoutError('synthetic')) as lookup:
            response = self.client.options('/api/auth/widget/bootstrap', headers={
                'Origin': 'https://example.invalid', 'Access-Control-Request-Method': 'GET',
            })
        self.assertEqual(response.status_code, 200)
        self.assertIn('GET', response.headers['Access-Control-Allow-Methods'])
        lookup.assert_not_called()

    def test_http_parser_rejection_is_not_reclassified_as_dependency_failure(self):
        with patch.object(tenant_context, '_resolve_tenant_profile', side_effect=RequestEntityTooLarge()):
            response = self.client.get('/auth/widget/bootstrap')
        self.assertEqual(response.status_code, 413)

    def test_process_version_exemption_does_not_resolve_dependencies(self):
        self.app.add_url_rule('/api/version', endpoint='config_bp.get_version_info', view_func=lambda: {'version': 'synthetic'})
        with patch.object(tenant_context, '_resolve_tenant_profile', side_effect=TimeoutError('synthetic')) as lookup:
            response = self.client.get('/api/version')
        self.assertEqual(response.status_code, 200)
        lookup.assert_not_called()

    def test_failure_clears_aliases_in_a_reused_application_context(self):
        tenant = SimpleNamespace(id=1, slug='qa-a', tipo='pyme', configuracion={}, dominio=None,
                                 to_public_dict=lambda: {'id': 1, 'slug': 'qa-a'})
        with self.app.app_context():
            with patch.object(tenant_context, '_find_tenant_by_slug', return_value=tenant):
                first = self.client.get('/auth/widget/bootstrap', headers={'X-Tenant': 'qa-a'})
            self.assertEqual(first.status_code, 200)
            self.assertIs(g.tenant_profile, tenant)
            with patch.object(tenant_context, '_find_tenant_by_slug', side_effect=TimeoutError('synthetic')):
                second = self.client.get('/auth/widget/bootstrap', headers={'X-Tenant': 'qa-b'})
            self.assertEqual(second.status_code, 503)
            self.assertIsNone(getattr(g, 'tenant_profile', None))
            self.assertIsNone(getattr(g, 'current_tenant', None))
            self.assertTrue(all(value is None for value in self.seen_context[-1].values()))


class ApplicationDependencyAvailabilityTest(unittest.TestCase):
    """Use create_app's actual exception handlers and registered production routes."""

    @classmethod
    def setUpClass(cls):
        from app import create_app, db
        from config import TestingConfig

        class OfflineConfig(TestingConfig):
            SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
            ENABLE_RUNTIME_SCHEMA_SYNC = False
            ENABLE_RUNTIME_TENANT_INIT = False
            SKIP_INIT_TENANTS = True
            PROPAGATE_EXCEPTIONS = False
            ENABLE_DEMO_MODE = True
            CORS_CREDENTIALS_ALLOWED_ORIGINS = ['https://panel.example.invalid']

        # No tables, external database, or account/session are created by this suite.
        with patch.object(db, 'create_all'):
            cls.app = create_app(OfflineConfig)
        cls.db = db

        @cls.app.route('/__test/dependency-failure')
        def dependency_failure():
            raise cls.app.config['SYNTHETIC_DEPENDENCY_ERROR']

    def setUp(self):
        self.lookup = patch.object(tenant_context, '_resolve_tenant_profile', return_value=None)
        self.lookup.start()
        self.addCleanup(self.lookup.stop)
        self.rollback = patch.object(self.db.session, 'rollback')
        self.rollback_mock = self.rollback.start()
        self.addCleanup(self.rollback.stop)
        self.client = self.app.test_client()

    def quota_error(self):
        # Deliberately synthetic text: no provider log, connection URI or account is read.
        return OperationalError('SELECT synthetic', {}, Exception('PRIVATE_DEPENDENCY_DIAGNOSTIC: quota exceeded'))

    def assert_database_unavailable(self, response):
        self.assertEqual(response.status_code, 503)
        payload = response.get_json()
        self.assertEqual(payload['contract_version'], 'shared.error.v1')
        self.assertEqual(payload['reason_code'], 'database_unavailable')
        self.assertIs(payload['retryable'], False)
        self.assertEqual(payload['status_code'], 503)
        self.assertEqual(payload['request_id'], response.headers['X-Request-Id'])
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertNotIn('Retry-After', response.headers)
        self.assertNotIn('X-Chatboc-Bootstrap', response.headers)
        self.assertNotIn('request_dispatched', payload)
        text = response.get_data(as_text=True)
        for private_fragment in ('PRIVATE_DEPENDENCY_DIAGNOSTIC', 'quota', 'SELECT synthetic'):
            self.assertNotIn(private_fragment, text)

    def test_real_global_handler_sanitizes_quota_operational_error(self):
        self.app.config['SYNTHETIC_DEPENDENCY_ERROR'] = self.quota_error()
        response = self.client.get('/__test/dependency-failure')
        self.assert_database_unavailable(response)

    def test_clerk_session_local_exception_guard_returns_availability(self):
        with patch.object(auth_routes, 'verify_clerk_session_token', side_effect=self.quota_error()):
            response = self.client.post('/api/auth/clerk/session', json={'clerk_token': 'synthetic-only'})
        self.assert_database_unavailable(response)

    def test_clerk_session_still_returns_availability_if_rollback_also_fails(self):
        self.rollback_mock.side_effect = TimeoutError('PRIVATE_DEPENDENCY_DIAGNOSTIC')
        with patch.object(auth_routes, 'verify_clerk_session_token', side_effect=self.quota_error()):
            response = self.client.post('/api/auth/clerk/session', json={'clerk_token': 'synthetic-only'})
        self.assert_database_unavailable(response)

    def test_real_demo_catalog_does_not_replace_dependency_failure_with_static_data(self):
        import routes.v2.demo as demo_routes
        with patch.dict(auth_routes._DEMO_CATALOG_CACHE, {'payload': None, 'expires_at': 0.0}), \
                patch.object(auth_routes, '_load_demo_rubros_cached', side_effect=self.quota_error()), \
                patch.object(auth_routes, '_first_active_tenant_for_demo') as fallback, \
                patch.object(demo_routes, '_enforce_demo_catalog_full_rate_limit', return_value=None):
            response = self.client.get('/api/v2/demo/catalog')
        self.assert_database_unavailable(response)
        fallback.assert_not_called()

    def test_business_authentication_401_is_preserved(self):
        error = auth_routes.ClerkAuthError('Synthetic invalid credential', status_code=401)
        with patch.object(auth_routes, 'verify_clerk_session_token', side_effect=error):
            response = self.client.post('/api/auth/clerk/session', json={'clerk_token': 'synthetic-only'})
        self.assertEqual(response.status_code, 401)
        self.assertNotEqual(response.get_json().get('reason_code'), 'database_unavailable')

    def test_business_tenant_denial_403_is_preserved(self):
        with patch.object(auth_routes, 'verify_clerk_session_token', side_effect=auth_routes.ClerkTenantInactive('Synthetic inactive tenant')):
            response = self.client.post('/api/auth/clerk/session', json={'clerk_token': 'synthetic-only'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['reason_code'], 'tenant_inactive')

    def test_authentication_denial_survives_rollback_failure(self):
        self.rollback_mock.side_effect = TimeoutError('synthetic rollback failure')
        error = auth_routes.ClerkAuthError('Synthetic invalid credential', status_code=401)
        with patch.object(auth_routes, 'verify_clerk_session_token', side_effect=error):
            response = self.client.post('/api/auth/clerk/session', json={'clerk_token': 'synthetic-only'})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['reason_code'], 'invalid_clerk_session')

    def test_tenant_denial_survives_rollback_failure(self):
        self.rollback_mock.side_effect = TimeoutError('synthetic rollback failure')
        with patch.object(auth_routes, 'verify_clerk_session_token', side_effect=auth_routes.ClerkTenantInactive('Synthetic inactive tenant')):
            response = self.client.post('/api/auth/clerk/session', json={'clerk_token': 'synthetic-only'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['reason_code'], 'tenant_inactive')

    def test_clerk_preflight_is_database_free_and_preserves_origin_policy(self):
        self.lookup.stop()
        for path in ('/auth/clerk/session', '/api/auth/clerk/session'):
            with self.subTest(path=path), patch.object(tenant_context, '_resolve_tenant_profile', side_effect=TimeoutError('synthetic')) as lookup:
                allowed = self.client.options(path, headers={
                    'Origin': 'https://panel.example.invalid', 'Access-Control-Request-Method': 'POST',
                })
                denied = self.client.options(path, headers={
                    'Origin': 'https://denied.example.invalid', 'Access-Control-Request-Method': 'POST',
                })
            self.assertEqual(allowed.status_code, 200)
            self.assertEqual(allowed.headers.get('Access-Control-Allow-Origin'), 'https://panel.example.invalid')
            self.assertIn('POST', allowed.headers.get('Access-Control-Allow-Methods', ''))
            self.assertEqual(denied.status_code, 200)
            self.assertNotIn('Access-Control-Allow-Origin', denied.headers)
            lookup.assert_not_called()

    def test_business_http_400_is_preserved_by_global_handler(self):
        self.app.config['SYNTHETIC_DEPENDENCY_ERROR'] = BadRequest('Synthetic invalid request')
        response = self.client.get('/__test/dependency-failure')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['reason_code'], 'bad_request')

    def test_exception_message_does_not_classify_non_dependency_errors(self):
        self.app.config['SYNTHETIC_DEPENDENCY_ERROR'] = RuntimeError('OperationalError quota exceeded')
        response = self.client.get('/__test/dependency-failure')
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()['reason_code'], 'server_error')


class WidgetTokenAvailabilityTest(unittest.TestCase):
    setUp = TenantDependencyAvailabilityTest.setUp

    class Column:
        """Only the SQL-expression boundary is synthetic; resolver and route are real."""
        def __getitem__(self, key):
            return self

        @property
        def astext(self):
            return self

        def contains(self, value):
            return self

        def is_(self, value):
            return self

    def token_response(self, outcomes):
        query = Mock()
        query.filter.return_value = query
        query.all.side_effect = outcomes
        model = SimpleNamespace(query=query, is_active=self.Column(), configuracion=self.Column())
        with patch.object(tenant_context, 'TenantProfile', model), \
                patch.object(tenant_context, '_fallback_default_tenant') as fallback:
            response = self.client.get('/api/auth/widget/bootstrap', headers={
                'X-Widget-Token': 'synthetic-issued-widget-token', 'X-Request-Id': 'qa-request-availability',
            })
        fallback.assert_not_called()
        return response, query

    def test_typed_lookup_failure_does_not_issue_a_second_query_or_become_400(self):
        error = OperationalError('SELECT synthetic', {}, Exception('PRIVATE_DEPENDENCY_DIAGNOSTIC'))
        response, query = self.token_response([error, PendingRollbackError('synthetic aborted transaction')])
        TenantDependencyAvailabilityTest.assert_unavailable(self, response)
        self.assertEqual(query.all.call_count, 1)
        self.assertTrue(all(value is None for value in self.seen_context[-1].values()))

    def test_invalid_token_keeps_existing_400_without_default_tenant(self):
        response, query = self.token_response([[], []])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {'error': 'tenant requerido'})
        self.assertTrue(all(value is None for value in self.seen_context[-1].values()))

    def test_ambiguous_token_stays_closed(self):
        first = SimpleNamespace(configuracion={'widget_tokens': ['synthetic-issued-widget-token']})
        second = SimpleNamespace(configuracion={'widget_tokens': ['synthetic-issued-widget-token']})
        response, query = self.token_response([[first, second]])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(query.all.call_count, 1)
        self.assertTrue(all(value is None for value in self.seen_context[-1].values()))

    def test_existing_non_dependency_query_compatibility_keeps_exact_token_match(self):
        tenant = SimpleNamespace(id=1, slug='qa-a', tipo='pyme', dominio=None,
                                 configuracion={'widget_tokens': ['synthetic-issued-widget-token']},
                                 to_public_dict=lambda: {'id': 1, 'slug': 'qa-a'})
        response, query = self.token_response([AttributeError('synthetic unsupported JSON expression'), [tenant]])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['tenant']['slug'], 'qa-a')
        self.assertEqual(query.all.call_count, 2)


if __name__ == '__main__':
    unittest.main()
