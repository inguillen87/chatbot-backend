from __future__ import annotations

import uuid

from models import JSONType, TimestampMixin, db


class CampaignPreparation(db.Model, TimestampMixin):
    """Immutable, tenant-scoped campaign preparation receipt.

    A preparation is deliberately not a provider dispatch.  It keeps the
    rendered message and the audience/readiness snapshot required for audit,
    while every recipient intent remains held until a separately reviewed
    dispatch contract exists.
    """

    __tablename__ = "campaign_preparation"

    STATUS_DRAFT = "draft"
    STATUS_SCHEDULED = "scheduled"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    VALID_STATUSES = frozenset(
        {
            STATUS_DRAFT,
            STATUS_SCHEDULED,
            STATUS_IN_PROGRESS,
            STATUS_COMPLETED,
            STATUS_FAILED,
        }
    )

    id = db.Column(
        db.String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    tenant_id = db.Column(
        db.Integer,
        db.ForeignKey("tenant_profile.id"),
        nullable=False,
        index=True,
    )
    created_by_user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=False,
        index=True,
    )
    contract_version = db.Column(db.String(64), nullable=False)
    status = db.Column(
        db.String(24),
        nullable=False,
        default=STATUS_DRAFT,
        server_default=STATUS_DRAFT,
        index=True,
    )
    channel = db.Column(db.String(20), nullable=False, index=True)
    template_id = db.Column(
        db.String(36),
        db.ForeignKey("notification_template.id"),
        nullable=False,
        index=True,
    )
    template_key = db.Column(db.String(80), nullable=False)
    message_template_registry_id = db.Column(
        db.Integer,
        db.ForeignKey("message_template_registry.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    idempotency_key = db.Column(db.String(128), nullable=False)
    payload_digest = db.Column(db.String(64), nullable=False)
    preview_digest = db.Column(db.String(64), nullable=False)
    rendered_subject = db.Column(db.String(255), nullable=True)
    rendered_body = db.Column(db.Text, nullable=False)
    scheduled_for = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    audience_json = db.Column(JSONType, nullable=False)
    readiness_json = db.Column(JSONType, nullable=False)

    __table_args__ = (
        db.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_campaign_preparation_tenant_idempotency",
        ),
        db.CheckConstraint(
            "status IN ('draft', 'scheduled', 'in_progress', 'completed', 'failed')",
            name="ck_campaign_preparation_status",
        ),
        db.CheckConstraint(
            "channel IN ('whatsapp', 'email')",
            name="ck_campaign_preparation_channel",
        ),
        db.CheckConstraint(
            "length(payload_digest) = 64",
            name="ck_campaign_preparation_payload_digest",
        ),
        db.CheckConstraint(
            "length(preview_digest) = 64",
            name="ck_campaign_preparation_preview_digest",
        ),
        db.Index(
            "ix_campaign_preparation_tenant_created",
            "tenant_id",
            "created_at",
            "id",
        ),
    )


class CampaignDeliveryIntent(db.Model, TimestampMixin):
    """Durable recipient-level queue receipt with no implicit transport."""

    __tablename__ = "campaign_delivery_intent"

    QUEUE_HELD = "held"
    QUEUE_EXCLUDED = "excluded"
    VALID_QUEUE_STATUSES = frozenset({QUEUE_HELD, QUEUE_EXCLUDED})

    TRANSPORT_NOT_ATTEMPTED = "not_attempted"
    TRANSPORT_UNKNOWN = "unknown"
    TRANSPORT_ACCEPTED = "accepted"
    TRANSPORT_SENT = "sent"
    TRANSPORT_DELIVERED = "delivered"
    TRANSPORT_READ = "read"
    TRANSPORT_FAILED = "failed"
    VALID_TRANSPORT_STATUSES = frozenset(
        {
            TRANSPORT_NOT_ATTEMPTED,
            TRANSPORT_UNKNOWN,
            TRANSPORT_ACCEPTED,
            TRANSPORT_SENT,
            TRANSPORT_DELIVERED,
            TRANSPORT_READ,
            TRANSPORT_FAILED,
        }
    )

    id = db.Column(
        db.String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    campaign_id = db.Column(
        db.String(36),
        db.ForeignKey("campaign_preparation.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id = db.Column(
        db.Integer,
        db.ForeignKey("tenant_profile.id"),
        nullable=False,
        index=True,
    )
    contact_id = db.Column(
        db.String(36),
        db.ForeignKey("contact.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    queue_status = db.Column(db.String(20), nullable=False, index=True)
    exclusion_reason = db.Column(db.String(64), nullable=True, index=True)
    transport_status = db.Column(
        db.String(24),
        nullable=False,
        default=TRANSPORT_NOT_ATTEMPTED,
        server_default=TRANSPORT_NOT_ATTEMPTED,
        index=True,
    )
    destination_digest = db.Column(db.String(64), nullable=True)
    rendered_content_digest = db.Column(db.String(64), nullable=False)
    attempt_count = db.Column(
        db.Integer, nullable=False, default=0, server_default="0"
    )
    provider_message_id = db.Column(db.String(180), nullable=True, index=True)
    last_error_code = db.Column(db.String(128), nullable=True)

    __table_args__ = (
        db.UniqueConstraint(
            "campaign_id",
            "contact_id",
            name="uq_campaign_delivery_intent_contact",
        ),
        db.CheckConstraint(
            "queue_status IN ('held', 'excluded')",
            name="ck_campaign_delivery_intent_queue_status",
        ),
        db.CheckConstraint(
            "transport_status IN ('not_attempted', 'unknown', 'accepted', 'sent', "
            "'delivered', 'read', 'failed')",
            name="ck_campaign_delivery_intent_transport_status",
        ),
        db.CheckConstraint(
            "attempt_count >= 0",
            name="ck_campaign_delivery_intent_attempt_count",
        ),
        db.CheckConstraint(
            "destination_digest IS NULL OR length(destination_digest) = 64",
            name="ck_campaign_delivery_intent_destination_digest",
        ),
        db.CheckConstraint(
            "length(rendered_content_digest) = 64",
            name="ck_campaign_delivery_intent_rendered_digest",
        ),
        db.CheckConstraint(
            "((queue_status = 'held' AND exclusion_reason IS NULL AND "
            "destination_digest IS NOT NULL) OR "
            "(queue_status = 'excluded' AND exclusion_reason IS NOT NULL))",
            name="ck_campaign_delivery_intent_eligibility",
        ),
        db.CheckConstraint(
            "transport_status = 'not_attempted' AND attempt_count = 0 AND "
            "provider_message_id IS NULL",
            name="ck_campaign_delivery_intent_prepare_only",
        ),
        db.Index(
            "ix_campaign_delivery_intent_tenant_queue",
            "tenant_id",
            "queue_status",
            "created_at",
            "id",
        ),
    )
