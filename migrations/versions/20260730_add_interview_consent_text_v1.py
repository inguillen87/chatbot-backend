"""bind interview consent to an immutable public text snapshot

Revision ID: 20260730_interview_consent_text_v1
Revises: 20260730_ticket_tenant_scope_v1
Create Date: 2026-07-30 23:10:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260730_interview_consent_text_v1"
down_revision = "20260730_ticket_tenant_scope_v1"
branch_labels = None
depends_on = None


_PROGRAM_SNAPSHOT_CHECK = (
    "(consent_text IS NULL AND consent_text_format IS NULL "
    "AND consent_text_normalization IS NULL) OR "
    "(consent_text IS NOT NULL AND consent_text_format = 'plain_text' "
    "AND consent_text_normalization = 'unicode_nfc_lf_trim_v1')"
)
_SESSION_CONSENT_CHECK = (
    "(consent_granted = false AND consent_policy_version IS NULL "
    "AND consent_text_sha256 IS NULL AND consent_recorded_at IS NULL "
    "AND consent_source IS NULL AND consent_attestation_kind IS NULL "
    "AND consent_evidence_provider IS NULL AND consent_evidence_ref IS NULL "
    "AND consent_evidence_sha256 IS NULL "
    "AND consent_evidence_captured_at IS NULL "
    "AND consent_attested_by_user_id IS NULL) OR "
    "(consent_granted = true AND consent_policy_version IS NOT NULL "
    "AND consent_text_sha256 IS NOT NULL AND consent_recorded_at IS NOT NULL "
    "AND consent_source IS NOT NULL AND "
    "((consent_attestation_kind = 'legacy_unverified' "
    "AND consent_evidence_provider IS NULL AND consent_evidence_ref IS NULL "
    "AND consent_evidence_sha256 IS NULL "
    "AND consent_evidence_captured_at IS NULL "
    "AND consent_attested_by_user_id IS NULL) OR "
    "(consent_attestation_kind = 'participant_event' "
    "AND consent_evidence_provider IS NOT NULL "
    "AND consent_evidence_ref IS NOT NULL "
    "AND consent_evidence_sha256 IS NOT NULL "
    "AND consent_evidence_captured_at IS NOT NULL "
    "AND consent_attested_by_user_id IS NOT NULL) OR "
    "(consent_attestation_kind = 'operator_attestation' "
    "AND consent_evidence_provider IS NULL AND consent_evidence_ref IS NULL "
    "AND consent_evidence_sha256 IS NULL "
    "AND consent_evidence_captured_at IS NOT NULL "
    "AND consent_attested_by_user_id IS NOT NULL)))"
)
_LEGACY_SESSION_CONSENT_CHECK = (
    "(consent_granted = false AND consent_policy_version IS NULL "
    "AND consent_recorded_at IS NULL AND consent_source IS NULL) OR "
    "(consent_granted = true AND consent_policy_version IS NOT NULL "
    "AND consent_recorded_at IS NOT NULL AND consent_source IS NOT NULL)"
)


def _sqlite_create_guards() -> None:
    program_invalid = (
        "(NEW.consent_text IS NULL AND "
        "(NEW.consent_text_format IS NOT NULL "
        "OR NEW.consent_text_normalization IS NOT NULL)) OR "
        "(NEW.consent_text IS NOT NULL AND "
        "(NEW.consent_text_format IS NOT 'plain_text' "
        "OR NEW.consent_text_normalization IS NOT 'unicode_nfc_lf_trim_v1'))"
    )
    session_invalid = (
        "(NEW.consent_granted = 0 AND "
        "(NEW.consent_policy_version IS NOT NULL "
        "OR NEW.consent_text_sha256 IS NOT NULL "
        "OR NEW.consent_recorded_at IS NOT NULL "
        "OR NEW.consent_source IS NOT NULL "
        "OR NEW.consent_attestation_kind IS NOT NULL "
        "OR NEW.consent_evidence_provider IS NOT NULL "
        "OR NEW.consent_evidence_ref IS NOT NULL "
        "OR NEW.consent_evidence_sha256 IS NOT NULL "
        "OR NEW.consent_evidence_captured_at IS NOT NULL "
        "OR NEW.consent_attested_by_user_id IS NOT NULL)) OR "
        "(NEW.consent_granted = 1 AND "
        "(NEW.consent_policy_version IS NULL "
        "OR NEW.consent_text_sha256 IS NULL "
        "OR NEW.consent_recorded_at IS NULL "
        "OR NEW.consent_source IS NULL "
        "OR NEW.consent_attestation_kind IS NULL "
        "OR NEW.consent_attestation_kind NOT IN "
        "('legacy_unverified','participant_event','operator_attestation') "
        "OR (NEW.consent_attestation_kind = 'legacy_unverified' AND "
        "(NEW.consent_evidence_provider IS NOT NULL "
        "OR NEW.consent_evidence_ref IS NOT NULL "
        "OR NEW.consent_evidence_sha256 IS NOT NULL "
        "OR NEW.consent_evidence_captured_at IS NOT NULL "
        "OR NEW.consent_attested_by_user_id IS NOT NULL)) "
        "OR (NEW.consent_attestation_kind = 'participant_event' AND "
        "(NEW.consent_evidence_provider IS NULL "
        "OR NEW.consent_evidence_ref IS NULL "
        "OR NEW.consent_evidence_sha256 IS NULL "
        "OR NEW.consent_evidence_captured_at IS NULL "
        "OR NEW.consent_attested_by_user_id IS NULL)) "
        "OR (NEW.consent_attestation_kind = 'operator_attestation' AND "
        "(NEW.consent_evidence_provider IS NOT NULL "
        "OR NEW.consent_evidence_ref IS NOT NULL "
        "OR NEW.consent_evidence_sha256 IS NOT NULL "
        "OR NEW.consent_evidence_captured_at IS NULL "
        "OR NEW.consent_attested_by_user_id IS NULL))))"
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_assessment_program_version_consent_snapshot_insert
        BEFORE INSERT ON assessment_program_version
        FOR EACH ROW WHEN {program_invalid}
        BEGIN
            SELECT RAISE(ABORT, 'invalid interview consent text snapshot');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_assessment_program_version_consent_snapshot_update
        BEFORE UPDATE ON assessment_program_version
        FOR EACH ROW WHEN {program_invalid}
        BEGIN
            SELECT RAISE(ABORT, 'invalid interview consent text snapshot');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_interview_session_consent_text_insert
        BEFORE INSERT ON interview_session
        FOR EACH ROW WHEN {session_invalid}
        BEGIN
            SELECT RAISE(ABORT, 'interview consent hash is required');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_interview_session_consent_text_update
        BEFORE UPDATE ON interview_session
        FOR EACH ROW WHEN {session_invalid}
        BEGIN
            SELECT RAISE(ABORT, 'interview consent hash is required');
        END
        """
    )


