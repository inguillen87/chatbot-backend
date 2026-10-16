"""Actual PostgreSQL FOR SHARE across HTTP/provider and WSGI body boundaries.

Runs only within the repository's fresh-loopback PostgreSQL runner. Provider
effects are explicit local events; no remote network or customer data is used.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
from threading import Event, Lock
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from flask import Flask, Response, current_app, request, session
from flask_sqlalchemy import SQLAlchemy
from psycopg import sql
from sqlalchemy import create_engine, event, text
from werkzeug.test import EnvironBuilder

from middleware.cutover_writer_fence import register_cutover_writer_fence
from services import global_writer_authority as authority
from cutover_writer_fence import cutover_writer_view
from utils.migration_managed_session import init_migration_managed_session


DATABASE_URL = None


class HttpWriterAuthorityLeasePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if DATABASE_URL is None or DATABASE_URL.host != '127.0.0.1':
            raise RuntimeError('A new dedicated loopback PostgreSQL cluster is required')
        cls.root = create_engine(DATABASE_URL, hide_parameters=True)
        cls.control_role = 'http_lease_control_' + uuid4().hex
        import secrets
        password = secrets.token_urlsafe(32)
        root_path = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('http_lease_authority_migration',
            root_path / 'migrations/versions/20260829_add_global_writer_authority.py')
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with cls.root.begin() as connection:
            if connection.execute(text("SELECT to_regclass('public.cutover_global_writer_authority')")).scalar_one() is not None:
                raise RuntimeError('HTTP lease fixture authority schema must be empty')
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            native = connection.connection.driver_connection
            native.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {}')
                           .format(sql.Identifier(cls.control_role), sql.Literal(password)))
            native.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}')
                           .format(sql.Identifier(DATABASE_URL.database), sql.Identifier(cls.control_role)))
            native.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(cls.control_role)))
            native.execute(sql.SQL('GRANT SELECT,UPDATE(authority_key) ON public.cutover_global_writer_authority TO {}')
                           .format(sql.Identifier(cls.control_role)))
        pool_size, max_overflow = authority._control_pool_configuration({
            'CUTOVER_GLOBAL_WRITER_AUTHORITY_POOL_SIZE': 8,
            'CUTOVER_GLOBAL_WRITER_AUTHORITY_MAX_OVERFLOW': 8})
        cls.control = create_engine(DATABASE_URL.set(username=cls.control_role, password=password),
            hide_parameters=True, pool_size=pool_size, max_overflow=max_overflow, pool_timeout=2,
            connect_args={'options': '-c statement_timeout=2500ms -c lock_timeout=1000ms '
                '-c idle_in_transaction_session_timeout=3000ms'})
        # The fixture is SCRAM loopback; only the network/TLS engine factory is
        # replaced. Middleware, SQL row locks, transactions and CAS are real.
        cls.engine_patch = patch.object(authority, '_control_database_engine', lambda config: cls.control)
        cls.engine_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.engine_patch.stop()
        cls.control.dispose()
        with cls.root.begin() as connection:
            connection.execute(text('DROP TABLE public.cutover_global_writer_authority'))
            native = connection.connection.driver_connection
            native.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(cls.control_role)))
            native.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(cls.control_role)))
        cls.root.dispose()

    def setUp(self):
        with self.root.begin() as connection:
            connection.execute(text("UPDATE public.cutover_global_writer_authority SET owner_runtime='vercel',epoch=1,render_fenced=TRUE,vercel_fenced=FALSE WHERE authority_key='primary'"))
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, CUTOVER_WRITER_FENCE_ENABLED=False,
            CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED=True, CUTOVER_RUNTIME_IDENTITY='vercel')
        register_cutover_writer_fence(self.app)
        self.cas_started = Event()

    def transition(self):
        with self.root.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout='8000ms'"))
            connection.execute(text("SET LOCAL application_name='chatboc_http_lease_cas_test'"))
            self.cas_started.set()
            return authority.attest_runtime_fenced(connection, runtime='vercel', expected_epoch=1)

    def assert_transition_waits_on_actual_pg_lock(self, transition):
        self.assertTrue(self.cas_started.wait(3))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.root.connect() as connection:
                waiting = connection.execute(text("SELECT count(*) FROM pg_stat_activity WHERE application_name='chatboc_http_lease_cas_test' AND state='active' AND wait_event_type='Lock'")).scalar_one()
            if waiting:
                break
            if transition.done():
                transition.result()
                self.fail('Authority transitioned before the HTTP request lease closed')
            Event().wait(0.02)
        self.assertGreater(waiting, 0, 'CAS must be observed waiting on a PostgreSQL lock')
        self.assertFalse(transition.done())

    def assert_fenced_after_release(self, result):
        self.assertEqual(result.epoch, 2)
        self.assertTrue(result.vercel_fenced)
        response = self.app.test_client().post('/write')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()['reason_code'], 'runtime_globally_fenced')

    @contextmanager
    def real_sql_session_app(self):
        app = Flask('pg-session-' + uuid4().hex)
        app.config.update(self.app.config)
        app.config.update(SECRET_KEY='local-pg-session-proof',
            SQLALCHEMY_DATABASE_URI=DATABASE_URL,
            SESSION_TYPE='sqlalchemy', SESSION_COOKIE_NAME='pg_session_proof',
            SESSION_SQLALCHEMY_TABLE='http_sql_session_' + uuid4().hex,
            SESSION_COOKIE_SECURE=False)
        database = SQLAlchemy(app)
        init_migration_managed_session(app, database)
        with app.app_context():
            table = app.session_interface.sql_session_model.__table__
            table.create(database.engine)
        register_cutover_writer_fence(app)
        try:
            yield app, database
        finally:
            with app.app_context():
                database.session.remove()
                table.drop(database.engine)
                database.engine.dispose()

    def test_expired_sql_cookie_opens_before_http_and_socket_context_without_any_dml(self):
        with self.real_sql_session_app() as (app, database):
            app.config['CUTOVER_WRITER_FENCE_ENABLED'] = True
            statements, commits = [], []
            with app.app_context():
                interface = app.session_interface
                database.session.add(interface.sql_session_model(
                    session_id=interface._get_store_id('expired-local-cookie'),
                    data=interface.serializer.encode({'actor': 'expired-actor'}),
                    expiry=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)))
                database.session.commit()
                engine = database.engine
                def capture_sql(conn, cursor, statement, params, context, many):
                    statements.append(statement.split()[0].upper())
                def capture_commit(conn):
                    commits.append(True)
                event.listen(engine, 'before_cursor_execute', capture_sql)
                event.listen(engine, 'commit', capture_commit)
            @app.get('/writer')
            @cutover_writer_view
            def writer():
                self.fail('static-fenced writer cannot execute')
            client = app.test_client()
            client.set_cookie('pg_session_proof', 'expired-local-cookie')
            try:
                response = client.get('/writer')
                self.assertEqual(response.status_code, 503)
                self.assertNotIn('Set-Cookie', response.headers)
                # Socket.IO opens this same Flask request context before dispatch.
                with app.test_request_context('/socket.io',
                    headers={'Cookie': 'pg_session_proof=expired-local-cookie'}):
                    self.assertNotIn('actor', session)
                self.assertFalse({'INSERT', 'UPDATE', 'DELETE'}.intersection(statements))
                self.assertEqual(commits, [])
                with app.app_context():
                    self.assertEqual(interface.sql_session_model.query.count(), 1)
            finally:
                event.remove(engine, 'before_cursor_execute', capture_sql)
                event.remove(engine, 'commit', capture_commit)

    def test_read_only_get_sql_session_commit_owns_lease_and_blocks_cas(self):
        entered, release = Event(), Event()
        with self.real_sql_session_app() as (app, database):
            @app.get('/store-session')
            def store():
                session['actor'] = 'durable-local-actor'
                return {'ok': True}
            with app.app_context():
                engine = database.engine
                def pending_commit(conn):
                    entered.set()
                    self.assertTrue(release.wait(8))
                event.listen(engine, 'commit', pending_commit)
            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    request_result = pool.submit(lambda: app.test_client().get('/store-session'))
                    try:
                        self.assertTrue(entered.wait(3))
                        self.assertEqual(self.control.pool.checkedout(), 1)
                        transition = pool.submit(self.transition)
                        self.assert_transition_waits_on_actual_pg_lock(transition)
                    finally:
                        release.set()
                    response = request_result.result(timeout=5)
                    result = transition.result(timeout=5)
                self.assertEqual(response.status_code, 200)
                self.assertIn('Set-Cookie', response.headers)
                self.assertEqual(result.epoch, 2)
                self.assertTrue(result.vercel_fenced)
                with app.app_context():
                    record = app.session_interface.sql_session_model.query.one()
                    self.assertEqual(app.session_interface.serializer.decode(record.data)['actor'],
                                     'durable-local-actor')
                self.assertEqual(self.control.pool.checkedout(), 0)
            finally:
                event.remove(engine, 'commit', pending_commit)

    def test_eight_concurrent_provider_requests_hold_cas_until_all_finish(self):
        release, all_entered = Event(), Event()
        counter_lock = Lock()
        entered, finished = [], []

        @self.app.post('/write')
        def writer():
            with counter_lock:
                entered.append(request.headers['X-Test-Writer'])
                if len(entered) == 8:
                    all_entered.set()
            self.assertTrue(release.wait(8))
            with counter_lock:
                finished.append(request.headers['X-Test-Writer'])
            return {'ok': True}

        with ThreadPoolExecutor(max_workers=9) as pool:
            requests = [pool.submit(lambda index=index: self.app.test_client().post(
                '/write', headers={'X-Test-Writer': str(index)}, buffered=True).status_code)
                for index in range(8)]
            try:
                self.assertTrue(all_entered.wait(5), 'all eight provider requests must be admitted')
                self.assertEqual(self.control.pool.checkedout(), 8)
                transition = pool.submit(self.transition)
                self.assert_transition_waits_on_actual_pg_lock(transition)
                self.assertEqual(finished, [])
            finally:
                release.set()
            self.assertEqual([future.result(timeout=5) for future in requests], [200] * 8)
            result = transition.result(timeout=5)
        self.assertEqual(set(finished), set(map(str, range(8))))
        self.assertEqual(self.control.pool.checkedout(), 0)
        self.assert_fenced_after_release(result)

    def test_socket_background_provider_finishes_before_cas_and_queued_welcome_is_denied(self):
        import socket_service
        entered, release, provider_finished = Event(), Event(), Event()

        def pending_provider(app, sid, auth):
            entered.set()
            self.assertTrue(release.wait(8))
            provider_finished.set()
            return True

        with patch.object(socket_service, '_send_admitted_welcome_message',
                          side_effect=pending_provider) as provider:
            # The independent SQLite suite proves real tenant/owner binding.
            # Here the admitted provider body is isolated to prove row-lock scope.
            auth = {'channel': 'web', 'tenant_slug': 'local-socket-proof'}
            with ThreadPoolExecutor(max_workers=2) as pool:
                worker = pool.submit(socket_service.send_welcome_message,
                                     self.app, 'local-socket-proof', auth)
                try:
                    self.assertTrue(entered.wait(3))
                    self.assertEqual(self.control.pool.checkedout(), 1)
                    transition = pool.submit(self.transition)
                    self.assert_transition_waits_on_actual_pg_lock(transition)
                    self.assertFalse(provider_finished.is_set())
                finally:
                    release.set()
                self.assertTrue(worker.result(timeout=5))
                result = transition.result(timeout=5)
            self.assertTrue(provider_finished.is_set())
            provider.assert_called_once()
            self.assertFalse(socket_service.send_welcome_message(
                self.app, 'queued-local-socket-proof', auth))
            provider.assert_called_once()
        self.assertEqual(result.epoch, 2)
        self.assertTrue(result.vercel_fenced)
        self.assertEqual(self.control.pool.checkedout(), 0)

    def test_provider_pending_past_old_idle_timeout_blocks_cas_until_request_finishes(self):
        entered, release, provider_finished = Event(), Event(), Event()
        @self.app.post('/write')
        def writer():
            entered.set()
            self.assertTrue(release.wait(8))
            provider_finished.set()
            return {'ok': True}

        with ThreadPoolExecutor(max_workers=2) as pool:
            request = pool.submit(lambda: self.app.test_client().post('/write', buffered=True).status_code)
            transition = None
            try:
                self.assertTrue(entered.wait(3))
                transition = pool.submit(self.transition)
                self.assert_transition_waits_on_actual_pg_lock(transition)
                # Exceed the runtime engine's old 3000ms idle timer while the
                # provider phase has not finished; no nominal lock assertion.
                Event().wait(3.25)
                self.assert_transition_waits_on_actual_pg_lock(transition)
                self.assertFalse(provider_finished.is_set())
            finally:
                release.set()
            self.assertEqual(request.result(timeout=5), 200)
            self.assertTrue(provider_finished.is_set())
            self.assert_fenced_after_release(transition.result(timeout=5))

    def test_stream_close_callback_provider_effect_finishes_before_cas(self):
        callback_started, callback_release, callback_finished = Event(), Event(), Event()
        @self.app.post('/write')
        def writer():
            response = Response(iter((b'one', b'two')))
            def on_close():
                callback_started.set()
                self.assertTrue(callback_release.wait(8))
                callback_finished.set()
            response.call_on_close(on_close)
            return response

        response = self.app.test_client().post('/write', buffered=False)
        with ThreadPoolExecutor(max_workers=2) as pool:
            transition = pool.submit(self.transition)
            closing = None
            try:
                self.assert_transition_waits_on_actual_pg_lock(transition)
                closing = pool.submit(response.close)
                self.assertTrue(callback_started.wait(3))
                self.assert_transition_waits_on_actual_pg_lock(transition)
                self.assertFalse(callback_finished.is_set())
            finally:
                callback_release.set()
            closing.result(timeout=5)
            self.assertTrue(callback_finished.is_set())
            self.assert_fenced_after_release(transition.result(timeout=5))

    def test_stream_error_rolls_back_and_releases_before_fence_completes(self):
        cleaned, closed = Event(), Event()
        @self.app.post('/write')
        def writer():
            def stream():
                try:
                    yield b'one'
                    raise ValueError('fixture_stream_error')
                finally:
                    cleaned.set()
            response = Response(stream())
            response.call_on_close(closed.set)
            return response

        response = self.app.test_client().post('/write', buffered=False)
        with ThreadPoolExecutor(max_workers=1) as pool:
            transition = pool.submit(self.transition)
            self.assert_transition_waits_on_actual_pg_lock(transition)
            with self.assertRaisesRegex(ValueError, 'fixture_stream_error'):
                response.get_data()
            response.close()
            self.assertTrue(cleaned.is_set() and closed.is_set())
            self.assert_fenced_after_release(transition.result(timeout=5))

    def test_handler_exception_rolls_back_control_transaction_without_lock_leak(self):
        @self.app.post('/write')
        def writer():
            raise ValueError('fixture_handler_error')

        with self.assertRaisesRegex(ValueError, 'fixture_handler_error'):
            self.app.test_client().post('/write')
        with self.root.connect() as connection:
            active = connection.execute(text('SELECT count(*) FROM pg_stat_activity WHERE usename=:role AND state LIKE :idle'),
                {'role': self.control_role, 'idle': 'idle in transaction%'}).scalar_one()
        self.assertEqual(active, 0)
        self.assert_fenced_after_release(self.transition())

    def test_never_consumed_wsgi_iterable_blocks_until_explicit_close(self):
        closed = Event()
        @self.app.post('/write')
        def writer():
            response = Response(iter((b'never_consumed',)))
            response.call_on_close(closed.set)
            return response

        iterable = self.app.wsgi_app(EnvironBuilder(path='/write', method='POST').get_environ(),
            lambda status, headers, exc_info=None: None)
        with ThreadPoolExecutor(max_workers=1) as pool:
            transition = pool.submit(self.transition)
            try:
                self.assert_transition_waits_on_actual_pg_lock(transition)
                self.assertFalse(closed.is_set())
            finally:
                iterable.close()
            self.assertTrue(closed.is_set())
            self.assert_fenced_after_release(transition.result(timeout=5))

    def test_nested_route_lease_reuses_connection_while_cas_is_queued(self):
        entered, release_nested, nested_finished = Event(), Event(), Event()
        @self.app.post('/write')
        def writer():
            outer = request.environ[authority.HTTP_REQUEST_LEASE_ENVIRON].lease
            entered.set()
            self.assertTrue(release_nested.wait(8))
            with authority.global_writer_authority_lease(current_app.config) as nested:
                self.assertIs(nested, outer)
                self.assertTrue(nested.revalidate(current_app.config).allowed)
                self.assertEqual(self.control.pool.checkedout(), 1)
            nested_finished.set()
            return {'ok': True}

        with ThreadPoolExecutor(max_workers=2) as pool:
            http = pool.submit(lambda: self.app.test_client().post('/write', buffered=True).status_code)
            transition = None
            try:
                self.assertTrue(entered.wait(3))
                transition = pool.submit(self.transition)
                self.assert_transition_waits_on_actual_pg_lock(transition)
            finally:
                release_nested.set()
            self.assertEqual(http.result(timeout=5), 200)
            self.assertTrue(nested_finished.is_set())
            self.assert_fenced_after_release(transition.result(timeout=5))
