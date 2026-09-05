from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
import sqlalchemy as sa


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT / "migrations" / "versions" / "20260729_add_pyme_order_payload_hash.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "pyme_order_payload_hash_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sqlite_upgrade_preserves_legacy_rows_and_enforces_hash_contract(tmp_path):
    engine = sa.create_engine(f"sqlite:///{(tmp_path / 'orders.sqlite3').as_posix()}")
    metadata = sa.MetaData()
    sa.Table(
        "pyme_pedido",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
    )
    metadata.create_all(engine)
    migration = _load_migration_module()

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO pyme_pedido (id, idempotency_key) VALUES (1, 'legacy-key')"
            )
        )
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        columns = {column["name"] for column in sa.inspect(connection).get_columns("pyme_pedido")}
        assert "idempotency_payload_hash" in columns
        assert connection.execute(
            sa.text("SELECT idempotency_payload_hash FROM pyme_pedido WHERE id = 1")
        ).scalar() is None

        connection.execute(
            sa.text(
                "INSERT INTO pyme_pedido (id, idempotency_key, idempotency_payload_hash) "
                "VALUES (2, 'bound-key', :payload_hash)"
            ),
            {"payload_hash": "a" * 64},
        )
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO pyme_pedido "
                        "(id, idempotency_key, idempotency_payload_hash) "
                        "VALUES (3, 'bad-hash', 'short')"
                    )
                )
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO pyme_pedido "
                        "(id, idempotency_key, idempotency_payload_hash) "
                        "VALUES (4, NULL, :payload_hash)"
                    ),
                    {"payload_hash": "b" * 64},
                )

        with Operations.context(context):
            migration.downgrade()
        columns = {column["name"] for column in sa.inspect(connection).get_columns("pyme_pedido")}
        assert "idempotency_payload_hash" not in columns

    engine.dispose()


def test_domain_effect_outbox_is_single_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == [
        "20260904_tenant_reply_delivery_v1"
    ]
