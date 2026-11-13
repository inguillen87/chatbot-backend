"""Offline provider/TLS contract tests. No real provider or tenant credentials."""
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from services.organization_address_vercel import (
    AddressObservation, AddressPlan, AddressVercelAdapter, FRONTEND_PROJECT,
    MAX_BODY, PLATFORM_RESERVED_LABELS, ProvisioningError, READINESS, Reply,
    VercelTransport, canonical, plan_digest, public_address_https_readiness,
    validate_address_host,
)

NOW = 1791650000
ROUTES = [{'src': '/api/(.*)', 'dest': 'https://api.chatboc.ar/api/$1'}]
ALIAS = 'provisional-organization.vercel.app'


def plan(**changes):
    return replace(AddressPlan('northside.chatboc.ar', 17, 'institution-alpha', 'platform',
        'c' * 64, NOW + 3600, 'dpl_1234567890', 'a' * 40, 'b' * 40,
        sha256(canonical(ROUTES)).hexdigest()), **changes)


def params(expected):
    return {'host': expected.host, 'tenant_id': expected.tenant_id,
        'tenant_slug': expected.tenant_slug, 'kind': expected.kind,
        'revision': expected.revision, 'nonce': '9' * 64}


def payload(expected, supplied):
    return {'contract_version': READINESS, 'host': supplied['host'], 'kind': supplied['kind'],
        'nonce': supplied['nonce'], 'tenant': {'id': supplied['tenant_id'], 'slug': supplied['tenant_slug']},
        'revision': supplied['revision'], 'backend_source': expected.backend_source,
        'observed_at': NOW, 'scope': 'pending_address_infrastructure_only',
        'active': False, 'auth_e2e_verified': False, 'private_assets_included': False}


def probe(expected, changes=None):
    def read(host, supplied):
        result = payload(expected, supplied)
        result.update(changes or {})
        return Reply(200, result)
    return read


class Provider:
    def __init__(self, expected):
        self.calls = []
        self.project = {'id': FRONTEND_PROJECT, 'targets': {'production': {
            'id': expected.frontend_deployment, 'readyState': 'READY',
            'meta': {'githubCommitSha': expected.frontend_source}}}}
        self.domain = {'name': expected.host, 'projectId': FRONTEND_PROJECT, 'verified': True}
        self.deployment = {'id': expected.frontend_deployment, 'projectId': FRONTEND_PROJECT,
            'target': 'production', 'readyState': 'READY',
            'gitSource': {'sha': expected.frontend_source}, 'routes': deepcopy(ROUTES)}
        self.alias = {'alias': expected.host, 'projectId': FRONTEND_PROJECT,
            'deploymentId': expected.frontend_deployment}

    def __call__(self, method, path, *, body=None):
        self.calls.append((method, path, body))
        if method != 'GET' or body is not None:
            raise AssertionError('Observer must never mutate a provider')
        if path == '/v9/projects/' + FRONTEND_PROJECT:
            value = self.project
        elif '/deployments/' in path:
            value = self.deployment
        elif '/aliases/' in path:
            value = self.alias
        else:
            value = self.domain
        return Reply(200, deepcopy(value)) if value is not None else Reply(404)


