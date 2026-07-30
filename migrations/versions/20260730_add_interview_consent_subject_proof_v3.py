"""pin interview subject channel identity and consent delivery proof

Revision ID: 20260730_interview_consent_proof_v3
Revises: 20260730_interview_consent_challenge_v2
Create Date: 2026-07-30 23:59:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260730_interview_consent_proof_v3"
down_revision = "20260730_interview_consent_challenge_v2"
branch_labels = None
depends_on = None


def _create_presentation_immutability_trigger() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION interview_consent_presentation_immutable()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'interview consent presentations are immutable';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_interview_consent_presentation_immutable
            BEFORE UPDATE OR DELETE ON interview_consent_presentation
            FOR EACH ROW EXECUTE FUNCTION interview_consent_presentation_immutable()
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_interview_consent_presentation_update_immutable
            BEFORE UPDATE ON interview_consent_presentation
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'interview consent presentations are immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_interview_consent_presentation_delete_immutable
            BEFORE DELETE ON interview_consent_presentation
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'interview consent presentations are immutable');
            END
            """
        )


def _drop_presentation_immutability_trigger() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_interview_consent_presentation_immutable "
            "ON interview_consent_presentation"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS interview_consent_presentation_immutable()"
        )
    elif dialect == "sqlite":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_interview_consent_presentation_update_immutable"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_interview_consent_presentation_delete_immutable"
        )


def _assert_no_unpinned_whatsapp_rows() -> None:
    bind = op.get_bind()
    unpinned_sessions = bind.execute(
        sa.text(
            "SELECT count(*) FROM interview_session "
            "WHERE channel = 'whatsapp' AND ("
            "subject_identity_binding_id IS NULL OR subject_identity_version IS NULL "
            "OR subject_identity_hmac IS NULL OR subject_chat_session_id IS NULL)"
        )
    ).scalar_one()
    unpinned_cases = bind.execute(
        sa.text(
            "SELECT count(*) FROM assessment_case "
            "WHERE source_channel = 'whatsapp' AND ("
            "subject_identity_binding_id IS NULL OR subject_identity_version IS NULL "
            "OR subject_identity_hmac IS NULL OR subject_chat_session_id IS NULL)"
        )
    ).scalar_one()
    if unpinned_sessions or unpinned_cases:
        raise RuntimeError(
            "Cannot migrate unpinned WhatsApp interview subjects; reconcile their "
            "tenant-scoped channel identity before applying consent proof v3"
        )


