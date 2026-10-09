from __future__ import annotations

from unittest.mock import patch
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from flask import Flask, jsonify, session
from flask_sqlalchemy import SQLAlchemy
import pytest
from sqlalchemy import event
from sqlalchemy.sql.schema import Table
from cutover_writer_fence import cutover_writer_view
from middleware.cutover_writer_fence import register_cutover_writer_fence
from services import global_writer_authority as authority

from utils.migration_managed_session import (
    MigrationManagedSqlAlchemySessionInterface,
    init_migration_managed_session,
)


def _session_app() -> tuple[Flask, SQLAlchemy]:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="test-only-secret",
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SESSION_TYPE="sqlalchemy",
        SESSION_SQLALCHEMY_TABLE="flask_sessions",
        SESSION_COOKIE_NAME="chatboc_session",
        SESSION_COOKIE_SECURE=False,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    database = SQLAlchemy()
    database.init_app(app)
    return app, database


def test_sqlalchemy_session_initialization_performs_no_schema_ddl() -> None:
    app, database = _session_app()

    with patch.object(Table, "create", side_effect=AssertionError("runtime DDL")):
        init_migration_managed_session(app, database)

    assert isinstance(
        app.session_interface,
        MigrationManagedSqlAlchemySessionInterface,
    )


def test_migrated_session_table_preserves_server_side_round_trip() -> None:
    app, database = _session_app()
    init_migration_managed_session(app, database)

    with app.app_context():
        app.session_interface.sql_session_model.__table__.create(database.engine)

    @app.get("/set-session")
    def set_session():
        session["actor"] = "municipio-operador"
        return jsonify(ok=True)

    @app.get("/read-session")
    def read_session():
        return jsonify(actor=session.get("actor"))

    client = app.test_client()
    response = client.get("/set-session")
    assert response.status_code == 200
    assert "chatboc_session=" in response.headers["Set-Cookie"]

    response = client.get("/read-session")
    assert response.status_code == 200
    assert response.get_json() == {"actor": "municipio-operador"}


def _real_sql_session_fixture(*, static_fence=False, global_guard=True):
    app, database = _session_app()
    app.config.update(TESTING=True, CUTOVER_WRITER_FENCE_ENABLED=static_fence,
        CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED=global_guard,
        CUTOVER_RUNTIME_IDENTITY='vercel')
    init_migration_managed_session(app, database)
    with app.app_context():
        app.session_interface.sql_session_model.__table__.create(database.engine)
        statements = []
        commits = []
        event.listen(database.engine, 'before_cursor_execute',
                     lambda conn, cursor, statement, params, context, many:
                         statements.append(statement.split()[0].upper()))
        event.listen(database.engine, 'commit', lambda conn: commits.append(True))
    return app, database, statements, commits


def _seed_sql_cookie(app, database, sid, *, expired=False):
    with app.app_context():
        interface = app.session_interface
        model = interface.sql_session_model
        expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=-1 if expired else 1)
        database.session.add(model(session_id=interface._get_store_id(sid),
            data=interface.serializer.encode({'actor': 'stored-actor', '_permanent': True}), expiry=expiry))
        database.session.commit()


@pytest.mark.parametrize('static_fence', [True, False])
@pytest.mark.parametrize('null_expiry', [True, False])
def test_expired_cookie_context_open_is_read_only_before_http_writer_gate(monkeypatch, static_fence, null_expiry):
    app, database, statements, commits = _real_sql_session_fixture(static_fence=static_fence)
    _seed_sql_cookie(app, database, 'expired-cookie', expired=True)
    if null_expiry:
        with app.app_context():
            app.session_interface.sql_session_model.query.one().expiry = None
            database.session.commit()
    register_cutover_writer_fence(app)
    monkeypatch.setattr('middleware.cutover_writer_fence.evaluate_global_writer_authority',
        lambda config: authority.GlobalWriterAuthorityDecision(False, True, 'runtime_globally_fenced'))
    @contextmanager
    def denied(config, **kwargs):
        yield SimpleNamespace(decision=authority.GlobalWriterAuthorityDecision(False, True, 'runtime_globally_fenced'))
    monkeypatch.setattr('utils.migration_managed_session.global_writer_authority_lease', denied)

    @app.get('/writer')
    @cutover_writer_view
    def writer():
        pytest.fail('fenced handler cannot run')

    client = app.test_client()
    client.set_cookie('chatboc_session', 'expired-cookie')
    statements.clear(); commits.clear()
    response = client.get('/writer')
    assert response.status_code == 503
    assert 'Set-Cookie' not in response.headers
    assert not {'DELETE', 'INSERT', 'UPDATE'}.intersection(statements)
    assert not commits
    with app.app_context():
        assert app.session_interface.sql_session_model.query.count() == 1


