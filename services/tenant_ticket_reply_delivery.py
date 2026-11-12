"""Durable, tenant-scoped delivery state for human TenantTicket replies."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import re
from typing import Any, Mapping

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import (
    AuditEvent,
    DomainEffectOutbox,
    MessageTemplateRegistry,
    ProviderSender,
    TenantTicketReplyEvent,
    WhatsAppContactState,
    db,
)
from services.message_templates import whatsapp_template_lifecycle


DELIVERY_CONTRACT_VERSION = "tenant_ticket.reply_delivery.v1"
DELIVERY_REALTIME_CONTRACT_VERSION = "tenant_ticket.reply_delivery.realtime.v1"
SERVICE_WINDOW_CONTRACT_VERSION = "whatsapp.service_window.v1"

_PROVIDER_STATUS_TO_DELIVERY = {
    "accepted": "provider_accepted",
    "scheduled": "provider_accepted",
    "queued": "provider_accepted",
    "sending": "provider_accepted",
    "sent": "provider_accepted",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
    "undelivered": "failed",
    "canceled": "failed",
    "cancelled": "failed",
}
_DELIVERY_RANK = {
    "saved": 0,
    "queued": 10,
    "uncertain": 15,
    "provider_accepted": 20,
    "delivered": 30,
    "read": 40,
}
_PROVIDER_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,179}$")
_TEMPLATE_BODY_VARIABLE_RE = re.compile(r"{{\s*([1-9][0-9]{0,2})\s*}}")
_UNRESOLVED_TEMPLATE_TOKEN_RE = re.compile(r"{{[^{}]*}}")


class TenantTicketReplyDeliveryError(ValueError):
    def __init__(self, code: str):
        self.code = str(code or "tenant_ticket_reply_delivery_invalid")
        super().__init__(self.code)


class TenantTicketReplyProviderMessageCollision(TenantTicketReplyDeliveryError):
    """A provider message id already belongs to another reply in the tenant.

    This is deliberately distinct from a transport exception.  The provider
    may already have accepted the message, so callers must quarantine the
    reply and reconcile it manually instead of retrying the send.
    """

    def __init__(self):
        super().__init__("whatsapp_provider_message_id_duplicate")


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    normalized = _utc(value)
    return normalized.isoformat() if normalized else None


def _recipient_variants(recipient: Any) -> tuple[str, ...]:
    normalized = str(recipient or "").strip()
    if normalized.lower().startswith("whatsapp:"):
        normalized = normalized.split(":", 1)[1].strip()
    if not normalized:
        return ()
    return normalized, f"whatsapp:{normalized}"


def whatsapp_service_window(
    *,
    tenant_id: int,
    provider_sender_id: int,
    recipient: Any,
    now: datetime | None = None,
    session=None,
) -> dict[str, Any]:
    """Resolve the tenant-bound inbound evidence for Meta's 24-hour rule."""

    current = _utc(now) or datetime.now(timezone.utc)
    try:
        normalized_sender_id = int(provider_sender_id)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TenantTicketReplyDeliveryError(
            "whatsapp_provider_sender_required"
        ) from exc
    if isinstance(provider_sender_id, bool) or normalized_sender_id <= 0:
        raise TenantTicketReplyDeliveryError("whatsapp_provider_sender_required")
    variants = _recipient_variants(recipient)
    state = None
    if variants:
        effect_session = session or db.session
        state = (
            effect_session.query(WhatsAppContactState)
            .filter(
                WhatsAppContactState.tenant_id == int(tenant_id),
                WhatsAppContactState.provider_sender_id == normalized_sender_id,
                WhatsAppContactState.recipient.in_(variants),
            )
            .order_by(WhatsAppContactState.last_inbound_at.desc())
            .first()
        )
    last_inbound = _utc(getattr(state, "last_inbound_at", None))
    expires_at = last_inbound + timedelta(hours=24) if last_inbound else None
    is_open = bool(
        last_inbound
        and last_inbound <= current + timedelta(minutes=5)
        and expires_at
        and current <= expires_at
    )
    return {
        "contract_version": SERVICE_WINDOW_CONTRACT_VERSION,
        "status": "open" if is_open else ("expired" if last_inbound else "unknown"),
        "last_inbound_at": _iso(last_inbound),
        "expires_at": _iso(expires_at),
        "free_form_allowed": is_open,
        "template_required": not is_open,
        "sender_bound": True,
        "provider_sender_id": normalized_sender_id,
        "authoritative_source": (
            "whatsapp_contact_state.provider_sender_id+recipient+last_inbound_at"
        ),
    }