class AddressPlanTests(unittest.TestCase):
    def test_host_is_independent_of_slug_and_alias_is_exactly_allowlisted(self):
        plan().validate(NOW)
        plan(host=ALIAS, allowed_aliases=(ALIAS,)).validate(NOW)
        plan(host='citizen.institution.org', kind='custom').validate(NOW)
        for host in ['other.vercel.app', 'suffix.' + ALIAS, 'vercel.app', 'branch.chatboc.ar.evil.org']:
            with self.subTest(host=host), self.assertRaises(ProvisioningError):
                plan(host=host, allowed_aliases=(ALIAS,)).validate(NOW)

    def test_reserved_platform_labels_are_not_requestable(self):
        for label in PLATFORM_RESERVED_LABELS:
            with self.subTest(label=label), self.assertRaises(ProvisioningError):
                plan(host=label + '.chatboc.ar').validate(NOW)

    def test_platform_label_bounds_and_custom_platform_rejection(self):
        for host in ['aa.chatboc.ar', 'a' * 41 + '.chatboc.ar', 'a.b.chatboc.ar',
                '-abc.chatboc.ar', 'abc-.chatboc.ar', 'chatboc.ar']:
            with self.subTest(host=host), self.assertRaises(ProvisioningError):
                plan(host=host).validate(NOW)
        for host in ['chatboc.ar', 'abc.chatboc.ar', ALIAS, 'vercel.app']:
            with self.subTest(host=host), self.assertRaises(ProvisioningError):
                plan(host=host, kind='custom').validate(NOW)
        self.assertEqual(validate_address_host('abc.chatboc.ar', 'platform'), 'abc.chatboc.ar')
        plan(host='a' * 40 + '.chatboc.ar').validate(NOW)

    def test_bad_plan_cannot_start_a_provider_read(self):
        invalid = [{'host': host} for host in ('Northside.chatboc.ar', 'northside.chatboc.ar.',
            'https://northside.chatboc.ar', 'northside.chatboc.ar:443', '127.0.0.1',
            '*.chatboc.ar', 'site.local', 'foo.onrender.com', 'site\u200b.example.org')]
        invalid += [{'tenant_id': True}, {'tenant_id': 0}, {'tenant_slug': 'Other'},
            {'kind': 'legacy'}, {'revision': 'x'}, {'valid_until': NOW},
            {'valid_until': True}, {'valid_until': NOW + 604801}, {'project_id': 'prj_other'},
            {'frontend_source': 'main'}, {'backend_source': 'b' * 39},
            {'frontend_deployment': 'production'}, {'routes_sha256': 'bad'},
            {'allowed_aliases': [ALIAS]}, {'allowed_aliases': (ALIAS, ALIAS)},
            {'allowed_aliases': ('evil.org',)}, {'allowed_aliases': ('*.vercel.app',)},
            {'allowed_aliases': (True,)}, {'host': 'citizen.institution.org', 'kind': 'custom',
                'allowed_aliases': (ALIAS,)}]
        provider = Provider(plan())
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ProvisioningError):
                AddressVercelAdapter(provider, clock=lambda: NOW).observe(plan(**changes))
        self.assertEqual(provider.calls, [])
        with self.assertRaisesRegex(ProvisioningError, 'clock'):
            plan().validate(True)

    def test_plan_hash_binds_identity_class_source_routes_and_server_aliases(self):
        original = plan()
        self.assertEqual(plan_digest(original), sha256(canonical(asdict(original))).hexdigest())
        variants = [replace(original, tenant_id=18), replace(original, kind='custom'),
            replace(original, revision='d' * 64), replace(original, frontend_source='d' * 40),
            replace(original, backend_source='d' * 40), replace(original, routes_sha256='d' * 64),
            replace(original, allowed_aliases=(ALIAS,)), replace(original, valid_until=NOW + 4000)]
        self.assertEqual(len({plan_digest(original), *(plan_digest(value) for value in variants)}), 9)
        with self.assertRaises(ProvisioningError):
            plan_digest(asdict(original))


