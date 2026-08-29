"""Render pre-deploy schema gate for the cutover writer fence.

An active Render service may still run the normal Alembic upgrade command.
Render standby is different: it is a rollback compute pointed at the already
migrated Neon database.  Its pre-deploy hook therefore verifies the exact
approved revision and schema inside an explicitly read-only transaction.  It
never applies a revision or acquires writer ownership.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cutover_writer_fence import (
    background_writer_fence_report,
    cutover_writer_fence_enabled,
)


PREDEPLOY_COMPONENT = "render_predeploy_migration"
PREDEPLOY_CONTRACT = "chatboc.render_predeploy.v1"
STANDBY_VERIFY_CONTRACT = "chatboc.render_standby_verify.v1"
STANDBY_SCHEMA_ACTION_ENV = "CHATBOC_RENDER_STANDBY_SCHEMA_ACTION"
STANDBY_SCHEMA_ACTION = "verify-only"


def _render_standby_mode_enabled(environ: Mapping[str, str]) -> bool:
    raw_value = environ.get("CHATBOC_RENDER_STANDBY_MODE")
    if raw_value is None:
        return False
    normalized = str(raw_value).strip().lower()
    if normalized in {"0", "false", "no", "off"}:
        return False
    return True


def _blocked(reason: str) -> int:
    print(
        json.dumps(
            {
                "contract": PREDEPLOY_CONTRACT,
                "status": "blocked",
                "reason": reason,
            },
            sort_keys=True,
        )
    )
    return 2


def _required_value(environ: Mapping[str, str], name: str, reason: str) -> str:
    value = str(environ.get(name) or "").strip()
    if not value:
        raise ValueError(reason)
    return value


def _verify_render_standby(
    *,
    database_url: str,
    expected_project_id: str,
    expected_branch_id: str,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Verify the rollback compute's Neon schema without mutating the DB.

    Imports stay lazy so a fenced, non-standby deployment can still attest the
    fence without loading SQLAlchemy, Alembic, or a database driver.
    """

    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    from scripts.apply_neon_cutover_migrations import (
        EXPECTED_INBOUND_FIFO_COLUMNS,
        EXPECTED_INBOUND_FIFO_INDEX,
        GLOBAL_WRITER_AUTHORITY_REVISION,
        CutoverMigrationFailure,
        _demo_contract,
        _global_writer_authority_contract,
        _idempotency_contract,
        _index_contract,
        _load_exact_migration_plan,
        _require_base_tables,
        _require_revision,
    )
    from scripts.preflight_neon_cutover import (
        PreflightFailure,
        _validate_neon_direct_url,
        _validate_neon_identity_value,
    )

    parsed = _validate_neon_direct_url(database_url)
    expected_project = _validate_neon_identity_value(
        expected_project_id,
        kind="project",
    )
    expected_branch = _validate_neon_identity_value(
        expected_branch_id,
        kind="branch",
    )
    root = project_root or Path(__file__).resolve().parents[1]
    plan = _load_exact_migration_plan(root)
    engine = create_engine(
        parsed.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
    )
    transaction = None
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                # Must be the first application statement in the transaction.
                # PostgreSQL will reject any subsequent DDL/DML even if a
                # future schema assertion is accidentally made mutating.
                connection.execute(text("SET TRANSACTION READ ONLY"))
                connection.execute(text("SET LOCAL statement_timeout = '30s'"))
                connection.execute(text("SET LOCAL lock_timeout = '2s'"))
                read_only = (
                    str(
                        connection.execute(
                            text("SHOW transaction_read_only")
                        ).scalar_one()
                    ).lower()
                    == "on"
                )
                if not read_only:
                    raise PreflightFailure("database_transaction_not_read_only")

                identity = connection.execute(
                    text(
                        """
                        SELECT
                            current_setting('neon.project_id', true) AS project_id,
                            current_setting('neon.branch_id', true) AS branch_id
                        """
                    )
                ).mappings().one()
                actual_project = str(identity["project_id"] or "").strip().lower()
                actual_branch = str(identity["branch_id"] or "").strip().lower()
                if actual_project != expected_project:
                    raise PreflightFailure("database_neon_project_mismatch")
                if actual_branch != expected_branch:
                    raise PreflightFailure("database_neon_branch_mismatch")

                revision = _require_revision(
                    connection,
                    GLOBAL_WRITER_AUTHORITY_REVISION,
                )
                _require_base_tables(connection)
                demo = _demo_contract(connection)
                idempotency = _idempotency_contract(connection)
                fifo = _index_contract(
                    connection,
                    table_name="whatsapp_inbound_turn",
                    index_name=EXPECTED_INBOUND_FIFO_INDEX,
                )
                global_writer_authority = _global_writer_authority_contract(
                    connection,
                    require_bootstrap=False,
                )
                # This is deliberately structural. Live standby verification
                # must not assume mutable business rows or an empty receipt
                # table; only the exact revision and database contracts matter.
                if not all(demo.values()):
                    raise CutoverMigrationFailure(
                        "database_demo_survey_contract_invalid"
                    )
                if not all(
                    idempotency[key]
                    for key in (
                        "table_present",
                        "expected_columns_present",
                        "expected_indexes_present",
                        "expected_constraints_present",
                    )
                ):
                    raise CutoverMigrationFailure(
                        "database_idempotency_contract_invalid"
                    )
                expected_fifo = {
                    "columns": EXPECTED_INBOUND_FIFO_COLUMNS,
                    "is_unique": False,
                    "is_valid": True,
                    "is_ready": True,
                    "is_unfiltered": True,
                    "has_plain_columns": True,
                    "has_no_included_columns": True,
                }
                if fifo != expected_fifo:
                    raise CutoverMigrationFailure(
                        "database_inbound_fifo_index_invalid"
                    )
                if not all(
                    global_writer_authority[key]
                    for key in (
                        "table_present",
                        "expected_columns_present",
                        "expected_constraints_present",
                        "singleton_state_valid",
                        "required_state_valid",
                    )
                ):
                    raise CutoverMigrationFailure(
                        "database_global_writer_authority_contract_invalid"
                    )
                schema = {
                    "demo_survey": demo,
                    "idempotency": {
                        key: idempotency[key]
                        for key in (
                            "table_present",
                            "expected_columns_present",
                            "expected_indexes_present",
                            "expected_constraints_present",
                        )
                    },
                    "inbound_fifo_index": fifo,
                    "global_writer_authority": global_writer_authority,
                }
            finally:
                # Roll back even though PostgreSQL enforced read-only.  A
                # standby pre-deploy must never leave an open transaction or
                # rely on commit semantics.
                transaction.rollback()
                transaction = None
    finally:
        if transaction is not None:
            transaction.rollback()
        engine.dispose()

    return {
        "contract": STANDBY_VERIFY_CONTRACT,
        "status": "verified",
        "schema_action": "verify-only",
        "ready": True,
        "writes_attempted": False,
        "writer_ownership_acquired": False,
        "database": {
            "provider": "neon",
            "connection_mode": "direct",
            "identity_verified": True,
            "transaction_read_only": True,
        },
        "migration": {
            "current_revision": revision,
            "expected_revision": GLOBAL_WRITER_AUTHORITY_REVISION,
            "at_exact_target": revision == GLOBAL_WRITER_AUTHORITY_REVISION,
            "local_graph_fingerprint_sha256": plan.graph_fingerprint_sha256,
        },
        "schema": {
            "ready": True,
            "contracts": schema,
        },
    }