def _approved_template_payload(
    registry: MessageTemplateRegistry | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if registry is None:
        return None, None
    lifecycle = whatsapp_template_lifecycle(
        registry.status,
        source="message_template_registry",
        provider_reference=registry.content_sid,
        observed_at=registry.last_sync_at,
    )
    if not lifecycle.get("production_send_allowed"):
        return None, None
    from services.tenant_twilio_messaging import tenant_template_delivery_snapshot

    delivery_snapshot = tenant_template_delivery_snapshot(registry)
    return {
        "id": int(registry.id),
        "name": registry.name,
        "language": registry.language,
        "category": registry.category,
        "body_preview": registry.body_preview,
        "variable_keys": list(delivery_snapshot.get("variable_keys") or []),
        "lifecycle": lifecycle,
    }, delivery_snapshot


def approved_whatsapp_templates(
    *, tenant_id: int, session=None, limit: int = 25
) -> list[dict[str, Any]]:
    effect_session = session or db.session
    rows = (
        effect_session.query(MessageTemplateRegistry)
        .filter_by(tenant_id=int(tenant_id), provider="twilio", channel="whatsapp")
        .order_by(MessageTemplateRegistry.name.asc(), MessageTemplateRegistry.id.asc())
        .limit(max(1, min(int(limit), 100)))
        .all()
    )
    return [
        public
        for row in rows
        if (public := _approved_template_payload(row)[0]) is not None
    ]


def normalize_template_variables(value: Any) -> dict[str, str] | None:
    from services.tenant_twilio_messaging import normalize_whatsapp_template_variables

    normalized, error = normalize_whatsapp_template_variables(value)
    if error:
        raise TenantTicketReplyDeliveryError(error)
    return normalized


def render_whatsapp_template_body_snapshot(
    body_preview: Any,
    variables: Mapping[str, str] | None,
) -> str:
    """Render the customer-visible body pinned to one approved template send.

    The provider call uses ``ContentSid`` rather than a free-form body.  This
    snapshot is therefore the only text the CRM may publish as delivered.  A
    missing or unresolved preview fails closed instead of allowing the
    operator timeline to claim a different message.
    """

    preview = str(body_preview or "").strip()
    if not preview:
        raise TenantTicketReplyDeliveryError(
            "whatsapp_template_body_preview_missing"
        )
    normalized_variables = dict(variables or {})

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in normalized_variables:
            raise TenantTicketReplyDeliveryError(
                "whatsapp_template_variables_schema_mismatch"
            )
        return normalized_variables[key]

    rendered = _TEMPLATE_BODY_VARIABLE_RE.sub(replace, preview).strip()
    if not rendered or _UNRESOLVED_TEMPLATE_TOKEN_RE.search(rendered):
        raise TenantTicketReplyDeliveryError(
            "whatsapp_template_body_unresolved"
        )
    return rendered


def prepare_whatsapp_reply_policy(
    *,
    tenant_id: int,
    provider_sender_id: int,
    provider_sender_binding: str | None = None,
    recipient: Any,
    template_registry_id: Any = None,
    template_variables: Any = None,
    session=None,
    now: datetime | None = None,
    dispatch_enabled: bool = True,
) -> tuple[int | None, dict[str, str] | None, dict[str, Any]]:
    effect_session = session or db.session
    window = whatsapp_service_window(
        tenant_id=int(tenant_id),
        provider_sender_id=provider_sender_id,
        recipient=recipient,
        now=now,
        session=effect_session,
    )
    normalized_template_id = None
    registry_payload = None
    registry_delivery_snapshot = None
    if template_registry_id not in (None, ""):
        try:
            normalized_template_id = int(template_registry_id)
        except (TypeError, ValueError, OverflowError) as exc:
            raise TenantTicketReplyDeliveryError(
                "whatsapp_template_registry_invalid"
            ) from exc
        if isinstance(template_registry_id, bool) or normalized_template_id <= 0:
            raise TenantTicketReplyDeliveryError("whatsapp_template_registry_invalid")
        registry = (
            effect_session.query(MessageTemplateRegistry)
            .filter_by(
                id=normalized_template_id,
                tenant_id=int(tenant_id),
                provider="twilio",
                channel="whatsapp",
            )
            .one_or_none()
        )
        registry_payload, registry_delivery_snapshot = _approved_template_payload(registry)
        if registry_payload is None:
            raise TenantTicketReplyDeliveryError("whatsapp_template_not_approved")
    variables = normalize_template_variables(template_variables)
    if variables is not None and normalized_template_id is None:
        raise TenantTicketReplyDeliveryError("whatsapp_template_registry_required")
    expected_variable_keys = set(
        (registry_delivery_snapshot or {}).get("variable_keys") or []
    )
    actual_variable_keys = set(variables or {})
    if normalized_template_id is not None and actual_variable_keys != expected_variable_keys:
        raise TenantTicketReplyDeliveryError(
            "whatsapp_template_variables_schema_mismatch"
        )
    delivery_body_snapshot = None
    if normalized_template_id is not None:
        delivery_body_snapshot = render_whatsapp_template_body_snapshot(
            (registry_delivery_snapshot or {}).get("body_preview"),
            variables,
        )
    if (
        dispatch_enabled
        and window["template_required"]
        and normalized_template_id is None
    ):
        raise TenantTicketReplyDeliveryError(
            "whatsapp_template_required_outside_24h"
        )
    snapshot = {
        **window,
        "selected_template": registry_payload,
        "_delivery_binding": {
            "provider_sender_id": int(provider_sender_id),
            "provider_sender_binding": str(provider_sender_binding or "").strip(),
            "template": registry_delivery_snapshot,
            "template_variables": variables,
            "delivery_body_snapshot": delivery_body_snapshot,
            "delivery_body_source": (
                "approved_template_registry_snapshot"
                if delivery_body_snapshot is not None
                else "operator_free_form"
            ),
        },
        "external_dispatch_enabled": bool(dispatch_enabled),
        "decision": (
            "approved_template"
            if normalized_template_id
            else ("free_form" if dispatch_enabled else "crm_only_not_dispatched")
        ),
    }
    return normalized_template_id, variables, snapshot


def _outbox_for_reply(
    record: TenantTicketReplyEvent, *, session=None
) -> DomainEffectOutbox | None:
    effect_session = session or db.session
    return (
        effect_session.query(DomainEffectOutbox)
        .filter_by(
            tenant_id=int(record.tenant_id),
            aggregate_type="tenant_ticket_reply",
            aggregate_ref=f"{record.ticket_id}:{record.event_id}",
            channel="whatsapp",
        )
        .order_by(DomainEffectOutbox.id.desc())
        .first()
    )


def serialize_reply_delivery(
    record: TenantTicketReplyEvent, *, session=None
) -> dict[str, Any]:
    status = str(record.whatsapp_delivery_status or "saved").strip().lower()
    source = "provider_status_callback" if record.whatsapp_status_event_id else "crm_transaction"
    error_code = record.whatsapp_error_code
    outbox = _outbox_for_reply(record, session=session)
    if not record.whatsapp_status_event_id and outbox is not None:
        source = "domain_effect_outbox"
        if outbox.status in {"pending", "processing", "retry_wait"}:
            status = "queued"
        elif outbox.status == "succeeded" and status in {"saved", "queued"}:
            status = "provider_accepted"
        elif outbox.status == "unknown":
            status = "uncertain"
            error_code = error_code or outbox.last_error_code or (
                (outbox.result_json or {}).get("reason_code")
                if isinstance(outbox.result_json, Mapping)
                else None
            ) or "outbox_unknown"
        elif outbox.status in {"skipped", "dead"}:
            status = "failed"
            error_code = outbox.last_error_code or (
                (outbox.result_json or {}).get("reason_code")
                if isinstance(outbox.result_json, Mapping)
                else None
            ) or f"outbox_{outbox.status}"
    template = None
    if record.whatsapp_template_registry_id:
        template = {
            "registry_id": int(record.whatsapp_template_registry_id),
            "variables_present": bool(record.whatsapp_template_variables),
        }
    public_policy_snapshot = dict(record.whatsapp_policy_snapshot or {})
    public_policy_snapshot.pop("_delivery_binding", None)
    public_policy_snapshot.pop("provider_sender_id", None)
    return {
        "contract_version": DELIVERY_CONTRACT_VERSION,
        "event_id": record.event_id,
        "channel": "whatsapp",
        "status": status,
        "authoritative_source": source,
        "provider_message_id": record.whatsapp_provider_message_id,
        "provider_status": record.whatsapp_provider_status,
        "error_code": error_code,
        "updated_at": _iso(record.whatsapp_status_updated_at or record.created_at),
        "provider_accepted_at": _iso(record.whatsapp_provider_accepted_at),
        "delivered_at": _iso(record.whatsapp_delivered_at),
        "read_at": _iso(record.whatsapp_read_at),
        "failed_at": _iso(record.whatsapp_failed_at),
        "automatic_retry_allowed": bool(
            outbox is not None
            and outbox.status in {"pending", "retry_wait"}
            and not record.whatsapp_status_event_id
            and status != "uncertain"
        ),
        "reconciliation_required": status == "uncertain",
        "service_window": public_policy_snapshot,
        "template": template,
    }


def list_ticket_reply_deliveries(
    *, tenant_id: int, ticket_id: int, session=None
) -> list[dict[str, Any]]:
    effect_session = session or db.session
    records = (
        effect_session.query(TenantTicketReplyEvent)
        .filter(
            TenantTicketReplyEvent.tenant_id == int(tenant_id),
            TenantTicketReplyEvent.ticket_id == int(ticket_id),
            TenantTicketReplyEvent.recipient_phone.isnot(None),
        )
        .order_by(TenantTicketReplyEvent.created_at.asc(), TenantTicketReplyEvent.id.asc())
        .all()
    )
    return [serialize_reply_delivery(record, session=effect_session) for record in records]


def record_provider_acceptance(
    *,
    reply_event_record_id: int,
    tenant_id: int,
    provider_message_id: Any,
    provider_sender_id: int,
) -> None:
    message_id = str(provider_message_id or "").strip()
    if not _PROVIDER_MESSAGE_ID_RE.fullmatch(message_id):
        raise TenantTicketReplyDeliveryError("whatsapp_provider_message_id_invalid")
    now = datetime.now(timezone.utc)
    try:
        with Session(db.engine) as session:
            with session.begin():
                record = (
                    session.query(TenantTicketReplyEvent)
                    .filter_by(id=int(reply_event_record_id), tenant_id=int(tenant_id))
                    .with_for_update()
                    .one_or_none()
                )
                if record is None:
                    raise TenantTicketReplyDeliveryError(
                        "tenant_ticket_reply_event_not_found"
                    )
                if record.whatsapp_provider_message_id not in (None, "", message_id):
                    raise TenantTicketReplyDeliveryError(
                        "whatsapp_provider_message_id_mismatch"
                    )
                if record.whatsapp_provider_sender_id not in (
                    None,
                    int(provider_sender_id),
                ):
                    raise TenantTicketReplyDeliveryError(
                        "whatsapp_provider_sender_mismatch"
                    )
                duplicate = (
                    session.query(TenantTicketReplyEvent.id)
                    .filter(
                        TenantTicketReplyEvent.tenant_id == int(tenant_id),
                        TenantTicketReplyEvent.whatsapp_provider_message_id == message_id,
                        TenantTicketReplyEvent.id != int(record.id),
                    )
                    .first()
                )
                if duplicate is not None:
                    raise TenantTicketReplyProviderMessageCollision()
                current = str(
                    record.whatsapp_delivery_status or "saved"
                ).strip().lower()
                record.whatsapp_provider_message_id = message_id
                record.whatsapp_provider_sender_id = int(provider_sender_id)
                record.whatsapp_provider_accepted_at = (
                    record.whatsapp_provider_accepted_at or now
                )
                if current in {"saved", "queued", "uncertain", "provider_accepted"}:
                    record.whatsapp_delivery_status = "provider_accepted"
                    record.whatsapp_provider_status = "accepted"
                    record.whatsapp_error_code = None
                    record.whatsapp_status_updated_at = now
    except TenantTicketReplyProviderMessageCollision:
        _record_provider_message_collision(
            reply_event_record_id=int(reply_event_record_id),
            tenant_id=int(tenant_id),
            provider_message_id=message_id,
            provider_sender_id=int(provider_sender_id),
            source="provider_acceptance",
        )
        raise
    except IntegrityError as exc:
        _record_provider_message_collision(
            reply_event_record_id=int(reply_event_record_id),
            tenant_id=int(tenant_id),
            provider_message_id=message_id,
            provider_sender_id=int(provider_sender_id),
            source="provider_acceptance_unique_race",
        )
        raise TenantTicketReplyProviderMessageCollision() from exc


def _record_provider_message_collision(
    *,
    reply_event_record_id: int,
    tenant_id: int,
    provider_message_id: str,
    provider_sender_id: int,
    source: str,
    delivery_event_id: int | None = None,
) -> None:
    """Persist a privacy-safe collision quarantine in an independent txn."""

    now = datetime.now(timezone.utc)
    message_id_hash = hashlib.sha256(
        str(provider_message_id).encode("utf-8")
    ).hexdigest()
    with Session(db.engine) as session:
        with session.begin():
            record = (
                session.query(TenantTicketReplyEvent)
                .filter_by(
                    id=int(reply_event_record_id),
                    tenant_id=int(tenant_id),
                )
                .with_for_update()
                .one_or_none()
            )
            if record is None:
                raise TenantTicketReplyDeliveryError(
                    "tenant_ticket_reply_event_not_found"
                )
            current = str(
                record.whatsapp_delivery_status or "saved"
            ).strip().lower()
            if record.whatsapp_status_event_id is None and current in {
                "saved",
                "queued",
                "uncertain",
                "provider_accepted",
            }:
                record.whatsapp_delivery_status = "uncertain"
                record.whatsapp_provider_status = "collision_quarantined"
                record.whatsapp_error_code = (
                    "whatsapp_provider_message_id_duplicate"
                )
                record.whatsapp_status_updated_at = now
                if record.whatsapp_provider_sender_id is None:
                    record.whatsapp_provider_sender_id = int(provider_sender_id)
            session.add(
                AuditEvent(
                    tenant_id=int(tenant_id),
                    event_type=(
                        "tenant_ticket.reply.whatsapp.provider_message_collision"
                    ),
                    resource_type="tenant_ticket_reply_event",
                    resource_id=str(record.id),
                    details={
                        "contract_version": DELIVERY_CONTRACT_VERSION,
                        "event_id": record.event_id,
                        "source": str(source or "unknown")[:80],
                        "provider_sender_id": int(provider_sender_id),
                        "delivery_event_id": (
                            int(delivery_event_id) if delivery_event_id else None
                        ),
                        "provider_message_id_sha256": message_id_hash,
                        "error_code": (
                            "whatsapp_provider_message_id_duplicate"
                        ),
                        "automatic_retry_allowed": False,
                        "quarantined": True,
                    },
                )
            )


def record_provider_uncertainty(
    *,
    reply_event_record_id: int,
    tenant_id: int,
    error_code: str = "provider_acceptance_unknown",
) -> None:
    """Persist ambiguity without downgrading signed provider callback truth."""

    now = datetime.now(timezone.utc)
    safe_error = str(error_code or "provider_acceptance_unknown").strip()[:80]
    with Session(db.engine) as session:
        with session.begin():
            record = (
                session.query(TenantTicketReplyEvent)
                .filter_by(id=int(reply_event_record_id), tenant_id=int(tenant_id))
                .with_for_update()
                .one_or_none()
            )
            if record is None:
                raise TenantTicketReplyDeliveryError("tenant_ticket_reply_event_not_found")
            current = str(record.whatsapp_delivery_status or "saved").strip().lower()
            if record.whatsapp_status_event_id is None and current in {
                "saved",
                "queued",
                "uncertain",
            }:
                record.whatsapp_delivery_status = "uncertain"
                record.whatsapp_provider_status = "unknown"
                record.whatsapp_error_code = safe_error
                record.whatsapp_status_updated_at = now


def _transition_allowed(current: str, incoming: str) -> bool:
    if current == "failed":
        return incoming == "failed"
    if incoming == "failed":
        return _DELIVERY_RANK.get(current, 0) < _DELIVERY_RANK["delivered"]
    return _DELIVERY_RANK.get(incoming, -1) >= _DELIVERY_RANK.get(current, 0)


def reconcile_provider_callback(
    *,
    tenant_id: int,
    reply_event_record_id: Any,
    provider_message_id: Any,
    provider_status: Any,
    provider_sender_id: Any,
    delivery_event_id: Any,
    error_code: Any = None,
) -> TenantTicketReplyEvent | None:
    try:
        record_id = int(reply_event_record_id)
        sender_id = int(provider_sender_id)
        status_event_id = int(delivery_event_id)
    except (TypeError, ValueError, OverflowError):
        return None
    message_id = str(provider_message_id or "").strip()
    provider_status_text = str(provider_status or "").strip().lower()[:80]
    incoming = _PROVIDER_STATUS_TO_DELIVERY.get(provider_status_text)
    if (
        record_id <= 0
        or sender_id <= 0
        or status_event_id <= 0
        or incoming is None
        or not _PROVIDER_MESSAGE_ID_RE.fullmatch(message_id)
    ):
        return None
    sender = ProviderSender.query.filter_by(
        id=sender_id, tenant_id=int(tenant_id), channel="whatsapp"
    ).one_or_none()
    if sender is None:
        return None
    record = (
        TenantTicketReplyEvent.query.filter_by(id=record_id, tenant_id=int(tenant_id))
        .with_for_update()
        .one_or_none()
    )
    if record is None or not record.recipient_phone:
        return None
    if record.whatsapp_provider_sender_id not in (None, sender_id):
        return None
    if record.whatsapp_provider_message_id not in (None, "", message_id):
        return None
    duplicate = (
        TenantTicketReplyEvent.query.filter(
            TenantTicketReplyEvent.tenant_id == int(tenant_id),
            TenantTicketReplyEvent.whatsapp_provider_message_id == message_id,
            TenantTicketReplyEvent.id != int(record.id),
        ).first()
    )
    if duplicate is not None:
        db.session.rollback()
        _record_provider_message_collision(
            reply_event_record_id=record_id,
            tenant_id=int(tenant_id),
            provider_message_id=message_id,
            provider_sender_id=sender_id,
            source="provider_callback",
            delivery_event_id=status_event_id,
        )
        raise TenantTicketReplyProviderMessageCollision()

    previous = str(record.whatsapp_delivery_status or "saved").strip().lower()
    if (
        record.whatsapp_status_event_id == status_event_id
        and previous == incoming
        and record.whatsapp_provider_status == provider_status_text
        and record.whatsapp_provider_message_id == message_id
        and record.whatsapp_provider_sender_id == sender_id
    ):
        return record

    applied = _transition_allowed(previous, incoming)
    now = datetime.now(timezone.utc)
    if applied:
        record.whatsapp_delivery_status = incoming
        record.whatsapp_provider_message_id = message_id
        record.whatsapp_provider_sender_id = sender_id
        record.whatsapp_provider_status = provider_status_text
        record.whatsapp_error_code = str(error_code or "").strip()[:80] or None
        record.whatsapp_status_event_id = status_event_id
        record.whatsapp_status_updated_at = now
        if incoming in {"provider_accepted", "delivered", "read"}:
            record.whatsapp_provider_accepted_at = record.whatsapp_provider_accepted_at or now
            record.whatsapp_error_code = None
        if incoming in {"delivered", "read"}:
            record.whatsapp_delivered_at = record.whatsapp_delivered_at or now
        if incoming == "read":
            record.whatsapp_read_at = record.whatsapp_read_at or now
        if incoming == "failed":
            record.whatsapp_failed_at = record.whatsapp_failed_at or now
        db.session.add(record)

    db.session.add(
        AuditEvent(
            tenant_id=int(tenant_id),
            event_type="tenant_ticket.reply.whatsapp.delivery_callback",
            resource_type="tenant_ticket_reply_event",
            resource_id=str(record.id),
            details={
                "contract_version": DELIVERY_CONTRACT_VERSION,
                "event_id": record.event_id,
                "previous_status": previous,
                "incoming_status": incoming,
                "provider_status": provider_status_text,
                "provider_sender_id": sender_id,
                "delivery_event_id": status_event_id,
                "provider_message_id_sha256": hashlib.sha256(
                    message_id.encode("utf-8")
                ).hexdigest(),
                "error_code": str(error_code or "").strip()[:80] or None,
                "applied": applied,
            },
        )
    )
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        _record_provider_message_collision(
            reply_event_record_id=record_id,
            tenant_id=int(tenant_id),
            provider_message_id=message_id,
            provider_sender_id=sender_id,
            source="provider_callback_unique_race",
            delivery_event_id=status_event_id,
        )
        raise TenantTicketReplyProviderMessageCollision() from exc
    return record


def emit_delivery_invalidation(*, tenant_id: int) -> None:
    from socket_service import emit_ticket_reply_delivery_updated

    emit_ticket_reply_delivery_updated({"tenant_profile_id": int(tenant_id)})


__all__ = [
    "DELIVERY_CONTRACT_VERSION",
    "DELIVERY_REALTIME_CONTRACT_VERSION",
    "TenantTicketReplyDeliveryError",
    "TenantTicketReplyProviderMessageCollision",
    "approved_whatsapp_templates",
    "emit_delivery_invalidation",
    "list_ticket_reply_deliveries",
    "normalize_template_variables",
    "prepare_whatsapp_reply_policy",
    "reconcile_provider_callback",
    "record_provider_acceptance",
    "record_provider_uncertainty",
    "serialize_reply_delivery",
    "whatsapp_service_window",
]
