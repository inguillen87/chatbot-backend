"""Merge WebAuthn support and public survey/password reset heads."""

# revision identifiers, used by Alembic.
revision = "20260406_merge_webauthn_public_survey_heads"
down_revision = (
    "20260405_add_webauthn_support",
    "20251210_merge_public_survey_password_reset_heads",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
