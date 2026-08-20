from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from models import CatalogUpload, MarketCart, MarketOrder, PedidoConversacional


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT
    / "migrations"
    / "versions"
    / "20260814_reconcile_model_schema_drift.py"
)

TARGET_COLUMNS = {
    "catalog_upload": {
        "preview_data",
        "warnings",
        "file_hash",
        "engine_used",
        "errors",
    },
    "market_cart": {"contact_email", "contact_key", "channel"},
    "market_order": {"contact_key", "session_id"},
    "pedido_conversacional": {"metadata"},
}

TARGET_INDEXES = {
    "catalog_upload": {"ix_catalog_upload_tenant_id"},
    "market_cart": {
        "ix_market_cart_contact_email",
        "ix_market_cart_contact_key",
        "ix_market_cart_tenant_contact",
    },
    "market_order": {
        "ix_market_order_contact_key",
        "ix_market_order_session_id",
        "ix_market_order_tenant_contact",
    },
}

MODEL_TABLES = {
    "catalog_upload": CatalogUpload.__table__,
    "market_cart": MarketCart.__table__,
    "market_order": MarketOrder.__table__,
    "pedido_conversacional": PedidoConversacional.__table__,
}


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "model_schema_drift_v1_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(connection, operation) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        operation()


def _legacy_schema(metadata: sa.MetaData) -> None:
    sa.Table(
        "catalog_upload",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        # Simulate a partial historical migration: one preview column exists.
        sa.Column("preview_data", sa.JSON()),
    )
    sa.Table(
        "market_cart",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
    )
    sa.Table(
        "market_order",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("contact_email", sa.String(120)),
    )
    sa.Table(
        "pedido_conversacional",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer()),
        sa.Column("items", sa.JSON(), nullable=False),
    )


def _assert_column_matches_model(actual: dict, model_column: sa.Column) -> None:
    assert actual["nullable"] == model_column.nullable
    if isinstance(model_column.type, sa.String):
        assert isinstance(actual["type"], sa.String)
        assert actual["type"].length == model_column.type.length
    else:
        assert isinstance(model_column.type, sa.JSON)
        assert isinstance(actual["type"], sa.JSON)


