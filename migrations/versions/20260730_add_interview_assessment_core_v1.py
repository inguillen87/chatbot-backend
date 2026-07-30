"""add tenant-scoped interview assessment core v1

Revision ID: 20260730_interview_core_v1
Revises: 20260730_survey_privacy_v1
Create Date: 2026-07-30 17:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260730_interview_core_v1"
down_revision = "20260730_survey_privacy_v1"
branch_labels = None
depends_on = None


def _create_published_version_immutability_trigger() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION assessment_program_version_immutable()
            RETURNS trigger AS $$
            BEGIN
                IF OLD.published_at IS NOT NULL THEN
                    RAISE EXCEPTION 'published assessment program versions are immutable';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_assessment_program_version_immutable
            BEFORE UPDATE ON assessment_program_version
            FOR EACH ROW EXECUTE FUNCTION assessment_program_version_immutable()
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_assessment_program_version_immutable
            BEFORE UPDATE ON assessment_program_version
            FOR EACH ROW
            WHEN OLD.published_at IS NOT NULL
            BEGIN
                SELECT RAISE(ABORT, 'published assessment program versions are immutable');
            END
            """
        )


def _drop_published_version_immutability_trigger() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_assessment_program_version_immutable "
            "ON assessment_program_version"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS assessment_program_version_immutable()"
        )
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_assessment_program_version_immutable")


