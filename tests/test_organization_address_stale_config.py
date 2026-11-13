"""Real PG JSONB guard against stale, unrelated tenant configuration writers.

Activation fixtures use the real request/TXT/activation services with synthetic
trusted observations; they are not production acceptance or SQL-seeded bindings.
The generic writer is paused at its UPDATE, after any Python-side merge, so a
provider-independent request/revoke can commit before the SQL actually executes.
"""
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from copy import deepcopy
from hashlib import sha256
from threading import Event
import time
import unittest
from uuid import uuid4

from flask import Flask
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from models import AuditEvent, Role, TenantProfile, User, UserRole, db
from services import organization_addresses as addresses
from services import organization_domain_binding as legacy
from services.organization_address_vercel import AddressObservation, plan_digest
from services.organization_domain_vercel import FRONTEND_PROJECT, canonical
from services.plan_access import tenant_allows_custom_domains
from utils.tenant_admin_access import can_manage_tenant_control_plane


DATABASE_URL = None  # Only the fresh loopback/TLS runner sets this.
CI_SERVICE_MODE = False  # Only the fixed GitHub service runner may enable this mode.
FIXTURE_DATABASE = 'chatboc_addresses_concurrency_ci'
WRITER = ContextVar('stale_address_config_writer', default=None)
ROUTES_HASH = sha256(canonical([{'src': '/api/(.*)', 'dest': 'https://api.chatboc.ar/api/$1'}])).hexdigest()


