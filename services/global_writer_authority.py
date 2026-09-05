"""PostgreSQL-backed global writer authority for Render/Vercel cutovers.

This guard is tenant-agnostic and contains no application payload.  It is a
second, shared line of defence behind the per-runtime writer fence: even if a
deployment is accidentally unfenced, only the runtime named by the singleton
PostgreSQL row may execute mutations.
"""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

from cutover_writer_fence import cutover_writer_fence_enabled
from global_writer_authority import (
    GLOBAL_WRITER_AUTHORITY_CONTRACT,
    SUPPORTED_WRITER_RUNTIMES,
    configured_writer_authority_database_url,
    configured_writer_runtime,
    global_writer_authority_enabled,
    writer_authority_endpoint_is_recognizably_pooled,
)


AUTHORITY_KEY = "primary"
AUTHORITY_TABLE = "cutover_global_writer_authority"
CONTROL_CONNECT_TIMEOUT_SECONDS = 4
CONTROL_POOL_TIMEOUT_SECONDS = 2
CONTROL_STATEMENT_TIMEOUT_MS = 2500
CONTROL_LOCK_TIMEOUT_MS = 1000
CONTROL_IDLE_TRANSACTION_TIMEOUT_MS = 3000
_CONTROL_ENGINE_LOCK = threading.Lock()
_CONTROL_ENGINE: Engine | None = None
_CONTROL_ENGINE_FINGERPRINT: str | None = None


@dataclass(frozen=True)
class GlobalWriterAuthorityState:
    owner_runtime: str | None
    epoch: int
    render_fenced: bool
    vercel_fenced: bool

    def runtime_fenced(self, runtime: str) -> bool:
        if runtime == "render":
            return self.render_fenced
        if runtime == "vercel":
            return self.vercel_fenced
        return True


@dataclass(frozen=True)
class GlobalWriterAuthorityDecision:
    allowed: bool
    enabled: bool
    reason_code: str
    epoch: int | None = None


@dataclass(frozen=True)
class GlobalWriterAuthorityLease:
    """Shared-row lease held until the protected application commit finishes."""

    decision: GlobalWriterAuthorityDecision
    executor: Any | None

    def revalidate(
        self, config: Mapping[str, Any] | None = None
    ) -> GlobalWriterAuthorityDecision:
        return evaluate_global_writer_authority(config, executor=self.executor)


