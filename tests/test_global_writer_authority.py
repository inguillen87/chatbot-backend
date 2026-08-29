from __future__ import annotations

import json

import pytest
from flask import Flask

import global_writer_authority as config_gate
import middleware.cutover_writer_fence as http_gate
import scripts.manage_global_writer_authority as cli
import services.global_writer_authority as authority
import routes.internal_cron as internal_cron
from cutover_writer_fence import cutover_writer_view


class _Result:
    def __init__(self, *, rowcount: int = 1):
        self.rowcount = rowcount


class _RecordingExecutor:
    def __init__(self, *, rowcount: int = 1):
        self.rowcount = rowcount
        self.calls = []

    def execute(self, statement, parameters=None):
        self.calls.append((str(statement), dict(parameters or {})))
        return _Result(rowcount=self.rowcount)


def _state(
    *,
    owner="render",
    epoch=7,
    render_fenced=False,
    vercel_fenced=True,
):
    return authority.GlobalWriterAuthorityState(
        owner_runtime=owner,
        epoch=epoch,
        render_fenced=render_fenced,
        vercel_fenced=vercel_fenced,
    )


def test_global_authority_is_disabled_by_default_and_malformed_opt_in_fails_closed(
    monkeypatch,
):
    monkeypatch.delenv(config_gate.GLOBAL_WRITER_AUTHORITY_FLAG, raising=False)
    assert config_gate.global_writer_authority_enabled() is False
    assert config_gate.global_writer_authority_enabled({}) is False
    assert (
        config_gate.global_writer_authority_enabled(
            {config_gate.GLOBAL_WRITER_AUTHORITY_FLAG: "typo"}
        )
        is True
    )


def test_enabled_authority_requires_declarative_runtime_before_database_access():
    class _ExplosiveExecutor:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("database must not be reached")

    decision = authority.evaluate_global_writer_authority(
        {
            config_gate.GLOBAL_WRITER_AUTHORITY_FLAG: True,
            "CUTOVER_WRITER_FENCE_ENABLED": False,
        },
        executor=_ExplosiveExecutor(),
    )
    assert decision.allowed is False
    assert decision.reason_code == "global_writer_runtime_identity_invalid"


@pytest.mark.parametrize(
    ("runtime", "state", "allowed", "reason_code"),
    [
        ("render", _state(), True, "runtime_is_global_writer_owner"),
        (
            "vercel",
            _state(),
            False,
            "runtime_not_global_writer_owner",
        ),
        (
            "render",
            _state(render_fenced=True),
            False,
            "runtime_globally_fenced",
        ),
    ],
)
def test_authority_requires_owner_and_persistent_runtime_unfenced(
    monkeypatch,
    runtime,
    state,
    allowed,
    reason_code,
):
    monkeypatch.setattr(
        authority,
        "load_global_writer_authority_state",
        lambda _executor=None: state,
    )
    decision = authority.evaluate_global_writer_authority(
        {
            config_gate.GLOBAL_WRITER_AUTHORITY_FLAG: True,
            config_gate.GLOBAL_WRITER_RUNTIME_IDENTITY: runtime,
            "CUTOVER_WRITER_FENCE_ENABLED": False,
        },
        executor=object(),
    )
    assert decision.allowed is allowed
    assert decision.reason_code == reason_code
    assert decision.epoch == state.epoch


def test_authority_database_failure_is_fail_closed_without_exception_details(monkeypatch):
    def _fail(_executor=None):
        raise authority.GlobalWriterAuthorityTransitionError(
            "global_writer_authority_database_unavailable"
        )

    monkeypatch.setattr(authority, "load_global_writer_authority_state", _fail)
    decision = authority.evaluate_global_writer_authority(
        {
            config_gate.GLOBAL_WRITER_AUTHORITY_FLAG: True,
            config_gate.GLOBAL_WRITER_RUNTIME_IDENTITY: "render",
            "CUTOVER_WRITER_FENCE_ENABLED": False,
        },
        executor=object(),
    )
    assert decision.allowed is False
    assert decision.reason_code == "global_writer_authority_database_unavailable"


def test_enabled_gate_never_falls_back_to_the_runtime_application_database():
    decision = authority.evaluate_global_writer_authority(
        {
            config_gate.GLOBAL_WRITER_AUTHORITY_FLAG: True,
            config_gate.GLOBAL_WRITER_RUNTIME_IDENTITY: "render",
            "CUTOVER_WRITER_FENCE_ENABLED": False,
            "SQLALCHEMY_DATABASE_URI": "postgresql://application-db.invalid/app",
            "DATABASE_URL": "postgresql://application-db.invalid/app",
        }
    )
    assert decision.allowed is False
    assert (
        decision.reason_code
        == "global_writer_authority_control_database_url_missing"
    )


