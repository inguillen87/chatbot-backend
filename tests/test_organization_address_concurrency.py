"""Real PostgreSQL v1/v2 writer serialization, using actual models/services.

Opt in through run_organization_address_concurrency_postgres.py. The ordinary
offline suite skips this module instead of reading DATABASE_URL or a .env file.
The runner owns a new loopback/TLS cluster containing only synthetic fixtures.
"""
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
import json
from threading import Event
import time
import unittest

from flask import Flask
from sqlalchemy import event, text

from models import AuditEvent, Role, TenantProfile, User, UserRole, db
from services import organization_addresses as addresses
from services import organization_domain_binding as legacy
from services import organization_address_runtime as runtime
from services.plan_access import tenant_allows_custom_domains
from utils.tenant_admin_access import can_manage_tenant_control_plane
from tests.organization_address_fixtures import set_fixture_config


DATABASE_URL = None  # Assigned only by the dedicated fresh-cluster runner.
CI_SERVICE_MODE = False  # Set only after the fixed GitHub service runner's identity checks.
FIXTURE_DATABASE = 'chatboc_addresses_concurrency_ci'
SLOT = ContextVar('address_concurrency_slot', default=None)
TABLE_LOCK = 'LOCK TABLE tenant_profile IN SHARE ROW EXCLUSIVE MODE'


