"""Real workflow row locks/CAS on the existing disposable loopback PG service."""
from tests.profile_acceptance_runtime import prepare_process

if __name__ == "__main__":
    prepare_process()

from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time
import unittest
from uuid import uuid4

from flask import Flask
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from models import MunicipioTicket, TenantProfile, TicketComentario, User, db
from services.ticket_workflow_policy import TicketWorkflowError, lock_and_validate_workflow_command


class TicketWorkflowPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Fixed loopback components cannot inherit any customer DSN.
        cls.url = URL.create("postgresql+psycopg", username="guide_test", password="guide_test",
            host="127.0.0.1", port=55432, database="guide_control_acceptance")
        cls.root = create_engine(cls.url, connect_args={"connect_timeout": 5})
        cls.schema = "ticket_workflow_" + uuid4().hex
        with cls.root.begin() as connection:
            connection.execute(CreateSchema(cls.schema))
        cls.app = Flask(__name__)
        cls.app.config.update(SQLALCHEMY_DATABASE_URI=cls.url,
            SQLALCHEMY_ENGINE_OPTIONS={"execution_options": {"schema_translate_map": {None: cls.schema}}},
            SQLALCHEMY_TRACK_MODIFICATIONS=False, TESTING=True)
        db.init_app(cls.app)
        with cls.app.app_context():
            cls.engine = db.engine
            pending = [m.__table__ for m in (MunicipioTicket, TenantProfile, User, TicketComentario)]
            for model in (MunicipioTicket, TenantProfile, User, TicketComentario):
                for relation in inspect(model).relationships:
                    if relation.lazy == "joined":
                        pending.append(relation.mapper.local_table)
                        if relation.secondary is not None:
                            pending.append(relation.secondary)
            tables = {}
            while pending:
                table = pending.pop()
                if table.key not in tables:
                    tables[table.key] = table
                    pending.extend(foreign.column.table for foreign in table.foreign_keys)
            User.metadata.create_all(cls.engine, tables=list(tables.values()))
        cls.Session = sessionmaker(bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        with cls.root.begin() as connection:
            connection.execute(DropSchema(cls.schema, cascade=True))
        cls.root.dispose()

    def setUp(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql(f'TRUNCATE "{self.schema}".ticket_comentario, '
                f'"{self.schema}".municipio_ticket, "{self.schema}".tenant_profile, '
                f'"{self.schema}"."user" CASCADE')
        with self.Session.begin() as session:
            actor = User(id=1, name="Synthetic owner", email="workflow-owner@example.invalid",
                rol="admin_municipio", tipo_chat="municipio", password_hash="not-a-login-credential")
            session.add(actor)
            session.flush()
            session.add(TenantProfile(id=1, slug="pg-workflow", nombre="Synthetic tenant", tipo="municipio",
                municipio_id=1, is_active=True))
            session.flush()
            actor.tenant_id, actor.tenant_slug, actor.municipio_id = 1, "pg-workflow", 1
            session.add(MunicipioTicket(id=1, tenant_id=1, municipio_id=1, nro_ticket="PG700001",
                pregunta="Synthetic case", estado="nuevo", categoria="Sugerencia"))

    def apply(self, session, expected="nuevo", destination="en_proceso"):
        actor, tenant = session.get(User, 1), session.get(TenantProfile, 1)
        ticket, state = lock_and_validate_workflow_command(session, MunicipioTicket, 1,
            actor=actor, tenant=tenant, ticket_type="municipio",
            data={"estado": destination, "expected_estado": expected})
        ticket.estado = state
        session.add(TicketComentario(municipio_ticket_id=1, user_id=1, comentario="Synthetic state audit",
            estado_ticket=state, es_admin=True, origen="sistema"))
        session.commit()
        return state

    def test_two_writers_same_expected_state_block_then_only_first_commits(self):
        locked, release, second_started = Event(), Event(), Event()
        second_pid = []
        def first():
            with self.app.app_context(), self.Session() as session:
                actor, tenant = session.get(User, 1), session.get(TenantProfile, 1)
                ticket, state = lock_and_validate_workflow_command(session, MunicipioTicket, 1,
                    actor=actor, tenant=tenant, ticket_type="municipio",
                    data={"estado": "en_proceso", "expected_estado": "nuevo"})
                locked.set()
                if not release.wait(5):
                    raise RuntimeError("Disposable acceptance lock timeout")
                ticket.estado = state
                session.add(TicketComentario(municipio_ticket_id=1, user_id=1, comentario="Synthetic first audit",
                    estado_ticket=state, es_admin=True, origen="sistema"))
                session.commit()
                return state
        def second():
            with self.app.app_context(), self.Session() as session:
                # Identity-map state from before the first writer commits must
                # be refreshed by the production helper's locked SELECT.
                stale_ticket = session.get(MunicipioTicket, 1)
                self.assertEqual(stale_ticket.estado, "nuevo")
                second_pid.append(session.execute(text("SELECT pg_backend_pid()")).scalar_one())
                second_started.set()
                try:
                    return self.apply(session)
                except TicketWorkflowError as error:
                    self.assertEqual(stale_ticket.estado, "en_proceso", "Locked SELECT must refresh the retained stale identity before rollback")
                    session.rollback()
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
                    with self.root.connect() as connection:
                        blocked = bool(connection.execute(text("SELECT pg_blocking_pids(:pid)"),
                            {"pid": second_pid[0]}).scalar_one())
                    if blocked:
                        break
                    time.sleep(.02)
                self.assertTrue(blocked, "Second writer must wait on the actual PostgreSQL row lock")
            finally:
                release.set()
            self.assertEqual(a.result(timeout=5), "en_proceso")
            self.assertEqual(b.result(timeout=5), (409, "stale_ticket_state"))
        with self.Session() as session:
            self.assertEqual(session.get(MunicipioTicket, 1).estado, "en_proceso")
            self.assertEqual(session.query(TicketComentario).count(), 1)

    def test_postgres_comment_trigger_failure_rolls_back_state_atomically(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql(f'CREATE FUNCTION "{self.schema}".reject_workflow_comment() '
                "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Synthetic audit failure'; END; $$")
            connection.exec_driver_sql(f'CREATE TRIGGER reject_workflow_comment BEFORE INSERT ON '
                f'"{self.schema}".ticket_comentario FOR EACH ROW '
                f'EXECUTE FUNCTION "{self.schema}".reject_workflow_comment()')
        try:
            with self.app.app_context(), self.Session() as session:
                with self.assertRaises(DBAPIError):
                    self.apply(session, destination="cerrado")
                session.rollback()
            with self.Session() as session:
                self.assertEqual(session.get(MunicipioTicket, 1).estado, "nuevo")
                self.assertEqual(session.query(TicketComentario).count(), 0)
        finally:
            with self.engine.begin() as connection:
                connection.exec_driver_sql(f'DROP TRIGGER reject_workflow_comment ON "{self.schema}".ticket_comentario')
                connection.exec_driver_sql(f'DROP FUNCTION "{self.schema}".reject_workflow_comment()')


if __name__ == "__main__":
    unittest.main()
