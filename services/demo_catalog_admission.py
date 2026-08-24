"""Atomic admission control for the expensive public demo catalog.

Production uses one Redis Lua execution so the deployment-wide and client
fixed-window counters are checked and incremented as a unit.  The in-memory
path is deliberately process-local and exists only for development and tests.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
import threading
import time
from typing import Literal

from limits.storage import MemoryStorage, RedisStorage


_NAMESPACE = "{demo-catalog-full-v3}"
_CLIENT_SCOPE_RE = re.compile(r"^[0-9a-f]{64}$")
_LOCAL_MEMORY_LOCK = threading.RLock()

_ALLOWED = 1
_GLOBAL_DENIED = 2
_CLIENT_DENIED = 3


# Both keys carry the same Redis hash tag, so the script remains valid if the
# backing Redis service is later clustered.  Each counter starts a fixed window
# on its first admitted request and retains that expiry on subsequent hits.
# Every value and TTL is validated before either counter is mutated.
REDIS_DUAL_FIXED_WINDOW_LUA = r"""
local global_raw = redis.call('GET', KEYS[1])
local client_raw = redis.call('GET', KEYS[2])

local global_count = 0
local client_count = 0
local global_ttl = tonumber(ARGV[3])
local client_ttl = tonumber(ARGV[4])

if global_raw then
    global_count = tonumber(global_raw)
    if (not global_count) or global_count < 0 or global_count ~= math.floor(global_count) then
        error('invalid global demo catalog counter')
    end
    global_ttl = redis.call('PTTL', KEYS[1])
    if global_ttl <= 0 then
        error('invalid global demo catalog counter expiry')
    end
end

if client_raw then
    client_count = tonumber(client_raw)
    if (not client_count) or client_count < 0 or client_count ~= math.floor(client_count) then
        error('invalid client demo catalog counter')
    end
    client_ttl = redis.call('PTTL', KEYS[2])
    if client_ttl <= 0 then
        error('invalid client demo catalog counter expiry')
    end
end

local global_limit = tonumber(ARGV[1])
local client_limit = tonumber(ARGV[2])

-- Prefer the global breaker when both windows are exhausted.  Crucially, a
-- denial returns before touching either key.
if global_count >= global_limit then
    return {2, global_ttl}
end
if client_count >= client_limit then
    return {3, client_ttl}
end

local new_global = redis.call('INCR', KEYS[1])
local new_client = redis.call('INCR', KEYS[2])
if new_global == 1 then
    redis.call('PEXPIRE', KEYS[1], ARGV[3])
end
if new_client == 1 then
    redis.call('PEXPIRE', KEYS[2], ARGV[4])
end

