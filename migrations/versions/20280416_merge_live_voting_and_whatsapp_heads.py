"""Merge live voting and whatsapp sender id heads."""

revision = "20280416_merge_live_voting_and_whatsapp_heads"
down_revision = (
    "20261205_add_whatsapp_sender_id_to_tenant_profile",
    "20280415_add_live_voting_flags_to_enc_encuesta",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Merge database heads."""
    pass


def downgrade() -> None:
    """Downgrade merge revision."""
    pass
