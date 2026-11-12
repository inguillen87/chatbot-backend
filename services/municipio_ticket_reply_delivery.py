"""Durable delivery state for tenant-scoped MunicipioTicket WhatsApp replies."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
from typing import Any, Mapping

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import (
    AuditEvent,
    DomainEffectOutbox,
    MunicipioTicketReplyEvent,
    ProviderSender,
    db,
)
from services.tenant_ticket_reply_delivery import TenantTicketReplyDeliveryError


DELIVERY_CONTRACT_VERSION = "municipio_ticket.reply_delivery.v1"
DELIVERY_REALTIME_CONTRACT_VERSION = "municipio_ticket.reply_delivery.realtime.v1"

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


class MunicipioTicketReplyProviderMessageCollision(TenantTicketReplyDeliveryError):
    """A provider message id already belongs to another municipal reply."""

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


def _outbox_for_reply(
    record: MunicipioTicketReplyEvent, *, session=None
) -> DomainEffectOutbox | None:
    effect_session = session or db.session
    return (
        effect_session.query(DomainEffectOutbox)
        .filter_by(
            tenant_id=int(record.tenant_id),
            aggregate_type="municipio_ticket_reply",
            aggregate_ref=f"{record.ticket_id}:{record.event_id}",
            channel="whatsapp",
        )
        .order_by(DomainEffectOutbox.id.desc())
        .first()
    )


def serialize_reply_delivery(
    record: MunicipioTicketReplyEvent, *, session=None
) -> dict[str, Any]:
    status = str(record.whatsapp_delivery_status or "saved").strip().lower()
    source = (
        "provider_status_callback"
        if record.whatsapp_status_event_id
        else "crm_transaction"
    )
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
    public_policy_snapshot = dict(record.whatsapp_policy_snapshot or {})
    public_policy_snapshot.pop("_delivery_binding", None)
    public_policy_snapshot.pop("provider_sender_id", None)
    template = None
    if record.whatsapp_template_registry_id:
        template = {
            "registry_id": int(record.whatsapp_template_registry_id),
            "variables_present": bool(record.whatsapp_template_variables),
        }
    return {
        "contract_version": DELIVERY_CONTRACT_VERSION,
        "event_id": record.event_id,
        "comment_id": int(record.comment_id),
        "source_model": "MunicipioTicket",
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
        effect_session.query(MunicipioTicketReplyEvent)
        .filter_by(tenant_id=int(tenant_id), ticket_id=int(ticket_id))
        .order_by(
            MunicipioTicketReplyEvent.created_at.asc(),
            MunicipioTicketReplyEvent.id.asc(),
        )
        .all()
    )
    return [serialize_reply_delivery(record, session=effect_session) for record in records]


def _record_provider_message_collision(
    *,
    reply_event_record_id: int,
    tenant_id: int,
    provider_message_id: str,
    provider_sender_id: int,
    source: str,
    delivery_event_id: int | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    message_id_hash = hashlib.sha256(provider_message_id.encode("utf-8")).hexdigest()
    with Session(db.engine) as session:
        with session.begin():
            record = (
                session.query(MunicipioTicketReplyEvent)
                .filter_by(id=int(reply_event_record_id), tenant_id=int(tenant_id))
                .with_for_update()
                .one_or_none()
            )
            if record is None:
                raise TenantTicketReplyDeliveryError(
                    "municipio_ticket_reply_event_not_found"
                )
            current = str(record.whatsapp_delivery_status or "saved").strip().lower()
            if record.whatsapp_status_event_id is None and current in {
                "saved",
                "queued",
                "uncertain",
                "provider_accepted",
            }:
                record.whatsapp_delivery_status = "uncertain"
                record.whatsapp_provider_status = "collision_quarantined"
                record.whatsapp_error_code = "whatsapp_provider_message_id_duplicate"
                record.whatsapp_status_updated_at = now
            session.add(
                AuditEvent(
                    tenant_id=int(tenant_id),
                    event_type=(
                        "municipio_ticket.reply.whatsapp.provider_message_collision"
                    ),
                    resource_type="municipio_ticket_reply_event",
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
                        "error_code": "whatsapp_provider_message_id_duplicate",
                        "automatic_retry_allowed": False,
                        "quarantined": True,
                    },
                )
            )


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
                    session.query(MunicipioTicketReplyEvent)
                    .filter_by(id=int(reply_event_record_id), tenant_id=int(tenant_id))
                    .with_for_update()
                    .one_or_none()
                )
                if record is None:
                    raise TenantTicketReplyDeliveryError(
                        "municipio_ticket_reply_event_not_found"
                    )
                if record.whatsapp_provider_message_id not in (None, "", message_id):
                    raise TenantTicketReplyDeliveryError(
                        "whatsapp_provider_message_id_mismatch"
                    )
                if int(record.whatsapp_provider_sender_id) != int(provider_sender_id):
                    raise TenantTicketReplyDeliveryError(
                        "whatsapp_provider_sender_mismatch"
                    )
                duplicate = (
                    session.query(MunicipioTicketReplyEvent.id)
                    .filter(
                        MunicipioTicketReplyEvent.tenant_id == int(tenant_id),
                        MunicipioTicketReplyEvent.whatsapp_provider_message_id == message_id,
                        MunicipioTicketReplyEvent.id != int(record.id),
                    )
                    .first()
                )
                if duplicate is not None:
                    raise MunicipioTicketReplyProviderMessageCollision()
                current = str(record.whatsapp_delivery_status or "saved").strip().lower()
                record.whatsapp_provider_message_id = message_id
                record.whatsapp_provider_accepted_at = (
                    record.whatsapp_provider_accepted_at or now
                )
                if current in {"saved", "queued", "uncertain", "provider_accepted"}:
                    record.whatsapp_delivery_status = "provider_accepted"
                    record.whatsapp_provider_status = "accepted"
                    record.whatsapp_error_code = None
                    record.whatsapp_status_updated_at = now
    except MunicipioTicketReplyProviderMessageCollision:
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
        raise MunicipioTicketReplyProviderMessageCollision() from exc


def record_provider_uncertainty(
    *,
    reply_event_record_id: int,
    tenant_id: int,
    error_code: str = "provider_acceptance_unknown",
) -> None:
    now = datetime.now(timezone.utc)
    safe_error = str(error_code or "provider_acceptance_unknown").strip()[:80]
    with Session(db.engine) as session:
        with session.begin():
            record = (
                session.query(MunicipioTicketReplyEvent)
                .filter_by(id=int(reply_event_record_id), tenant_id=int(tenant_id))
                .with_for_update()
                .one_or_none()
            )
            if record is None:
                raise TenantTicketReplyDeliveryError(
                    "municipio_ticket_reply_event_not_found"
                )
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
) -> MunicipioTicketReplyEvent | None:
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
    record = (
        MunicipioTicketReplyEvent.query.filter_by(
            id=record_id, tenant_id=int(tenant_id)
        )
        .with_for_update()
        .one_or_none()
    )
    if sender is None or record is None:
        return None
    if int(record.whatsapp_provider_sender_id) != sender_id:
        return None
    if record.whatsapp_provider_message_id not in (None, "", message_id):
        return None
    duplicate = (
        MunicipioTicketReplyEvent.query.filter(
            MunicipioTicketReplyEvent.tenant_id == int(tenant_id),
            MunicipioTicketReplyEvent.whatsapp_provider_message_id == message_id,
            MunicipioTicketReplyEvent.id != int(record.id),
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
        raise MunicipioTicketReplyProviderMessageCollision()

    previous = str(record.whatsapp_delivery_status or "saved").strip().lower()
    if (
        record.whatsapp_status_event_id == status_event_id
        and previous == incoming
        and record.whatsapp_provider_status == provider_status_text
        and record.whatsapp_provider_message_id == message_id
    ):
        return record

    applied = _transition_allowed(previous, incoming)
    now = datetime.now(timezone.utc)
    if applied:
        record.whatsapp_delivery_status = incoming
        record.whatsapp_provider_message_id = message_id
        record.whatsapp_provider_status = provider_status_text
        record.whatsapp_error_code = str(error_code or "").strip()[:80] or None
        record.whatsapp_status_event_id = status_event_id
        record.whatsapp_status_updated_at = now
        if incoming in {"provider_accepted", "delivered", "read"}:
            record.whatsapp_provider_accepted_at = (
                record.whatsapp_provider_accepted_at or now
            )
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
            event_type="municipio_ticket.reply.whatsapp.delivery_callback",
            resource_type="municipio_ticket_reply_event",
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
        raise MunicipioTicketReplyProviderMessageCollision() from exc
    return record


def emit_delivery_invalidation(*, tenant_id: int) -> None:
    from socket_service import emit_ticket_reply_delivery_updated

    emit_ticket_reply_delivery_updated({"tenant_profile_id": int(tenant_id)})


__all__ = [
    "DELIVERY_CONTRACT_VERSION",
    "DELIVERY_REALTIME_CONTRACT_VERSION",
    "MunicipioTicketReplyProviderMessageCollision",
    "emit_delivery_invalidation",
    "list_ticket_reply_deliveries",
    "reconcile_provider_callback",
    "record_provider_acceptance",
    "record_provider_uncertainty",
    "serialize_reply_delivery",
]
