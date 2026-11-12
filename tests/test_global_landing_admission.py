"""Actual app landing contract; ORM discovery is controlled and SQL forbidden.

Run in tests.profile_acceptance_runtime.prepare_process's disposable process.
No tenant content, credentials, provider requests or database are required.
"""
from contextlib import ExitStack
from types import SimpleNamespace
import os
import unittest
from unittest.mock import patch

from flask import g, request
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError


class GlobalLandingAdmissionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import create_app, db
        from config import TestingConfig
        cls.sql = []
        def forbidden_sql(*args):
            cls.sql.append('forbidden')
            raise AssertionError('Landing admission tests must not execute SQL')
        cls.forbidden_sql = forbidden_sql
        event.listen(Engine, 'before_cursor_execute', forbidden_sql)
        cls.addClassCleanup(event.remove, Engine, 'before_cursor_execute', forbidden_sql)
        class OfflineConfig(TestingConfig):
            SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
            ENABLE_RUNTIME_SCHEMA_SYNC = False
            ENABLE_RUNTIME_TENANT_INIT = False
            SKIP_INIT_TENANTS = True
            PROPAGATE_EXCEPTIONS = False
            ENABLE_DEMO_MODE = False
            CUTOVER_WRITER_FENCE_ENABLED = False
            CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED = False
        with patch.object(db, 'create_all'):
            cls.app = create_app(OfflineConfig)
        cls.db = db
        cls.contexts = []
        def seed_retired_context():
            for key in ('tenant_profile', 'tenant_profile_slug', 'current_tenant',
                        'current_tenant_slug', 'tenant_slug', 'tenant'):
                setattr(g, key, 'retired-synthetic')
        cls.app.before_request_funcs[None].insert(0, seed_retired_context)
        @cls.app.after_request
        def observe_context(response):
            cls.contexts.append({key: getattr(g, key, None) for key in
                ('tenant_profile', 'tenant_profile_slug', 'current_tenant',
                 'current_tenant_slug', 'tenant_slug', 'tenant')})
            return response

    def setUp(self):
        import middleware.tenant_context as tc
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.app.config['WIDGET_TOKEN_COOKIE_NAME'] = 'widget_token'
        self.stack.enter_context(patch.object(self.db.session, 'rollback'))
        self.stack.enter_context(patch.object(self.app.session_interface, 'save_session', return_value=None))
        # Identity-bearing negative cases exercise admission, not credential
        # validity. Keep the original authentication hooks but make their actor
        # lookup an explicit anonymous double; no token/SQL access is permitted.
        self.actor_lookup = self.stack.enter_context(patch('utils.auth_helpers.user_from_token', return_value=None))
        self.owner_lookup = self.stack.enter_context(patch('utils.auth_helpers._lookup_owner_for_static_token', return_value=None))
        self.resolver = self.stack.enter_context(patch.object(tc, '_resolve_tenant_profile',
            side_effect=OperationalError('SYNTHETIC_SQL_ONLY', {}, Exception('PRIVATE_LANDING_DIAGNOSTIC'))))
        self.client = self.app.test_client()
        self.addCleanup(self.assertEqual, self.sql, [])

    def call(self, path='/api/public/landing-experience', method='GET', host='www.chatboc.ar', **kwargs):
        headers = {'X-Request-Id': 'global-landing-synthetic'} | kwargs.pop('headers', {})
        return self.client.open(path, method=method, base_url='https://' + host, headers=headers, **kwargs)

    def assert_unavailable(self, response):
        self.assertEqual(response.status_code, 503)
        data = response.get_json()
        self.assertEqual(data['contract_version'], 'shared.error.v1')
        self.assertEqual(data['reason_code'], 'tenant_context_unavailable')
        self.assertIs(data['retryable'], False)
        self.assertEqual(data['request_id'], 'global-landing-synthetic')
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertNotIn('PRIVATE_LANDING_DIAGNOSTIC', response.get_data(as_text=True))
        self.assertTrue(all(value is None for value in self.contexts[-1].values()))

    def test_global_contract_and_preflight_on_exact_canonical_hosts_do_not_resolve(self):
        for host in ('chatboc.ar', 'www.chatboc.ar', 'api.chatboc.ar',
                     'chatboc-frontend.vercel.app', 'chatboc-backend.vercel.app'):
            for method in ('GET', 'HEAD', 'OPTIONS'):
                with self.subTest(host=host, method=method):
                    response = self.call(host=host, method=method)
                    self.assertEqual(response.status_code, 200)
                    if method == 'GET':
                        data = response.get_json()
                        self.assertEqual(data['contract_version'], 'public.landing_experience.v1')
                        self.assertEqual(data['experience_kind'], 'platform')
                        self.assertIsNone(data['tenant']['slug'])
                        self.assertFalse(data['tenant']['white_label'])
                    self.assertTrue(all(value is None for value in self.contexts[-1].values()))
        self.resolver.assert_not_called()
        self.actor_lookup.assert_not_called()
        self.owner_lookup.assert_not_called()

    def test_safe_exact_runtime_candidate_host(self):
        own = 'chatboc-backend-synthetic123-marcelos-projects-c26aa499.vercel.app'
        cases = (
            (own, own, {}, 200),
            (own, 'chatboc-backend-other123-marcelos-projects-c26aa499.vercel.app', {}, 503),
            ('synthetic.customer.invalid', 'synthetic.customer.invalid', {}, 503),
            ('chatboc-frontend-synthetic123-marcelos-projects-c26aa499.vercel.app',
             'chatboc-frontend-synthetic123-marcelos-projects-c26aa499.vercel.app', {}, 503),
            (None, own, {}, 503),
            (own, 'synthetic.customer.invalid', {'X-Forwarded-Host': own}, 503),
            (own, own, {'Origin': 'https://synthetic.customer.invalid'}, 503),
        )
        for runtime, host, headers, status in cases:
            with self.subTest(runtime=runtime, host=host, headers=headers):
                environment = {'VERCEL_URL': runtime} if runtime is not None else {}
                with patch.dict(os.environ, environment, clear=True):
                    response = self.call(host=host, headers=headers)
                self.assertEqual(response.status_code, status)
                if status == 503:self.assert_unavailable(response)

    def test_anonymous_global_contract_with_real_sqlalchemy_session_lifecycle_is_read_only(self):
        from utils.migration_managed_session import (
            MigrationManagedSqlAlchemySessionInterface, RetirementIsolatedSessionInterface,
        )
        interface = RetirementIsolatedSessionInterface(
            MigrationManagedSqlAlchemySessionInterface(self.app, self.db, cleanup_n_requests=None))
        with patch.object(self.app, 'session_interface', interface):
            response = self.call()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['experience_kind'], 'platform')
        self.assertNotIn('Set-Cookie', response.headers)
        self.resolver.assert_not_called()
        self.actor_lookup.assert_not_called()
        self.owner_lookup.assert_not_called()

    def test_page_and_canonical_proxy_hints_preserve_static_contract(self):
        response = self.call('?'.join(('/api/public/landing-experience', 'page=soluciones')),
            headers={'X-Forwarded-Host': 'www.chatboc.ar', 'X-Original-Host': 'chatboc.ar',
                     'X-Host': 'api.chatboc.ar', 'Origin': 'https://www.chatboc.ar',
                     'Referer': 'https://www.chatboc.ar/planes?utm_source=synthetic'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['selected_page'], 'soluciones')
        self.resolver.assert_not_called()

    def test_custom_host_cannot_be_authorized_by_canonical_origin_or_forwarded_host(self):
        for headers in ({}, {'Origin': 'https://www.chatboc.ar'}, {'X-Forwarded-Host': 'www.chatboc.ar'}):
            with self.subTest(headers=headers):
                self.assert_unavailable(self.call(host='synthetic.customer.invalid', headers=headers))
        self.assertEqual(self.resolver.call_count, 3)

    def test_conflicting_or_malformed_host_hints_do_not_bypass_discovery(self):
        for header, value in (
            ('X-Forwarded-Host', 'synthetic.customer.invalid'),
            ('X-Original-Host', 'synthetic.customer.invalid'), ('X-Host', 'synthetic.customer.invalid'),
            ('Origin', 'https://synthetic.customer.invalid'), ('Referer', 'https://synthetic.customer.invalid/'),
            ('X-Forwarded-Host', 'www.chatboc.ar,synthetic.customer.invalid'),
            ('X-Forwarded-Host', 'www.chatboc.ar:444'), ('X-Host', 'user@www.chatboc.ar'),
            ('Origin', 'null'), ('X-Host', 'https://www.chatboc.ar'), ('X-Original-Host', ''),
        ):
            with self.subTest(header=header, value=value):
                self.assert_unavailable(self.call(headers={header: value}))

    def test_all_tenant_and_widget_query_selectors_keep_tenant_discovery(self):
        keys = ('tenant_slug', 'tenant', 'slug', 'tenant_id', 'widget_token', 'entityToken',
                'owner_token', 'ownerToken', 'whatsapp_destination_number')
        for key in keys:
            for value in ('', 'synthetic'):
                with self.subTest(key=key, value=value):
                    self.assert_unavailable(self.call('/api/public/landing-experience?' + key + '=' + value))

    def test_tenant_widget_and_authorization_headers_keep_tenant_discovery(self):
        for header in ('X-Tenant', 'X-Tenant-Slug', 'X-Tenant-ID', 'X-Widget-Token',
                       'X-Entity-Token', 'X-Owner-Token', 'X-Token', 'Authorization'):
            for value in ('', 'synthetic'):
                with self.subTest(header=header, value=value):
                    self.assert_unavailable(self.call(headers={header: value}))

    def test_widget_owner_and_configured_widget_cookies_keep_tenant_discovery(self):
        self.app.config['WIDGET_TOKEN_COOKIE_NAME'] = 'embed_authority'
        for cookie in ('widget_token', 'owner_token', 'embed_authority'):
            for value in ('', 'synthetic'):
                with self.subTest(cookie=cookie, value=value):
                    self.client.set_cookie(cookie, value, domain='www.chatboc.ar')
                    try:self.assert_unavailable(self.call())
                    finally:self.client.delete_cookie(cookie, domain='www.chatboc.ar')

    def test_tenant_referrer_paths_are_not_reinterpreted_as_brand_requests(self):
        for path in ('/t/synthetic', '/municipio/synthetic', '/pymes/synthetic', '/api/public/tenants/synthetic/menu'):
            with self.subTest(path=path):
                self.assert_unavailable(self.call(headers={'Referer': 'https://www.chatboc.ar' + path}))

    def test_unknown_or_duplicate_args_and_request_bodies_do_not_bypass(self):
        for path in ('/api/public/landing-experience?page=a&page=b', '/api/public/landing-experience?unreviewed=1'):
            with self.subTest(path=path):self.assert_unavailable(self.call(path))
        self.assert_unavailable(self.call(data='{"tenant":"synthetic"}', content_type='application/json'))
        self.assert_unavailable(self.call(headers={'Transfer-Encoding': 'chunked'}))

    def test_other_routes_methods_and_endpoint_identity_are_not_exempt(self):
        for path in ('/api/public/widget-config', '/api/public/tenants/synthetic/menu',
                     '/api/public/landing-experience/', '/public/landing-experience'):
            with self.subTest(path=path):self.assert_unavailable(self.call(path))
        self.assert_unavailable(self.call(method='POST'))
        import middleware.tenant_context as tc
        with self.app.test_request_context('/api/public/landing-experience', base_url='https://www.chatboc.ar'):
            request.url_rule = SimpleNamespace(endpoint='other.landing_experience')
            self.assertFalse(tc._is_global_platform_landing_request())

    def test_explicit_tenant_route_behavior_and_white_label_contact_boundary_remain(self):
        import routes.public_resolver as route
        tenant = SimpleNamespace(slug='synthetic-municipio', tipo='municipio', vertical='government', configuracion={})
        self.resolver.side_effect = None
        self.resolver.return_value = None
        with patch.object(route, 'resolve_tenant_only', return_value=tenant) as resolve:
            response = self.call('/api/public/landing-experience?tenant=synthetic-municipio')
        self.assertEqual(response.status_code, 200)
        self.resolver.assert_called_once()
        self.assertEqual(resolve.call_args.kwargs['tenant_slug'], 'synthetic-municipio')
        self.assertTrue(resolve.call_args.kwargs['require_explicit_slug'])
        self.assertTrue(response.get_json()['tenant']['white_label'])
        self.assertIsNone(response.get_json()['contact'])

    def test_custom_host_uses_only_the_request_resolved_active_binding(self):
        import time
        from services.organization_domain_binding import empty_record, KEY
        now = int(time.time())
        tenant = SimpleNamespace(id=51, slug='synthetic-institution', tipo='municipio',
            vertical='government', is_active=True, plan='pro', configuracion={}, dominio=None)
        record = empty_record(tenant) | {'version': 1, 'host': 'conversa.gob.ar', 'status': 'active',
            'challenge': 'a' * 64, 'requested_at': now - 60, 'challenge_expires_at': now - 60 + 86400,
            'dns_verified_at': now - 30, 'dns_valid_until': now + 3600,
            'platform_verified_at': now - 20, 'platform_valid_until': now + 3600}
        tenant.configuracion[KEY] = record
        self.resolver.side_effect = None; self.resolver.return_value = tenant
        response = self.call(host='conversa.gob.ar')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['tenant']['slug'], tenant.slug)
        self.assertIsNone(response.get_json()['contact'])
        self.resolver.assert_called_once()

    def test_custom_host_without_an_admissible_binding_never_gets_platform_contacts(self):
        for tenant in (None, SimpleNamespace(id=52, slug='unrelated-default', tipo='municipio',
                vertical='government', is_active=True, plan='pro', configuracion={}, dominio='conversa.gob.ar')):
            with self.subTest(tenant=getattr(tenant, 'slug', None)):
                self.resolver.side_effect = None; self.resolver.return_value = tenant
                response = self.call(host='conversa.gob.ar')
                self.assertEqual(response.status_code, 404)
                self.assertNotIn('contact', response.get_json())

    def test_custom_binding_must_match_host_be_current_active_and_entitled(self):
        import time
        from copy import deepcopy
        from services.organization_domain_binding import empty_record, KEY
        now = int(time.time())
        tenant = SimpleNamespace(id=51, slug='synthetic-institution', tipo='municipio',
            vertical='government', is_active=True, plan='pro', configuracion={}, dominio=None)
        record = empty_record(tenant) | {'version': 1, 'host': 'conversa.gob.ar', 'status': 'active',
            'challenge': 'a' * 64, 'requested_at': now - 60, 'challenge_expires_at': now - 60 + 86400,
            'dns_verified_at': now - 30, 'dns_valid_until': now + 3600,
            'platform_verified_at': now - 20, 'platform_valid_until': now + 3600}
        for mutation in ('wrong_host', 'expired', 'inactive', 'plan', 'malformed', 'canonical_forwarded'):
            with self.subTest(mutation=mutation):
                current = deepcopy(tenant); current.configuracion[KEY] = deepcopy(record)
                headers = {}
                if mutation == 'wrong_host':current.configuracion[KEY]['host'] = 'other.gob.ar'
                elif mutation == 'expired':current.configuracion[KEY]['platform_valid_until'] = now - 1
                elif mutation == 'inactive':current.is_active = False
                elif mutation == 'plan':current.plan = 'free'
                elif mutation == 'malformed':current.configuracion[KEY].pop('version')
                else:headers['X-Forwarded-Host'] = 'www.chatboc.ar'
                self.resolver.side_effect = None; self.resolver.return_value = current
                response = self.call(host='conversa.gob.ar', headers=headers)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn('contact', response.get_json())

    def test_canonical_host_never_reuses_an_ambient_default_profile(self):
        self.resolver.side_effect = None
        self.resolver.return_value = SimpleNamespace(id=52, slug='unrelated-default', tipo='municipio',
            vertical='government', is_active=True, plan='pro', configuracion={}, dominio='conversa.gob.ar')
        response = self.call('/api/public/landing-experience?unreviewed=1')
        self.assertEqual(response.status_code, 200)
        self.resolver.assert_called_once()
        self.assertIsNone(response.get_json()['tenant']['slug'])
        self.assertFalse(response.get_json()['tenant']['white_label'])
        self.assertIsNotNone(response.get_json()['contact'])

    def test_global_contact_is_links_only_and_uses_the_published_channels(self):
        data = self.call().get_json()['contact']
        self.assertEqual(set(data), {'title', 'description', 'methods', 'back_label', 'back_href', 'refresh_label'})
        self.assertEqual([method['href'] for method in data['methods']],
            ['mailto:info@chatboc.ar?subject=Demostraci%C3%B3n%20Chatboc', 'https://wa.me/5492613168608'])
        self.assertTrue(all(set(method) == {'id', 'label', 'description', 'href'} for method in data['methods']))
        self.assertEqual(data['back_href'], '/')
        self.resolver.assert_not_called()

    def test_hero_and_demo_copy_label_the_presentation_without_runtime_claims(self):
        data = self.call().get_json()
        expected = {'kind': 'illustrative_walkthrough', 'label': 'Ejemplo de recorrido',
                    'notice': 'Ilustración del proceso. No envía mensajes ni crea registros.'}
        self.assertEqual(data['hero']['presentation'], expected)
        self.assertEqual(data['hero']['headline'], 'Convertí conversaciones en operaciones reales')
        self.assertEqual(data['hero']['primary_cta'], {'label': 'Solicitar demostración', 'href': '/contacto', 'intent': 'sales_contact'})
        self.assertEqual(data['hero']['secondary_cta'], {'label': 'Ver recorrido', 'href': '/demo', 'intent': 'start_demo'})
        self.assertEqual(data['hero']['conversation_demo']['presentation'], expected)
        self.assertEqual(set(data['hero']['capability_overview']), {'label', 'title', 'description', 'steps'})
        self.assertEqual(len(data['hero']['capability_overview']['steps']), 3)
        self.assertTrue(all(set(step) == {'id', 'label', 'detail'} for step in data['hero']['capability_overview']['steps']))
        self.assertEqual(set(data['demo_showcase']), {'eyebrow', 'title', 'description', 'loading_title',
            'loading_description', 'error_message', 'empty_message', 'retry_label', 'start_error_message',
            'starting_label', 'card_label', 'open_label'})
        self.assertEqual(data['hero']['contract_scope'], 'operational_demo_data')
        self.resolver.assert_not_called()
