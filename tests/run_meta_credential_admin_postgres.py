"""Opt-in real HTTP/JSONB/CAS/session/authority locks on a fresh loopback PG18.

Reuses the repository's strict disposable-cluster identity guard. No arbitrary
DSN, remote host, customer credentials or provider network is accepted. The
local PostgreSQL service must support TLS; this runner never starts a server.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
import importlib.util
import inspect
import json
import os
from pathlib import Path
from threading import Event
import time
from unittest.mock import patch

from tests.run_auth_session_lifecycle_postgres import (
    FixtureRefused, fixture_configuration, verify_empty_fixture,
)

PHASE = "fixture_configuration"


class FixtureFailure(RuntimeError):
    pass


def require(value, code):
    if not value:
        raise FixtureFailure(code)


def fixture(module, url):
    # Reuse the exact synthetic seed/Graph fixture used by the offline suite.
    # Only the transport-specific SQLite assertion and cleanup are replaced;
    # the outer runner owns the verified empty public schema.
    source = inspect.getsource(module.environment.__wrapped__)
    old = 'assert db.engine.dialect.name == "sqlite" and db.engine.url.database == ":memory:"'
    require(source.startswith("@pytest.fixture\n") and source.count(old) == 1
        and source.count('SQLALCHEMY_DATABASE_URI="sqlite:///:memory:"') == 1
        and source.count("        db.drop_all()\n") == 1, "meta_fixture_shape_changed")
    source = source.removeprefix("@pytest.fixture\n").replace(old,
        'assert db.engine.dialect.name == "postgresql"').replace(
        'SQLALCHEMY_DATABASE_URI="sqlite:///:memory:"', 'SQLALCHEMY_DATABASE_URI=FIXTURE_URL').replace(
        "        db.drop_all()\n", "")
    scope = {**vars(module), "FIXTURE_URL": url}
    exec(compile(source, "synthetic_meta_admin_postgres_fixture.py", "exec"), scope)
    return contextmanager(scope["environment"])()


def exercise(target):
    global PHASE
    import psycopg
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from flask import current_app
    from sqlalchemy import create_engine, event, select, text
    from sqlalchemy.engine import URL
    from models import AuditEvent, ProviderConnection, User, db
    from extensions import limiter
    from middleware.cutover_writer_fence import register_cutover_writer_fence
    from routes.meta_credential_admin import meta_credential_admin_bp
    from services import global_writer_authority as authority
    from services import meta_credential_admin_runtime as runtime
    from services import meta_credential_operations as ops
    from services.auth_session_lifecycle import issue_token, revoke_provider_session
    from utils import meta_credential_http as boundary
    from utils.auth_helpers import auth_session_version
    from tests import test_meta_credential_operations as seed

    url = URL.create("postgresql+psycopg", username=target["user"], password=target["password"],
        host="127.0.0.1", port=target["port"], database=target["dbname"], query={"sslmode": "require"})
    root = create_engine(url, hide_parameters=True)
    cases = []
    PHASE = "synthetic_schema"
    try:
        with fixture(seed, url) as rows:
            engine, app = db.engine, rows.app
            now = int(time.time())
            app.config.update(SECRET_KEY=os.environ["SECRET_KEY"], META_CREDENTIAL_ADMIN_ENABLED=True,
                AUTH_TOKEN_COOKIE_NAME="auth_token", RATELIMIT_STORAGE_URI="memory://", RATELIMIT_ENABLED=True,
                CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED=True, CUTOVER_RUNTIME_IDENTITY="vercel",
                CUTOVER_GLOBAL_WRITER_AUTHORITY_DATABASE_URL=url.render_as_string(hide_password=False))
            boundary.register_meta_credential_http_boundary(app)
            register_cutover_writer_fence(app)
            app.register_blueprint(meta_credential_admin_bp)
            limiter.init_app(app)
            limiter.reset()
            with engine.begin() as connection:
                for table in ("user", "tenant_profile", "provider_connection", "provider_sender", "whatsapp_numero"):
                    connection.execute(text(f"SELECT setval(pg_get_serial_sequence('public.\"{table}\"','id'), "
                        f"(SELECT max(id) FROM public.\"{table}\"), true)"))
                path = Path(__file__).resolve().parents[1] / "migrations/versions/20260829_add_global_writer_authority.py"
                spec = importlib.util.spec_from_file_location("meta_admin_fixture_authority", path)
                migration = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(migration)
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
                connection.execute(text("UPDATE public.cutover_global_writer_authority SET owner_runtime='vercel', "
                    "epoch=6, render_fenced=TRUE, vercel_fenced=FALSE WHERE authority_key='primary'"))
                require(connection.execute(text("SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()"))
                    .scalar_one() is True, "fixture_tls_required")

            def token(sid):
                actor = db.session.get(User, 1)
                result = issue_token({"user_id": 1, "session_kind": "clerk", "auth_provider": "clerk",
                    "clerk_sid": sid, "sv": auth_session_version(actor), "exp": now + 3600,
                    "audience": "tenant_owner", "auth_intent": "tenant_owner"})
                db.session.remove()
                return result
            first_sid = "synthetic-postgres-admin-first"
            bearer = token(first_sid)

            def graph(method, address, **kwargs):
                result = rows.graph(method, address, **kwargs)
                if address.endswith("/debug_token"):
                    payload = json.loads(result.content)
                    payload["data"].update(expires_at=now + 7200, data_access_expires_at=now + 7200)
                    result.content = json.dumps(payload).encode()
                return result
            slot = ContextVar("meta_admin_fixture_slot", default=None)
            def post(action, body, label=None, credential=None):
                marker = slot.set(label)
                try:
                    with app.test_client() as client:
                        response = client.post(boundary.PREFIX + "/" + action, json=body,
                            headers={"Authorization": "Bearer " + (credential or bearer)}, buffered=True)
                        return response.status_code, response.get_json()
                finally:
                    slot.reset(marker)
            def approved(name, credential=None):
                body = {"target": name, "access_token": seed.TOKEN,
                    "expected_revision": ops.TARGETS[name].expected_revision, "expected_writer_epoch": 6}
                code, result = post("plan", body, credential=credential)
                require(code == 200, "postgres_plan_rejected")
                return {**body, "expires_at": result["plan"]["expires_at"],
                    "approved_plan_sha256": result["plan_sha256"],
                    "expected_database_identity_sha256": result["plan"]["database_identity_sha256"]}
            def waits(application_name):
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    with root.connect() as monitor:
                        waiting = monitor.execute(text("SELECT count(*) FROM pg_stat_activity WHERE application_name=:name "
                            "AND wait_event_type='Lock' AND cardinality(pg_blocking_pids(pid))>0"),
                            {"name": application_name}).scalar_one()
                    if waiting:
                        return
                    Event().wait(.025)
                raise FixtureFailure("physical_lock_wait_not_observed")

            with patch.object(boundary, "runtime_is_production", lambda: True), patch.object(
                    runtime, "native_graph_request", graph):
                PHASE = "concurrent_tdf_cas"
                value = approved("tdf-sandbox")
                locked, release, attempted = Event(), Event(), Event()
                pids = {}
                def before(connection, _cursor, statement, *_):
                    if "LOCK TABLE public.provider_connection" in statement and slot.get() == "loser":
                        pids["loser"] = connection.connection.driver_connection.info.backend_pid
                        attempted.set()
                def after(connection, _cursor, statement, *_):
                    if "LOCK TABLE public.provider_connection" in statement and slot.get() == "winner":
                        pids["winner"] = connection.connection.driver_connection.info.backend_pid
                        locked.set()
                        require(release.wait(10), "cas_holder_timeout")
                event.listen(engine, "before_cursor_execute", before)
                event.listen(engine, "after_cursor_execute", after)
                try:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        winner = pool.submit(post, "apply", value, "winner")
                        try:
                            require(locked.wait(5), "cas_holder_lock_missing")
                            loser = pool.submit(post, "apply", value, "loser")
                            require(attempted.wait(5), "cas_contender_lock_missing")
                            require(pids["winner"] != pids["loser"], "cas_connections_not_distinct")
                            deadline = time.monotonic() + 2
                            while True:
                                with root.connect() as monitor:
                                    blockers = monitor.execute(text("SELECT pg_blocking_pids(:pid)"),
                                        {"pid": pids["loser"]}).scalar_one()
                                if pids["winner"] in blockers:
                                    break
                                require(time.monotonic() < deadline, "cas_physical_table_wait_missing")
                                Event().wait(.025)
                            require(not loser.done(), "cas_loser_did_not_wait")
                        finally:
                            release.set()
                        first, second = winner.result(timeout=12), loser.result(timeout=12)
                finally:
                    event.remove(engine, "before_cursor_execute", before)
                    event.remove(engine, "after_cursor_execute", after)
                require(first[0] == 200 and first[1].get("committed") is True, "cas_winner_not_committed")
                require(second[0] in {409, 503} and second[1].get("committed") is not True, "cas_two_winners")
                db.session.remove()
                require(db.session.get(ProviderConnection, 3).config[seed.PRIVATE_CONFIG_KEY]["revision"] == 7,
                    "cas_final_revision_wrong")
                require(AuditEvent.query.count() == 1, "cas_audit_not_unique")
                db.session.remove()
                cases.append("real_table_lock_jsonb_cas_one_winner")

                PHASE = "revoked_before_apply"
                value = approved("junin-production")
                revoke_provider_session(first_sid, reason="synthetic_fixture", commit=True)
                db.session.remove()
                require(post("apply", value)[0] == 401, "revoked_session_admitted")
                require(AuditEvent.query.count() == 1, "revoked_session_wrote")
                db.session.remove()
                cases.append("revoked_clerk_session_rejected_before_apply")

                PHASE = "commit_session_and_authority_locks"
                second_sid = "synthetic-postgres-admin-second"
                second_bearer = token(second_sid)
                value = approved("junin-production", second_bearer)
                committing, release = Event(), Event()
                def pause_commit(_connection):
                    if slot.get() == "commit-holder":
                        committing.set()
                        require(release.wait(10), "commit_holder_timeout")
                def revoke():
                    with app.app_context():
                        db.session.execute(text("SET LOCAL application_name='meta_admin_session_revoke'"))
                        db.session.execute(text("SET LOCAL lock_timeout='8000ms'"))
                        revoke_provider_session(second_sid, reason="synthetic_fixture", commit=True)
                        db.session.remove()
                        return True
                def transition():
                    with root.begin() as connection:
                        connection.execute(text("SET LOCAL application_name='meta_admin_authority_transition'"))
                        connection.execute(text("SET LOCAL lock_timeout='8000ms'"))
                        return authority.attest_runtime_fenced(connection, runtime="vercel", expected_epoch=6)
                event.listen(engine, "commit", pause_commit)
                try:
                    with ThreadPoolExecutor(max_workers=3) as pool:
                        writer = pool.submit(post, "apply", value, "commit-holder", second_bearer)
                        try:
                            require(committing.wait(5), "actual_commit_boundary_missing")
                            revoker = pool.submit(revoke)
                            switch = pool.submit(transition)
                            waits("meta_admin_session_revoke")
                            waits("meta_admin_authority_transition")
                            require(not revoker.done() and not switch.done(), "admitted_commit_not_protected")
                        finally:
                            release.set()
                        result = writer.result(timeout=12)
                        require(revoker.result(timeout=12), "session_revocation_did_not_complete")
                        changed = switch.result(timeout=12)
                finally:
                    event.remove(engine, "commit", pause_commit)
                require(result[0] == 200 and result[1].get("committed") is True, "protected_commit_failed")
                require(changed.epoch == 7 and changed.vercel_fenced, "authority_transition_failed")
                require(post("status", {"target": "junin-production"}, credential=second_bearer)[0] == 401,
                    "revoked_postcommit_session_survived")
                db.session.remove()
                require(AuditEvent.query.count() == 2, "protected_commit_audit_wrong")
                db.session.remove()
                cases.extend(["real_session_revocation_waits_for_credential_commit",
                              "real_global_authority_waits_through_http_commit"])
        return {"successful": True, "cases_passed": cases, "count": len(cases),
                "target": "fresh_loopback_postgres18_tls", "provider_calls": "synthetic_get_only",
                "remote_database_access": False, "runtime_identity": "synthetic_test_configuration"}
    finally:
        if authority._CONTROL_ENGINE is not None:
            authority._CONTROL_ENGINE.dispose()
        with root.begin() as cleanup:
            cleanup.execute(text("DROP SCHEMA public CASCADE"))
            cleanup.execute(text("CREATE SCHEMA public"))
        root.dispose()


def main(argv=None):
    global PHASE
    target = fixture_configuration(argv)
    from tests.profile_acceptance_runtime import prepare_process
    prepare_process()
    os.environ["CLERK_SUPERADMIN_EMAILS"] = "operator@example.invalid"
    os.environ["CORS_ALLOWED_ORIGINS"] = "https://panel.example.invalid"
    import psycopg
    PHASE = "verify_empty_fixture"
    with psycopg.connect(**target, sslmode="require", connect_timeout=3) as connection:
        verify_empty_fixture(connection)
    result = exercise(target)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FixtureRefused, FixtureFailure) as error:
        print(json.dumps({"successful": False, "phase": PHASE, "reason_code": str(error)}))
        raise SystemExit(2)
    except Exception as error:
        print(json.dumps({"successful": False, "phase": PHASE,
            "reason_code": "disposable_fixture_failed", "error_type": type(error).__name__}))
        raise SystemExit(1)