class OrganizationAddressConcurrencyTests(unittest.TestCase):
    evidence = []

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
        cls.app = Flask('organization-address-concurrency-offline')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI=DATABASE_URL,
            SQLALCHEMY_TRACK_MODIFICATIONS=False, SECRET_KEY='synthetic-local-tests-only',
            CUTOVER_WRITER_FENCE_ENABLED=False, ORGANIZATION_PLATFORM_ALIAS_BINDINGS_JSON='{}')
        db.init_app(cls.app)
        with cls.app.app_context():
            cls.engine = db.engine
            cls.database_errors = []
            def database_error(context):
                error = context.original_exception
                cls.database_errors.append({'sqlstate': getattr(error, 'sqlstate', None),
                    'message': getattr(getattr(error, 'diag', None), 'message_primary', type(error).__name__)})
            cls.database_error = database_error
            event.listen(cls.engine, 'handle_error', cls.database_error)
            # Real mappings plus their FK/eager-join dependencies, not alternate
            # test implementations of the registry, authority or JSON storage.
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
            for identifier in (1, 2):
                actor = User(id=identifier, name='Synthetic address owner',
                    email=f'address-owner-{identifier}@example.invalid', rol='admin',
                    password_hash='synthetic-not-a-login', municipio_id=identifier,
                    tipo_chat='municipio')
                db.session.add(actor)
            db.session.flush()
            for identifier in (1, 2):
                slug = f'address-fixture-{identifier}'
                tenant = TenantProfile(id=identifier, slug=slug, nombre=slug, tipo='municipio',
                    municipio_id=identifier, is_active=True, plan='full', configuracion={'keep': True})
                db.session.add(tenant)
            db.session.flush()
            for identifier in (1, 2):
                actor = db.session.get(User, identifier)
                actor.tenant_id, actor.tenant_slug = identifier, f'address-fixture-{identifier}'
            db.session.commit()
            with cls.engine.connect() as connection:
                assert connection.execute(text('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()')).scalar_one() is (not CI_SERVICE_MODE)
                assert connection.execute(text("SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='tenant_profile' "
                    "AND column_name='configuracion'")).scalar_one() == 'jsonb'

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            event.remove(cls.engine, 'handle_error', cls.database_error)
            cls.engine.dispose()

    def setUp(self):
        self.now = int(time.time())
        self.database_errors.clear()
        with self.app.app_context():
            db.session.query(AuditEvent).delete()
            for tenant in db.session.query(TenantProfile).all():
                set_fixture_config(db.session, tenant, {'keep': True})
                tenant.plan, tenant.dominio = 'full', None
            db.session.commit()

    def state(self, api, tenant_id):
        with self.app.app_context():
            tenant = db.session.get(TenantProfile, tenant_id)
            if api == 'v1':
                return legacy.build_domain_binding(tenant, can_edit=True, entitled=True, now=self.now)
            return addresses.build_addresses(tenant, can_manage=True, config=self.app.config, now=self.now)

    def save(self, api, tenant_id, operation, revision, *, host=None, txt_reader=None, slot=None):
        marker = SLOT.set(slot)
        try:
            with self.app.app_context():
                data = {'operation': operation, 'expected_revision': revision}
                if operation == 'request':
                    data['host'] = host
                arguments = dict(tenant_id=tenant_id, actor_id=tenant_id, data=data,
                    authorize=can_manage_tenant_control_plane, now=self.now, txt_reader=txt_reader)
                try:
                    if api == 'v1':
                        return legacy.save_domain_binding(db.session, TenantProfile, User, AuditEvent,
                            entitlement=tenant_allows_custom_domains, **arguments)
                    data['kind'] = 'custom'
                    if api == 'runtime':
                        with runtime._transaction(self.engine, read_only=False) as session:
                            self.assertEqual(session.execute(text('SHOW transaction_isolation')).scalar_one(), 'read committed')
                            # An early query must not pin the tenant snapshot
                            # before this contender waits for the namespace.
                            session.query(TenantProfile).count()
                            return addresses.save_addresses(session, TenantProfile, User, AuditEvent,
                                config=self.app.config, **arguments)
                    return addresses.save_addresses(db.session, TenantProfile, User, AuditEvent,
                        config=self.app.config, **arguments)
                except legacy.DomainBindingError as error:
                    return {'saved': False, 'code': error.code, 'status': error.status}
                finally:
                    db.session.remove()
        finally:
            SLOT.reset(marker)

    def race(self, winner_api, loser_api):
        host = 'address-collision.example.org'
        revisions = [self.state(winner_api, 1)['revision'], self.state(loser_api, 2)['revision']]
        locked, attempted, release = Event(), Event(), Event()
        pids = {}

        def before(connection, _cursor, statement, *_):
            if statement == TABLE_LOCK and SLOT.get() == 'contender':
                pids['contender'] = connection.connection.driver_connection.info.backend_pid
                attempted.set()

        def after(connection, _cursor, statement, *_):
            if statement == TABLE_LOCK and SLOT.get() == 'holder':
                pids['holder'] = connection.connection.driver_connection.info.backend_pid
                locked.set()
                self.assertTrue(release.wait(5), 'Table-lock holder was not released')

        event.listen(self.engine, 'before_cursor_execute', before)
        event.listen(self.engine, 'after_cursor_execute', after)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                winner = pool.submit(self.save, winner_api, 1, 'request', revisions[0], host=host, slot='holder')
                try:
                    self.assertTrue(locked.wait(3), 'First writer did not acquire the actual table lock')
                    loser = pool.submit(self.save, loser_api, 2, 'request', revisions[1], host=host, slot='contender')
                    self.assertTrue(attempted.wait(2), 'Second writer did not attempt the shared lock')
                    self.assertNotEqual(pids['holder'], pids['contender'])
                    deadline = time.monotonic() + 1.5
                    while True:
                        with self.engine.connect() as monitor:
                            blockers = monitor.execute(text('SELECT pg_blocking_pids(:pid)'),
                                {'pid': pids['contender']}).scalar_one()
                        if pids['holder'] in blockers:
                            break
                        self.assertLess(time.monotonic(), deadline, 'Physical PostgreSQL blocking was not observed')
                        Event().wait(.01)
                    self.assertFalse(loser.done(), 'Contender completed while the holder still owned the lock')
                finally:
                    release.set()
                first, second = winner.result(timeout=5), loser.result(timeout=5)
        finally:
            release.set()
            event.remove(self.engine, 'before_cursor_execute', before)
            event.remove(self.engine, 'after_cursor_execute', after)
        self.assertIs(first.get('saved'), True, {'result': first, 'database_errors': self.database_errors})
        self.assertEqual(second, {'saved': False,
            'code': 'host_in_use' if loser_api == 'v1' else 'address_host_in_use', 'status': 409})
        with self.app.app_context():
            tenants = db.session.query(TenantProfile).order_by(TenantProfile.id).all()
            claims = [(tenant.id, kind, row['host']) for tenant in tenants
                for kind, row in addresses.claim_rows(tenant)]
            self.assertEqual(claims, [(1, 'legacy_v1' if winner_api == 'v1' else 'custom', host)])
            self.assertEqual(tenants[1].configuracion, {'keep': True})
            self.assertEqual(legacy.read_record(tenants[1])['version'], 0)
            self.assertEqual(addresses.read_store(tenants[1])['version'], 0)
            audits = db.session.query(AuditEvent).all()
            self.assertEqual(len(audits), 1, 'Losing transaction left a phantom audit')
            self.assertEqual((audits[0].tenant_id, audits[0].actor_user_id), (1, 1))
            self.assertEqual(audits[0].event_type,
                'organization.domain.request' if winner_api == 'v1' else 'organization.address.request')
            self.assertNotIn('challenge', json.dumps(audits[0].details))
        self.evidence.append({'case': winner_api + '_holder_' + loser_api + '_contender',
            'independent_backend_pids': True, 'physical_table_lock_observed': True,
            'successes': 1, 'conflicts': 1, 'claims': 1, 'audits': 1})

    def test_v1_holder_v2_contender_has_one_claim_and_one_audit(self):
        self.race('v1', 'v2')

    def test_v2_holder_v1_contender_has_one_claim_and_one_audit(self):
        self.race('v2', 'v1')

    def test_runtime_write_sees_winner_after_wait_even_with_prior_read(self):
        self.race('v1', 'runtime')

    def test_txt_callback_precedes_actual_table_lock_for_both_writers(self):
        for tenant_id, api in enumerate(('v1', 'v2'), 1):
            with self.subTest(api=api):
                host = f'address-txt-{api}.example.org'
                requested = self.save(api, tenant_id, 'request', self.state(api, tenant_id)['revision'], host=host)
                self.assertIs(requested.get('saved'), True,
                    {'result': requested, 'database_errors': self.database_errors})
                state = self.state(api, tenant_id)
                expected = (state if api == 'v1' else state['custom'])['dns_proof']['value']
                trace = []

                def before(_connection, _cursor, statement, *_):
                    if statement == TABLE_LOCK:
                        trace.append('table_lock')

                def reader(value):
                    self.assertEqual(value, host)
                    self.assertEqual(trace, [], 'TXT was called after acquiring or attempting the table lock')
                    pid = db.session.execute(text('SELECT pg_backend_pid()')).scalar_one()
                    with self.engine.connect() as monitor:
                        locks = monitor.execute(text("SELECT mode FROM pg_locks WHERE pid=:pid "
                            "AND relation='tenant_profile'::regclass AND granted "
                            "AND mode <> 'AccessShareLock'"), {'pid': pid}).scalars().all()
                    self.assertEqual(locks, [], 'TXT held a tenant writer or row lock')
                    trace.append('txt')
                    return [expected]

                event.listen(self.engine, 'before_cursor_execute', before)
                try:
                    verified = self.save(api, tenant_id, 'verify_dns', state['revision'], txt_reader=reader)
                finally:
                    event.remove(self.engine, 'before_cursor_execute', before)
                self.assertIs(verified.get('saved'), True, verified)
                self.assertEqual(trace, ['txt', 'table_lock'])
                state = self.state(api, tenant_id)
                self.assertEqual((state if api == 'v1' else state['custom'])['status'], 'pending_platform')
                with self.app.app_context():
                    self.assertEqual(db.session.query(AuditEvent).filter_by(tenant_id=tenant_id).count(), 2)
                self.evidence.append({'case': api + '_txt_before_table_lock',
                    'txt_calls': 1, 'table_lock_attempts': 1, 'writer_locks_during_txt': 0})
