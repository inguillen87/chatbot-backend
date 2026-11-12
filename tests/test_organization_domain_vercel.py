"""Offline real transport shapes and durable effects; never Vercel/customer DNS."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from services.organization_domain_binding import READINESS
from services.organization_domain_vercel import (
    DomainPlan, FRONTEND_PROJECT, IntentJournal, MAX_BODY, ProvisioningError, Reply,
    VercelDomainAdapter, VercelTransport, _PinnedHTTPSConnection, canonical,
    public_https_readiness,
)

NOW = 1791650000
ROUTES = [{'src': '/api/(.*)', 'dest': 'https://api.chatboc.ar/api/$1'}]


def plan(**changes):
    return replace(DomainPlan('conversa.gob.ar', 46, 'tierra-del-fuego', 'c' * 64,
        NOW + 3600, 'dpl_1234567890', 'a' * 40, 'b' * 40,
        sha256(canonical(ROUTES)).hexdigest()), **changes)


class FakeVercel:
    def __init__(self, expected, *, assigned=True, verified=True):
        self.expected, self.calls = expected, []
        self.domain = {'name': expected.host, 'projectId': FRONTEND_PROJECT, 'verified': verified} if assigned else None
        self.deployment = {'id': expected.frontend_deployment, 'projectId': FRONTEND_PROJECT,
            'target': 'production', 'readyState': 'READY', 'gitSource': {'sha': expected.frontend_source},
            'routes': deepcopy(ROUTES)}
        self.alias = {'alias': expected.host, 'projectId': FRONTEND_PROJECT,
            'deploymentId': expected.frontend_deployment}
        self.accept_then_raise = False
        self.readback_wrong_project = False

    def __call__(self, method, path, *, body=None):
        self.calls.append((method, path, deepcopy(body)))
        if method == 'POST':
            if path.endswith('/verify'):
                self.domain['verified'] = True
            else:
                self.domain = {'name': body['name'], 'projectId': FRONTEND_PROJECT, 'verified': False}
            if self.accept_then_raise:
                raise TimeoutError('private provider body must never leave adapter')
            if self.readback_wrong_project:
                self.domain['projectId'] = 'prj_other'
            return Reply(200, deepcopy(self.domain))
        if '/deployments/' in path:
            return Reply(200, deepcopy(self.deployment))
        if '/aliases/' in path:
            return Reply(200, deepcopy(self.alias))
        return Reply(200, deepcopy(self.domain)) if self.domain else Reply(404)


def probe(expected, changes=None):
    def call(host, params):
        row = {'contract_version': READINESS, 'host': host, 'nonce': params['nonce'],
            'tenant': {'id': params['tenant_id'], 'slug': params['tenant_slug']},
            'revision': params['revision'], 'backend_source': expected.backend_source,
            'observed_at': NOW, 'scope': 'pending_domain_infrastructure_only',
            'active': False, 'auth_e2e_verified': False, 'private_assets_included': False}
        row.update(changes or {})
        return Reply(200, row)
    return call


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='chatboc-vercel-domain-offline-')
        self.plan = plan()
        self.provider = FakeVercel(self.plan)
        self.adapter = VercelDomainAdapter(self.provider, https_probe=probe(self.plan), clock=lambda: NOW)
        self.journal = IntentJournal(self.directory.name, self.plan)

    def tearDown(self):
        self.directory.cleanup()

    def test_fresh_readback_is_only_infrastructure_and_all_calls_are_get(self):
        value = self.adapter.observe(self.plan)
        self.assertEqual(value.host, self.plan.host)
        self.assertTrue(value.ownership_verified and value.https_ready and value.app_routes_ready)
        self.assertEqual(value.valid_until, self.plan.dns_valid_until)
        self.assertEqual(len(self.provider.calls), 6)
        self.assertEqual({row[0] for row in self.provider.calls}, {'GET'})
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_wrong_project_redirect_environment_alias_source_and_routes_fail_closed(self):
        cases = [('domain', 'projectId', 'prj_other'), ('domain', 'redirect', 'https://other.invalid'),
            ('domain', 'gitBranch', 'preview'), ('domain', 'customEnvironmentId', 'env_other'),
            ('domain', 'verified', 'true'), ('deployment', 'target', 'preview'),
            ('deployment', 'readyState', 'BUILDING'), ('deployment', 'gitSource', {'sha': 'd' * 40}),
            ('deployment', 'routes', [{'src': '/(.*)', 'dest': '/index.html'}]),
            ('alias', 'deploymentId', 'dpl_other123'), ('alias', 'projectId', 'prj_other'),
            ('alias', 'redirectStatusCode', 308), ('alias', 'deletedAt', NOW),
            ('alias', 'microfrontends', {'defaultApp': {'projectId': 'prj_other'}})]
        for target, key, value in cases:
            with self.subTest(target=target, key=key):
                provider = FakeVercel(self.plan)
                getattr(provider, target)[key] = value
                adapter = VercelDomainAdapter(provider, https_probe=probe(self.plan), clock=lambda: NOW)
                with self.assertRaises(ProvisioningError):
                    adapter.observe(self.plan)
                self.assertTrue(all(row[0] == 'GET' for row in provider.calls))

    def test_nonce_identity_revision_source_freshness_and_public_only_contract(self):
        cases = [{'nonce': 'd' * 64}, {'tenant': {'id': 47, 'slug': self.plan.tenant_slug}},
            {'tenant': {'id': 46, 'slug': 'other'}}, {'revision': 'd' * 64},
            {'backend_source': 'd' * 40}, {'observed_at': NOW - 61}, {'observed_at': NOW + 1},
            {'observed_at': True}, {'active': True}, {'auth_e2e_verified': True},
            {'private_assets_included': True}, {'scope': 'private_assets'}, {'extra': 'no'}]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ProvisioningError):
                VercelDomainAdapter(self.provider, https_probe=probe(self.plan, changes),
                    clock=lambda: NOW).observe(self.plan)

    def test_total_observation_budget_and_removed_domain_abort(self):
        clock = iter([NOW, NOW, NOW + 61])
        with self.assertRaisesRegex(ProvisioningError, 'fresh_platform'):
            VercelDomainAdapter(self.provider, https_probe=probe(self.plan), clock=lambda: next(clock)).observe(self.plan)
        def removed(host, params):
            self.provider.domain = None
            return probe(self.plan)(host, params)
        with self.assertRaisesRegex(ProvisioningError, 'ownership_pending'):
            VercelDomainAdapter(self.provider, https_probe=removed, clock=lambda: NOW).observe(self.plan)

    def test_reject_malformed_plan_before_any_effect(self):
        for changes in [{'tenant_id': True}, {'dns_valid_until': NOW}, {'host': 'Conversa.gob.ar'},
                {'frontend_source': 'main'}, {'project_id': 'prj_other'}, {'revision': 'x'},
                {'frontend_deployment': 'production'}, {'routes_sha256': 'x'}]:
            with self.subTest(changes=changes), self.assertRaises(ProvisioningError):
                self.adapter.observe(plan(**changes))
        self.assertEqual(self.provider.calls, [])

    def test_add_verify_exact_one_post_each_intents_precede_and_no_false_active(self):
        self.provider.domain = None
        original = self.provider.__call__
        def checked(method, path, *, body=None):
            if method == 'POST':
                operation = 'verify' if path.endswith('/verify') else 'add'
                self.assertTrue(self.journal.path(operation, 'intent').is_file())
            return original(method, path, body=body)
        adapter = VercelDomainAdapter(checked, clock=lambda: NOW)
        for operation in ('add', 'verify'):
            value = adapter.attempt(operation, self.plan, self.journal, owner_guard=lambda p: True)
            self.assertFalse(value['active'] or value['auth_e2e_verified'])
            self.assertTrue(self.journal.path(operation, 'receipt').is_file())
            with self.assertRaisesRegex(ProvisioningError, 'read_only_reconcile'):
                adapter.attempt(operation, self.plan, self.journal, owner_guard=lambda p: True)
        posts = [row for row in self.provider.calls if row[0] == 'POST']
        self.assertEqual(posts, [('POST', f'/v10/projects/{FRONTEND_PROJECT}/domains', {'name': self.plan.host}),
            ('POST', f'/v9/projects/{FRONTEND_PROJECT}/domains/{self.plan.host}/verify', None)])

    def test_unknown_provider_acceptance_never_retries_and_reconcile_only_gets(self):
        self.provider.domain = None
        self.provider.accept_then_raise = True
        with self.assertRaisesRegex(ProvisioningError, 'read_only_reconcile'):
            self.adapter.attempt('add', self.plan, self.journal, owner_guard=lambda p: True)
        self.assertTrue(self.journal.path('add', 'intent').is_file())
        self.assertFalse(self.journal.path('add', 'receipt').exists())
        before = len(self.provider.calls)
        with self.assertRaises(ProvisioningError):
            self.adapter.attempt('add', self.plan, self.journal, owner_guard=lambda p: True)
        self.assertEqual(len(self.provider.calls), before)
        value = self.adapter.reconcile('add', self.plan, self.journal)
        self.assertFalse(value['active'])
        self.assertEqual(len([r for r in self.provider.calls if r[0] == 'POST']), 1)
        self.assertTrue(all(r[0] == 'GET' for r in self.provider.calls[before:]))

    def test_owner_rejection_and_foreign_journal_cannot_consume_intent_or_provider(self):
        with self.assertRaises(ProvisioningError):
            self.adapter.attempt('add', self.plan, self.journal, owner_guard=lambda p: False)
        foreign = IntentJournal(self.directory.name, plan(host='other.gob.ar'))
        for operation in ('attempt', 'reconcile'):
            with self.subTest(operation=operation), self.assertRaisesRegex(ProvisioningError, 'plan_journal'):
                if operation == 'attempt':
                    self.adapter.attempt('add', self.plan, foreign, owner_guard=lambda p: True)
                else:
                    self.adapter.reconcile('add', self.plan, foreign)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_owner_recheck_and_wrong_provider_readback_preserve_unknown_intent(self):
        self.provider.domain = None
        guards = iter([True, False])
        with self.assertRaises(ProvisioningError):
            self.adapter.attempt('add', self.plan, self.journal, owner_guard=lambda p: next(guards))
        self.assertEqual([r for r in self.provider.calls if r[0] == 'POST'], [])
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])
        self.provider.readback_wrong_project = True
        with self.assertRaises(ProvisioningError):
            self.adapter.attempt('add', self.plan, self.journal, owner_guard=lambda p: True)
        self.assertTrue(self.journal.path('add', 'intent').is_file())
        self.assertFalse(self.journal.path('add', 'receipt').exists())


class TransportTests(unittest.TestCase):
    def response(self, *, status=200, raw=b'{"verified":true}', headers=None):
        response = Mock(status=status)
        response.getheader.side_effect = lambda k, default=None: (headers or {}).get(k, default)
        response.read.return_value = raw
        return response

    def test_fixed_tls_json_team_request_and_no_redirect_or_error_body_reads(self):
        for status in (200, 301, 401, 500, 404):
            with self.subTest(status=status):
                conn = Mock(); response = self.response(status=status); conn.getresponse.return_value = response
                with patch('services.organization_domain_vercel.http.client.HTTPSConnection', return_value=conn) as constructor:
                    transport = VercelTransport('offline-' + 'x' * 32, team_id='team_123456789')
                    if status in (200, 404):
                        result = transport('GET', f'/v9/projects/{FRONTEND_PROJECT}/domains/conversa.gob.ar')
                        self.assertEqual(result.status, status)
                    else:
                        with self.assertRaises(ProvisioningError):
                            transport('GET', f'/v9/projects/{FRONTEND_PROJECT}/domains/conversa.gob.ar')
                    self.assertEqual(constructor.call_args.args, ('api.vercel.com',))
                    self.assertTrue(constructor.call_args.kwargs['context'].check_hostname)
                    self.assertEqual(conn.request.call_args.args[1], f'/v9/projects/{FRONTEND_PROJECT}/domains/conversa.gob.ar?teamId=team_123456789')
                    conn.close.assert_called_once()
                    self.assertEqual(response.read.call_count, 1 if status == 200 else 0)

    def test_provider_duplicate_keys_encoding_declared_and_stream_bounds(self):
        cases = [(b'{"verified":true,"verified":false}', {}), (b'{}', {'Content-Encoding': 'gzip'}),
            (b'{}', {'Content-Length': str(MAX_BODY + 1)}), (b'x' * (MAX_BODY + 1), {}), (b'[]', {'Content-Length': '-1'})]
        for raw, headers in cases:
            with self.subTest(headers=headers):
                conn = Mock(); conn.getresponse.return_value = self.response(raw=raw, headers=headers)
                with patch('services.organization_domain_vercel.http.client.HTTPSConnection', return_value=conn):
                    with self.assertRaises(ProvisioningError):
                        VercelTransport('offline-' + 'x' * 32)('GET', f'/v9/projects/{FRONTEND_PROJECT}/domains/conversa.gob.ar')
                conn.close.assert_called_once()

    def test_deployment_explicitly_requests_official_git_source_details(self):
        conn = Mock(); conn.getresponse.return_value = self.response()
        with patch('services.organization_domain_vercel.http.client.HTTPSConnection', return_value=conn):
            VercelTransport('offline-' + 'x' * 32)('GET', '/v13/deployments/dpl_1234567890')
        self.assertEqual(conn.request.call_args.args[1], '/v13/deployments/dpl_1234567890?withGitRepoInfo=true')

    def test_disallowed_paths_methods_or_extended_mutation_body_never_connect(self):
        with patch('services.organization_domain_vercel.http.client.HTTPSConnection') as constructor:
            transport = VercelTransport('offline-' + 'x' * 32)
            for method, path, body in [('DELETE', '/v9/projects/x', None), ('GET', 'https://api.vercel.com', None),
                    ('POST', f'/v10/projects/{FRONTEND_PROJECT}/domains', {'name': 'conversa.gob.ar', 'redirect': 'other'}),
                    ('POST', '/v4/aliases/conversa.gob.ar', {})]:
                with self.assertRaises(ProvisioningError):
                    transport(method, path, body=body)
            constructor.assert_not_called()

    def test_public_https_private_or_mixed_dns_denied_before_connection(self):
        for addresses in [('127.0.0.1',), ('8.8.8.8', '10.0.0.1'), ('::1',), ('169.254.169.254',)]:
            answers = [(2, 1, 6, '', (address, 443)) for address in addresses]
            with patch('services.organization_domain_vercel.socket.getaddrinfo', return_value=answers), \
                    patch('services.organization_domain_vercel._PinnedHTTPSConnection') as constructor:
                with self.assertRaisesRegex(ProvisioningError, 'public_dns'):
                    public_https_readiness('conversa.gob.ar', {})
                constructor.assert_not_called()

    def test_public_https_exact_ip_pinned_sni_and_success_body(self):
        answers = [(2, 1, 6, '', ('8.8.8.8', 443))]
        conn = Mock(); conn.getresponse.return_value = self.response(raw=b'{"active":false}', headers={'Content-Type': 'application/json; charset=utf-8'})
        with patch('services.organization_domain_vercel.socket.getaddrinfo', return_value=answers), \
                patch('services.organization_domain_vercel._PinnedHTTPSConnection', return_value=conn) as constructor:
            self.assertEqual(public_https_readiness('conversa.gob.ar', {'nonce': 'abc'}).data, {'active': False})
            constructor.assert_called_once_with('conversa.gob.ar', '8.8.8.8')
            self.assertEqual(conn.request.call_args.args, ('GET', '/api/public/host-readiness?nonce=abc'))
            conn.close.assert_called_once()
        pinned = _PinnedHTTPSConnection('conversa.gob.ar', '8.8.8.8')
        tls = Mock(); pinned._context = tls
        with patch('services.organization_domain_vercel.socket.create_connection', return_value='offline_socket') as create:
            pinned.connect()
            create.assert_called_once_with(('8.8.8.8', 443), timeout=5)
            tls.wrap_socket.assert_called_once_with('offline_socket', server_hostname='conversa.gob.ar')

    def test_public_https_redirect_never_follows_or_reads_body(self):
        answers = [(2, 1, 6, '', ('8.8.8.8', 443))]
        conn = Mock(); response = self.response(status=302); conn.getresponse.return_value = response
        with patch('services.organization_domain_vercel.socket.getaddrinfo', return_value=answers), \
                patch('services.organization_domain_vercel._PinnedHTTPSConnection', return_value=conn):
            with self.assertRaises(ProvisioningError):
                public_https_readiness('conversa.gob.ar', {})
            response.read.assert_not_called()
            conn.request.assert_called_once()
            conn.close.assert_called_once()
