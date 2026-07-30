"""add consent-gated voice call lifecycle v1

Revision ID: 20260730_voice_consent_v1
Revises: 20260730_survey_governance_v1
Create Date: 2026-07-30 22:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260730_voice_consent_v1"
down_revision = "20260730_survey_governance_v1"
branch_labels = None
depends_on = None


def _create_immutability_triggers() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION voice_call_lifecycle_guard()
            RETURNS trigger AS $$
            BEGIN
                IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
                   OR NEW.provider IS DISTINCT FROM OLD.provider
                   OR NEW.provider_call_sid IS DISTINCT FROM OLD.provider_call_sid
                   OR NEW.direction IS DISTINCT FROM OLD.direction
                   OR NEW.consent_policy_version IS DISTINCT FROM OLD.consent_policy_version
                   OR NEW.recording_allowed IS DISTINCT FROM OLD.recording_allowed
                   OR NEW.recording_enabled IS DISTINCT FROM OLD.recording_enabled
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                    RAISE EXCEPTION 'voice call identity and policy are immutable';
                END IF;

                IF OLD.state IN ('completed', 'failed') AND NEW IS DISTINCT FROM OLD THEN
                    RAISE EXCEPTION 'terminal voice call lifecycle is immutable';
                END IF;

                IF NOT (
                    NEW.state = OLD.state
                    OR (OLD.state = 'received' AND NEW.state IN ('consent_pending','completed','failed'))
                    OR (OLD.state = 'consent_pending' AND NEW.state IN ('stream_authorized','completed','failed'))
                    OR (OLD.state = 'stream_authorized' AND NEW.state IN ('completed','failed'))
                ) THEN
                    RAISE EXCEPTION 'invalid voice call lifecycle transition';
                END IF;

                IF NOT (
                    NEW.consent_status = OLD.consent_status
                    OR (OLD.consent_status = 'required' AND NEW.consent_status IN ('granted','declined'))
                ) THEN
                    RAISE EXCEPTION 'voice consent decision is terminal';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_voice_call_lifecycle_guard
            BEFORE UPDATE ON voice_call_lifecycle
            FOR EACH ROW EXECUTE FUNCTION voice_call_lifecycle_guard()
            """
        )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION voice_call_lifecycle_event_immutable()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'voice call lifecycle events are append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_voice_call_lifecycle_event_immutable
            BEFORE UPDATE OR DELETE ON voice_call_lifecycle_event
            FOR EACH ROW EXECUTE FUNCTION voice_call_lifecycle_event_immutable()
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_voice_call_lifecycle_identity_guard
            BEFORE UPDATE ON voice_call_lifecycle
            FOR EACH ROW WHEN
                NEW.tenant_id IS NOT OLD.tenant_id
                OR NEW.provider IS NOT OLD.provider
                OR NEW.provider_call_sid IS NOT OLD.provider_call_sid
                OR NEW.direction IS NOT OLD.direction
                OR NEW.consent_policy_version IS NOT OLD.consent_policy_version
                OR NEW.recording_allowed IS NOT OLD.recording_allowed
                OR NEW.recording_enabled IS NOT OLD.recording_enabled
                OR NEW.created_at IS NOT OLD.created_at
            BEGIN
                SELECT RAISE(ABORT, 'voice call identity and policy are immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_voice_call_lifecycle_terminal_guard
            BEFORE UPDATE ON voice_call_lifecycle
            FOR EACH ROW WHEN OLD.state IN ('completed', 'failed')
            BEGIN
                SELECT RAISE(ABORT, 'terminal voice call lifecycle is immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_voice_call_lifecycle_state_guard
            BEFORE UPDATE ON voice_call_lifecycle
            FOR EACH ROW WHEN NOT (
                NEW.state = OLD.state
                OR (OLD.state = 'received' AND NEW.state IN ('consent_pending','completed','failed'))
                OR (OLD.state = 'consent_pending' AND NEW.state IN ('stream_authorized','completed','failed'))
                OR (OLD.state = 'stream_authorized' AND NEW.state IN ('completed','failed'))
            )
            BEGIN
                SELECT RAISE(ABORT, 'invalid voice call lifecycle transition');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_voice_call_consent_guard
            BEFORE UPDATE ON voice_call_lifecycle
            FOR EACH ROW WHEN NOT (
                NEW.consent_status = OLD.consent_status
                OR (OLD.consent_status = 'required' AND NEW.consent_status IN ('granted','declined'))
            )
            BEGIN
                SELECT RAISE(ABORT, 'voice consent decision is terminal');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_voice_call_lifecycle_event_update
            BEFORE UPDATE ON voice_call_lifecycle_event
            BEGIN
                SELECT RAISE(ABORT, 'voice call lifecycle events are append-only');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_voice_call_lifecycle_event_delete
            BEFORE DELETE ON voice_call_lifecycle_event
            BEGIN
                SELECT RAISE(ABORT, 'voice call lifecycle events are append-only');
            END
            """
        )


def _drop_immutability_triggers() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_voice_call_lifecycle_event_immutable "
            "ON voice_call_lifecycle_event"
        )
        op.execute("DROP FUNCTION IF EXISTS voice_call_lifecycle_event_immutable()")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_voice_call_lifecycle_guard "
            "ON voice_call_lifecycle"
        )
        op.execute("DROP FUNCTION IF EXISTS voice_call_lifecycle_guard()")
    elif dialect == "sqlite":
        for name in (
            "trg_voice_call_lifecycle_event_delete",
            "trg_voice_call_lifecycle_event_update",
            "trg_voice_call_consent_guard",
            "trg_voice_call_lifecycle_state_guard",
            "trg_voice_call_lifecycle_terminal_guard",
            "trg_voice_call_lifecycle_identity_guard",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {name}")


def upgrade() -> None:
    op.create_table(
        "voice_call_lifecycle",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False, server_default="twilio"),
        sa.Column("provider_call_sid", sa.String(length=80), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False, server_default="received"),
        sa.Column("consent_status", sa.String(length=20), nullable=False, server_default="required"),
        sa.Column("consent_policy_version", sa.String(length=64), nullable=False),
        sa.Column("ai_processing_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("recording_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("recording_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_provider_status", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant_profile.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "state IN ('received','consent_pending','stream_authorized','completed','failed')",
            name="ck_voice_call_lifecycle_state",
        ),
        sa.CheckConstraint(
            "consent_status IN ('required','granted','declined')",
            name="ck_voice_call_lifecycle_consent",
        ),
        sa.CheckConstraint(
            "direction IN ('inbound','outbound')",
            name="ck_voice_call_lifecycle_direction",
        ),
        sa.CheckConstraint(
            "ai_processing_allowed = false OR consent_status = 'granted'",
            name="ck_voice_call_lifecycle_ai_requires_consent",
        ),
        sa.CheckConstraint(
            "recording_allowed = false AND recording_enabled = false",
            name="ck_voice_call_lifecycle_recording_disabled_v1",
        ),
        sa.CheckConstraint(
            "(state IN ('completed','failed') AND terminal_at IS NOT NULL) OR "
            "(state NOT IN ('completed','failed') AND terminal_at IS NULL)",
            name="ck_voice_call_lifecycle_terminal_at",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_voice_call_lifecycle_tenant_id"),
        sa.UniqueConstraint(
            "provider",
            "provider_call_sid",
            name="uq_voice_call_lifecycle_provider_call_sid",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "provider_call_sid",
            name="uq_voice_call_lifecycle_tenant_call_sid",
        ),
    )
    op.create_index("ix_voice_call_lifecycle_tenant_id", "voice_call_lifecycle", ["tenant_id"])
    op.create_index("ix_voice_call_lifecycle_state", "voice_call_lifecycle", ["state"])
    op.create_index(
        "ix_voice_call_lifecycle_consent_status",
        "voice_call_lifecycle",
        ["consent_status"],
    )

    op.create_table(
        "voice_call_lifecycle_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("lifecycle_id", sa.Integer(), nullable=False),
        sa.Column("event_key", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("provider_status", sa.String(length=32), nullable=True),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "lifecycle_id"],
            ["voice_call_lifecycle.tenant_id", "voice_call_lifecycle.id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "state IN ('received','consent_pending','stream_authorized','completed','failed')",
            name="ck_voice_call_lifecycle_event_state",
        ),
        sa.UniqueConstraint(
            "lifecycle_id", "event_key", name="uq_voice_call_lifecycle_event_key"
        ),
    )
    op.create_index(
        "ix_voice_call_lifecycle_event_tenant_id",
        "voice_call_lifecycle_event",
        ["tenant_id"],
    )
    op.create_index(
        "ix_voice_call_lifecycle_event_lifecycle_id",
        "voice_call_lifecycle_event",
        ["lifecycle_id"],
    )
    op.create_index(
        "ix_voice_call_lifecycle_event_state",
        "voice_call_lifecycle_event",
        ["state"],
    )
    _create_immutability_triggers()


def downgrade() -> None:
    _drop_immutability_triggers()
    op.drop_index(
        "ix_voice_call_lifecycle_event_state", table_name="voice_call_lifecycle_event"
    )
    op.drop_index(
        "ix_voice_call_lifecycle_event_lifecycle_id",
        table_name="voice_call_lifecycle_event",
    )
    op.drop_index(
        "ix_voice_call_lifecycle_event_tenant_id",
        table_name="voice_call_lifecycle_event",
    )
    op.drop_table("voice_call_lifecycle_event")
    op.drop_index("ix_voice_call_lifecycle_consent_status", table_name="voice_call_lifecycle")
    op.drop_index("ix_voice_call_lifecycle_state", table_name="voice_call_lifecycle")
    op.drop_index("ix_voice_call_lifecycle_tenant_id", table_name="voice_call_lifecycle")
    op.drop_table("voice_call_lifecycle")
