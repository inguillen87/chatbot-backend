from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app
from sqlalchemy.exc import IntegrityError

from extensions import db
from models_campaigns import CampaignDeliveryIntent, CampaignPreparation
from models_memory import Contact, InteractionEvent
from services.professional_message_preview import (
    ProfessionalMessageContractError,
    preview_notification_template,
)


CAMPAIGN_PREPARE_CONTRACT_VERSION = "crm_campaign_prepare.v1"
MAX_CAMPAIGN_RECIPIENTS = 500

_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_ALLOWED_FIELDS = frozenset(
    {
        "template_id",
        "key",
        "channel",
        "context",
        "content_variables",
        "contact_ids",
        "max_per_week",
        "min_interval_hours",
        "scheduled_for",
        "timezone",
    }
)


class CampaignPreparationError(ValueError):
    """Stable validation error that never includes submitted values."""

    def __init__(
        self,
        code: str,
        *,
        field: str | None = None,
        status_code: int = 400,
    ) -> None:
        self.code = str(code)
        self.field = field
        self.status_code = int(status_code)
        super().__init__(self.code)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": CAMPAIGN_PREPARE_CONTRACT_VERSION,
            "error": self.code,
        }
        if self.field:
            payload["field"] = self.field
        return payload


def _canonical_digest(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise CampaignPreparationError(
            "campaign_payload_not_serializable", field="body"
        ) from exc
    return hashlib.sha256(payload).hexdigest()


def _normalize_idempotency_key(value: Any) -> str:
    if not isinstance(value, str):
        raise CampaignPreparationError(
            "idempotency_key_required", field="Idempotency-Key"
        )
    normalized = value.strip()
    if not _IDEMPOTENCY_KEY_RE.fullmatch(normalized):
        raise CampaignPreparationError(
            "idempotency_key_invalid", field="Idempotency-Key"
        )
    return normalized


def _bounded_integer(
    value: Any,
    *,
    field: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise CampaignPreparationError(
            "campaign_integer_invalid", field=field
        )
    if value < minimum or value > maximum:
        raise CampaignPreparationError(
            "campaign_integer_out_of_range", field=field
        )
    return value


def _normalize_contact_ids(value: Any) -> tuple[list[str], int]:
    if not isinstance(value, list) or not value:
        raise CampaignPreparationError(
            "contact_ids_required", field="contact_ids"
        )
    if len(value) > MAX_CAMPAIGN_RECIPIENTS:
        raise CampaignPreparationError(
            "campaign_recipient_limit_exceeded", field="contact_ids"
        )

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise CampaignPreparationError(
                "contact_id_invalid", field="contact_ids"
            )
        contact_id = raw.strip()
        if not contact_id or len(contact_id) > 128 or "\x00" in contact_id:
            raise CampaignPreparationError(
                "contact_id_invalid", field="contact_ids"
            )
        if contact_id not in seen:
            seen.add(contact_id)
            normalized.append(contact_id)
    return normalized, len(value) - len(normalized)


def _normalize_schedule(value: Any, timezone_name: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise CampaignPreparationError(
            "scheduled_for_invalid", field="scheduled_for"
        )
    zone_name = str(timezone_name or "UTC").strip() or "UTC"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(zone_name))
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise CampaignPreparationError(
            "scheduled_for_invalid", field="scheduled_for"
        ) from exc
    return parsed.astimezone(timezone.utc)


def _destination_for(contact: Contact, channel: str) -> str:
    if channel == "whatsapp":
        return str(contact.phone or "").strip()
    return str(contact.email or "").strip().lower()


def _destination_digest(*, tenant_id: int, channel: str, destination: str) -> str:
    secret = str(current_app.config.get("SECRET_KEY") or "").encode("utf-8")
    if not secret:
        raise CampaignPreparationError("campaign_digest_secret_missing")
    material = f"{tenant_id}:{channel}:{destination}".encode("utf-8")
    return hmac.new(secret, material, hashlib.sha256).hexdigest()


def _legacy_frequency_counts(
    *,
    tenant_id: int,
    contact_ids: list[str],
    min_interval_hours: int,
) -> tuple[Counter[str], set[str]]:
    """Conservatively count legacy records that were labelled as sends.

    Historical ``campaign_send`` rows do not prove provider delivery.  They are
    still used as a conservative frequency guard, but never as delivery
    metrics in the v1 preparation contract.
    """

    if not contact_ids:
        return Counter(), set()
    now = datetime.now(timezone.utc)
    week_since = now - timedelta(days=7)
    interval_since = now - timedelta(hours=min_interval_hours)
    events = InteractionEvent.query.filter(
        InteractionEvent.tenant_id == tenant_id,
        InteractionEvent.contact_id.in_(contact_ids),
        InteractionEvent.direction == "outbound",
        InteractionEvent.created_at >= week_since,
    ).all()
    weekly: Counter[str] = Counter()
    recent: set[str] = set()
    for event in events:
        metadata = (
            event.metadata_payload
            if isinstance(event.metadata_payload, Mapping)
            else {}
        )
        if metadata.get("event_type") != "campaign_send" or not event.contact_id:
            continue
        weekly[event.contact_id] += 1
        created_at = event.created_at
        if created_at is not None:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if created_at >= interval_since:
                recent.add(event.contact_id)
    return weekly, recent


def _serialize_receipt(intent: CampaignDeliveryIntent) -> dict[str, Any]:
    return {
        "intent_id": intent.id,
        "contact_id": intent.contact_id,
        "queue_status": intent.queue_status,
        "exclusion_reason": intent.exclusion_reason,
        "transport_status": intent.transport_status,
        "attempt_count": int(intent.attempt_count or 0),
        "provider_receipt_present": bool(intent.provider_message_id),
    }


def serialize_campaign_preparation(
    campaign: CampaignPreparation,
    *,
    idempotent_replay: bool,
) -> dict[str, Any]:
    intents = (
        CampaignDeliveryIntent.query.filter_by(
            tenant_id=campaign.tenant_id,
            campaign_id=campaign.id,
        )
        .order_by(CampaignDeliveryIntent.created_at.asc(), CampaignDeliveryIntent.id.asc())
        .all()
    )
    audience = dict(campaign.audience_json or {})
    readiness = dict(campaign.readiness_json or {})
    transport_counts = Counter(intent.transport_status for intent in intents)
    return {
        "contract_version": CAMPAIGN_PREPARE_CONTRACT_VERSION,
        "campaign": {
            "id": campaign.id,
            "status": campaign.status,
            "channel": campaign.channel,
            "template": {
                "id": campaign.template_id,
                "key": campaign.template_key,
                "message_template_registry_id": campaign.message_template_registry_id,
            },
            "scheduled_for_utc": (
                campaign.scheduled_for.isoformat()
                if campaign.scheduled_for is not None
                else None
            ),
            "created_at": (
                campaign.created_at.isoformat()
                if campaign.created_at is not None
                else None
            ),
            "idempotent_replay": bool(idempotent_replay),
        },
        "preview": {
            "subject": campaign.rendered_subject,
            "body": campaign.rendered_body,
            "strict_variable_contract": True,
            "contains_unresolved_variables": False,
        },
        "audience": audience,
        "readiness": readiness,
        "queue": {
            "state": "held",
            "dispatch_authorized": False,
            "receipts": [_serialize_receipt(intent) for intent in intents],
            "transport_outcomes": {
                "not_attempted": transport_counts.get("not_attempted", 0),
                "unknown": transport_counts.get("unknown", 0),
                "accepted": transport_counts.get("accepted", 0),
                "sent": transport_counts.get("sent", 0),
                "delivered": transport_counts.get("delivered", 0),
                "read": transport_counts.get("read", 0),
                "failed": transport_counts.get("failed", 0),
            },
        },
        "side_effects": {
            "campaigns_created": 0 if idempotent_replay else 1,
            "queue_receipts_created": 0 if idempotent_replay else len(intents),
            "notifications_queued": 0,
            "provider_calls_performed": False,
            "messages_sent": 0,
        },
    }


def prepare_campaign(
    *,
    tenant_id: int,
    actor_user_id: int,
    idempotency_key: Any,
    payload: Any,
) -> tuple[CampaignPreparation, bool]:
    """Render, evaluate and persist held campaign delivery intents.

    This function performs database writes only.  It never inserts a
    ``Notification`` row, invokes a provider, or authorizes a retry.
    """

    if not isinstance(payload, dict):
        raise CampaignPreparationError(
            "request_body_must_be_object", field="body"
        )
    unexpected = sorted(set(payload) - _ALLOWED_FIELDS)
    if unexpected:
        raise CampaignPreparationError(
            "campaign_request_fields_unsupported", field="body"
        )

    idem = _normalize_idempotency_key(idempotency_key)
    contact_ids, duplicate_count = _normalize_contact_ids(payload.get("contact_ids"))
    max_per_week = _bounded_integer(
        payload.get("max_per_week"),
        field="max_per_week",
        default=2,
        minimum=1,
        maximum=50,
    )
    min_interval_hours = _bounded_integer(
        payload.get("min_interval_hours"),
        field="min_interval_hours",
        default=24,
        minimum=1,
        maximum=168,
    )
    scheduled_for = _normalize_schedule(
        payload.get("scheduled_for"), payload.get("timezone")
    )

    canonical_request = {
        "tenant_id": int(tenant_id),
        "template_id": payload.get("template_id"),
        "key": payload.get("key"),
        "channel": (
            str(payload.get("channel") or "").strip().lower() or None
        ),
        "context": payload.get("context") if payload.get("context") is not None else {},
        "content_variables": (
            payload.get("content_variables")
            if payload.get("content_variables") is not None
            else {}
        ),
        "contact_ids": sorted(contact_ids),
        "max_per_week": max_per_week,
        "min_interval_hours": min_interval_hours,
        "scheduled_for_utc": scheduled_for.isoformat() if scheduled_for else None,
    }
    payload_digest = _canonical_digest(canonical_request)

    existing = CampaignPreparation.query.filter_by(
        tenant_id=int(tenant_id), idempotency_key=idem
    ).one_or_none()
    if existing is not None:
        persisted = str(existing.payload_digest or "").strip().lower()
        if not persisted or not hmac.compare_digest(persisted, payload_digest):
            raise CampaignPreparationError(
                "campaign_idempotency_conflict", status_code=409
            )
        return existing, False

    try:
        preview = preview_notification_template(
            tenant_id=tenant_id,
            template_id=payload.get("template_id"),
            key=payload.get("key"),
            channel=payload.get("channel"),
            context=payload.get("context"),
            content_variables=payload.get("content_variables"),
        )
    except ProfessionalMessageContractError as exc:
        status_code = 404 if exc.code == "notification_template_not_found" else 400
        raise CampaignPreparationError(
            exc.code, field=exc.field, status_code=status_code
        ) from exc

    channel = str(preview["template"]["channel"])
    if channel not in {"whatsapp", "email"}:
        raise CampaignPreparationError(
            "campaign_channel_not_supported", field="channel"
        )

    contacts = Contact.query.filter(
        Contact.tenant_id == int(tenant_id),
        Contact.id.in_(contact_ids),
    ).all()
    contacts_by_id = {contact.id: contact for contact in contacts}
    ordered_contacts = [
        contacts_by_id[contact_id]
        for contact_id in contact_ids
        if contact_id in contacts_by_id
    ]
    weekly_counts, recent_contacts = _legacy_frequency_counts(
        tenant_id=int(tenant_id),
        contact_ids=[contact.id for contact in ordered_contacts],
        min_interval_hours=min_interval_hours,
    )

    exclusion_counts: Counter[str] = Counter()
    evaluated: list[tuple[Contact, str | None, str]] = []
    for contact in ordered_contacts:
        preferences = (
            contact.preferences if isinstance(contact.preferences, Mapping) else {}
        )
        destination = _destination_for(contact, channel)
        reason = None
        if (
            preferences.get("marketing_opt_out") is True
            or preferences.get("marketing_opt_in") is False
        ):
            reason = "opt_out"
        elif preferences.get("marketing_opt_in") is not True:
            # This contract is for marketing campaigns.  Absence of recorded
            # consent is not consent; transactional messaging needs a separate
            # purpose-specific contract and cannot be inferred here.
            reason = "consent_missing"
        elif not destination:
            reason = "missing_whatsapp" if channel == "whatsapp" else "missing_email"
        elif (
            weekly_counts.get(contact.id, 0) >= max_per_week
            or contact.id in recent_contacts
        ):
            reason = "frequency_window"
        if reason:
            exclusion_counts[reason] += 1
        evaluated.append((contact, reason, destination))

    eligible_count = sum(1 for _, reason, _ in evaluated if reason is None)
    audience = {
        "requested": len(contact_ids) + duplicate_count,
        "unique_requested": len(contact_ids),
        "resolved": len(ordered_contacts),
        "eligible": eligible_count,
        "excluded": len(evaluated) - eligible_count,
        "unresolved": len(contact_ids) - len(ordered_contacts),
        "duplicates_ignored": duplicate_count,
        "exclusion_counts": {
            "opt_out": exclusion_counts.get("opt_out", 0),
            "consent_missing": exclusion_counts.get("consent_missing", 0),
            "missing_whatsapp": exclusion_counts.get("missing_whatsapp", 0),
            "missing_email": exclusion_counts.get("missing_email", 0),
            "frequency_window": exclusion_counts.get("frequency_window", 0),
        },
        "rate_limit_policy": {
            "max_per_week": max_per_week,
            "min_interval_hours": min_interval_hours,
            "legacy_records_are_conservative_guards_only": True,
        },
        "consent_policy": {
            "purpose": "marketing",
            "explicit_opt_in_required": True,
        },
    }

    blockers = list(preview["readiness"]["blockers"])
    blockers.append("campaign_dispatch_not_authorized")
    if scheduled_for is not None:
        blockers.append("campaign_scheduler_not_connected")
    if eligible_count == 0:
        blockers.append("campaign_audience_empty")
    blockers = list(dict.fromkeys(blockers))
    readiness = {
        "preview_valid": True,
        "audience_evaluated": True,
        "consent_evaluated": True,
        "rate_limit_evaluated": True,
        "provider_template_approval_valid": bool(
            preview["readiness"]["provider_template_approval_valid"]
        ),
        "provider_template_ready": bool(
            preview["readiness"]["provider_template_ready"]
        ),
        "transport_readiness_checked": False,
        "production_send_allowed": False,
        "blockers": blockers,
    }
    preview_digest = _canonical_digest(
        {
            "template": preview["template"],
            "rendered": preview["rendered"],
            "provider_template": preview["provider_template"],
        }
    )
    content_digest = _canonical_digest(
        {
            "subject": preview["rendered"]["subject"],
            "body": preview["rendered"]["body"],
        }
    )
    campaign = CampaignPreparation(
        tenant_id=int(tenant_id),
        created_by_user_id=int(actor_user_id),
        contract_version=CAMPAIGN_PREPARE_CONTRACT_VERSION,
        status=CampaignPreparation.STATUS_DRAFT,
        channel=channel,
        template_id=preview["template"]["id"],
        template_key=preview["template"]["key"],
        message_template_registry_id=preview["template"][
            "message_template_registry_id"
        ],
        idempotency_key=idem,
        payload_digest=payload_digest,
        preview_digest=preview_digest,
        rendered_subject=preview["rendered"]["subject"],
        rendered_body=preview["rendered"]["body"],
        scheduled_for=scheduled_for,
        audience_json=audience,
        readiness_json=readiness,
    )

    try:
        with db.session.begin_nested():
            db.session.add(campaign)
            db.session.flush()
            for contact, reason, destination in evaluated:
                db.session.add(
                    CampaignDeliveryIntent(
                        campaign_id=campaign.id,
                        tenant_id=int(tenant_id),
                        contact_id=contact.id,
                        queue_status=(
                            CampaignDeliveryIntent.QUEUE_EXCLUDED
                            if reason
                            else CampaignDeliveryIntent.QUEUE_HELD
                        ),
                        exclusion_reason=reason,
                        transport_status=CampaignDeliveryIntent.TRANSPORT_NOT_ATTEMPTED,
                        destination_digest=(
                            _destination_digest(
                                tenant_id=int(tenant_id),
                                channel=channel,
                                destination=destination,
                            )
                            if destination
                            else None
                        ),
                        rendered_content_digest=content_digest,
                        attempt_count=0,
                    )
                )
            db.session.flush()
    except IntegrityError:
        existing = CampaignPreparation.query.filter_by(
            tenant_id=int(tenant_id), idempotency_key=idem
        ).one_or_none()
        if existing is None:
            raise
        persisted = str(existing.payload_digest or "").strip().lower()
        if not persisted or not hmac.compare_digest(persisted, payload_digest):
            raise CampaignPreparationError(
                "campaign_idempotency_conflict", status_code=409
            )
        return existing, False

    return campaign, True
