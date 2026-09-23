"""Operator-safe runtime dependency readiness checks.

The public readiness contract intentionally exposes only coarse component
states.  Connection URLs, exception messages, hosts and credentials must never
be serialized or logged by this module.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import math
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from redis import Redis
from sqlalchemy import text
from sqlalchemy.engine import Engine


logger = logging.getLogger(__name__)

_DEFAULT_DATABASE_TIMEOUT_SECONDS = 1.5
_DEFAULT_REDIS_TIMEOUT_SECONDS = 1.0
_MIN_TIMEOUT_SECONDS = 0.1
_MAX_TIMEOUT_SECONDS = 5.0
# Keep this contract deliberately small.  This table backs an unconditionally
# registered request path, so a production-like runtime cannot serve its
# advertised contract without it.  Extra columns and indexes are allowed for
# forward compatibility; optional feature tables do not belong here unless
# guarded by their corresponding runtime flag.
_REQUIRED_POSTGRESQL_TABLE = "municipio_chat_idempotency_receipt"
_REQUIRED_POSTGRESQL_COLUMNS = (
    ("id", "int4", True),
    ("tenant_id", "int4", True),
    ("endpoint", "varchar", True),
    ("actor_scope_hash", "varchar", True),
    ("idempotency_key_hash", "varchar", True),
    ("request_hash", "varchar", True),
    ("status", "varchar", True),
    ("response_status", "int4", False),
    ("response_json", "jsonb", False),
    ("response_request_id", "varchar", False),
    ("contract_version", "varchar", True),
    ("created_at", "timestamptz", True),
    ("updated_at", "timestamptz", True),
    ("completed_at", "timestamptz", False),
    ("expired_at", "timestamptz", False),
)
_REQUIRED_POSTGRESQL_UNIQUE_COLUMNS = (
    "tenant_id",
    "endpoint",
    "actor_scope_hash",
    "idempotency_key_hash",
)
_SCHEMA_PRIVILEGE_STATE_KEYS = (
    "schema_usage_privilege",
    "select_privilege",
    "insert_privilege",
    "update_privilege",
    "delete_privilege",
    "identity_sequence_privilege",
)


class _CacheEntry:
    def __init__(self) -> None:
        self.value: dict[str, Any] | None = None
        self.expires_at = 0.0
        self.inflight = False


def _probe_unavailable_payload(*, database_status: str) -> dict[str, Any]:
    return {
        "contract_version": "runtime.readiness.v1",
        "ready": False,
        "status": "not_ready",
        "components": {
            "database": {"status": database_status, "required": True},
            "redis": {"status": "not_checked", "required": True},
        },
    }


def _cache_safe_copy(payload: dict[str, Any]) -> dict[str, Any]:
    copied = copy.deepcopy(payload)
    # Correlation belongs to each HTTP request, never to the shared probe.
    copied.pop("request_id", None)
    return copied


class RuntimeReadinessCache:
    """Per-process TTL cache with one in-flight probe per dependency key."""

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._condition = threading.Condition()
        self._entries: dict[tuple[Any, ...], _CacheEntry] = {}

    def clear(self) -> None:
        with self._condition:
            self._entries.clear()
            self._condition.notify_all()

    def get_or_probe(
        self,
        *,
        key: tuple[Any, ...],
        probe: Callable[[], dict[str, Any]],
        ttl_seconds: object,
        wait_timeout_seconds: object,
    ) -> dict[str, Any]:
        ttl = _bounded_timeout(ttl_seconds, default=1.0)
        wait_timeout = _bounded_timeout(wait_timeout_seconds, default=5.0)
        deadline = self._clock() + wait_timeout

        with self._condition:
            entry = self._entries.setdefault(key, _CacheEntry())
            now = self._clock()
            if entry.value is not None and now < entry.expires_at:
                return _cache_safe_copy(entry.value)

            while entry.inflight:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return _probe_unavailable_payload(
                        database_status="probe_timeout"
                    )
                self._condition.wait(timeout=remaining)
                now = self._clock()
                if entry.value is not None and now < entry.expires_at:
                    return _cache_safe_copy(entry.value)

            entry.inflight = True

        try:
            probed = probe()
            if not isinstance(probed, dict):
                raise TypeError("readiness probe returned a non-object payload")
            value = _cache_safe_copy(probed)
        except Exception as exc:
            logger.warning(
                "Runtime readiness aggregate probe failed error_type=%s",
                type(exc).__name__,
            )
            value = _probe_unavailable_payload(database_status="probe_error")

        with self._condition:
            entry.value = _cache_safe_copy(value)
            entry.expires_at = self._clock() + ttl
            entry.inflight = False
            self._condition.notify_all()
            return _cache_safe_copy(value)


_RUNTIME_READINESS_CACHE = RuntimeReadinessCache()


def _bounded_timeout(value: object, *, default: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(timeout):
        return default
    return min(max(timeout, _MIN_TIMEOUT_SECONDS), _MAX_TIMEOUT_SECONDS)


def _postgresql_schema_probe(
    *,
    timeout_seconds: float,
) -> tuple[Any, dict[str, Any]]:
    # PostgreSQL treats comma-separated privilege names as "any of".  Emit
    # one boolean per operation so _schema_contract_failure_reason can require
    # every permission used by the receipt lifecycle.
    statement = text(
        """
        SELECT
            relation.oid IS NOT NULL AS relation_present,
            COALESCE(
                relation.relkind IN ('r', 'p'),
                FALSE
            ) AS table_kind_valid,
            COALESCE(
                (
                    SELECT pg_catalog.jsonb_object_agg(
                        CAST(attribute.attname AS text),
                        pg_catalog.jsonb_build_array(
                            CAST(data_type.typname AS text),
                            attribute.attnotnull
                        )
                    )
                    FROM pg_catalog.pg_attribute AS attribute
                    JOIN pg_catalog.pg_type AS data_type
                      ON data_type.oid = attribute.atttypid
                    WHERE attribute.attrelid = relation.oid
                      AND attribute.attnum > 0
                      AND NOT attribute.attisdropped
                ),
                CAST('{}' AS jsonb)
            ) AS columns,
            COALESCE(
                (
                    SELECT pg_catalog.jsonb_agg(
                        indexed_columns.column_names
                    )
                    FROM pg_catalog.pg_index AS index_definition
                    CROSS JOIN LATERAL (
                        SELECT pg_catalog.jsonb_agg(
                            CAST(attribute.attname AS text)
                            ORDER BY key_column.ordinality
                        ) AS column_names
                        FROM pg_catalog.unnest(index_definition.indkey)
                            WITH ORDINALITY AS key_column(attnum, ordinality)
                        JOIN pg_catalog.pg_attribute AS attribute
                          ON attribute.attrelid = index_definition.indrelid
                         AND attribute.attnum = key_column.attnum
                        WHERE key_column.ordinality
                            <= index_definition.indnkeyatts
                    ) AS indexed_columns
                    WHERE index_definition.indrelid = relation.oid
                      AND index_definition.indisunique
                      AND index_definition.indisvalid
                      AND index_definition.indisready
                      AND index_definition.indislive
                      AND index_definition.indpred IS NULL
                      AND index_definition.indexprs IS NULL
                ),
                CAST('[]' AS jsonb)
            ) AS unique_indexes,
            identity_sequence.name IS NOT NULL
                AS identity_sequence_present,
            COALESCE(
                pg_catalog.has_schema_privilege(
                    relation.relnamespace,
                    'USAGE'
                ),
                FALSE
            ) AS schema_usage_privilege,
            COALESCE(
                pg_catalog.has_table_privilege(relation.oid, 'SELECT'),
                FALSE
            ) AS select_privilege,
            COALESCE(
                pg_catalog.has_table_privilege(relation.oid, 'INSERT'),
                FALSE
            ) AS insert_privilege,
            COALESCE(
                pg_catalog.has_table_privilege(relation.oid, 'UPDATE'),
                FALSE
            ) AS update_privilege,
            COALESCE(
                pg_catalog.has_table_privilege(relation.oid, 'DELETE'),
                FALSE
            ) AS delete_privilege,
            COALESCE(
                pg_catalog.has_sequence_privilege(
                    identity_sequence.name,
                    'USAGE'
                )
                OR pg_catalog.has_sequence_privilege(
                    identity_sequence.name,
                    'UPDATE'
                ),
                FALSE
            ) AS identity_sequence_privilege
        FROM (
            SELECT
                resolved.oid,
                resolved.relnamespace,
                resolved.relkind
            FROM (
                SELECT pg_catalog.to_regclass(:required_table) AS oid
            ) AS lookup
            LEFT JOIN pg_catalog.pg_class AS resolved
              ON resolved.oid = lookup.oid
        ) AS relation
        LEFT JOIN LATERAL (
            SELECT CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_attribute AS identity_attribute
                    WHERE identity_attribute.attrelid = relation.oid
                      AND CAST(identity_attribute.attname AS text) =
                          :identity_column
                      AND identity_attribute.attnum > 0
                      AND NOT identity_attribute.attisdropped
                )
                THEN pg_catalog.pg_get_serial_sequence(
                    CAST(CAST(relation.oid AS regclass) AS text),
                    :identity_column
                )
                ELSE NULL
            END AS name
        ) AS identity_sequence ON TRUE
        """
    ).execution_options(timeout=timeout_seconds)
    bind_params: dict[str, Any] = {
        "required_table": _REQUIRED_POSTGRESQL_TABLE,
        "identity_column": "id",
    }
    return statement, bind_params


def _schema_contract_failure_reason(
    state: Mapping[str, Any],
) -> str | None:
    if state.get("relation_present") is not True:
        return "required_schema_missing"
    if (
        state.get("table_kind_valid") is not True
        or state.get("identity_sequence_present") is not True
    ):
        return "required_schema_incompatible"

    observed_columns = state.get("columns")
    if not isinstance(observed_columns, Mapping):
        return "required_schema_incompatible"
    for column_name, type_name, not_null in _REQUIRED_POSTGRESQL_COLUMNS:
        observed_contract = observed_columns.get(column_name)
        if (
            not isinstance(observed_contract, (list, tuple))
            or len(observed_contract) != 2
            or observed_contract[0] != type_name
            or not isinstance(observed_contract[1], bool)
            or observed_contract[1] != not_null
        ):
            return "required_schema_incompatible"

    observed_unique_indexes = state.get("unique_indexes")
    required_unique_columns = list(_REQUIRED_POSTGRESQL_UNIQUE_COLUMNS)
    if not isinstance(observed_unique_indexes, (list, tuple)):
        return "required_schema_incompatible"
    normalized_unique_indexes = [
        list(index_columns)
        for index_columns in observed_unique_indexes
        if isinstance(index_columns, (list, tuple))
    ]

    def has_equivalent_unique_index(required_columns: list[str]) -> bool:
        return any(
            len(index_columns) == len(required_columns)
            and set(index_columns) == set(required_columns)
            for index_columns in normalized_unique_indexes
        )

    if not has_equivalent_unique_index(["id"]):
        return "required_schema_incompatible"
    if not has_equivalent_unique_index(required_unique_columns):
        return "required_idempotency_uniqueness_missing"
    if any(
        state.get(key) is not True
        for key in _SCHEMA_PRIVILEGE_STATE_KEYS
    ):
        return "required_database_privilege_missing"
    return None


def _database_status(
    engine: Engine,
    *,
    timeout_seconds: float,
    require_postgresql: bool,
) -> dict[str, Any]:
    """Run bounded connectivity and critical-schema probes without row reads."""

    timeout_ms = max(1, int(timeout_seconds * 1000))
    dialect_name = ""
    try:
        with engine.connect() as connection:
            dialect_name = str(getattr(connection.dialect, "name", "")).lower()
            if dialect_name == "postgresql":
                # Render production uses PostgreSQL. SET LOCAL scopes the
                # timeout to this transaction and is rolled back on close.
                connection.exec_driver_sql(
                    f"SET LOCAL statement_timeout = {timeout_ms}"
                )
            if require_postgresql and dialect_name == "postgresql":
                schema_statement, bind_params = _postgresql_schema_probe(
                    timeout_seconds=timeout_seconds
                )
                schema_state = connection.execute(
                    schema_statement,
                    bind_params,
                ).mappings().one()
                failure_reason = _schema_contract_failure_reason(schema_state)
                if failure_reason is not None:
                    logger.warning(
                        "Runtime readiness database schema probe failed "
                        "reason_code=%s",
                        failure_reason,
                    )
                    return {
                        "status": "error",
                        "required": True,
                        "reason_code": failure_reason,
                    }
            else:
                result = connection.execute(
                    text("SELECT 1").execution_options(
                        timeout=timeout_seconds
                    )
                )
                if result.scalar_one() != 1:
                    raise RuntimeError(
                        "unexpected database readiness result"
                    )
    except Exception as exc:
        logger.warning(
            "Runtime readiness database probe failed error_type=%s",
            type(exc).__name__,
        )
        return {"status": "error", "required": True}

    if require_postgresql and dialect_name != "postgresql":
        logger.warning(
            "Runtime readiness database probe rejected non-PostgreSQL production storage"
        )
        return {"status": "invalid_configuration", "required": True}

    return {"status": "ok", "required": True}


def _shared_redis_uri_state(redis_uri: object) -> tuple[str, str]:
    raw_uri = str(redis_uri or "").strip()
    if not raw_uri or raw_uri.lower() == "memory://":
        return "not_configured", raw_uri

    try:
        parsed = urlsplit(raw_uri)
    except ValueError:
        return "invalid_configuration", ""

    if parsed.scheme.lower() not in {"redis", "rediss"} or not parsed.hostname:
        return "invalid_configuration", ""
    return "configured", raw_uri


def _redis_status(
    redis_uri: str,
    *,
    timeout_seconds: float,
    redis_factory: Callable[..., Any],
) -> dict[str, Any]:
    client = None
    try:
        client = redis_factory(
            redis_uri,
            socket_connect_timeout=timeout_seconds,
            socket_timeout=timeout_seconds,
            retry_on_timeout=False,
            health_check_interval=0,
        )
        if client.ping() is not True:
            raise RuntimeError("unexpected Redis readiness result")
    except Exception as exc:
        logger.warning(
            "Runtime readiness Redis probe failed error_type=%s",
            type(exc).__name__,
        )
        return {"status": "error", "required": True}
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                logger.warning(
                    "Runtime readiness Redis close failed error_type=%s",
                    type(exc).__name__,
                )

    return {"status": "ok", "required": True}


def evaluate_runtime_readiness(
    *,
    engine: Engine,
    redis_uri: object,
    production_like: bool,
    database_timeout_seconds: object = _DEFAULT_DATABASE_TIMEOUT_SECONDS,
    redis_timeout_seconds: object = _DEFAULT_REDIS_TIMEOUT_SECONDS,
    redis_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Return the stable, secret-free readiness state for the web runtime."""

    database_timeout = _bounded_timeout(
        database_timeout_seconds,
        default=_DEFAULT_DATABASE_TIMEOUT_SECONDS,
    )
    redis_timeout = _bounded_timeout(
        redis_timeout_seconds,
        default=_DEFAULT_REDIS_TIMEOUT_SECONDS,
    )

    database = _database_status(
        engine,
        timeout_seconds=database_timeout,
        require_postgresql=bool(production_like),
    )
    redis_configuration, normalized_redis_uri = _shared_redis_uri_state(redis_uri)

    if database["status"] != "ok" and redis_configuration == "configured":
        # DB is authoritative and already makes the process not ready. Avoid a
        # second network timeout and avoid amplifying an outage through probes.
        redis = {"status": "not_checked", "required": True}
    elif redis_configuration == "configured":
        redis = _redis_status(
            normalized_redis_uri,
            timeout_seconds=redis_timeout,
            redis_factory=redis_factory or Redis.from_url,
        )
    elif redis_configuration == "not_configured":
        redis = {
            "status": "not_configured",
            "required": bool(production_like),
        }
    else:
        # An explicit malformed backend is a configuration failure in every
        # environment; silently treating it as optional hides drift.
        redis = {"status": "invalid_configuration", "required": True}

    components = {"database": database, "redis": redis}
    ready = all(
        not component["required"] or component["status"] == "ok"
        for component in components.values()
    )
    return {
        "contract_version": "runtime.readiness.v1",
        "ready": ready,
        "status": "ready" if ready else "not_ready",
        "components": components,
    }


