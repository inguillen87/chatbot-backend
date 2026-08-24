from __future__ import annotations

import threading
from types import SimpleNamespace

from flask import Flask
from sqlalchemy import create_engine

import routes.health as health_routes
from config import build_database_engine_options
from services.runtime_readiness import (
    RuntimeReadinessCache,
    evaluate_runtime_readiness,
)


class _HealthyRedis:
    def __init__(self):
        self.closed = False

    def ping(self):
        return True

    def close(self):
        self.closed = True


def _sqlite_engine():
    return create_engine("sqlite:///:memory:", future=True)


def _postgres_engine():
    class Result:
        def scalar_one(self):
            return 1

    class Connection:
        dialect = SimpleNamespace(name="postgresql")

        def __init__(self):
            self.driver_statements = []
            self.statements = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def exec_driver_sql(self, statement):
            self.driver_statements.append(statement)

        def execute(self, statement):
            self.statements.append(statement)
            return Result()

    connection = Connection()
    return SimpleNamespace(connect=lambda: connection), connection


def test_database_and_redis_are_ready_with_short_timeouts():
    redis_client = _HealthyRedis()
    factory_calls = []
    engine, _connection = _postgres_engine()

    def redis_factory(uri, **kwargs):
        factory_calls.append((uri, kwargs))
        return redis_client

    payload = evaluate_runtime_readiness(
        engine=engine,
        redis_uri="rediss://:secret@redis.internal:6380/0",
        production_like=True,
        database_timeout_seconds=0.75,
        redis_timeout_seconds=0.5,
        redis_factory=redis_factory,
    )

    assert payload == {
        "contract_version": "runtime.readiness.v1",
        "ready": True,
        "status": "ready",
        "components": {
            "database": {"status": "ok", "required": True},
            "redis": {"status": "ok", "required": True},
        },
    }
    assert factory_calls == [
        (
            "rediss://:secret@redis.internal:6380/0",
            {
                "socket_connect_timeout": 0.5,
                "socket_timeout": 0.5,
                "retry_on_timeout": False,
                "health_check_interval": 0,
            },
        )
    ]
    assert redis_client.closed is True


def test_postgres_database_probe_sets_local_statement_timeout():
    engine, connection = _postgres_engine()

    payload = evaluate_runtime_readiness(
        engine=engine,
        redis_uri="memory://",
        production_like=False,
        database_timeout_seconds=0.75,
    )

    assert payload["ready"] is True
    assert connection.driver_statements == [
        "SET LOCAL statement_timeout = 750"
    ]
    assert str(connection.statements[0]) == "SELECT 1"
    assert connection.statements[0].get_execution_options()["timeout"] == 0.75


def test_database_failure_is_fail_closed_without_exception_detail(caplog):
    class BrokenEngine:
        def connect(self):
            raise ConnectionError("postgresql://user:password@private-host/db")

    redis_factory_calls = []

    def redis_factory(*_args, **_kwargs):
        redis_factory_calls.append(True)
        return _HealthyRedis()

    payload = evaluate_runtime_readiness(
        engine=BrokenEngine(),
        redis_uri="redis://redis.internal:6379/0",
        production_like=True,
        redis_factory=redis_factory,
    )

    assert payload["ready"] is False
    assert payload["status"] == "not_ready"
    assert payload["components"]["database"] == {
        "status": "error",
        "required": True,
    }
    assert payload["components"]["redis"] == {
        "status": "not_checked",
        "required": True,
    }
    assert redis_factory_calls == []
    assert "password" not in repr(payload)
    assert "private-host" not in repr(payload)
    assert "password" not in caplog.text
    assert "private-host" not in caplog.text


def test_production_rejects_sqlite_even_when_select_one_succeeds():
    redis_factory_calls = []
    payload = evaluate_runtime_readiness(
        engine=_sqlite_engine(),
        redis_uri="redis://redis.internal:6379/0",
        production_like=True,
        redis_factory=lambda *_args, **_kwargs: redis_factory_calls.append(True),
    )

    assert payload["ready"] is False
    assert payload["components"]["database"] == {
        "status": "invalid_configuration",
        "required": True,
    }
    assert payload["components"]["redis"]["status"] == "not_checked"
    assert redis_factory_calls == []


def test_redis_failure_is_fail_closed_and_closes_client(caplog):
    class BrokenRedis(_HealthyRedis):
        def ping(self):
            raise ConnectionError("rediss://:password@private-redis:6380/0")

    redis_client = BrokenRedis()
    payload = evaluate_runtime_readiness(
        engine=_sqlite_engine(),
        redis_uri="rediss://:secret@redis.internal:6380/0",
        production_like=False,
        redis_factory=lambda *_args, **_kwargs: redis_client,
    )

    assert payload["ready"] is False
    assert payload["components"]["database"]["status"] == "ok"
    assert payload["components"]["redis"] == {
        "status": "error",
        "required": True,
    }
    assert redis_client.closed is True
    assert "password" not in repr(payload)
    assert "private-redis" not in repr(payload)
    assert "password" not in caplog.text
    assert "private-redis" not in caplog.text


