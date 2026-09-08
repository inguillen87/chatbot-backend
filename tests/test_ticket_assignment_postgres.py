"""Opt-in, real PostgreSQL contention checks against a disposable local schema.

CHATBOC_ASSIGNMENT_POSTGRES_URL must name a loopback, non-default-port database
whose name starts with assignment_qa_. No production schema/migration is used.
Two request sessions contend; a third read-only connection observes PostgreSQL's
actual blocker PIDs, so a passing test cannot be a merely sequential simulation.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
import threading
import time
from types import SimpleNamespace
import uuid

import jwt
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from app import create_app, db
from config import TestingConfig
from models import MunicipioTicket, PymeTicket, TenantProfile, TenantTicket, TicketComentario, User


@pytest.fixture(scope="module")
def postgres_app():
    raw = os.getenv("CHATBOC_ASSIGNMENT_POSTGRES_URL")
    if not raw:
        pytest.skip("Disposable local PostgreSQL URL not provided")
    url = make_url(raw)
    assert url.drivername == "postgresql+psycopg2"
    assert url.host == "127.0.0.1" and url.port and url.port != 5432
    assert url.database and url.database.startswith("assignment_qa_")
    assert not url.query, "DSN query/service overrides are not accepted"
    schema = "assignment_qa_" + uuid.uuid4().hex
    admin_engine = create_engine(url, pool_pre_ping=True)
    with admin_engine.begin() as connection:
        assert connection.scalar(text("SELECT inet_server_addr()::text")).split("/")[0] == "127.0.0.1"
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    class PostgresConfig(TestingConfig):
        SQLALCHEMY_DATABASE_URI = raw
        SQLALCHEMY_ENGINE_OPTIONS = {
            "pool_size": 5,
            "max_overflow": 2,
            "connect_args": {"options": f"-csearch_path={schema} -clock_timeout=10000 -cstatement_timeout=15000"},
        }
        RATELIMIT_ENABLED = False
        TWILIO_ALLOW_NETWORK_IN_TESTS = False
        SECRET_KEY = "disposable-postgres-assignment-qa-not-a-production-secret"

    app = None
    try:
        app = create_app(PostgresConfig)
        with app.app_context():
            assert db.engine.dialect.name == "postgresql"
            assert db.session.scalar(text("SHOW transaction_isolation")) == "read committed"
        yield app
    finally:
        if app:
            with app.app_context():
                db.session.remove()
                db.engine.dispose()
        # This schema was generated and created above, never supplied by a caller.
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.fixture
def pg_case(postgres_app):
    unique = uuid.uuid4().hex[:12]
    with postgres_app.app_context():
        owner = User(name="QA supervisor", email=f"qa-owner-{unique}@test.local", password_hash="qa",
                     rol="admin", tipo_chat="municipio")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(slug=f"qa-{unique}", nombre="Disposable assignment QA", tipo="municipio",
                               municipio_id=owner.id, plan="full")
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id, owner.tenant_slug, owner.municipio_id = tenant.id, tenant.slug, owner.id
        operators = []
        for index in range(2):
            user = User(name=f"QA operator {index}", email=f"qa-op-{unique}-{index}@test.local", password_hash="qa",
                        rol="empleado", es_empleado=True, tipo_chat="municipio", tenant_id=tenant.id,
                        tenant_slug=tenant.slug, municipio_id=owner.id, empresa_id=owner.id,
                        accesibilidad={"employee_scope": {"categorias": ["bacheo"], "permisos": []}})
            db.session.add(user)
            operators.append(user)
        tickets = [
            MunicipioTicket(tenant_id=tenant.id, municipio_id=owner.id, user_id=owner.id,
                            asunto="QA", categoria="bacheo", pregunta="Synthetic QA", estado="nuevo"),
            PymeTicket(tenant_id=tenant.id, user_id=owner.id, categoria="bacheo", pregunta="Synthetic QA",
                       estado="nuevo", nro_ticket=543210 + owner.id),
            TenantTicket(tenant_id=tenant.id, user_id=owner.id, categoria="bacheo", descripcion="Synthetic QA",
                         estado="nuevo", origen="web", datos_extra={"title": "Synthetic QA", "channel": "web"}),
        ]
        db.session.add_all(tickets)
        db.session.commit()
        case = SimpleNamespace(app=postgres_app, engine=db.engine, tenant_id=tenant.id, slug=tenant.slug,
                               owner_id=owner.id, operator_ids=[operator.id for operator in operators],
                               tickets={type(ticket).__name__: ticket.id for ticket in tickets})
        return case


MODELS = {cls.__name__: cls for cls in (MunicipioTicket, PymeTicket, TenantTicket)}


def _action(case, model, actor_id, action="claim", **payload):
    with case.app.test_client() as client:
        role = "admin" if actor_id == case.owner_id else "empleado"
        token = jwt.encode({"user_id": actor_id, "rol": role, "tenant_slug": case.slug},
                           case.app.config["SECRET_KEY"], algorithm="HS256")
        response = client.post(f"/api/v2/inbox/omnichannel/{case.tickets[model]}/actions",
                               headers={"Authorization": f"Bearer {token}", "X-Tenant-Slug": case.slug,
                                        "X-Tenant": case.slug},
                               json={"action": action, "source_model": model, "ticket_id": case.tickets[model], **payload})
        return response.status_code, response.get_json()


def _snapshot(case, model):
    with case.app.app_context():
        ticket = db.session.get(MODELS[model], case.tickets[model])
        if model == "TenantTicket":
            extra = deepcopy(ticket.datos_extra or {})
            return extra.get("assignee_id"), len(extra.get("comments", [])), extra, ticket.categoria
        field = "municipio_ticket_id" if model == "MunicipioTicket" else "pyme_ticket_id"
        count = TicketComentario.query.filter_by(**{field: ticket.id}).count()
        return ticket.asignado_a_id, count, deepcopy(getattr(ticket, "datos_extra", None) or {}), ticket.categoria


def _race(case, model, first, second):
    """Hold request A after its real SELECT FOR UPDATE; prove B waits on A."""
    acquired, release, second_started = threading.Event(), threading.Event(), threading.Event()
    pids = {}
    table = MODELS[model].__tablename__

    def is_target(statement):
        return "FOR UPDATE" in statement.upper() and table in statement

    def before(connection, cursor, statement, parameters, context, executemany):
        name = threading.current_thread().name
        if name == "assignment-qa-second" and is_target(statement):
            pids["second"] = cursor.connection.get_backend_pid()
            second_started.set()

    def after(connection, cursor, statement, parameters, context, executemany):
        name = threading.current_thread().name
        if name == "assignment-qa-first" and is_target(statement) and not acquired.is_set():
            pids["first"] = cursor.connection.get_backend_pid()
            acquired.set()
            assert release.wait(12), "Timed out waiting to release first PostgreSQL row lock"

    def run(name, operation):
        threading.current_thread().name = name
        return operation()

    event.listen(case.engine, "before_cursor_execute", before)
    event.listen(case.engine, "after_cursor_execute", after)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(run, "assignment-qa-first", first)
            try:
                assert acquired.wait(8), first_future.result(timeout=1) if first_future.done() else "First request never locked ticket"
                second_future = pool.submit(run, "assignment-qa-second", second)
                assert second_started.wait(8), second_future.result(timeout=1) if second_future.done() else "Second request never reached lock"
                assert pids["first"] != pids["second"]
                blocked = False
                deadline = time.monotonic() + 5
                with case.engine.connect() as observer:
                    while time.monotonic() < deadline:
                        blockers = observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": pids["second"]})
                        if pids["first"] in blockers:
                            blocked = True
                            break
                        time.sleep(0.025)
                assert blocked, f"PostgreSQL did not report the expected blocker: {pids}"
                assert not second_future.done()
            finally:
                release.set()
            return first_future.result(timeout=18), second_future.result(timeout=18)
    finally:
        event.remove(case.engine, "before_cursor_execute", before)
        event.remove(case.engine, "after_cursor_execute", after)


@pytest.mark.parametrize("model", list(MODELS))
@pytest.mark.parametrize("same_actor", [False, True])
def test_postgres_competing_claims_and_replay(pg_case, model, same_actor):
    case = pg_case
    first_id, other_id = case.operator_ids
    second_id = first_id if same_actor else other_id
    first, second = _race(case, model, lambda: _action(case, model, first_id), lambda: _action(case, model, second_id))
    assert first[0] == 200, first
    assert second[0] == (200 if same_actor else 409), second
    if same_actor:
        assert second[1]["delivery"]["idempotent_replay"] is True
    assert _snapshot(case, model)[:2] == (first_id, 1)


@pytest.mark.parametrize("model", list(MODELS))
@pytest.mark.parametrize("same_target", [False, True])
def test_postgres_supervisor_compare_and_set_race(pg_case, model, same_target):
    case = pg_case
    first_id, other_id = case.operator_ids
    second_id = first_id if same_target else other_id
    def assign(target):
        return _action(case, model, case.owner_id, "assign", assignee_id=target, expected_assignee_id=None)
    first, second = _race(case, model, lambda: assign(first_id), lambda: assign(second_id))
    assert first[0] == 200, first
    assert second[0] == (200 if same_target else 409), second
    if same_target:
        assert second[1]["delivery"]["idempotent_replay"] is True
    assert _snapshot(case, model)[:2] == (first_id, 1)
    assert _action(case, model, case.owner_id, "assign", assignee_id=other_id,
                   expected_assignee_id=first_id)[0] == 200
    assert _snapshot(case, model)[:2] == (other_id, 2)


@pytest.mark.parametrize("change_category", [False, True])
def test_postgres_stale_json_comment_preserves_assignment_or_denies_new_category(pg_case, change_category):
    from services.ticket_assignment_policy import lock_assignment_ticket
    from services.v2.ticket_service import add_comment
    case = pg_case
    loaded, proceed = threading.Event(), threading.Event()

    def comment():
        threading.current_thread().name = "assignment-qa-second"
        with case.app.test_request_context():
            ticket = db.session.get(TenantTicket, case.tickets["TenantTicket"])
            assert (ticket.datos_extra or {}).get("assignee_id") is None
            loaded.set()
            assert proceed.wait(12)
            tenant = db.session.get(TenantProfile, case.tenant_id)
            actor = db.session.get(User, case.operator_ids[0])
            try:
                result = add_comment(tenant=tenant, actor_user=actor, ticket=ticket, body="Synthetic internal note", visibility="internal")
            except LookupError as exc:
                db.session.rollback()
                return str(exc)
            db.session.commit()
            return result

    def first_operation():
        if not change_category:
            return _action(case, "TenantTicket", case.operator_ids[0])
        with case.app.app_context():
            ticket = lock_assignment_ticket(db.session.get(TenantTicket, case.tickets["TenantTicket"]))
            ticket.categoria = "arbolado"
            db.session.commit()
            return "category changed"

    # _race starts the first operation first. Preload B's independent identity map
    # before A starts, then release B to its production lock once A has the lock.
    with ThreadPoolExecutor(max_workers=1) as preload_pool:
        pending = preload_pool.submit(comment)
        assert loaded.wait(8)
        def second():
            # The worker holding the stale identity map must carry the observed
            # name so the real SQL event records its PostgreSQL session PID.
            proceed.set()
            return pending.result(timeout=18)
        try:
            first, result = _race(case, "TenantTicket", first_operation, second)
        finally:
            proceed.set()
    snapshot = _snapshot(case, "TenantTicket")
    if change_category:
        assert first == "category changed"
        assert result == "ticket_not_found"
        assert snapshot[:2] == (None, 0)
        assert snapshot[3] == "arbolado"
        return
    assert first[0] == 200, first
    assert snapshot[:2] == (case.operator_ids[0], 2)
    assert result["body"] == "Synthetic internal note"
    assert snapshot[2]["comments"][-1]["body"] == "Synthetic internal note"


@pytest.mark.parametrize("model", list(MODELS))
def test_postgres_category_refresh_denies_claim_after_lock_wait(pg_case, model):
    from services.ticket_assignment_policy import lock_assignment_ticket
    case = pg_case

    def change_category():
        with case.app.app_context():
            ticket = db.session.get(MODELS[model], case.tickets[model])
            ticket = lock_assignment_ticket(ticket)
            ticket.categoria = "arbolado"
            db.session.commit()
            return "category changed"

    first, second = _race(case, model, change_category, lambda: _action(case, model, case.operator_ids[0]))
    assert first == "category changed"
    assert second[0] == 404, second
    snapshot = _snapshot(case, model)
    assert snapshot[:2] == (None, 0)
    assert snapshot[3] == "arbolado"


def test_postgres_legacy_assignment_rechecks_actor_category_after_wait(pg_case):
    from services.ticket_assignment_policy import lock_assignment_ticket
    case = pg_case
    with case.app.app_context():
        actor = db.session.get(User, case.operator_ids[0])
        actor.accesibilidad = {"employee_scope": {"categorias": ["bacheo"], "permisos": ["tickets.assign"]}}
        target = db.session.get(User, case.operator_ids[1])
        target.ticket_categorias = "bacheo,arbolado"
        db.session.commit()

    def change_category():
        with case.app.app_context():
            ticket = lock_assignment_ticket(db.session.get(MunicipioTicket, case.tickets["MunicipioTicket"]))
            ticket.categoria = "arbolado"
            db.session.commit()

    def legacy_assign():
        with case.app.test_client() as client:
            token = jwt.encode({"user_id": case.operator_ids[0], "rol": "empleado", "tenant_slug": case.slug},
                               case.app.config["SECRET_KEY"], algorithm="HS256")
            response = client.post(f"/tickets/municipio/{case.tickets['MunicipioTicket']}/asignar",
                                   headers={"Authorization": f"Bearer {token}", "X-Tenant-Slug": case.slug},
                                   json={"user_id": case.operator_ids[1], "expected_assignee_id": None})
            return response.status_code, response.get_json()

    _, second = _race(case, "MunicipioTicket", change_category, legacy_assign)
    assert second[0] == 404, second
    assert _snapshot(case, "MunicipioTicket")[:2] == (None, 0)