class OrganizationAddressStaleConfigTests(unittest.TestCase):
    evidence = []
    CONFIGURATION_STORAGE = 'jsonb'

    @classmethod
    def setUpClass(cls):
        if DATABASE_URL is None:
            raise unittest.SkipTest('Requires the dedicated fresh-loopback PostgreSQL runner')
        local_transport = (DATABASE_URL.query.get('sslmode') == 'verify-full' or
            (CI_SERVICE_MODE is True and DATABASE_URL.port == 5432
                and DATABASE_URL.username == 'postgres' and DATABASE_URL.password is None
                and dict(DATABASE_URL.query) == {'sslmode': 'disable'}))
        if (DATABASE_URL.host != '127.0.0.1' or DATABASE_URL.database != FIXTURE_DATABASE or not local_transport):
            raise RuntimeError('Explicit disposable loopback TLS fixture required')
        cls.app = Flask('address-stale-config-postgres-offline')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI=DATABASE_URL,
            SQLALCHEMY_TRACK_MODIFICATIONS=False, SECRET_KEY='synthetic-only-local-fixture',
            CUTOVER_WRITER_FENCE_ENABLED=False, ORGANIZATION_PLATFORM_ALIAS_BINDINGS_JSON='{}')
        db.init_app(cls.app)
        with cls.app.app_context():
            cls.engine = db.engine
            pending = [model.__table__ for model in (User, TenantProfile, AuditEvent, Role, UserRole)]
            pending.extend(db.metadata.tables[name] for name in ('empleado_categoria', 'categorias_ticket'))
            tables = set()
            while pending:
                table = pending.pop()
                if table in tables:
                    continue
                tables.add(table)
                pending.extend(key.column.table for key in table.foreign_keys)
            db.metadata.create_all(cls.engine, tables=list(tables))
            if cls.CONFIGURATION_STORAGE == 'json':
                # Reproduce the physical types from the original migrations,
                # rather than assuming create_all matches an upgraded database.
                with cls.engine.begin() as connection:
                    connection.execute(text('ALTER TABLE tenant_profile ALTER COLUMN configuracion TYPE json USING configuracion::json'))
                    connection.execute(text('ALTER TABLE audit_event ALTER COLUMN details TYPE json USING details::json'))
            with cls.engine.connect() as connection:
                assert connection.execute(text('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()')).scalar_one() is (not CI_SERVICE_MODE)
                data_type = connection.execute(text("SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='tenant_profile' AND column_name='configuracion'"))
                assert data_type.scalar_one() == cls.CONFIGURATION_STORAGE

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            cls.engine.dispose()

    def setUp(self):
        self.now = int(time.time())

    def tenant(self):
        """Each case gets a new owner/tenant; no test bypass erases private keys."""
        with self.app.app_context():
            name = 'stale-fixture-' + uuid4().hex[:12]
            actor = User(name=name, email=name + '@example.invalid', rol='admin',
                password_hash='synthetic-not-a-login', tipo_chat='municipio')
            db.session.add(actor)
            db.session.flush()
            actor.municipio_id = actor.id
            tenant = TenantProfile(slug=name, nombre=name, tipo='municipio', municipio_id=actor.id,
                is_active=True, plan='full', configuracion={'keep': True})
            db.session.add(tenant)
            db.session.flush()
            actor.tenant_id, actor.tenant_slug = tenant.id, tenant.slug
            identifier = tenant.id
            db.session.commit()
            return identifier

    def state(self, api, tenant_id):
        with self.app.app_context():
            tenant = db.session.get(TenantProfile, tenant_id)
            if api == 'v1':
                return legacy.build_domain_binding(tenant, can_edit=True, entitled=True, now=self.now)
            return addresses.build_addresses(tenant, can_manage=True, config=self.app.config, now=self.now)

    def config(self, tenant_id):
        with self.app.app_context():
            return deepcopy(db.session.get(TenantProfile, tenant_id).configuracion)

    def test_platform_request_commits_claim_and_audit(self):
        tenant_id = self.tenant()
        host = f'platform-claim-{tenant_id}.chatboc.ar'
        with self.app.app_context():
            tenant = db.session.get(TenantProfile, tenant_id)
            before = addresses.revision(tenant)
            result = addresses.save_addresses(db.session, TenantProfile, User, AuditEvent,
                tenant_id=tenant_id, actor_id=tenant.municipio_id,
                data={'kind': 'platform', 'operation': 'request', 'host': host,
                    'expected_revision': before},
                authorize=can_manage_tenant_control_plane, config=self.app.config, now=self.now)
            self.assertIs(result['saved'], True)
            self.assertEqual(result['addresses']['platform']['status'], 'pending_platform')
            self.assertFalse(result['addresses']['platform']['active'])
        with self.app.app_context():
            tenant = db.session.get(TenantProfile, tenant_id)
            self.assertIs(tenant.configuracion['keep'], True)
            self.assertEqual(tenant.configuracion[addresses.KEY]['platform']['host'], host)
            self.assertNotEqual(addresses.revision(tenant), before)
            audit = AuditEvent.query.filter_by(tenant_id=tenant_id).one()
            self.assertEqual(audit.event_type, 'organization.address.request')
            self.assertEqual(audit.details['previous_revision'], before)
            self.assertEqual(audit.details['revision'], addresses.revision(tenant))
            self.assertFalse(audit.details['provider_changes_performed'])
        self.evidence.append({'case': 'v2_platform_request_and_audit',
            'storage': self.CONFIGURATION_STORAGE, 'protected_status': 'pending_platform',
            'audit_events': 1, 'provider_changes_performed': False})

    def save(self, api, tenant_id, operation):
        with self.app.app_context():
            tenant = db.session.get(TenantProfile, tenant_id)
            state = self.state(api, tenant_id)
            data = {'operation': operation, 'expected_revision': state['revision']}
            if operation == 'request':
                data['host'] = f'stale-claim-{tenant_id}.example.org'
            proof = None
            if operation == 'verify_dns':
                proof = (state if api == 'v1' else state['custom'])['dns_proof']['value']
            arguments = dict(tenant_id=tenant_id, actor_id=tenant.municipio_id, data=data,
                authorize=can_manage_tenant_control_plane, now=self.now,
                txt_reader=lambda host: [proof])
            if api == 'v1':
                return legacy.save_domain_binding(db.session, TenantProfile, User, AuditEvent,
                    entitlement=tenant_allows_custom_domains, **arguments)
            data['kind'] = 'custom'
            return addresses.save_addresses(db.session, TenantProfile, User, AuditEvent,
                config=self.app.config, **arguments)

    def activate(self, api, tenant_id):
        self.assertIs(self.save(api, tenant_id, 'request')['saved'], True)
        self.assertIs(self.save(api, tenant_id, 'verify_dns')['saved'], True)
        with self.app.app_context():
            tenant = db.session.get(TenantProfile, tenant_id)
            if api == 'v1':
                row = legacy.read_record(tenant)
                observation = legacy.PlatformObservation(row['host'], FRONTEND_PROJECT,
                    self.now, self.now + 3600, True, True, True)
                result = legacy.activate_verified_binding(db.session, TenantProfile, User, AuditEvent,
                    tenant_id=tenant_id, actor_id=tenant.municipio_id,
                    expected_revision=legacy.revision(tenant, row), authorize=can_manage_tenant_control_plane,
                    entitlement=tenant_allows_custom_domains, observation=observation,
                    expected_project_id=FRONTEND_PROJECT, now=self.now)
            else:
                plan = addresses.prepare_plan(tenant, kind='custom', expected_revision=addresses.revision(tenant),
                    frontend_deployment='dpl_1234567890', frontend_source='a' * 40, backend_source='b' * 40,
                    routes_sha256=ROUTES_HASH, config=self.app.config, now=self.now)
                observation = AddressObservation(plan_digest(plan), plan.host, plan.kind,
                    FRONTEND_PROJECT, self.now, self.now + 3600)
                result = addresses.activate_address(db.session, TenantProfile, User, AuditEvent,
                    actor_id=tenant.municipio_id, plan=plan, observation=observation,
                    approved_plan_sha256=plan_digest(plan), authorize=can_manage_tenant_control_plane,
                    config=self.app.config, now=self.now)
            self.assertIs(result['saved'], True)
        state = self.state(api, tenant_id)
        self.assertIs((state if api == 'v1' else state['custom'])['active'], True)

    def interleave_generic_update(self, tenant_id, concurrent_change):
        """An old ORM snapshot reaches SQL; another connection commits first."""
        update_ready, release = Event(), Event()
        observed = {}

        def before(connection, _cursor, statement, *_):
            if (WRITER.get() == 'generic' and statement.lstrip().upper().startswith('UPDATE ')
                    and 'TENANT_PROFILE' in statement.upper()):
                observed['generic_pid'] = connection.connection.driver_connection.info.backend_pid
                update_ready.set()
                self.assertTrue(release.wait(8), 'Generic UPDATE was not released')

        def generic():
            marker = WRITER.set('generic')
            try:
                with self.app.app_context(), Session(self.engine) as session:
                    tenant = session.get(TenantProfile, tenant_id)
                    snapshot = deepcopy(tenant.configuracion)
                    observed['snapshot'] = snapshot
                    tenant.configuracion = {**snapshot, 'unrelated_setting': {'saved': True}}
                    session.commit()
            finally:
                WRITER.reset(marker)

        event.listen(self.engine, 'before_cursor_execute', before)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                writer = pool.submit(generic)
                try:
                    self.assertTrue(update_ready.wait(5), 'Generic writer did not reach its actual UPDATE')
                    with self.app.app_context():
                        other_pid = db.session.execute(text('SELECT pg_backend_pid()')).scalar_one()
                        self.assertNotEqual(observed['generic_pid'], other_pid)
                    concurrent_change()
                finally:
                    release.set()
                writer.result(timeout=5)
        finally:
            release.set()
            event.remove(self.engine, 'before_cursor_execute', before)
        result = self.config(tenant_id)
        self.assertEqual(result['unrelated_setting'], {'saved': True})
        self.assertIs(result['keep'], True)
        return observed['snapshot'], result

    def test_stale_active_snapshot_cannot_resurrect_a_committed_revoke(self):
        for api, key in [('v1', legacy.KEY), ('v2', addresses.KEY)]:
            with self.subTest(api=api):
                tenant_id = self.tenant()
                self.activate(api, tenant_id)
                expected = {}
                def revoke():
                    self.assertIs(self.save(api, tenant_id, 'revoke')['saved'], True)
                    expected.update(self.config(tenant_id))
                old, result = self.interleave_generic_update(tenant_id, revoke)
                old_record = old[key] if api == 'v1' else old[key]['custom']
                self.assertEqual(old_record['status'], 'active')
                self.assertEqual(result[key], expected[key], 'Stale JSON resurrected or altered the revoke')
                record = result[key] if api == 'v1' else result[key]['custom']
                self.assertEqual(record['status'], 'revoked')
                with self.app.app_context():
                    self.assertIsNone(addresses.resolve_host(db.session, TenantProfile,
                        f'stale-claim-{tenant_id}.example.org', legacy_entitlement=tenant_allows_custom_domains,
                        config=self.app.config, now=self.now))
                    self.assertEqual(AuditEvent.query.filter_by(tenant_id=tenant_id).count(), 4)
                self.evidence.append({'case': api + '_stale_active_after_revoke',
                    'protected_status': 'revoked', 'unrelated_write_saved': True,
                    'interleaving_at_sql_update': True, 'audit_events': 4})

    def test_pre_request_snapshot_cannot_delete_a_new_claim(self):
        for api, key in [('v1', legacy.KEY), ('v2', addresses.KEY)]:
            with self.subTest(api=api):
                tenant_id = self.tenant()
                expected = {}
                def request():
                    self.assertIs(self.save(api, tenant_id, 'request')['saved'], True)
                    expected.update(self.config(tenant_id))
                old, result = self.interleave_generic_update(tenant_id, request)
                self.assertNotIn(key, old)
                self.assertEqual(result[key], expected[key], 'Stale JSON deleted or altered the new claim')
                with self.app.app_context():
                    tenant = db.session.get(TenantProfile, tenant_id)
                    claims = addresses.claim_rows(tenant)
                    self.assertEqual(len(claims), 1)
                    self.assertEqual(claims[0][1]['status'], 'pending_dns')
                    self.assertEqual(AuditEvent.query.filter_by(tenant_id=tenant_id).count(), 1)
                self.evidence.append({'case': api + '_stale_empty_after_request',
                    'claims': 1, 'unrelated_write_saved': True,
                    'interleaving_at_sql_update': True, 'audit_events': 1})

    def test_generic_writer_cannot_invent_private_address_state(self):
        for api, key in [('v1', legacy.KEY), ('v2', addresses.KEY)]:
            with self.subTest(api=api):
                donor, target = self.tenant(), self.tenant()
                self.activate(api, donor)
                forged = deepcopy(self.config(donor)[key])
                with self.app.app_context(), Session(self.engine) as session:
                    tenant = session.get(TenantProfile, target)
                    forged['tenant_id'], forged['tenant_slug'] = tenant.id, tenant.slug
                    row = forged if api == 'v1' else forged['custom']
                    row['host'] = f'forged-claim-{target}.example.org'
                    tenant.configuracion = {**tenant.configuracion, key: forged, 'unrelated_setting': True}
                    session.commit()
                current = self.config(target)
                self.assertNotIn(key, current, 'Generic configuration writer inserted a private active binding')
                self.assertIs(current['unrelated_setting'], True)
                with self.app.app_context():
                    tenant = db.session.get(TenantProfile, target)
                    self.assertEqual(addresses.claim_rows(tenant), [])
                    self.assertEqual(AuditEvent.query.filter_by(tenant_id=target).count(), 0)
                self.evidence.append({'case': api + '_generic_private_injection',
                    'claims': 0, 'unrelated_write_saved': True, 'audit_events': 0})


class OrganizationAddressLegacyJsonConfigTests(OrganizationAddressStaleConfigTests):
    """Same real-model guard cases on the legacy PostgreSQL JSON columns."""
    CONFIGURATION_STORAGE = 'json'
    evidence = []
