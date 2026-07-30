from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT
    / "migrations"
    / "versions"
    / "20260730_ticket_tenant_scope_v1.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("ticket_tenant_scope_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ticket_tenant_scope_migration_backfills_only_unique_owner(tmp_path):
    engine = sa.create_engine(f"sqlite:///{(tmp_path / 'scope.sqlite3').as_posix()}")
    metadata = sa.MetaData()
    sa.Table(
        "tenant_profile",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("municipio_id", sa.Integer(), nullable=True),
        sa.Column("pyme_id", sa.Integer(), nullable=True),
    )
    sa.Table(
        "municipio_ticket",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("municipio_id", sa.Integer(), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=True),
    )
    metadata.create_all(engine)
    migration = _load_migration()

    assert migration.revision == "20260730_ticket_tenant_scope_v1"
    assert migration.down_revision == "20260730_voice_consent_v1"

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO tenant_profile (id, municipio_id, pyme_id) VALUES "
                "(1, 10, NULL), (2, 20, NULL), (3, 20, NULL), (4, NULL, 40)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO municipio_ticket (id, municipio_id, tenant_id) VALUES "
                "(101, 10, NULL), (102, 20, NULL), (103, 30, NULL), "
                "(104, 20, 3), (105, 40, NULL)"
            )
        )
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        rows = dict(
            connection.execute(
                sa.text("SELECT id, tenant_id FROM municipio_ticket ORDER BY id")
            ).all()
        )
        assert rows == {
            101: 1,
            102: None,
            103: None,
            104: 3,
            105: 4,
        }
        index_names = {
            index["name"]
            for index in sa.inspect(connection).get_indexes("municipio_ticket")
        }
        assert "ix_municipio_ticket_owner_tenant" in index_names

        with Operations.context(context):
            migration.downgrade()

        remaining_index_names = {
            index["name"]
            for index in sa.inspect(connection).get_indexes("municipio_ticket")
        }
        assert "ix_municipio_ticket_owner_tenant" not in remaining_index_names
        assert connection.execute(
            sa.text("SELECT tenant_id FROM municipio_ticket WHERE id = 101")
        ).scalar_one() == 1

    engine.dispose()
