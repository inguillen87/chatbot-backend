"""Reconcile additive model columns missing from persisted schemas.

Revision ID: 20260814_model_schema_drift_v1
Revises: 20260814_survey_scope_canonical_v1
Create Date: 2026-08-14

A production model-to-database audit found a small set of nullable application
columns that never received matching Alembic operations.  ORM loads therefore
selected undefined columns and could fail otherwise unrelated authenticated
requests.

This is intentionally a forward-only, additive repair. Existing rows and
columns are preserved; only missing nullable columns and their model-declared
indexes are created. Downgrade does not reintroduce the broken schema or erase
new extraction/order metadata.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260814_model_schema_drift_v1"
down_revision = "20260814_survey_scope_canonical_v1"
branch_labels = None
depends_on = None


_POSTGRES_ADVISORY_LOCK_ID = 91_610_393_044_314

_REQUIRED_BASE_COLUMNS = {
    "catalog_upload": {"id", "tenant_id", "filename"},
    "market_cart": {"id", "tenant_id", "status"},
    "market_order": {"id", "tenant_id", "status"},
    "pedido_conversacional": {"id"},
}

_INDEX_SPECS = {
    "catalog_upload": {
        "ix_catalog_upload_tenant_id": ("tenant_id",),
    },
    "market_cart": {
        "ix_market_cart_contact_email": ("contact_email",),
        "ix_market_cart_contact_key": ("contact_key",),
        "ix_market_cart_tenant_contact": ("tenant_id", "contact_key", "status"),
    },
    "market_order": {
        "ix_market_order_contact_key": ("contact_key",),
        "ix_market_order_session_id": ("session_id",),
        "ix_market_order_tenant_contact": (
            "tenant_id",
            "contact_key",
            "status",
        ),
    },
}


def _json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(
        postgresql.JSONB(astext_type=sa.Text()),
        "postgresql",
    )


def _column_specs() -> dict[str, tuple[sa.Column, ...]]:
    """Return fresh Column objects so an idempotency re-run remains valid."""

    return {
        "catalog_upload": (
            # preview_data/warnings were in a historical migration, but are
            # included as guarded repairs for partially migrated databases.
            sa.Column("preview_data", _json_type(), nullable=True),
            sa.Column("warnings", _json_type(), nullable=True),
            sa.Column("file_hash", sa.String(length=64), nullable=True),
            sa.Column("engine_used", sa.String(length=50), nullable=True),
            sa.Column("errors", _json_type(), nullable=True),
        ),
        "market_cart": (
            sa.Column("contact_email", sa.String(length=120), nullable=True),
            sa.Column("contact_key", sa.String(length=160), nullable=True),
            # The model's ``default='web'`` is application-side. Existing rows
            # remain NULL rather than receiving invented provenance.
            sa.Column("channel", sa.String(length=50), nullable=True),
        ),
        "market_order": (
            sa.Column("contact_key", sa.String(length=160), nullable=True),
            sa.Column("session_id", sa.String(length=120), nullable=True),
        ),
        "pedido_conversacional": (
            sa.Column("metadata", _json_type(), nullable=True),
        ),
    }


def _lock_migration(connection) -> None:
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _POSTGRES_ADVISORY_LOCK_ID},
        )


def _type_signature(
    dialect: sa.engine.Dialect,
    column_type: sa.types.TypeEngine,
) -> str:
    """Return the concrete SQL type emitted for the active dialect."""

    implementation = column_type.dialect_impl(dialect)
    return " ".join(
        implementation.compile(dialect=dialect).upper().split()
    )


def _types_compatible(
    dialect: sa.engine.Dialect,
    actual_type: sa.types.TypeEngine,
    expected_type: sa.types.TypeEngine,
) -> bool:
    actual_implementation = actual_type.dialect_impl(dialect)
    expected_implementation = expected_type.dialect_impl(dialect)

    if _type_signature(dialect, actual_type) == _type_signature(
        dialect,
        expected_type,
    ):
        return True

    # preview_data/warnings can already be PostgreSQL JSON from the historical
    # schema. Preserve that valid data-bearing type while new JSON columns use
    # the model's JSONB variant. No other type family is relaxed.
    return isinstance(actual_implementation, sa.JSON) and isinstance(
        expected_implementation,
        sa.JSON,
    )


def _validate_existing_column(
    connection,
    *,
    table_name: str,
    expected: sa.Column,
    actual: dict,
) -> None:
    actual_type = _type_signature(connection.dialect, actual["type"])
    expected_type = _type_signature(connection.dialect, expected.type)
    actual_nullable = actual.get("nullable")

    if not _types_compatible(
        connection.dialect,
        actual["type"],
        expected.type,
    ) or actual_nullable != expected.nullable:
        raise RuntimeError(
            "schema drift column conflicts with the model contract: "
            f"table={table_name} name={expected.name} "
            f"type={actual_type} nullable={actual_nullable} "
            f"expected_type={expected_type} "
            f"expected_nullable={expected.nullable}"
        )


def _validate_existing_index(
    *,
    table_name: str,
    index_name: str,
    column_names: tuple[str, ...],
    existing: dict | None,
) -> None:
    if existing is None:
        return

    existing_columns = tuple(existing.get("column_names") or ())
    if existing_columns != column_names or bool(existing.get("unique")):
        raise RuntimeError(
            "schema drift index conflicts with the model contract: "
            f"table={table_name} name={index_name} "
            f"columns={list(existing_columns)} "
            f"unique={bool(existing.get('unique'))}"
        )


def upgrade() -> None:
    connection = op.get_bind()
    _lock_migration(connection)
    inspector = sa.inspect(connection)
    table_names = set(inspector.get_table_names())
    column_specs = _column_specs()
    existing_columns_by_table: dict[str, dict[str, dict]] = {}
    existing_indexes_by_table: dict[str, dict[str, dict]] = {}

    # Complete the entire preflight before issuing any DDL. SQLite/pysqlite can
    # retain ALTER TABLE changes even when a later migration check raises, so a
    # progressive inspect-and-mutate loop could leave a database half-repaired.
    for table_name, columns in column_specs.items():
        if table_name not in table_names:
            raise RuntimeError(
                f"{table_name} is missing even though the production schema audit "
                "reported only additive column drift; refusing to synthesize a "
                "partial core table"
            )

        existing_columns = {
            column["name"]: column
            for column in inspector.get_columns(table_name)
        }
        existing_columns_by_table[table_name] = existing_columns
        missing_base_columns = sorted(
            _REQUIRED_BASE_COLUMNS[table_name] - set(existing_columns)
        )
        if missing_base_columns:
            raise RuntimeError(
                f"{table_name} is missing required base columns: "
                + ", ".join(missing_base_columns)
            )

        for column in columns:
            actual = existing_columns.get(column.name)
            if actual is not None:
                _validate_existing_column(
                    connection,
                    table_name=table_name,
                    expected=column,
                    actual=actual,
                )

    for table_name, indexes in _INDEX_SPECS.items():
        existing_indexes = {
            index["name"]: index
            for index in inspector.get_indexes(table_name)
            if index.get("name")
        }
        existing_indexes_by_table[table_name] = existing_indexes
        for index_name, column_names in indexes.items():
            _validate_existing_index(
                table_name=table_name,
                index_name=index_name,
                column_names=column_names,
                existing=existing_indexes.get(index_name),
            )

    for table_name, columns in column_specs.items():
        existing_columns = existing_columns_by_table[table_name]
        for column in columns:
            if column.name not in existing_columns:
                op.add_column(table_name, column)

    for table_name, indexes in _INDEX_SPECS.items():
        for index_name, column_names in indexes.items():
            if index_name not in existing_indexes_by_table[table_name]:
                op.create_index(
                    op.f(index_name),
                    table_name,
                    list(column_names),
                    unique=False,
                )


def downgrade() -> None:
    # Forward-only schema repair. Dropping these fields would make the current
    # application model unreadable again and could destroy ingestion/order data.
    pass