class AddressObserverTests(unittest.TestCase):
    def test_all_binding_kinds_get_fresh_exact_proof_without_provider_mutations(self):
        for expected in [plan(), plan(host=ALIAS, allowed_aliases=(ALIAS,)),
                plan(host='citizen.institution.org', kind='custom')]:
            with self.subTest(host=expected.host):
                provider = Provider(expected)
                witness = Mock(side_effect=probe(expected))
                result = AddressVercelAdapter(provider, https_probe=witness, clock=lambda: NOW).observe(expected)
                self.assertEqual(result, AddressObservation(plan_digest(expected), expected.host,
                    expected.kind, FRONTEND_PROJECT, NOW, expected.valid_until))
                self.assertEqual(len(provider.calls), 8)
                self.assertTrue(all(method == 'GET' and body is None for method, _, body in provider.calls))
                witness.assert_called_once()
                self.assertEqual(set(witness.call_args.args[1]), set(params(expected)))
                self.assertEqual(len(witness.call_args.args[1]['nonce']), 64)
                self.assertNotIn('auth_e2e_verified', asdict(result))

    def test_formal_production_target_must_match_even_if_alias_and_deployment_match(self):
        cases = [('id', 'prj_other'), ('targets', None), ('targets', {'production': None}),
            ('targets', {'production': {'id': 'dpl_other123', 'readyState': 'READY'}}),
            ('targets', {'production': {'id': plan().frontend_deployment, 'readyState': 'BUILDING'}}),
            ('targets', {'production': {'id': plan().frontend_deployment, 'readyState': 'READY',
                'meta': {'githubCommitSha': 'd' * 40}}})]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                provider = Provider(plan()); provider.project[key] = value
                with self.assertRaisesRegex(ProvisioningError, 'production_target'):
                    AddressVercelAdapter(provider, https_probe=probe(plan()), clock=lambda: NOW).observe(plan())
                self.assertEqual(len(provider.calls), 1)

    def test_mixed_provider_identity_route_or_publication_fails_closed(self):
        cases = [('domain', 'name', 'other.chatboc.ar'), ('domain', 'projectId', 'prj_other'),
            ('domain', 'verified', 'true'), ('domain', 'redirect', 'https://other.org'),
            ('domain', 'gitBranch', 'preview'), ('domain', 'customEnvironmentId', 'env_other'),
            ('deployment', 'id', 'dpl_other123'), ('deployment', 'target', 'preview'),
            ('deployment', 'projectId', 'prj_other'), ('deployment', 'readyState', 'BUILDING'),
            ('deployment', 'gitSource', {'sha': 'd' * 40}), ('deployment', 'gitSource', []),
            ('deployment', 'routes', [{'src': '/(.*)', 'dest': '/index.html'}]),
            ('alias', 'alias', 'other.chatboc.ar'), ('alias', 'deploymentId', 'dpl_other123'),
            ('alias', 'projectId', 'prj_other'), ('alias', 'redirectStatusCode', 308),
            ('alias', 'deletedAt', NOW), ('alias', 'microfrontends', {})]
        for target, key, value in cases:
            with self.subTest(target=target, key=key):
                provider = Provider(plan()); getattr(provider, target)[key] = value
                with self.assertRaises(ProvisioningError):
                    AddressVercelAdapter(provider, https_probe=probe(plan()), clock=lambda: NOW).observe(plan())
                self.assertTrue(all(call[0] == 'GET' for call in provider.calls))
        provider = Provider(plan())
        provider.deployment['routes'] = [{'src': '/api/(.*)', 'dest': 'https://other.org/api/$1'}]
        expected = plan(routes_sha256=sha256(canonical(provider.deployment['routes'])).hexdigest())
        with self.assertRaisesRegex(ProvisioningError, 'same_origin_backend_proxy'):
            AddressVercelAdapter(provider, clock=lambda: NOW).observe(expected)

    def test_nonce_scope_identity_clock_and_public_flags_are_exact(self):
        cases = [{'nonce': '0' * 64}, {'host': 'other.chatboc.ar'}, {'kind': 'custom'},
            {'tenant': {'id': 18, 'slug': 'institution-alpha'}}, {'tenant': None},
            {'tenant': {'id': 17, 'slug': 'other'}}, {'revision': '0' * 64},
            {'backend_source': 'd' * 40}, {'observed_at': NOW - 61},
            {'observed_at': NOW + 1}, {'observed_at': True}, {'active': True},
            {'auth_e2e_verified': 0}, {'private_assets_included': True},
            {'scope': 'pending_domain_infrastructure_only'}, {'extra': None},
            {'contract_version': 'public.tenant_host_readiness.v1'}]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ProvisioningError):
                AddressVercelAdapter(Provider(plan()), https_probe=probe(plan(), changes),
                    clock=lambda: NOW).observe(plan())

    def test_final_provider_recheck_detects_rotation_revocation_and_changed_routes(self):
        for change in ('production', 'domain', 'alias', 'source', 'routes'):
            with self.subTest(change=change):
                provider = Provider(plan())
                def changing(host, supplied):
                    if change == 'production': provider.project['targets']['production']['id'] = 'dpl_other123'
                    elif change == 'domain': provider.domain = None
                    elif change == 'alias': provider.alias['deploymentId'] = 'dpl_other123'
                    elif change == 'source': provider.deployment['gitSource']['sha'] = 'd' * 40
                    else: provider.deployment['routes'].append({'src': '/(.*)', 'dest': 'https://other.org'})
                    return probe(plan())(host, supplied)
                with self.assertRaises(ProvisioningError):
                    AddressVercelAdapter(provider, https_probe=changing, clock=lambda: NOW).observe(plan())

    def test_observation_budget_expiry_and_backward_clock_fail(self):
        for times in ([NOW, NOW + 61], [NOW, NOW, NOW + 61], [NOW, NOW - 1],
                [NOW, NOW + 1, NOW], [NOW, True]):
            with self.subTest(times=times), self.assertRaises(ProvisioningError):
                clock = iter(times)
                AddressVercelAdapter(Provider(plan()), https_probe=probe(plan()),
                    clock=lambda: next(clock)).observe(plan())
        expected = plan(valid_until=NOW + 10); clock = iter([NOW, NOW, NOW + 10])
        with self.assertRaisesRegex(ProvisioningError, 'expired'):
            AddressVercelAdapter(Provider(expected), https_probe=probe(expected),
                clock=lambda: next(clock)).observe(expected)

    def test_no_retries_and_foreign_exception_details_never_escape(self):
        for failure in [TimeoutError('sensitive-provider-body'),
                ProvisioningError('sensitive-provider-body'), ProvisioningError(['private'])]:
            provider = Mock(side_effect=failure)
            with self.subTest(failure=type(failure)), self.assertRaisesRegex(
                    ProvisioningError, '^address_provider_observation_failed$'):
                AddressVercelAdapter(provider, clock=lambda: NOW).observe(plan())
            provider.assert_called_once()

    def test_mutating_legacy_methods_are_not_exposed(self):
        provider = Mock()
        adapter = AddressVercelAdapter(provider, clock=lambda: NOW)
        for method in (adapter.attempt, adapter.reconcile):
            with self.assertRaisesRegex(ProvisioningError, 'fixed_vercel_operation'):
                method('add', plan(), object())
        provider.assert_not_called()


