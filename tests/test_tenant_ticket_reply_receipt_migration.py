from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from alembic.config import Config
import sqlalchemy as sa


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT
    / "migrations"
    / "versions"
    / "20260815_extend_ticket_receipts_for_tenant_replies.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "tenant_ticket_reply_receipt_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _old_schema(metadata: sa.MetaData) -> None:
    sa.Table(
        "tenant_profile",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    sa.Table(
        "ticket_domain_effect_receipt",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenant_profile.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(191), nullable=False),
        sa.Column("effect_kind", sa.String(48), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("contract_version", sa.String(48), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "effect_kind IN ('ticket.create.municipio', 'ticket.create.pyme', "
            "'ticket.comment.municipio', 'ticket.comment.pyme')",
            name="ck_ticket_domain_effect_kind",
        ),
        sa.CheckConstraint(
            "resource_type IN ('municipio_ticket', 'pyme_ticket', 'ticket_comentario')",
            name="ck_ticket_domain_effect_resource_type",
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64",
            name="ck_ticket_domain_effect_payload_hash",
        ),
        sa.CheckConstraint(
            "resource_id > 0",
            name="ck_ticket_domain_effect_resource_id_positive",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_ticket_domain_effect_tenant_idempotency",
        ),
    )


def test_tenant_reply_receipt_constraint_upgrade_is_additive_and_idempotent(tmp_path):
    database_path = tmp_path / "tenant-ticket-receipts.sqlite3"
    engine = sa.create_engine(f"sqlite:///{database_path.as_posix()}")
    metadata = sa.MetaData()
    _old_schema(metadata)
    metadata.create_all(engine)
    migration = _load_migration()

    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1)"))
        connection.execute(
            sa.text(
                "INSERT INTO ticket_domain_effect_receipt "
                "(id, tenant_id, idempotency_key, effect_kind, payload_hash, "
                "resource_type, resource_id, result_json, contract_version, "
                "created_at, updated_at) VALUES "
                "(1, 1, 'existing-key', 'ticket.comment.municipio', :digest, "
                "'ticket_comentario', 10, '{}', 'ticket.domain_effect.v1', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"digest": "a" * 64},
        )
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            migration.upgrade()

        checks = {
            item["name"]: "".join(str(item.get("sqltext") or "").lower().split())
            for item in sa.inspect(connection).get_check_constraints(
                "ticket_domain_effect_receipt"
            )
        }
        assert "ticket.comment.tenant" in checks["ck_ticket_domain_effect_kind"]
        assert "tenant_ticket" in checks["ck_ticket_domain_effect_resource_type"]
        assert connection.scalar(
            sa.text("SELECT COUNT(*) FROM ticket_domain_effect_receipt WHERE id = 1")
        ) == 1
        connection.execute(
            sa.text(
                "INSERT INTO ticket_domain_effect_receipt "
                "(id, tenant_id, idempotency_key, effect_kind, payload_hash, "
                "resource_type, resource_id, result_json, contract_version, "
                "created_at, updated_at) VALUES "
                "(2, 1, 'tenant-reply-key', 'ticket.comment.tenant', :digest, "
                "'tenant_ticket', 11, '{}', 'ticket.domain_effect.v1', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"digest": "b" * 64},
        )


def test_tenant_reply_receipt_migration_is_the_single_alembic_head():
    config = Config(str(ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["20260820_survey_response_origin_v1"]
