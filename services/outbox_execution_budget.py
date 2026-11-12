"""Cron-only execution budget for durable outbox reconciliation.

The Vercel reconciliation deadline is cooperative at the worker level.  This
module carries that same deadline into the blocking I/O adapters that are
reachable from ``/internal/cron/outbox``.  Outside the explicit context manager
every helper is a no-op, so regular web requests and permanent workers retain
their existing provider configuration.
"""

from __future__ import annotations

import math
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any

from sqlalchemy import event


DEFAULT_IO_TIMEOUT_CAP_SECONDS = 4.0
DEFAULT_CLEANUP_RESERVE_SECONDS = 6.0
DEFAULT_PERSISTENCE_TIMEOUT_CAP_SECONDS = 1.0
MINIMUM_IO_TIMEOUT_SECONDS = 0.1


class OutboxExecutionBudgetExceeded(TimeoutError):
    """No safe time remains to begin another blocking I/O operation."""


@dataclass(frozen=True)
class _OutboxExecutionBudget:
    deadline_monotonic: float
    clock: Callable[[], float]
    io_timeout_cap_seconds: float
    cleanup_reserve_seconds: float


_ACTIVE_BUDGET: ContextVar[_OutboxExecutionBudget | None] = ContextVar(
    "chatboc_outbox_execution_budget",
    default=None,
)
_PERSISTENCE_DEPTH: ContextVar[int] = ContextVar(
    "chatboc_outbox_persistence_depth",
    default=0,
)
_HOOKED_ENGINES: "weakref.WeakSet[Any]" = weakref.WeakSet()
_HOOK_LOCK = threading.Lock()


def outbox_execution_budget_active() -> bool:
    return _ACTIVE_BUDGET.get() is not None


def outbox_persistence_budget_active() -> bool:
    """Return whether this call stack is persisting a terminal outbox state."""

    return outbox_execution_budget_active() and _PERSISTENCE_DEPTH.get() > 0


