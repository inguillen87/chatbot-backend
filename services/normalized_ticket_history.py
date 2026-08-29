from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from flask import current_app
from sqlalchemy import and_, func, or_

from extensions import db
from models import AuditEvent, Conversation, Message, TenantTicket, TenantTicketReplyEvent
from utils.time_utils import datetime_to_iso_utc


NORMALIZED_TICKET_HISTORY_CONTRACT_VERSION = "tenant_ticket.normalized_history.v2"
NORMALIZED_TICKET_HISTORY_CURSOR_VERSION = 2
DEFAULT_NORMALIZED_TICKET_HISTORY_LIMIT = 50
MAX_NORMALIZED_TICKET_HISTORY_LIMIT = 100
MAX_LEGACY_FALLBACK_COMMENTS = 100

_SOURCE_RANK = {
    "ticket_attachment": 1,
    "legacy_fallback": 2,
    "audit_event": 3,
    "conversation_message": 4,
    "reply_event": 5,
}


class NormalizedTicketHistoryError(ValueError):
    """Base error for the normalized TenantTicket history reader."""


class NormalizedTicketHistoryCursorError(NormalizedTicketHistoryError):
    """Raised when a cursor is malformed, forged, or bound to another view."""


class NormalizedTicketHistoryNotFound(LookupError):
    """Raised when the ticket does not belong to the requested tenant."""


@dataclass(frozen=True)
class _CursorBoundary:
    created_at: datetime
    source_rank: int
    source_id: int

    @property
    def sort_key(self) -> tuple[datetime, int, int]:
        return self.created_at, self.source_rank, self.source_id


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        return _utc(datetime.fromisoformat(raw))
    except ValueError:
        return None


def _cursor_timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _cursor_secret() -> bytes:
    raw = current_app.config.get("TICKET_HISTORY_CURSOR_SECRET") or current_app.config.get("SECRET_KEY")
    if isinstance(raw, bytes):
        secret = raw
    else:
        secret = str(raw or "").encode("utf-8")
    if not secret:
        raise RuntimeError("ticket_history_cursor_secret_missing")
    return secret


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(f"{value}{'=' * (-len(value) % 4)}")


