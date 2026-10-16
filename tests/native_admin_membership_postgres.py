"""Real account models and transactions in a disposable loopback PG schema.

The fixed endpoint belongs to the existing CI service, never a customer DSN.
No authorization provider is mocked and no login credentials are exercised.
"""
from tests.profile_acceptance_runtime import prepare_process

if __name__ == '__main__':
    prepare_process()

from concurrent.futures import ThreadPoolExecutor
import os
from threading import Event
import time
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from models import AdminAuditLog, TenantProfile, User
from services.native_admin_membership import (
    NativeAdminMembershipError, normalize_native_admin_membership,
    read_native_admin_membership,
)


class NativeAdminMembershipPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Explicit components prevent an environment variable from selecting a
        # remote database. The service has synthetic, disposable CI credentials.
        cls.root_engine = create_engine(URL.create('postgresql+psycopg',
            username='guide_test', password='guide_test', host='127.0.0.1',
            port=55432, database='guide_control_acceptance'),
            connect_args={'connect_timeout': 5}, pool_pre_ping=True)
        cls.schema = 'native_admin_' + uuid4().hex
        with cls.root_engine.begin() as connection:
            connection.execute(CreateSchema(cls.schema))
        cls.engine = cls.root_engine.execution_options(schema_translate_map={None: cls.schema})
        # Create the real model tables plus their FK dependency closure, without
        # changing their global metadata or creating the rest of the product DB.
        pending = [User.__table__, TenantProfile.__table__, AdminAuditLog.__table__]
        for model in (User, TenantProfile, AdminAuditLog):
            for relation in inspect(model).relationships:
                if relation.lazy == 'joined':
                    pending.append(relation.mapper.local_table)
                    if relation.secondary is not None:
                        pending.append(relation.secondary)
        tables = {}
        while pending:
            table = pending.pop()
            if table.key in tables:
                continue
            tables[table.key] = table
            pending.extend(foreign_key.column.table for foreign_key in table.foreign_keys)
        User.metadata.create_all(cls.engine, tables=list(tables.values()))
        cls.Session = sessionmaker(bind=cls.engine)
        cls.prior_allowlist = os.environ.get('CLERK_SUPERADMIN_EMAILS')
        os.environ['CLERK_SUPERADMIN_EMAILS'] = 'platform-pg-acceptance@example.invalid'

    @classmethod
    def tearDownClass(cls):
        if cls.prior_allowlist is None:
            os.environ.pop('CLERK_SUPERADMIN_EMAILS', None)
        else:
            os.environ['CLERK_SUPERADMIN_EMAILS'] = cls.prior_allowlist
        with cls.root_engine.begin() as connection:
            connection.execute(DropSchema(cls.schema, cascade=True))
        cls.root_engine.dispose()

    def setUp(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql(f'TRUNCATE "{self.schema}".admin_audit_log, '
                f'"{self.schema}".tenant_profile, "{self.schema}"."user", '
                f'"{self.schema}".rubro CASCADE')
        with self.Session.begin() as session:
            # No native credential is valid here; these fixtures exercise only
            # the original ORM membership service and its real DB transactions.
            session.add_all([
                User(id=1, name='Institution owner', email='owner-pg@example.invalid',
                    password_hash='not-a-login-credential', rol='admin', tipo_chat='municipio'),
                User(id=2, name='Existing native administrator', email='target-pg@example.invalid',
                    password_hash='not-a-login-credential', rol='admin_municipio',
                    tipo_chat='municipio', municipio_id=2),
                User(id=3, name='Platform administrator', email='platform-pg-acceptance@example.invalid',
                    password_hash='not-a-login-credential', rol='super_admin',
                    accesibilidad={'auth': {'provider': 'clerk', 'session_version': 1,
                        'clerk': {'user_id': 'isolated-pg-actor'}}}),
            ])
            session.flush()
            session.add(TenantProfile(id=1, slug='pg-membership-acceptance',
                nombre='PG membership acceptance', tipo='municipio', municipio_id=1,
                is_active=True))
            session.flush()
            for user_id in (1, 2):
                user = session.get(User, user_id)
                user.tenant_id = 1
                user.tenant_slug = 'pg-membership-acceptance'
        with self.Session() as session:
            self.review = read_native_admin_membership(session, actor=session.get(User, 3),
                slug='pg-membership-acceptance', user_id=2)
        self.body = {'expected_revision': self.review['expected_revision'], 'request_id': str(uuid4())}

    def apply(self, session, body=None):
        return normalize_native_admin_membership(session, actor=session.get(User, 3),
            actor_session_version=1, slug='pg-membership-acceptance', user_id=2,
            data=body or self.body)

    def _concurrent_actions(self, second_body):
        locked, release, second_started = Event(), Event(), Event()
        second_pid = []

        def first():
            with self.Session() as session:
                session.execute(select(TenantProfile).where(TenantProfile.id == 1).with_for_update()).scalar_one()
                locked.set()
                if not release.wait(5):
                    raise RuntimeError('Acceptance row-lock wait expired')
                return self.apply(session)

        def second():
            with self.Session() as session:
                second_pid.append(session.execute(text('SELECT pg_backend_pid()')).scalar_one())
                second_started.set()
                try:
                    return self.apply(session, second_body)
                except NativeAdminMembershipError as error:
                    return error.status, error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(first)
            self.assertTrue(locked.wait(3))
            b = pool.submit(second)
            self.assertTrue(second_started.wait(3))
            try:
                blocked = False
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    with self.root_engine.connect() as connection:
                        blocked = bool(connection.execute(text('SELECT pg_blocking_pids(:pid)'),
                            {'pid': second_pid[0]}).scalar_one())
                    if blocked:
                        break
                    time.sleep(.02)
                self.assertTrue(blocked, 'The second action must wait for the actual PostgreSQL row lock')
            finally:
                release.set()
            return a.result(timeout=5), b.result(timeout=5)

    def _assert_one_applied_audit(self):
        with self.Session() as session:
            self.assertEqual(session.get(User, 2).municipio_id, 1)
            self.assertEqual(session.query(AdminAuditLog).count(), 1)
            self.assertEqual(session.get(User, 2).password_hash, 'not-a-login-credential')
            self.assertEqual(session.get(User, 2).rol, 'admin_municipio')
            self.assertEqual(session.get(User, 2).tenant_id, 1)

    def test_concurrent_stale_revision_waits_then_cannot_commit(self):
        second_body = {**self.body, 'request_id': str(uuid4())}
        first, second = self._concurrent_actions(second_body)
        self.assertTrue(first['action_receipt']['applied'])
        self.assertEqual(second, (412, 'legacy_membership_revision_conflict'))
        self._assert_one_applied_audit()

    def test_concurrent_same_request_returns_same_receipt_and_single_audit(self):
        first, second = self._concurrent_actions(self.body)
        self.assertFalse(first['idempotent_replay'])
        self.assertTrue(second['idempotent_replay'])
        self.assertEqual(second['action_receipt'], first['action_receipt'])
        self._assert_one_applied_audit()

    def test_real_postgres_audit_failure_rolls_back_account_change(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql(f'CREATE FUNCTION "{self.schema}".reject_membership_audit() '
                "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                "RAISE EXCEPTION 'Acceptance audit unavailable'; END; $$")
            connection.exec_driver_sql(f'CREATE TRIGGER reject_membership_audit BEFORE INSERT ON '
                f'"{self.schema}".admin_audit_log FOR EACH ROW '
                f'EXECUTE FUNCTION "{self.schema}".reject_membership_audit()')
        try:
            with self.Session() as session, self.assertRaises(NativeAdminMembershipError) as failure:
                self.apply(session)
            self.assertEqual(failure.exception.status, 503)
            with self.Session() as session:
                self.assertEqual(session.get(User, 2).municipio_id, 2)
                self.assertEqual(session.query(AdminAuditLog).count(), 0)
        finally:
            with self.engine.begin() as connection:
                connection.exec_driver_sql(f'DROP TRIGGER reject_membership_audit ON "{self.schema}".admin_audit_log')
                connection.exec_driver_sql(f'DROP FUNCTION "{self.schema}".reject_membership_audit()')


if __name__ == '__main__':
    unittest.main()