@pytest.mark.parametrize(
    ("database_url", "reason_code"),
    [
        (
            "sqlite:///authority.db",
            "global_writer_authority_requires_postgresql",
        ),
        (
            "postgresql://operator:secret@control.invalid/authority",
            "global_writer_authority_control_database_tls_required",
        ),
        (
            "postgresql://operator:secret@control.invalid/authority"
            "?sslmode=require&options=-c%20statement_timeout%3D0",
            "global_writer_authority_control_database_url_invalid",
        ),
    ],
)
def test_enabled_gate_rejects_unsafe_control_database_urls(
    database_url,
    reason_code,
):
    decision = authority.evaluate_global_writer_authority(
        {
            config_gate.GLOBAL_WRITER_AUTHORITY_FLAG: True,
            config_gate.GLOBAL_WRITER_RUNTIME_IDENTITY: "render",
            config_gate.GLOBAL_WRITER_AUTHORITY_DATABASE_URL: database_url,
            "CUTOVER_WRITER_FENCE_ENABLED": False,
        }
    )
    assert decision.allowed is False
    assert decision.reason_code == reason_code


def test_control_engine_bounds_connect_pool_statement_lock_and_idle_waits(monkeypatch):
    captured = {}

    class _Engine:
        def dispose(self):
            return None

    def _create_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _Engine()

    monkeypatch.setattr(authority, "_CONTROL_ENGINE", None)
    monkeypatch.setattr(authority, "_CONTROL_ENGINE_FINGERPRINT", None)
    monkeypatch.setattr(authority, "create_engine", _create_engine)
    authority._control_database_engine(
        {
            config_gate.GLOBAL_WRITER_AUTHORITY_DATABASE_URL: (
                "postgresql://operator:secret@control.invalid/authority"
                "?sslmode=require"
            )
        }
    )

    kwargs = captured["kwargs"]
    assert kwargs["pool_timeout"] == authority.CONTROL_POOL_TIMEOUT_SECONDS
    assert kwargs["connect_args"]["connect_timeout"] == (
        authority.CONTROL_CONNECT_TIMEOUT_SECONDS
    )
    options = kwargs["connect_args"]["options"]
    assert (
        f"statement_timeout={authority.CONTROL_STATEMENT_TIMEOUT_MS}ms"
        in options
    )
    assert f"lock_timeout={authority.CONTROL_LOCK_TIMEOUT_MS}ms" in options
    assert (
        "idle_in_transaction_session_timeout="
        f"{authority.CONTROL_IDLE_TRANSACTION_TIMEOUT_MS}ms"
    ) in options


def test_control_database_outage_is_redacted_and_fail_closed(monkeypatch):
    secret = "postgresql://operator:do-not-print@control.invalid/authority"

    class _Engine:
        def connect(self):
            raise RuntimeError(secret)

    monkeypatch.setattr(
        authority,
        "_control_database_engine",
        lambda _config: _Engine(),
    )
    decision = authority.evaluate_global_writer_authority(
        {
            config_gate.GLOBAL_WRITER_AUTHORITY_FLAG: True,
            config_gate.GLOBAL_WRITER_RUNTIME_IDENTITY: "vercel",
            config_gate.GLOBAL_WRITER_AUTHORITY_DATABASE_URL: (
                f"{secret}?sslmode=require"
            ),
            "CUTOVER_WRITER_FENCE_ENABLED": False,
        }
    )
    assert decision.allowed is False
    assert (
        decision.reason_code
        == "global_writer_authority_control_database_unavailable"
    )
    assert "do-not-print" not in repr(decision)


def test_http_gate_blocks_unsafe_request_before_handler_and_keeps_read_only_get(
    monkeypatch,
):
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        CUTOVER_WRITER_FENCE_ENABLED=False,
        CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED=True,
        CUTOVER_RUNTIME_IDENTITY="vercel",
    )
    calls = []

    @app.post("/write")
    def write():
        calls.append("write")
        return {"ok": True}

    @app.get("/read")
    def read():
        calls.append("read")
        return {"ok": True}

    monkeypatch.setattr(
        http_gate,
        "evaluate_global_writer_authority",
        lambda _config: authority.GlobalWriterAuthorityDecision(
            allowed=False,
            enabled=True,
            reason_code="runtime_not_global_writer_owner",
            epoch=4,
        ),
    )
    http_gate.register_cutover_writer_fence(app)
    client = app.test_client()

    blocked = client.post("/write")
    assert blocked.status_code == 503
    assert blocked.get_json() == {
        "contract_version": "cutover.global_writer_authority.v1",
        "status": "maintenance",
        "reason_code": "runtime_not_global_writer_owner",
        "retryable": True,
    }
    assert blocked.headers["Cache-Control"] == "no-store"
    assert blocked.headers["Retry-After"] == "60"
    assert calls == []

    assert client.get("/read").status_code == 200
    assert calls == ["read"]