def _encode_cursor(
    boundary: _CursorBoundary,
    *,
    tenant_id: int,
    ticket_id: int,
    include_internal: bool,
) -> str:
    payload = {
        "v": NORMALIZED_TICKET_HISTORY_CURSOR_VERSION,
        "tenant_id": tenant_id,
        "ticket_id": ticket_id,
        "scope": "internal" if include_internal else "public",
        "ts": _cursor_timestamp(boundary.created_at),
        "rank": boundary.source_rank,
        "id": boundary.source_id,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(_cursor_secret(), raw, hashlib.sha256).digest()
    return f"{_b64encode(raw)}.{_b64encode(signature)}"


def _decode_cursor(
    cursor: str,
    *,
    tenant_id: int,
    ticket_id: int,
    include_internal: bool,
) -> _CursorBoundary:
    if not isinstance(cursor, str) or not cursor.strip() or len(cursor) > 2048:
        raise NormalizedTicketHistoryCursorError("cursor invalido")
    try:
        encoded_payload, encoded_signature = cursor.split(".", 1)
        raw = _b64decode(encoded_payload)
        supplied_signature = _b64decode(encoded_signature)
        expected_signature = hmac.new(_cursor_secret(), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise NormalizedTicketHistoryCursorError("cursor invalido")
        payload = json.loads(raw.decode("utf-8"))
    except NormalizedTicketHistoryCursorError:
        raise
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise NormalizedTicketHistoryCursorError("cursor invalido") from exc

    expected_scope = "internal" if include_internal else "public"
    if (
        not isinstance(payload, dict)
        or payload.get("v") != NORMALIZED_TICKET_HISTORY_CURSOR_VERSION
        or payload.get("tenant_id") != tenant_id
        or payload.get("ticket_id") != ticket_id
        or payload.get("scope") != expected_scope
    ):
        raise NormalizedTicketHistoryCursorError("cursor invalido")

    created_at = _parse_timestamp(payload.get("ts"))
    source_rank = payload.get("rank")
    source_id = payload.get("id")
    if (
        created_at is None
        or not isinstance(source_rank, int)
        or source_rank not in _SOURCE_RANK.values()
        or not isinstance(source_id, int)
        or source_id < 1
    ):
        raise NormalizedTicketHistoryCursorError("cursor invalido")
    return _CursorBoundary(created_at, source_rank, source_id)


def _validated_limit(limit: Any) -> int:
    try:
        normalized = int(limit)
    except (TypeError, ValueError) as exc:
        raise NormalizedTicketHistoryError("limit debe ser un entero") from exc
    if normalized < 1 or normalized > MAX_NORMALIZED_TICKET_HISTORY_LIMIT:
        raise NormalizedTicketHistoryError(
            f"limit debe estar entre 1 y {MAX_NORMALIZED_TICKET_HISTORY_LIMIT}"
        )
    return normalized


def _stable_fallback_id(item: Mapping[str, Any]) -> int:
    explicit = item.get("id") or item.get("event_id") or item.get("comment_id")
    if explicit not in (None, ""):
        identity: dict[str, Any] = {"explicit_id": str(explicit)}
    else:
        attachment = item.get("attachmentInfo") or item.get("attachment_info") or item.get("source_attachment")
        attachment_identity = None
        if isinstance(attachment, Mapping):
            attachment_identity = {
                "id": attachment.get("id"),
                "url": attachment.get("url"),
                "name": attachment.get("name") or attachment.get("filename"),
            }
        actor = item.get("actor") if isinstance(item.get("actor"), Mapping) else {}
        identity = {
            "created_at": item.get("created_at") or item.get("timestamp") or item.get("fecha"),
            "body": item.get("body") or item.get("texto") or item.get("comentario"),
            "visibility": item.get("visibility") or "public",
            "origin": item.get("origin"),
            "action": item.get("action"),
            "actor": {
                "id": actor.get("id") or item.get("author_user_id"),
                "name": actor.get("name"),
                "role": actor.get("role"),
            },
            "attachment": attachment_identity,
        }
    fingerprint = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    # 60 bits remain positive on every supported SQL/Python platform and are
    # stable even when the public identifier is a UUID.
    return int(hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:15], 16) + 1


def _apply_descending_boundary(query, created_at_column, id_column, *, source_rank: int, boundary: _CursorBoundary | None):
    if boundary is None:
        return query
    timestamp = boundary.created_at
    if source_rank < boundary.source_rank:
        condition = or_(created_at_column < timestamp, created_at_column == timestamp)
    elif source_rank > boundary.source_rank:
        condition = created_at_column < timestamp
    else:
        condition = or_(
            created_at_column < timestamp,
            and_(created_at_column == timestamp, id_column < boundary.source_id),
        )
    return query.filter(condition)


def _candidate(
    *,
    source: str,
    source_id: int,
    created_at: datetime,
    payload: dict[str, Any],
    dedupe_keys: Iterable[str],
) -> dict[str, Any]:
    return {
        **payload,
        "source": source,
        "created_at": datetime_to_iso_utc(created_at),
        "_created_at": _utc(created_at),
        "_source_rank": _SOURCE_RANK[source],
        "_source_id": source_id,
        "_dedupe_keys": tuple(key for key in dedupe_keys if key),
    }


def _attachment_dedupe_keys(attachment: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(attachment, Mapping):
        return ()
    keys: list[str] = []
    for key in ("id", "url", "name", "filename", "original_filename"):
        value = str(attachment.get(key) or "").strip()
        if value:
            normalized_key = "name" if key in {"filename", "original_filename"} else key
            candidate_key = f"attachment:{normalized_key}:{value.casefold()}"
            if candidate_key not in keys:
                keys.append(candidate_key)
    return tuple(keys)


def _stable_attachment_id(attachment: Mapping[str, Any]) -> int:
    identity = {
        "id": attachment.get("id"),
        "url": attachment.get("url"),
        "name": attachment.get("name") or attachment.get("filename") or attachment.get("original_filename"),
    }
    fingerprint = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return int(hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:15], 16) + 1


def _attachment_candidates(
    ticket: TenantTicket,
    *,
    limit: int,
    boundary: _CursorBoundary | None,
) -> tuple[list[dict[str, Any]], bool]:
    # Runtime import avoids coupling the normalized ledger to ticket mutation
    # services while reusing the canonical attachment serializer.
    from services.v2.ticket_service import ticket_attachment_payloads

    created_at = _utc(ticket.created_at)
    candidates: list[dict[str, Any]] = []
    for attachment in ticket_attachment_payloads(ticket):
        if not isinstance(attachment, Mapping):
            continue
        source_id = _stable_attachment_id(attachment)
        if boundary is not None and (
            created_at,
            _SOURCE_RANK["ticket_attachment"],
            source_id,
        ) >= boundary.sort_key:
            continue
        attachment_payload = dict(attachment)
        attachment_name = (
            attachment_payload.get("name")
            or attachment_payload.get("filename")
            or attachment_payload.get("original_filename")
            or "archivo"
        )
        candidates.append(
            _candidate(
                source="ticket_attachment",
                source_id=source_id,
                created_at=created_at,
                payload={
                    "id": f"attachment:{source_id}",
                    "event_id": None,
                    "event_type": "ticket.attachment_received",
                    "body": f"Adjunto recibido: {attachment_name}",
                    "visibility": "public",
                    "sender_type": "user",
                    "sender_user_id": ticket.user_id,
                    "attachmentInfo": attachment_payload,
                    "attachments": [attachment_payload],
                },
                dedupe_keys=(
                    f"ticket-attachment-row:{source_id}",
                    *_attachment_dedupe_keys(attachment_payload),
                ),
            )
        )
    candidates.sort(key=_sort_key, reverse=True)
    return candidates[: limit + 1], len(candidates) > limit


def _reply_candidates(
    *,
    tenant_id: int,
    ticket_id: int,
    include_internal: bool,
    limit: int,
    boundary: _CursorBoundary | None,
) -> tuple[list[dict[str, Any]], bool]:
    query = TenantTicketReplyEvent.query.filter_by(tenant_id=tenant_id, ticket_id=ticket_id)
    if not include_internal:
        query = query.filter(TenantTicketReplyEvent.visibility == "public")
    query = _apply_descending_boundary(
        query,
        TenantTicketReplyEvent.created_at,
        TenantTicketReplyEvent.id,
        source_rank=_SOURCE_RANK["reply_event"],
        boundary=boundary,
    )
    rows = (
        query.order_by(TenantTicketReplyEvent.created_at.desc(), TenantTicketReplyEvent.id.desc())
        .limit(limit + 1)
        .all()
    )
    candidates = [
        _candidate(
            source="reply_event",
            source_id=row.id,
            created_at=row.created_at,
            payload={
                "id": f"reply:{row.id}",
                "event_id": row.event_id,
                "event_type": "ticket.reply",
                "body": row.body,
                "visibility": row.visibility,
                "actor": {
                    "id": row.actor_user_id,
                    "name": row.actor_name,
                    "role": row.actor_role,
                },
            },
            dedupe_keys=(f"event:{row.event_id}", f"reply-row:{row.id}"),
        )
        for row in rows
    ]
    return candidates, len(rows) == limit + 1


def _message_candidates(
    *,
    tenant_id: int,
    conversation_id: str | None,
    include_internal: bool,
    limit: int,
    boundary: _CursorBoundary | None,
) -> tuple[list[dict[str, Any]], bool]:
    if conversation_id is None:
        return [], False
    query = Message.query.filter_by(tenant_id=tenant_id, conversation_id=conversation_id)
    if not include_internal:
        visibility = func.lower(func.coalesce(Message.meta_payload["visibility"].as_string(), "public"))
        query = query.filter(visibility == "public")
    query = _apply_descending_boundary(
        query,
        Message.created_at,
        Message.id,
        source_rank=_SOURCE_RANK["conversation_message"],
        boundary=boundary,
    )
    rows = query.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit + 1).all()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        metadata = row.meta_payload if isinstance(row.meta_payload, Mapping) else {}
        visibility = str(metadata.get("visibility") or "public").strip().lower()
        event_id = str(metadata.get("event_id") or metadata.get("reply_event_id") or "").strip()
        payload: dict[str, Any] = {
            "id": f"message:{row.id}",
            "event_id": event_id or None,
            "event_type": "conversation.message",
            "body": row.body,
            "visibility": "internal" if visibility == "internal" else "public",
            "direction": row.direction,
            "sender_type": row.sender_type,
            "sender_user_id": row.sender_user_id,
            "conversation_id": row.conversation_id,
            "channel_session_id": row.channel_session_id,
        }
        attachment = metadata.get("attachmentInfo") or metadata.get("attachment_info")
        if isinstance(attachment, Mapping):
            payload["attachmentInfo"] = dict(attachment)
            payload["attachments"] = [dict(attachment)]
        if include_internal:
            payload["metadata"] = dict(metadata)
        dedupe_keys = [f"message-row:{row.id}"]
        if event_id:
            dedupe_keys.append(f"event:{event_id}")
        dedupe_keys.extend(_attachment_dedupe_keys(attachment if isinstance(attachment, Mapping) else None))
        candidates.append(
            _candidate(
                source="conversation_message",
                source_id=row.id,
                created_at=row.created_at,
                payload=payload,
                dedupe_keys=dedupe_keys,
            )
        )
    return candidates, len(rows) == limit + 1


