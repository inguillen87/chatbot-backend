"""add server-owned survey response provenance

Revision ID: 20260820_survey_response_origin_v1
Revises: 20260815_tenant_reply_event_v1
Create Date: 2026-08-20

Public request metadata is not a provenance authority. This forward-only
migration adds a closed, indexed origin written by the backend. Complete
survey-bound seed contracts become trusted synthetic rows; historical
seed-shaped rows without that contract are quarantined as legacy_unverified
and are never promoted to citizen truth.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from alembic import op
import sqlalchemy as sa


revision = "20260820_survey_response_origin_v1"
down_revision = "20260815_tenant_reply_event_v1"
branch_labels = None
depends_on = None


_REAL = "real"
_SYNTHETIC_DEMO = "synthetic_demo"
_LEGACY_UNVERIFIED = "legacy_unverified"
_SEED_CONTRACT = "surveys.demo_seeding.v1"
_BATCH_PATTERN = re.compile(
    r"^seed-(?P<survey_id>[1-9][0-9]*)-(?P<timestamp>[0-9]{9,16})"
    r"(?:-(?P<nonce>[a-f0-9]{12}))?$"
)
_SQL_STRING_LITERAL_PATTERN = re.compile(r"'(?:''|[^'])*'")
_SURVEY_INDEX = "ix_enc_respuesta_survey_origin_submitted_id"
_TENANT_INDEX = "ix_enc_respuesta_tenant_origin_submitted_id"
_PUBLIC_RESPONSE_SURVEY_INDEX = "ix_public_survey_response_survey_id"
_LEGACY_FINGERPRINT_CONSTRAINT = "uq_enc_respuesta_huella"
_REAL_FINGERPRINT_INDEX = "uq_enc_respuesta_real_huella"


def _metadata_mapping(raw: Any) -> Mapping[str, Any] | None:
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, UnicodeDecodeError):
            return None
        return parsed if isinstance(parsed, Mapping) else None
    return None


def _seed_origin(raw: Any, *, survey_id: Any) -> str | None:
    metadata = _metadata_mapping(raw)
    if metadata is None or metadata.get("is_demo_seed") is not True:
        return None
    batch_id = metadata.get("demo_batch_id")
    if not isinstance(batch_id, str):
        return None
    match = _BATCH_PATTERN.fullmatch(batch_id)
    if match is None:
        return None
    try:
        expected_survey_id = int(survey_id)
        batch_survey_id = int(match.group("survey_id"))
    except (TypeError, ValueError, OverflowError):
        return None
    if expected_survey_id <= 0 or batch_survey_id != expected_survey_id:
        return None
    if metadata.get("demo_seed_contract_version") == _SEED_CONTRACT:
        return _SYNTHETIC_DEMO
    return _LEGACY_UNVERIFIED


def _backfill_seed_origins() -> None:
    context = op.get_context()
    if bool(getattr(context, "as_sql", False)):
        raise RuntimeError(
            "survey response provenance migration requires online execution "
            "because the conservative JSON classifier is Python-owned"
        )

    bind = op.get_bind()
    response_table = sa.table(
        "enc_respuesta",
        sa.column("id", sa.Integer()),
        sa.column("encuesta_id", sa.Integer()),
        sa.column("metadata_payload", sa.JSON()),
        sa.column("response_origin", sa.String(length=32)),
    )
    result = bind.execute(
        sa.select(
            response_table.c.id,
            response_table.c.encuesta_id,
            response_table.c.metadata_payload,
        )
        .where(response_table.c.metadata_payload.is_not(None))
        .execution_options(stream_results=True)
    )
    while True:
        rows = result.fetchmany(500)
        if not rows:
            break
        classified_ids: dict[str, list[int]] = {
            _SYNTHETIC_DEMO: [],
            _LEGACY_UNVERIFIED: [],
        }
        for row in rows:
            origin = _seed_origin(
                row.metadata_payload,
                survey_id=row.encuesta_id,
            )
            if origin in classified_ids:
                classified_ids[origin].append(int(row.id))
        for origin, response_ids in classified_ids.items():
            if not response_ids:
                continue
            bind.execute(
                sa.update(response_table)
                .where(response_table.c.id.in_(response_ids))
                .values(response_origin=origin)
            )


def _create_index_if_missing_exact(
    table_name: str,
    index_name: str,
    columns: tuple[str, ...],
) -> None:
    context = op.get_context()
    if bool(getattr(context, "as_sql", False)):
        op.create_index(index_name, table_name, list(columns), unique=False)
        return

    existing = {
        str(index.get("name") or ""): index
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }
    current = existing.get(index_name)
    if current is not None:
        current_columns = tuple(current.get("column_names") or ())
        if current_columns == columns and not bool(current.get("unique")):
            return
        raise RuntimeError(
            f"conflicting index {index_name} on {table_name}: "
            f"expected {columns}, found {current_columns}"
        )
    op.create_index(index_name, table_name, list(columns), unique=False)


def _assert_real_fingerprints_unique(bind: Any) -> None:
    duplicate = bind.execute(
        sa.text(
            "SELECT encuesta_id, huella_unica FROM enc_respuesta "
            "WHERE response_origin = 'real' AND huella_unica IS NOT NULL "
            "GROUP BY encuesta_id, huella_unica HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "cannot create real-only survey fingerprint index: duplicate real rows exist"
        )


def _preflight_base_schema(bind: Any) -> set[str]:
    inspector = sa.inspect(bind)
    required_tables = {"enc_respuesta", "public_survey_response"}
    missing_tables = sorted(
        table for table in required_tables if not inspector.has_table(table)
    )
    if missing_tables:
        raise RuntimeError(
            f"survey provenance preflight missing tables: {missing_tables}"
        )
    response_columns = {
        str(column.get("name") or "")
        for column in inspector.get_columns("enc_respuesta")
    }
    required_columns = {
        "id",
        "encuesta_id",
        "tenant_id",
        "huella_unica",
        "submitted_at",
        "metadata_payload",
    }
    missing_columns = sorted(required_columns - response_columns)
    if missing_columns:
        raise RuntimeError(
            "survey provenance preflight missing enc_respuesta columns: "
            f"{missing_columns}"
        )
    return response_columns


def _assert_index_absent_or_exact(
    bind: Any,
    *,
    table_name: str = "enc_respuesta",
    name: str,
    columns: tuple[str, ...],
    unique: bool,
    expected_predicate: str | None = None,
) -> bool:
    current = {
        str(item.get("name") or ""): item
        for item in sa.inspect(bind).get_indexes(table_name)
    }.get(name)
    if current is None:
        return False
    current_columns = tuple(current.get("column_names") or ())
    if current_columns != columns or bool(current.get("unique")) != unique:
        raise RuntimeError(
            f"conflicting index {name}: expected columns={columns}, unique={unique}; "
            f"found columns={current_columns}, unique={bool(current.get('unique'))}"
        )
    dialect_options = current.get("dialect_options") or {}
    predicate = str(
        dialect_options.get("postgresql_where")
        or dialect_options.get("sqlite_where")
        or current.get("postgresql_where")
        or current.get("sqlite_where")
        or ""
    )
    if _normalized_index_predicate(predicate) != _normalized_index_predicate(
        expected_predicate
    ):
        raise RuntimeError(
            f"conflicting index {name}: unexpected partial predicate"
        )
    return True


def _protect_sql_string_literals(raw: Any) -> tuple[str, list[tuple[str, str]]]:
    literals: list[tuple[str, str]] = []

    def _replace(match: re.Match[str]) -> str:
        token = f"__chatboc_sql_literal_{len(literals)}__"
        literals.append((token, match.group(0)))
        return token

    return _SQL_STRING_LITERAL_PATTERN.sub(_replace, str(raw)), literals


def _restore_sql_string_literals(
    normalized: str,
    literals: list[tuple[str, str]],
) -> str:
    for token, literal in literals:
        normalized = normalized.replace(token, literal)
    return normalized


def _normalized_index_predicate(raw: Any) -> str:
    if raw is None:
        return ""
    protected, literals = _protect_sql_string_literals(raw)
    predicate = protected.strip().lower().replace('"', "")
    predicate = re.sub(
        r"::(?:pg_catalog\.)?(?:text|character\s+varying|varchar)(?:\(\d+\))?",
        "",
        predicate,
    )
    return _restore_sql_string_literals(
        re.sub(r"[\s()]", "", predicate),
        literals,
    )


def _normalized_postgresql_index_definition(raw: Any) -> str:
    if raw is None:
        return ""
    protected, literals = _protect_sql_string_literals(raw)
    definition = protected.strip().lower().replace('"', "")
    definition = re.sub(
        r"::(?:pg_catalog\.)?(?:text|character\s+varying|varchar)(?:\(\d+\))?",
        "",
        definition,
    )
    definition = re.sub(r"\bconcurrently\b", "", definition)
    definition = re.sub(r"\bif\s+not\s+exists\b", "", definition)
    definition = re.sub(r"\busing\s+btree\b", "", definition)
    definition = re.sub(
        r"\bon\s+(?:[a-z_][a-z0-9_$]*\.)+([a-z_][a-z0-9_$]*)",
        r"on \1",
        definition,
    )
    return _restore_sql_string_literals(
        re.sub(r"[\s()]", "", definition),
        literals,
    )


def _postgresql_index_catalog_state(
    bind: Any,
    *,
    table_name: str,
    name: str,
) -> tuple[bool | None, str | None]:
    row = bind.execute(
        sa.text(
            "SELECT idx.indisvalid, idx.indisready, "
            "pg_get_indexdef(idx.indexrelid) AS index_definition "
            "FROM pg_index idx "
            "JOIN pg_class index_class ON index_class.oid = idx.indexrelid "
            "JOIN pg_class table_class ON table_class.oid = idx.indrelid "
            "JOIN pg_namespace ns ON ns.oid = table_class.relnamespace "
            "WHERE ns.nspname = current_schema() "
            "AND table_class.relname = :table_name "
            "AND index_class.relname = :index_name"
        ),
        {"table_name": table_name, "index_name": name},
    ).first()
    if row is None:
        return None, None
    return (
        bool(row.indisvalid) and bool(row.indisready),
        str(row.index_definition or ""),
    )


def _postgresql_index_state(
    bind: Any,
    *,
    table_name: str,
    name: str,
    columns: tuple[str, ...],
    unique: bool,
    expected_predicate: str | None = None,
    expected_definition: str,
) -> str:
    exists = _assert_index_absent_or_exact(
        bind,
        table_name=table_name,
        name=name,
        columns=columns,
        unique=unique,
        expected_predicate=expected_predicate,
    )
    if not exists:
        return "absent"
    validity, current_definition = _postgresql_index_catalog_state(
        bind,
        table_name=table_name,
        name=name,
    )
    if validity is None:
        raise RuntimeError(
            f"postgresql index {name} disappeared during migration preflight"
        )
    if _normalized_postgresql_index_definition(
        current_definition
    ) != _normalized_postgresql_index_definition(expected_definition):
        raise RuntimeError(
            f"conflicting index {name}: unexpected postgresql definition"
        )
    return "valid" if validity else "invalid"


def _plan_postgresql_index_ddl(
    bind: Any,
    *,
    table_name: str,
    name: str,
    columns: tuple[str, ...],
    unique: bool,
    expected_predicate: str | None,
    drop_sql: str,
    create_sql: str,
) -> tuple[str | None, str | None]:
    state = _postgresql_index_state(
        bind,
        table_name=table_name,
        name=name,
        columns=columns,
        unique=unique,
        expected_predicate=expected_predicate,
        expected_definition=create_sql,
    )
    if state == "valid":
        return None, None
    if state == "invalid":
        return drop_sql, create_sql
    return None, create_sql


def _assert_postgresql_index_exact_and_valid(
    bind: Any,
    *,
    table_name: str,
    name: str,
    columns: tuple[str, ...],
    unique: bool,
    expected_predicate: str | None,
    expected_definition: str,
) -> None:
    state = _postgresql_index_state(
        bind,
        table_name=table_name,
        name=name,
        columns=columns,
        unique=unique,
        expected_predicate=expected_predicate,
        expected_definition=expected_definition,
    )
    if state != "valid":
        raise RuntimeError(f"postgresql index {name} is missing or invalid")


def _upgrade_postgresql() -> None:
    bind = op.get_bind()
    response_columns = _preflight_base_schema(bind)
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '15min'"))

    existing_unique_constraints = {
        str(item.get("name") or "")
        for item in sa.inspect(bind).get_unique_constraints("enc_respuesta")
    }
    if "response_origin" not in response_columns:
        op.execute(
            sa.text(
                "ALTER TABLE enc_respuesta ADD COLUMN response_origin "
                "VARCHAR(32) DEFAULT 'real' NOT NULL"
            )
        )
    else:
        origin_column = next(
            column
            for column in sa.inspect(bind).get_columns("enc_respuesta")
            if column.get("name") == "response_origin"
        )
        column_type = origin_column.get("type")
        if (
            bool(origin_column.get("nullable"))
            or getattr(column_type, "length", None) != 32
            or "real" not in str(origin_column.get("default") or "").lower()
        ):
            raise RuntimeError(
                "conflicting existing enc_respuesta.response_origin column"
            )
    existing_checks = {
        str(item.get("name") or ""): str(item.get("sqltext") or "")
        for item in sa.inspect(bind).get_check_constraints("enc_respuesta")
    }
    current_origin_check = existing_checks.get(
        "ck_enc_respuesta_response_origin"
    )
    if current_origin_check is not None and "legacy_unverified" not in current_origin_check:
        op.execute(
            sa.text(
                "ALTER TABLE enc_respuesta DROP CONSTRAINT "
                "ck_enc_respuesta_response_origin"
            )
        )
        current_origin_check = None
    if current_origin_check is None:
        op.execute(
            sa.text(
                "ALTER TABLE enc_respuesta ADD CONSTRAINT "
                "ck_enc_respuesta_response_origin CHECK "
                "(response_origin IN ('real', 'synthetic_demo', 'legacy_unverified')) "
                "NOT VALID"
            )
        )
    _backfill_seed_origins()
    _assert_real_fingerprints_unique(bind)
    op.execute(
        sa.text(
            "ALTER TABLE enc_respuesta VALIDATE CONSTRAINT "
            "ck_enc_respuesta_response_origin"
        )
    )

    index_specs = (
        (
            _REAL_FINGERPRINT_INDEX,
            ("encuesta_id", "huella_unica"),
            True,
            "response_origin = 'real' AND huella_unica IS NOT NULL",
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
            "uq_enc_respuesta_real_huella ON enc_respuesta "
            "(encuesta_id, huella_unica) WHERE "
            "response_origin = 'real' AND huella_unica IS NOT NULL",
            "DROP INDEX CONCURRENTLY IF EXISTS uq_enc_respuesta_real_huella",
        ),
        (
            _SURVEY_INDEX,
            ("encuesta_id", "response_origin", "submitted_at", "id"),
            False,
            None,
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_enc_respuesta_survey_origin_submitted_id ON enc_respuesta "
            "(encuesta_id, response_origin, submitted_at, id)",
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "ix_enc_respuesta_survey_origin_submitted_id",
        ),
        (
            _TENANT_INDEX,
            ("tenant_id", "response_origin", "submitted_at", "id"),
            False,
            None,
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_enc_respuesta_tenant_origin_submitted_id ON enc_respuesta "
            "(tenant_id, response_origin, submitted_at, id)",
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "ix_enc_respuesta_tenant_origin_submitted_id",
        ),
    )
    # A failed CREATE INDEX CONCURRENTLY can leave an exact but invalid
    # pg_index entry. Plan every expected definition before executing DDL so
    # any name/shape conflict fails closed without dropping another index.
    pending_drop_sql: list[str] = []
    pending_create_sql: list[str] = []
    for name, columns, unique, expected_predicate, create_sql, drop_sql in index_specs:
        planned_drop, planned_create = _plan_postgresql_index_ddl(
            bind,
            table_name="enc_respuesta",
            name=name,
            columns=columns,
            unique=unique,
            expected_predicate=expected_predicate,
            drop_sql=drop_sql,
            create_sql=create_sql,
        )
        if planned_drop is not None:
            pending_drop_sql.append(planned_drop)
        if planned_create is not None:
            pending_create_sql.append(planned_create)

    public_create_sql = (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_public_survey_response_survey_id ON "
        "public_survey_response (survey_id)"
    )
    public_drop_sql = (
        "DROP INDEX CONCURRENTLY IF EXISTS "
        "ix_public_survey_response_survey_id"
    )
    planned_drop, planned_create = _plan_postgresql_index_ddl(
        bind,
        table_name="public_survey_response",
        name=_PUBLIC_RESPONSE_SURVEY_INDEX,
        columns=("survey_id",),
        unique=False,
        expected_predicate=None,
        drop_sql=public_drop_sql,
        create_sql=public_create_sql,
    )
    if planned_drop is not None:
        pending_drop_sql.append(planned_drop)
    if planned_create is not None:
        pending_create_sql.append(planned_create)

    context = op.get_context()
    with context.autocommit_block():
        op.execute(sa.text("SET lock_timeout = '5s'"))
        op.execute(sa.text("SET statement_timeout = '15min'"))
        # DROP INDEX CONCURRENTLY is only planned for an expected definition
        # whose pg_index row is present but not both ready and valid.
        for drop_sql in pending_drop_sql:
            op.execute(sa.text(drop_sql))
        for create_sql in pending_create_sql:
            op.execute(sa.text(create_sql))
    for name, columns, unique, predicate, create_sql, _drop_sql in index_specs:
        _assert_postgresql_index_exact_and_valid(
            bind,
            table_name="enc_respuesta",
            name=name,
            columns=columns,
            unique=unique,
            expected_predicate=predicate,
            expected_definition=create_sql,
        )
    _assert_postgresql_index_exact_and_valid(
        bind,
        table_name="public_survey_response",
        name=_PUBLIC_RESPONSE_SURVEY_INDEX,
        columns=("survey_id",),
        unique=False,
        expected_predicate=None,
        expected_definition=public_create_sql,
    )
    if _LEGACY_FINGERPRINT_CONSTRAINT in existing_unique_constraints:
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(
            sa.text(
                "ALTER TABLE enc_respuesta DROP CONSTRAINT "
                "uq_enc_respuesta_huella"
            )
        )


def _upgrade_portable() -> None:
    bind = op.get_bind()
    _preflight_base_schema(bind)
    existing_unique_constraints = {
        str(item.get("name") or "")
        for item in sa.inspect(bind).get_unique_constraints("enc_respuesta")
    }
    with op.batch_alter_table("enc_respuesta", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "response_origin",
                sa.String(length=32),
                server_default=_REAL,
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_enc_respuesta_response_origin",
            "response_origin IN ('real', 'synthetic_demo', 'legacy_unverified')",
        )
        if _LEGACY_FINGERPRINT_CONSTRAINT in existing_unique_constraints:
            batch_op.drop_constraint(
                _LEGACY_FINGERPRINT_CONSTRAINT,
                type_="unique",
            )

    _backfill_seed_origins()
    _assert_real_fingerprints_unique(bind)

    op.create_index(
        _REAL_FINGERPRINT_INDEX,
        "enc_respuesta",
        ["encuesta_id", "huella_unica"],
        unique=True,
        postgresql_where=sa.text(
            "response_origin = 'real' AND huella_unica IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "response_origin = 'real' AND huella_unica IS NOT NULL"
        ),
    )

    op.create_index(
        _SURVEY_INDEX,
        "enc_respuesta",
        ["encuesta_id", "response_origin", "submitted_at", "id"],
        unique=False,
    )
    op.create_index(
        _TENANT_INDEX,
        "enc_respuesta",
        ["tenant_id", "response_origin", "submitted_at", "id"],
        unique=False,
    )
    _create_index_if_missing_exact(
        "public_survey_response",
        _PUBLIC_RESPONSE_SURVEY_INDEX,
        ("survey_id",),
    )


def upgrade() -> None:
    context = op.get_context()
    if bool(getattr(context, "as_sql", False)):
        raise RuntimeError(
            "20260820 survey provenance migration does not support offline SQL"
        )
    if op.get_bind().dialect.name == "postgresql":
        _upgrade_postgresql()
    else:
        _upgrade_portable()


def downgrade() -> None:
    raise RuntimeError(
        "20260820 survey response provenance is forward-only; downgrade is unsafe"
    )
