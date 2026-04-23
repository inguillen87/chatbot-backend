"""Add WebAuthn credential storage and anon_id on user."""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260405_add_webauthn_support"
down_revision = "20260115_add_tenant_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user", sa.Column("anon_id", sa.String(length=80), nullable=True))
    op.create_index("ix_user_anon_id", "user", ["anon_id"], unique=False)

    op.create_table(
        "webauthn_credential",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("credential_id", sa.String(length=255), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("sign_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("transports", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("credential_id"),
    )
    op.create_index(
        "ix_webauthn_credential_user_id",
        "webauthn_credential",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_webauthn_credential_user_id", table_name="webauthn_credential")
    op.drop_table("webauthn_credential")
    op.drop_index("ix_user_anon_id", table_name="user")
    op.drop_column("user", "anon_id")
