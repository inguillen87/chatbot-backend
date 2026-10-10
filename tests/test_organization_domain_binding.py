"""Offline full-app/real-SQL domain contracts; no customer DB or provider calls.

Run only in the dedicated profile_acceptance_runtime.prepare_process process.
Fresh PlatformObservation fixtures are synthetic, never deployment acceptance.
"""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from sqlalchemy.exc import SQLAlchemyError
from tests.profile_acceptance_runtime import create_disposable_app
from services.organization_domain_binding import (
    KEY, PUBLIC, CHALLENGE_TTL, VERIFICATION_TTL, DomainBindingError, PlatformObservation,
    activate_verified_binding, build_domain_binding, normalize_host, read_record,
    resolve_active_host, save_domain_binding,
)


class DomainLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='chatboc-domain-offline-')
        cls.app, cls.accounts, cls.password = create_disposable_app(cls.directory.name)

    @classmethod
    def tearDownClass(cls):
        from database import db
        with cls.app.app_context():
            db.session.remove()
            db.engine.dispose()
        cls.directory.cleanup()

    def setUp(self):
        from database import db
        from models import User, TenantProfile, AuditEvent
        from utils.tenant_admin_access import can_manage_tenant_control_plane
        from services.plan_access import tenant_allows_custom_domains
        self.db, self.User, self.Tenant, self.Audit = db, User, TenantProfile, AuditEvent
        self.authorize, self.entitlement = can_manage_tenant_control_plane, tenant_allows_custom_domains
        self.context = self.app.app_context()
        self.context.push()
        db.session.query(AuditEvent).delete()
        for tenant in TenantProfile.query.filter(TenantProfile.slug.in_(['acceptance-a', 'acceptance-b'])).all():
            tenant.configuracion = {'keep': True}
            tenant.plan = 'pro'
            tenant.dominio = None
            tenant.logo_url = '/logos/institution.svg'
            tenant.theme_json = {'primary_color': '#123abc', 'accent_color': '#334455'}
            tenant.is_active = True
        db.session.commit()
        self.now = int(time.time())
        self.tenant_id = self.accounts['acceptance-a']['tenant_id']
        self.actor_id = self.accounts['acceptance-a']['id']
        self.endpoint = '/api/admin/tenants/acceptance-a/domain'

    def tearDown(self):
        self.db.session.rollback()
        self.db.session.remove()
        self.context.pop()

    def tenant(self, identifier=None):
        return self.db.session.get(self.Tenant, identifier or self.tenant_id)

    def descriptor(self):
        return build_domain_binding(self.tenant(), can_edit=True,
            entitled=self.entitlement(self.tenant()), now=self.now)

    def save(self, operation, *, host='conversa.gob.ar', revision=None, actor=None, reader=None):
        data = {'expected_revision': revision or self.descriptor()['revision'], 'operation': operation}
        if operation == 'request':
            data['host'] = host
        return save_domain_binding(self.db.session, self.Tenant, self.User, self.Audit,
            tenant_id=self.tenant_id, actor_id=actor or self.actor_id, data=data,
            authorize=self.authorize, entitlement=self.entitlement, now=self.now, txt_reader=reader)

    def pending_platform(self):
        self.save('request')
        proof = self.descriptor()['dns_proof']['value']
        self.save('verify_dns', reader=lambda host: [proof])

    def observation(self, **changes):
        observation = PlatformObservation('conversa.gob.ar', 'prj_existing_frontend', self.now,
            self.now + 3600, True, True, True)
        return replace(observation, **changes)

    def activate(self, observation=None):
        return activate_verified_binding(self.db.session, self.Tenant, self.User, self.Audit,
            tenant_id=self.tenant_id, actor_id=self.actor_id,
            expected_revision=self.descriptor()['revision'], authorize=self.authorize,
            entitlement=self.entitlement, observation=observation or self.observation(),
            expected_project_id='prj_existing_frontend', now=self.now)

    def resolve(self, host='conversa.gob.ar', now=None):
        return resolve_active_host(self.db.session, self.Tenant, host,
            entitlement=self.entitlement, now=now or self.now)

    def login(self, account='acceptance-a'):
        client = self.app.test_client()
        response = client.post('/auth/login', json={'email': self.accounts[account]['email'], 'password': self.password})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json.get('token'))
        return client

    def test_exact_host_normalization_without_guessed_aliases(self):
        self.assertEqual(normalize_host('Conversa.GOB.ar.'), 'conversa.gob.ar')
        self.assertEqual(normalize_host('atención.gob.ar'), 'xn--atencin-q0a.gob.ar')
        for value in ['https://conversa.gob.ar', 'conversa.gob.ar:443', 'localhost', '127.0.0.1',
                '*.gob.ar', 'foo.local', 'foo.vercel.app', 'foo.onrender.com', 'www.chatboc.ar',
                'panel.chatboc.ar', 'a..gob.ar', '-a.gob.ar', 'a.gob.ar..', ' a.gob.ar',
                'a.gob.ar/path', 'a\u200b.gob.ar', 'a\u3002gob.ar', 'a.gob.ar@evil.com']:
            with self.subTest(value=value), self.assertRaises(DomainBindingError):
                normalize_host(value)

    def test_request_and_dns_are_durable_but_never_claim_active(self):
        before = self.descriptor()['revision']
        result = self.save('request', host='Conversa.GOB.ar.')
        self.assertFalse(result['domain']['active'])
        self.assertEqual(result['domain']['status'], 'pending_dns')
        self.assertNotEqual(result['domain']['revision'], before)
        self.assertEqual(self.tenant().configuracion['keep'], True)
        self.assertIsNone(self.resolve())
        calls = []
        proof = result['domain']['dns_proof']['value']
        def reader(host):
            calls.append(host)
            return [proof]
        result = self.save('verify_dns', reader=reader)
        self.assertEqual(calls, ['conversa.gob.ar'])
        self.assertEqual(result['domain']['status'], 'pending_platform')
        self.assertFalse(result['domain']['active'])
        self.db.session.remove()
        self.assertEqual(self.descriptor()['status'], 'pending_platform')
        self.assertIsNone(self.resolve())
        self.assertEqual(self.db.session.query(self.Audit).count(), 2)
        self.assertNotIn(proof, json.dumps([a.details for a in self.Audit.query.all()]))

    def test_dns_mismatch_expiry_and_unbounded_output_do_not_write(self):
        self.save('request')
        before = deepcopy(self.tenant().configuracion)
        for reader in [lambda h: [], lambda h: ['wrong'], lambda h: ['x' * 513],
                lambda h: ['x'] * 17, lambda h: ['no-ascii-ñ']]:
            with self.assertRaises(DomainBindingError):
                self.save('verify_dns', reader=reader)
            self.assertEqual(self.tenant().configuracion, before)
        self.now += CHALLENGE_TTL
        calls = []
        with self.assertRaises(DomainBindingError):
            self.save('verify_dns', reader=lambda h: calls.append(h) or [])
        self.assertEqual(calls, [])
        self.assertEqual(self.db.session.query(self.Audit).count(), 1)

    def test_activation_requires_fresh_exact_trusted_platform_and_dns(self):
        self.pending_platform()
        before = deepcopy(self.tenant().configuracion)
        for observation in [self.observation(host='other.gob.ar'), self.observation(https_ready=False),
                self.observation(ownership_verified=False), self.observation(app_routes_ready=False),
                self.observation(observed_at=self.now - 61), self.observation(observed_at=self.now + 1),
                self.observation(frontend_project_id='prj_other'), self.observation(valid_until=self.now),
                self.observation(valid_until=self.now + VERIFICATION_TTL + 1),
                {'https_ready': True}]:
            with self.subTest(observation=observation), self.assertRaises(DomainBindingError):
                self.activate(observation)
            self.assertEqual(self.tenant().configuracion, before)
        result = self.activate()
        self.assertTrue(result['domain']['active'])
        public = self.resolve('CONVERSA.gob.ar.')
        self.assertEqual(public['contract_version'], PUBLIC)
        self.assertEqual(public['tenant']['id'], self.tenant_id)
        self.assertEqual(public['tenant']['slug'], 'acceptance-a')
        self.assertEqual(public['tenant']['logo_url'], '/logos/institution.svg')
        self.assertEqual(public['brand']['primary_color'], '#123ABC')
        self.assertEqual(public['paths'], {'home': '/', 'login': '/login', 'workspace': '/perfil'})
        self.assertNotIn('challenge', json.dumps(public))
        self.assertIsNone(self.resolve('www.conversa.gob.ar'))
        self.assertIsNone(self.resolve(now=self.now + 3600))

    def test_legacy_domain_alone_and_all_ambiguity_fail_closed(self):
        self.tenant().dominio = 'conversa.gob.ar'
        self.db.session.commit()
        self.assertIsNone(self.resolve())
        self.pending_platform()
        self.activate()
        other = self.tenant(self.accounts['acceptance-b']['tenant_id'])
        other.dominio = 'Conversa.GOB.ar.'
        self.db.session.commit()
        self.assertIsNone(self.resolve())
        other.dominio = 'www.conversa.gob.ar'
        self.db.session.commit()
        self.assertIsNone(self.resolve())
        other.dominio = None
        other.configuracion = {KEY: {'status': 'active', 'host': 'conversa.gob.ar'}}
        self.db.session.commit()
        self.assertIsNone(self.resolve())

    def test_claim_collision_foreign_subdomain_and_optimistic_conflict(self):
        other = self.tenant(self.accounts['acceptance-b']['tenant_id'])
        other.dominio = 'Conversa.gob.ar.'
        self.db.session.commit()
        with self.assertRaises(DomainBindingError) as error:
            self.save('request')
        self.assertEqual(error.exception.code, 'host_in_use')
        other.dominio = None
        self.db.session.commit()
        with self.assertRaises(DomainBindingError):
            self.save('request', host='acceptance-b.chatboc.ar')
        before = self.descriptor()['revision']
        self.save('request', host='acceptance-a.chatboc.ar')
        with self.assertRaises(DomainBindingError) as error:
            self.save('request', revision=before)
        self.assertEqual(error.exception.status, 412)

    def test_real_roles_and_downgrade_rechecked_revoke_preserved(self):
        calls = []
        for account in ['acceptance-b', 'viewer']:
            with self.assertRaises(DomainBindingError) as error:
                self.save('request', actor=self.accounts[account]['id'], reader=lambda h: calls.append(h))
            self.assertEqual(error.exception.status, 403)
        self.assertEqual(calls, [])
        self.pending_platform()
        self.activate()
        self.tenant().plan = 'free'
        self.db.session.commit()
        self.assertIsNone(self.resolve())
        with self.assertRaises(DomainBindingError):
            self.save('request')
        result = self.save('revoke')
        self.assertEqual(result['domain']['status'], 'revoked')
        self.assertFalse(result['domain']['active'])
        self.assertIsNone(result['domain']['dns_proof'])

    def test_demo_or_capability_flags_do_not_unlock_and_other_features_unchanged(self):
        from services.plan_access import tenant_allows_workspace_branding
        tenant = self.tenant()
        self.assertTrue(self.entitlement(tenant))
        self.assertFalse(tenant_allows_workspace_branding(tenant))
        for plan in ['free', 'demo', None]:
            tenant.plan = plan
            tenant.capabilities_json = {'custom_domains': True, 'full': True}
            self.assertFalse(self.entitlement(tenant))
        tenant.plan = 'full'
        tenant.configuracion = {'demo_mode': True}
        self.assertFalse(self.entitlement(tenant))

    def test_commit_failure_has_no_partial_state_or_audit(self):
        with patch.object(type(self.db.session()), 'commit', side_effect=SQLAlchemyError('fixture private error')):
            with self.assertRaises(DomainBindingError) as error:
                self.save('request')
        self.assertEqual(error.exception.code, 'domain_save_unconfirmed')
        self.assertNotIn('private', str(error.exception))
        self.db.session.remove()
        self.assertEqual(self.descriptor()['status'], 'unconfigured')
        self.assertEqual(self.Audit.query.count(), 0)

    def test_public_http_unknown_pending_and_ambient_scope_never_override(self):
        self.save('request')
        client = self.app.test_client()
        bodies = []
        for host in ['conversa.gob.ar', 'other.gob.ar']:
            response = client.get('/api/public/host-resolution', query_string={'host': host},
                headers={'X-Tenant': 'acceptance-b', 'X-Tenant-ID': str(self.accounts['acceptance-b']['tenant_id'])})
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            bodies.append(response.json)
        self.assertEqual(bodies[0], bodies[1])
        self.pending_platform()
        self.activate()
        response = client.get('/api/public/host-resolution?host=conversa.gob.ar', headers={'X-Tenant': 'acceptance-b'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['tenant']['id'], self.tenant_id)
        for query in ['', '?host=conversa.gob.ar&host=other.gob.ar', '?host=conversa.gob.ar&tenant=acceptance-b', '?host=https://conversa.gob.ar']:
            response = client.get('/api/public/host-resolution' + query)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_admin_http_is_scoped_does_not_accept_activation_and_origin(self):
        client = self.login()
        response = client.get(self.endpoint)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
        revision = response.json['revision']
        for data in [{'expected_revision': revision, 'operation': 'activate'},
                {'expected_revision': revision, 'operation': 'request', 'host': 'conversa.gob.ar', 'status': 'active'}]:
            self.assertEqual(client.put(self.endpoint, json=data).status_code, 400)
        data = {'expected_revision': revision, 'operation': 'request', 'host': 'conversa.gob.ar'}
        self.assertEqual(client.put(self.endpoint, json=data, headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(client.put(self.endpoint, json=data).status_code, 200)
        for account in ['acceptance-b', 'viewer']:
            foreign = self.login(account)
            self.assertEqual(foreign.get(self.endpoint).status_code, 403)
            self.assertEqual(foreign.put(self.endpoint, json=data).status_code, 403)
        self.assertEqual(self.app.test_client().get(self.endpoint).status_code, 401)

    def test_admin_http_uses_existing_same_tenant_admin_and_delegated_roles(self):
        for account in ['second', 'delegated']:
            client = self.login(account)
            response = client.get(self.endpoint)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json['can_edit'])
            data = {'expected_revision': response.json['revision'], 'operation': 'request',
                'host': 'conversa.gob.ar'}
            self.assertEqual(client.put(self.endpoint, json=data).status_code, 200)

    def test_domain_writes_respect_existing_maintenance_fence(self):
        client = self.login()
        before = self.descriptor()
        try:
            self.app.config['CUTOVER_WRITER_FENCE_ENABLED'] = True
            response = client.get(self.endpoint)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json['can_edit'])
            self.assertFalse(response.json['can_revoke'])
            self.assertEqual(response.json['reason_code'], 'maintenance')
            response = client.put(self.endpoint, json={'expected_revision': before['revision'],
                'operation': 'request', 'host': 'conversa.gob.ar'})
            self.assertEqual(response.status_code, 503)
        finally:
            self.app.config['CUTOVER_WRITER_FENCE_ENABLED'] = False
        self.assertEqual(self.descriptor()['revision'], before['revision'])
        self.assertEqual(self.Audit.query.filter(self.Audit.event_type.like('organization.domain.%')).count(), 0)

    def test_private_binding_is_removed_from_public_config(self):
        from services.public_tenant_config import sanitize_public_tenant_config
        self.save('request')
        sanitized = sanitize_public_tenant_config(self.tenant().configuracion)
        self.assertNotIn(KEY, sanitized)
        self.assertTrue(sanitized['keep'])

    def test_bounded_txt_reader_joins_fragments_without_provisioning(self):
        from types import SimpleNamespace
        from services.organization_domain_binding import read_dns_txt
        with patch('dns.resolver.resolve', return_value=[SimpleNamespace(strings=(b'chatboc-', b'proof'))]) as dns:
            self.assertEqual(read_dns_txt('conversa.gob.ar'), ['chatboc-proof'])
        dns.assert_called_once_with('_chatboc-verify.conversa.gob.ar', 'TXT',
            lifetime=3, search=False, raise_on_no_answer=True)
        for rows in [[SimpleNamespace(strings=(b'x' * 513,))], [SimpleNamespace(strings=(b'x',))] * 17]:
            with patch('dns.resolver.resolve', return_value=rows), self.assertRaises(DomainBindingError):
                read_dns_txt('conversa.gob.ar')

    def test_json_shaped_operations_and_storage_cannot_crash_or_activate(self):
        for operation in [[], {}, True, None]:
            with self.assertRaises(DomainBindingError):
                save_domain_binding(self.db.session, self.Tenant, self.User, self.Audit,
                    tenant_id=self.tenant_id, actor_id=self.actor_id,
                    data={'expected_revision': self.descriptor()['revision'], 'operation': operation},
                    authorize=self.authorize, entitlement=self.entitlement, now=self.now)
        self.pending_platform()
        record = read_record(self.tenant())
        record['status'] = {'active': True}
        self.tenant().configuracion = {KEY: record}
        self.db.session.commit()
        self.assertIsNone(self.resolve())

    def test_active_replacement_requires_revoke_and_vault_free_public_logo(self):
        self.pending_platform()
        self.activate()
        before = deepcopy(self.tenant().configuracion)
        with self.assertRaises(DomainBindingError) as error:
            self.save('request', host='replacement.gob.ar')
        self.assertEqual(error.exception.code, 'domain_revoke_required')
        self.assertEqual(self.tenant().configuracion, before)
        for value, expected in [('https://www.chatboc.ar/logo.svg', 'https://www.chatboc.ar/logo.svg'),
                ('javascript:alert(1)', None), ('https://user:secret@public.org/logo.svg', None),
                ('https://127.0.0.1/logo.svg', None), ('//other.org/logo.svg', None)]:
            self.tenant().logo_url = value
            self.db.session.commit()
            self.assertEqual(self.resolve()['tenant']['logo_url'], expected)

    def test_inactive_tenant_and_malformed_binding_not_repaired(self):
        self.pending_platform()
        self.activate()
        self.tenant().is_active = False
        self.db.session.commit()
        self.assertIsNone(self.resolve())
        with self.assertRaises(DomainBindingError):
            self.save('revoke')
        self.tenant().is_active = True
        record = read_record(self.tenant())
        record['tenant_slug'] = 'acceptance-b'
        self.tenant().configuracion = {KEY: record}
        self.db.session.commit()
        self.assertIsNone(self.resolve())
        with self.assertRaises(DomainBindingError):
            self.descriptor()

    def test_branch_has_no_automatic_vercel_preview(self):
        manifest = json.loads((Path(__file__).resolve().parents[1] / 'vercel.json').read_text())
        self.assertIs(manifest['git']['deploymentEnabled']['feat/whitelabel-domain-lifecycle-20261010'], False)
