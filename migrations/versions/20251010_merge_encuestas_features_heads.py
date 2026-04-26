"""Merge encuestas core and features table heads."""

revision = "20251010_merge_heads"
down_revision = ("20250210_add_features_table", "20251008b2c3")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Merge database heads."""
    pass


def downgrade() -> None:
    """Downgrade merge revision."""
    pass
