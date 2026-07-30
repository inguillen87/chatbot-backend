from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT / "migrations" / "versions" / "20260730_add_pyme_order_context.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "pyme_order_context_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sqlite_upgrade_preserves_legacy_rows_and_persists_order_context(tmp_path):
    engine = sa.create_engine(f"sqlite:///{(tmp_path / 'orders.sqlite3').as_posix()}")
    metadata = sa.MetaData()
    sa.Table(
        "pyme_pedido",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asunto", sa.String(255), nullable=True),
    )
    metadata.create_all(engine)
    migration = _load_migration_module()

    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO pyme_pedido (id, asunto) VALUES (1, 'legacy')")
        )
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        columns = {
            column["name"]: column
            for column in sa.inspect(connection).get_columns("pyme_pedido")
        }
        assert columns["rubro"]["type"].length == 100
        assert columns["channel"]["type"].length == 50
        assert connection.execute(
            sa.text("SELECT rubro, channel FROM pyme_pedido WHERE id = 1")
        ).one() == (None, None)

        connection.execute(
            sa.text(
                "INSERT INTO pyme_pedido (id, asunto, rubro, channel) "
                "VALUES (2, 'nuevo', 'Almacen', 'whatsapp')"
            )
        )
        assert connection.execute(
            sa.text("SELECT rubro, channel FROM pyme_pedido WHERE id = 2")
        ).one() == ("Almacen", "whatsapp")

        with Operations.context(context):
            migration.downgrade()
        remaining = {
            column["name"]
            for column in sa.inspect(connection).get_columns("pyme_pedido")
        }
        assert "rubro" not in remaining
        assert "channel" not in remaining

    engine.dispose()
