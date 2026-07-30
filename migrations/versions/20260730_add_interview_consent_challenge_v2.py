"""add one-time participant-bound interview consent challenges

Revision ID: 20260730_interview_consent_challenge_v2
Revises: 20260730_channel_session_identity_v1
Create Date: 2026-07-30 23:55:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260730_interview_consent_challenge_v2"
down_revision = "20260730_channel_session_identity_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "interview_consent_challenge",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("interview_session_id", sa.Integer(), nullable=False),
        sa.Column("program_version_id", sa.Integer(), nullable=False),
        sa.Column("consent_text_sha256", sa.String(length=64), nullable=False),
        sa.Column("expected_identity_binding_id", sa.Integer(), nullable=False),
        sa.Column("expected_identity_hmac", sa.String(length=64), nullable=False),
        sa.Column("nonce_sha256", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_by_user_id", sa.Integer(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("consumed_turn_id", sa.Integer(), nullable=True),
        sa.Column("consumed_provider_message_sid", sa.String(length=180), nullable=True),
        sa.Column(
            "contract_version",
            sa.String(length=48),
            nullable=False,
            server_default="interview.consent_challenge.v2",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "interview_session_id"],
            ["interview_session.tenant_id", "interview_session.id"],
            name="fk_interview_consent_challenge_session_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["expected_identity_binding_id", "tenant_id"],
            [
                "channel_session_identity_binding.id",
                "channel_session_identity_binding.tenant_id",
            ],
            name="fk_interview_consent_challenge_identity_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["consumed_turn_id", "tenant_id"],
            ["whatsapp_inbound_turn.id", "whatsapp_inbound_turn.tenant_id"],
            name="fk_interview_consent_challenge_turn_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["issued_by_user_id"],
            ["user.id"],
            name="fk_interview_consent_challenge_issued_by_user",
        ),
        sa.ForeignKeyConstraint(
            ["consumed_by_user_id"],
            ["user.id"],
            name="fk_interview_consent_challenge_consumed_by_user",
        ),
        sa.CheckConstraint(
            "length(consent_text_sha256) = 64",
            name="ck_interview_consent_challenge_text_hash",
        ),
        sa.CheckConstraint(
            "length(expected_identity_hmac) = 64",
            name="ck_interview_consent_challenge_identity_hash",
        ),
        sa.CheckConstraint(
            "length(nonce_sha256) = 64",
            name="ck_interview_consent_challenge_nonce_hash",
        ),
        sa.CheckConstraint(
            "(consumed_at IS NULL AND consumed_by_user_id IS NULL "
            "AND consumed_turn_id IS NULL AND consumed_provider_message_sid IS NULL) OR "
            "(consumed_at IS NOT NULL AND consumed_by_user_id IS NOT NULL "
            "AND consumed_turn_id IS NOT NULL AND consumed_provider_message_sid IS NOT NULL)",
            name="ck_interview_consent_challenge_consumption",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "nonce_sha256",
            name="uq_interview_consent_challenge_tenant_nonce",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "consumed_provider_message_sid",
            name="uq_interview_consent_challenge_consumed_turn_sid",
        ),
    )
    op.create_index(
        "ix_interview_consent_challenge_tenant_session",
        "interview_consent_challenge",
        ["tenant_id", "interview_session_id"],
    )
    op.create_index(
        "ix_interview_consent_challenge_expiry",
        "interview_consent_challenge",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_interview_consent_challenge_expiry",
        table_name="interview_consent_challenge",
    )
    op.drop_index(
        "ix_interview_consent_challenge_tenant_session",
        table_name="interview_consent_challenge",
    )
    op.drop_table("interview_consent_challenge")
