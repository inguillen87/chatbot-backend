"""add source-anonymous survey privacy contract

Revision ID: 20260730_survey_privacy_v1
Revises: 20260730_pyme_order_context
Create Date: 2026-07-30 15:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260730_survey_privacy_v1"
down_revision = "20260730_pyme_order_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("enc_encuesta", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "privacy_mode",
                sa.String(length=32),
                nullable=False,
                server_default="legacy",
            )
        )
        batch_op.add_column(
            sa.Column("privacy_policy_version", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("privacy_policy_url", sa.String(length=500), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "privacy_consent_required",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column("response_retention_days", sa.Integer(), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_enc_encuesta_privacy_mode",
            "privacy_mode IN ('legacy', 'source_anonymous')",
        )
        batch_op.create_check_constraint(
            "ck_enc_encuesta_response_retention_days",
            "response_retention_days IS NULL OR "
            "(response_retention_days >= 1 AND response_retention_days <= 3650)",
        )
        batch_op.create_check_constraint(
            "ck_enc_encuesta_source_anonymous_policy",
            "privacy_mode <> 'source_anonymous' OR "
            "(privacy_policy_version IS NOT NULL AND privacy_policy_url IS NOT NULL "
            "AND privacy_consent_required = true "
            "AND response_retention_days IS NOT NULL "
            "AND coalesce(puntos_recompensa, 0) = 0)",
        )

    with op.batch_alter_table("enc_respuesta", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "privacy_mode",
                sa.String(length=32),
                nullable=False,
                server_default="legacy",
            )
        )
        batch_op.add_column(
            sa.Column("privacy_policy_version", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "privacy_consent_recorded_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "retention_expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            "ck_enc_respuesta_privacy_mode",
            "privacy_mode IN ('legacy', 'source_anonymous')",
        )
        batch_op.create_check_constraint(
            "ck_enc_respuesta_source_anonymous_minimization",
            "privacy_mode <> 'source_anonymous' OR "
            "(user_id IS NULL AND dni IS NULL AND phone IS NULL AND ip IS NULL "
            "AND ua IS NULL AND lat IS NULL AND lng IS NULL "
            "AND utm_source IS NULL AND utm_campaign IS NULL "
            "AND edad IS NULL AND anio_nacimiento IS NULL "
            "AND (metadata_payload IS NULL OR CAST(metadata_payload AS TEXT) = 'null') "
            "AND privacy_policy_version IS NOT NULL "
            "AND privacy_consent_recorded_at IS NOT NULL "
            "AND retention_expires_at IS NOT NULL)",
        )

    op.create_index(
        "ix_enc_respuesta_retention_expires_at",
        "enc_respuesta",
        ["retention_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_enc_respuesta_retention_expires_at",
        table_name="enc_respuesta",
    )
    with op.batch_alter_table("enc_respuesta", schema=None) as batch_op:
        batch_op.drop_constraint(
            "ck_enc_respuesta_source_anonymous_minimization",
            type_="check",
        )
        batch_op.drop_constraint("ck_enc_respuesta_privacy_mode", type_="check")
        batch_op.drop_column("retention_expires_at")
        batch_op.drop_column("privacy_consent_recorded_at")
        batch_op.drop_column("privacy_policy_version")
        batch_op.drop_column("privacy_mode")

    with op.batch_alter_table("enc_encuesta", schema=None) as batch_op:
        batch_op.drop_constraint(
            "ck_enc_encuesta_source_anonymous_policy",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_enc_encuesta_response_retention_days",
            type_="check",
        )
        batch_op.drop_constraint("ck_enc_encuesta_privacy_mode", type_="check")
        batch_op.drop_column("response_retention_days")
        batch_op.drop_column("privacy_consent_required")
        batch_op.drop_column("privacy_policy_url")
        batch_op.drop_column("privacy_policy_version")
        batch_op.drop_column("privacy_mode")
