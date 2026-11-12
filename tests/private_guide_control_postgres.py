"""Service locking acceptance on a dedicated loopback CI PostgreSQL database only."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time
import unittest
import uuid
from sqlalchemy import Boolean, Column, Integer, JSON, MetaData, String, create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema
from services.tenant_conversation_guide_control import build_control, save_control, GuideControlError

class GuideControlPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine('postgresql+psycopg://guide_test:guide_test@127.0.0.1:55432/guide_control_acceptance')
        cls.schema = 'guide_control_' + uuid.uuid4().hex
        with cls.engine.begin() as connection: connection.execute(CreateSchema(cls.schema))
        base = declarative_base(metadata=MetaData(schema=cls.schema))
        class Tenant(base):
            __tablename__ = 'tenant'
            id = Column(Integer, primary_key=True); slug = Column(String, nullable=False)
            is_active = Column(Boolean, nullable=False); configuracion = Column(JSON, nullable=False)
        class Actor(base):
            __tablename__ = 'actor'
            id = Column(Integer, primary_key=True)
        class Audit(base):
            __tablename__ = 'audit'
            id = Column(Integer, primary_key=True); tenant_id = Column(Integer); actor_user_id = Column(Integer)
            event_type = Column(String); resource_type = Column(String); resource_id = Column(String); details = Column(JSON)
        cls.Tenant, cls.Actor, cls.Audit, cls.base = Tenant, Actor, Audit, base
        base.metadata.create_all(cls.engine)
        cls.Session = sessionmaker(bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        with cls.engine.begin() as connection: connection.execute(DropSchema(cls.schema, cascade=True))
        cls.engine.dispose()

    def setUp(self):
        with self.Session.begin() as session:
            for model in (self.Audit, self.Tenant, self.Actor): session.query(model).delete()
            session.add(self.Actor(id=1))
            session.add(self.Tenant(id=1, slug='qa-organization', is_active=True, configuracion={'preserved': 1}))
        with self.Session() as session:
            control = build_control(session.get(self.Tenant, 1), can_edit=True)
            artifact = control['installed_guide']
            self.body = {'contract_version': 'tenant.conversation_guide_control_command.v1',
                'tenant': control['tenant'], 'expected_revision': control['revision'],
                'enabled': True, 'guide_id': 'accessible-support-evaluation',
                'acknowledge_evaluation_only': True, 'expected_guide_sha256': artifact['guide_sha256'],
                'expected_source_sha256': artifact['source']['sha256']}

    def save(self, session, authorize=lambda actor, tenant: True):
        return save_control(session, self.Tenant, self.Actor, self.Audit, tenant_id=1,
            tenant_slug='qa-organization', actor_id=1, data=self.body,
            authorize=authorize, writes_blocked=lambda: False)

    def test_two_editors_are_serialized_and_stale_writer_cannot_commit(self):
        locked, release, second_started = Event(), Event(), Event()
        second_pid = []
        def first_authorize(actor, tenant):
            locked.set()
            if not release.wait(4): raise RuntimeError('test lock wait expired')
            return True
        def first():
            with self.Session() as session: return self.save(session, first_authorize)
        def second():
            with self.Session() as session:
                second_pid.append(session.execute(text('select pg_backend_pid()')).scalar_one())
                second_started.set()
                try: return self.save(session)
                except GuideControlError as error: return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(first); self.assertTrue(locked.wait(3))
            b = pool.submit(second); self.assertTrue(second_started.wait(3))
            blocked = False
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    with self.engine.connect() as connection:
                        blocked = bool(connection.execute(text('select pg_blocking_pids(:pid)'), {'pid': second_pid[0]}).scalar_one())
                    if blocked: break
                    time.sleep(.02)
                self.assertTrue(blocked, 'Second transaction must wait for the first row lock')
            finally: release.set()
            self.assertTrue(a.result(timeout=5)['saved'])
            self.assertEqual(b.result(timeout=5), 'guide_control_revision_conflict')
        with self.Session() as session:
            self.assertEqual(session.query(self.Audit).count(), 1)
            self.assertEqual(session.get(self.Tenant, 1).configuracion['private_conversation_guide_version'], 1)

    def test_refresh_under_lock_preserves_other_settings(self):
        with self.Session() as stale:
            stale.get(self.Tenant, 1)
            with self.Session.begin() as other:
                row = other.get(self.Tenant, 1)
                row.configuracion = {'preserved': 1, 'other_setting': {'new': True}}
            self.assertTrue(self.save(stale)['saved'])
        with self.Session() as session:
            self.assertEqual(session.get(self.Tenant, 1).configuracion['other_setting'], {'new': True})
            self.assertEqual(session.query(self.Audit).count(), 1)

if __name__ == '__main__': unittest.main()