def get_cached_runtime_readiness(
    *,
    engine: Engine,
    redis_uri: object,
    production_like: bool,
    database_timeout_seconds: object = _DEFAULT_DATABASE_TIMEOUT_SECONDS,
    redis_timeout_seconds: object = _DEFAULT_REDIS_TIMEOUT_SECONDS,
    cache_ttl_seconds: object = 1.0,
    redis_factory: Callable[..., Any] | None = None,
    cache: RuntimeReadinessCache | None = None,
) -> dict[str, Any]:
    """Coalesce concurrent public probes and return a request-safe copy."""

    database_timeout = _bounded_timeout(
        database_timeout_seconds,
        default=_DEFAULT_DATABASE_TIMEOUT_SECONDS,
    )
    redis_timeout = _bounded_timeout(
        redis_timeout_seconds,
        default=_DEFAULT_REDIS_TIMEOUT_SECONDS,
    )
    ttl = _bounded_timeout(cache_ttl_seconds, default=1.0)
    redis_fingerprint = hashlib.sha256(
        str(redis_uri or "").strip().encode("utf-8")
    ).digest()
    try:
        hash(engine)
    except TypeError:
        engine_identity: object = (type(engine).__qualname__, id(engine))
    else:
        # Holding the process-scoped engine prevents Python object-id reuse from
        # ever matching an unrelated app instance during the short cache TTL.
        engine_identity = engine
    key = (
        engine_identity,
        bool(production_like),
        redis_fingerprint,
        database_timeout,
        redis_timeout,
        ttl,
        id(redis_factory) if redis_factory is not None else 0,
    )
    coordinator = cache or _RUNTIME_READINESS_CACHE
    return coordinator.get_or_probe(
        key=key,
        probe=lambda: evaluate_runtime_readiness(
            engine=engine,
            redis_uri=redis_uri,
            production_like=production_like,
            database_timeout_seconds=database_timeout,
            redis_timeout_seconds=redis_timeout,
            redis_factory=redis_factory,
        ),
        ttl_seconds=ttl,
        wait_timeout_seconds=database_timeout + redis_timeout + 1.0,
    )


__all__ = [
    "RuntimeReadinessCache",
    "evaluate_runtime_readiness",
    "get_cached_runtime_readiness",
]
