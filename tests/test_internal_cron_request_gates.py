"""Full application cron admission: no generic identity/tenant I/O before gates.

Run in the disposable profile acceptance runtime. Credentials, query terminals
and worker import guards are synthetic; routing, middleware and cron gates are
the application originals. No tables or external database are used.
"""
from contextlib import ExitStack
import builtins
import unittest
from unittest.mock import patch

from flask import g
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

import middleware.tenant_context as tenant_context
from models import TenantProfile
import routes.internal_cron as cron_routes
import utils.auth_helpers as auth_helpers


CRONS = (
    ('outbox-reconciliation', 'outbox_reconciliation', 'vercel_outbox_cron_disabled'),
    ('whatsapp-payload-retention', 'whatsapp_payload_retention', 'vercel_maintenance_crons_disabled'),
    ('survey-privacy-retention', 'survey_privacy_retention', 'vercel_maintenance_crons_disabled'),
    ('weekly-analytics-report', 'weekly_analytics_report', 'vercel_weekly_analytics_cron_disabled'),
)
SECRET = 'synthetic-cron-gate-test-only-' + ('x' * 32)
WORKER_MODULES = frozenset({
    'services.outbox_reconciliation', 'services.whatsapp_inbound_worker',
    'services.survey_privacy', 'services.weekly_analytics_reports',
})


class _TenantQuery:
    def __init__(self):
        self.terminals = []
        self.error = None

    def filter(self, *args):
        return self

    def order_by(self, *args):
        return self

    def limit(self, *args):
        return self

    def first(self):
        return self._finish('first')

    def all(self):
        return self._finish('all')

    def _finish(self, kind):
        self.terminals.append(kind)
        if self.error is not None:
            raise self.error
        return [] if kind == 'all' else None


class InternalCronRequestGatesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import create_app, db
        import app as app_module
        from config import TestingConfig

        class OfflineConfig(TestingConfig):
            SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
            ENABLE_RUNTIME_SCHEMA_SYNC = False
            ENABLE_RUNTIME_TENANT_INIT = False
            SKIP_INIT_TENANTS = True
            PROPAGATE_EXCEPTIONS = False
            CUTOVER_WRITER_FENCE_ENABLED = False
            CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED = False
            VERCEL_OUTBOX_CRON_ENABLED = False
            VERCEL_MAINTENANCE_CRONS_ENABLED = False
            VERCEL_WEEKLY_ANALYTICS_CRON_ENABLED = False
            PUBLIC_CATALOG_DEFAULT_TENANT = None
            CRON_SECRET = SECRET

        with patch.object(db, 'create_all'):
            cls.app = create_app(OfflineConfig)
        cls.db = db
        cls.app_module = app_module

        # Deliberately map an original view under a different URL/endpoint.
        # Generic middleware must not exempt arbitrary aliases or prefixes.
        cls.app.add_url_rule('/api/auth/cron-gate-control',
                             endpoint='fixture_cron_alias',
                             view_func=cron_routes.outbox_reconciliation,
                             methods=['GET'])
        cls.app.add_url_rule('/api/auth/cron-gate-same-endpoint-control',
                             endpoint='internal_cron.outbox_reconciliation',
                             view_func=cron_routes.outbox_reconciliation,
                             methods=['GET'])

        @cls.app.after_request
        def observe_context(response):
            cls.last_context = {name: getattr(g, name, None) for name in (
                'viewer', 'current_user', 'token_payload',
                'auth_credential_source', 'explicit_bearer_present',
                'contact_identity', 'tenant_profile', 'tenant_profile_slug',
                'current_tenant', 'current_tenant_slug', 'tenant_slug', 'tenant',
            )}
            return response

    def setUp(self):
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.app.config.update(CUTOVER_WRITER_FENCE_ENABLED=False,
                               CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED=False,
                               CRON_SECRET=SECRET)
        self.query = _TenantQuery()
        self.stack.enter_context(patch.object(TenantProfile, 'query', self.query))
        self.user = self.stack.enter_context(patch.object(auth_helpers, 'user_from_token', return_value=None))
        # Ordinary negative-control Bearers still exercise obtener_token, but
        # static-owner schema inspection must not execute SQL in this fixture.
        self.static_owner = self.stack.enter_context(patch.object(
            auth_helpers, '_lookup_owner_for_static_token', return_value=None))
        self.contact = self.stack.enter_context(patch.object(
            self.app_module, 'resolve_contact_identity_from_request', return_value=None))
        self.fallback = self.stack.enter_context(patch.object(
            tenant_context, '_fallback_default_tenant',
            side_effect=AssertionError('No cron default tenant fallback')))
        self.auth_gate = self.stack.enter_context(patch.object(
            cron_routes, '_has_valid_cron_authorization', wraps=cron_routes._has_valid_cron_authorization))
        self.activation_gates = [self.stack.enter_context(patch.object(
            cron_routes, name, wraps=getattr(cron_routes, name))) for name in (
                '_outbox_cron_is_enabled', '_maintenance_crons_are_enabled',
                '_weekly_analytics_cron_is_enabled')]
        self.sql_attempts = []

        def reject_sql(*args):
            self.sql_attempts.append('forbidden_sql')
            raise AssertionError('This suite must never execute SQL')

        event.listen(self.db.engine, 'before_cursor_execute', reject_sql)
        self.addCleanup(event.remove, self.db.engine, 'before_cursor_execute', reject_sql)
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name in WORKER_MODULES:
                raise AssertionError('A closed cron gate must not import a worker')
            return original_import(name, *args, **kwargs)

        self.stack.enter_context(patch.object(builtins, '__import__', side_effect=guarded_import))
        self.client = self.app.test_client()

    def request_cron(self, slug, *, method='GET', authorization=None, query=''):
        headers = {'X-Tenant': 'ignored-cron-hint'}
        if authorization is not None:
            headers['Authorization'] = authorization
        return self.client.open('/api/internal/cron/' + slug + query,
                                method=method, headers=headers)

    def assert_no_generic_resolution(self):
        self.assertEqual(self.query.terminals, [])
        self.user.assert_not_called()
        self.static_owner.assert_not_called()
        self.contact.assert_not_called()
        self.fallback.assert_not_called()
        self.assertEqual(self.sql_attempts, [])

    def assert_unauthorized(self, response):
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json(), {
            'contract_version': 'internal.cron.authorization.v1',
            'status': 'unauthorized',
        })
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_missing_and_invalid_credentials_reach_original_401_without_generic_io(self):
        for slug, _, _ in CRONS:
            for authorization in (None, 'Bearer wrong', 'Basic synthetic', 'Bearer ' + SECRET + '-wrong'):
                with self.subTest(cron=slug, authorization_kind=str(authorization)[:10]):
                    self.assert_unauthorized(self.request_cron(slug, authorization=authorization))
                    self.assert_no_generic_resolution()

    def test_dependency_failure_cannot_preempt_independent_cron_authorization(self):
        self.query.error = OperationalError('synthetic', {}, Exception('dependency unavailable'))
        for slug, _, _ in CRONS:
            with self.subTest(cron=slug):
                self.assert_unauthorized(self.request_cron(slug))
                self.assert_no_generic_resolution()

    def test_valid_cron_secret_reaches_disabled_gate_without_generic_io(self):
        for slug, _, reason in CRONS:
            with self.subTest(cron=slug):
                response = self.request_cron(slug, authorization='Bearer ' + SECRET)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.get_json(), {
                    'contract_version': 'internal.cron.activation.v1',
                    'executed': False, 'reason_code': reason, 'status': 'disabled',
                })
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assert_no_generic_resolution()

    def test_missing_or_short_configured_secret_still_fails_closed(self):
        for secret in (None, '', 'short', b'x' * 32):
            self.app.config['CRON_SECRET'] = secret
            with self.subTest(secret_type=type(secret).__name__):
                self.assert_unauthorized(self.request_cron(CRONS[0][0], authorization='Bearer ' + SECRET))
                self.assert_no_generic_resolution()

    def test_registered_head_and_automatic_options_do_not_resolve_generic_identity(self):
        for slug, _, _ in CRONS:
            with self.subTest(cron=slug, method='HEAD'):
                response = self.request_cron(slug, method='HEAD')
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.get_data(), b'')
                self.assert_no_generic_resolution()
            with self.subTest(cron=slug, method='OPTIONS'):
                response = self.request_cron(slug, method='OPTIONS', authorization='Bearer wrong')
                self.assertEqual(response.status_code, 200)
                self.assertIn('GET', response.headers['Allow'])
                self.assert_no_generic_resolution()

    def test_cutover_fence_precedes_cron_and_generic_gates(self):
        self.app.config['CUTOVER_WRITER_FENCE_ENABLED'] = True
        for slug, _, _ in CRONS:
            with self.subTest(cron=slug):
                response = self.request_cron(slug, authorization='Bearer ' + SECRET)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.get_json()['reason_code'], 'cutover_writer_fence_enabled')
                self.assertEqual(response.headers['Retry-After'], '60')
                self.auth_gate.assert_not_called()
                for activation in self.activation_gates:
                    activation.assert_not_called()
                self.assert_no_generic_resolution()

    def test_existing_blueprint_fence_still_rejects_automatic_options(self):
        self.app.config['CUTOVER_WRITER_FENCE_ENABLED'] = True
        for slug, _, _ in CRONS:
            with self.subTest(cron=slug):
                response = self.request_cron(slug, method='OPTIONS')
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.get_json()['reason_code'], 'cutover_writer_fence_enabled')
                self.assertEqual(response.headers['Retry-After'], '60')
                self.auth_gate.assert_not_called()
                self.assert_no_generic_resolution()

    def test_query_hints_and_reused_context_cannot_attach_retired_actor_or_tenant(self):
        for key in ('viewer', 'current_user', 'contact_identity', 'tenant_profile',
                    'tenant_profile_slug', 'current_tenant', 'current_tenant_slug',
                    'tenant_slug', 'tenant', '_login_user'):
            setattr(g, key, 'retired-synthetic-context')
        g.token_payload = {'retired': True}
        g.auth_credential_source = 'retired'
        g.explicit_bearer_present = True
        response = self.request_cron(CRONS[0][0], authorization='Bearer wrong',
                                     query='?tenant=retired&tenant_id=99')
        self.assert_unauthorized(response)
        self.assert_no_generic_resolution()
        for key, value in self.last_context.items():
            with self.subTest(context_key=key):
                expected = {} if key == 'token_payload' else False if key == 'explicit_bearer_present' else None
                self.assertEqual(value, expected)
        self.assertNotIn('_login_user', g)

    def test_arbitrary_alias_same_view_is_not_exempt(self):
        response = self.client.get('/api/auth/cron-gate-control', headers={'Authorization': 'Bearer wrong'})
        self.assert_unauthorized(response)
        self.assertGreater(len(self.query.terminals), 0)
        self.user.assert_called_once_with('wrong')
        self.contact.assert_called_once()
        self.assertEqual(self.sql_attempts, [])

    def test_alias_of_same_endpoint_is_not_exempt(self):
        response = self.client.get('/api/auth/cron-gate-same-endpoint-control',
                                   headers={'Authorization': 'Bearer wrong'})
        self.assert_unauthorized(response)
        self.assertGreater(len(self.query.terminals), 0)
        self.user.assert_called_once_with('wrong')
        self.contact.assert_called_once()
        self.assertEqual(self.sql_attempts, [])

    def test_lookalike_unknown_route_keeps_generic_resolution(self):
        response = self.client.get('/api/internal/cron/outbox-reconciliation-extra',
                                   headers={'Authorization': 'Bearer wrong'})
        self.assertEqual(response.status_code, 404)
        self.assertGreater(len(self.query.terminals), 0)
        self.user.assert_called_once_with('wrong')
        self.contact.assert_called_once()
        self.assertEqual(self.sql_attempts, [])

    def test_health_stays_dependency_free(self):
        response = self.client.get('/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'status': 'ok'})
        self.assert_no_generic_resolution()