def _audit_candidates(
    *,
    tenant_id: int,
    ticket_id: int,
    include_internal: bool,
    limit: int,
    boundary: _CursorBoundary | None,
) -> tuple[list[dict[str, Any]], bool]:
    # Audit details are operational by default.  They are never exposed merely
    # because a producer happened to omit a visibility field.
    if not include_internal:
        return [], False
    query = AuditEvent.query.filter_by(
        tenant_id=tenant_id,
        resource_type="tenant_ticket",
        resource_id=str(ticket_id),
    )
    query = _apply_descending_boundary(
        query,
        AuditEvent.created_at,
        AuditEvent.id,
        source_rank=_SOURCE_RANK["audit_event"],
        boundary=boundary,
    )
    rows = query.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(limit + 1).all()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        details = row.details if isinstance(row.details, Mapping) else {}
        event_id = str(details.get("event_id") or "").strip()
        dedupe_keys = [f"audit-row:{row.id}"]
        if event_id:
            dedupe_keys.append(f"event:{event_id}")
        candidates.append(
            _candidate(
                source="audit_event",
                source_id=row.id,
                created_at=row.created_at,
                payload={
                    "id": f"audit:{row.id}",
                    "event_id": event_id or None,
                    "event_type": row.event_type,
                    "body": details.get("body") or details.get("message") or row.event_type,
                    "visibility": "internal",
                    "actor": {"id": row.actor_user_id},
                    "resource_type": row.resource_type,
                    "resource_id": row.resource_id,
                    "details": dict(details),
                },
                dedupe_keys=dedupe_keys,
            )
        )
    return candidates, len(rows) == limit + 1


