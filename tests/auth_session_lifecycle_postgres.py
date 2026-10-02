"""Actual lifecycle service row locks in a newly created loopback PG database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
import unittest
from uuid import uuid4

from flask import Flask
from sqlalchemy import select
from sqlalchemy.schema import CreateSchema, DropSchema

from models import db, User, TenantProfile, AuthSession, AuthProviderSession, AuthSessionAudit, AuthSessionRetirement
from services.auth_session_lifecycle import (
    issue_token, descriptor_for_token, retire_session, refresh_native_token,
    lineage_for_claims, revoke_provider_session, provider_session_revoked,
)

DATABASE_URL = None  # Set only by the dedicated fresh-loopback cluster runner.


class AuthSessionLifecyclePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if DATABASE_URL is None or DATABASE_URL.host != '127.0.0.1':
            raise RuntimeError('A new dedicated loopback PostgreSQL cluster is required')
        from sqlalchemy import create_engine
        cls.root = create_engine(DATABASE_URL)
        cls.schema = 'auth_lifecycle_' + uuid4().hex
        with cls.root.begin() as connection:
            connection.execute(CreateSchema(cls.schema))
        cls.app = Flask(__name__)
        cls.app.config.update(SQLALCHEMY_DATABASE_URI=DATABASE_URL,
            SQLALCHEMY_ENGINE_OPTIONS={'execution_options': {'schema_translate_map': {None: cls.schema}}},
            SQLALCHEMY_TRACK_MODIFICATIONS=False, SECRET_KEY=uuid4().hex, TESTING=True)
        db.init_app(cls.app)
        with cls.app.app_context():
            cls.engine = db.engine
            pending = [m.__table__ for m in (User, TenantProfile, AuthProviderSession, AuthSession,
                                          AuthSessionAudit, AuthSessionRetirement)]
            pending.extend(db.metadata.tables[name] for name in ('empleado_categoria', 'categorias_ticket'))
            tables = set()
            while pending:
                table = pending.pop()
                if table in tables:
                    continue
                tables.add(table)
                pending.extend(key.column.table for key in table.foreign_keys)
            db.metadata.create_all(cls.engine, tables=list(tables))

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove(); cls.engine.dispose()
        with cls.root.begin() as connection:
            connection.execute(DropSchema(cls.schema, cascade=True))
        cls.root.dispose()

    def setUp(self):
        with self.app.app_context():
            actor = User(name='Disposable actor', email=uuid4().hex + '@example.invalid', rol='usuario')
            actor.set_password(uuid4().hex)
            db.session.add(actor); db.session.commit()
            self.actor_id = actor.id

    def token(self, sid=None):
        claims = {'user_id': self.actor_id, 'exp': datetime.now(timezone.utc) + timedelta(hours=1)}
        if sid:
            claims.update(auth_provider='clerk', session_kind='clerk', sid=sid, clerk_sid=sid, sv=1)
        return issue_token(claims)

    def test_actual_native_login_with_default_password_hash_and_postgres_retirement(self):
        import os
        import secrets
        os.environ['CORS_ALLOWED_ORIGINS'] = 'https://panel.example.invalid'
        from app import create_app
        from config import TestingConfig
        from sqlalchemy import text

        database_url, schema = DATABASE_URL, self.schema
        class NativeLoginPostgresConfig(TestingConfig):
            SQLALCHEMY_DATABASE_URI = database_url
            SQLALCHEMY_ENGINE_OPTIONS = {'execution_options': {'schema_translate_map': {None: schema}}}
            SECRET_KEY = secrets.token_hex(32)
            SESSION_TYPE = 'null'
            SESSION_COOKIE_SAMESITE = 'Lax'
            CUTOVER_WRITER_FENCE_ENABLED = False
            RATELIMIT_STORAGE_URI = 'memory://'
            OUTBOUND_NOTIFICATIONS_ENABLED = False

        app = create_app(NativeLoginPostgresConfig)
        try:
            password = secrets.token_urlsafe(24)
            with app.app_context():
                actor = db.session.get(User, self.actor_id)
                actor.rol = 'admin'; actor.tipo_chat = 'municipio'
                actor.set_password(password)
                self.assertGreater(len(actor.password_hash), 128)
                tenant = TenantProfile(slug='pg-native-' + uuid4().hex,
                    nombre='Disposable PostgreSQL login', tipo='municipio',
                    municipio_id=actor.id, vertical='government', configuracion={})
                db.session.add(tenant); db.session.flush()
                actor.tenant_id = tenant.id; actor.tenant_slug = tenant.slug; actor.municipio_id = actor.id
                email = actor.email; db.session.commit()
                column_type = db.session.execute(text("SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema=:schema AND table_name='user' AND column_name='password_hash'"),
                    {'schema': schema}).scalar_one()
                self.assertEqual(column_type, 'text')

            first_client, second_client = app.test_client(), app.test_client()
            wrong_password = first_client.post('/auth/login', json={'email':email, 'password':'incorrect'})
            self.assertEqual(wrong_password.status_code, 401)
            first = first_client.post('/auth/login', json={'email':email, 'password':password})
            second = second_client.post('/auth/login', json={'email':email, 'password':password})
            self.assertEqual(first.status_code, 200, first.get_json())
            self.assertEqual(second.status_code, 200, second.get_json())
            first, second = first.get_json(), second.get_json()
            first_proof, second_proof = first['session_retirement'], second['session_retirement']
            self.assertNotEqual(first_proof['lineage_id'], second_proof['lineage_id'])
            profile = second_client.get('/api/me', headers={'Authorization':'Bearer ' + second['token']})
            self.assertEqual(profile.status_code, 200, profile.get_json())
            self.assertEqual(profile.get_json()['session_retirement'], second_proof)
            retirement = second_client.post('/api/v2/auth/sessions/retire',
                json={'proof':first_proof['proof'], 'request_id':uuid4().hex})
            self.assertEqual(retirement.status_code, 200, retirement.get_json())
            self.assertNotIn('Set-Cookie', retirement.headers)
            denied = app.test_client().get('/api/me', headers={'Authorization':'Bearer ' + first['token']})
            self.assertIn(denied.status_code, (401, 403))
            self.assertEqual(second_client.get('/api/me').status_code, 200)
            with app.app_context():
                db.session.expire_all()
                self.assertIsNotNone(db.session.get(AuthSession, first_proof['lineage_id']).revoked_at)
                self.assertIsNone(db.session.get(AuthSession, second_proof['lineage_id']).revoked_at)
        finally:
            with app.app_context():
                db.session.remove(); db.engine.dispose()

    def test_logout_lock_blocks_refresh_child_and_is_visible_to_another_worker(self):
        import jwt
        with self.app.app_context():
            token = self.token(); proof = descriptor_for_token(token)
        locked, release, child_started = Event(), Event(), Event()
        def retire_worker():
            with self.app.app_context():
                db.session.execute(select(AuthSession).where(AuthSession.id == proof['lineage_id']).with_for_update())
                locked.set(); self.assertTrue(release.wait(10))
                return retire_session(proof['proof'], uuid4().hex)
        def refresh_worker():
            with self.app.app_context():
                child_started.set()
                try:
                    return refresh_native_token(token, expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
                except ValueError:
                    db.session.rollback(); return 'denied'
        with ThreadPoolExecutor(max_workers=2) as pool:
            retiring = pool.submit(retire_worker)
            self.assertTrue(locked.wait(10))
            refresh = pool.submit(refresh_worker)
            self.assertTrue(child_started.wait(10))
            self.assertFalse(refresh.done())
            release.set()
            self.assertTrue(retiring.result(timeout=10)['local_revoked'])
            self.assertEqual(refresh.result(timeout=10), 'denied')
        with self.app.app_context():
            with self.assertRaises(ValueError):
                lineage_for_claims(jwt.decode(token, self.app.config['SECRET_KEY'], algorithms=['HS256']))

    def test_refresh_committed_before_logout_child_is_revoked_too(self):
        import jwt
        with self.app.app_context():
            token = self.token(); proof = descriptor_for_token(token)
            child, _ = refresh_native_token(token, expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
            retire_session(proof['proof'], uuid4().hex)
        with self.app.app_context():
            with self.assertRaises(ValueError):
                lineage_for_claims(jwt.decode(child, self.app.config['SECRET_KEY'], algorithms=['HS256']))

    def test_provider_event_lock_prevents_exchange_resurrection_after_unlinked_tombstone(self):
        sid = 'synthetic_sid_' + uuid4().hex
        locked, release, exchange_started = Event(), Event(), Event()
        def webhook_worker():
            with self.app.app_context():
                revoke_provider_session(sid, reason='session.revoked', confirmed=True)
                locked.set(); self.assertTrue(release.wait(10)); db.session.commit()
        def exchange_worker():
            with self.app.app_context():
                exchange_started.set()
                try:
                    return self.token(sid)
                except ValueError:
                    db.session.rollback(); return 'denied'
        with ThreadPoolExecutor(max_workers=2) as pool:
            webhook = pool.submit(webhook_worker); self.assertTrue(locked.wait(10))
            exchange = pool.submit(exchange_worker); self.assertTrue(exchange_started.wait(10))
            self.assertFalse(exchange.done()); release.set()
            webhook.result(timeout=10)
            self.assertEqual(exchange.result(timeout=10), 'denied')
        with self.app.app_context():
            self.assertTrue(provider_session_revoked(sid))
            self.assertEqual(AuthSession.query.filter_by(provider_session_id=sid).count(), 0)

    def test_simultaneous_replay_has_one_audit_and_one_receipt(self):
        with self.app.app_context():
            token = self.token(); proof = descriptor_for_token(token)
        request_id = uuid4().hex
        def worker():
            with self.app.app_context():
                return retire_session(proof['proof'], request_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [future.result(timeout=10) for future in (pool.submit(worker), pool.submit(worker))]
        self.assertEqual({result['status'] for result in results}, {'retired', 'already_retired'})
        with self.app.app_context():
            self.assertEqual(AuthSessionAudit.query.filter_by(request_id=request_id).count(), 1)
            self.assertEqual(AuthSessionRetirement.query.filter_by(request_id=request_id).count(), 1)

    def test_real_migration_and_exact_schema_postcheck_on_empty_public_schema(self):
        import importlib.util
        from pathlib import Path
        from alembic.operations import Operations
        from alembic.runtime.migration import MigrationContext
        from sqlalchemy import text
        from scripts.apply_neon_cutover_migrations import (
            AUTH_SESSION_REVISION, POST_SYNC_SCHEMA_REQUIREMENTS,
            _platform_table_contract, _load_exact_migration_plan)
        root = Path(__file__).resolve().parents[1]
        plan = _load_exact_migration_plan(root)
        self.assertEqual(plan.script.get_heads(), [AUTH_SESSION_REVISION])
        spec = importlib.util.spec_from_file_location('disposable_auth_migration',
            root / 'migrations/versions/20261001_add_auth_session_lifecycle_v1.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with self.root.connect() as connection:
            transaction = connection.begin()
            try:
                self.assertIsNone(connection.execute(text("SELECT to_regclass('public.auth_session')")).scalar_one())
                connection.execute(text('CREATE TABLE public."user" (id integer PRIMARY KEY)'))
                with Operations.context(MigrationContext.configure(connection)):
                    module.upgrade()
                for specification in POST_SYNC_SCHEMA_REQUIREMENTS[AUTH_SESSION_REVISION]:
                    with self.subTest(table=specification['table']):
                        self.assertTrue(all(_platform_table_contract(connection, specification).values()))
                from services.runtime_readiness import _auth_session_schema_probe, _auth_session_contract_failure_reason
                probe = _auth_session_schema_probe(timeout_seconds=1.5)
                self.assertIsNone(_auth_session_contract_failure_reason(connection.execute(probe).mappings().one()))
                role = 'disposable_auth_app_' + uuid4().hex
                connection.execute(text('CREATE ROLE ' + role))
                connection.execute(text('SET LOCAL ROLE ' + role))
                self.assertEqual(_auth_session_contract_failure_reason(connection.execute(probe).mappings().one()),
                                 'required_auth_session_privilege_missing')
                connection.execute(text('RESET ROLE'))
                connection.execute(text('GRANT USAGE ON SCHEMA public TO ' + role))
                connection.execute(text('GRANT SELECT, INSERT, UPDATE ON auth_session, auth_provider_session, auth_session_retirement TO ' + role))
                connection.execute(text('GRANT SELECT, INSERT ON auth_session_audit TO ' + role))
                connection.execute(text('SET LOCAL ROLE ' + role))
                self.assertIsNone(_auth_session_contract_failure_reason(connection.execute(probe).mappings().one()))
                connection.execute(text('RESET ROLE'))
                with self.assertRaises(RuntimeError):
                    module.downgrade()
            finally:
                transaction.rollback()

    def test_different_request_ids_serialize_one_retirement_transition(self):
        with self.app.app_context():
            token = self.token(); proof = descriptor_for_token(token)
        def worker():
            with self.app.app_context():
                return retire_session(proof['proof'], uuid4().hex)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [future.result(timeout=10) for future in (pool.submit(worker), pool.submit(worker))]
        self.assertEqual({result['status'] for result in results}, {'retired', 'already_retired'})
        with self.app.app_context():
            self.assertEqual(AuthSessionAudit.query.filter_by(lineage_id=proof['lineage_id'], event_type='retired').count(), 1)

    def test_request_id_collision_across_families_rolls_back_losing_retirement(self):
        from services.auth_session_lifecycle import SessionLifecycleError
        with self.app.app_context():
            proofs = [descriptor_for_token(self.token()), descriptor_for_token(self.token())]
        request_id = uuid4().hex
        def worker(proof):
            with self.app.app_context():
                try:
                    retire_session(proof['proof'], request_id); return 200
                except SessionLifecycleError as error:
                    db.session.rollback(); return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [future.result(timeout=10) for future in (pool.submit(worker, proofs[0]), pool.submit(worker, proofs[1]))]
        self.assertEqual(sorted(results), [200, 409])
        with self.app.app_context():
            rows = [db.session.get(AuthSession, proof['lineage_id']) for proof in proofs]
            self.assertEqual(sum(row.revoked_at is not None for row in rows), 1)