class AddressTransportTests(unittest.TestCase):
    def response(self, raw=b'{}', status=200, headers=None):
        values = {'Content-Type': 'application/json', 'Cache-Control': 'no-store'}
        values.update(headers or {})
        response = Mock(status=status)
        response.getheader.side_effect = lambda key, default=None: values.get(key, default)
        response.read.return_value = raw
        return response

    def answers(self, *addresses):
        return [(2, 1, 6, '', (address, 443)) for address in addresses]

    def test_project_read_uses_existing_authenticated_transport_with_no_post_capability(self):
        conn = Mock(); conn.getresponse.return_value = self.response()
        with patch('services.organization_domain_vercel.http.client.HTTPSConnection', return_value=conn) as connect:
            transport = VercelTransport('offline-' + 'x' * 32, team_id='team_123456789')
            result = transport('GET', '/v9/projects/' + FRONTEND_PROJECT)
            self.assertEqual(result.status, 200)
            self.assertEqual(conn.request.call_args.args[:2],
                ('GET', '/v9/projects/' + FRONTEND_PROJECT + '?teamId=team_123456789'))
            self.assertTrue(connect.call_args.kwargs['context'].check_hostname)
        with patch('services.organization_domain_vercel.http.client.HTTPSConnection') as connect:
            for method in ('POST', 'PATCH', 'DELETE'):
                with self.assertRaises(ProvisioningError):
                    transport(method, '/v9/projects/' + FRONTEND_PROJECT)
            connect.assert_not_called()

    def test_public_probe_is_exact_nonce_get_pinned_ip_with_hostname_and_no_credentials(self):
        expected = plan(host=ALIAS, allowed_aliases=(ALIAS,))
        conn = Mock(); conn.getresponse.return_value = self.response(json.dumps(payload(expected, params(expected))).encode())
        with patch('services.organization_address_vercel.socket.getaddrinfo', return_value=self.answers('8.8.8.8')), \
                patch('services.organization_address_vercel._PinnedHTTPSConnection', return_value=conn) as connect:
            result = public_address_https_readiness(expected.host, params(expected))
        self.assertEqual(result.status, 200)
        connect.assert_called_once_with(expected.host, '8.8.8.8')
        self.assertEqual(conn.request.call_args.args[0], 'GET')
        target = urlsplit(conn.request.call_args.args[1])
        self.assertEqual(target.path, '/api/public/address-readiness')
        self.assertEqual(parse_qs(target.query), {key: [str(value)] for key, value in params(expected).items()})
        self.assertEqual(conn.request.call_args.kwargs['headers'],
            {'Accept': 'application/json', 'Accept-Encoding': 'identity'})
        conn.close.assert_called_once()

    def test_private_mixed_multicast_and_unbounded_dns_never_connect(self):
        cases = [(), ('127.0.0.1',), ('169.254.169.254',), ('::1',), ('224.0.0.1',),
            ('8.8.8.8', '10.0.0.1'), tuple('8.8.8.8' for _ in range(17))]
        for addresses in cases:
            with self.subTest(addresses=addresses), \
                    patch('services.organization_address_vercel.socket.getaddrinfo', return_value=self.answers(*addresses)), \
                    patch('services.organization_address_vercel._PinnedHTTPSConnection') as connect:
                with self.assertRaisesRegex(ProvisioningError, 'public_dns'):
                    public_address_https_readiness(plan().host, params(plan()))
                connect.assert_not_called()

    def test_invalid_probe_scope_fails_before_dns(self):
        for changes in [{'host': 'other.chatboc.ar'}, {'tenant_id': True}, {'nonce': 'short'},
                {'revision': None}, {'kind': 'legacy'}, {'tenant_slug': '../other'}, {'extra': True}]:
            supplied = {**params(plan()), **changes}
            with self.subTest(changes=changes), patch('services.organization_address_vercel.socket.getaddrinfo') as dns:
                with self.assertRaises(ProvisioningError):
                    public_address_https_readiness(plan().host, supplied)
                dns.assert_not_called()

    def test_probe_rejects_redirect_error_cached_compressed_ambiguous_and_large_responses(self):
        cases = [(b'{}', 301, {}), (b'{}', 500, {}), (b'{}', 200, {'Content-Type': 'text/html'}),
            (b'{}', 200, {'Cache-Control': 'public, max-age=300'}),
            (b'{}', 200, {'Content-Encoding': 'gzip'}),
            (b'{}', 200, {'Content-Length': str(MAX_BODY + 1)}),
            (b'{}', 200, {'Content-Length': '-1'}), (b'x' * (MAX_BODY + 1), 200, {}),
            (b'{"a":1,"a":2}', 200, {}), (b'{"a":NaN}', 200, {})]
        for raw, status, headers in cases:
            conn = Mock(); conn.getresponse.return_value = self.response(raw, status, headers)
            with self.subTest(status=status, headers=headers), \
                    patch('services.organization_address_vercel.socket.getaddrinfo', return_value=self.answers('8.8.8.8')), \
                    patch('services.organization_address_vercel._PinnedHTTPSConnection', return_value=conn):
                with self.assertRaises(ProvisioningError):
                    public_address_https_readiness(plan().host, params(plan()))
            conn.close.assert_called_once()
            self.assertEqual(conn.request.call_count, 1)
            if status != 200:
                conn.getresponse.return_value.read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