def test_read_only_get_refresh_and_cookie_are_skipped_when_authority_denies(monkeypatch):
    app, database, statements, commits = _real_sql_session_fixture()
    _seed_sql_cookie(app, database, 'existing-cookie')
    register_cutover_writer_fence(app)
    acquired = []
    @contextmanager
    def denied(config, **kwargs):
        acquired.append(kwargs)
        yield SimpleNamespace(decision=authority.GlobalWriterAuthorityDecision(False, True, 'runtime_globally_fenced'))
    monkeypatch.setattr('utils.migration_managed_session.global_writer_authority_lease', denied)

    @app.get('/read')
    def read():
        return {'actor': session.get('actor')}

    client = app.test_client(); client.set_cookie('chatboc_session', 'existing-cookie')
    statements.clear(); commits.clear()
    response = client.get('/read')
    assert response.status_code == 200
    assert response.get_json() == {'actor': 'stored-actor'}
    assert acquired == [{'request_lifetime': True}]
    assert 'Set-Cookie' not in response.headers
    assert not {'DELETE', 'INSERT', 'UPDATE'}.intersection(statements)
    assert not commits


def test_get_session_commit_holds_own_lease_and_real_server_side_round_trip(monkeypatch):
    app, database, statements, commits = _real_sql_session_fixture()
    active = []
    acquired = []
    @contextmanager
    def allowed(config, **kwargs):
        acquired.append(kwargs)
        active.append(True)
        try:
            yield SimpleNamespace(decision=authority.GlobalWriterAuthorityDecision(True, True, 'owner'))
        finally:
            active.pop()
    monkeypatch.setattr('utils.migration_managed_session.global_writer_authority_lease', allowed)
    with app.app_context():
        def assert_write_covered(conn, cursor, statement, params, context, many):
            if statement.split()[0].upper() in {'INSERT', 'UPDATE', 'DELETE'}:
                assert active, 'actual session DML must run under its own lease'
        event.listen(database.engine, 'before_cursor_execute', assert_write_covered)
        event.listen(database.engine, 'commit', lambda conn: active or pytest.fail('session commit outside lease'))

    @app.get('/write-session')
    def write():
        session['actor'] = 'new-actor'
        return {'ok': True}
    @app.get('/read-session')
    def read():
        return {'actor': session.get('actor')}
    client = app.test_client()
    assert client.get('/write-session').status_code == 200
    assert client.get('/read-session').get_json() == {'actor': 'new-actor'}
    assert acquired == [{'request_lifetime': True}, {'request_lifetime': True}]
    assert 'INSERT' in statements and 'UPDATE' in statements
    assert len(commits) == 2
    assert not active


def test_http_session_save_reuses_live_outer_lease_without_another_control_connection(monkeypatch):
    app, database, statements, commits = _real_sql_session_fixture()
    register_cutover_writer_fence(app)
    active = []
    @contextmanager
    def allowed(config, **kwargs):
        active.append(True)
        try:
            yield SimpleNamespace(decision=authority.GlobalWriterAuthorityDecision(True, True, 'owner'))
        finally:
            active.pop()
    monkeypatch.setattr('middleware.cutover_writer_fence.evaluate_global_writer_authority',
        lambda config: authority.GlobalWriterAuthorityDecision(True, True, 'owner'))
    monkeypatch.setattr('middleware.cutover_writer_fence.global_writer_authority_lease', allowed)
    monkeypatch.setattr(authority, '_control_database_engine', lambda config:
        pytest.fail('SQL session persistence must reuse the live outer HTTP lease'))
    with app.app_context():
        event.listen(database.engine, 'commit', lambda conn: active or pytest.fail('unleased commit'))

    @app.post('/write')
    def write():
        session['actor'] = 'leased-actor'
        return {'ok': True}
    response = app.test_client().post('/write', buffered=False)
    assert response.status_code == 200
    assert 'INSERT' in statements
    assert len(commits) == 1
    assert active
    response.close()
    assert not active


def test_static_fence_blocks_cleanup_and_regeneration_without_changing_sid():
    app, database, statements, commits = _real_sql_session_fixture(static_fence=True)
    _seed_sql_cookie(app, database, 'expired-cleanup', expired=True)
    statements.clear(); commits.clear()
    with app.app_context():
        interface = app.session_interface
        interface._delete_expired_sessions()
        stored = interface.session_class({'actor': 'actor'}, sid='original-sid')
        interface.regenerate(stored)
        assert stored.sid == 'original-sid'
        assert interface.sql_session_model.query.count() == 1
    assert not {'DELETE', 'INSERT', 'UPDATE'}.intersection(statements)
    assert not commits
