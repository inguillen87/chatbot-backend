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
from collections.abc import Callable
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


def _database_status(
    engine: Engine,
    *,
    timeout_seconds: float,
    require_postgresql: bool,
) -> dict[str, Any]:
    """Run a side-effect-free database probe with a short statement timeout."""

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
            result = connection.execute(
                text("SELECT 1").execution_options(timeout=timeout_seconds)
            )
            if result.scalar_one() != 1:
                raise RuntimeError("unexpected database readiness result")
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
