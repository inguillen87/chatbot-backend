"""BE-06 roles org units audit foundation

Revision ID: 20260331_be06_roles_org_units_audit
Revises: 20260331_be05_notification_orchestrator
Create Date: 2026-03-31 01:30:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "20260331_be06_roles_org_units_audit"
down_revision = "20260331_be05_notification_orchestrator"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "org_unit",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["parent_id"], ["org_unit.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_org_unit_tenant_name"),
    )
    op.create_index("ix_org_unit_tenant_id", "org_unit", ["tenant_id"])

    op.create_table(
        "user_org_unit",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("org_unit_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_unit_id"], ["org_unit.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "org_unit_id", name="uq_user_org_unit"),
    )
    op.create_index("ix_user_org_unit_tenant_id", "user_org_unit", ["tenant_id"])

    op.create_table(
        "audit_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("resource_type", sa.String(length=80), nullable=True),
        sa.Column("resource_id", sa.String(length=120), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("ip_address", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_event_tenant_id", "audit_event", ["tenant_id"])
    op.create_index("ix_audit_event_event_type", "audit_event", ["event_type"])
    op.create_index("ix_audit_event_created_at", "audit_event", ["created_at"])


def downgrade():
    op.drop_index("ix_audit_event_created_at", table_name="audit_event")
    op.drop_index("ix_audit_event_event_type", table_name="audit_event")
    op.drop_index("ix_audit_event_tenant_id", table_name="audit_event")
    op.drop_table("audit_event")

    op.drop_index("ix_user_org_unit_tenant_id", table_name="user_org_unit")
    op.drop_table("user_org_unit")

    op.drop_index("ix_org_unit_tenant_id", table_name="org_unit")
    op.drop_table("org_unit")
