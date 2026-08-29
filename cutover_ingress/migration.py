"""Small, explicit migration runner for the separate ingress database.

The cutover buffer must never share the application's Alembic graph.  Keeping a
one-row revision table in the independent database makes startup verification
deterministic without importing application models or ``database.db``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import re

from sqlalchemy import CheckConstraint, Engine, inspect, select, update
from sqlalchemy.engine import Connection

from .schema import (
    buffered_whatsapp_ingress,
    metadata,
    schema_revision,
    twilio_idempotency_evidence,
)


CUTOVER_INGRESS_SCHEMA_REVISION_V1 = "cutover_whatsapp_ingress_v1"
CUTOVER_INGRESS_SCHEMA_REVISION = "cutover_whatsapp_ingress_v2"


class CutoverIngressMigrationError(RuntimeError):
    pass


def _normalized_sql(value: object) -> str:
    return re.sub(
        r"\s+", " ", str("" if value is None else value).strip()
    ).lower()


def _canonical_predicate(value: object) -> str:
    """Normalize equivalent PostgreSQL/SQLite predicate introspection.

    PostgreSQL renders VARCHAR checks with explicit text casts and rewrites
    ``IN (...)`` as ``= ANY (ARRAY[...])``.  Removing only those dialect
    artifacts lets us compare the complete authored predicate rather than
    weakening verification to constraint names alone.
    """

    resolved = _normalized_sql(value)
    resolved = re.sub(
        r"::(?:character varying|varchar|text)(?:\[\])?",
        "",
        resolved,
    )
    resolved = re.sub(r"=\s*any\b", " in ", resolved)
    resolved = re.sub(r"\barray\b", "", resolved)
    resolved = resolved.replace("[", "(").replace("]", ")")
    resolved = resolved.replace("(", "").replace(")", "")
    resolved = re.sub(r"\s*(>=|<=|<>|=)\s*", r"\1", resolved)
    resolved = re.sub(r"\s*,\s*", ",", resolved)
    return re.sub(r"\s+", " ", resolved).strip()


def _normalized_type(value: object, connection: Connection) -> str:
    return _normalized_sql(value.compile(dialect=connection.dialect))


def _assert_table_columns(connection: Connection, table) -> None:
    inspector = inspect(connection)
    actual_columns = inspector.get_columns(table.name)
    expected_columns = list(table.columns)
    if [column["name"] for column in actual_columns] != [
        column.name for column in expected_columns
    ]:
        raise CutoverIngressMigrationError(
            f"cutover_ingress_schema_columns_mismatch:{table.name}"
        )
    for expected, actual in zip(expected_columns, actual_columns):
        if bool(actual["nullable"]) != bool(expected.nullable):
            raise CutoverIngressMigrationError(
                f"cutover_ingress_schema_column_nullability_mismatch:{table.name}:{expected.name}"
            )
        if _normalized_type(actual["type"], connection) != _normalized_type(
            expected.type, connection
        ):
            raise CutoverIngressMigrationError(
                f"cutover_ingress_schema_column_type_mismatch:{table.name}:{expected.name}"
            )
    actual_pk = inspector.get_pk_constraint(table.name).get(
        "constrained_columns"
    ) or []
    expected_pk = [column.name for column in table.primary_key.columns]
    if list(actual_pk) != expected_pk:
        raise CutoverIngressMigrationError(
            f"cutover_ingress_schema_primary_key_mismatch:{table.name}"
        )


def _assert_check_constraints(connection: Connection, table) -> None:
    inspector = inspect(connection)
    actual = {
        str(item.get("name") or ""): _canonical_predicate(item.get("sqltext"))
        for item in inspector.get_check_constraints(table.name)
    }
    expected = {
        str(constraint.name): _canonical_predicate(
            constraint.sqltext.compile(
                dialect=connection.dialect,
                compile_kwargs={"literal_binds": True},
            )
        )
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    if set(actual) != set(expected):
        raise CutoverIngressMigrationError(
            "cutover_ingress_schema_check_constraints_mismatch"
        )
    if actual != expected:
        raise CutoverIngressMigrationError(
            "cutover_ingress_schema_check_constraints_mismatch"
        )


def _index_where(index: dict[str, object], dialect_name: str) -> str:
    options = index.get("dialect_options") or {}
    if not isinstance(options, dict):
        return ""
    return _canonical_predicate(options.get(f"{dialect_name}_where"))


def _assert_indexes(connection: Connection, table) -> None:
    inspector = inspect(connection)
    dialect_name = connection.dialect.name
    actual = {
        str(index.get("name") or ""): (
            tuple(index.get("column_names") or ()),
            bool(index.get("unique")),
            _index_where(index, dialect_name),
        )
        for index in inspector.get_indexes(table.name)
    }
    expected: dict[str, tuple[tuple[str, ...], bool, str]] = {}
    for index in table.indexes:
        dialect_options = index.dialect_options.get(dialect_name, {})
        expected[str(index.name)] = (
            tuple(column.name for column in index.columns),
            bool(index.unique),
            _canonical_predicate(dialect_options.get("where")),
        )
    if actual != expected:
        raise CutoverIngressMigrationError(
            "cutover_ingress_schema_indexes_mismatch"
        )


def _assert_structural_contract(connection: Connection) -> None:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    required = {table.name for table in metadata.sorted_tables}
    if tables != required:
        raise CutoverIngressMigrationError("cutover_ingress_schema_tables_mismatch")
    for table in metadata.sorted_tables:
        _assert_table_columns(connection, table)
        _assert_check_constraints(connection, table)
        _assert_indexes(connection, table)
    for table in metadata.sorted_tables:
        if inspector.get_foreign_keys(table.name):
            raise CutoverIngressMigrationError(
                f"cutover_ingress_schema_foreign_keys_mismatch:{table.name}"
            )


def _migrate_v1_to_v2(connection: Connection, tables: set[str]) -> None:
    """Add only the retry-token evidence table to the exact known v1 schema."""

    legacy_tables = {schema_revision.name, buffered_whatsapp_ingress.name}
    if tables != legacy_tables:
        raise CutoverIngressMigrationError("cutover_ingress_incomplete_schema")
    for table in (schema_revision, buffered_whatsapp_ingress):
        _assert_table_columns(connection, table)
        _assert_check_constraints(connection, table)
        _assert_indexes(connection, table)
        if inspect(connection).get_foreign_keys(table.name):
            raise CutoverIngressMigrationError(
                f"cutover_ingress_schema_foreign_keys_mismatch:{table.name}"
            )

    twilio_idempotency_evidence.create(connection)
    connection.execute(
        update(schema_revision)
        .where(schema_revision.c.revision == CUTOVER_INGRESS_SCHEMA_REVISION_V1)
        .values(
            revision=CUTOVER_INGRESS_SCHEMA_REVISION,
            applied_at=datetime.now(timezone.utc),
        )
    )


def migrate_cutover_ingress(engine: Engine) -> str:
    """Create v2, upgrade exact v1, or verify the exact known revision."""

    with engine.begin() as connection:
        tables = set(inspect(connection).get_table_names())
        if schema_revision.name in tables:
            revisions = set(connection.scalars(select(schema_revision.c.revision)))
            if revisions == {CUTOVER_INGRESS_SCHEMA_REVISION_V1}:
                _migrate_v1_to_v2(connection, tables)
                _assert_structural_contract(connection)
                return CUTOVER_INGRESS_SCHEMA_REVISION
            if revisions != {CUTOVER_INGRESS_SCHEMA_REVISION}:
                raise CutoverIngressMigrationError(
                    "cutover_ingress_unknown_schema_revision"
                )
            # ``create_all`` remains useful for repairing no objects: it only
            # issues CREATE for missing tables/indexes and never mutates known
            # columns.  An existing version with missing tables is refused
            # below instead of being silently accepted.
            required = {table.name for table in metadata.sorted_tables}
            if not required.issubset(tables):
                raise CutoverIngressMigrationError(
                    "cutover_ingress_incomplete_schema"
                )
            _assert_structural_contract(connection)
            return CUTOVER_INGRESS_SCHEMA_REVISION

        if tables:
            raise CutoverIngressMigrationError(
                "cutover_ingress_database_not_empty"
            )

        metadata.create_all(connection)
        connection.execute(
            schema_revision.insert().values(
                revision=CUTOVER_INGRESS_SCHEMA_REVISION,
                applied_at=datetime.now(timezone.utc),
            )
        )
        _assert_structural_contract(connection)
    return CUTOVER_INGRESS_SCHEMA_REVISION


def assert_cutover_ingress_schema(engine: Engine) -> None:
    with engine.connect() as connection:
        _assert_structural_contract(connection)
        revisions = set(connection.scalars(select(schema_revision.c.revision)))
        if revisions != {CUTOVER_INGRESS_SCHEMA_REVISION}:
            raise CutoverIngressMigrationError(
                "cutover_ingress_unknown_schema_revision"
            )