def upgrade() -> None:
    with op.batch_alter_table("interview_consent_challenge") as batch_op:
        batch_op.add_column(
            sa.Column("expected_identity_version", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("expected_chat_session_id", sa.String(length=36), nullable=True)
        )

    op.execute(
        """
        UPDATE interview_consent_challenge
        SET expected_identity_version = (
                SELECT binding.identity_version
                FROM channel_session_identity_binding AS binding
                WHERE binding.id = interview_consent_challenge.expected_identity_binding_id
                  AND binding.tenant_id = interview_consent_challenge.tenant_id
            ),
            expected_chat_session_id = (
                SELECT binding.chat_session_id
                FROM channel_session_identity_binding AS binding
                WHERE binding.id = interview_consent_challenge.expected_identity_binding_id
                  AND binding.tenant_id = interview_consent_challenge.tenant_id
            ),
            expires_at = CASE
                WHEN consumed_at IS NULL THEN issued_at
                ELSE expires_at
            END,
            contract_version = 'interview.consent_challenge.v3'
        """
    )
    missing_challenge_snapshot = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM interview_consent_challenge WHERE "
            "expected_identity_version IS NULL OR expected_chat_session_id IS NULL"
        )
    ).scalar_one()
    if missing_challenge_snapshot:
        raise RuntimeError(
            "Cannot migrate consent challenges without their tenant identity snapshot"
        )
    with op.batch_alter_table("interview_consent_challenge") as batch_op:
        batch_op.alter_column(
            "expected_identity_version", existing_type=sa.String(length=32), nullable=False
        )
        batch_op.alter_column(
            "expected_chat_session_id", existing_type=sa.String(length=36), nullable=False
        )
        batch_op.alter_column(
            "contract_version",
            existing_type=sa.String(length=48),
            server_default="interview.consent_challenge.v3",
        )
        batch_op.create_check_constraint(
            "ck_interview_consent_challenge_identity_snapshot",
            "length(expected_identity_version) > 0 AND length(expected_chat_session_id) > 0",
        )
        batch_op.create_unique_constraint(
            "uq_interview_consent_challenge_tenant_id", ["tenant_id", "id"]
        )

    with op.batch_alter_table("interview_session") as batch_op:
        batch_op.add_column(
            sa.Column("subject_identity_binding_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("subject_identity_version", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("subject_identity_hmac", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("subject_chat_session_id", sa.String(length=36), nullable=True)
        )
    op.execute(
        """
        UPDATE interview_session
        SET subject_identity_binding_id = (
                SELECT challenge.expected_identity_binding_id
                FROM interview_consent_challenge AS challenge
                WHERE challenge.tenant_id = interview_session.tenant_id
                  AND challenge.interview_session_id = interview_session.id
                ORDER BY challenge.id DESC LIMIT 1
            ),
            subject_identity_version = (
                SELECT challenge.expected_identity_version
                FROM interview_consent_challenge AS challenge
                WHERE challenge.tenant_id = interview_session.tenant_id
                  AND challenge.interview_session_id = interview_session.id
                ORDER BY challenge.id DESC LIMIT 1
            ),
            subject_identity_hmac = (
                SELECT challenge.expected_identity_hmac
                FROM interview_consent_challenge AS challenge
                WHERE challenge.tenant_id = interview_session.tenant_id
                  AND challenge.interview_session_id = interview_session.id
                ORDER BY challenge.id DESC LIMIT 1
            ),
            subject_chat_session_id = (
                SELECT challenge.expected_chat_session_id
                FROM interview_consent_challenge AS challenge
                WHERE challenge.tenant_id = interview_session.tenant_id
                  AND challenge.interview_session_id = interview_session.id
                ORDER BY challenge.id DESC LIMIT 1
            )
        WHERE channel = 'whatsapp'
        """
    )

    with op.batch_alter_table("assessment_case") as batch_op:
        batch_op.add_column(
            sa.Column("subject_identity_binding_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("subject_identity_version", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("subject_identity_hmac", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("subject_chat_session_id", sa.String(length=36), nullable=True)
        )
    op.execute(
        """
        UPDATE assessment_case
        SET subject_identity_binding_id = (
                SELECT session.subject_identity_binding_id
                FROM interview_session AS session
                WHERE session.tenant_id = assessment_case.tenant_id
                  AND session.assessment_case_id = assessment_case.id
                  AND session.channel = 'whatsapp'
                ORDER BY session.id ASC LIMIT 1
            ),
            subject_identity_version = (
                SELECT session.subject_identity_version
                FROM interview_session AS session
                WHERE session.tenant_id = assessment_case.tenant_id
                  AND session.assessment_case_id = assessment_case.id
                  AND session.channel = 'whatsapp'
                ORDER BY session.id ASC LIMIT 1
            ),
            subject_identity_hmac = (
                SELECT session.subject_identity_hmac
                FROM interview_session AS session
                WHERE session.tenant_id = assessment_case.tenant_id
                  AND session.assessment_case_id = assessment_case.id
                  AND session.channel = 'whatsapp'
                ORDER BY session.id ASC LIMIT 1
            ),
            subject_chat_session_id = (
                SELECT session.subject_chat_session_id
                FROM interview_session AS session
                WHERE session.tenant_id = assessment_case.tenant_id
                  AND session.assessment_case_id = assessment_case.id
                  AND session.channel = 'whatsapp'
                ORDER BY session.id ASC LIMIT 1
            )
        WHERE source_channel = 'whatsapp'
        """
    )
    _assert_no_unpinned_whatsapp_rows()

    with op.batch_alter_table("assessment_case") as batch_op:
        batch_op.create_foreign_key(
            "fk_assessment_case_subject_identity_tenant",
            "channel_session_identity_binding",
            ["subject_identity_binding_id", "tenant_id"],
            ["id", "tenant_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_assessment_case_subject_channel_identity",
            "(source_channel = 'whatsapp' AND subject_identity_binding_id IS NOT NULL "
            "AND subject_identity_version IS NOT NULL AND subject_identity_hmac IS NOT NULL "
            "AND length(subject_identity_hmac) = 64 AND subject_chat_session_id IS NOT NULL) "
            "OR (source_channel <> 'whatsapp' AND subject_identity_binding_id IS NULL "
            "AND subject_identity_version IS NULL AND subject_identity_hmac IS NULL "
            "AND subject_chat_session_id IS NULL)",
        )
    op.create_index(
        "ix_assessment_case_subject_identity_binding_id",
        "assessment_case",
        ["subject_identity_binding_id"],
    )

    with op.batch_alter_table("interview_session") as batch_op:
        batch_op.create_foreign_key(
            "fk_interview_session_subject_identity_tenant",
            "channel_session_identity_binding",
            ["subject_identity_binding_id", "tenant_id"],
            ["id", "tenant_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_interview_session_subject_channel_identity",
            "(channel = 'whatsapp' AND subject_identity_binding_id IS NOT NULL "
            "AND subject_identity_version IS NOT NULL AND subject_identity_hmac IS NOT NULL "
            "AND length(subject_identity_hmac) = 64 AND subject_chat_session_id IS NOT NULL) "
            "OR (channel <> 'whatsapp' AND subject_identity_binding_id IS NULL "
            "AND subject_identity_version IS NULL AND subject_identity_hmac IS NULL "
            "AND subject_chat_session_id IS NULL)",
        )
    op.create_index(
        "ix_interview_session_subject_identity_binding_id",
        "interview_session",
        ["subject_identity_binding_id"],
    )

    op.create_table(
        "interview_consent_presentation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("interview_session_id", sa.Integer(), nullable=False),
        sa.Column("consent_challenge_id", sa.Integer(), nullable=False),
        sa.Column("outbound_attempt_id", sa.String(length=36), nullable=False),
        sa.Column("outbound_provider", sa.String(length=32), nullable=False),
        sa.Column("outbound_provider_message_sid", sa.String(length=180), nullable=False),
        sa.Column("outbound_content_sid", sa.String(length=180), nullable=False),
        sa.Column("outbound_provider_status", sa.String(length=16), nullable=False),
        sa.Column("outbound_provider_status_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outbound_payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("expected_identity_binding_id", sa.Integer(), nullable=False),
        sa.Column("expected_identity_version", sa.String(length=32), nullable=False),
        sa.Column("expected_identity_hmac", sa.String(length=64), nullable=False),
        sa.Column("expected_chat_session_id", sa.String(length=36), nullable=False),
        sa.Column("consent_text_sha256", sa.String(length=64), nullable=False),
        sa.Column("challenge_nonce_sha256", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("registered_by_user_id", sa.Integer(), nullable=False),
        sa.Column("registration_idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("registration_request_hash", sa.String(length=64), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "contract_version",
            sa.String(length=48),
            nullable=False,
            server_default="interview.consent_presentation.v1",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "interview_session_id"],
            ["interview_session.tenant_id", "interview_session.id"],
            name="fk_interview_consent_presentation_session_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "consent_challenge_id"],
            ["interview_consent_challenge.tenant_id", "interview_consent_challenge.id"],
            name="fk_interview_consent_presentation_challenge_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["expected_identity_binding_id", "tenant_id"],
            ["channel_session_identity_binding.id", "channel_session_identity_binding.tenant_id"],
            name="fk_interview_consent_presentation_identity_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["outbound_attempt_id"],
            ["whatsapp_outbound_attempt.attempt_id"],
            name="fk_interview_consent_presentation_outbound_attempt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["registered_by_user_id"],
            ["user.id"],
            name="fk_interview_consent_presentation_registered_by_user",
        ),
        sa.CheckConstraint(
            "outbound_provider_status IN ('delivered', 'read')",
            name="ck_interview_consent_presentation_provider_status",
        ),
        sa.CheckConstraint(
            "action = 'grant_consent'",
            name="ck_interview_consent_presentation_action",
        ),
        sa.CheckConstraint(
            "length(outbound_payload_sha256) = 64 AND length(expected_identity_hmac) = 64 "
            "AND length(consent_text_sha256) = 64 AND length(challenge_nonce_sha256) = 64",
            name="ck_interview_consent_presentation_hashes",
        ),
        sa.CheckConstraint(
            "outbound_provider_status_at <= registered_at",
            name="ck_interview_consent_presentation_ordering",
        ),
        sa.UniqueConstraint(
            "consent_challenge_id",
            name="uq_interview_consent_presentation_challenge",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "outbound_attempt_id",
            name="uq_interview_consent_presentation_attempt",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "outbound_provider",
            "outbound_provider_message_sid",
            name="uq_interview_consent_presentation_provider_message",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "registration_idempotency_key",
            name="uq_interview_consent_presentation_idempotency",
        ),
    )
    op.create_index(
        "ix_interview_consent_presentation_tenant_session",
        "interview_consent_presentation",
        ["tenant_id", "interview_session_id"],
    )
    _create_presentation_immutability_trigger()


def downgrade() -> None:
    _drop_presentation_immutability_trigger()
    op.drop_index(
        "ix_interview_consent_presentation_tenant_session",
        table_name="interview_consent_presentation",
    )
    op.drop_table("interview_consent_presentation")

    op.drop_index(
        "ix_interview_session_subject_identity_binding_id",
        table_name="interview_session",
    )
    with op.batch_alter_table("interview_session") as batch_op:
        batch_op.drop_constraint(
            "ck_interview_session_subject_channel_identity", type_="check"
        )
        batch_op.drop_constraint(
            "fk_interview_session_subject_identity_tenant", type_="foreignkey"
        )
        batch_op.drop_column("subject_chat_session_id")
        batch_op.drop_column("subject_identity_hmac")
        batch_op.drop_column("subject_identity_version")
        batch_op.drop_column("subject_identity_binding_id")

    op.drop_index(
        "ix_assessment_case_subject_identity_binding_id",
        table_name="assessment_case",
    )
    with op.batch_alter_table("assessment_case") as batch_op:
        batch_op.drop_constraint(
            "ck_assessment_case_subject_channel_identity", type_="check"
        )
        batch_op.drop_constraint(
            "fk_assessment_case_subject_identity_tenant", type_="foreignkey"
        )
        batch_op.drop_column("subject_chat_session_id")
        batch_op.drop_column("subject_identity_hmac")
        batch_op.drop_column("subject_identity_version")
        batch_op.drop_column("subject_identity_binding_id")

    with op.batch_alter_table("interview_consent_challenge") as batch_op:
        batch_op.drop_constraint(
            "uq_interview_consent_challenge_tenant_id", type_="unique"
        )
        batch_op.drop_constraint(
            "ck_interview_consent_challenge_identity_snapshot", type_="check"
        )
        batch_op.alter_column(
            "contract_version",
            existing_type=sa.String(length=48),
            server_default="interview.consent_challenge.v2",
        )
        batch_op.drop_column("expected_chat_session_id")
        batch_op.drop_column("expected_identity_version")

