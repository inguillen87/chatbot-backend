"""Read-only, redacted preflight for the Chatboc Neon cutover.

The command deliberately accepts a database URL only through an environment
variable.  It validates that the target is a direct Neon PostgreSQL endpoint,
starts a read-only transaction, and emits aggregate/schema evidence without
printing credentials or application rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, URL, make_url


CONTRACT_VERSION = "chatboc.neon_cutover_preflight.v1"
DEFAULT_ENVIRONMENT_VARIABLE = "MIGRATIONS_DATABASE_URL"
DEFAULT_PROJECT_ID_ENVIRONMENT_VARIABLE = "EXPECTED_NEON_PROJECT_ID"
DEFAULT_BRANCH_ID_ENVIRONMENT_VARIABLE = "EXPECTED_NEON_BRANCH_ID"
ENVIRONMENT_VARIABLE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
NEON_PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
NEON_BRANCH_ID_PATTERN = re.compile(r"^br-[a-z0-9-]{3,63}$")
REQUIRED_HEAD_TABLES = (
    "cutover_global_writer_authority",
    "demo_survey_participation",
    "municipio_chat_idempotency_receipt",
)
EXPECTED_DEMO_TRIGGER = "trg_demo_survey_participation_immutable"
EXPECTED_DEMO_INDEXES = {
    "ix_demo_survey_participation_slug_option",
    "ix_demo_survey_participation_slug_order",
}
EXPECTED_CHAT_INDEXES = {
    "ix_municipio_chat_idempotency_tenant_created",
}
EXPECTED_GLOBAL_WRITER_AUTHORITY_CONSTRAINTS = {
    "ck_cutover_global_writer_authority_epoch",
    "ck_cutover_global_writer_authority_owner",
    "ck_cutover_global_writer_authority_safe_state",
    "ck_cutover_global_writer_authority_singleton",
    "pk_cutover_global_writer_authority",
}
LEGACY_REPAIR_TICKET_IDS = (322, 344, 347)


class PreflightFailure(RuntimeError):
    """A safe, stable reason code for an intentionally blocked preflight."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_environment_variable_name(name: str) -> str:
    normalized = str(name or "").strip()
    if not ENVIRONMENT_VARIABLE_PATTERN.fullmatch(normalized):
        raise PreflightFailure("database_environment_variable_invalid")
    return normalized


def _validate_neon_direct_url(raw_url: str) -> URL:
    try:
        parsed = make_url(str(raw_url or "").strip())
    except Exception as exc:
        raise PreflightFailure("database_url_invalid") from exc

    if parsed.get_backend_name() != "postgresql":
        raise PreflightFailure("database_backend_not_postgresql")

    host = str(parsed.host or "").strip().lower().rstrip(".")
    if not host.endswith(".neon.tech"):
        raise PreflightFailure("database_provider_not_neon")
    if "-pooler." in host:
        raise PreflightFailure("database_connection_not_direct")

    sslmode = str(parsed.query.get("sslmode") or "").strip().lower()
    if sslmode not in {"require", "verify-ca", "verify-full"}:
        raise PreflightFailure("database_tls_not_required")
    if not parsed.database:
        raise PreflightFailure("database_name_missing")
    if not parsed.username:
        raise PreflightFailure("database_username_missing")
    return parsed


def _validate_neon_identity_value(value: str, *, kind: str) -> str:
    normalized = str(value or "").strip().lower()
    pattern = (
        NEON_PROJECT_ID_PATTERN
        if kind == "project"
        else NEON_BRANCH_ID_PATTERN
        if kind == "branch"
        else None
    )
    if pattern is None or not pattern.fullmatch(normalized):
        raise PreflightFailure(f"expected_neon_{kind}_id_invalid")
    return normalized