def upgrade() -> None:
    op.create_table(
        "assessment_program",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("program_type", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="draft"),
        sa.Column("published_version_number", sa.Integer(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"]),
        sa.CheckConstraint(
            "program_type IN ('school_admission', 'municipal_intake', "
            "'employment_interview', 'customer_qualification')",
            name="ck_assessment_program_type",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'retired')",
            name="ck_assessment_program_status",
        ),
        sa.CheckConstraint(
            "published_version_number IS NULL OR published_version_number > 0",
            name="ck_assessment_program_published_version",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_assessment_program_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_assessment_program_tenant_idempotency",
        ),
    )
    op.create_index(
        "ix_assessment_program_tenant_status",
        "assessment_program",
        ["tenant_id", "status"],
    )

    op.create_table(
        "assessment_program_version",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("program_id", sa.Integer(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="draft"),
        sa.Column("definition_json", sa.JSON(), nullable=False),
        sa.Column("definition_hash", sa.String(length=64), nullable=False),
        sa.Column("consent_policy_version", sa.String(length=64), nullable=False),
        sa.Column("consent_text_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("create_idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("create_request_hash", sa.String(length=64), nullable=False),
        sa.Column("published_by_user_id", sa.Integer(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publish_idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("publish_request_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "program_id"],
            ["assessment_program.tenant_id", "assessment_program.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["published_by_user_id"], ["user.id"]),
        sa.CheckConstraint(
            "version_number > 0", name="ck_assessment_program_version_number"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published')",
            name="ck_assessment_program_version_status",
        ),
        sa.CheckConstraint(
            "(status = 'draft' AND published_at IS NULL "
            "AND published_by_user_id IS NULL AND publish_idempotency_key IS NULL "
            "AND publish_request_hash IS NULL) OR "
            "(status = 'published' AND published_at IS NOT NULL "
            "AND published_by_user_id IS NOT NULL AND publish_idempotency_key IS NOT NULL "
            "AND publish_request_hash IS NOT NULL)",
            name="ck_assessment_program_version_publish_state",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "program_id",
            "version_number",
            name="uq_assessment_program_version_number",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "program_id",
            "id",
            name="uq_assessment_program_version_tenant_program_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "create_idempotency_key",
            name="uq_assessment_program_version_create_idem",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "publish_idempotency_key",
            name="uq_assessment_program_version_publish_idem",
        ),
    )
    op.create_index(
        "ix_assessment_program_version_tenant_status",
        "assessment_program_version",
        ["tenant_id", "status"],
    )

    op.create_table(
        "assessment_case",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("program_id", sa.Integer(), nullable=False),
        sa.Column("program_version_id", sa.Integer(), nullable=False),
        sa.Column("subject_type", sa.String(length=40), nullable=False),
        sa.Column("subject_ref", sa.String(length=160), nullable=False),
        sa.Column(
            "status", sa.String(length=40), nullable=False, server_default="consent_pending"
        ),
        sa.Column("source_channel", sa.String(length=24), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "program_id", "program_version_id"],
            [
                "assessment_program_version.tenant_id",
                "assessment_program_version.program_id",
                "assessment_program_version.id",
            ],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"]),
        sa.CheckConstraint(
            "subject_type IN ('student_applicant', 'citizen', 'candidate', 'customer')",
            name="ck_assessment_case_subject_type",
        ),
        sa.CheckConstraint(
            "status IN ('consent_pending', 'scheduled', 'in_progress', "
            "'awaiting_human_review', 'withdrawn', 'cancelled')",
            name="ck_assessment_case_status",
        ),
        sa.CheckConstraint(
            "source_channel IN ('api', 'web', 'widget', 'whatsapp', 'voice', 'in_person')",
            name="ck_assessment_case_source_channel",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            "program_version_id",
            name="uq_assessment_case_tenant_id_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_assessment_case_tenant_idempotency",
        ),
    )
    op.create_index(
        "ix_assessment_case_tenant_status",
        "assessment_case",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_assessment_case_program_version",
        "assessment_case",
        ["program_version_id"],
    )

    op.create_table(
        "interview_session",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("assessment_case_id", sa.Integer(), nullable=False),
        sa.Column("program_version_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="scheduled"),
        sa.Column("channel", sa.String(length=24), nullable=False),
        sa.Column("interviewer_user_id", sa.Integer(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consent_granted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("consent_policy_version", sa.String(length=64), nullable=True),
        sa.Column("consent_recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consent_source", sa.String(length=24), nullable=True),
        sa.Column("create_idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("create_request_hash", sa.String(length=64), nullable=False),
        sa.Column("start_idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("start_request_hash", sa.String(length=64), nullable=True),
        sa.Column("complete_idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("complete_request_hash", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "assessment_case_id", "program_version_id"],
            [
                "assessment_case.tenant_id",
                "assessment_case.id",
                "assessment_case.program_version_id",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["interviewer_user_id"], ["user.id"]),
        sa.CheckConstraint(
            "status IN ('scheduled', 'active', 'completed', 'interrupted', "
            "'no_show', 'void')",
            name="ck_interview_session_status",
        ),
        sa.CheckConstraint(
            "channel IN ('api', 'web', 'widget', 'whatsapp', 'voice', 'in_person')",
            name="ck_interview_session_channel",
        ),
        sa.CheckConstraint(
            "(consent_granted = false AND consent_policy_version IS NULL "
            "AND consent_recorded_at IS NULL AND consent_source IS NULL) OR "
            "(consent_granted = true AND consent_policy_version IS NOT NULL "
            "AND consent_recorded_at IS NOT NULL AND consent_source IS NOT NULL)",
            name="ck_interview_session_consent",
        ),
        sa.CheckConstraint(
            "consent_source IS NULL OR consent_source IN "
            "('web', 'widget', 'whatsapp', 'voice', 'in_person')",
            name="ck_interview_session_consent_source",
        ),
        sa.CheckConstraint(
            "status NOT IN ('active', 'completed') OR consent_granted = true",
            name="ck_interview_session_active_consent",
        ),
        sa.CheckConstraint(
            "(start_idempotency_key IS NULL AND start_request_hash IS NULL) OR "
            "(start_idempotency_key IS NOT NULL AND start_request_hash IS NOT NULL)",
            name="ck_interview_session_start_idempotency",
        ),
        sa.CheckConstraint(
            "(complete_idempotency_key IS NULL AND complete_request_hash IS NULL) OR "
            "(complete_idempotency_key IS NOT NULL AND complete_request_hash IS NOT NULL)",
            name="ck_interview_session_complete_idempotency",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_interview_session_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "create_idempotency_key",
            name="uq_interview_session_create_idempotency",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "start_idempotency_key",
            name="uq_interview_session_start_idempotency",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "complete_idempotency_key",
            name="uq_interview_session_complete_idempotency",
        ),
    )
    op.create_index(
        "ix_interview_session_case_status",
        "interview_session",
        ["assessment_case_id", "status"],
    )

    op.create_table(
        "interview_evidence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("interview_session_id", sa.Integer(), nullable=False),
        sa.Column("evidence_type", sa.String(length=24), nullable=False),
        sa.Column("source_channel", sa.String(length=24), nullable=False),
        sa.Column("storage_ref", sa.String(length=500), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "interview_session_id"],
            ["interview_session.tenant_id", "interview_session.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"]),
        sa.CheckConstraint(
            "evidence_type IN ('audio', 'image', 'file', 'transcript', "
            "'location', 'structured')",
            name="ck_interview_evidence_type",
        ),
        sa.CheckConstraint(
            "source_channel IN ('api', 'web', 'widget', 'whatsapp', 'voice', 'in_person')",
            name="ck_interview_evidence_source_channel",
        ),
        sa.CheckConstraint(
            "length(content_sha256) = 64",
            name="ck_interview_evidence_sha256_length",
        ),
        sa.CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0",
            name="ck_interview_evidence_size",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_interview_evidence_tenant_idempotency",
        ),
    )
    op.create_index(
        "ix_interview_evidence_session",
        "interview_evidence",
        ["interview_session_id"],
    )

    _create_published_version_immutability_trigger()


def downgrade() -> None:
    _drop_published_version_immutability_trigger()
    op.drop_index("ix_interview_evidence_session", table_name="interview_evidence")
    op.drop_table("interview_evidence")
    op.drop_index("ix_interview_session_case_status", table_name="interview_session")
    op.drop_table("interview_session")
    op.drop_index("ix_assessment_case_program_version", table_name="assessment_case")
    op.drop_index("ix_assessment_case_tenant_status", table_name="assessment_case")
    op.drop_table("assessment_case")
    op.drop_index(
        "ix_assessment_program_version_tenant_status",
        table_name="assessment_program_version",
    )
    op.drop_table("assessment_program_version")
    op.drop_index("ix_assessment_program_tenant_status", table_name="assessment_program")
    op.drop_table("assessment_program")
