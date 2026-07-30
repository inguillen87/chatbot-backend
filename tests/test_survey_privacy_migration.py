from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT / "migrations" / "versions" / "20260730_add_survey_privacy_v1.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "survey_privacy_v1_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_schema(metadata: sa.MetaData) -> None:
    sa.Table(
        "enc_encuesta",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("puntos_recompensa", sa.Integer(), nullable=True),
    )
    sa.Table(
        "enc_respuesta",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("dni", sa.String(32), nullable=True),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("ua", sa.String(255), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lng", sa.Float(), nullable=True),
        sa.Column("utm_source", sa.String(120), nullable=True),
        sa.Column("utm_campaign", sa.String(120), nullable=True),
        sa.Column("edad", sa.Integer(), nullable=True),
        sa.Column("anio_nacimiento", sa.Integer(), nullable=True),
        sa.Column("metadata_payload", sa.JSON(), nullable=True),
    )


def test_sqlite_upgrade_backfills_legacy_and_enforces_source_anonymous_contract(
    tmp_path,
):
    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / 'survey-privacy.sqlite3').as_posix()}"
    )
    metadata = sa.MetaData()
    _legacy_schema(metadata)
    metadata.create_all(engine)
    migration = _load_migration_module()

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO enc_encuesta (id, puntos_recompensa) VALUES (1, 0)"
            )
        )
        connection.execute(sa.text("INSERT INTO enc_respuesta (id) VALUES (1)"))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        assert connection.execute(
            sa.text(
                "SELECT privacy_mode, privacy_consent_required "
                "FROM enc_encuesta WHERE id = 1"
            )
        ).one() == ("legacy", 0)
        assert connection.execute(
            sa.text("SELECT privacy_mode FROM enc_respuesta WHERE id = 1")
        ).scalar_one() == "legacy"

        connection.execute(
            sa.text(
                "INSERT INTO enc_encuesta "
                "(id, puntos_recompensa, privacy_mode, privacy_policy_version, "
                "privacy_policy_url, privacy_consent_required, response_retention_days) "
                "VALUES (2, 0, 'source_anonymous', '2026-07', "
                "'https://chatboc.ar/privacy/2026-07', 1, 365)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO enc_respuesta "
                "(id, privacy_mode, privacy_policy_version, "
                "privacy_consent_recorded_at, retention_expires_at) "
                "VALUES (2, 'source_anonymous', '2026-07', CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP)"
            )
        )

        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO enc_respuesta "
                        "(id, privacy_mode, privacy_policy_version, "
                        "privacy_consent_recorded_at, retention_expires_at, ip) "
                        "VALUES (3, 'source_anonymous', '2026-07', CURRENT_TIMESTAMP, "
                        "CURRENT_TIMESTAMP, '203.0.113.7')"
                    )
                )

        indexes = {
            index["name"]
            for index in sa.inspect(connection).get_indexes("enc_respuesta")
        }
        assert "ix_enc_respuesta_retention_expires_at" in indexes

        with Operations.context(context):
            migration.downgrade()
        survey_columns = {
            column["name"]
            for column in sa.inspect(connection).get_columns("enc_encuesta")
        }
        response_columns = {
            column["name"]
            for column in sa.inspect(connection).get_columns("enc_respuesta")
        }
        assert "privacy_mode" not in survey_columns
        assert "privacy_mode" not in response_columns

    engine.dispose()
