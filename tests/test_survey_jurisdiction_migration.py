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
    / "20260820_add_survey_content_jurisdiction.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "survey_content_jurisdiction_v1_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _base_schema(metadata: sa.MetaData) -> None:
    sa.Table("user", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table(
        "tenant_profile",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    sa.Table(
        "enc_encuesta",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_enc_encuesta_tenant_id"),
    )


def _insert_receipt(connection, *, receipt_id: int = 1, tenant_id: int = 1) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO survey_content_receipt ("
            "id, tenant_id, survey_id, contract_version, event_type, decision, "
            "content_sha256, jurisdiction_ref, content_origin, actor_user_id, "
            "receipt_json, receipt_sha256, created_at"
            ") VALUES ("
            ":id, :tenant_id, 10, 'surveys.content_receipt.v1', "
            "'review_approved', 'approved', :content_hash, 'ar:mun:junin', "
            "'legacy_unverified', 1, '{}', :receipt_hash, CURRENT_TIMESTAMP)"
        ),
        {
            "id": receipt_id,
            "tenant_id": tenant_id,
            "content_hash": "a" * 64,
            "receipt_hash": f"{receipt_id:064x}",
        },
    )


def test_sqlite_additive_defaults_scope_and_immutable_receipts(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{(tmp_path / 'survey-jurisdiction.sqlite3').as_posix()}"
    )
    metadata = sa.MetaData()
    _base_schema(metadata)
    metadata.create_all(engine)
    migration = _load_migration_module()
    assert migration.revision == "20260820_survey_content_jurisdiction_v1"
    assert migration.down_revision == "20260820_survey_response_origin_v1"

    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        connection.execute(sa.text('INSERT INTO "user" (id) VALUES (1), (2)'))
        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1), (2)"))
        connection.execute(
            sa.text("INSERT INTO enc_encuesta (id, tenant_id) VALUES (10, 1)")
        )
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        inspector = sa.inspect(connection)
        assert "survey_content_receipt" in inspector.get_table_names()
        tenant_columns = {
            column["name"] for column in inspector.get_columns("tenant_profile")
        }
        assert {
            "jurisdiction_ref",
            "jurisdiction_status",
            "jurisdiction_evidence_ref",
            "jurisdiction_verified_by_user_id",
            "jurisdiction_verified_at",
        }.issubset(tenant_columns)
        survey_columns = {
            column["name"] for column in inspector.get_columns("enc_encuesta")
        }
        assert {
            "jurisdiction_ref",
            "content_origin",
            "content_origin_ref",
        }.issubset(survey_columns)

        legacy = connection.execute(
            sa.text(
                "SELECT jurisdiction_ref, content_origin, content_origin_ref "
                "FROM enc_encuesta WHERE id = 10"
            )
        ).mappings().one()
        assert legacy == {
            "jurisdiction_ref": None,
            "content_origin": "legacy_unverified",
            "content_origin_ref": None,
        }
        tenant = connection.execute(
            sa.text(
                "SELECT jurisdiction_ref, jurisdiction_status "
                "FROM tenant_profile WHERE id = 1"
            )
        ).mappings().one()
        assert tenant == {
            "jurisdiction_ref": None,
            "jurisdiction_status": "unverified",
        }

        _insert_receipt(connection)
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "UPDATE survey_content_receipt SET decision = 'blocked' "
                        "WHERE id = 1"
                    )
                )
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text("DELETE FROM survey_content_receipt WHERE id = 1")
                )
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                _insert_receipt(connection, receipt_id=2, tenant_id=2)

        with Operations.context(context):
            migration.downgrade()
        inspector = sa.inspect(connection)
        assert "survey_content_receipt" not in inspector.get_table_names()
        assert "content_origin" not in {
            column["name"] for column in inspector.get_columns("enc_encuesta")
        }
        assert "jurisdiction_status" not in {
            column["name"] for column in inspector.get_columns("tenant_profile")
        }

    engine.dispose()


def test_postgresql_receipt_trigger_guards_update_and_delete(monkeypatch):
    migration = _load_migration_module()
    statements: list[str] = []

    class _Dialect:
        name = "postgresql"

    class _Bind:
        dialect = _Dialect()

    class _FakeOp:
        @staticmethod
        def get_bind():
            return _Bind()

        @staticmethod
        def execute(statement):
            statements.append(str(statement))

    monkeypatch.setattr(migration, "op", _FakeOp())
    migration._create_receipt_immutability_trigger()
    sql = "\n".join(statements)
    assert "BEFORE UPDATE OR DELETE" in sql
    assert "append-only" in sql
