"""Three-level address behavior on the real disposable app and SQL models.

Provider observations here are synthetic fixtures. They do not certify a live
domain, deploy a site, send a message, or connect to the customer database.
"""
from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch

from sqlalchemy.exc import SQLAlchemyError

from tests import test_organization_domain_binding as v1_tests
from services import organization_addresses as addresses
from services import organization_domain_binding as legacy
from services.organization_address_vercel import AddressObservation, plan_digest


class AddressLifecycleTests(unittest.TestCase):
    setUpClass = classmethod(v1_tests.DomainLifecycleTests.setUpClass.__func__)
    tearDownClass = classmethod(v1_tests.DomainLifecycleTests.tearDownClass.__func__)
    tearDown = v1_tests.DomainLifecycleTests.tearDown
    tenant = v1_tests.DomainLifecycleTests.tenant
    login = v1_tests.DomainLifecycleTests.login

    def setUp(self):
        v1_tests.DomainLifecycleTests.setUp(self)
        from extensions import limiter
        limiter.reset()
        self.endpoint = '/api/admin/tenants/acceptance-a/addresses'
        self.app.config.update(ORGANIZATION_PLATFORM_ALIAS_BINDINGS_JSON='{}',
            ORGANIZATION_ADDRESS_ACTIVATION_ENABLED=False, ORGANIZATION_ADDRESS_VERCEL_TOKEN=None,
            BACKEND_VERSION='b' * 40)

    def descriptor(self):
        return addresses.build_addresses(self.tenant(), can_manage=True,
            config=self.app.config, now=self.now)

    def save(self, kind='platform', operation='request', *, host='northside.chatboc.ar',
            revision=None, reader=None, actor=None):
        data = {'kind': kind, 'operation': operation,
            'expected_revision': revision or self.descriptor()['revision']}
        if operation == 'request':
            data['host'] = host
        return addresses.save_addresses(self.db.session, self.Tenant, self.User, self.Audit,
            tenant_id=self.tenant_id, actor_id=actor or self.actor_id, data=data,
            authorize=self.authorize, config=self.app.config, now=self.now, txt_reader=reader)

    def ready_custom(self):
        self.save('custom', host='civic.gob.ar')
        proof = self.descriptor()['custom']['dns_proof']['value']
        self.save('custom', 'verify_dns', reader=lambda _host: [proof])

    def plan(self, kind='platform'):
        return addresses.prepare_plan(self.tenant(), kind=kind,
            expected_revision=self.descriptor()['revision'], frontend_deployment='dpl_1234567890',
            frontend_source='a' * 40, routes_sha256='c' * 64, backend_source='b' * 40,
            config=self.app.config, now=self.now)

    def activate(self, kind='platform', **changes):
        plan = self.plan(kind)
        observation = AddressObservation(plan_digest(plan), plan.host, plan.kind,
            plan.project_id, self.now, min(plan.valid_until, self.now + 3600))
        arguments = dict(actor_id=self.actor_id, plan=plan, observation=observation,
            approved_plan_sha256=plan_digest(plan), authorize=self.authorize,
            config=self.app.config, now=self.now)
        arguments.update(changes)
        return addresses.activate_address(self.db.session, self.Tenant, self.User, self.Audit, **arguments)

    def resolve(self, host='northside.chatboc.ar'):
        return legacy.resolve_active_host(self.db.session, self.Tenant, host,
            entitlement=self.entitlement, now=self.now)

    def test_three_level_contract_no_eager_migration_or_false_activation(self):
        before = deepcopy(self.tenant().configuracion)
        payload = self.descriptor()
        self.assertEqual(payload['contract_version'], 'organization.addresses.v2')
        self.assertEqual(payload['base'], {'url': 'https://chatboc.ar/t/acceptance-a', 'available': True})
        self.assertEqual(payload['revision'], addresses.revision(self.tenant()))
        self.assertEqual(len(payload['revision']), 64)
        self.assertEqual(payload['version'], 0)
        self.assertTrue(payload['capabilities']['platform']['can_request'])
        self.assertTrue(payload['capabilities']['custom']['can_request'])
        self.assertEqual(payload['capabilities']['activation']['reason_code'], 'operator_runtime_unavailable')
        self.assertFalse(payload['capabilities']['activation']['available'])
        self.assertFalse(payload['auth_e2e_verified'])
        self.assertEqual(self.tenant().configuracion, before)
        self.assertEqual(self.Audit.query.count(), 0)

    def test_platform_pro_accepts_exact_non_slug_host_no_txt_and_resolves_only_when_active(self):
        self.tenant().plan = 'pro'
        self.db.session.commit()
        result = self.save()
        slot = result['addresses']['platform']
        self.assertEqual(slot['status'], 'pending_platform')
        self.assertIsNone(slot['dns_proof'])
        self.assertIsNone(self.resolve())
        self.assertIsNone(self.resolve('acceptance-a.chatboc.ar'))
        self.assertEqual(self.tenant().configuracion['keep'], True)
        self.activate()
        public = self.resolve()
        self.assertEqual(public['contract_version'], 'public.tenant_host.v1')
        self.assertEqual(public['tenant']['id'], self.tenant_id)
        self.assertFalse(public['canonical_redirect_required'])
        self.assertEqual(public['canonical_origin'], public['origin'])
        self.assertNotIn(addresses.KEY, json.dumps(public))
        self.assertIn('provider_evidence', self.Audit.query.order_by(self.Audit.id.desc()).first().details)

    def test_full_custom_and_platform_coexist_and_canonical_has_no_loop(self):
        self.save()
        self.activate()
        self.ready_custom()
        self.activate('custom')
        platform = self.resolve()
        custom = self.resolve('civic.gob.ar')
        self.assertTrue(platform['canonical_redirect_required'])
        self.assertEqual(platform['canonical_origin'], custom['origin'])
        self.assertFalse(custom['canonical_redirect_required'])
        self.assertEqual(custom['canonical_origin'], custom['origin'])
        self.assertEqual(self.descriptor()['canonical_url'], 'https://civic.gob.ar/')
        self.assertIsNone(self.resolve('www.civic.gob.ar'))

    def test_plan_downgrade_suspends_without_removing_and_revoke_works(self):
        self.save()
        self.activate()
        self.ready_custom()
        self.activate('custom')
        stored = deepcopy(self.tenant().configuracion)
        self.tenant().plan = 'pro'
        self.db.session.commit()
        self.assertIsNotNone(self.resolve())
        self.assertIsNone(self.resolve('civic.gob.ar'))
        self.assertFalse(self.resolve()['canonical_redirect_required'])
        self.assertEqual(self.descriptor()['custom']['status'], 'suspended')
        self.tenant().plan = 'free'
        self.db.session.commit()
        self.assertIsNone(self.resolve())
        self.assertEqual(self.tenant().configuracion, stored)
        self.assertTrue(self.descriptor()['base']['available'])
        self.save('platform', 'revoke')
        self.save('custom', 'revoke')
        self.assertEqual(self.descriptor()['platform']['status'], 'revoked')

    def test_new_custom_requires_full_in_v1_and_v2_even_capabilities(self):
        for plan in ('pro', 'free', 'demo'):
            with self.subTest(plan=plan):
                tenant = self.tenant()
                tenant.plan = plan
                tenant.capabilities_json = {'integrations.full_access': True, 'full': True, 'custom_domains': True}
                self.db.session.commit()
                self.assertFalse(self.descriptor()['capabilities']['custom']['can_request'])
                with self.assertRaises(legacy.DomainBindingError):
                    self.save('custom', host='civic.gob.ar')
                descriptor = legacy.build_domain_binding(tenant, can_edit=True,
                    entitled=self.entitlement(tenant), now=self.now)
                with self.assertRaises(legacy.DomainBindingError):
                    legacy.save_domain_binding(self.db.session, self.Tenant, self.User, self.Audit,
                        tenant_id=tenant.id, actor_id=self.actor_id, data={'operation': 'request',
                            'expected_revision': descriptor['revision'], 'host': 'civic.gob.ar'},
                        authorize=self.authorize, entitlement=self.entitlement, now=self.now)
        self.assertEqual(self.Audit.query.count(), 0)

    def test_existing_v1_pro_can_verify_resolve_revoke_but_not_replace(self):
        descriptor = legacy.build_domain_binding(self.tenant(), can_edit=True, entitled=True, now=self.now)
        legacy.save_domain_binding(self.db.session, self.Tenant, self.User, self.Audit,
            tenant_id=self.tenant_id, actor_id=self.actor_id, data={'operation': 'request',
                'expected_revision': descriptor['revision'], 'host': 'civic.gob.ar'},
            authorize=self.authorize, entitlement=self.entitlement, now=self.now)
        self.tenant().plan = 'pro'
        self.db.session.commit()
        self.assertEqual(self.descriptor()['custom']['source'], 'legacy_v1')
        proof = self.descriptor()['custom']['dns_proof']['value']
        self.save('custom', 'verify_dns', reader=lambda _host: [proof])
        self.activate('custom')
        self.assertIsNotNone(self.resolve('civic.gob.ar'))
        self.assertNotIn(addresses.KEY, self.tenant().configuracion)
        self.save('custom', 'revoke')
        self.assertFalse(self.descriptor()['capabilities']['custom']['can_request'])
        with self.assertRaises(legacy.DomainBindingError):
            self.save('custom', host='replacement.gob.ar')
        self.assertIsNone(self.resolve('civic.gob.ar'))

    def test_cross_slot_and_cross_api_claims_conflict(self):
        self.save()
        old = legacy.build_domain_binding(self.tenant(), can_edit=True, entitled=True, now=self.now)
        with self.assertRaises(legacy.DomainBindingError):
            legacy.save_domain_binding(self.db.session, self.Tenant, self.User, self.Audit,
                tenant_id=self.tenant_id, actor_id=self.actor_id,
                data={'operation': 'request', 'expected_revision': old['revision'], 'host': 'northside.chatboc.ar'},
                authorize=self.authorize, entitlement=self.entitlement, now=self.now)
        self.save('custom', host='civic.gob.ar')
        old = legacy.build_domain_binding(self.tenant(), can_edit=True, entitled=True, now=self.now)
        with self.assertRaisesRegex(legacy.DomainBindingError, 'revoke_required'):
            legacy.save_domain_binding(self.db.session, self.Tenant, self.User, self.Audit,
                tenant_id=self.tenant_id, actor_id=self.actor_id,
                data={'operation': 'request', 'expected_revision': old['revision'], 'host': 'different.gob.ar'},
                authorize=self.authorize, entitlement=self.entitlement, now=self.now)
        other = self.tenant(self.accounts['acceptance-b']['tenant_id'])
        with self.assertRaisesRegex(legacy.DomainBindingError, 'host_in_use'):
            addresses.save_addresses(self.db.session, self.Tenant, self.User, self.Audit,
                tenant_id=other.id, actor_id=self.accounts['acceptance-b']['id'],
                data={'operation': 'request', 'kind': 'custom', 'expected_revision': addresses.revision(other),
                    'host': 'civic.gob.ar'}, authorize=self.authorize, config=self.app.config, now=self.now)
        self.assertEqual(self.Audit.query.count(), 2)

    def test_revision_covers_legacy_changes_and_stale_writes_are_not_replayed(self):
        original = self.descriptor()['revision']
        descriptor = legacy.build_domain_binding(self.tenant(), can_edit=True, entitled=True, now=self.now)
        legacy.save_domain_binding(self.db.session, self.Tenant, self.User, self.Audit,
            tenant_id=self.tenant_id, actor_id=self.actor_id, data={'operation': 'request',
                'expected_revision': descriptor['revision'], 'host': 'civic.gob.ar'},
            authorize=self.authorize, entitlement=self.entitlement, now=self.now)
        with self.assertRaises(legacy.DomainBindingError) as caught:
            self.save(revision=original)
        self.assertEqual(caught.exception.status, 412)
        self.assertEqual(self.Audit.query.count(), 1)

    def test_reserved_hosts_alias_map_and_policy_removal_fail_closed(self):
        for host in ('www.chatboc.ar', 'mail.chatboc.ar', 'admin.chatboc.ar', 'a.chatboc.ar',
                'other.vercel.app', 'one.two.chatboc.ar', 'api.chatboc.ar', 'chatboc.ar'):
            with self.subTest(host=host), self.assertRaises(legacy.DomainBindingError):
                self.save(host=host)
        alias = 'institution-preview.vercel.app'
        self.app.config['ORGANIZATION_PLATFORM_ALIAS_BINDINGS_JSON'] = json.dumps({alias:
            {'tenant_id': self.tenant_id, 'tenant_slug': self.tenant().slug}})
        self.save(host=alias)
        self.activate()
        self.assertIsNotNone(self.resolve(alias))
        self.assertIsNone(self.resolve('anything.vercel.app'))
        self.app.config['ORGANIZATION_PLATFORM_ALIAS_BINDINGS_JSON'] = '{}'
        self.assertIsNone(self.resolve(alias))
        self.assertEqual(self.descriptor()['platform']['status'], 'suspended')
        self.assertTrue(self.descriptor()['capabilities']['platform']['can_revoke'])

    def test_alias_configuration_cannot_make_custom_plan_invalid(self):
        self.app.config['ORGANIZATION_PLATFORM_ALIAS_BINDINGS_JSON'] = json.dumps({
            'authorized-alias.vercel.app': {'tenant_id': self.tenant_id, 'tenant_slug': self.tenant().slug}})
        self.ready_custom()
        self.assertEqual(self.plan('custom').allowed_aliases, ())
        self.activate('custom')
        self.assertIsNotNone(self.resolve('civic.gob.ar'))

    def test_exact_readiness_does_not_activate_and_has_no_private_assets(self):
        self.save()
        client = self.app.test_client()
        params = {'host': 'northside.chatboc.ar', 'tenant_id': self.tenant_id,
            'tenant_slug': self.tenant().slug, 'kind': 'platform',
            'revision': self.descriptor()['revision'], 'nonce': 'd' * 64}
        with patch.object(legacy, 'read_dns_txt', side_effect=AssertionError('platform does not use TXT')):
            result = client.get('/api/public/address-readiness', query_string=params)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json['contract_version'], addresses.READINESS)
        self.assertEqual(result.json['scope'], 'pending_address_infrastructure_only')
        self.assertFalse(result.json['active'])
        self.assertFalse(result.json['auth_e2e_verified'])
        self.assertEqual(result.headers['Cache-Control'], 'no-store')
        for changed in ({'host': 'another.chatboc.ar'}, {'kind': 'custom'}, {'revision': 'e' * 64},
                {'tenant_slug': 'acceptance-b'}, {'tenant_id': self.accounts['acceptance-b']['tenant_id']}):
            self.assertEqual(client.get('/api/public/address-readiness', query_string={**params, **changed}).status_code, 404)
        self.assertEqual(self.Audit.query.count(), 1)
        self.assertIsNone(self.resolve())

    def test_real_owner_http_scope_strict_commands_and_no_activation(self):
        owner = self.login()
        get = owner.get(self.endpoint)
        self.assertEqual(get.status_code, 200, get.json)
        self.assertIn('no-store', get.headers['Cache-Control'])
        data = {'kind': 'platform', 'operation': 'request', 'expected_revision': get.json['revision'],
            'host': 'northside.chatboc.ar'}
        for value in ({**data, 'active': True}, {**data, 'kind': []}, {**data, 'operation': []}):
            result = owner.put(self.endpoint, json=value, headers={'Origin': 'https://www.chatboc.ar'})
            self.assertEqual(result.status_code, 400, result.json)
        result = owner.put(self.endpoint, json=data, headers={'Origin': 'https://www.chatboc.ar'})
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json['addresses']['platform']['status'], 'pending_platform')
        self.assertEqual(owner.post(self.endpoint + '/activate', json={}).status_code, 403)
        self.assertEqual(self.login('acceptance-b').get(self.endpoint).status_code, 403)
        self.assertIn(self.app.test_client().get(self.endpoint).status_code, (401, 403))

    def test_failure_rolls_back_private_state_and_audit(self):
        with patch.object(type(self.db.session()), 'commit', side_effect=SQLAlchemyError('synthetic-private')):
            with self.assertRaisesRegex(legacy.DomainBindingError, 'address_save_unconfirmed'):
                self.save()
        self.db.session.remove()
        self.assertEqual(self.descriptor()['platform']['status'], 'unconfigured')
        self.assertEqual(self.Audit.query.count(), 0)

    def test_full_app_operator_uses_real_clerk_lineage_without_ambient_tenant(self):
        from services.auth_session_lifecycle import issue_token
        from services import organization_address_runtime as runtime
        from utils.auth_helpers import auth_session_version
        self.save()
        actor = self.db.session.get(self.User, self.actor_id)
        role, email = actor.rol, actor.email
        actor.rol = 'super_admin'
        self.db.session.commit()
        self.app.config.update(ORGANIZATION_ADDRESS_ACTIVATION_ENABLED=True,
            ORGANIZATION_ADDRESS_VERCEL_TOKEN='synthetic-runtime-provider-token-only',
            ORGANIZATION_ADDRESS_VERCEL_TEAM_ID=runtime.TEAM)
        def observed(_adapter, plan):
            return AddressObservation(plan_digest(plan), plan.host, plan.kind,
                plan.project_id, self.now, self.now + 600)
        try:
            with patch.dict(os.environ, {'CLERK_SUPERADMIN_EMAILS': email}):
                token = issue_token({'user_id': self.actor_id, 'session_kind': 'clerk', 'auth_provider': 'clerk',
                    'clerk_sid': 'synthetic-full-app-address-session', 'sv': auth_session_version(actor),
                    'exp': self.now + 3600, 'audience': 'tenant_owner', 'auth_intent': 'tenant_owner'})
                self.db.session.remove()
                client = self.app.test_client()
                headers = {'Authorization': 'Bearer ' + token, 'Origin': 'https://www.chatboc.ar',
                    'X-Tenant': 'acceptance-b', 'X-Tenant-ID': str(self.accounts['acceptance-b']['tenant_id'])}
                body = {'kind': 'platform', 'expected_revision': self.descriptor()['revision'],
                    'frontend_deployment': 'dpl_1234567890', 'frontend_source': 'a' * 40,
                    'routes_sha256': 'c' * 64}
                self.db.session.remove()
                with patch.object(runtime.AddressVercelAdapter, 'observe', observed):
                    result = client.post(self.endpoint + '/activation-plan', json=body, headers=headers)
                    self.assertEqual(result.status_code, 200, result.json)
                    self.assertEqual(result.json['plan']['tenant_id'], self.tenant_id)
                    self.assertEqual(self.Audit.query.count(), 1)
                    self.db.session.remove()
                    result = client.post(self.endpoint + '/activate', json={**body,
                        'approved_plan_sha256': result.json['plan_sha256']}, headers=headers)
                    self.assertEqual(result.status_code, 200, result.json)
                self.assertTrue(result.json['committed'])
                self.assertTrue(result.json['addresses']['platform']['active'])
                self.assertFalse(result.json['provider_changes_performed'])
                self.assertEqual(self.Audit.query.count(), 2)
                self.assertEqual(self.resolve()['tenant']['id'], self.tenant_id)
        finally:
            self.db.session.rollback()
            self.db.session.get(self.User, self.actor_id).rol = role
            self.db.session.commit()

    def test_private_store_is_removed_from_public_config(self):
        from services.public_tenant_config import sanitize_public_tenant_config
        self.ready_custom()
        original = deepcopy(self.tenant().configuracion)
        public = sanitize_public_tenant_config(original)
        self.assertNotIn(addresses.KEY, public)
        self.assertNotIn(self.descriptor()['custom']['dns_proof']['value'], json.dumps(public))
        self.assertEqual(public['keep'], True)

    def test_generic_settings_and_bulk_paths_cannot_forge_private_authority(self):
        from sqlalchemy import bindparam, insert, literal, select, update
        from sqlalchemy.orm import aliased
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        from services.organization_address_config_guard import AddressConfigGuardError
        forged = {addresses.KEY: {'status': 'active', 'host': 'forged.chatboc.ar'}}
        tenant = self.tenant()
        tenant.configuracion = {**forged, 'keep': True, 'unrelated_setting': 'retained'}
        self.db.session.commit()
        self.assertNotIn(addresses.KEY, tenant.configuracion)
        self.assertEqual(tenant.configuracion['unrelated_setting'], 'retained')
        methods = (
            lambda: self.db.session.query(self.Tenant).filter_by(id=self.tenant_id).update({'configuracion': forged}),
            lambda: self.db.session.execute(update(self.Tenant).where(self.Tenant.id == self.tenant_id).values(configuracion=forged)),
            lambda: self.db.session.execute(update(self.Tenant), [{'id': self.tenant_id, 'configuracion': forged}]),
            lambda: self.db.session.bulk_update_mappings(self.Tenant, [{'id': self.tenant_id, 'configuracion': forged}]),
            lambda: self.db.session.bulk_insert_mappings(self.Tenant, [{'id': 99000, 'slug': 'forged-fixture',
                'nombre': 'Synthetic', 'tipo': 'municipio', 'configuracion': forged}]),
            lambda: self.db.session.execute(insert(self.Tenant).values([{'id': 99000, 'slug': 'forged-fixture',
                'nombre': 'Synthetic', 'tipo': 'municipio', 'configuracion': forged}])),
            lambda: self.db.session.execute(insert(self.Tenant).from_select(['id', 'configuracion'],
                select(literal(99000), literal(forged, type_=self.Tenant.__table__.c.configuracion.type)))),
            lambda: self.db.session.execute(insert(self.Tenant).values(id=99000, slug='forged-fixture',
                nombre='Synthetic', tipo='municipio', configuracion=bindparam('supplied_config')),
                {'supplied_config': forged}),
            lambda: self.db.session.execute(update(aliased(self.Tenant)).values(configuracion=forged)),
            lambda: self.db.session.execute(sqlite_insert(self.Tenant).values(id=self.tenant_id,
                slug=self.tenant().slug, nombre='Synthetic', tipo='municipio', configuracion={'unrelated': True})
                .on_conflict_do_update(index_elements=[self.Tenant.id], set_={'configuracion': {'unrelated': True}})),
        )
        for method in methods:
            with self.subTest(method=method), self.assertRaisesRegex(AddressConfigGuardError, 'bulk_configuration'):
                method()
            self.db.session.rollback()
        # Other columns retain their legitimate existing bulk behavior.
        self.db.session.execute(update(self.Tenant).where(self.Tenant.id == self.tenant_id).values(plan='pro'))
        self.db.session.commit()
        self.db.session.expire_all()
        self.assertEqual(self.tenant().plan, 'pro')
        self.assertEqual(self.Audit.query.count(), 0)

    def test_private_config_guard_keeps_batched_relationship_reads(self):
        # The application guard is already installed. A global ORM execution
        # listener must not make unrelated selectinload + yield_per reads fail.
        from sqlalchemy import Column, ForeignKey, Integer, create_engine
        from sqlalchemy.orm import Session, declarative_base, relationship, selectinload
        base = declarative_base()

        class Parent(base):
            __tablename__ = 'address_guard_read_parent'
            id = Column(Integer, primary_key=True)

        class Child(base):
            __tablename__ = 'address_guard_read_child'
            id = Column(Integer, primary_key=True)
            parent_id = Column(Integer, ForeignKey(Parent.id), nullable=False)

        Parent.children = relationship(Child, order_by=Child.id)
        engine = create_engine('sqlite:///:memory:')
        try:
            base.metadata.create_all(engine)
            with Session(engine) as session:
                session.add_all([Parent(id=1, children=[Child(id=11), Child(id=12)]),
                    Parent(id=2), Parent(id=3, children=[Child(id=31)])])
                session.commit()
                session.expunge_all()
                rows = session.query(Parent).options(selectinload(Parent.children)).order_by(Parent.id).yield_per(2)
                self.assertEqual([(row.id, [child.id for child in row.children]) for row in rows],
                    [(1, [11, 12]), (2, []), (3, [31])])
        finally:
            engine.dispose()

    def test_private_write_permit_is_one_section_one_flush_and_cleared_on_failure(self):
        from services.organization_address_config_guard import address_section_write, _PERMITS
        self.save()
        state = deepcopy(self.tenant().configuracion[addresses.KEY])
        tenant = self.tenant()
        with self.assertRaisesRegex(RuntimeError, 'synthetic_rollback'):
            with address_section_write(self.db.session, tenant, addresses.KEY, state):
                self.db.session.rollback()
                raise RuntimeError('synthetic_rollback')
        self.assertNotIn(_PERMITS, self.db.session.info)
        with address_section_write(self.db.session, tenant, addresses.KEY, state):
            tenant.configuracion = {**tenant.configuracion, 'before_flush': True}
            self.db.session.flush()
            self.assertNotIn(_PERMITS, self.db.session.info)
            # The already-consumed marker cannot replace a private section in a
            # second flush, even while execution is still inside that context.
            tenant.configuracion = {'unrelated_setting': 'after', legacy.KEY: {'status': 'active'}}
            self.db.session.flush()
        self.db.session.commit()
        self.assertEqual(tenant.configuracion[addresses.KEY], state)
        self.assertNotIn(legacy.KEY, tenant.configuracion)
        self.assertEqual(tenant.configuracion['unrelated_setting'], 'after')

    def test_time_is_read_after_waiting_for_lock_and_again_before_commit(self):
        self.save()
        original_lock = legacy._locked_scope
        ticks = [self.now]
        def waited(*args, **kwargs):
            value = original_lock(*args, **kwargs)
            ticks[0] += 61
            return value
        with patch.object(legacy, '_locked_scope', waited):
            with self.assertRaisesRegex(legacy.DomainBindingError, 'platform_not_verified'):
                self.activate(now=None, clock=lambda: ticks[0])
        self.assertEqual(self.descriptor()['platform']['status'], 'pending_platform')
        ticks[0] = self.now
        # Use an explicit clock progression to cover the final guard independently
        # of SQLAlchemy's autoflush implementation details.
        times = iter((self.now, self.now + 61))
        with self.assertRaisesRegex(legacy.DomainBindingError, 'platform_not_verified'):
            self.activate(now=None, clock=lambda: next(times))
        self.assertEqual(self.descriptor()['platform']['status'], 'pending_platform')
        self.assertEqual(self.Audit.query.count(), 1)

    def test_v1_activation_rechecks_clock_after_lock_and_before_commit(self):
        old = legacy.build_domain_binding(self.tenant(), can_edit=True, entitled=True, now=self.now)
        legacy.save_domain_binding(self.db.session, self.Tenant, self.User, self.Audit,
            tenant_id=self.tenant_id, actor_id=self.actor_id, data={'operation': 'request',
                'expected_revision': old['revision'], 'host': 'civic.gob.ar'},
            authorize=self.authorize, entitlement=self.entitlement, now=self.now)
        proof = self.descriptor()['custom']['dns_proof']['value']
        self.save('custom', 'verify_dns', reader=lambda _host: [proof])
        old = legacy.build_domain_binding(self.tenant(), can_edit=True, entitled=True, now=self.now)
        observation = legacy.PlatformObservation('civic.gob.ar', 'prj_existing_frontend', self.now,
            self.now + 3600, True, True, True)
        for clocks in ((self.now + 61,), (self.now, self.now + 61)):
            times = iter(clocks)
            with self.assertRaisesRegex(legacy.DomainBindingError, 'platform_not_verified'):
                legacy.activate_verified_binding(self.db.session, self.Tenant, self.User, self.Audit,
                    tenant_id=self.tenant_id, actor_id=self.actor_id, expected_revision=old['revision'],
                    authorize=self.authorize, entitlement=self.entitlement, observation=observation,
                    expected_project_id='prj_existing_frontend', clock=lambda: next(times))
        self.assertEqual(self.descriptor()['custom']['status'], 'pending_platform')
        self.assertEqual(self.Audit.query.count(), 2)
