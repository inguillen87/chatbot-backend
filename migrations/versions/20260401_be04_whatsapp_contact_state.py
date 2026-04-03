"""BE-04 whatsapp contact state for 24h window

Revision ID: 20260401_be04_whatsapp_contact_state
Revises: 20260331_be04_whatsapp_enterprise_rules
Create Date: 2026-04-01 00:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260401_be04_whatsapp_contact_state"
down_revision = "20260331_be04_whatsapp_enterprise_rules"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "whatsapp_contact_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("recipient", sa.String(length=255), nullable=False),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "recipient", name="uq_whatsapp_contact_state_tenant_recipient"),
    )
    op.create_index("ix_whatsapp_contact_state_tenant_id", "whatsapp_contact_state", ["tenant_id"])
    op.create_index("ix_whatsapp_contact_state_recipient", "whatsapp_contact_state", ["recipient"])
    op.create_index("ix_whatsapp_contact_state_last_inbound_at", "whatsapp_contact_state", ["last_inbound_at"])


def downgrade():
    op.drop_index("ix_whatsapp_contact_state_last_inbound_at", table_name="whatsapp_contact_state")
    op.drop_index("ix_whatsapp_contact_state_recipient", table_name="whatsapp_contact_state")
    op.drop_index("ix_whatsapp_contact_state_tenant_id", table_name="whatsapp_contact_state")
    op.drop_table("whatsapp_contact_state")