def _load_migration_directory(project_root: Path) -> ScriptDirectory:
    config = AlembicConfig(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    return ScriptDirectory.from_config(config)


def _migration_state(
    script: ScriptDirectory,
    current_revisions: Sequence[str],
) -> dict[str, Any]:
    expected_heads = sorted(script.get_heads())
    current = sorted({str(value).strip() for value in current_revisions if str(value).strip()})
    if len(expected_heads) != 1:
        raise PreflightFailure("local_migration_heads_ambiguous")
    if not current:
        raise PreflightFailure("database_migration_revision_missing")

    for revision in current:
        try:
            script.get_revision(revision)
        except Exception as exc:
            raise PreflightFailure("database_migration_revision_unknown") from exc

    if current == expected_heads:
        pending: list[str] = []
    else:
        try:
            revisions = script.iterate_revisions(expected_heads, current)
            pending = [item.revision for item in reversed(list(revisions))]
        except Exception as exc:
            raise PreflightFailure("database_migration_graph_diverged") from exc
        if not pending:
            raise PreflightFailure("database_migration_graph_diverged")

    return {
        "current_revisions": current,
        "expected_heads": expected_heads,
        "pending_revisions": pending,
        "at_head": not pending,
    }


def _quoted_public_table(connection: Connection, table_name: str) -> str:
    preparer = connection.dialect.identifier_preparer
    return f"{preparer.quote_identifier('public')}.{preparer.quote_identifier(table_name)}"


def _public_table_names(connection: Connection) -> list[str]:
    rows = connection.execute(
        text(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """
        )
    ).scalars()
    return [str(value) for value in rows]


def _exact_table_counts(
    connection: Connection,
    table_names: Sequence[str],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table_name in table_names:
        statement = text(f"SELECT count(*) FROM {_quoted_public_table(connection, table_name)}")
        counts[table_name] = int(connection.execute(statement).scalar_one())
    return counts


def _index_names(connection: Connection, table_name: str) -> set[str]:
    return {
        str(value)
        for value in connection.execute(
            text(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname = 'public' AND tablename = :table_name
                """
            ),
            {"table_name": table_name},
        ).scalars()
    }


