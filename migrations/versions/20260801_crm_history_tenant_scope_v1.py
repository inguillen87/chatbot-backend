"""Persist historical tenant scope for CRM notes and LLM review rows.

Revision ID: 20260801_crm_history_scope_v1
Revises: 20260730_interview_consent_proof_v3
Create Date: 2026-08-01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260801_crm_history_scope_v1"
down_revision = "20260730_interview_consent_proof_v3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("cliente_nota") as batch_op:
        batch_op.add_column(
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey(
                    "tenant_profile.id",
                    name="fk_cliente_nota_tenant_id_tenant_profile",
                ),
                nullable=True,
            )
        )
        batch_op.create_index("ix_cliente_nota_tenant_id", ["tenant_id"], unique=False)

    with op.batch_alter_table("llm_interaction_log") as batch_op:
        batch_op.add_column(
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey(
                    "tenant_profile.id",
                    name="fk_llm_interaction_log_tenant_id_tenant_profile",
                ),
                nullable=True,
            )
        )
        batch_op.create_index(
            "ix_llm_interaction_log_tenant_id",
            ["tenant_id"],
            unique=False,
        )

    # Legacy notes deliberately remain NULL.  A creator's *current* tenant is
    # not historical evidence: the employee may have moved between tenants
    # after writing the note.  Read paths quarantine NULL rows fail-closed;
    # only newly created notes receive an authoritative tenant snapshot.

    # LLM logs do have an immutable scope source persisted with the historical
    # conversation, so those rows may be backfilled from ChatSessionContext.
    op.execute(
        sa.text(
            """
            UPDATE llm_interaction_log
               SET tenant_id = (
                   SELECT c.tenant_id
                     FROM chat_session_context AS c
                    WHERE c.chat_session_id = llm_interaction_log.chat_session_id
               )
             WHERE tenant_id IS NULL
               AND EXISTS (
                   SELECT 1
                     FROM chat_session_context AS c
                     JOIN tenant_profile AS tp ON tp.id = c.tenant_id
                    WHERE c.chat_session_id = llm_interaction_log.chat_session_id
               )
            """
        )
    )


def downgrade():
    with op.batch_alter_table("llm_interaction_log") as batch_op:
        batch_op.drop_index("ix_llm_interaction_log_tenant_id")
        batch_op.drop_column("tenant_id")

    with op.batch_alter_table("cliente_nota") as batch_op:
        batch_op.drop_index("ix_cliente_nota_tenant_id")
        batch_op.drop_column("tenant_id")
