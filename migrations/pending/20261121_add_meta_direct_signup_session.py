"""Track direct Meta signups and prevent reuse of a provider session or code.

State and provider session are stored as digests, and the code as a keyed digest.
Promote this revision into the reviewed graph before enabling direct signup.
"""

from alembic import op
import sqlalchemy as sa


revision = "20261121_meta_direct_signup_v1"
down_revision = "20261001_auth_session_v1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "meta_direct_signup_session",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant_profile.id"), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("created_at_epoch", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("provider_session_hash", sa.String(64), nullable=True),
        sa.Column("code_hash", sa.String(64), nullable=True),
        sa.Column("waba_id", sa.String(40), nullable=True),
        sa.Column("phone_number_id", sa.String(40), nullable=True),
        sa.Column("connection_id", sa.Integer(), sa.ForeignKey("provider_connection.id"), nullable=True),
        sa.Column("sender_id", sa.Integer(), sa.ForeignKey("provider_sender.id"), nullable=True),
        sa.Column("credential_revision", sa.Integer(), nullable=True),
        sa.Column("reason_code", sa.String(100), nullable=True),
        sa.UniqueConstraint("provider_session_hash", name="uq_meta_signup_provider_session_hash"),
        sa.UniqueConstraint("code_hash", name="uq_meta_signup_code_hash"),
        sa.CheckConstraint(
            "status IN ('issued', 'exchanging', 'pending', 'connected', 'rejected')",
            name="ck_meta_signup_status",
        ),
    )


def downgrade():
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        # Keep new signups from entering between the emptiness check and DROP.
        connection.execute(sa.text("LOCK TABLE meta_direct_signup_session IN ACCESS EXCLUSIVE MODE"))
    if connection.execute(sa.text("SELECT 1 FROM meta_direct_signup_session LIMIT 1")).first():
        raise RuntimeError("Cannot remove a populated Meta signup replay ledger")
    op.drop_table("meta_direct_signup_session")
