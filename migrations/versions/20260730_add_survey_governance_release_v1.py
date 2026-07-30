"""add immutable tenant-scoped survey governance releases

Revision ID: 20260730_survey_governance_v1
Revises: 20260730_interview_core_v1
Create Date: 2026-07-30 20:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260730_survey_governance_v1"
down_revision = "20260730_interview_core_v1"
branch_labels = None
depends_on = None


_IMMUTABLE_COLUMNS = (
    "tenant_id",
    "survey_id",
    "version_number",
    "contract_version",
    "snapshot_json",
    "snapshot_sha256",
    "policy_sha256",
    "eligibility_policy_version",
    "consent_policy_version",
    "created_by_user_id",
    "create_idempotency_key",
    "create_request_hash",
    "created_at",
    "published_by_user_id",
    "published_at",
    "publish_idempotency_key",
    "publish_request_hash",
)


def _create_release_triggers() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        comparisons = " AND ".join(
            f"NEW.{column} IS NOT DISTINCT FROM OLD.{column}"
            for column in _IMMUTABLE_COLUMNS
        )
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION survey_governance_release_guard()
            RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    IF OLD.status IN ('published', 'closed') THEN
                        RAISE EXCEPTION 'published/closed survey releases are immutable';
                    END IF;
                    RETURN OLD;
                END IF;
                IF OLD.status = 'closed' THEN
                    RAISE EXCEPTION 'closed survey releases are immutable';
                END IF;
                IF OLD.status = 'published' AND NOT (
                    NEW.status = 'closed' AND {comparisons}
                ) THEN
                    RAISE EXCEPTION 'published survey releases only allow close';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_survey_governance_release_guard
            BEFORE UPDATE OR DELETE ON survey_governance_release
            FOR EACH ROW EXECUTE FUNCTION survey_governance_release_guard()
            """
        )
    elif dialect == "sqlite":
        comparisons = " AND ".join(
            f"NEW.{column} IS OLD.{column}" for column in _IMMUTABLE_COLUMNS
        )
        op.execute(
            """
            CREATE TRIGGER trg_survey_governance_release_closed_update
            BEFORE UPDATE ON survey_governance_release
            FOR EACH ROW WHEN OLD.status = 'closed'
            BEGIN
                SELECT RAISE(ABORT, 'closed survey releases are immutable');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_survey_governance_release_published_update
            BEFORE UPDATE ON survey_governance_release
            FOR EACH ROW WHEN OLD.status = 'published' AND NOT (
                NEW.status = 'closed' AND {comparisons}
            )
            BEGIN
                SELECT RAISE(ABORT, 'published survey releases only allow close');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_survey_governance_release_delete
            BEFORE DELETE ON survey_governance_release
            FOR EACH ROW WHEN OLD.status IN ('published', 'closed')
            BEGIN
                SELECT RAISE(ABORT, 'published/closed survey releases are immutable');
            END
            """
        )


def _drop_release_triggers() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_survey_governance_release_guard "
            "ON survey_governance_release"
        )
        op.execute("DROP FUNCTION IF EXISTS survey_governance_release_guard()")
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_survey_governance_release_delete")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_survey_governance_release_published_update"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_survey_governance_release_closed_update"
        )


