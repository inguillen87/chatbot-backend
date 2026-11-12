from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT
    / "migrations"
    / "versions"
    / "20260825_repair_legacy_municipio_ticket_tenant_scope.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "legacy_municipio_ticket_tenant_repair",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _database(tmp_path: Path, name: str = "tenant-repair.sqlite3"):
    engine = sa.create_engine(f"sqlite:///{(tmp_path / name).as_posix()}")
    metadata = sa.MetaData()
    sa.Table(
        "tenant_profile",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(), nullable=False, unique=True),
        sa.Column("tipo", sa.String(), nullable=False),
        sa.Column("municipio_id", sa.Integer(), nullable=True),
        sa.Column("pyme_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
    )
    sa.Table(
        "user",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=True),
        sa.Column("tipo_chat", sa.String(), nullable=True),
        sa.Column("empresa_id", sa.Integer(), nullable=True),
    )
    sa.Table(
        "municipio_ticket",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("municipio_id", sa.Integer(), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=True),
    )
    sa.Table(
        "archivo_adjunto",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("municipio_ticket_id", sa.Integer(), nullable=True),
    )
    metadata.create_all(engine)
    return engine


def _seed_valid_evidence(connection) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO tenant_profile "
            "(id, slug, tipo, municipio_id, pyme_id, is_active) VALUES "
            "(1, 'almacen', 'pyme', NULL, 1, 1), "
            "(22, 'junin', 'municipio', 500, NULL, 1)"
        )
    )
    connection.execute(
        sa.text(
            'INSERT INTO "user" (id, tenant_id, tipo_chat, empresa_id) VALUES '
            "(700, NULL, 'municipio', 500), "
            "(701, 22, 'municipio', 500), "
            "(800, 1, 'pyme', 1)"
        )
    )
    connection.execute(
        sa.text(
            "INSERT INTO municipio_ticket "
            "(id, user_id, municipio_id, tenant_id) VALUES "
            "(322, 700, 1, 1), "
            "(344, 700, 1, 1), "
            "(347, 701, 1, 1), "
            "(999, 700, 1, 1)"
        )
    )
    attachment_values = []
    next_id = 1
    for ticket_id, count in ((322, 15), (344, 1), (347, 1)):
        for offset in range(count):
            actor_id = 700 if offset % 2 == 0 else 701
            attachment_values.append(
                {
                    "id": next_id,
                    "user_id": actor_id,
                    "municipio_ticket_id": ticket_id,
                }
            )
            next_id += 1
    connection.execute(
        sa.text(
            "INSERT INTO archivo_adjunto (id, user_id, municipio_ticket_id) "
            "VALUES (:id, :user_id, :municipio_ticket_id)"
        ),
        attachment_values,
    )


def _run_upgrade(connection, migration) -> None:
    migration.op = Operations(MigrationContext.configure(connection))
    migration.upgrade()


def _ticket_scopes(connection) -> dict[int, tuple[int | None, int | None]]:
    return {
        int(row.id): (row.tenant_id, row.municipio_id)
        for row in connection.execute(
            sa.text(
                "SELECT id, tenant_id, municipio_id FROM municipio_ticket "
                "ORDER BY id"
            )
        )
    }


def test_repair_updates_only_three_evidence_backed_tickets_and_is_idempotent(
    tmp_path,
):
    engine = _database(tmp_path)
    migration = _load_migration()
    assert migration.revision == "20260825_legacy_municipio_ticket_scope_repair_v1"
    assert migration.down_revision == "20260825_demo_survey_participation_v1"

    with engine.begin() as connection:
        _seed_valid_evidence(connection)
        _run_upgrade(connection, migration)
        assert _ticket_scopes(connection) == {
            322: (22, 1),
            344: (22, 1),
            347: (22, 1),
            999: (1, 1),
        }

        # Re-running the data operation validates the same evidence and does
        # not rewrite, duplicate, or broaden the repaired set.
        _run_upgrade(connection, migration)
        assert _ticket_scopes(connection) == {
            322: (22, 1),
            344: (22, 1),
            347: (22, 1),
            999: (1, 1),
        }

        migration.downgrade()
        assert _ticket_scopes(connection)[322] == (22, 1)

    engine.dispose()


@pytest.mark.parametrize(
    ("mutation_sql", "reason_code"),
    [
        (
            'UPDATE "user" SET tipo_chat = \'pyme\' WHERE id = 700',
            "attachment_actor_scope_conflict",
        ),
        (
            'UPDATE "user" SET tenant_id = 1 WHERE id = 700',
            "attachment_actor_scope_conflict",
        ),
        (
            "UPDATE tenant_profile SET is_active = 0 WHERE slug = 'junin'",
            "target_tenant_contract_changed",
        ),
        (
            "INSERT INTO tenant_profile "
            "(id, slug, tipo, municipio_id, pyme_id, is_active) "
            "VALUES (23, 'junin-shadow', 'municipio', 500, NULL, 1)",
            "attachment_actor_owner_not_unique",
        ),
        (
            "UPDATE archivo_adjunto SET user_id = NULL WHERE id = 1",
            "attachment_actor_missing",
        ),
        (
            "DELETE FROM archivo_adjunto WHERE id = 1",
            "attachment_evidence_count_changed",
        ),
        (
            "UPDATE municipio_ticket SET tenant_id = NULL WHERE id = 344",
            "ticket_scope_state_changed",
        ),
    ],
)
def test_repair_aborts_atomically_when_any_authoritative_precondition_changes(
    tmp_path,
    mutation_sql,
    reason_code,
):
    engine = _database(tmp_path, f"tenant-repair-{reason_code}.sqlite3")
    migration = _load_migration()
    with engine.begin() as connection:
        _seed_valid_evidence(connection)
        connection.execute(sa.text(mutation_sql))

    with engine.connect() as connection:
        transaction = connection.begin()
        before = _ticket_scopes(connection)
        with pytest.raises(
            migration._RepairPreconditionError,
            match=reason_code,
        ):
            _run_upgrade(connection, migration)
        transaction.rollback()

    with engine.connect() as connection:
        assert _ticket_scopes(connection) == before
    engine.dispose()


def test_repair_does_not_guess_on_empty_database_but_rejects_partial_source_set(
    tmp_path,
):
    migration = _load_migration()
    engine = _database(tmp_path)

    with engine.begin() as connection:
        _run_upgrade(connection, migration)
        connection.execute(
            sa.text(
                "INSERT INTO municipio_ticket "
                "(id, user_id, municipio_id, tenant_id) VALUES "
                "(322, NULL, NULL, NULL)"
            )
        )
        with pytest.raises(
            migration._RepairPreconditionError,
            match="expected_ticket_set_changed",
        ):
            _run_upgrade(connection, migration)

    engine.dispose()
