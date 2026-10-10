"""Durable human-handoff ledger for legacy municipal claims.

The normalized event is authoritative for idempotency.  The legacy
``MunicipioTicket.datos_extra.handoff`` object and ``TicketComentario`` row are
updated in the same transaction so existing inbox readers remain compatible.
No provider dispatch is performed from this service.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified

from extensions import db
from models import (
    AuditEvent,
    MunicipioTicket,
    MunicipioTicketHandoffEvent,
    TicketComentario,
    User,
)

CONTRACT_VERSION = MunicipioTicketHandoffEvent.CONTRACT_VERSION
PROJECTION_CONTRACT_VERSION = MunicipioTicketHandoffEvent.PROJECTION_CONTRACT_VERSION
SUPPORTED_CHANNELS = {"operator", "live_chat", "phone"}
_TERMINAL_PROJECTION_STATES = {
    "resolved",
    "cancelled",
    "canceled",
    "expired",
    "rejected",
}
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
_MAX_REASON_LENGTH = 500


class MunicipioTicketHandoffError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        reason_code: str,
        action_hint: str,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.reason_code = reason_code
        self.action_hint = action_hint


@dataclass(frozen=True)
class MunicipioTicketHandoffResult:
    ticket: MunicipioTicket
    event: MunicipioTicketHandoffEvent
    comment: TicketComentario
    replayed: bool


def _error(
    message: str,
    *,
    status_code: int = 400,
    reason_code: str,
    action_hint: str,
) -> MunicipioTicketHandoffError:
    return MunicipioTicketHandoffError(
        message,
        status_code=status_code,
        reason_code=reason_code,
        action_hint=action_hint,
    )


def _normalize_text(value: Any, *, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise _error(
            f"{field} es requerido y debe ser texto.",
            reason_code=f"handoff_{field}_required",
            action_hint=f"send_handoff_{field}",
        )
    normalized = unicodedata.normalize(
        "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
    ).strip()
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _error(
            f"{field} contiene caracteres no validos.",
            reason_code=f"handoff_{field}_invalid",
            action_hint=f"send_valid_handoff_{field}",
        ) from exc
    if not normalized:
        raise _error(
            f"{field} es requerido.",
            reason_code=f"handoff_{field}_required",
            action_hint=f"send_handoff_{field}",
        )
    if "\x00" in normalized or len(normalized) > max_length:
        raise _error(
            f"{field} no tiene un formato valido.",
            reason_code=f"handoff_{field}_invalid",
            action_hint=f"send_valid_handoff_{field}",
        )
    return normalized


def normalize_idempotency_key(value: Any) -> str:
    if not isinstance(value, str):
        raise _error(
            "Idempotency-Key es requerido para derivar el reclamo.",
            reason_code="handoff_idempotency_key_required",
            action_hint="send_idempotency_key_header",
        )
    normalized = value.strip()
    if not normalized:
        raise _error(
            "Idempotency-Key es requerido para derivar el reclamo.",
            reason_code="handoff_idempotency_key_required",
            action_hint="send_idempotency_key_header",
        )
    if not _IDEMPOTENCY_KEY_PATTERN.fullmatch(normalized):
        raise _error(
            "Idempotency-Key no tiene un formato valido.",
            reason_code="handoff_idempotency_key_invalid",
            action_hint="send_stable_idempotency_key_header",
        )
    return normalized


def normalize_channel(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(
            "channel es requerido para derivar el reclamo.",
            reason_code="handoff_channel_required",
            action_hint="choose_supported_handoff_channel",
        )
    channel = value.strip().lower()
    channel = {
        "agent": "operator",
        "human": "operator",
        "humano": "operator",
        "operador": "operator",
        "livechat": "live_chat",
    }.get(channel, channel)
    if channel not in SUPPORTED_CHANNELS:
        raise _error(
            "El canal de handoff no es valido.",
            reason_code="invalid_handoff_channel",
            action_hint="choose_supported_handoff_channel",
        )
    return channel


def normalize_reason(value: Any) -> str:
    return _normalize_text(value, field="reason", max_length=_MAX_REASON_LENGTH)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request_digest(
    *,
    tenant_id: int,
    ticket_id: int,
    actor_user_id: int,
    channel: str,
    reason: str,
) -> str:
    canonical = json.dumps(
        {
            "action": "handoff",
            "actor_user_id": int(actor_user_id),
            "channel": channel,
            "contract_version": CONTRACT_VERSION,
            "reason": reason,
            "source_model": "MunicipioTicket",
            "tenant_id": int(tenant_id),
            "ticket_id": int(ticket_id),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return _sha256(canonical)


def _projection_state(extra: Mapping[str, Any]) -> str:
    handoff = (
        extra.get("handoff") if isinstance(extra.get("handoff"), Mapping) else None
    )
    if not handoff:
        return "idle"
    status = str(handoff.get("status") or "").strip().lower()
    if not status or status in _TERMINAL_PROJECTION_STATES:
        return "idle"
    return status


def _validate_existing(
    event: MunicipioTicketHandoffEvent,
    *,
    request_digest: str,
    ticket_id: int,
) -> None:
    if (
        event.request_digest != request_digest
        or event.source_model != "MunicipioTicket"
        or int(event.ticket_id) != int(ticket_id)
        or event.action != "handoff"
    ):
        raise _error(
            "La Idempotency-Key ya fue usada con otro payload.",
            status_code=409,
            reason_code="handoff_idempotency_payload_conflict",
            action_hint="reuse_key_only_for_identical_payload",
        )


def _result_for_event(
    event: MunicipioTicketHandoffEvent,
    *,
    replayed: bool,
) -> MunicipioTicketHandoffResult:
    ticket = db.session.get(MunicipioTicket, event.ticket_id)
    comment = db.session.get(TicketComentario, event.comment_id)
    if ticket is None or comment is None:
        raise _error(
            "El receipt de handoff existe pero su proyeccion no esta disponible.",
            status_code=409,
            reason_code="handoff_idempotency_replay_unavailable",
            action_hint="refresh_inbox",
        )
    return MunicipioTicketHandoffResult(
        ticket=ticket,
        event=event,
        comment=comment,
        replayed=replayed,
    )


def request_human_handoff(
    *,
    ticket: MunicipioTicket,
    actor: User,
    tenant_id: int,
    raw_idempotency_key: Any,
    raw_channel: Any,
    raw_reason: Any,
    occurred_at: datetime | None = None,
) -> MunicipioTicketHandoffResult:
    """Create or replay one municipal handoff without external dispatch."""

    key = normalize_idempotency_key(raw_idempotency_key)
    channel = normalize_channel(raw_channel)
    reason = normalize_reason(raw_reason)
    key_hash = _sha256(key)
    digest = _request_digest(
        tenant_id=tenant_id,
        ticket_id=ticket.id,
        actor_user_id=actor.id,
        channel=channel,
        reason=reason,
    )

    existing = MunicipioTicketHandoffEvent.query.filter_by(
        tenant_id=int(tenant_id), idempotency_key_hash=key_hash
    ).one_or_none()
    if existing is not None:
        _validate_existing(existing, request_digest=digest, ticket_id=ticket.id)
        return _result_for_event(existing, replayed=True)

    extra = (
        deepcopy(ticket.datos_extra) if isinstance(ticket.datos_extra, Mapping) else {}
    )
    if _projection_state(extra) != "idle":
        raise _error(
            "La transicion de handoff no es valida para el estado actual.",
            status_code=409,
            reason_code="invalid_handoff_transition",
            action_hint="refresh_inbox",
        )

    now = occurred_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_iso = now.astimezone(timezone.utc).isoformat()
    event_id = f"mhe_{uuid.uuid4().hex}"
    actor_ref = {"id": actor.id, "name": actor.name}
    projection = {
        "contract_version": PROJECTION_CONTRACT_VERSION,
        "event_id": event_id,
        "ledger_contract_version": CONTRACT_VERSION,
        "channel": channel,
        "status": "requested",
        "requested_at": now_iso,
        "requested_by": actor_ref,
        "reason": reason,
    }

    comment = TicketComentario(
        municipio_ticket_id=ticket.id,
        comentario=f"Handoff solicitado al equipo ({channel})",
        user_id=actor.id,
        es_admin=True,
        origen="internal",
        estado_ticket="handoff",
    )
    db.session.add(comment)
    db.session.flush()

    event = MunicipioTicketHandoffEvent(
        tenant_id=int(tenant_id),
        source_model="MunicipioTicket",
        ticket_id=ticket.id,
        comment_id=comment.id,
        event_id=event_id,
        action="handoff",
        status="requested",
        channel=channel,
        reason=reason,
        actor_user_id=actor.id,
        previous_assignee_user_id=ticket.asignado_a_id,
        idempotency_key_hash=key_hash,
        request_digest=digest,
        projection_contract_version=PROJECTION_CONTRACT_VERSION,
        contract_version=CONTRACT_VERSION,
        created_at=now,
    )
    extra["handoff"] = projection
    ticket.datos_extra = extra
    flag_modified(ticket, "datos_extra")
    if str(ticket.estado or "").strip().lower() in {"nuevo", "open"}:
        ticket.estado = "en_proceso"
    ticket.ultima_actividad = now
    db.session.add(ticket)
    db.session.add(event)
    # Deliberately omit reason, actor name, contact details and the raw key.
    db.session.add(
        AuditEvent(
            tenant_id=int(tenant_id),
            actor_user_id=actor.id,
            event_type="municipio_ticket.handoff.requested",
            resource_type="municipio_ticket",
            resource_id=str(ticket.id),
            details={
                "contract_version": CONTRACT_VERSION,
                "event_id": event_id,
                "source_model": "MunicipioTicket",
                "status": "requested",
                "channel": channel,
                "external_dispatch": False,
                "reason_present": True,
                "raw_idempotency_key_persisted": False,
            },
        )
    )

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        collision = MunicipioTicketHandoffEvent.query.filter_by(
            tenant_id=int(tenant_id), idempotency_key_hash=key_hash
        ).one_or_none()
        if collision is None:
            raise
        _validate_existing(collision, request_digest=digest, ticket_id=ticket.id)
        return _result_for_event(collision, replayed=True)

    return _result_for_event(event, replayed=False)


def list_handoff_events(
    *,
    tenant_id: int,
    ticket_id: int,
    limit: int = 100,
) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(int(limit), 100))
    rows = (
        MunicipioTicketHandoffEvent.query.filter_by(
            tenant_id=int(tenant_id),
            source_model="MunicipioTicket",
            ticket_id=int(ticket_id),
        )
        .order_by(
            MunicipioTicketHandoffEvent.created_at.desc(),
            MunicipioTicketHandoffEvent.id.desc(),
        )
        .limit(bounded_limit)
        .all()
    )
    return [row.to_event_dict() for row in reversed(rows)]


__all__ = [
    "CONTRACT_VERSION",
    "MunicipioTicketHandoffError",
    "MunicipioTicketHandoffResult",
    "list_handoff_events",
    "normalize_channel",
    "normalize_idempotency_key",
    "normalize_reason",
    "request_human_handoff",
]