def _fallback_candidates(
    ticket: TenantTicket,
    *,
    include_internal: bool,
    limit: int,
    boundary: _CursorBoundary | None,
) -> tuple[list[dict[str, Any]], bool, dict[str, int]]:
    extra = ticket.datos_extra if isinstance(ticket.datos_extra, Mapping) else {}
    raw_comments = extra.get("comments") if isinstance(extra.get("comments"), list) else []
    valid_comments = [(index, item) for index, item in enumerate(raw_comments) if isinstance(item, Mapping)]
    bounded = valid_comments[-MAX_LEGACY_FALLBACK_COMMENTS:]
    canonical_event_ids = {
        str(item.get("event_id") or item.get("id") or item.get("comment_id"))
        for _, item in bounded
        if str(item.get("origin") or "").strip().lower() == "admin_panel"
        and str(item.get("action") or "").strip().lower() == "reply"
        and (item.get("event_id") or item.get("id") or item.get("comment_id")) not in (None, "")
    }
    persisted_event_ids: set[str] = set()
    if canonical_event_ids:
        # The fallback is capped at 100, so this anti-join remains bounded. It
        # prevents a canonical reply delivered on page N from reappearing as a
        # legacy JSON row on page N+1 after the reply keyset boundary advances.
        persisted_event_ids = {
            str(value)
            for (value,) in (
                db.session.query(TenantTicketReplyEvent.event_id)
                .filter(
                    TenantTicketReplyEvent.tenant_id == ticket.tenant_id,
                    TenantTicketReplyEvent.ticket_id == ticket.id,
                    TenantTicketReplyEvent.event_id.in_(canonical_event_ids),
                )
                .all()
            )
        }
    candidates: list[dict[str, Any]] = []
    for _original_index, item in bounded:
        visibility = str(item.get("visibility") or "public").strip().lower()
        if not include_internal and visibility != "public":
            continue
        created_at = _parse_timestamp(item.get("created_at") or item.get("timestamp") or item.get("fecha"))
        if created_at is None:
            continue
        source_id = _stable_fallback_id(item)
        if boundary is not None and (created_at, _SOURCE_RANK["legacy_fallback"], source_id) >= boundary.sort_key:
            continue
        explicit_id = item.get("event_id") or item.get("id") or item.get("comment_id")
        origin = str(item.get("origin") or "").strip().lower()
        action = str(item.get("action") or "").strip().lower()
        event_key = (
            f"event:{explicit_id}"
            if explicit_id not in (None, "") and origin == "admin_panel" and action == "reply"
            else ""
        )
        if event_key and str(explicit_id) in persisted_event_ids:
            continue
        payload: dict[str, Any] = {
            "id": f"legacy:{explicit_id if explicit_id not in (None, '') else source_id}",
            "event_id": str(explicit_id) if event_key else None,
            "event_type": item.get("event_type") or ("ticket.reply" if action == "reply" else "ticket.comment"),
            "body": item.get("body") or item.get("texto") or item.get("comentario") or "",
            "visibility": "internal" if visibility == "internal" else "public",
            "actor": dict(item.get("actor")) if isinstance(item.get("actor"), Mapping) else None,
            "legacy_fallback": True,
        }
        attachment = item.get("attachmentInfo") or item.get("attachment_info") or item.get("source_attachment")
        if isinstance(attachment, Mapping):
            payload["attachmentInfo"] = dict(attachment)
            payload["attachments"] = [dict(attachment)]
        candidates.append(
            _candidate(
                source="legacy_fallback",
                source_id=source_id,
                created_at=created_at,
                payload=payload,
                dedupe_keys=(
                    event_key,
                    f"legacy-row:{source_id}",
                    *_attachment_dedupe_keys(attachment if isinstance(attachment, Mapping) else None),
                ),
            )
        )
    candidates.sort(key=_sort_key, reverse=True)
    selected = candidates[: limit + 1]
    stats = {
        "available": len(valid_comments),
        "considered": len(bounded),
        "discarded_by_bound": max(0, len(valid_comments) - len(bounded)),
    }
    return selected, len(candidates) > limit, stats


