"""Add append-only managed interview assignment history.

Revision ID: 20260802_interview_assignment_v1
Revises: 20260802_campaign_prepare_v1
Create Date: 2026-08-02
"""

from alembic import op
import sqlalchemy as sa


revision = "20260802_interview_assignment_v1"
down_revision = "20260802_campaign_prepare_v1"
branch_labels = None
depends_on = None


_REASONS = (
    "initial_assignment",
    "workload_balance",
    "availability",
    "specialty_match",
    "continuity",
    "supervisor_override",
)


def _create_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION interview_assignment_immutable()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'interview assignment history is immutable';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_interview_assignment_immutable
            BEFORE UPDATE OR DELETE ON interview_assignment
            FOR EACH ROW EXECUTE FUNCTION interview_assignment_immutable()
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_interview_assignment_update_immutable
            BEFORE UPDATE ON interview_assignment
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'interview assignment history is immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_interview_assignment_delete_immutable
            BEFORE DELETE ON interview_assignment
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'interview assignment history is immutable');
            END
            """
        )


def _drop_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_interview_assignment_immutable "
            "ON interview_assignment"
        )
        op.execute("DROP FUNCTION IF EXISTS interview_assignment_immutable()")
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_interview_assignment_update_immutable")
        op.execute("DROP TRIGGER IF EXISTS trg_interview_assignment_delete_immutable")


def upgrade() -> None:
    reason_sql = ", ".join(f"'{value}'" for value in _REASONS)
    op.create_table(
        "interview_assignment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("interview_session_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("assignee_user_id", sa.Integer(), nullable=False),
        sa.Column("previous_assignee_user_id", sa.Integer(), nullable=False),
        sa.Column("assigned_by_user_id", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=40), nullable=False),
        sa.Column("supersedes_assignment_id", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("version > 0", name="ck_interview_assignment_version"),
        sa.CheckConstraint(
            f"reason_code IN ({reason_sql})",
            name="ck_interview_assignment_reason_code",
        ),
        sa.CheckConstraint(
            "(version = 1 AND supersedes_assignment_id IS NULL) OR "
            "(version > 1 AND supersedes_assignment_id IS NOT NULL)",
            name="ck_interview_assignment_predecessor",
        ),
        sa.CheckConstraint(
            "supersedes_assignment_id IS NULL OR supersedes_assignment_id <> id",
            name="ck_interview_assignment_not_self_superseding",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_interview_assignment_request_hash",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "interview_session_id"],
            ["interview_session.tenant_id", "interview_session.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["assignee_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["previous_assignee_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["assigned_by_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "interview_session_id", "supersedes_assignment_id"],
            [
                "interview_assignment.tenant_id",
                "interview_assignment.interview_session_id",
                "interview_assignment.id",
            ],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_interview_assignment_tenant_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "interview_session_id",
            "id",
            name="uq_interview_assignment_session_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "interview_session_id",
            "version",
            name="uq_interview_assignment_session_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_interview_assignment_tenant_idempotency",
        ),
    )
    op.create_index(
        "ix_interview_assignment_tenant_session_version",
        "interview_assignment",
        ["tenant_id", "interview_session_id", "version"],
        unique=False,
    )
    op.create_index(
        "ix_interview_assignment_tenant_assignee",
        "interview_assignment",
        ["tenant_id", "assignee_user_id", "created_at"],
        unique=False,
    )
    _create_immutability_guards()


def downgrade() -> None:
    total = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM interview_assignment")
    ).scalar_one()
    if total:
        raise RuntimeError(
            "interview assignment history exists; downgrade would erase audit records"
        )

    _drop_immutability_guards()
    op.drop_index(
        "ix_interview_assignment_tenant_assignee",
        table_name="interview_assignment",
    )
    op.drop_index(
        "ix_interview_assignment_tenant_session_version",
        table_name="interview_assignment",
    )
    op.drop_table("interview_assignment")
