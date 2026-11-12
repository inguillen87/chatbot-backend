"""Exercise survey voting and content review on the dedicated PostgreSQL fixture."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
import inspect
import json
import os
from pathlib import Path
import re
import socket
import sys
from threading import Event
from time import monotonic, sleep
import traceback
from unittest.mock import patch
from uuid import uuid4

from tests.run_auth_session_lifecycle_postgres import (
    FixtureRefused, fixture_configuration, verify_empty_fixture,
)

HOST = "127.0.0.1"


class FixtureFailure(RuntimeError):
    """Error label without request, identity or connection contents."""


def require(condition, code):
    if not condition:
        raise FixtureFailure(code)


def adapt_crm_fixture(crm, config):
    # Reuse the signed Meta fixture's setup and provider doubles, replacing only
    # its SQLite check and teardown. The owned PostgreSQL schema is dropped below.
    source = inspect.getsource(crm.crm_environment.__wrapped__)
    marker = 'assert str(db.engine.url) == "sqlite:///:memory:"'
    require(source.startswith("@pytest.fixture\n") and source.count(marker) == 1
            and source.count("            db.drop_all()\n") == 1, "crm_fixture_shape_changed")
    source = source.removeprefix("@pytest.fixture\n").replace(
        marker, 'assert db.engine.dialect.name == "postgresql"'
    ).replace("            db.drop_all()\n", "")
    scope = {**vars(crm), "_MetaCrmAcceptanceConfig": config}
    exec(compile(source, "synthetic_postgres_crm_fixture.py", "exec"), scope)
    return contextmanager(scope["crm_environment"])()


def lock_resource(statement):
    sql = " ".join(statement.casefold().split())
    selects_lock = re.search(r"\bfor (?:no key update|key share|update|share)\b", sql)
    if re.search(r"\b(?:from|update) (?:[a-z0-9_]+\.)?enc_encuesta\b", sql):
        if selects_lock or sql.startswith("update "):
            return "survey"
    if selects_lock and re.search(r"\bfrom (?:[a-z0-9_]+\.)?tenant_profile\b", sql):
        return "tenant"
    return None


def observe_order(engine, target, holder, vote, review):
    from sqlalchemy import event
    import psycopg

    slot = ContextVar("survey_fixture_slot", default=None)
    paused, waiting, release = Event(), Event(), Event()
    waiter = "review" if holder == "vote" else "vote"
    locks = {"vote": [], "review": []}
    pids = {}

    def before(connection, cursor, statement, *_):
        actor, resource = slot.get(), lock_resource(statement)
        if actor is None or resource is None:
            return
        if resource == "tenant":
            require("survey" in locks[actor], "tenant_lock_requested_before_survey")
        pids[actor] = connection.connection.driver_connection.info.backend_pid
        locks[actor].append(resource)
        if actor == waiter and resource == "survey":
            waiting.set()

    def after(connection, cursor, statement, *_):
        if slot.get() == holder and lock_resource(statement) == "survey" and not paused.is_set():
            paused.set()
            require(release.wait(17), "fixture_holder_not_released")

    def run(actor):
        token = slot.set(actor)
        try:
            return vote() if actor == "vote" else review()
        finally:
            slot.reset(token)

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {holder: pool.submit(run, holder)}
            try:
                require(paused.wait(10), "holder_did_not_acquire_survey_lock")
                futures[waiter] = pool.submit(run, waiter)
                require(waiting.wait(8), "waiter_did_not_request_survey_lock")
                require(pids[holder] != pids[waiter], "concurrent_connections_not_distinct")
                require(locks[holder] == locks[waiter] == ["survey"], "first_lock_order_mismatch")
                with psycopg.connect(**target, autocommit=True) as monitor:
                    deadline = monotonic() + 4
                    while True:
                        blockers = monitor.execute("SELECT pg_blocking_pids(%s)", (pids[waiter],)).fetchone()[0]
                        if pids[holder] in blockers:
                            break
                        require(monotonic() < deadline, "no_physical_survey_lock_wait")
                        sleep(.05)
                require(not futures[waiter].done(), "waiter_completed_during_lock")
            finally:
                release.set()
            results = {name: task.result(timeout=18) for name, task in futures.items()}
    finally:
        event.remove(engine, "before_cursor_execute", before)
        event.remove(engine, "after_cursor_execute", after)
    require(all(sequence[0] == "survey" and "tenant" in sequence for sequence in locks.values()),
            "expected_lock_order_not_observed")
    return results, locks


def exercise(target):
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import URL
    from sqlalchemy.schema import CreateSchema, DropSchema
    from models import WhatsAppOutboundAttempt, db
    from models_survey_jurisdiction import SurveyContentReceipt
    from services import whatsapp_inbound_worker as worker
    from services.constants import CONTEXTO_MUNICIPIO
    from services.whatsapp_survey_conversation import WHATSAPP_SURVEY_FLOW_STATE_KEY
    from tests import test_meta_whatsapp_crm_acceptance as crm
    from tests import test_meta_whatsapp_survey_journey as journey
    from tests.test_meta_whatsapp_inbound_worker import _post, _turn

    schema = "survey_order_" + uuid4().hex
    url = URL.create("postgresql+psycopg", username=target["user"], password=target["password"],
                     host=HOST, port=target["port"], database=target["dbname"])
    connect = {"connect_timeout": 3, "sslmode": "disable", "gssencmode": "disable",
               "options": f"-c search_path={schema},public -c statement_timeout=17000 -c lock_timeout=12000"}
    control = create_engine(url, connect_args=connect)

    class PostgresConfig(crm._MetaCrmAcceptanceConfig):
        SQLALCHEMY_DATABASE_URI = url
        SQLALCHEMY_ENGINE_OPTIONS = {"execution_options": {"schema_translate_map": {None: schema}},
                                     "connect_args": connect}
        SESSION_TYPE = "null"
        SESSION_COOKIE_SAMESITE = "Lax"
        RATELIMIT_STORAGE_URI = "memory://"
        OUTBOUND_NOTIFICATIONS_ENABLED = False

    try:
        with control.begin() as connection:
            connection.execute(CreateSchema(schema))
        try:
            with adapt_crm_fixture(crm, PostgresConfig) as env:
                require(db.session.scalar(text("SHOW transaction_isolation")) == "read committed",
                        "application_isolation_mismatch")
                env.tenant.slug = env.owner.tenant_slug = "junin"
                env.tenant.configuracion = {**(env.tenant.configuracion or {}), "encuestas_enabled": True}
                db.session.commit()
                tenant_id = env.tenant.id
                admin, headers = crm._login(env, env.owner)
                builder = journey.survey_fixture.WhatsAppSurveyConversationTest(methodName="runTest")
                builder.app, builder.tenant, builder.owner, builder.client = env.app, env.tenant, env.owner, admin
                builder._admin_headers = lambda: headers
                receipts = []
                for holder, decision in (("review", "block"), ("vote", "approve")):
                    survey = builder._create_governed_survey(slug=f"concurrent-native-{holder}")
                    survey_id = survey.id
                    env.app.config.update(SURVEY_JURISDICTION_GATE_MODE="enforce_visibility",
                                          SURVEY_JURISDICTION_GATE_TENANT_IDS=str(tenant_id))
                    journey.begin(env, holder)
                    journey.send(env, "1", f"{holder}-first-answer")
                    turn_id = _post(env, body="2", message_id=f"wamid.synthetic/order-{holder}-final+=")
                    endpoint = f"/api/v2/surveys/{survey_id}/content-review"
                    readiness = admin.get(endpoint, headers=headers)
                    require(readiness.status_code == 200, "content_review_readiness_failed")
                    expected = readiness.get_json()["jurisdiction"]["content_sha256"]
                    key = f"qa.native.concurrent.{holder}.{survey_id}"
                    db.session.rollback()

                    def vote():
                        with env.app.app_context():
                            return worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)

                    def review():
                        return admin.post(endpoint, headers={**headers, "Idempotency-Key": key},
                                          json={"decision": decision, "expected_content_sha256": expected,
                                                "evidence_ref": f"qa-review:postgres-{holder}"})

                    outcomes, locks = observe_order(db.engine, target, holder, vote, review)
                    require(outcomes["vote"]["completed"] == 1, "native_vote_turn_did_not_complete")
                    require(outcomes["review"].status_code == 201, "content_review_did_not_commit")
                    db.session.rollback()
                    row = _turn(turn_id)
                    attempts = WhatsAppOutboundAttempt.query.filter_by(inbound_turn_id=row.id).all()
                    require(row.status == "completed" and len(attempts) == 1
                            and attempts[0].status == "pending" and attempts[0].provider == "meta",
                            "native_turn_outbox_state_invalid")
                    body = attempts[0].payload_json["message"]["text"]["body"]
                    expected_message = "ya no está disponible" if holder == "review" else "Participación registrada"
                    require(expected_message in body, "native_turn_reply_invalid")
                    counts = journey.counts(survey)
                    require(counts == ((0, 0, 0) if holder == "review" else (1, 1, 2)), "survey_rows_invalid")
                    content = SurveyContentReceipt.query.filter_by(
                        survey_id=survey_id, tenant_id=tenant_id, idempotency_key=key).one()
                    require(content.event_type == ("review_blocked" if decision == "block" else "review_approved")
                            and content.request_content_sha256 == expected, "content_review_receipt_invalid")
                    municipal = journey.context(row).context_data[CONTEXTO_MUNICIPIO]
                    require(WHATSAPP_SURVEY_FLOW_STATE_KEY not in municipal, "native_survey_flow_not_cleared")
                    receipts.append({"first_lock_holder": holder, "distinct_connections": True,
                                     "pg_blocking_pids_observed": True, "lock_requests": locks,
                                     "review_http": 201, "review_event": content.event_type,
                                     "turn_status": row.status, "outbox_pending": len(attempts),
                                     "responses": counts[0], "receipts": counts[1], "effects": counts[2]})
                    db.session.rollback()
                return receipts
        finally:
            with control.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
            with control.connect() as connection:
                require(connection.scalar(text("SELECT to_regnamespace(:schema) IS NULL"), {"schema": schema}),
                        "owned_schema_cleanup_failed")
    finally:
        control.dispose()


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    target = fixture_configuration(arguments)
    target.update(hostaddr=HOST, connect_timeout=3, sslmode="disable", gssencmode="disable")
    from tests.profile_acceptance_runtime import prepare_process

    prepare_process()
    os.environ["CORS_ALLOWED_ORIGINS"] = "https://panel.example.invalid"
    import psycopg

    with psycopg.connect(**target) as connection:
        verify_empty_fixture(connection)
        address, port = connection.execute("SELECT inet_server_addr(), inet_server_port()").fetchone()
        require(port == target["port"], "fixture_server_port_mismatch")
        if "--fresh-loopback-fixture" in arguments:
            require(str(address) == HOST, "fixture_server_not_loopback")
        require(connection.execute("SHOW default_transaction_isolation").fetchone()[0] == "read committed",
                "fixture_isolation_mismatch")
        version = int(connection.execute("SHOW server_version_num").fetchone()[0])

    def deny(*_args, **_kwargs):
        raise FixtureFailure("python_network_outside_owned_fixture")

    with ExitStack() as stack:
        for method in ("connect", "connect_ex", "sendto"):
            stack.enter_context(patch.object(socket.socket, method, deny))
        stack.enter_context(patch.object(socket, "getaddrinfo", deny))
        cases = exercise(target)
    print(json.dumps({"successful": True, "server_version_num": version, "isolation": "read committed",
                      "fixture_database": target["dbname"], "schema_removed": True,
                      "cases": cases, "remote_connections": 0}),
          flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FixtureFailure, FixtureRefused) as error:
        print(json.dumps({"successful": False, "reason_code": str(error)}), flush=True)
        raise SystemExit(2)
    except Exception as error:
        frames = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                  for frame in traceback.extract_tb(error.__traceback__)]
        print(json.dumps({"successful": False, "error_type": type(error).__name__, "frames": frames}), flush=True)
        raise SystemExit(1)
