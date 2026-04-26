"""BE-05 notification orchestrator core tables

Revision ID: 20260331_be05_notification_orchestrator
Revises: 20260331_be02_conversation_linking
Create Date: 2026-03-31 01:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260331_be05_notification_orchestrator"
down_revision = "20260331_be02_conversation_linking"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "notification_template",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("subject_template", sa.String(length=255), nullable=True),
        sa.Column("body_template", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("quiet_hours_start", sa.Integer(), nullable=True),
        sa.Column("quiet_hours_end", sa.Integer(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "key", "channel", name="uq_notification_template_tenant_key_channel"),
    )
    op.create_index("ix_notification_template_tenant_id", "notification_template", ["tenant_id"])

    op.create_table(
        "notification",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("template_id", sa.String(length=36), nullable=True),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("recipient", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"]),
        sa.ForeignKeyConstraint(["template_id"], ["notification_template.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_notification_tenant_idempotency"),
    )
    op.create_index("ix_notification_tenant_id", "notification", ["tenant_id"])
    op.create_index("ix_notification_channel", "notification", ["channel"])
    op.create_index("ix_notification_status", "notification", ["status"])
    op.create_index("ix_notification_next_retry_at", "notification", ["next_retry_at"])

    op.create_table(
        "notification_attempt",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("notification_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=True),
        sa.Column("provider_message_id", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["notification_id"], ["notification.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notification_attempt_notification_id", "notification_attempt", ["notification_id"])
    op.create_index("ix_notification_attempt_tenant_id", "notification_attempt", ["tenant_id"])


def downgrade():
    op.drop_index("ix_notification_attempt_tenant_id", table_name="notification_attempt")
    op.drop_index("ix_notification_attempt_notification_id", table_name="notification_attempt")
    op.drop_table("notification_attempt")

    op.drop_index("ix_notification_next_retry_at", table_name="notification")
    op.drop_index("ix_notification_status", table_name="notification")
    op.drop_index("ix_notification_channel", table_name="notification")
    op.drop_index("ix_notification_tenant_id", table_name="notification")
    op.drop_table("notification")

    op.drop_index("ix_notification_template_tenant_id", table_name="notification_template")
    op.drop_table("notification_template")