def test_model_schema_drift_repair_is_additive_idempotent_and_contract_exact(
    tmp_path,
):
    migration = _load_migration()
    assert migration.revision == "20260814_model_schema_drift_v1"
    assert migration.down_revision == "20260814_survey_scope_canonical_v1"

    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / 'model-schema-drift.sqlite3').as_posix()}"
    )
    metadata = sa.MetaData()
    _legacy_schema(metadata)
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO catalog_upload "
                "(id, tenant_id, filename, preview_data) "
                "VALUES (1, 6, 'catalogo.xlsx', :preview_data)"
            ),
            {"preview_data": '{"items": 3}'},
        )
        connection.execute(
            sa.text(
                "INSERT INTO market_cart (id, tenant_id, session_id, status) "
                "VALUES (2, 6, 'session-cart', 'open')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO market_order (id, tenant_id, status, contact_email) "
                "VALUES (3, 6, 'pending', 'existing@example.com')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO pedido_conversacional (id, tenant_id, items) "
                "VALUES (4, 6, '[]')"
            )
        )

        _run(connection, migration.upgrade)
        _run(connection, migration.upgrade)

        inspector = sa.inspect(connection)
        for table_name, target_columns in TARGET_COLUMNS.items():
            columns = {
                column["name"]: column
                for column in inspector.get_columns(table_name)
            }
            assert target_columns.issubset(columns)
            for column_name in target_columns:
                _assert_column_matches_model(
                    columns[column_name],
                    MODEL_TABLES[table_name].c[column_name],
                )

        for table_name, target_indexes in TARGET_INDEXES.items():
            actual_indexes = {
                index["name"]: index
                for index in inspector.get_indexes(table_name)
            }
            model_indexes = {
                index.name: tuple(column.name for column in index.columns)
                for index in MODEL_TABLES[table_name].indexes
            }
            assert target_indexes.issubset(actual_indexes)
            for index_name in target_indexes:
                assert tuple(actual_indexes[index_name]["column_names"]) == (
                    model_indexes[index_name]
                )
                assert bool(actual_indexes[index_name]["unique"]) is False

        assert connection.execute(
            sa.text("SELECT tenant_id, filename FROM catalog_upload WHERE id = 1")
        ).one() == (6, "catalogo.xlsx")
        assert connection.execute(
            sa.text("SELECT session_id, status FROM market_cart WHERE id = 2")
        ).one() == ("session-cart", "open")
        assert connection.execute(
            sa.text("SELECT contact_email, status FROM market_order WHERE id = 3")
        ).one() == ("existing@example.com", "pending")
        assert connection.execute(
            sa.text("SELECT items FROM pedido_conversacional WHERE id = 4")
        ).scalar_one() == "[]"

        connection.execute(
            sa.text(
                "UPDATE catalog_upload SET file_hash = :file_hash, "
                "engine_used = 'openai', errors = '[]' WHERE id = 1"
            ),
            {"file_hash": "a" * 64},
        )
        connection.execute(
            sa.text(
                "UPDATE market_cart SET contact_email = 'cart@example.com', "
                "contact_key = 'email:cart@example.com', channel = 'whatsapp' "
                "WHERE id = 2"
            )
        )
        connection.execute(
            sa.text(
                "UPDATE market_order SET contact_key = 'email:order@example.com', "
                "session_id = 'session-order' WHERE id = 3"
            )
        )
        connection.execute(
            sa.text(
                "UPDATE pedido_conversacional SET metadata = '{\"source\":\"ocr\"}' "
                "WHERE id = 4"
            )
        )
        assert connection.execute(
            sa.text(
                "SELECT file_hash, engine_used FROM catalog_upload WHERE id = 1"
            )
        ).one() == ("a" * 64, "openai")
        assert connection.execute(
            sa.text(
                "SELECT contact_key, channel FROM market_cart WHERE id = 2"
            )
        ).one() == ("email:cart@example.com", "whatsapp")
        assert connection.execute(
            sa.text(
                "SELECT contact_key, session_id FROM market_order WHERE id = 3"
            )
        ).one() == ("email:order@example.com", "session-order")
        assert "source" in connection.execute(
            sa.text("SELECT metadata FROM pedido_conversacional WHERE id = 4")
        ).scalar_one()

        _run(connection, migration.downgrade)
        for table_name, target_columns in TARGET_COLUMNS.items():
            assert target_columns.issubset(
                {
                    column["name"]
                    for column in sa.inspect(connection).get_columns(table_name)
                }
            )

    engine.dispose()


def test_model_schema_drift_repair_fails_closed_on_missing_core_table(tmp_path):
    migration = _load_migration()
    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / 'missing-core-table.sqlite3').as_posix()}"
    )

    with engine.begin() as connection:
        with pytest.raises(RuntimeError, match="catalog_upload is missing"):
            _run(connection, migration.upgrade)

    engine.dispose()


def test_model_schema_drift_preflights_all_tables_before_any_ddl(tmp_path):
    migration = _load_migration()
    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / 'late-missing-core-table.sqlite3').as_posix()}"
    )
    metadata = sa.MetaData()
    _legacy_schema(metadata)
    metadata.remove(metadata.tables["pedido_conversacional"])
    metadata.create_all(engine)

    with engine.begin() as connection:
        before_columns = {
            table_name: {
                column["name"]
                for column in sa.inspect(connection).get_columns(table_name)
            }
            for table_name in ("catalog_upload", "market_cart", "market_order")
        }
        before_indexes = {
            table_name: {
                index["name"]
                for index in sa.inspect(connection).get_indexes(table_name)
            }
            for table_name in ("catalog_upload", "market_cart", "market_order")
        }

        with pytest.raises(RuntimeError, match="pedido_conversacional is missing"):
            _run(connection, migration.upgrade)

        for table_name in before_columns:
            assert {
                column["name"]
                for column in sa.inspect(connection).get_columns(table_name)
            } == before_columns[table_name]
            assert {
                index["name"]
                for index in sa.inspect(connection).get_indexes(table_name)
            } == before_indexes[table_name]

    engine.dispose()


