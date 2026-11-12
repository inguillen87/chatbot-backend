"""Real Flask-SocketIO dispatch with offline DB/provider and lease boundaries."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, local
from types import SimpleNamespace

from flask import Flask, current_app, has_request_context, request, session
import pytest
from sqlalchemy import event

from models import ChatSessionContext, Rubro, TenantProfile, User, db
import services.global_writer_authority as authority
import services.municipio_responder as responder
import socket_service as sockets


class LeaseTracker:
    """Track local admission scopes; no PostgreSQL or provider acceptance implied."""

    def __init__(self):
        self.allowed = True
        self.events = []
        self.thread = local()
        self.leases = []

    @property
    def active(self):
        return getattr(self.thread, "active", None)

    @contextmanager
    def lease(self, config, *, request_lifetime=False):
        assert config is current_app.config
        assert request_lifetime is True
        assert self.active is None
        decision = authority.GlobalWriterAuthorityDecision(
            self.allowed, True,
            "runtime_is_global_writer_owner" if self.allowed else "runtime_globally_fenced",
            2,
        )
        lease = authority.GlobalWriterAuthorityLease(decision, object())
        self.leases.append(lease)
        self.thread.active = lease
        self.events.append("lease_enter")
        try:
            yield lease
        finally:
            self.events.append("lease_release")
            self.thread.active = None

    def effect(self, name):
        assert self.active is not None
        assert self.active.decision.allowed
        self.events.append(name)


@pytest.fixture
def runtime(monkeypatch):
    app = Flask(__name__)
    app.config.update(
        TESTING=True, SECRET_KEY="offline-socket-test",
        CUTOVER_WRITER_FENCE_ENABLED=False,
        CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED=True,
        CUTOVER_RUNTIME_IDENTITY="vercel",
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"check_same_thread": False}},
    )
    sockets.socketio.init_app(app, message_queue=None, manage_session=True)
    tracker = LeaseTracker()
    monkeypatch.setattr(sockets, "global_writer_authority_lease", tracker.lease)
    client = sockets.socketio.test_client(app)
    assert client.is_connected()
    yield app, client, tracker
    if client.is_connected():
        client.disconnect()


@pytest.mark.parametrize("event_name,error_event", [
    ("send_chat_message", "chat_error"), ("location", "location_error"),
])
@pytest.mark.parametrize("static_fence", [False, True])
def test_mutating_events_refuse_before_identity_db_and_provider(runtime, monkeypatch, event_name, error_event, static_fence):
    app, client, tracker = runtime
    app.config["CUTOVER_WRITER_FENCE_ENABLED"] = static_fence
    tracker.allowed = False

    def unexpected(*args, **kwargs):
        pytest.fail("denied event reached identity, DB or provider work")

    monkeypatch.setattr(sockets, "_socket_request_token", unexpected)
    monkeypatch.setattr(sockets, "_resolve_location_response_room", unexpected)
    monkeypatch.setattr(sockets, "_user_from_token", unexpected)
    monkeypatch.setattr(responder, "handle_location_update", unexpected)
    client.emit(event_name, {"token": "local", "lat": 1, "lon": 2})
    received = client.get_received()
    assert len(received) == 1 and received[0]["name"] == error_event
    payload = received[0]["args"][0]
    reason = "cutover_writer_fence_enabled" if static_fence else "runtime_globally_fenced"
    assert payload == {
        "error": reason, "contract_version": "cutover.socket_writer_fence.v1",
        "status": "maintenance", "reason_code": reason,
        "request_dispatched": False, "retryable": True,
    }
    assert tracker.events == ([] if static_fence else ["lease_enter", "lease_release"])


@pytest.mark.parametrize("event_name,error_event", [
    ("send_chat_message", "chat_error"), ("location", "location_error"),
])
def test_acquisition_failure_is_safe_error_without_dispatch(runtime, monkeypatch, event_name, error_event):
    _, client, tracker = runtime

    @contextmanager
    def unavailable(config, *, request_lifetime=False):
        assert request_lifetime
        raise authority.GlobalWriterAuthorityTransitionError("global_writer_control_unavailable")
        yield

    monkeypatch.setattr(sockets, "global_writer_authority_lease", unavailable)
    monkeypatch.setattr(sockets, "_socket_request_token", lambda *_: pytest.fail("dispatch"))
    monkeypatch.setattr(sockets, "_resolve_location_response_room", lambda *_: pytest.fail("dispatch"))
    client.emit(event_name, {})
    received = client.get_received()
    assert received[0]["name"] == error_event
    assert received[0]["args"][0]["reason_code"] == "global_writer_control_unavailable"
    assert tracker.events == []


@pytest.mark.parametrize("static_fence", [False, True])
def test_web_connect_denied_before_background_scheduling(runtime, monkeypatch, static_fence):
    app, _, tracker = runtime
    tracker.allowed = False
    app.config["CUTOVER_WRITER_FENCE_ENABLED"] = static_fence
    monkeypatch.setattr(sockets.socketio, "start_background_task", lambda *_: pytest.fail("background scheduled"))
    denied_client = sockets.socketio.test_client(app, auth={"channel": "web", "tenant_slug": "offline-municipio"})
    assert not denied_client.is_connected()
    assert tracker.events == ([] if static_fence else ["lease_enter", "lease_release"])


@pytest.mark.parametrize("static_fence", [False, True])
def test_queued_welcome_rechecks_ownership_when_background_starts(runtime, monkeypatch, static_fence):
    app, _, tracker = runtime
    queued = []

    def enqueue(*args):
        tracker.effect("background_scheduled")
        queued.append(args)

    monkeypatch.setattr(sockets.socketio, "start_background_task", enqueue)
    monkeypatch.setattr(sockets, "_send_admitted_welcome_message", lambda *_: pytest.fail("background effects"))
    web_client = sockets.socketio.test_client(app, auth={"channel": "web", "tenant_slug": "offline-municipio"})
    assert web_client.is_connected()
    assert tracker.events == ["lease_enter", "background_scheduled", "lease_release"]
    tracker.allowed = False
    app.config["CUTOVER_WRITER_FENCE_ENABLED"] = static_fence
    target, *args = queued[0]
    assert target(*args) is False
    assert tracker.events == ["lease_enter", "background_scheduled", "lease_release"] + (
        [] if static_fence else ["lease_enter", "lease_release"]
    )
    web_client.disconnect()


def location_provider(runtime, monkeypatch, effect):
    _, _, tracker = runtime
    monkeypatch.setattr(sockets, "_resolve_location_response_room", lambda _: request.sid)

    def location(payload):
        tracker.effect("geocoder")
        session["location"] = payload["lat"]
        with authority.global_writer_authority_lease(current_app.config) as nested:
            assert nested is tracker.active
        effect()
        return {"message_body": "local result"}

    monkeypatch.setattr(responder, "handle_location_update", location)
    monkeypatch.setattr(authority, "global_writer_authority_connection", lambda *_args, **_kwargs: pytest.fail("nested connection"))


def test_admitted_location_preserves_result_and_reuses_active_lease(runtime, monkeypatch):
    _, client, tracker = runtime
    location_provider(runtime, monkeypatch, lambda: tracker.effect("provider_done"))
    original_emit = sockets.socketio.emit

    def tracked_emit(*args, **kwargs):
        tracker.effect("message_emit")
        return original_emit(*args, **kwargs)

    monkeypatch.setattr(sockets.socketio, "emit", tracked_emit)
    client.emit("location", {"lat": 1, "lon": 2})
    assert client.get_received()[0]["args"] == {"message_body": "local result"}
    assert tracker.events == ["lease_enter", "geocoder", "provider_done", "message_emit", "lease_release"]
    assert authority.HTTP_REQUEST_LEASE_ENVIRON not in sockets.socketio.server.environ[client.eio_sid]


@pytest.mark.parametrize("phase", ["provider", "emit", "request_setup"])
def test_errors_release_lease_restore_environment_and_preserve_original_error(runtime, monkeypatch, phase):
    _, client, tracker = runtime

    def fail():
        raise ValueError("original_socket_failure")

    location_provider(runtime, monkeypatch, fail if phase == "provider" else lambda: None)
    if phase == "emit":
        monkeypatch.setattr(sockets.socketio, "emit", lambda *_args, **_kwargs: fail())
    if phase == "request_setup":
        monkeypatch.setattr(sockets, "request", SimpleNamespace(_get_current_object=fail))
    with pytest.raises(ValueError, match="original_socket_failure"):
        client.emit("location", {"lat": 1, "lon": 2})
    assert tracker.events[0] == "lease_enter" and tracker.events[-1] == "lease_release"
    assert tracker.active is None
    assert authority.HTTP_REQUEST_LEASE_ENVIRON not in sockets.socketio.server.environ[client.eio_sid]


def test_denial_emit_failure_also_releases_lease(runtime, monkeypatch):
    _, client, tracker = runtime
    tracker.allowed = False
    monkeypatch.setattr(sockets, "emit", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("denial_emit_failure")))
    with pytest.raises(ValueError, match="denial_emit_failure"):
        client.emit("location", {})
    assert tracker.events == ["lease_enter", "lease_release"]


def test_two_real_events_same_sid_have_independent_thread_leases_and_nested_reuse(runtime, monkeypatch):
    _, client, tracker = runtime
    barrier = Barrier(2)
    seen = []

    def parallel_effect():
        seen.append(tracker.active)
        barrier.wait(timeout=5)
        with authority.global_writer_authority_lease(current_app.config) as nested:
            assert nested is tracker.active
        assert authority.HTTP_REQUEST_LEASE_ENVIRON not in sockets.socketio.server.environ[client.eio_sid]

    location_provider(runtime, monkeypatch, parallel_effect)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(client.emit, "location", {"lat": value, "lon": 2}) for value in (1, 3)]
        for future in futures:
            future.result(timeout=10)
    assert len(seen) == 2 and seen[0] is not seen[1]
    assert tracker.events.count("lease_enter") == tracker.events.count("lease_release") == 2
    assert len(client.get_received()) == 2
    assert authority.HTTP_REQUEST_LEASE_ENVIRON not in sockets.socketio.server.environ[client.eio_sid]


def test_provider_in_progress_keeps_scope_until_emit_and_then_releases(runtime, monkeypatch):
    _, client, tracker = runtime
    entered, release = Event(), Event()

    def blocked_provider():
        entered.set()
        assert release.wait(timeout=5)
        tracker.effect("provider_done")

    location_provider(runtime, monkeypatch, blocked_provider)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.emit, "location", {"lat": 1, "lon": 2})
        assert entered.wait(timeout=5)
        assert "lease_release" not in tracker.events
        release.set()
        future.result(timeout=10)
    assert tracker.events == ["lease_enter", "geocoder", "provider_done", "lease_release"]


def test_chat_message_holds_lease_from_identity_through_db_provider_commit_and_realtime(runtime, monkeypatch):
    _, client, tracker = runtime
    user = SimpleNamespace(id=7, municipio_id=7, tipo_chat="municipio")
    ticket = SimpleNamespace(tenant_id=9, municipio_id=7)
    tenant = SimpleNamespace(id=9)

    def identity(_token):
        tracker.effect("identity_query")
        return user

    def get(model, _id):
        tracker.effect("ticket_query" if model is sockets.MunicipioTicket else "tenant_query")
        return ticket if model is sockets.MunicipioTicket else tenant

    def comment(**kwargs):
        tracker.effect("comment_and_provider")
        assert kwargs["comentario_data"]["emit_socket"] is False
        with authority.global_writer_authority_lease(current_app.config) as nested:
            assert nested is tracker.active
        return SimpleNamespace(to_dict=lambda: {"id": 1, "comentario": "Respuesta"})

    monkeypatch.setattr(sockets, "_user_from_token", identity)
    monkeypatch.setattr(sockets, "_panel_socket_credential", lambda *_: True)
    monkeypatch.setattr(sockets, "_clerk_identity_rooms", lambda *_: [])
    monkeypatch.setattr(sockets, "_is_ticket_operator", lambda *_: True)
    monkeypatch.setattr(sockets, "_get_owner_user", lambda *_: user)
    monkeypatch.setattr(sockets, "_user_can_operate_tenant", lambda *_: True)
    monkeypatch.setattr(sockets, "employee_ticket_category_access_allows", lambda *_: True)
    monkeypatch.setattr(sockets, "db", SimpleNamespace(session=SimpleNamespace(get=get, commit=lambda: tracker.effect("commit"))))
    monkeypatch.setattr(sockets, "servicio_tickets", SimpleNamespace(crear_comentario=comment))
    monkeypatch.setattr(sockets, "emit_new_chat_message", lambda *_: tracker.effect("realtime_emit"))
    monkeypatch.setattr(authority, "global_writer_authority_connection", lambda *_args, **_kwargs: pytest.fail("nested connection"))
    client.emit("send_chat_message", {"token": "local", "room": "ticket_municipio_3", "ticket_id": 3,
                                      "ticket_type": "municipio", "message": "Respuesta"})
    assert tracker.events == ["lease_enter", "identity_query", "ticket_query", "tenant_query",
                              "comment_and_provider", "commit", "realtime_emit", "lease_release"]


@pytest.mark.parametrize("static_fence", [False, True])
def test_gwa_disabled_preserves_legacy_dispatch_but_static_fence_still_wins(runtime, monkeypatch, static_fence):
    app, client, tracker = runtime
    app.config.update(CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED=False, CUTOVER_WRITER_FENCE_ENABLED=static_fence)
    monkeypatch.setattr(sockets, "global_writer_authority_lease", authority.global_writer_authority_lease)
    monkeypatch.setattr(authority, "global_writer_authority_connection", lambda *_args, **_kwargs: pytest.fail("legacy control connection"))
    monkeypatch.setattr(sockets, "_resolve_location_response_room", lambda _: request.sid)
    called = []
    monkeypatch.setattr(responder, "handle_location_update", lambda _: called.append("geocoder") or {"message_body": "legacy"})
    client.emit("location", {"lat": 1, "lon": 2})
    received = client.get_received()[0]
    assert called == ([] if static_fence else ["geocoder"])
    assert received["name"] == ("location_error" if static_fence else "message")
    assert tracker.events == []


@pytest.mark.parametrize("provider_error", [False, True])
def test_welcome_real_sqlite_first_query_commit_provider_audio_emit_all_within_own_scope(runtime, monkeypatch, provider_error):
    app, _, tracker = runtime
    db.init_app(app)
    with app.app_context():
        db.create_all()
        rubro = Rubro(clave="offline-municipio", nombre="Municipio")
        unrelated = User(name="Unrelated first owner", email="unrelated@example.invalid", password_hash="offline", rol="admin", tipo_chat="municipio", rubro=rubro)
        owner = User(name="Selected owner", email="owner@example.invalid", password_hash="offline", rol="admin", tipo_chat="municipio", rubro=rubro)
        db.session.add_all([rubro, unrelated, owner])
        db.session.flush()
        owner_id = owner.id
        assert unrelated.id != owner_id
        db.session.add(TenantProfile(slug="offline-municipio", nombre="Selected municipio", tipo="municipio", municipio_id=owner_id, is_active=True))
        db.session.commit()
        engine = db.engine

    def sql_boundary(_conn, _cursor, statement, _parameters, _context, _executemany):
        tracker.effect("sql_query" if statement.lstrip().upper().startswith("SELECT") else "sql_write")

    def response(**kwargs):
        tracker.effect("llm_provider")
        assert not has_request_context()
        assert kwargs["chat_db_context"].chat_session_id
        assert kwargs["owner_user"].id == owner_id
        assert kwargs["chat_db_context"].user_id == owner_id
        if provider_error:
            raise ValueError("welcome_provider_failure")
        return {"message_body": "Bienvenido", "generar_audio": True}

    def audio(**kwargs):
        tracker.effect("tts_storage_provider")
        return "https://audio.example.invalid/offline"

    monkeypatch.setattr(responder, "responder_municipio", response)
    monkeypatch.setattr(sockets, "generar_audio", audio)
    monkeypatch.setattr(sockets.socketio, "emit", lambda *_args, **_kwargs: tracker.effect("welcome_emit"))
    event.listen(engine, "before_cursor_execute", sql_boundary)
    try:
        if provider_error:
            with pytest.raises(ValueError, match="welcome_provider_failure"):
                sockets.send_welcome_message(app, "offline-sid", {"channel": "web", "tenant_slug": "offline-municipio"})
        else:
            sockets.send_welcome_message(app, "offline-sid", {"channel": "web", "tenant_slug": "offline-municipio", "tenantSlug": "offline-municipio"})
    finally:
        event.remove(engine, "before_cursor_execute", sql_boundary)
    assert tracker.events[0] == "lease_enter" and tracker.events[1] == "sql_query"
    assert "sql_write" in tracker.events and "llm_provider" in tracker.events
    assert tracker.events[-1] == "lease_release"
    assert ("tts_storage_provider" in tracker.events) is (not provider_error)
    assert ("welcome_emit" in tracker.events) is (not provider_error)
    with app.app_context():
        assert ChatSessionContext.query.count() == 1
        assert ChatSessionContext.query.first().user_id == owner_id
        db.session.remove()
        db.drop_all()


@pytest.mark.parametrize("static_fence", [False, True])
def test_survey_web_connection_without_tenant_is_read_only_and_schedules_nothing(runtime, monkeypatch, static_fence):
    app, _, tracker = runtime
    tracker.allowed = False
    app.config["CUTOVER_WRITER_FENCE_ENABLED"] = static_fence
    monkeypatch.setattr(sockets.socketio, "start_background_task", lambda *_: pytest.fail("survey welcome scheduled"))
    monkeypatch.setattr(sockets, "_user_from_token", lambda *_: pytest.fail("survey queried customer"))
    survey_client = sockets.socketio.test_client(app, auth={"channel": "web"})
    assert survey_client.is_connected()
    assert tracker.events == []
    survey_client.disconnect()


@pytest.mark.parametrize("auth", [
    {}, {"tenant_slug": "iframe"}, {"tenantSlug": "widget"},
    {"tenant_slug": "embed"}, {"tenant_slug": "https://a.example.invalid"},
    {"tenant_slug": ["tenant-a"]}, {"tenant_slug": "tenant-a", "tenantSlug": "tenant-b"},
])
def test_welcome_missing_reserved_invalid_and_conflicting_identity_refuses_before_any_customer_query(runtime, monkeypatch, auth):
    app, _, tracker = runtime
    import services.tenant_resolver as resolver
    monkeypatch.setattr(resolver, "_tenant_by_slug", lambda *_: pytest.fail("invalid identity queried tenants"))
    monkeypatch.setattr(responder, "responder_municipio", lambda **_: pytest.fail("invalid identity reached provider"))
    monkeypatch.setattr(sockets, "generar_audio", lambda **_: pytest.fail("invalid identity reached audio/storage"))
    monkeypatch.setattr(sockets.socketio, "emit", lambda *_args, **_kwargs: pytest.fail("invalid identity emitted welcome"))
    assert sockets.send_welcome_message(app, "invalid-welcome", {"channel": "web", **auth}) is False
    assert tracker.events == ["lease_enter", "lease_release"]
    # The admitted entrypoint also rejects future direct callers by itself.
    with app.app_context():
        assert sockets._send_admitted_welcome_message(app, "invalid-welcome", auth) is False


@pytest.mark.parametrize("case", ["unknown", "inactive", "private_owner_role", "wrong_owner_type", "wrong_tenant_type", "missing_rubro", "alias"])
def test_welcome_unavailable_identity_never_borrows_an_admin_or_creates_context_or_calls_provider(runtime, monkeypatch, case):
    app, _, tracker = runtime
    db.init_app(app)
    with app.app_context():
        db.create_all()
        rubro = Rubro(clave="bound-municipio", nombre="Bound municipio")
        unrelated = User(name="First unrelated admin", email="first@example.invalid", password_hash="offline", rol="admin", tipo_chat="municipio", rubro=rubro)
        owner = User(name="Bound owner", email="bound@example.invalid", password_hash="offline", rol="super_admin" if case == "private_owner_role" else "admin", tipo_chat="pyme" if case == "wrong_owner_type" else "municipio", rubro=None if case == "missing_rubro" else rubro)
        db.session.add_all([rubro, unrelated, owner])
        db.session.flush()
        db.session.add(TenantProfile(slug="bound-municipio", nombre="Bound municipio", tipo="pyme" if case == "wrong_tenant_type" else "municipio", municipio_id=owner.id, is_active=case != "inactive"))
        db.session.commit()
    monkeypatch.setattr(responder, "responder_municipio", lambda **_: pytest.fail("unavailable tenant reached provider"))
    monkeypatch.setattr(sockets, "generar_audio", lambda **_: pytest.fail("unavailable tenant reached audio/storage"))
    monkeypatch.setattr(sockets.socketio, "emit", lambda *_args, **_kwargs: pytest.fail("unavailable tenant emitted welcome"))
    slug = "unknown-municipio" if case == "unknown" else "bound-municipio.chatboc.ar" if case == "alias" else "bound-municipio"
    assert sockets.send_welcome_message(app, "unavailable-welcome", {"channel": "web", "tenant_slug": slug}) is False
    assert tracker.events == ["lease_enter", "lease_release"]
    with app.app_context():
        assert ChatSessionContext.query.count() == 0
        db.session.remove()
        db.drop_all()


def test_conflicting_web_connection_is_rejected_before_lease_or_scheduling(runtime, monkeypatch):
    app, _, tracker = runtime
    monkeypatch.setattr(sockets.socketio, "start_background_task", lambda *_: pytest.fail("conflicting welcome scheduled"))
    denied = sockets.socketio.test_client(app, auth={"channel": "web", "tenant_slug": "tenant-a", "tenantSlug": "tenant-b"})
    assert not denied.is_connected()
    assert tracker.events == []