def upgrade() -> None:
    with op.batch_alter_table("enc_encuesta") as batch_op:
        batch_op.create_unique_constraint(
            "uq_enc_encuesta_tenant_id", ["tenant_id", "id"]
        )

    op.create_table(
        "survey_governance_release",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("survey_id", sa.Integer(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "contract_version",
            sa.String(length=48),
            nullable=False,
            server_default="surveys.governance_release.v1",
        ),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("eligibility_policy_version", sa.String(length=64), nullable=False),
        sa.Column("consent_policy_version", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("create_idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("create_request_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_by_user_id", sa.Integer(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publish_idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("publish_request_hash", sa.String(length=64), nullable=True),
        sa.Column("closed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("close_request_hash", sa.String(length=64), nullable=True),
        sa.Column("closure_manifest_json", sa.Text(), nullable=True),
        sa.Column("closure_manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("closed_response_count", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant_profile.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "survey_id"],
            ["enc_encuesta.tenant_id", "enc_encuesta.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["user.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["published_by_user_id"], ["user.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["closed_by_user_id"], ["user.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'closed')",
            name="ck_survey_governance_release_status",
        ),
        sa.CheckConstraint(
            "version_number > 0", name="ck_survey_governance_release_version"
        ),
        sa.CheckConstraint(
            "closed_response_count IS NULL OR closed_response_count >= 0",
            name="ck_survey_governance_release_response_count",
        ),
        sa.CheckConstraint(
            "(status = 'draft' AND published_at IS NULL AND closed_at IS NULL) OR "
            "(status = 'published' AND published_at IS NOT NULL AND closed_at IS NULL "
            "AND published_by_user_id IS NOT NULL AND publish_idempotency_key IS NOT NULL "
            "AND publish_request_hash IS NOT NULL) OR "
            "(status = 'closed' AND published_at IS NOT NULL AND closed_at IS NOT NULL "
            "AND published_by_user_id IS NOT NULL AND publish_idempotency_key IS NOT NULL "
            "AND publish_request_hash IS NOT NULL AND closed_by_user_id IS NOT NULL "
            "AND close_idempotency_key IS NOT NULL AND close_request_hash IS NOT NULL "
            "AND closure_manifest_json IS NOT NULL AND closure_manifest_sha256 IS NOT NULL "
            "AND closed_response_count IS NOT NULL)",
            name="ck_survey_governance_release_lifecycle",
        ),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_survey_governance_release_tenant_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "survey_id",
            "version_number",
            name="uq_survey_governance_release_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "create_idempotency_key",
            name="uq_survey_governance_release_create_idem",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "publish_idempotency_key",
            name="uq_survey_governance_release_publish_idem",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "close_idempotency_key",
            name="uq_survey_governance_release_close_idem",
        ),
    )
    op.create_index(
        "ix_survey_governance_release_tenant_id",
        "survey_governance_release",
        ["tenant_id"],
    )
    op.create_index(
        "ix_survey_governance_release_survey_id",
        "survey_governance_release",
        ["survey_id"],
    )
    op.create_index(
        "ix_survey_governance_release_status",
        "survey_governance_release",
        ["status"],
    )
    op.create_index(
        "uq_survey_governance_one_published",
        "survey_governance_release",
        ["tenant_id", "survey_id"],
        unique=True,
        sqlite_where=sa.text("status = 'published'"),
        postgresql_where=sa.text("status = 'published'"),
    )
    _create_release_triggers()

    with op.batch_alter_table("enc_respuesta") as batch_op:
        batch_op.add_column(sa.Column("governance_release_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "governance_eligibility_policy_version",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "governance_consent_policy_version",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "governance_acknowledged_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_enc_respuesta_governance_release_tenant",
            "survey_governance_release",
            ["tenant_id", "governance_release_id"],
            ["tenant_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_enc_respuesta_governance_ack",
            "governance_release_id IS NULL OR "
            "(governance_eligibility_policy_version IS NOT NULL "
            "AND governance_consent_policy_version IS NOT NULL "
            "AND governance_acknowledged_at IS NOT NULL)",
        )
        batch_op.create_index(
            "ix_enc_respuesta_governance_release_id", ["governance_release_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("enc_respuesta") as batch_op:
        batch_op.drop_index("ix_enc_respuesta_governance_release_id")
        batch_op.drop_constraint(
            "ck_enc_respuesta_governance_ack", type_="check"
        )
        batch_op.drop_constraint(
            "fk_enc_respuesta_governance_release_tenant", type_="foreignkey"
        )
        batch_op.drop_column("governance_acknowledged_at")
        batch_op.drop_column("governance_consent_policy_version")
        batch_op.drop_column("governance_eligibility_policy_version")
        batch_op.drop_column("governance_release_id")

    _drop_release_triggers()
    op.drop_index(
        "uq_survey_governance_one_published",
        table_name="survey_governance_release",
    )
    op.drop_index(
        "ix_survey_governance_release_status",
        table_name="survey_governance_release",
    )
    op.drop_index(
        "ix_survey_governance_release_survey_id",
        table_name="survey_governance_release",
    )
    op.drop_index(
        "ix_survey_governance_release_tenant_id",
        table_name="survey_governance_release",
    )
    op.drop_table("survey_governance_release")

    with op.batch_alter_table("enc_encuesta") as batch_op:
        batch_op.drop_constraint("uq_enc_encuesta_tenant_id", type_="unique")