def _critical_schema_state(
    connection: Connection,
    counts: Mapping[str, int],
) -> dict[str, Any]:
    tables = {
        table_name: {
            "present": table_name in counts,
            "rows": counts.get(table_name),
        }
        for table_name in REQUIRED_HEAD_TABLES
    }

    trigger_present = False
    demo_indexes: set[str] = set()
    if "demo_survey_participation" in counts:
        trigger_present = bool(
            connection.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_trigger trigger
                        JOIN pg_class relation ON relation.oid = trigger.tgrelid
                        JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
                        WHERE namespace.nspname = 'public'
                          AND relation.relname = 'demo_survey_participation'
                          AND trigger.tgname = :trigger_name
                          AND NOT trigger.tgisinternal
                    )
                    """
                ),
                {"trigger_name": EXPECTED_DEMO_TRIGGER},
            ).scalar_one()
        )
        demo_indexes = _index_names(connection, "demo_survey_participation")

    chat_indexes = (
        _index_names(connection, "municipio_chat_idempotency_receipt")
        if "municipio_chat_idempotency_receipt" in counts
        else set()
    )

    global_writer_authority_valid = False
    if "cutover_global_writer_authority" in counts:
        rows = list(
            connection.execute(
                text(
                    """
                    SELECT authority_key, owner_runtime, epoch,
                           render_fenced, vercel_fenced
                    FROM cutover_global_writer_authority
                    ORDER BY authority_key
                    """
                )
            ).mappings()
        )
        constraints = _constraint_names(
            connection,
            "cutover_global_writer_authority",
        )
        if len(rows) == 1:
            row = dict(rows[0])
            owner = row.get("owner_runtime")
            epoch = row.get("epoch")
            render_fenced = row.get("render_fenced")
            vercel_fenced = row.get("vercel_fenced")
            ownership_safe = (
                owner is None
                and render_fenced is True
                and vercel_fenced is True
            ) or (owner == "render" and vercel_fenced is True) or (
                owner == "vercel" and render_fenced is True
            )
            global_writer_authority_valid = bool(
                row.get("authority_key") == "primary"
                and isinstance(epoch, int)
                and not isinstance(epoch, bool)
                and epoch >= 0
                and isinstance(render_fenced, bool)
                and isinstance(vercel_fenced, bool)
                and ownership_safe
                and EXPECTED_GLOBAL_WRITER_AUTHORITY_CONSTRAINTS.issubset(
                    constraints
                )
            )

    repair = {
        "status": "schema_unavailable",
        "rows_present": None,
        "rows_scoped_to_junin": None,
    }
    if {"municipio_ticket", "tenant_profile"}.issubset(counts):
        repair_row = connection.execute(
            text(
                """
                SELECT
                    count(*) AS rows_present,
                    count(*) FILTER (WHERE lower(tenant.slug) = 'junin') AS rows_scoped_to_junin
                FROM municipio_ticket ticket
                LEFT JOIN tenant_profile tenant ON tenant.id = ticket.tenant_id
                WHERE ticket.id IN (322, 344, 347)
                """
            )
        ).mappings().one()
        rows_present = int(repair_row["rows_present"])
        rows_scoped = int(repair_row["rows_scoped_to_junin"])
        repair = {
            "status": (
                "not_applicable"
                if rows_present == 0
                else "repaired"
                if rows_present == len(LEGACY_REPAIR_TICKET_IDS)
                and rows_scoped == len(LEGACY_REPAIR_TICKET_IDS)
                else "incomplete"
            ),
            "rows_present": rows_present,
            "rows_scoped_to_junin": rows_scoped,
        }

    checks = {
        "required_tables_present": all(item["present"] for item in tables.values()),
        "demo_immutability_trigger_present": trigger_present,
        "demo_indexes_present": EXPECTED_DEMO_INDEXES.issubset(demo_indexes),
        "chat_idempotency_indexes_present": EXPECTED_CHAT_INDEXES.issubset(chat_indexes),
        "global_writer_authority_valid": global_writer_authority_valid,
        "legacy_ticket_scope_repair_valid": repair["status"] in {"not_applicable", "repaired"},
    }
    return {
        "checks": checks,
        "ready": all(checks.values()),
        "tables": tables,
        "legacy_ticket_scope_repair": repair,
    }


def _constraint_names(connection: Connection, table_name: str) -> set[str]:
    return {
        str(value)
        for value in connection.execute(
            text(
                """
                SELECT constraint_name
                FROM information_schema.table_constraints
                WHERE table_schema = 'public' AND table_name = :table_name
                """
            ),
            {"table_name": table_name},
        ).scalars()
    }


def _wal_position(connection: Connection) -> dict[str, Any]:
    """Return a comparable WAL position for primaries and read replicas."""

    in_recovery = bool(
        connection.execute(text("SELECT pg_is_in_recovery()"))
        .scalar_one()
    )
    wal_function = (
        "pg_last_wal_replay_lsn()" if in_recovery else "pg_current_wal_lsn()"
    )
    wal_lsn_value = connection.execute(
        text(f"SELECT {wal_function}::text")
    ).scalar_one_or_none()
    wal_lsn = str(wal_lsn_value or "").strip()
    if not wal_lsn:
        raise PreflightFailure("database_wal_lsn_missing")
    return {
        "wal_lsn": wal_lsn,
        "wal_lsn_source": "replay" if in_recovery else "current",
        "in_recovery": in_recovery,
    }


def _database_state(
    connection: Connection,
    *,
    expected_project_id: str,
    expected_branch_id: str,
) -> dict[str, Any]:
    connection.execute(text("SET TRANSACTION READ ONLY"))
    connection.execute(text("SET LOCAL statement_timeout = '30s'"))
    connection.execute(text("SET LOCAL lock_timeout = '2s'"))

    transaction_read_only = str(
        connection.execute(text("SHOW transaction_read_only")).scalar_one()
    ).lower() == "on"
    if not transaction_read_only:
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
    actual_project_id = str(identity["project_id"] or "").strip().lower()
    actual_branch_id = str(identity["branch_id"] or "").strip().lower()
    if actual_project_id != expected_project_id:
        raise PreflightFailure("database_neon_project_mismatch")
    if actual_branch_id != expected_branch_id:
        raise PreflightFailure("database_neon_branch_mismatch")

    table_names = _public_table_names(connection)
    if "alembic_version" not in table_names:
        raise PreflightFailure("database_migration_table_missing")
    counts = _exact_table_counts(connection, table_names)
    revisions = [
        str(value)
        for value in connection.execute(
            text("SELECT version_num FROM alembic_version ORDER BY version_num")
        ).scalars()
    ]

    wal = _wal_position(connection)
    server_version_num = str(
        connection.execute(text("SHOW server_version_num")).scalar_one()
    )
    return {
        "transaction_read_only": transaction_read_only,
        "neon_identity": {
            "project_id": actual_project_id,
            "branch_id": actual_branch_id,
        },
        "server_version_num": server_version_num,
        **wal,
        "current_revisions": revisions,
        "table_count": len(table_names),
        "exact_row_count": sum(counts.values()),
        "row_count_inventory_fingerprint_sha256": _canonical_sha256(counts),
        "evidence_scope": "destination_inventory_only",
        "content_parity_certified": False,
        "critical_schema": _critical_schema_state(connection, counts),
    }


def run_preflight(
    *,
    database_url: str,
    project_root: Path,
    expected_project_id: str,
    expected_branch_id: str,
) -> tuple[dict[str, Any], int]:
    parsed = _validate_neon_direct_url(database_url)
    expected_project_id = _validate_neon_identity_value(
        expected_project_id,
        kind="project",
    )
    expected_branch_id = _validate_neon_identity_value(
        expected_branch_id,
        kind="branch",
    )
    script = _load_migration_directory(project_root)
    # Neon emits the generic ``postgresql://`` scheme.  Be explicit about the
    # supported psycopg v3 driver so this safety-critical command does not
    # depend on whichever PostgreSQL driver happens to win SQLAlchemy's local
    # default resolution (and so it behaves the same in CI and on Windows).
    engine_url = parsed.set(drivername="postgresql+psycopg")
    engine = create_engine(engine_url, pool_pre_ping=True, pool_recycle=300)
    try:
        with engine.connect() as connection:
            with connection.begin():
                database = _database_state(
                    connection,
                    expected_project_id=expected_project_id,
                    expected_branch_id=expected_branch_id,
                )
    finally:
        engine.dispose()

    migration = _migration_state(script, database.pop("current_revisions"))
    at_head = bool(migration["at_head"])
    critical_ready = bool(database["critical_schema"]["ready"])
    if at_head and critical_ready:
        status = "ready"
        exit_code = 0
    elif not at_head:
        status = "migration_required"
        exit_code = 3
    else:
        status = "blocked"
        exit_code = 4

    host = str(parsed.host or "").lower().rstrip(".")
    payload = {
        "contract_version": CONTRACT_VERSION,
        "status": status,
        "ready": status == "ready",
        "target": {
            "provider": "neon",
            "connection_mode": "direct",
            "tls_required": True,
            "host_fingerprint_sha256": hashlib.sha256(host.encode("utf-8")).hexdigest(),
        },
        "migration": migration,
        "database": database,
    }
    return payload, exit_code


def _failure_payload(reason_code: str, *, error_type: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "status": "blocked",
        "ready": False,
        "reason_code": reason_code,
    }
    if error_type:
        payload["error_type"] = error_type
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--environment-variable",
        default=DEFAULT_ENVIRONMENT_VARIABLE,
        help="Name of the environment variable holding the direct Neon URL.",
    )
    parser.add_argument(
        "--project-id-environment-variable",
        default=DEFAULT_PROJECT_ID_ENVIRONMENT_VARIABLE,
        help="Name of the environment variable holding the expected Neon project ID.",
    )
    parser.add_argument(
        "--branch-id-environment-variable",
        default=DEFAULT_BRANCH_ID_ENVIRONMENT_VARIABLE,
        help="Name of the environment variable holding the expected Neon branch ID.",
    )
    args = parser.parse_args(argv)

    try:
        environment_variable = _validate_environment_variable_name(
            args.environment_variable
        )
        database_url = os.environ.get(environment_variable, "")
        if not database_url:
            raise PreflightFailure("database_environment_variable_missing")
        project_environment_variable = _validate_environment_variable_name(
            args.project_id_environment_variable
        )
        branch_environment_variable = _validate_environment_variable_name(
            args.branch_id_environment_variable
        )
        expected_project_id = os.environ.get(project_environment_variable, "")
        expected_branch_id = os.environ.get(branch_environment_variable, "")
        if not expected_project_id:
            raise PreflightFailure("expected_neon_project_id_missing")
        if not expected_branch_id:
            raise PreflightFailure("expected_neon_branch_id_missing")
        payload, exit_code = run_preflight(
            database_url=database_url,
            project_root=Path(__file__).resolve().parents[1],
            expected_project_id=expected_project_id,
            expected_branch_id=expected_branch_id,
        )
    except PreflightFailure as exc:
        payload = _failure_payload(exc.reason_code)
        exit_code = 2
    except Exception as exc:  # Never serialize provider or connection details.
        payload = _failure_payload(
            "database_preflight_failed",
            error_type=type(exc).__name__,
        )
        exit_code = 2

    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