def _finite_positive(value: object, *, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if not math.isfinite(parsed) or parsed <= 0:
        return fallback
    return parsed


@contextmanager
def activate_outbox_execution_budget(
    *,
    deadline_monotonic: float,
    clock: Callable[[], float] = time.monotonic,
    io_timeout_cap_seconds: float = DEFAULT_IO_TIMEOUT_CAP_SECONDS,
    cleanup_reserve_seconds: float = DEFAULT_CLEANUP_RESERVE_SECONDS,
) -> Iterator[None]:
    """Activate a deadline only for the current cron execution context."""

    if not callable(clock):
        raise TypeError("outbox_execution_budget_clock_invalid")
    deadline = _finite_positive(
        deadline_monotonic,
        fallback=0.0,
    )
    if deadline <= 0:
        raise ValueError("outbox_execution_budget_deadline_invalid")
    cap = _finite_positive(
        io_timeout_cap_seconds,
        fallback=DEFAULT_IO_TIMEOUT_CAP_SECONDS,
    )
    reserve = _finite_positive(
        cleanup_reserve_seconds,
        fallback=DEFAULT_CLEANUP_RESERVE_SECONDS,
    )
    token = _ACTIVE_BUDGET.set(
        _OutboxExecutionBudget(
            deadline_monotonic=deadline,
            clock=clock,
            io_timeout_cap_seconds=cap,
            cleanup_reserve_seconds=reserve,
        )
    )
    try:
        yield
    finally:
        _ACTIVE_BUDGET.reset(token)


@contextmanager
def outbox_persistence_budget() -> Iterator[None]:
    """Allow bounded terminal persistence to use the cleanup reserve.

    This mode does not extend the invocation deadline and does not affect
    provider timeout helpers.  It only changes database statement budgeting,
    so new external I/O remains forbidden once the cleanup reserve begins.
    """

    token = _PERSISTENCE_DEPTH.set(_PERSISTENCE_DEPTH.get() + 1)
    try:
        yield
    finally:
        _PERSISTENCE_DEPTH.reset(token)


def outbox_persistence_operation(func: Callable[..., Any]) -> Callable[..., Any]:
    """Mark one shared terminal-transition function as persistence-only."""

    @wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with outbox_persistence_budget():
            return func(*args, **kwargs)

    return wrapped


def outbox_io_timeout_seconds(
    configured_timeout_seconds: object | None = None,
    *,
    minimum_seconds: float = MINIMUM_IO_TIMEOUT_SECONDS,
) -> float | None:
    """Return a cron-safe per-attempt timeout, or ``None`` outside the cron.

    The returned value is always below both the invocation deadline and the
    configured per-I/O cap.  Raising before a new call is safer than starting
    an operation that cannot leave time to persist its fenced outcome.
    """

    budget = _ACTIVE_BUDGET.get()
    if budget is None:
        return None

    minimum = _finite_positive(
        minimum_seconds,
        fallback=MINIMUM_IO_TIMEOUT_SECONDS,
    )
    remaining = (
        budget.deadline_monotonic
        - float(budget.clock())
        - budget.cleanup_reserve_seconds
    )
    if not math.isfinite(remaining) or remaining < minimum:
        raise OutboxExecutionBudgetExceeded("outbox_execution_budget_exhausted")

    configured = budget.io_timeout_cap_seconds
    if configured_timeout_seconds not in (None, ""):
        configured = _finite_positive(
            configured_timeout_seconds,
            fallback=budget.io_timeout_cap_seconds,
        )
    return max(
        minimum,
        min(configured, budget.io_timeout_cap_seconds, remaining),
    )


def outbox_database_timeout_seconds(
    *,
    minimum_seconds: float = MINIMUM_IO_TIMEOUT_SECONDS,
) -> float | None:
    """Return the phase-aware timeout for a new database statement.

    Ordinary statements obey the same cleanup reserve as provider I/O.
    Explicit terminal persistence may consume the real time remaining before
    the invocation deadline, but every statement is capped at one second.
    """

    budget = _ACTIVE_BUDGET.get()
    if budget is None:
        return None

    minimum = _finite_positive(
        minimum_seconds,
        fallback=MINIMUM_IO_TIMEOUT_SECONDS,
    )
    persistence_active = outbox_persistence_budget_active()
    reserve = 0.0 if persistence_active else budget.cleanup_reserve_seconds
    remaining = budget.deadline_monotonic - float(budget.clock()) - reserve
    if not math.isfinite(remaining) or remaining < minimum:
        raise OutboxExecutionBudgetExceeded("outbox_execution_budget_exhausted")

    cap = (
        min(
            budget.io_timeout_cap_seconds,
            DEFAULT_PERSISTENCE_TIMEOUT_CAP_SECONDS,
        )
        if persistence_active
        else budget.io_timeout_cap_seconds
    )
    return max(minimum, min(cap, remaining))


def require_outbox_time_remaining() -> None:
    """Fail before a new blocking boundary when the cron has no safe runway."""

    if outbox_execution_budget_active():
        outbox_io_timeout_seconds()


def outbox_twilio_http_client() -> Any | None:
    """Return a bounded Twilio transport only inside reconciliation."""

    timeout_seconds = outbox_io_timeout_seconds()
    if timeout_seconds is None:
        return None
    from twilio.http.http_client import TwilioHttpClient

    return TwilioHttpClient(timeout=timeout_seconds)


def _apply_postgresql_transaction_timeouts(connection: Any) -> None:
    if not outbox_execution_budget_active():
        return
    dialect = str(getattr(getattr(connection, "dialect", None), "name", ""))
    if dialect.lower() != "postgresql":
        return

    timeout_seconds = outbox_database_timeout_seconds()
    if timeout_seconds is None:  # pragma: no cover - guarded above
        return
    statement_timeout_ms = max(1, int(timeout_seconds * 1000))
    # Lock waits get a tighter slice so leases and row claims leave time for
    # the provider call and for persisting its fenced outcome.
    lock_timeout_ms = max(1, min(statement_timeout_ms, 2000))
    connection.exec_driver_sql(
        f"SET LOCAL lock_timeout = {lock_timeout_ms}"
    )
    connection.exec_driver_sql(
        f"SET LOCAL statement_timeout = {statement_timeout_ms}"
    )


def _guard_postgresql_statement_start(
    connection: Any,
    cursor: Any,
    statement: str,
    _parameters: Any,
    _context: Any,
    _executemany: bool,
) -> None:
    timeout_seconds = outbox_database_timeout_seconds()
    if timeout_seconds is None:
        return

    # A terminal transition can enter persistence mode after its transaction
    # began (for example, survey effects whose handler and status update are
    # atomic). Refresh SET LOCAL through the DBAPI cursor so SQLAlchemy's event
    # hook cannot recurse and the shorter persistence limit takes effect now.
    dialect = str(getattr(getattr(connection, "dialect", None), "name", ""))
    normalized_statement = str(statement or "").lstrip().upper()
    if (
        outbox_persistence_budget_active()
        and dialect.lower() == "postgresql"
        and not normalized_statement.startswith("SET LOCAL ")
    ):
        statement_timeout_ms = max(1, int(timeout_seconds * 1000))
        lock_timeout_ms = max(1, min(statement_timeout_ms, 1000))
        cursor.execute(f"SET LOCAL lock_timeout = {lock_timeout_ms}")
        cursor.execute(f"SET LOCAL statement_timeout = {statement_timeout_ms}")


def install_outbox_database_timeout_hook(engine: Any) -> None:
    """Install one inert-by-default SQLAlchemy transaction hook per engine."""

    with _HOOK_LOCK:
        if engine in _HOOKED_ENGINES:
            return
        event.listen(engine, "begin", _apply_postgresql_transaction_timeouts)
        event.listen(
            engine,
            "before_cursor_execute",
            _guard_postgresql_statement_start,
        )
        _HOOKED_ENGINES.add(engine)


__all__ = [
    "DEFAULT_CLEANUP_RESERVE_SECONDS",
    "DEFAULT_IO_TIMEOUT_CAP_SECONDS",
    "DEFAULT_PERSISTENCE_TIMEOUT_CAP_SECONDS",
    "OutboxExecutionBudgetExceeded",
    "activate_outbox_execution_budget",
    "install_outbox_database_timeout_hook",
    "outbox_database_timeout_seconds",
    "outbox_execution_budget_active",
    "outbox_io_timeout_seconds",
    "outbox_persistence_budget",
    "outbox_persistence_budget_active",
    "outbox_persistence_operation",
    "outbox_twilio_http_client",
    "require_outbox_time_remaining",
]
