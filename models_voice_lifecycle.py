"""Minimal, auditable lifecycle for consent-gated Twilio voice calls.

This ledger deliberately stores provider/control-plane identifiers and bounded
decision codes only.  Phone numbers, DTMF digits, audio and transcripts do not
belong in these tables. The transport can be PSTN or WhatsApp Business
Calling; both use the same consent boundary while retaining provider rules at
the routing layer.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from database import db


VOICE_CALL_STATES = (
    "received",
    "consent_pending",
    "stream_authorized",
    "completed",
    "failed",
)
VOICE_CONSENT_STATUSES = ("required", "granted", "declined")
VOICE_CALL_DIRECTIONS = ("inbound", "outbound")
VOICE_CONSENT_POLICY_VERSION = "voice.consent.v1"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class VoiceCallLifecycle(db.Model):
    """Current state for one provider call within one authoritative tenant."""

    __tablename__ = "voice_call_lifecycle"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(
        db.Integer,
        db.ForeignKey("tenant_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider = db.Column(db.String(20), nullable=False, default="twilio")
    provider_call_sid = db.Column(db.String(80), nullable=False)
    direction = db.Column(db.String(20), nullable=False)
    state = db.Column(db.String(24), nullable=False, default="received", index=True)
    consent_status = db.Column(
        db.String(20), nullable=False, default="required", index=True
    )
    consent_policy_version = db.Column(db.String(64), nullable=False)
    ai_processing_allowed = db.Column(db.Boolean, nullable=False, default=False)

    # Recording is intentionally impossible in v1.  Both values are persisted
    # so a future policy cannot silently reinterpret historical calls.
    recording_allowed = db.Column(db.Boolean, nullable=False, default=False)
    recording_enabled = db.Column(db.Boolean, nullable=False, default=False)

    last_provider_status = db.Column(db.String(32), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now
    )
    terminal_at = db.Column(db.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('received','consent_pending','stream_authorized','completed','failed')",
            name="ck_voice_call_lifecycle_state",
        ),
        CheckConstraint(
            "consent_status IN ('required','granted','declined')",
            name="ck_voice_call_lifecycle_consent",
        ),
        CheckConstraint(
            "direction IN ('inbound','outbound')",
            name="ck_voice_call_lifecycle_direction",
        ),
        CheckConstraint(
            "ai_processing_allowed = false OR consent_status = 'granted'",
            name="ck_voice_call_lifecycle_ai_requires_consent",
        ),
        CheckConstraint(
            "recording_allowed = false AND recording_enabled = false",
            name="ck_voice_call_lifecycle_recording_disabled_v1",
        ),
        CheckConstraint(
            "(state IN ('completed','failed') AND terminal_at IS NOT NULL) OR "
            "(state NOT IN ('completed','failed') AND terminal_at IS NULL)",
            name="ck_voice_call_lifecycle_terminal_at",
        ),
        UniqueConstraint(
            "tenant_id", "id", name="uq_voice_call_lifecycle_tenant_id"
        ),
        UniqueConstraint(
            "provider",
            "provider_call_sid",
            name="uq_voice_call_lifecycle_provider_call_sid",
        ),
        UniqueConstraint(
            "tenant_id",
            "provider_call_sid",
            name="uq_voice_call_lifecycle_tenant_call_sid",
        ),
    )


class VoiceCallLifecycleEvent(db.Model):
    """Append-only, idempotent audit event without user content or PII."""

    __tablename__ = "voice_call_lifecycle_event"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, index=True)
    lifecycle_id = db.Column(db.Integer, nullable=False, index=True)
    event_key = db.Column(db.String(128), nullable=False)
    state = db.Column(db.String(24), nullable=False, index=True)
    reason_code = db.Column(db.String(64), nullable=False)
    provider_status = db.Column(db.String(32), nullable=True)
    policy_version = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "lifecycle_id"],
            ["voice_call_lifecycle.tenant_id", "voice_call_lifecycle.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "state IN ('received','consent_pending','stream_authorized','completed','failed')",
            name="ck_voice_call_lifecycle_event_state",
        ),
        UniqueConstraint(
            "lifecycle_id",
            "event_key",
            name="uq_voice_call_lifecycle_event_key",
        ),
    )