def main(
    *,
    run_command: Callable[..., object] = subprocess.run,
    command: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    verify_standby: Callable[..., Mapping[str, Any]] = _verify_render_standby,
) -> int:
    """Run active migrations, or verify an exact read-only standby schema."""

    runtime_env = os.environ if environ is None else environ
    standby_mode = _render_standby_mode_enabled(runtime_env)

    if standby_mode:
        schema_action = str(
            runtime_env.get(STANDBY_SCHEMA_ACTION_ENV) or ""
        ).strip().lower()
        if not schema_action:
            return _blocked("render_standby_schema_action_missing")
        if schema_action != STANDBY_SCHEMA_ACTION:
            return _blocked("render_standby_schema_action_not_verify_only")
        try:
            database_url = _required_value(
                runtime_env,
                "MIGRATIONS_DATABASE_URL",
                "migrations_database_url_missing",
            )
            expected_project_id = _required_value(
                runtime_env,
                "EXPECTED_NEON_PROJECT_ID",
                "expected_neon_project_id_missing",
            )
            expected_branch_id = _required_value(
                runtime_env,
                "EXPECTED_NEON_BRANCH_ID",
                "expected_neon_branch_id_missing",
            )
        except ValueError as exc:
            return _blocked(str(exc))

        try:
            report = verify_standby(
                database_url=database_url,
                expected_project_id=expected_project_id,
                expected_branch_id=expected_branch_id,
            )
        except Exception as exc:
            # Stable error codes from the verification helpers are safe to
            # expose.  Never serialize exception messages or connection data.
            reason = str(getattr(exc, "reason_code", "") or "").strip()
            return _blocked(reason or "standby_schema_verification_failed")
        print(json.dumps(dict(report), sort_keys=True))
        return 0

    if cutover_writer_fence_enabled(runtime_env):
        print(json.dumps(background_writer_fence_report(PREDEPLOY_COMPONENT), sort_keys=True))
        return 0

    migration_command = list(
        command or (sys.executable, "-m", "flask", "db", "upgrade")
    )
    completed = run_command(migration_command, check=False)
    return int(getattr(completed, "returncode", 1))


if __name__ == "__main__":
    raise SystemExit(main())