def test_production_without_shared_redis_is_not_ready():
    payload = evaluate_runtime_readiness(
        engine=_sqlite_engine(),
        redis_uri="memory://",
        production_like=True,
    )

    assert payload["ready"] is False
    assert payload["components"]["redis"] == {
        "status": "not_configured",
        "required": True,
    }


def test_local_without_shared_redis_keeps_database_only_readiness():
    payload = evaluate_runtime_readiness(
        engine=_sqlite_engine(),
        redis_uri="memory://",
        production_like=False,
    )

    assert payload["ready"] is True
    assert payload["components"]["redis"] == {
        "status": "not_configured",
        "required": False,
    }


def test_network_database_engine_options_bound_connect_and_pool_waits():
    options = build_database_engine_options(
        "postgresql+psycopg://db.internal/app",
        connect_timeout_seconds="0.01",
        pool_timeout_seconds="999",
    )

    assert options["connect_args"] == {"connect_timeout": 1}
    assert options["pool_timeout"] == 10.0
    assert options["pool_pre_ping"] is True
    assert options["pool_size"] == 10
    assert options["max_overflow"] == 20


def test_sqlite_engine_options_do_not_receive_network_pool_arguments():
    options = build_database_engine_options(
        "sqlite:///:memory:",
        connect_timeout_seconds="invalid",
        pool_timeout_seconds=float("inf"),
    )

    assert options == {"connect_args": {"timeout": 5}}


def test_readiness_cache_ttl_returns_deep_copy_without_request_id():
    now = [100.0]
    cache = RuntimeReadinessCache(clock=lambda: now[0])
    probe_calls = []

    def probe():
        probe_calls.append(True)
        return {
            "contract_version": "runtime.readiness.v1",
            "ready": True,
            "status": "ready",
            "request_id": "must-not-be-cached",
            "components": {
                "database": {"status": "ok", "required": True},
                "redis": {"status": "ok", "required": True},
            },
        }

    first = cache.get_or_probe(
        key=("same-runtime",),
        probe=probe,
        ttl_seconds=1.0,
        wait_timeout_seconds=1.0,
    )
    first["components"]["database"]["status"] = "mutated"
    second = cache.get_or_probe(
        key=("same-runtime",),
        probe=probe,
        ttl_seconds=1.0,
        wait_timeout_seconds=1.0,
    )

    assert probe_calls == [True]
    assert "request_id" not in first
    assert "request_id" not in second
    assert second["components"]["database"]["status"] == "ok"

    now[0] += 1.01
    cache.get_or_probe(
        key=("same-runtime",),
        probe=probe,
        ttl_seconds=1.0,
        wait_timeout_seconds=1.0,
    )
    assert probe_calls == [True, True]


