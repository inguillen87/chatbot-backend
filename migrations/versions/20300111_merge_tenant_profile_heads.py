"""Finalize tenant profile whatsapp sender chain.

Revision ID: 20300111_merge_tenant_profile_heads
Revises: 20300110_add_whatsapp_sender_to_tenant_profile
Create Date: 2030-01-11 00:00:00.000000
"""

revision = "20300111_merge_tenant_profile_heads"
down_revision = "20300110_add_whatsapp_sender_to_tenant_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Merge tenant profile heads."""
    pass


def downgrade() -> None:
    """Downgrade merge revision."""
    pass
