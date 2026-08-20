"""add server-owned survey jurisdiction and immutable content receipts

Revision ID: 20260820_survey_content_jurisdiction_v1
Revises: 20260820_survey_response_origin_v1
Create Date: 2026-08-20 18:00:00.000000

Existing surveys are deliberately marked ``legacy_unverified``.  This
migration does not infer institutional truth from copy, names, slugs or ids.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260820_survey_content_jurisdiction_v1"
down_revision = "20260820_survey_response_origin_v1"
branch_labels = None
depends_on = None


def _create_receipt_immutability_trigger() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION survey_content_receipt_immutable_guard()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'survey content receipts are append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_survey_content_receipt_immutable
            BEFORE UPDATE OR DELETE ON survey_content_receipt
            FOR EACH ROW EXECUTE FUNCTION survey_content_receipt_immutable_guard()
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_survey_content_receipt_update
            BEFORE UPDATE ON survey_content_receipt
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'survey content receipts are immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_survey_content_receipt_delete
            BEFORE DELETE ON survey_content_receipt
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'survey content receipts are append-only');
            END
            """
        )


def _drop_receipt_immutability_trigger() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_survey_content_receipt_immutable "
            "ON survey_content_receipt"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS survey_content_receipt_immutable_guard()"
        )
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_survey_content_receipt_delete")
        op.execute("DROP TRIGGER IF EXISTS trg_survey_content_receipt_update")


def upgrade() -> None:
    with op.batch_alter_table("tenant_profile") as batch_op:
        batch_op.add_column(sa.Column("jurisdiction_ref", sa.String(160), nullable=True))
        batch_op.add_column(
            sa.Column(
                "jurisdiction_status",
                sa.String(24),
                nullable=False,
                server_default="unverified",
            )
        )
        batch_op.add_column(
            sa.Column("jurisdiction_evidence_ref", sa.String(255), nullable=True)
        )
        batch_op.add_column(
            sa.Column("jurisdiction_verified_by_user_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "jurisdiction_verified_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_tenant_profile_jurisdiction_verified_by_user",
            "user",
            ["jurisdiction_verified_by_user_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_tenant_profile_jurisdiction_status",
            "jurisdiction_status IN ('unverified', 'verified', 'not_applicable')",
        )
        batch_op.create_check_constraint(
            "ck_tenant_profile_verified_jurisdiction_evidence",
            "jurisdiction_status <> 'verified' OR "
            "(jurisdiction_ref IS NOT NULL AND jurisdiction_evidence_ref IS NOT NULL "
            "AND jurisdiction_verified_by_user_id IS NOT NULL "
            "AND jurisdiction_verified_at IS NOT NULL)",
        )

    with op.batch_alter_table("enc_encuesta") as batch_op:
        batch_op.add_column(sa.Column("jurisdiction_ref", sa.String(160), nullable=True))
        batch_op.add_column(
            sa.Column(
                "content_origin",
                sa.String(32),
                nullable=False,
                server_default="legacy_unverified",
            )
        )
        batch_op.add_column(
            sa.Column("content_origin_ref", sa.String(255), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_enc_encuesta_content_origin",
            "content_origin IN ('manual', 'template_catalog', "
            "'draft_materialization', 'duplicate', 'seed_demo', 'import', "
            "'legacy_unverified')",
        )

    op.create_table(
        "survey_content_receipt",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("survey_id", sa.Integer(), nullable=False),
        sa.Column(
            "contract_version",
            sa.String(48),
            nullable=False,
            server_default="surveys.content_receipt.v1",
        ),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("request_content_sha256", sa.String(64), nullable=True),
        sa.Column("jurisdiction_ref", sa.String(160), nullable=True),
        sa.Column(
            "content_origin",
            sa.String(32),
            nullable=False,
            server_default="legacy_unverified",
        ),
        sa.Column("content_origin_ref", sa.String(255), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("evidence_ref", sa.String(255), nullable=True),
        sa.Column("reason_code", sa.String(80), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("previous_receipt_sha256", sa.String(64), nullable=True),
        sa.Column("receipt_json", sa.Text(), nullable=False),
        sa.Column("receipt_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant_profile.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "survey_id"],
            ["enc_encuesta.tenant_id", "enc_encuesta.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["user.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "event_type IN ('created', 'updated', 'rebound', "
            "'review_approved', 'review_blocked', 'published')",
            name="ck_survey_content_receipt_event",
        ),
        sa.CheckConstraint(
            "decision IN ('recorded', 'approved', 'blocked', 'published')",
            name="ck_survey_content_receipt_decision",
        ),
        sa.CheckConstraint(
            "content_origin IN ('manual', 'template_catalog', "
            "'draft_materialization', 'duplicate', 'seed_demo', 'import', "
            "'legacy_unverified')",
            name="ck_survey_content_receipt_origin",
        ),
        sa.CheckConstraint(
            "length(content_sha256) = 64 AND length(receipt_sha256) = 64",
            name="ck_survey_content_receipt_hashes",
        ),
        sa.CheckConstraint(
            "request_content_sha256 IS NULL OR length(request_content_sha256) = 64",
            name="ck_survey_content_receipt_request_hash",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_survey_content_receipt_tenant_idempotency",
        ),
        sa.UniqueConstraint(
            "receipt_sha256", name="uq_survey_content_receipt_sha256"
        ),
    )
    op.create_index(
        "ix_survey_content_receipt_tenant_id",
        "survey_content_receipt",
        ["tenant_id"],
    )
    op.create_index(
        "ix_survey_content_receipt_survey_id",
        "survey_content_receipt",
        ["survey_id"],
    )
    op.create_index(
        "ix_survey_content_receipt_event_type",
        "survey_content_receipt",
        ["event_type"],
    )
    op.create_index(
        "ix_survey_content_receipt_content_sha256",
        "survey_content_receipt",
        ["content_sha256"],
    )
    op.create_index(
        "ix_survey_content_receipt_created_at",
        "survey_content_receipt",
        ["created_at"],
    )
    op.create_index(
        "ix_survey_content_receipt_survey_order",
        "survey_content_receipt",
        ["tenant_id", "survey_id", "id"],
    )
    _create_receipt_immutability_trigger()


def downgrade() -> None:
    _drop_receipt_immutability_trigger()
    op.drop_table("survey_content_receipt")

    with op.batch_alter_table("enc_encuesta") as batch_op:
        batch_op.drop_constraint(
            "ck_enc_encuesta_content_origin", type_="check"
        )
        batch_op.drop_column("content_origin_ref")
        batch_op.drop_column("content_origin")
        batch_op.drop_column("jurisdiction_ref")

    with op.batch_alter_table("tenant_profile") as batch_op:
        batch_op.drop_constraint(
            "ck_tenant_profile_verified_jurisdiction_evidence", type_="check"
        )
        batch_op.drop_constraint(
            "ck_tenant_profile_jurisdiction_status", type_="check"
        )
        batch_op.drop_constraint(
            "fk_tenant_profile_jurisdiction_verified_by_user", type_="foreignkey"
        )
        batch_op.drop_column("jurisdiction_verified_at")
        batch_op.drop_column("jurisdiction_verified_by_user_id")
        batch_op.drop_column("jurisdiction_evidence_ref")
        batch_op.drop_column("jurisdiction_status")
        batch_op.drop_column("jurisdiction_ref")