def test_readiness_cache_coalesces_concurrent_probe():
    cache = RuntimeReadinessCache()
    probe_started = threading.Event()
    release_probe = threading.Event()
    follower_entered = threading.Event()
    follower_done = threading.Event()
    probe_calls = []
    results = []
    errors = []

    def probe():
        probe_calls.append(True)
        probe_started.set()
        assert release_probe.wait(timeout=2)
        return {
            "contract_version": "runtime.readiness.v1",
            "ready": True,
            "status": "ready",
            "components": {},
        }

    def leader():
        try:
            results.append(
                cache.get_or_probe(
                    key=("shared",),
                    probe=probe,
                    ttl_seconds=1.0,
                    wait_timeout_seconds=1.0,
                )
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def follower():
        follower_entered.set()
        try:
            results.append(
                cache.get_or_probe(
                    key=("shared",),
                    probe=probe,
                    ttl_seconds=1.0,
                    wait_timeout_seconds=1.0,
                )
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            follower_done.set()

    leader_thread = threading.Thread(target=leader)
    follower_thread = threading.Thread(target=follower)
    leader_thread.start()
    assert probe_started.wait(timeout=1)
    follower_thread.start()
    assert follower_entered.wait(timeout=1)
    assert follower_done.wait(timeout=0.05) is False
    release_probe.set()
    leader_thread.join(timeout=2)
    follower_thread.join(timeout=2)

    assert errors == []
    assert probe_calls == [True]
    assert len(results) == 2
    assert results[0] == results[1]
    assert results[0] is not results[1]


def test_readiness_cache_follower_timeout_fails_closed_without_second_probe():
    cache = RuntimeReadinessCache()
    probe_started = threading.Event()
    release_probe = threading.Event()
    leader_done = threading.Event()
    probe_calls = []

    def probe():
        probe_calls.append(True)
        probe_started.set()
        assert release_probe.wait(timeout=2)
        return {
            "contract_version": "runtime.readiness.v1",
            "ready": True,
            "status": "ready",
            "components": {},
        }

    def leader():
        try:
            cache.get_or_probe(
                key=("slow-shared",),
                probe=probe,
                ttl_seconds=1.0,
                wait_timeout_seconds=1.0,
            )
        finally:
            leader_done.set()

    leader_thread = threading.Thread(target=leader)
    leader_thread.start()
    assert probe_started.wait(timeout=1)
    try:
        follower = cache.get_or_probe(
            key=("slow-shared",),
            probe=probe,
            ttl_seconds=1.0,
            wait_timeout_seconds=0.1,
        )
    finally:
        release_probe.set()
        leader_thread.join(timeout=2)

    assert leader_done.is_set()
    assert probe_calls == [True]
    assert follower["ready"] is False
    assert follower["components"]["database"]["status"] == "probe_timeout"


def _route_app(monkeypatch, *, environment: str, engine):
    app = Flask(__name__)
    app.config.update(
        ENV=environment,
        RATELIMIT_STORAGE_URI="redis://redis.internal:6379/0",
        READINESS_DATABASE_TIMEOUT_SECONDS=0.5,
        READINESS_REDIS_TIMEOUT_SECONDS=0.25,
        READINESS_CACHE_TTL_SECONDS=1.0,
    )
    monkeypatch.setattr(health_routes, "db", SimpleNamespace(engine=engine))
    app.register_blueprint(health_routes.runtime_readiness_bp)
    return app


def test_readiness_route_returns_200_and_correlated_request_id(monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    redis_calls = []

    def redis_factory(*_args, **_kwargs):
        redis_calls.append(True)
        return _HealthyRedis()

    monkeypatch.setattr(
        "services.runtime_readiness.Redis.from_url",
        redis_factory,
    )
    app = _route_app(monkeypatch, environment="testing", engine=_sqlite_engine())

    response = app.test_client().get(
        "/health/ready",
        headers={"X-Request-Id": "readiness-ok-1"},
    )
    second = app.test_client().get(
        "/health/ready",
        headers={"X-Request-Id": "readiness-ok-2"},
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "ready"
    assert response.get_json()["request_id"] == "readiness-ok-1"
    assert response.headers["X-Request-Id"] == "readiness-ok-1"
    assert response.headers["Cache-Control"] == "no-store"
    assert second.status_code == 200
    assert second.get_json()["request_id"] == "readiness-ok-2"
    assert second.headers["X-Request-Id"] == "readiness-ok-2"
    assert redis_calls == [True]


def test_readiness_route_returns_sanitized_503(monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)

    class BrokenEngine:
        def connect(self):
            raise RuntimeError("postgresql://admin:secret@db.internal/app")

    monkeypatch.setattr(
        "services.runtime_readiness.Redis.from_url",
        lambda *_args, **_kwargs: _HealthyRedis(),
    )
    app = _route_app(monkeypatch, environment="production", engine=BrokenEngine())

    response = app.test_client().get("/health/ready")
    serialized = response.get_data(as_text=True)

    assert response.status_code == 503
    assert response.get_json()["status"] == "not_ready"
    assert response.get_json()["components"]["database"]["status"] == "error"
    assert response.get_json()["request_id"]
    assert response.headers["X-Request-Id"] == response.get_json()["request_id"]
    assert "secret" not in serialized
    assert "db.internal" not in serialized


def test_readiness_route_does_not_reflect_unsafe_request_id(monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    monkeypatch.setattr(
        "services.runtime_readiness.Redis.from_url",
        lambda *_args, **_kwargs: _HealthyRedis(),
    )
    app = _route_app(monkeypatch, environment="testing", engine=_sqlite_engine())

    response = app.test_client().get(
        "/health/ready",
        headers={"X-Request-Id": "rediss://user:secret@private-host/0"},
    )

    request_id = response.get_json()["request_id"]
    assert response.status_code == 200
    assert request_id != "rediss://user:secret@private-host/0"
    assert response.headers["X-Request-Id"] == request_id
    assert "secret" not in response.get_data(as_text=True)


def test_legacy_database_health_does_not_expose_exception_detail(monkeypatch, caplog):
    class BrokenSession:
        def execute(self, _statement):
            raise RuntimeError("postgresql://admin:secret@db.internal/app")

    app = Flask(__name__)
    monkeypatch.setattr(
        health_routes,
        "db",
        SimpleNamespace(session=BrokenSession()),
    )
    app.register_blueprint(health_routes.health_bp)

    response = app.test_client().get("/api/health/")
    serialized = response.get_data(as_text=True)

    assert response.status_code == 500
    assert response.get_json() == {"status": "degraded", "db": "error"}
    assert "secret" not in serialized
    assert "db.internal" not in serialized
    assert "secret" not in caplog.text
    assert "db.internal" not in caplog.text


def test_application_registers_liveness_and_readiness(client):
    liveness = client.get("/health")
    readiness = client.get("/health/ready")

    assert liveness.status_code == 200
    assert liveness.get_json() == {"status": "ok"}
    assert readiness.status_code == 200
    assert readiness.get_json()["components"]["database"]["status"] == "ok"
    assert readiness.get_json()["components"]["redis"] == {
        "required": False,
        "status": "not_configured",
    }
