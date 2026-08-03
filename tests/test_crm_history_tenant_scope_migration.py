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
    / "20260801_crm_history_tenant_scope_v1.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "crm_history_tenant_scope_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_crm_history_scope_quarantines_legacy_notes_and_backfills_context_logs(tmp_path):
    engine = sa.create_engine(f"sqlite:///{(tmp_path / 'crm-scope.sqlite3').as_posix()}")
    metadata = sa.MetaData()
    sa.Table(
        "tenant_profile",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    sa.Table(
        "user",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=True),
    )
    sa.Table(
        "cliente_nota",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cliente_user_id", sa.Integer(), nullable=False),
        sa.Column("creada_por_user_id", sa.Integer(), nullable=False),
        sa.Column("nota", sa.Text(), nullable=False),
    )
    sa.Table(
        "chat_session_context",
        metadata,
        sa.Column("chat_session_id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=True),
    )
    sa.Table(
        "llm_interaction_log",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chat_session_id", sa.String(36), nullable=False),
        sa.Column("user_query", sa.Text(), nullable=False),
    )
    metadata.create_all(engine)
    migration = _load_migration()

    assert migration.revision == "20260801_crm_history_scope_v1"
    assert migration.down_revision == "20260730_interview_consent_proof_v3"

    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1), (2)"))
        connection.execute(
            sa.text(
                'INSERT INTO "user" (id, tenant_id) VALUES '
                "(10, 1), (11, NULL), (12, 999)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO cliente_nota "
                "(id, cliente_user_id, creada_por_user_id, nota) VALUES "
                "(101, 50, 10, 'valid'), "
                "(102, 50, 11, 'no scope'), "
                "(103, 50, 12, 'dangling')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO chat_session_context (chat_session_id, tenant_id) VALUES "
                "('session-valid', 2), ('session-null', NULL), ('session-dangling', 999)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO llm_interaction_log (id, chat_session_id, user_query) VALUES "
                "(201, 'session-valid', 'valid'), "
                "(202, 'session-null', 'no scope'), "
                "(203, 'session-dangling', 'dangling')"
            )
        )

        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        assert dict(
            connection.execute(
                sa.text("SELECT id, tenant_id FROM cliente_nota ORDER BY id")
            ).all()
        ) == {101: None, 102: None, 103: None}
        assert dict(
            connection.execute(
                sa.text("SELECT id, tenant_id FROM llm_interaction_log ORDER BY id")
            ).all()
        ) == {201: 2, 202: None, 203: None}
        inspector = sa.inspect(connection)
        assert "ix_cliente_nota_tenant_id" in {
            index["name"] for index in inspector.get_indexes("cliente_nota")
        }
        assert "ix_llm_interaction_log_tenant_id" in {
            index["name"] for index in inspector.get_indexes("llm_interaction_log")
        }

        with Operations.context(context):
            migration.downgrade()

        assert "tenant_id" not in {
            column["name"] for column in sa.inspect(connection).get_columns("cliente_nota")
        }
        assert "tenant_id" not in {
            column["name"]
            for column in sa.inspect(connection).get_columns("llm_interaction_log")
        }

    engine.dispose()