def _sort_key(item: Mapping[str, Any]) -> tuple[datetime, int, int]:
    return item["_created_at"], item["_source_rank"], item["_source_id"]


def _deduplicate(candidates: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    # Canonical persisted sources win over JSON compatibility rows regardless
    # of input order.  The final chronological order is applied afterwards.
    canonical_first = sorted(
        candidates,
        key=lambda item: (item["_source_rank"], item["_created_at"], item["_source_id"]),
        reverse=True,
    )
    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    for item in canonical_first:
        keys = set(item.get("_dedupe_keys") or ())
        if keys and keys.intersection(seen):
            continue
        seen.update(keys)
        selected.append(item)
    return sorted(selected, key=_sort_key, reverse=True)


def _public_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if not key.startswith("_")}


def read_normalized_ticket_history(
    *,
    tenant_id: int,
    ticket_id: int,
    include_internal: bool,
    limit: int = DEFAULT_NORMALIZED_TICKET_HISTORY_LIMIT,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Read one tenant-scoped history page from normalized SQL sources.

    Each persisted origin is keyset-filtered in SQL and reads at most
    ``limit + 1`` rows.  ``TenantTicket.datos_extra.comments`` is considered
    only as a bounded compatibility source; it is never the canonical reply
    ledger.  Conversation messages are included only when the ticket stores an
    explicit conversation id that resolves inside the same tenant.
    """

    normalized_tenant_id = int(tenant_id)
    normalized_ticket_id = int(ticket_id)
    normalized_limit = _validated_limit(limit)
    ticket = TenantTicket.query.filter_by(
        id=normalized_ticket_id,
        tenant_id=normalized_tenant_id,
    ).one_or_none()
    if ticket is None:
        raise NormalizedTicketHistoryNotFound("ticket no encontrado")

    boundary = (
        _decode_cursor(
            cursor,
            tenant_id=normalized_tenant_id,
            ticket_id=normalized_ticket_id,
            include_internal=include_internal,
        )
        if cursor
        else None
    )

    extra = ticket.datos_extra if isinstance(ticket.datos_extra, Mapping) else {}
    requested_conversation_id = str(extra.get("conversation_id") or "").strip() or None
    linked_conversation_id: str | None = None
    if requested_conversation_id:
        linked = Conversation.query.filter_by(
            id=requested_conversation_id,
            tenant_id=normalized_tenant_id,
        ).one_or_none()
        if linked is not None:
            linked_conversation_id = linked.id

    replies, replies_more = _reply_candidates(
        tenant_id=normalized_tenant_id,
        ticket_id=normalized_ticket_id,
        include_internal=include_internal,
        limit=normalized_limit,
        boundary=boundary,
    )
    messages, messages_more = _message_candidates(
        tenant_id=normalized_tenant_id,
        conversation_id=linked_conversation_id,
        include_internal=include_internal,
        limit=normalized_limit,
        boundary=boundary,
    )
    audits, audits_more = _audit_candidates(
        tenant_id=normalized_tenant_id,
        ticket_id=normalized_ticket_id,
        include_internal=include_internal,
        limit=normalized_limit,
        boundary=boundary,
    )
    attachments, attachments_more = _attachment_candidates(
        ticket,
        limit=normalized_limit,
        boundary=boundary,
    )
    fallback, fallback_more, fallback_stats = _fallback_candidates(
        ticket,
        include_internal=include_internal,
        limit=normalized_limit,
        boundary=boundary,
    )

    merged = _deduplicate([*replies, *messages, *audits, *fallback, *attachments])
    page_desc = merged[:normalized_limit]
    source_has_more = replies_more or messages_more or audits_more or fallback_more or attachments_more
    has_more = len(merged) > normalized_limit or source_has_more
    next_cursor = None
    if has_more and page_desc:
        oldest = page_desc[-1]
        next_cursor = _encode_cursor(
            _CursorBoundary(oldest["_created_at"], oldest["_source_rank"], oldest["_source_id"]),
            tenant_id=normalized_tenant_id,
            ticket_id=normalized_ticket_id,
            include_internal=include_internal,
        )

    # UI timelines consume pages in chronological order; the cursor still
    # points to the oldest delivered item so clients can prepend older pages.
    items = [_public_item(item) for item in reversed(page_desc)]
    return {
        "contract_version": NORMALIZED_TICKET_HISTORY_CONTRACT_VERSION,
        "items": items,
        "pagination": {
            "direction": "older",
            "order": "chronological_asc",
            "limit": normalized_limit,
            "returned_count": len(items),
            "has_more": has_more,
            "next_cursor": next_cursor,
        },
        "sources": {
            "reply_event": {"fetched": len(replies), "truncated": replies_more},
            "conversation_message": {
                "fetched": len(messages),
                "truncated": messages_more,
                "requested_conversation_id": requested_conversation_id,
                "linked_conversation_id": linked_conversation_id,
            },
            "audit_event": {"fetched": len(audits), "truncated": audits_more},
            "ticket_attachment": {
                "fetched": len(attachments),
                "truncated": attachments_more,
            },
            "legacy_fallback": {
                "fetched": len(fallback),
                "truncated": fallback_more,
                **fallback_stats,
            },
        },
    }


__all__ = [
    "DEFAULT_NORMALIZED_TICKET_HISTORY_LIMIT",
    "MAX_LEGACY_FALLBACK_COMMENTS",
    "MAX_NORMALIZED_TICKET_HISTORY_LIMIT",
    "NORMALIZED_TICKET_HISTORY_CONTRACT_VERSION",
    "NormalizedTicketHistoryCursorError",
    "NormalizedTicketHistoryError",
    "NormalizedTicketHistoryNotFound",
    "read_normalized_ticket_history",
]
