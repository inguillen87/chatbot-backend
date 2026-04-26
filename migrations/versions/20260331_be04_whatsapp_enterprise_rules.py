"""BE-04 whatsapp enterprise rules

Revision ID: 20260331_be04_whatsapp_enterprise_rules
Revises: 20260331_be06_roles_org_units_audit
Create Date: 2026-03-31 02:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "20260331_be04_whatsapp_enterprise_rules"
down_revision = "20260331_be06_roles_org_units_audit"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "whatsapp_enterprise_rule",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("enforce_template_outside_24h", sa.Boolean(), nullable=False),
        sa.Column("max_outbound_per_hour", sa.Integer(), nullable=True),
        sa.Column("quiet_hours_start", sa.Integer(), nullable=True),
        sa.Column("quiet_hours_end", sa.Integer(), nullable=True),
        sa.Column("blocked_keywords", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id"),
    )
    op.create_index("ix_whatsapp_enterprise_rule_tenant_id", "whatsapp_enterprise_rule", ["tenant_id"])


def downgrade():
    op.drop_index("ix_whatsapp_enterprise_rule_tenant_id", table_name="whatsapp_enterprise_rule")
    op.drop_table("whatsapp_enterprise_rule")
