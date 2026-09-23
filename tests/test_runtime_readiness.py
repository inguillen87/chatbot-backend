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


def _postgres_columns():
    return {
        "id": ["int4", True],
        "tenant_id": ["int4", True],
        "endpoint": ["varchar", True],
        "actor_scope_hash": ["varchar", True],
        "idempotency_key_hash": ["varchar", True],
        "request_hash": ["varchar", True],
        "status": ["varchar", True],
        "response_status": ["int4", False],
        "response_json": ["jsonb", False],
        "response_request_id": ["varchar", False],
        "contract_version": ["varchar", True],
        "created_at": ["timestamptz", True],
        "updated_at": ["timestamptz", True],
        "completed_at": ["timestamptz", False],
        "expired_at": ["timestamptz", False],
    }


def _postgres_engine(**schema_overrides):
    schema_state = {
        "relation_present": True,
        "table_kind_valid": True,
        "columns": _postgres_columns(),
        "unique_indexes": [
            ["id"],
            [
                "tenant_id",
                "endpoint",
                "actor_scope_hash",
                "idempotency_key_hash",
            ],
        ],
        "identity_sequence_present": True,
        "schema_usage_privilege": True,
        "select_privilege": True,
        "insert_privilege": True,
        "update_privilege": True,
        "delete_privilege": True,
        "identity_sequence_privilege": True,
    }
    schema_state.update(schema_overrides)

    class Result:
        def __init__(self, value=None, mapping=None):
            self.value = value
            self.mapping = mapping

        def scalar_one(self):
            return self.value

        def mappings(self):
            return self

        def one(self):
            return dict(self.mapping)

    class Connection:
        dialect = SimpleNamespace(name="postgresql")

        def __init__(self):
            self.driver_statements = []
            self.statements = []
            self.statement_parameters = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def exec_driver_sql(self, statement):
            self.driver_statements.append(statement)

        def execute(self, statement, parameters=None):
            self.statements.append(statement)
            self.statement_parameters.append(parameters)
            if "AS relation_present" in str(statement):
                return Result(mapping=schema_state)
            return Result(value=1)

    connection = Connection()
    return SimpleNamespace(connect=lambda: connection), connection


def test_database_and_redis_are_ready_with_short_timeouts():
    redis_client = _HealthyRedis()
    factory_calls = []
    engine, connection = _postgres_engine()

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
    assert len(connection.statements) == 1
    probe_sql = str(connection.statements[0])
    assert "pg_catalog.to_regclass" in probe_sql
    assert "FROM municipio_chat_idempotency_receipt" not in probe_sql
    assert "pg_catalog.pg_attribute" in probe_sql
    assert "pg_catalog.jsonb_object_agg" in probe_sql
    assert "pg_catalog.pg_index" in probe_sql
    for index_guard in (
        "index_definition.indisunique",
        "index_definition.indisvalid",
        "index_definition.indisready",
        "index_definition.indislive",
        "index_definition.indpred IS NULL",
        "index_definition.indexprs IS NULL",
    ):
        assert index_guard in probe_sql
    assert probe_sql.count("pg_catalog.has_table_privilege") == 4
    for table_privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert f"relation.oid, '{table_privilege}'" in probe_sql
    assert "pg_catalog.has_sequence_privilege" in probe_sql
    params = connection.statement_parameters[0]
    assert params == {
        "required_table": "municipio_chat_idempotency_receipt",
        "identity_column": "id",
    }


def test_missing_required_postgres_table_is_not_ready_and_skips_redis():
    engine, connection = _postgres_engine(relation_present=False)
    redis_factory_calls = []

    payload = evaluate_runtime_readiness(
        engine=engine,
        redis_uri="redis://redis.internal:6379/0",
        production_like=True,
        redis_factory=lambda *_args, **_kwargs: redis_factory_calls.append(True),
    )

    assert payload == {
        "contract_version": "runtime.readiness.v1",
        "ready": False,
        "status": "not_ready",
        "components": {
            "database": {
                "status": "error",
                "required": True,
                "reason_code": "required_schema_missing",
            },
            "redis": {"status": "not_checked", "required": True},
        },
    }
    assert redis_factory_calls == []
    assert "demo_survey_participation" not in repr(
        connection.statement_parameters[0]
    )


def test_missing_required_postgres_column_is_not_ready():
    columns = _postgres_columns()
    columns.pop("request_hash")
    engine, _connection = _postgres_engine(columns=columns)

    payload = evaluate_runtime_readiness(
        engine=engine,
        redis_uri="memory://",
        production_like=True,
    )

    assert payload["ready"] is False
    assert payload["components"]["database"] == {
        "status": "error",
        "required": True,
        "reason_code": "required_schema_incompatible",
    }


def test_missing_idempotency_unique_index_is_not_ready():
    engine, _connection = _postgres_engine(unique_indexes=[["id"]])

    payload = evaluate_runtime_readiness(
        engine=engine,
        redis_uri="memory://",
        production_like=True,
    )

    assert payload["ready"] is False
    assert payload["components"]["database"] == {
        "status": "error",
        "required": True,
        "reason_code": "required_idempotency_uniqueness_missing",
    }


def test_equivalent_reordered_idempotency_unique_index_is_accepted():
    engine, _connection = _postgres_engine(
        unique_indexes=[
            ["id"],
            [
                "idempotency_key_hash",
                "actor_scope_hash",
                "endpoint",
                "tenant_id",
            ],
        ]
    )

    payload = evaluate_runtime_readiness(
        engine=engine,
        redis_uri="memory://",
        production_like=True,
    )

    assert payload["components"]["database"] == {
        "status": "ok",
        "required": True,
    }


def test_missing_runtime_schema_table_or_sequence_privilege_is_not_ready():
    for missing_privilege in (
        "schema_usage_privilege",
        "select_privilege",
        "insert_privilege",
        "update_privilege",
        "delete_privilege",
        "identity_sequence_privilege",
    ):
        engine, _connection = _postgres_engine(
            **{missing_privilege: False}
        )

        payload = evaluate_runtime_readiness(
            engine=engine,
            redis_uri="memory://",
            production_like=True,
        )

        assert payload["ready"] is False
        assert payload["components"]["database"] == {
            "status": "error",
            "required": True,
            "reason_code": "required_database_privilege_missing",
        }


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


def test_vercel_preview_route_caches_fail_closed_schema_result(monkeypatch):
    for variable in (
        "ENV",
        "FLASK_ENV",
        "RENDER",
        "RENDER_EXTERNAL_URL",
        "VERCEL",
        "VERCEL_ENV",
        "VERCEL_URL",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("VERCEL_ENV", "preview")

    redis_calls = []
    monkeypatch.setattr(
        "services.runtime_readiness.Redis.from_url",
        lambda *_args, **_kwargs: redis_calls.append(True),
    )
    engine, connection = _postgres_engine(relation_present=False)
    app = _route_app(monkeypatch, environment="testing", engine=engine)

    first = app.test_client().get(
        "/health/ready",
        headers={"X-Request-Id": "schema-missing-1"},
    )
    second = app.test_client().get(
        "/health/ready",
        headers={"X-Request-Id": "schema-missing-2"},
    )

    assert first.status_code == 503
    assert second.status_code == 503
    assert first.get_json()["contract_version"] == "runtime.readiness.v1"
    assert first.get_json()["components"]["database"] == {
        "status": "error",
        "required": True,
        "reason_code": "required_schema_missing",
    }
    assert second.get_json()["request_id"] == "schema-missing-2"
    assert len(connection.statements) == 1
    assert redis_calls == []


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