@pytest.mark.parametrize(
    ("column_type", "nullable"),
    (
        pytest.param(sa.Integer(), True, id="wrong-type"),
        pytest.param(sa.String(63), True, id="wrong-length"),
        pytest.param(sa.String(64), False, id="wrong-nullability"),
    ),
)
def test_model_schema_drift_rejects_wrong_existing_column_contract(
    tmp_path,
    column_type,
    nullable,
):
    migration = _load_migration()
    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / f'wrong-file-hash-{nullable}.sqlite3').as_posix()}"
    )
    metadata = sa.MetaData()
    _legacy_schema(metadata)
    metadata.tables["catalog_upload"].append_column(
        sa.Column("file_hash", column_type, nullable=nullable)
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        before_columns = {
            column["name"]
            for column in sa.inspect(connection).get_columns("catalog_upload")
        }
        with pytest.raises(
            RuntimeError,
            match="table=catalog_upload name=file_hash",
        ):
            _run(connection, migration.upgrade)
        assert {
            column["name"]
            for column in sa.inspect(connection).get_columns("catalog_upload")
        } == before_columns

    engine.dispose()


@pytest.mark.parametrize(
    ("index_columns", "unique"),
    (
        pytest.param(
            ("contact_key", "tenant_id", "status"),
            False,
            id="wrong-columns",
        ),
        pytest.param(
            ("tenant_id", "contact_key", "status"),
            True,
            id="wrong-unique",
        ),
    ),
)
def test_model_schema_drift_rejects_wrong_index_before_any_ddl(
    tmp_path,
    index_columns,
    unique,
):
    migration = _load_migration()
    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / f'wrong-index-{unique}.sqlite3').as_posix()}"
    )
    metadata = sa.MetaData()
    _legacy_schema(metadata)
    market_order = metadata.tables["market_order"]
    market_order.append_column(
        sa.Column("contact_key", sa.String(160), nullable=True)
    )
    sa.Index(
        "ix_market_order_tenant_contact",
        *(market_order.c[column_name] for column_name in index_columns),
        unique=unique,
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        before_columns = {
            table_name: {
                column["name"]
                for column in sa.inspect(connection).get_columns(table_name)
            }
            for table_name in MODEL_TABLES
        }
        before_indexes = {
            table_name: {
                index["name"]
                for index in sa.inspect(connection).get_indexes(table_name)
            }
            for table_name in TARGET_INDEXES
        }

        with pytest.raises(
            RuntimeError,
            match="table=market_order name=ix_market_order_tenant_contact",
        ):
            _run(connection, migration.upgrade)

        for table_name in before_columns:
            assert {
                column["name"]
                for column in sa.inspect(connection).get_columns(table_name)
            } == before_columns[table_name]
        for table_name in before_indexes:
            assert {
                index["name"]
                for index in sa.inspect(connection).get_indexes(table_name)
            } == before_indexes[table_name]

    engine.dispose()


def test_model_schema_drift_json_columns_compile_as_postgresql_jsonb():
    migration = _load_migration()
    assert (
        str(migration._json_type().compile(dialect=postgresql.dialect()))
        == "JSONB"
    )
    assert (
        migration._type_signature(postgresql.dialect(), migration._json_type())
        == "JSONB"
    )
    assert migration._type_signature(postgresql.dialect(), sa.JSON()) == "JSON"


def test_model_schema_drift_accepts_historical_postgresql_json_columns():
    migration = _load_migration()
    connection = SimpleNamespace(dialect=postgresql.dialect())
    expected = sa.Column("preview_data", migration._json_type(), nullable=True)

    migration._validate_existing_column(
        connection,
        table_name="catalog_upload",
        expected=expected,
        actual={"type": postgresql.JSON(), "nullable": True},
    )

    with pytest.raises(RuntimeError, match="expected_nullable=True"):
        migration._validate_existing_column(
            connection,
            table_name="catalog_upload",
            expected=expected,
            actual={"type": postgresql.JSON(), "nullable": False},
        )


def test_model_schema_drift_repair_is_the_single_alembic_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == [
        "20260820_survey_response_origin_v1"
    ]
