"""Bind interview consent presentations to signed callback and template proof.

Revision ID: 20260801_interview_delivery_v4
Revises: 20260801_crm_history_scope_v1
Create Date: 2026-08-01

The v3 revision is already published and therefore remains immutable. Existing
v3 presentation rows are retained with NULL v4 proof fields and fail closed in
the application; this migration never fabricates historical callback evidence.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260801_interview_delivery_v4"
down_revision = "20260801_crm_history_scope_v1"
branch_labels = None
depends_on = None


_TABLE = "interview_consent_presentation"


def _drop_immutability_trigger() -> None:
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


def _create_immutability_trigger() -> None:
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


def _assert_identity_snapshots_are_unambiguous() -> None:
    """Refuse contradictory legacy identities instead of choosing one row."""

    rows = op.get_bind().execute(
        sa.text(
            "SELECT challenge.tenant_id, challenge.interview_session_id, "
            "challenge.expected_identity_binding_id, "
            "challenge.expected_identity_version, challenge.expected_identity_hmac, "
            "challenge.expected_chat_session_id, binding.identity_version AS binding_version, "
            "binding.identity_hmac AS binding_hmac, "
            "binding.chat_session_id AS binding_chat_session_id "
            "FROM interview_consent_challenge AS challenge "
            "LEFT JOIN channel_session_identity_binding AS binding "
            "ON binding.id = challenge.expected_identity_binding_id "
            "AND binding.tenant_id = challenge.tenant_id "
            "ORDER BY challenge.tenant_id, challenge.interview_session_id, challenge.id"
        )
    ).mappings()
    challenge_snapshots: dict[tuple[int, int], tuple[object, ...]] = {}
    for row in rows:
        snapshot = (
            row["expected_identity_binding_id"],
            row["expected_identity_version"],
            row["expected_identity_hmac"],
            row["expected_chat_session_id"],
        )
        binding_snapshot = (
            row["expected_identity_binding_id"],
            row["binding_version"],
            row["binding_hmac"],
            row["binding_chat_session_id"],
        )
        if snapshot != binding_snapshot:
            raise RuntimeError(
                "Cannot migrate a consent challenge whose identity snapshot "
                "does not match its tenant-scoped channel binding"
            )
        key = (row["tenant_id"], row["interview_session_id"])
        prior = challenge_snapshots.setdefault(key, snapshot)
        if prior != snapshot:
            raise RuntimeError(
                "Cannot migrate ambiguous consent identities for one interview session"
            )

    sessions = op.get_bind().execute(
        sa.text(
            "SELECT tenant_id, assessment_case_id, subject_identity_binding_id, "
            "subject_identity_version, subject_identity_hmac, subject_chat_session_id "
            "FROM interview_session WHERE channel = 'whatsapp' "
            "ORDER BY tenant_id, assessment_case_id, id"
        )
    ).mappings()
    case_snapshots: dict[tuple[int, int], tuple[object, ...]] = {}
    for row in sessions:
        snapshot = (
            row["subject_identity_binding_id"],
            row["subject_identity_version"],
            row["subject_identity_hmac"],
            row["subject_chat_session_id"],
        )
        key = (row["tenant_id"], row["assessment_case_id"])
        prior = case_snapshots.setdefault(key, snapshot)
        if prior != snapshot:
            raise RuntimeError(
                "Cannot migrate conflicting WhatsApp identities for one assessment case"
            )


def upgrade() -> None:
    _assert_identity_snapshots_are_unambiguous()
    _drop_immutability_trigger()
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(
            "ck_interview_consent_presentation_hashes",
            type_="check",
        )
        batch_op.add_column(
            sa.Column("outbound_status_event_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "outbound_status_event_sha256",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "outbound_template_sha256",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_interview_consent_presentation_status_event",
            "messaging_event_ledger",
            ["outbound_status_event_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "uq_interview_consent_presentation_status_event",
            ["tenant_id", "outbound_status_event_id"],
        )
        batch_op.create_check_constraint(
            "ck_interview_consent_presentation_hashes",
            "((outbound_status_event_id IS NULL "
            "AND outbound_status_event_sha256 IS NULL "
            "AND outbound_template_sha256 IS NULL) OR "
            "(outbound_status_event_id IS NOT NULL "
            "AND length(outbound_status_event_sha256) = 64 "
            "AND length(outbound_template_sha256) = 64)) "
            "AND length(outbound_payload_sha256) = 64 "
            "AND length(expected_identity_hmac) = 64 "
            "AND length(consent_text_sha256) = 64 "
            "AND length(challenge_nonce_sha256) = 64",
        )
    _create_immutability_trigger()


def downgrade() -> None:
    _drop_immutability_trigger()
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(
            "ck_interview_consent_presentation_hashes",
            type_="check",
        )
        batch_op.drop_constraint(
            "uq_interview_consent_presentation_status_event",
            type_="unique",
        )
        batch_op.drop_constraint(
            "fk_interview_consent_presentation_status_event",
            type_="foreignkey",
        )
        batch_op.drop_column("outbound_template_sha256")
        batch_op.drop_column("outbound_status_event_sha256")
        batch_op.drop_column("outbound_status_event_id")
        batch_op.create_check_constraint(
            "ck_interview_consent_presentation_hashes",
            "length(outbound_payload_sha256) = 64 "
            "AND length(expected_identity_hmac) = 64 "
            "AND length(consent_text_sha256) = 64 "
            "AND length(challenge_nonce_sha256) = 64",
        )
    _create_immutability_trigger()