class GlobalWriterAuthorityTransitionError(RuntimeError):
    """Stable control-plane failure that never includes a DSN or payload."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _executor_dialect_name(executor: Any) -> str:
    try:
        bind = executor.get_bind() if hasattr(executor, "get_bind") else executor
        return str(bind.dialect.name).strip().lower()
    except Exception as exc:
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_database_unavailable"
        ) from exc


def _control_database_engine(config: Mapping[str, Any] | None) -> Engine:
    """Build/reuse only the explicit shared control database connection."""

    database_url = configured_writer_authority_database_url(config)
    if database_url is None:
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_control_database_url_missing"
        )
    try:
        parsed = make_url(database_url)
    except Exception as exc:
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_control_database_url_invalid"
        ) from exc
    if parsed.get_backend_name() != "postgresql":
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_requires_postgresql"
        )
    if not parsed.host or not parsed.username or not parsed.password or not parsed.database:
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_control_database_url_incomplete"
        )
    indirect_parameters = {
        "host",
        "hostaddr",
        "options",
        "passfile",
        "password",
        "service",
        "servicefile",
        "user",
    }.intersection(str(key).lower() for key in parsed.query)
    if indirect_parameters:
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_control_database_url_invalid"
        )
    if writer_authority_endpoint_is_recognizably_pooled(
        parsed.host,
        parsed.query,
    ):
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_control_database_must_be_direct"
        )
    if str(parsed.query.get("sslmode") or "").strip().lower() not in {
        "require",
        "verify-ca",
        "verify-full",
    }:
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_control_database_tls_required"
        )
    fingerprint = hashlib.sha256(database_url.encode("utf-8")).hexdigest()
    global _CONTROL_ENGINE, _CONTROL_ENGINE_FINGERPRINT
    with _CONTROL_ENGINE_LOCK:
        if _CONTROL_ENGINE is not None and _CONTROL_ENGINE_FINGERPRINT == fingerprint:
            return _CONTROL_ENGINE
        if _CONTROL_ENGINE is not None:
            _CONTROL_ENGINE.dispose()
        try:
            _CONTROL_ENGINE = create_engine(
                parsed.set(drivername="postgresql+psycopg"),
                pool_pre_ping=True,
                pool_size=2,
                max_overflow=1,
                pool_recycle=300,
                pool_timeout=CONTROL_POOL_TIMEOUT_SECONDS,
                connect_args={
                    "application_name": "chatboc_global_writer_authority_gate",
                    "connect_timeout": CONTROL_CONNECT_TIMEOUT_SECONDS,
                    # libpq applies these settings as soon as the session is
                    # established.  This also bounds a SELECT waiting on a
                    # table/row lock; connect_timeout alone does not.
                    "options": (
                        f"-c statement_timeout={CONTROL_STATEMENT_TIMEOUT_MS}ms "
                        f"-c lock_timeout={CONTROL_LOCK_TIMEOUT_MS}ms "
                        "-c idle_in_transaction_session_timeout="
                        f"{CONTROL_IDLE_TRANSACTION_TIMEOUT_MS}ms"
                    ),
                },
            )
        except Exception as exc:
            _CONTROL_ENGINE = None
            _CONTROL_ENGINE_FINGERPRINT = None
            raise GlobalWriterAuthorityTransitionError(
                "global_writer_authority_control_database_unavailable"
            ) from exc
        _CONTROL_ENGINE_FINGERPRINT = fingerprint
        return _CONTROL_ENGINE


@contextmanager
def global_writer_authority_connection(config: Mapping[str, Any] | None = None):
    """Yield the common control connection, never the application DB session."""

    engine = _control_database_engine(config)
    try:
        with engine.connect() as connection:
            yield connection
    except GlobalWriterAuthorityTransitionError:
        raise
    except Exception as exc:
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_control_database_unavailable"
        ) from exc


def load_global_writer_authority_state(
    executor: Any, *, lock_for_share: bool = False
) -> GlobalWriterAuthorityState:
    """Read and strictly validate the singleton authority row."""

    if _executor_dialect_name(executor) != "postgresql":
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_requires_postgresql"
        )
    lock_clause = "FOR SHARE" if lock_for_share else ""
    try:
        row = executor.execute(
            text(
                f"""
                SELECT owner_runtime, epoch, render_fenced, vercel_fenced
                FROM public.cutover_global_writer_authority
                WHERE authority_key = :authority_key
                {lock_clause}
                """
            ),
            {"authority_key": AUTHORITY_KEY},
        ).mappings().one_or_none()
    except GlobalWriterAuthorityTransitionError:
        raise
    except Exception as exc:
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_database_unavailable"
        ) from exc
    if row is None:
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_state_missing"
        )
    owner = str(row["owner_runtime"] or "").strip().lower() or None
    if owner is not None and owner not in SUPPORTED_WRITER_RUNTIMES:
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_state_invalid"
        )
    raw_epoch = row["epoch"]
    if isinstance(raw_epoch, bool):
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_state_invalid"
        )
    try:
        epoch = int(raw_epoch)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_state_invalid"
        ) from exc
    if epoch < 0 or not isinstance(row["render_fenced"], bool) or not isinstance(
        row["vercel_fenced"], bool
    ):
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_state_invalid"
        )
    return GlobalWriterAuthorityState(
        owner_runtime=owner,
        epoch=epoch,
        render_fenced=row["render_fenced"],
        vercel_fenced=row["vercel_fenced"],
    )


def evaluate_global_writer_authority(
    config: Mapping[str, Any] | None = None,
    *,
    executor: Any | None = None,
) -> GlobalWriterAuthorityDecision:
    """Evaluate the shared writer gate, failing closed once it is enabled."""

    if not global_writer_authority_enabled(config):
        return GlobalWriterAuthorityDecision(
            allowed=True,
            enabled=False,
            reason_code="global_writer_authority_disabled",
        )
    runtime = configured_writer_runtime(config)
    if runtime is None:
        return GlobalWriterAuthorityDecision(
            allowed=False,
            enabled=True,
            reason_code="global_writer_runtime_identity_invalid",
        )
    if cutover_writer_fence_enabled(config):
        return GlobalWriterAuthorityDecision(
            allowed=False,
            enabled=True,
            reason_code="cutover_writer_fence_enabled",
        )
    try:
        if executor is None:
            with global_writer_authority_connection(config) as control_connection:
                state = load_global_writer_authority_state(control_connection)
        else:
            state = load_global_writer_authority_state(executor)
    except GlobalWriterAuthorityTransitionError as exc:
        return GlobalWriterAuthorityDecision(
            allowed=False,
            enabled=True,
            reason_code=exc.reason_code,
        )
    if state.owner_runtime != runtime:
        return GlobalWriterAuthorityDecision(
            allowed=False,
            enabled=True,
            reason_code="runtime_not_global_writer_owner",
            epoch=state.epoch,
        )
    if state.runtime_fenced(runtime):
        return GlobalWriterAuthorityDecision(
            allowed=False,
            enabled=True,
            reason_code="runtime_globally_fenced",
            epoch=state.epoch,
        )
    return GlobalWriterAuthorityDecision(
        allowed=True,
        enabled=True,
        reason_code="runtime_is_global_writer_owner",
        epoch=state.epoch,
    )


@contextmanager
def global_writer_authority_lease(
    config: Mapping[str, Any] | None = None,
):
    """Hold a shared control-row lock across the protected application commit.

    Authority transitions update the singleton row and therefore wait for this
    PostgreSQL ``FOR SHARE`` lease. The caller must revalidate the yielded
    decision immediately before committing its application transaction.
    """

    if not global_writer_authority_enabled(config):
        yield GlobalWriterAuthorityLease(
            decision=GlobalWriterAuthorityDecision(
                allowed=True,
                enabled=False,
                reason_code="global_writer_authority_disabled",
            ),
            executor=None,
        )
        return
    runtime = configured_writer_runtime(config)
    if runtime is None:
        yield GlobalWriterAuthorityLease(
            decision=GlobalWriterAuthorityDecision(
                allowed=False,
                enabled=True,
                reason_code="global_writer_runtime_identity_invalid",
            ),
            executor=None,
        )
        return
    if cutover_writer_fence_enabled(config):
        yield GlobalWriterAuthorityLease(
            decision=GlobalWriterAuthorityDecision(
                allowed=False,
                enabled=True,
                reason_code="cutover_writer_fence_enabled",
            ),
            executor=None,
        )
        return

    with global_writer_authority_connection(config) as control_connection:
        with control_connection.begin():
            load_global_writer_authority_state(
                control_connection, lock_for_share=True
            )
            decision = evaluate_global_writer_authority(
                config, executor=control_connection
            )
            yield GlobalWriterAuthorityLease(
                decision=decision,
                executor=control_connection,
            )


def background_global_writer_authority_report(
    component: str,
    config: Mapping[str, Any] | None = None,
    *,
    executor: Any | None = None,
) -> dict[str, Any] | None:
    """Return a payload-free fenced report, or ``None`` when writes may run."""

    decision = evaluate_global_writer_authority(config, executor=executor)
    if decision.allowed:
        return None
    return {
        "contract_version": GLOBAL_WRITER_AUTHORITY_CONTRACT,
        "component": str(component),
        "executed": False,
        "reason_code": decision.reason_code,
        "status": "fenced",
    }


def _validate_runtime(runtime: str) -> str:
    normalized = str(runtime or "").strip().lower()
    if normalized not in SUPPORTED_WRITER_RUNTIMES:
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_runtime_identity_invalid"
        )
    return normalized


def _validate_epoch(expected_epoch: int) -> int:
    if isinstance(expected_epoch, bool):
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_epoch_invalid"
        )
    try:
        normalized = int(expected_epoch)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_epoch_invalid"
        ) from exc
    if normalized < 0:
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_epoch_invalid"
        )
    return normalized


def _cas_result_or_raise(executor: Any, result: Any, expected_epoch: int) -> GlobalWriterAuthorityState:
    if result.rowcount != 1:
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_cas_rejected"
        )
    state = load_global_writer_authority_state(executor)
    if state.epoch != expected_epoch + 1:
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_epoch_postcheck_failed"
        )
    return state


def attest_runtime_fenced(
    executor: Any,
    *,
    runtime: str,
    expected_epoch: int,
) -> GlobalWriterAuthorityState:
    runtime = _validate_runtime(runtime)
    expected_epoch = _validate_epoch(expected_epoch)
    column = "render_fenced" if runtime == "render" else "vercel_fenced"
    result = executor.execute(
        text(
            f"""
            UPDATE public.cutover_global_writer_authority
            SET {column} = TRUE,
                epoch = epoch + 1,
                updated_at = now()
            WHERE authority_key = :authority_key
              AND epoch = :expected_epoch
              AND {column} = FALSE
            """
        ),
        {"authority_key": AUTHORITY_KEY, "expected_epoch": expected_epoch},
    )
    return _cas_result_or_raise(executor, result, expected_epoch)


def bootstrap_global_writer_owner(
    executor: Any,
    *,
    runtime: str,
    expected_epoch: int,
) -> GlobalWriterAuthorityState:
    runtime = _validate_runtime(runtime)
    expected_epoch = _validate_epoch(expected_epoch)
    result = executor.execute(
        text(
            """
            UPDATE public.cutover_global_writer_authority
            SET owner_runtime = :runtime,
                epoch = epoch + 1,
                updated_at = now()
            WHERE authority_key = :authority_key
              AND epoch = :expected_epoch
              AND owner_runtime IS NULL
              AND render_fenced IS TRUE
              AND vercel_fenced IS TRUE
            """
        ),
        {
            "authority_key": AUTHORITY_KEY,
            "expected_epoch": expected_epoch,
            "runtime": runtime,
        },
    )
    return _cas_result_or_raise(executor, result, expected_epoch)


def transfer_global_writer_owner(
    executor: Any,
    *,
    from_runtime: str,
    to_runtime: str,
    expected_epoch: int,
) -> GlobalWriterAuthorityState:
    from_runtime = _validate_runtime(from_runtime)
    to_runtime = _validate_runtime(to_runtime)
    expected_epoch = _validate_epoch(expected_epoch)
    if from_runtime == to_runtime:
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_transfer_target_invalid"
        )
    result = executor.execute(
        text(
            """
            UPDATE public.cutover_global_writer_authority
            SET owner_runtime = :to_runtime,
                epoch = epoch + 1,
                updated_at = now()
            WHERE authority_key = :authority_key
              AND epoch = :expected_epoch
              AND owner_runtime = :from_runtime
              AND render_fenced IS TRUE
              AND vercel_fenced IS TRUE
            """
        ),
        {
            "authority_key": AUTHORITY_KEY,
            "expected_epoch": expected_epoch,
            "from_runtime": from_runtime,
            "to_runtime": to_runtime,
        },
    )
    return _cas_result_or_raise(executor, result, expected_epoch)


def activate_global_writer_owner(
    executor: Any,
    *,
    runtime: str,
    expected_epoch: int,
) -> GlobalWriterAuthorityState:
    runtime = _validate_runtime(runtime)
    expected_epoch = _validate_epoch(expected_epoch)
    own_column = "render_fenced" if runtime == "render" else "vercel_fenced"
    other_column = "vercel_fenced" if runtime == "render" else "render_fenced"
    result = executor.execute(
        text(
            f"""
            UPDATE public.cutover_global_writer_authority
            SET {own_column} = FALSE,
                epoch = epoch + 1,
                updated_at = now()
            WHERE authority_key = :authority_key
              AND epoch = :expected_epoch
              AND owner_runtime = :runtime
              AND {own_column} IS TRUE
              AND {other_column} IS TRUE
            """
        ),
        {
            "authority_key": AUTHORITY_KEY,
            "expected_epoch": expected_epoch,
            "runtime": runtime,
        },
    )
    return _cas_result_or_raise(executor, result, expected_epoch)


__all__ = [
    "AUTHORITY_KEY",
    "AUTHORITY_TABLE",
    "CONTROL_CONNECT_TIMEOUT_SECONDS",
    "CONTROL_IDLE_TRANSACTION_TIMEOUT_MS",
    "CONTROL_LOCK_TIMEOUT_MS",
    "CONTROL_POOL_TIMEOUT_SECONDS",
    "CONTROL_STATEMENT_TIMEOUT_MS",
    "GlobalWriterAuthorityDecision",
    "GlobalWriterAuthorityLease",
    "GlobalWriterAuthorityState",
    "GlobalWriterAuthorityTransitionError",
    "activate_global_writer_owner",
    "attest_runtime_fenced",
    "background_global_writer_authority_report",
    "bootstrap_global_writer_owner",
    "evaluate_global_writer_authority",
    "global_writer_authority_connection",
    "global_writer_authority_lease",
    "load_global_writer_authority_state",
    "transfer_global_writer_owner",
]