def _sqlite_drop_guards() -> None:
    for name in (
        "trg_interview_session_consent_text_update",
        "trg_interview_session_consent_text_insert",
        "trg_assessment_program_version_consent_snapshot_update",
        "trg_assessment_program_version_consent_snapshot_insert",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")


def upgrade() -> None:
    op.add_column(
        "assessment_program_version",
        sa.Column("consent_text", sa.Text(), nullable=True),
    )
    op.add_column(
        "assessment_program_version",
        sa.Column("consent_text_format", sa.String(length=24), nullable=True),
    )
    op.add_column(
        "assessment_program_version",
        sa.Column("consent_text_normalization", sa.String(length=48), nullable=True),
    )
    op.add_column(
        "interview_session",
        sa.Column("consent_text_sha256", sa.String(length=64), nullable=True),
    )
    for column in (
        sa.Column("consent_attestation_kind", sa.String(length=32), nullable=True),
        sa.Column("consent_evidence_provider", sa.String(length=64), nullable=True),
        sa.Column("consent_evidence_ref", sa.String(length=500), nullable=True),
        sa.Column("consent_evidence_sha256", sa.String(length=64), nullable=True),
        sa.Column("consent_evidence_captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consent_attested_by_user_id", sa.Integer(), nullable=True),
    ):
        op.add_column("interview_session", column)

    # Existing granted sessions can be bound deterministically to the pinned
    # immutable program-version digest.  Legacy versions have no recoverable
    # public text; application code keeps them visible but blocks new starts.
    op.execute(
        """
        UPDATE interview_session
        SET consent_text_sha256 = (
            SELECT assessment_program_version.consent_text_sha256
            FROM assessment_program_version
            WHERE assessment_program_version.id = interview_session.program_version_id
              AND assessment_program_version.tenant_id = interview_session.tenant_id
        ), consent_attestation_kind = 'legacy_unverified'
        WHERE consent_granted = true
        """
    )

    op.create_index(
        "uq_interview_session_consent_evidence_ref",
        "interview_session",
        ["tenant_id", "consent_evidence_ref"],
        unique=True,
    )

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.create_foreign_key(
            "fk_interview_session_consent_attested_by_user",
            "interview_session",
            "user",
            ["consent_attested_by_user_id"],
            ["id"],
        )
        op.create_check_constraint(
            "ck_assessment_program_version_consent_snapshot",
            "assessment_program_version",
            _PROGRAM_SNAPSHOT_CHECK,
        )
        op.drop_constraint(
            "ck_interview_session_consent",
            "interview_session",
            type_="check",
        )
        op.create_check_constraint(
            "ck_interview_session_consent",
            "interview_session",
            _SESSION_CONSENT_CHECK,
        )
    elif dialect == "sqlite":
        _sqlite_create_guards()
    else:
        with op.batch_alter_table("assessment_program_version") as batch:
            batch.create_check_constraint(
                "ck_assessment_program_version_consent_snapshot",
                _PROGRAM_SNAPSHOT_CHECK,
            )
        with op.batch_alter_table("interview_session") as batch:
            batch.create_foreign_key(
                "fk_interview_session_consent_attested_by_user",
                "user",
                ["consent_attested_by_user_id"],
                ["id"],
            )
            batch.drop_constraint("ck_interview_session_consent", type_="check")
            batch.create_check_constraint(
                "ck_interview_session_consent", _SESSION_CONSENT_CHECK
            )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.drop_index(
        "uq_interview_session_consent_evidence_ref",
        table_name="interview_session",
    )
    if dialect == "postgresql":
        op.drop_constraint(
            "fk_interview_session_consent_attested_by_user",
            "interview_session",
            type_="foreignkey",
        )
        op.drop_constraint(
            "ck_assessment_program_version_consent_snapshot",
            "assessment_program_version",
            type_="check",
        )
        op.drop_constraint(
            "ck_interview_session_consent",
            "interview_session",
            type_="check",
        )
        op.create_check_constraint(
            "ck_interview_session_consent",
            "interview_session",
            _LEGACY_SESSION_CONSENT_CHECK,
        )
    elif dialect == "sqlite":
        _sqlite_drop_guards()
        # These nullable columns are not part of SQLite's original table
        # constraints.  Native DROP COLUMN preserves referencing tables,
        # whereas Alembic batch recreation would violate their live FKs.
        for column in (
            "consent_attested_by_user_id",
            "consent_evidence_captured_at",
            "consent_evidence_sha256",
            "consent_evidence_ref",
            "consent_evidence_provider",
            "consent_attestation_kind",
            "consent_text_sha256",
        ):
            op.drop_column("interview_session", column)
        op.drop_column("assessment_program_version", "consent_text_normalization")
        op.drop_column("assessment_program_version", "consent_text_format")
        op.drop_column("assessment_program_version", "consent_text")
        return
    else:
        with op.batch_alter_table("assessment_program_version") as batch:
            batch.drop_constraint(
                "ck_assessment_program_version_consent_snapshot", type_="check"
            )
        with op.batch_alter_table("interview_session") as batch:
            batch.drop_constraint(
                "fk_interview_session_consent_attested_by_user",
                type_="foreignkey",
            )
            batch.drop_constraint("ck_interview_session_consent", type_="check")
            batch.create_check_constraint(
                "ck_interview_session_consent", _LEGACY_SESSION_CONSENT_CHECK
            )

    for column in (
        "consent_attested_by_user_id",
        "consent_evidence_captured_at",
        "consent_evidence_sha256",
        "consent_evidence_ref",
        "consent_evidence_provider",
        "consent_attestation_kind",
        "consent_text_sha256",
    ):
        op.drop_column("interview_session", column)
    op.drop_column("assessment_program_version", "consent_text_normalization")
    op.drop_column("assessment_program_version", "consent_text_format")
    op.drop_column("assessment_program_version", "consent_text")
