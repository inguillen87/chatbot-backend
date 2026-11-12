"""Real voice admission locks on the existing disposable CI PostgreSQL service.

No production DSN, application .env, provider request, or microphone is used.
The fixed loopback service must be empty; only a newly created random schema is
used and removed. Run separately after profile_acceptance_runtime.prepare_process.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import re
import secrets
import socket
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask
import pytest
import requests
from sqlalchemy import text
from sqlalchemy.orm import Session

from models import AuditEvent, TenantProfile, User, db
from services import browser_realtime as voice

POSTGRES_OPT_IN = os.environ.get('CHATBOC_VOICE_TEST_POSTGRES') == '1'
TEST_URL = 'postgresql+psycopg://postgres@127.0.0.1:5432/vaultcredregression'
NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
DEADLINE = int((NOW+timedelta(days=3)).timestamp())
pg_only = pytest.mark.skipif(not POSTGRES_OPT_IN,
    reason='requires the explicitly enabled disposable loopback CI PostgreSQL service')


def require_disposable_target(url):
    if os.environ.get('CHATBOC_VOICE_TEST_POSTGRES') != '1':
        raise RuntimeError('voice_postgres_explicit_opt_in_required')
    if (url.drivername != 'postgresql+psycopg' or url.host != '127.0.0.1'
            or url.port != 5432 or url.database != 'vaultcredregression'
            or url.username != 'postgres' or url.password is not None
            or url.query):
        raise RuntimeError('voice_postgres_disposable_target_required')


@pytest.fixture(autouse=True)
def deny_provider_io(monkeypatch):
    original_dns, original_connect = socket.getaddrinfo, socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    def denied(*args, **kwargs):
        raise AssertionError('voice_admission_contract_forbids_provider_network')
    def dns(host, port, *args, **kwargs):
        if POSTGRES_OPT_IN and host == '127.0.0.1' and port == 5432:
            return original_dns(host, port, *args, **kwargs)
        return denied()
    def connect(sock, address):
        if POSTGRES_OPT_IN and address == ('127.0.0.1', 5432):
            return original_connect(sock, address)
        return denied()
    def connect_ex(sock, address):
        if POSTGRES_OPT_IN and address == ('127.0.0.1', 5432):
            return original_connect_ex(sock, address)
        return denied()
    monkeypatch.setattr(socket, 'getaddrinfo', dns)
    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', connect_ex)
    monkeypatch.setattr(requests.sessions.Session, 'request', denied)
    monkeypatch.setattr(voice.http.client, 'HTTPSConnection', denied)


@pytest.fixture
def pg_store():
    app = Flask('disposable-voice-admission-postgres')
    schema = 'voice_trial_contract_' + secrets.token_hex(8)
    assert re.fullmatch(r'voice_trial_contract_[0-9a-f]{16}', schema)
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI=TEST_URL,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={'connect_args': {
            'options': f'-csearch_path={schema} -cstatement_timeout=10000 -clock_timeout=10000'}})
    db.init_app(app)
    with app.app_context():
        engine = db.engine
        require_disposable_target(engine.url)
        created = False
        try:
            with engine.begin() as setup:
                assert setup.execute(text('SELECT current_database()')).scalar_one() == 'vaultcredregression'
                assert setup.execute(text('SELECT current_user')).scalar_one() == 'postgres'
                # Refuse to enter a fixture containing other tables.
                assert setup.execute(text("SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema NOT IN ('pg_catalog','information_schema') "
                    "AND table_schema NOT LIKE 'pg_%'")).scalar_one() == 0
                setup.execute(text(f'CREATE SCHEMA "{schema}"'))
                created = True
                assert setup.execute(text('SELECT current_schema()')).scalar_one() == schema
            db.create_all()
            actors = [User(name=f'Synthetic voice actor {n}', email=f'voice-{n}@example.invalid',
                rol='admin', password_hash='synthetic-not-a-login-hash') for n in (1, 2)]
            db.session.add_all(actors); db.session.flush()
            tenants = [TenantProfile(slug=f'voice-{n}', nombre=f'Synthetic voice tenant {n}',
                tipo='municipio', municipio_id=actor.id, is_active=True, configuracion={})
                for n, actor in enumerate(actors, 1)]
            db.session.add_all(tenants); db.session.commit()
            yield SimpleNamespace(engine=engine, actor_ids=[a.id for a in actors],
                tenant_ids=[t.id for t in tenants], schema=schema)
        finally:
            db.session.rollback(); db.session.remove()
            try:
                if created:
                    with engine.begin() as cleanup:
                        cleanup.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            finally:
                engine.dispose()


def quota(*, total=2, deadline=DEADLINE):
    return voice.limits({'browser_realtime_voice': {'enabled': True,
        'max_sessions_per_hour': 3, 'max_total_sessions': total, 'trial_expires_at': deadline}})


def ledger(session, now=NOW):
    return voice.VoiceLedger(session, TenantProfile, AuditEvent, clock=lambda: now)


def wait_for_lock(engine, backend_pid):
    """Require a real PostgreSQL lock wait, not an elapsed-time assumption."""
    until = monotonic()+5
    while monotonic() < until:
        with engine.connect() as observer:
            state = observer.execute(text('SELECT wait_event_type FROM pg_stat_activity '
                'WHERE pid=:pid AND datname=current_database()'), {'pid': backend_pid}).scalar_one_or_none()
        if state == 'Lock':
            return
        sleep(0.02)
    pytest.fail('contender_did_not_enter_real_postgres_lock_wait')


@pg_only
def test_concurrent_same_tenant_reservation_serializes_and_total_never_restarts(pg_store):
    tenant, foreign = pg_store.tenant_ids
    actor, other_actor = pg_store.actor_ids
    # One resolved reservation predates this hour and publication.
    with Session(pg_store.engine) as session:
        session.add(AuditEvent(tenant_id=tenant, actor_user_id=actor,
            resource_type='unrelated-contract', resource_id='unrelated-reservation',
            event_type=voice.EVENT+'intent', details={}, created_at=NOW))
        session.commit()
        prior = ledger(session, NOW-timedelta(days=1)); prior.lock(tenant)
        old = prior.reserve(tenant, actor, 'old-revision', quota())
        prior.append(tenant, actor, old, 'failed', {})
    locked, release, contender_started = Event(), Event(), Event()
    backend = []
    def first_worker():
        with Session(pg_store.engine) as session:
            first = ledger(session); first.lock(tenant)
            locked.set(); assert release.wait(8)
            identifier = first.reserve(tenant, actor, 'current-revision', quota())
            first.append(tenant, actor, identifier, 'failed', {})
            return identifier
    def second_worker():
        with Session(pg_store.engine) as session:
            backend.append(session.execute(text('SELECT pg_backend_pid()')).scalar_one())
            contender_started.set()
            second = ledger(session, NOW+timedelta(hours=2)); second.lock(tenant)
            try:
                second.reserve(tenant, other_actor, 'another-revision', quota())
            except voice.VoiceError as error:
                session.rollback()
                return error.code
            pytest.fail('concurrent_trial_admitted_beyond_total_cap')
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_worker)
        assert locked.wait(5)
        second = pool.submit(second_worker)
        try:
            assert contender_started.wait(5)
            wait_for_lock(pg_store.engine, backend[0])
            assert not second.done()
        finally:
            release.set()
        current = first.result(timeout=10)
        denied = second.result(timeout=10)
    # The contender may observe the winner's pending intent or its terminal
    # receipt; both are fail-closed. A fresh worker then proves the total bound.
    assert denied in {'browser_voice_previous_session_pending', 'browser_voice_total_cap'}
    assert current != old
    with Session(pg_store.engine) as session:
        fresh = ledger(session, NOW+timedelta(days=1)); fresh.lock(tenant)
        with pytest.raises(voice.VoiceError, match='total_cap'):
            fresh.reserve(tenant, other_actor, 'third-revision', quota(deadline=DEADLINE+86400))
        assert fresh.admission_snapshot(tenant, quota()) == {
            'total_sessions_reserved': 2, 'total_sessions_remaining': 0}
        session.rollback()
        separate = ledger(session); separate.lock(foreign)
        assert separate.reserve(foreign, other_actor, 'foreign-revision', quota(total=1))
        assert separate.admission_snapshot(foreign, quota(total=1)) == {
            'total_sessions_reserved': 1, 'total_sessions_remaining': 0}
        with pytest.raises(voice.VoiceError, match='previous_session_pending'):
            separate.reserve(foreign, other_actor, 'unknown-next-revision', quota(total=1))


@pg_only
def test_deadline_crossed_while_waiting_for_tenant_lock_reserves_nothing(pg_store):
    tenant, actor = pg_store.tenant_ids[0], pg_store.actor_ids[0]
    locked, release, contender_started = Event(), Event(), Event()
    backend, current = [], [NOW]
    def holder():
        with Session(pg_store.engine) as session:
            ledger(session).lock(tenant); locked.set()
            assert release.wait(8); session.rollback()
    def contender():
        with Session(pg_store.engine) as session:
            backend.append(session.execute(text('SELECT pg_backend_pid()')).scalar_one())
            contender_started.set()
            trial = voice.VoiceLedger(session, TenantProfile, AuditEvent, clock=lambda: current[0])
            trial.lock(tenant)
            with pytest.raises(voice.VoiceError, match='trial_expired'):
                trial.reserve(tenant, actor, 'published-revision', quota(deadline=int(NOW.timestamp())+1))
            session.rollback()
    with ThreadPoolExecutor(max_workers=2) as pool:
        holding = pool.submit(holder); assert locked.wait(5)
        waiting = pool.submit(contender)
        try:
            assert contender_started.wait(5)
            wait_for_lock(pg_store.engine, backend[0])
            current[0] = NOW+timedelta(seconds=1)
        finally:
            release.set()
        holding.result(timeout=10); waiting.result(timeout=10)
    with Session(pg_store.engine) as session:
        assert session.query(AuditEvent).filter_by(resource_type=voice.CONTRACT,
            event_type=voice.EVENT+'intent').count() == 0


@pg_only
def test_stop_after_expiry_and_exhaustion_is_durable_idempotent_and_not_new_admission(pg_store):
    tenant, actor = pg_store.tenant_ids[0], pg_store.actor_ids[0]
    provider = Mock(return_value={'stopped': True})
    with Session(pg_store.engine) as session:
        trial = ledger(session); trial.lock(tenant)
        identifier = trial.reserve(tenant, actor, 'published-revision', quota(total=1))
        trial.append(tenant, actor, identifier, 'accepted', {'call_id': 'rtc_synthetic'})
    with Session(pg_store.engine) as session:
        stopped = ledger(session, NOW+timedelta(days=4))
        assert stopped.stop(tenant, actor, identifier, 'synthetic', provider=provider)['stopped']
        assert stopped.stop(tenant, actor, identifier, 'synthetic', provider=provider)['stopped']
        assert provider.call_count == 1
        assert stopped.admission_snapshot(tenant, quota(total=1))['total_sessions_reserved'] == 1
        stopped.lock(tenant)
        with pytest.raises(voice.VoiceError, match='trial_expired'):
            stopped.reserve(tenant, actor, 'new-revision', quota(total=1))
        session.rollback(); stopped.lock(tenant)
        with pytest.raises(voice.VoiceError, match='total_cap'):
            stopped.reserve(tenant, actor, 'new-revision', quota(total=1, deadline=DEADLINE+86400*3))
        assert [row.event_type for row in session.query(AuditEvent).filter_by(
            resource_type=voice.CONTRACT).order_by(AuditEvent.id)] == [voice.EVENT+kind for kind in
                ('intent','accepted','stop_intent','stopped')]


def test_postgres_target_guard_requires_opt_in_before_any_connection(monkeypatch):
    from sqlalchemy.engine import make_url
    monkeypatch.delenv('CHATBOC_VOICE_TEST_POSTGRES', raising=False)
    with pytest.raises(RuntimeError, match='explicit_opt_in_required'):
        require_disposable_target(make_url(TEST_URL))


@pytest.mark.parametrize('url', [
    'postgresql+psycopg://postgres@database.example.invalid:5432/vaultcredregression',
    'postgresql+psycopg://postgres@127.0.0.1:5432/customer',
    'postgresql+psycopg://other@127.0.0.1:5432/vaultcredregression',
    'postgresql+psycopg://postgres:secret@127.0.0.1:5432/vaultcredregression',
    TEST_URL+'?options=custom', 'sqlite:///:memory:'])
def test_postgres_target_guard_refuses_foreign_or_ambient_targets(monkeypatch, url):
    from sqlalchemy.engine import make_url
    monkeypatch.setenv('CHATBOC_VOICE_TEST_POSTGRES', '1')
    with pytest.raises(RuntimeError, match='disposable_target_required'):
        require_disposable_target(make_url(url))
