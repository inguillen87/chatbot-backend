"""add durable municipal human handoff event v1

Revision ID: 20260905_municipio_handoff_v1
Revises: 20260905_municipio_reply_v1
Create Date: 2026-09-05 20:00:00.000000

The normalized event is the tenant-scoped idempotency receipt for the initial
human handoff request.  ``municipio_ticket.datos_extra.handoff`` remains a
compatibility projection and no provider message is implied by this ledger.
Rows are write-once, while deletion remains available to the aggregate's
authorized tenant/ticket retention and purge lifecycle.
"""

import sqlalchemy as sa
from alembic import op

revision = "20260905_municipio_handoff_v1"
down_revision = "20260905_municipio_reply_v1"
branch_labels = None
depends_on = None


def _create_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION municipio_ticket_handoff_event_immutable()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'municipio ticket handoff events are immutable';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_municipio_ticket_handoff_event_immutable
            BEFORE UPDATE ON municipio_ticket_handoff_event
            FOR EACH ROW EXECUTE FUNCTION municipio_ticket_handoff_event_immutable()
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_municipio_ticket_handoff_event_update
            BEFORE UPDATE ON municipio_ticket_handoff_event
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'municipio ticket handoff events are immutable');
            END
            """
        )


def _drop_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_municipio_ticket_handoff_event_immutable "
            "ON municipio_ticket_handoff_event"
        )
        op.execute("DROP FUNCTION IF EXISTS municipio_ticket_handoff_event_immutable()")
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_municipio_ticket_handoff_event_update")


def upgrade() -> None:
    op.create_table(
        "municipio_ticket_handoff_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column(
            "source_model",
            sa.String(length=32),
            server_default="MunicipioTicket",
            nullable=False,
        ),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("comment_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column(
            "action",
            sa.String(length=24),
            server_default="handoff",
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="requested",
            nullable=False,
        ),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("previous_assignee_user_id", sa.Integer(), nullable=True),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "projection_contract_version",
            sa.String(length=48),
            server_default="inbox.handoff.v1",
            nullable=False,
        ),
        sa.Column(
            "contract_version",
            sa.String(length=48),
            server_default="municipio_ticket.handoff_event.v1",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_model = 'MunicipioTicket'",
            name="ck_municipio_handoff_source_model",
        ),
        sa.CheckConstraint(
            "action = 'handoff' AND status = 'requested'",
            name="ck_municipio_handoff_action_status",
        ),
        sa.CheckConstraint(
            "channel IN ('operator', 'live_chat', 'phone')",
            name="ck_municipio_handoff_channel",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_municipio_handoff_reason_nonempty",
        ),
        sa.CheckConstraint(
            "length(reason) <= 500",
            name="ck_municipio_handoff_reason_bounded",
        ),
        sa.CheckConstraint(
            "length(trim(event_id)) > 0",
            name="ck_municipio_handoff_event_id_nonempty",
        ),
        sa.CheckConstraint(
            "length(idempotency_key_hash) = 64 AND length(request_digest) = 64",
            name="ck_municipio_handoff_digests",
        ),
        sa.CheckConstraint(
            "projection_contract_version = 'inbox.handoff.v1' AND "
            "contract_version = 'municipio_ticket.handoff_event.v1'",
            name="ck_municipio_handoff_contract_versions",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant_profile.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["municipio_ticket.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["comment_id"], ["ticket_comentario.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["previous_assignee_user_id"], ["user.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "event_id",
            name="uq_municipio_handoff_tenant_event",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_municipio_handoff_tenant_idempotency",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "comment_id",
            name="uq_municipio_handoff_tenant_comment",
        ),
    )
    op.create_index(
        "ix_municipio_handoff_ticket",
        "municipio_ticket_handoff_event",
        ["tenant_id", "ticket_id", "created_at", "id"],
        unique=False,
    )
    _create_immutability_guards()


def downgrade() -> None:
    _drop_immutability_guards()
    op.drop_index(
        "ix_municipio_handoff_ticket",
        table_name="municipio_ticket_handoff_event",
    )
    op.drop_table("municipio_ticket_handoff_event")
