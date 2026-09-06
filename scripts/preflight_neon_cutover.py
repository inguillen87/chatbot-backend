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
# This is the reviewed cutover boundary, not merely whichever migration was
# added most recently.  Both read-only preflight and the writer use this single
# allowlist value so a new or branched Alembic head fails closed until reviewed.
REVIEWED_MIGRATION_HEAD = "20260906_flask_sessions_v1"
REQUIRED_HEAD_TABLES = (
    "cutover_global_writer_authority",
    "demo_survey_participation",
    "flask_sessions",
    "inbox_ticket_artifact",
    "municipio_ticket_handoff_event",
    "municipio_ticket_reply_event",
    "municipio_chat_idempotency_receipt",
    "tenant_blueprint_application",
    "tenant_blueprint_launch_receipt",
    "tenant_ticket_reply_event",
    "territorial_geocoding_attempt",
    "territorial_geocoding_job",
    "territorial_geocoding_review",
    "territorial_geocoding_sync_receipt",
    "whatsapp_contact_state",
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
TERRITORIAL_SCHEMA_REQUIREMENTS: dict[str, dict[str, Any]] = {
    "territorial_geocoding_job": {
        "constraints": {
            "ck_territorial_geocoding_job_status": "CHECK",
            "ck_territorial_geocoding_job_attempt_count": "CHECK",
            "ck_territorial_geocoding_job_fingerprint": "CHECK",
            "ck_territorial_geocoding_job_address_digest": "CHECK",
            "ck_territorial_geocoding_job_jurisdiction_digest": "CHECK",
            "uq_territorial_geocoding_job_candidate": "UNIQUE",
        },
        "constraint_columns": {
            "uq_territorial_geocoding_job_candidate": (
                "tenant_id",
                "candidate_fingerprint",
            ),
        },
        "primary_key": ("id",),
        "indexes": {
            "ix_territorial_geocoding_job_tenant_id": (
                ("tenant_id",),
                False,
            ),
            "ix_territorial_geocoding_job_tenant_status_created": (
                ("tenant_id", "status", "created_at", "id"),
                False,
            ),
        },
        "foreign_keys": (
            (("tenant_id",), "public", "tenant_profile", ("id",), "CASCADE"),
        ),
    },
    "territorial_geocoding_attempt": {
        "constraints": {
            "ck_territorial_geocoding_attempt_status": "CHECK",
            "ck_territorial_geocoding_attempt_number": "CHECK",
            "ck_territorial_geocoding_attempt_request_digest": "CHECK",
            "ck_territorial_geocoding_attempt_result_digest": "CHECK",
            "ck_territorial_geocoding_attempt_write_state": "CHECK",
            "uq_territorial_geocoding_attempt_request": "UNIQUE",
            "uq_territorial_geocoding_attempt_number": "UNIQUE",
        },
        "constraint_columns": {
            "uq_territorial_geocoding_attempt_request": (
                "job_id",
                "request_digest",
            ),
            "uq_territorial_geocoding_attempt_number": (
                "job_id",
                "attempt_number",
            ),
        },
        "primary_key": ("id",),
        "indexes": {
            "ix_territorial_geocoding_attempt_job_id": (("job_id",), False),
            "ix_territorial_geocoding_attempt_tenant_id": (
                ("tenant_id",),
                False,
            ),
            "ix_territorial_geocoding_attempt_tenant_created": (
                ("tenant_id", "created_at", "id"),
                False,
            ),
        },
        "foreign_keys": (
            (
                ("job_id",),
                "public",
                "territorial_geocoding_job",
                ("id",),
                "CASCADE",
            ),
            (("tenant_id",), "public", "tenant_profile", ("id",), "CASCADE"),
        ),
    },
    "territorial_geocoding_review": {
        "constraints": {
            "ck_territorial_geocoding_review_decision": "CHECK",
            "ck_territorial_geocoding_review_job_status": "CHECK",
            "ck_territorial_geocoding_review_digests": "CHECK",
            "ck_territorial_geocoding_review_no_coordinate_write": "CHECK",
            "uq_territorial_geocoding_review_idempotency": "UNIQUE",
        },
        "constraint_columns": {
            "uq_territorial_geocoding_review_idempotency": (
                "tenant_id",
                "job_id",
                "idempotency_key_hash",
            ),
        },
        "primary_key": ("id",),
        "indexes": {
            "ix_territorial_geocoding_review_job_id": (("job_id",), False),
            "ix_territorial_geocoding_review_tenant_id": (
                ("tenant_id",),
                False,
            ),
            "ix_territorial_geocoding_review_reviewer_user_id": (
                ("reviewer_user_id",),
                False,
            ),
            "ix_territorial_geocoding_review_tenant_job_created": (
                ("tenant_id", "job_id", "created_at", "id"),
                False,
            ),
        },
        "foreign_keys": (
            (
                ("job_id",),
                "public",
                "territorial_geocoding_job",
                ("id",),
                "CASCADE",
            ),
            (("tenant_id",), "public", "tenant_profile", ("id",), "CASCADE"),
            (("reviewer_user_id",), "public", "user", ("id",), "RESTRICT"),
        ),
    },
    "territorial_geocoding_sync_receipt": {
        "constraints": {
            "ck_territorial_geocoding_sync_digests": "CHECK",
            "uq_territorial_geocoding_sync_idempotency": "UNIQUE",
        },
        "constraint_columns": {
            "uq_territorial_geocoding_sync_idempotency": (
                "tenant_id",
                "idempotency_key_hash",
            ),
        },
        "primary_key": ("id",),
        "indexes": {
            "ix_territorial_geocoding_sync_receipt_tenant_id": (
                ("tenant_id",),
                False,
            ),
            "ix_territorial_geocoding_sync_receipt_actor_user_id": (
                ("actor_user_id",),
                False,
            ),
            "ix_territorial_geocoding_sync_tenant_created": (
                ("tenant_id", "created_at", "id"),
                False,
            ),
        },
        "foreign_keys": (
            (("actor_user_id",), "public", "user", ("id",), "RESTRICT"),
            (("tenant_id",), "public", "tenant_profile", ("id",), "CASCADE"),
        ),
    },
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


def _single_migration_head(script: ScriptDirectory) -> str:
    """Return the reviewed sole head or fail closed on graph drift."""

    expected_heads = sorted({str(value).strip() for value in script.get_heads()})
    if len(expected_heads) != 1 or not expected_heads[0]:
        raise PreflightFailure("local_migration_heads_ambiguous")
    actual_head = expected_heads[0]
    if actual_head != REVIEWED_MIGRATION_HEAD:
        raise PreflightFailure("local_migration_head_not_allowlisted")
    return actual_head


def _migration_state(
    script: ScriptDirectory,
    current_revisions: Sequence[str],
) -> dict[str, Any]:
    expected_heads = [_single_migration_head(script)]
    current = sorted({str(value).strip() for value in current_revisions if str(value).strip()})
    if not current:
        raise PreflightFailure("database_migration_revision_missing")
    if len(current) != 1:
        raise PreflightFailure("database_migration_heads_ambiguous")

    for revision in current:
        try:
            resolved = script.get_revision(revision)
        except Exception as exc:
            raise PreflightFailure("database_migration_revision_unknown") from exc
        if resolved is None:
            raise PreflightFailure("database_migration_revision_unknown")

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


def _constraint_inventory(
    connection: Connection,
    table_name: str,
) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    rows = connection.execute(
        text(
            """
            SELECT constraints.constraint_name,
                   constraints.constraint_type,
                   columns.column_name,
                   columns.ordinal_position
            FROM information_schema.table_constraints constraints
            LEFT JOIN information_schema.key_column_usage columns
              ON columns.constraint_catalog = constraints.constraint_catalog
             AND columns.constraint_schema = constraints.constraint_schema
             AND columns.constraint_name = constraints.constraint_name
             AND columns.table_schema = constraints.table_schema
             AND columns.table_name = constraints.table_name
            WHERE constraints.table_schema = 'public'
              AND constraints.table_name = :table_name
            ORDER BY constraints.constraint_name, columns.ordinal_position
            """
        ),
        {"table_name": table_name},
    ).mappings()
    constraint_types: dict[str, str] = {}
    constraint_columns: dict[str, list[str]] = {}
    for raw_row in rows:
        row = dict(raw_row)
        constraint_name = str(row["constraint_name"])
        constraint_types[constraint_name] = str(row["constraint_type"]).upper()
        column_name = row.get("column_name")
        if column_name is not None:
            constraint_columns.setdefault(constraint_name, []).append(
                str(column_name)
            )
    return constraint_types, {
        name: tuple(columns) for name, columns in constraint_columns.items()
    }


def _index_inventory(
    connection: Connection,
    table_name: str,
) -> dict[str, tuple[tuple[str, ...], bool]]:
    rows = connection.execute(
        text(
            """
            SELECT index_relation.relname AS index_name,
                   index_metadata.indisunique AS is_unique,
                   attribute.attname AS column_name,
                   index_key.ordinality AS ordinal_position
            FROM pg_catalog.pg_class table_relation
            JOIN pg_catalog.pg_namespace table_namespace
              ON table_namespace.oid = table_relation.relnamespace
            JOIN pg_catalog.pg_index index_metadata
              ON index_metadata.indrelid = table_relation.oid
            JOIN pg_catalog.pg_class index_relation
              ON index_relation.oid = index_metadata.indexrelid
            CROSS JOIN LATERAL unnest(index_metadata.indkey)
              WITH ORDINALITY AS index_key(attribute_number, ordinality)
            JOIN pg_catalog.pg_attribute attribute
              ON attribute.attrelid = table_relation.oid
             AND attribute.attnum = index_key.attribute_number
            WHERE table_namespace.nspname = 'public'
              AND table_relation.relname = :table_name
              AND index_key.ordinality <= index_metadata.indnkeyatts
            ORDER BY index_relation.relname, index_key.ordinality
            """
        ),
        {"table_name": table_name},
    ).mappings()
    inventory: dict[str, dict[str, Any]] = {}
    for raw_row in rows:
        row = dict(raw_row)
        index_name = str(row["index_name"])
        item = inventory.setdefault(
            index_name,
            {"columns": [], "is_unique": bool(row["is_unique"])},
        )
        item["columns"].append(str(row["column_name"]))
    return {
        name: (tuple(item["columns"]), bool(item["is_unique"]))
        for name, item in inventory.items()
    }


def _foreign_key_inventory(
    connection: Connection,
    table_name: str,
) -> set[tuple[tuple[str, ...], str, str, tuple[str, ...], str]]:
    rows = connection.execute(
        text(
            """
            SELECT foreign_key.constraint_name,
                   source_column.column_name AS source_column,
                   referenced_column.table_schema AS referenced_schema,
                   referenced_column.table_name AS referenced_table,
                   referenced_column.column_name AS referenced_column,
                   referential.delete_rule,
                   source_column.ordinal_position
            FROM information_schema.table_constraints foreign_key
            JOIN information_schema.key_column_usage source_column
              ON source_column.constraint_catalog = foreign_key.constraint_catalog
             AND source_column.constraint_schema = foreign_key.constraint_schema
             AND source_column.constraint_name = foreign_key.constraint_name
             AND source_column.table_schema = foreign_key.table_schema
             AND source_column.table_name = foreign_key.table_name
            JOIN information_schema.referential_constraints referential
              ON referential.constraint_catalog = foreign_key.constraint_catalog
             AND referential.constraint_schema = foreign_key.constraint_schema
             AND referential.constraint_name = foreign_key.constraint_name
            JOIN information_schema.key_column_usage referenced_column
              ON referenced_column.constraint_catalog =
                 referential.unique_constraint_catalog
             AND referenced_column.constraint_schema =
                 referential.unique_constraint_schema
             AND referenced_column.constraint_name =
                 referential.unique_constraint_name
             AND referenced_column.ordinal_position =
                 source_column.position_in_unique_constraint
            WHERE foreign_key.table_schema = 'public'
              AND foreign_key.table_name = :table_name
              AND foreign_key.constraint_type = 'FOREIGN KEY'
            ORDER BY foreign_key.constraint_name, source_column.ordinal_position
            """
        ),
        {"table_name": table_name},
    ).mappings()
    grouped: dict[str, dict[str, Any]] = {}
    for raw_row in rows:
        row = dict(raw_row)
        constraint_name = str(row["constraint_name"])
        item = grouped.setdefault(
            constraint_name,
            {
                "source_columns": [],
                "referenced_schema": str(row["referenced_schema"]),
                "referenced_table": str(row["referenced_table"]),
                "referenced_columns": [],
                "delete_rule": str(row["delete_rule"]).upper(),
            },
        )
        item["source_columns"].append(str(row["source_column"]))
        item["referenced_columns"].append(str(row["referenced_column"]))
    return {
        (
            tuple(item["source_columns"]),
            item["referenced_schema"],
            item["referenced_table"],
            tuple(item["referenced_columns"]),
            item["delete_rule"],
        )
        for item in grouped.values()
    }


def _foreign_key_label(
    signature: tuple[tuple[str, ...], str, str, tuple[str, ...], str],
) -> str:
    source_columns, schema, table, target_columns, delete_rule = signature
    return (
        f"{','.join(source_columns)}->{schema}.{table}."
        f"{','.join(target_columns)}[{delete_rule}]"
    )


def _territorial_schema_state(
    connection: Connection,
    counts: Mapping[str, int],
) -> dict[str, Any]:
    tables: dict[str, dict[str, Any]] = {}
    for table_name, raw_requirements in TERRITORIAL_SCHEMA_REQUIREMENTS.items():
        requirements = dict(raw_requirements)
        present = table_name in counts
        constraint_types: dict[str, str] = {}
        constraint_columns: dict[str, tuple[str, ...]] = {}
        indexes: dict[str, tuple[tuple[str, ...], bool]] = {}
        foreign_keys: set[
            tuple[tuple[str, ...], str, str, tuple[str, ...], str]
        ] = set()
        if present:
            constraint_types, constraint_columns = _constraint_inventory(
                connection,
                table_name,
            )
            indexes = _index_inventory(connection, table_name)
            foreign_keys = _foreign_key_inventory(connection, table_name)

        expected_constraints = dict(requirements["constraints"])
        expected_constraint_columns = dict(requirements["constraint_columns"])
        missing_constraints = sorted(
            constraint_name
            for constraint_name, constraint_type in expected_constraints.items()
            if constraint_types.get(constraint_name) != constraint_type
            or (
                constraint_name in expected_constraint_columns
                and constraint_columns.get(constraint_name)
                != expected_constraint_columns[constraint_name]
            )
        )

        expected_indexes = dict(requirements["indexes"])
        missing_indexes = sorted(
            index_name
            for index_name, signature in expected_indexes.items()
            if indexes.get(index_name) != signature
        )

        expected_primary_key = tuple(requirements["primary_key"])
        primary_key_valid = any(
            constraint_types.get(constraint_name) == "PRIMARY KEY"
            and columns == expected_primary_key
            for constraint_name, columns in constraint_columns.items()
        )

        expected_foreign_keys = set(requirements["foreign_keys"])
        missing_foreign_keys = sorted(
            _foreign_key_label(signature)
            for signature in expected_foreign_keys.difference(foreign_keys)
        )
        table_ready = bool(
            present
            and not missing_constraints
            and not missing_indexes
            and primary_key_valid
            and not missing_foreign_keys
        )
        tables[table_name] = {
            "present": present,
            "rows": counts.get(table_name),
            "constraints_valid": not missing_constraints,
            "indexes_valid": not missing_indexes,
            "primary_key_valid": primary_key_valid,
            "foreign_keys_valid": not missing_foreign_keys,
            "missing_constraints": missing_constraints,
            "missing_indexes": missing_indexes,
            "missing_foreign_keys": missing_foreign_keys,
            "ready": table_ready,
        }

    checks = {
        "tables_present": all(item["present"] for item in tables.values()),
        "constraints_valid": all(
            item["constraints_valid"] for item in tables.values()
        ),
        "indexes_valid": all(item["indexes_valid"] for item in tables.values()),
        "primary_keys_valid": all(
            item["primary_key_valid"] for item in tables.values()
        ),
        "foreign_keys_valid": all(
            item["foreign_keys_valid"] for item in tables.values()
        ),
    }
    return {
        "checks": checks,
        "ready": all(checks.values()),
        "tables": tables,
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
    territorial_schema = _territorial_schema_state(connection, counts)

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
        "territorial_tables_present": territorial_schema["checks"][
            "tables_present"
        ],
        "territorial_constraints_valid": territorial_schema["checks"][
            "constraints_valid"
        ],
        "territorial_indexes_valid": territorial_schema["checks"][
            "indexes_valid"
        ],
        "territorial_primary_keys_valid": territorial_schema["checks"][
            "primary_keys_valid"
        ],
        "territorial_foreign_keys_valid": territorial_schema["checks"][
            "foreign_keys_valid"
        ],
    }
    return {
        "checks": checks,
        "ready": all(checks.values()),
        "tables": tables,
        "territorial_schema": territorial_schema,
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
