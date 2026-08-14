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
    / "20260814_backfill_survey_tenant_scope_aliases.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "survey_tenant_scope_canonical_v1_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema(metadata: sa.MetaData) -> None:
    sa.Table(
        "tenant_profile",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("municipio_id", sa.Integer()),
        sa.Column("pyme_id", sa.Integer()),
        sa.Column("encuestas_tenant_id", sa.Integer()),
    )
    sa.Table(
        "enc_encuesta",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
    )
    sa.Table(
        "enc_respuesta",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("encuesta_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
    )
    sa.Table(
        "enc_anchor_snapshot",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("encuesta_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
    )
    sa.Table(
        "survey_governance_release",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("survey_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
    )


def _engine(tmp_path, filename: str):
    engine = sa.create_engine(f"sqlite:///{(tmp_path / filename).as_posix()}")
    metadata = sa.MetaData()
    _schema(metadata)
    metadata.create_all(engine)
    return engine


def test_migration_physically_canonicalizes_unique_legacy_owner_scope(tmp_path):
    engine = _engine(tmp_path, "survey-scope-canonical.sqlite3")
    migration = _load_migration_module()
    assert migration.revision == "20260814_survey_scope_canonical_v1"
    assert migration.down_revision == "20260802_whatsapp_workflow_v1"

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO tenant_profile "
                "(id, municipio_id, pyme_id, encuestas_tenant_id) VALUES "
                "(6, 142, NULL, NULL), (22, 500, NULL, NULL), "
                "(30, NULL, 300, 300)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO enc_encuesta (id, tenant_id) VALUES "
                "(1, 142), (2, 22), (3, 300)"
            )
        )
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            migration.upgrade()

        aliases = dict(
            connection.execute(
                sa.text(
                    "SELECT id, encuestas_tenant_id FROM tenant_profile ORDER BY id"
                )
            ).all()
        )
        surveys = dict(
            connection.execute(
                sa.text("SELECT id, tenant_id FROM enc_encuesta ORDER BY id")
            ).all()
        )
        assert aliases == {6: 142, 22: None, 30: 300}
        assert surveys == {1: 6, 2: 22, 3: 30}

    engine.dispose()


def test_migration_prefers_canonical_id_over_legacy_owner_collision(tmp_path):
    engine = _engine(tmp_path, "survey-scope-collision.sqlite3")
    migration = _load_migration_module()

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO tenant_profile "
                "(id, municipio_id, pyme_id, encuestas_tenant_id) VALUES "
                "(6, 142, NULL, NULL), (142, 900, NULL, NULL)"
            )
        )
        connection.execute(
            sa.text("INSERT INTO enc_encuesta (id, tenant_id) VALUES (1, 142)")
        )

        migration._canonicalize_survey_tenant_scopes(connection)

        assert connection.execute(
            sa.text("SELECT tenant_id FROM enc_encuesta WHERE id = 1")
        ).scalar_one() == 142
        assert connection.execute(
            sa.text(
                "SELECT encuestas_tenant_id FROM tenant_profile WHERE id = 6"
            )
        ).scalar_one() is None

    engine.dispose()


def test_canonical_id_precedence_preserves_existing_history_untouched(tmp_path):
    engine = _engine(tmp_path, "survey-scope-canonical-history.sqlite3")
    migration = _load_migration_module()

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO tenant_profile "
                "(id, municipio_id, pyme_id, encuestas_tenant_id) VALUES "
                "(20, 22, NULL, NULL), (22, 4, NULL, NULL)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO enc_encuesta (id, tenant_id) VALUES (1, 22)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO enc_respuesta (id, encuesta_id, tenant_id) "
                "VALUES (10, 1, 22)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO survey_governance_release "
                "(id, survey_id, tenant_id) VALUES (50, 1, 22)"
            )
        )

        migration._canonicalize_survey_tenant_scopes(connection)

        assert connection.execute(
            sa.text("SELECT tenant_id FROM enc_encuesta WHERE id = 1")
        ).scalar_one() == 22
        assert connection.execute(
            sa.text("SELECT tenant_id FROM enc_respuesta WHERE id = 10")
        ).scalar_one() == 22
        assert connection.execute(
            sa.text("SELECT tenant_id FROM survey_governance_release WHERE id = 50")
        ).scalar_one() == 22

    engine.dispose()


def test_migration_fails_closed_on_conflicting_explicit_alias(tmp_path):
    engine = _engine(tmp_path, "survey-scope-explicit-alias-collision.sqlite3")
    migration = _load_migration_module()

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO tenant_profile "
                "(id, municipio_id, pyme_id, encuestas_tenant_id) VALUES "
                "(3, 30, NULL, NULL), (5, 50, NULL, 3)"
            )
        )
        connection.execute(
            sa.text("INSERT INTO enc_encuesta (id, tenant_id) VALUES (1, 3)")
        )

        with pytest.raises(RuntimeError, match="conflicting canonical and alias"):
            migration._canonicalize_survey_tenant_scopes(connection)

        assert connection.execute(
            sa.text("SELECT tenant_id FROM enc_encuesta WHERE id = 1")
        ).scalar_one() == 3

    engine.dispose()


def test_migration_fails_closed_before_touching_immutable_ledgers(tmp_path):
    engine = _engine(tmp_path, "survey-scope-governed.sqlite3")
    migration = _load_migration_module()

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO tenant_profile "
                "(id, municipio_id, pyme_id, encuestas_tenant_id) "
                "VALUES (6, 142, NULL, NULL)"
            )
        )
        connection.execute(
            sa.text("INSERT INTO enc_encuesta (id, tenant_id) VALUES (1, 142)")
        )
        connection.execute(
            sa.text(
                "INSERT INTO survey_governance_release (id, survey_id, tenant_id) "
                "VALUES (50, 1, 142)"
            )
        )

        with pytest.raises(RuntimeError, match="immutable dependencies"):
            migration._canonicalize_survey_tenant_scopes(connection)

        assert connection.execute(
            sa.text("SELECT tenant_id FROM enc_encuesta WHERE id = 1")
        ).scalar_one() == 142
        assert connection.execute(
            sa.text(
                "SELECT encuestas_tenant_id FROM tenant_profile WHERE id = 6"
            )
        ).scalar_one() is None

    engine.dispose()


@pytest.mark.parametrize("history_kind", ["response", "anchor"])
def test_migration_fails_closed_on_participation_or_anchor_history(
    tmp_path,
    history_kind,
):
    engine = _engine(tmp_path, f"survey-scope-{history_kind}.sqlite3")
    migration = _load_migration_module()

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO tenant_profile "
                "(id, municipio_id, pyme_id, encuestas_tenant_id) "
                "VALUES (6, 142, NULL, NULL), (22, 500, NULL, NULL)"
            )
        )
        connection.execute(
            sa.text("INSERT INTO enc_encuesta (id, tenant_id) VALUES (1, 142)")
        )
        if history_kind == "response":
            connection.execute(
                sa.text(
                    "INSERT INTO enc_respuesta (id, encuesta_id, tenant_id) "
                    "VALUES (10, 1, 142)"
                )
            )
            expected = "response identity history"
        else:
            connection.execute(
                sa.text(
                    "INSERT INTO enc_anchor_snapshot (id, encuesta_id, tenant_id) "
                    "VALUES (20, 1, 142)"
                )
            )
            expected = "cryptographic anchor history"

        with pytest.raises(RuntimeError, match=expected):
            migration._canonicalize_survey_tenant_scopes(connection)

    engine.dispose()