return {1, 0}
"""


@dataclass(frozen=True)
class DemoCatalogAdmission:
    allowed: bool
    scope: Literal["global", "client"] | None = None
    retry_after_seconds: int = 0
    reset_at_epoch: float | None = None


def demo_catalog_counter_keys(storage, client_scope: str) -> tuple[str, str]:
    """Return bounded, co-located keys without exposing the source address."""

    if not _CLIENT_SCOPE_RE.fullmatch(client_scope):
        raise ValueError("invalid demo catalog client scope")

    global_key = f"demo-catalog-full-v3:{_NAMESPACE}:global"
    client_key = f"demo-catalog-full-v3:{_NAMESPACE}:client:{client_scope}"
    if isinstance(storage, RedisStorage):
        global_key = storage.prefixed_key(global_key)
        client_key = storage.prefixed_key(client_key)
    return global_key, client_key


def _validate_limits(
    *,
    client_limit: int,
    client_window_seconds: int,
    global_limit: int,
    global_window_seconds: int,
) -> None:
    for value in (
        client_limit,
        client_window_seconds,
        global_limit,
        global_window_seconds,
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("invalid demo catalog rate limit")
    if client_limit > 10_000 or global_limit > 10_000:
        raise ValueError("demo catalog rate limit exceeds supported capacity")
    if client_window_seconds > 86_400 or global_window_seconds > 86_400:
        raise ValueError("demo catalog rate limit window exceeds supported duration")
    if global_limit * client_window_seconds <= client_limit * global_window_seconds:
        raise ValueError("global demo catalog limit must exceed client capacity")


def _decision(code: int, retry_milliseconds: int) -> DemoCatalogAdmission:
    if code == _ALLOWED:
        if retry_milliseconds != 0:
            raise RuntimeError("invalid Redis admission response")
        return DemoCatalogAdmission(allowed=True)

    scope = {
        _GLOBAL_DENIED: "global",
        _CLIENT_DENIED: "client",
    }.get(code)
    if scope is None or retry_milliseconds <= 0:
        raise RuntimeError("invalid Redis admission response")
    retry_after = max(1, math.ceil(retry_milliseconds / 1_000))
    now = time.time()
    return DemoCatalogAdmission(
        allowed=False,
        scope=scope,
        retry_after_seconds=retry_after,
        reset_at_epoch=now + (retry_milliseconds / 1_000),
    )


def _admit_with_redis(
    storage: RedisStorage,
    *,
    global_key: str,
    client_key: str,
    client_limit: int,
    client_window_seconds: int,
    global_limit: int,
    global_window_seconds: int,
) -> DemoCatalogAdmission:
    connection = storage.get_connection()
    result = connection.eval(
        REDIS_DUAL_FIXED_WINDOW_LUA,
        2,
        global_key,
        client_key,
        global_limit,
        client_limit,
        global_window_seconds * 1_000,
        client_window_seconds * 1_000,
    )
    if not isinstance(result, (list, tuple)) or len(result) != 2:
        raise RuntimeError("invalid Redis admission response")
    try:
        code = int(result[0])
        retry_milliseconds = int(result[1])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("invalid Redis admission response") from exc

    maximum_retry = {
        _ALLOWED: 0,
        _GLOBAL_DENIED: global_window_seconds * 1_000,
        _CLIENT_DENIED: client_window_seconds * 1_000,
    }.get(code)
    if maximum_retry is None:
        raise RuntimeError("invalid Redis admission response")
    if retry_milliseconds < 0 or retry_milliseconds > maximum_retry:
        raise RuntimeError("invalid Redis admission response")
    return _decision(code, retry_milliseconds)


def _memory_retry_after(storage: MemoryStorage, key: str, window_seconds: int) -> int:
    reset_at = float(storage.get_expiry(key))
    return max(1, math.ceil(reset_at - time.time())) if reset_at else window_seconds


def _admit_with_process_memory(
    storage: MemoryStorage,
    *,
    global_key: str,
    client_key: str,
    client_limit: int,
    client_window_seconds: int,
    global_limit: int,
    global_window_seconds: int,
) -> DemoCatalogAdmission:
    """Deterministic single-process fallback; never suitable for multiworker use."""

    with _LOCAL_MEMORY_LOCK:
        global_count = int(storage.get(global_key))
        client_count = int(storage.get(client_key))
        if global_count >= global_limit:
            retry_after = _memory_retry_after(
                storage,
                global_key,
                global_window_seconds,
            )
            return DemoCatalogAdmission(
                allowed=False,
                scope="global",
                retry_after_seconds=retry_after,
                reset_at_epoch=time.time() + retry_after,
            )
        if client_count >= client_limit:
            retry_after = _memory_retry_after(
                storage,
                client_key,
                client_window_seconds,
            )
            return DemoCatalogAdmission(
                allowed=False,
                scope="client",
                retry_after_seconds=retry_after,
                reset_at_epoch=time.time() + retry_after,
            )

        # The shared lock makes these two in-process increments one admission
        # decision.  MemoryStorage itself supplies fixed-window expiry/reset.
        storage.incr(global_key, global_window_seconds)
        storage.incr(client_key, client_window_seconds)
        return DemoCatalogAdmission(allowed=True)


def admit_demo_catalog_full(
    storage,
    *,
    client_scope: str,
    client_limit: int,
    client_window_seconds: int,
    global_limit: int,
    global_window_seconds: int,
    allow_process_memory: bool,
) -> DemoCatalogAdmission:
    """Atomically admit against the deployment and per-client windows."""

    _validate_limits(
        client_limit=client_limit,
        client_window_seconds=client_window_seconds,
        global_limit=global_limit,
        global_window_seconds=global_window_seconds,
    )
    global_key, client_key = demo_catalog_counter_keys(storage, client_scope)

    if isinstance(storage, RedisStorage):
        return _admit_with_redis(
            storage,
            global_key=global_key,
            client_key=client_key,
            client_limit=client_limit,
            client_window_seconds=client_window_seconds,
            global_limit=global_limit,
            global_window_seconds=global_window_seconds,
        )
    if allow_process_memory and isinstance(storage, MemoryStorage):
        return _admit_with_process_memory(
            storage,
            global_key=global_key,
            client_key=client_key,
            client_limit=client_limit,
            client_window_seconds=client_window_seconds,
            global_limit=global_limit,
            global_window_seconds=global_window_seconds,
        )
    raise RuntimeError("shared Redis admission storage required")


__all__ = [
    "DemoCatalogAdmission",
    "REDIS_DUAL_FIXED_WINDOW_LUA",
    "admit_demo_catalog_full",
    "demo_catalog_counter_keys",
]