def test_http_gate_applies_to_explicit_mutating_get_view(monkeypatch):
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        CUTOVER_WRITER_FENCE_ENABLED=False,
        CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED=True,
        CUTOVER_RUNTIME_IDENTITY="render",
    )
    calls = []

    @app.get("/writer-get")
    @cutover_writer_view
    def writer_get():
        calls.append("handler")
        return {"ok": True}

    monkeypatch.setattr(
        http_gate,
        "evaluate_global_writer_authority",
        lambda _config: authority.GlobalWriterAuthorityDecision(
            allowed=False,
            enabled=True,
            reason_code="runtime_globally_fenced",
            epoch=9,
        ),
    )
    http_gate.register_cutover_writer_fence(app)
    response = app.test_client().get("/writer-get")
    assert response.status_code == 503
    assert response.get_json()["reason_code"] == "runtime_globally_fenced"
    assert calls == []


def test_internal_mutating_get_cron_checks_global_authority_before_auth(monkeypatch):
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        CUTOVER_WRITER_FENCE_ENABLED=False,
        CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED=True,
        CUTOVER_RUNTIME_IDENTITY="vercel",
        CRON_SECRET="x" * 40,
    )
    monkeypatch.setattr(
        internal_cron,
        "background_global_writer_authority_report",
        lambda *_args, **_kwargs: {
            "contract_version": "cutover.global_writer_authority.v1",
            "component": "internal_cron",
            "executed": False,
            "reason_code": "runtime_not_global_writer_owner",
            "status": "fenced",
        },
    )
    app.register_blueprint(internal_cron.internal_cron_bp)
    response = app.test_client().get(
        "/api/internal/cron/outbox-reconciliation"
    )
    assert response.status_code == 503
    assert response.get_json()["reason_code"] == "runtime_not_global_writer_owner"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Retry-After"] == "60"


def test_transfer_cas_requires_both_runtimes_fenced_and_expected_epoch(monkeypatch):
    executor = _RecordingExecutor()
    monkeypatch.setattr(
        authority,
        "load_global_writer_authority_state",
        lambda _executor=None: _state(
            owner="vercel",
            epoch=12,
            render_fenced=True,
            vercel_fenced=True,
        ),
    )
    state = authority.transfer_global_writer_owner(
        executor,
        from_runtime="render",
        to_runtime="vercel",
        expected_epoch=11,
    )
    sql, parameters = executor.calls[0]
    assert "epoch = :expected_epoch" in sql
    assert "render_fenced IS TRUE" in sql
    assert "vercel_fenced IS TRUE" in sql
    assert "owner_runtime = :from_runtime" in sql
    assert parameters == {
        "authority_key": "primary",
        "expected_epoch": 11,
        "from_runtime": "render",
        "to_runtime": "vercel",
    }
    assert state.owner_runtime == "vercel"
    assert state.epoch == 12


def test_transition_rejects_stale_epoch_without_fallback_read(monkeypatch):
    executor = _RecordingExecutor(rowcount=0)
    monkeypatch.setattr(
        authority,
        "load_global_writer_authority_state",
        lambda _executor=None: (_ for _ in ()).throw(
            AssertionError("postcheck must not run")
        ),
    )
    with pytest.raises(
        authority.GlobalWriterAuthorityTransitionError,
        match="global_writer_authority_cas_rejected",
    ):
        authority.attest_runtime_fenced(
            executor,
            runtime="render",
            expected_epoch=3,
        )


def test_cli_mutations_require_both_opt_in_and_local_fence():
    executor = _RecordingExecutor()
    with pytest.raises(
        authority.GlobalWriterAuthorityTransitionError,
        match="global_writer_authority_not_enabled",
    ):
        cli.execute_authority_command(
            executor,
            command="attest-fenced",
            environ={
                config_gate.GLOBAL_WRITER_RUNTIME_IDENTITY: "render",
                "CUTOVER_WRITER_FENCE_ENABLED": "true",
            },
            expected_epoch=1,
        )
    with pytest.raises(
        authority.GlobalWriterAuthorityTransitionError,
        match="local_writer_fence_required_for_transition",
    ):
        cli.execute_authority_command(
            executor,
            command="attest-fenced",
            environ={
                config_gate.GLOBAL_WRITER_AUTHORITY_FLAG: "true",
                config_gate.GLOBAL_WRITER_RUNTIME_IDENTITY: "render",
                "CUTOVER_WRITER_FENCE_ENABLED": "false",
            },
            expected_epoch=1,
        )


def test_cli_never_echoes_database_url_or_exception_message(monkeypatch, capsys):
    secret_url = (
        "postgresql://operator:do-not-print@example.invalid/control"
        "?sslmode=require"
    )
    monkeypatch.setattr(cli, "create_engine", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(secret_url)))
    exit_code = cli.main(
        [
            "--environment-variable",
            config_gate.GLOBAL_WRITER_AUTHORITY_DATABASE_URL,
            "status",
        ],
        environ={
            config_gate.GLOBAL_WRITER_AUTHORITY_DATABASE_URL: secret_url,
        },
    )
    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    assert exit_code == 3
    assert payload["reason_code"] == "global_writer_authority_runtime_failed"
    assert secret_url not in payload_text
    assert "do-not-print" not in payload_text
