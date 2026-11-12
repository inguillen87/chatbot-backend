"""Typed Meta service replies in the existing WhatsApp durable outbox.

No HTTP ingress, conversation executor, credential installation or sandbox use.
Provider/tenant identity comes from verified server objects and persisted rows.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import copy
import hashlib
import hmac
import json
import math
import re
import uuid
from typing import Any, Mapping

from flask import current_app
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import WhatsAppInboundTurn, WhatsAppOutboundAttempt, db
from services import meta_whatsapp_cloud as cloud
from services.meta_whatsapp_runtime import MetaOutboundRuntime, MetaRuntimeError
from services.meta_whatsapp_webhook import MetaWebhookEvent
from services.meta_whatsapp_media import MetaMediaReference, parse_media_reference, revalidate_media_reference
from services.whatsapp_inbound_durability import resolve_whatsapp_inbound_queue_tenants
from services.whatsapp_provider_policy import (
    WhatsAppProviderPolicyError, meta_provider_isolation_enabled, whatsapp_provider_config,
)
from services.outbox_execution_budget import (
    OutboxExecutionBudgetExceeded, outbox_persistence_operation,
    require_outbox_time_remaining,
)
from services.whatsapp_inbound_turns import (
    InboundTurnClaim, PAYLOAD_SCRUB_CONTRACT_VERSION, dead_whatsapp_outbound_attempt,
    hold_meta_whatsapp_outbound_before_post, meta_whatsapp_outbound_enabled,
    uncertain_whatsapp_outbound_attempt, retry_whatsapp_outbound_attempt,
    derive_whatsapp_stream_key, _validate_session_identity_scope,
)


CONTRACT = "meta.service_reply_outbox.v1"
SOURCE_CONTRACT = "meta.verified_inbound_source.v1"
INBOUND_CONTRACT = "meta.verified_inbound_payload.v1"
RUNTIME_EXTENSION = "chatboc.meta_whatsapp_outbound_runtime"
_EVENT_KEY = re.compile(r"meta:[0-9a-f]{64}\Z")
_PHONE = re.compile(r"[1-9][0-9]{6,14}\Z")
_WINDOW = 24 * 3600
_MAX_BYTES = 64 * 1024
_MAX_MESSAGES = 64


@dataclass(frozen=True)
class MetaReplyContent:
    kind: str
    body: str
    rows: tuple[Mapping[str, str], ...] = ()
    media: MetaMediaReference | None = None


@dataclass(frozen=True)
class MetaReplyBundle:
    """An ordered server result; empty means no automatic provider message."""
    messages: tuple[MetaReplyContent, ...]


@dataclass(frozen=True)
class MetaReplyReceipt:
    outcome: str
    turn_id: str
    outbound_attempt_ids: tuple[str, ...]
    committed: bool


@dataclass(frozen=True)
class MetaDispatchOutcome:
    status: str
    attempt_id: str
    tenant_id: int
    provider_message_sid: str | None = None
    error_code: str | None = None


def _canonical(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise MetaRuntimeError("meta_outbox_payload_invalid") from None
    if len(raw) > _MAX_BYTES:
        raise MetaRuntimeError("meta_outbox_payload_too_large")
    return hashlib.sha256(raw).hexdigest()


def _runtime(runtime=None):
    if runtime is None:
        runtime = current_app.extensions.get(RUNTIME_EXTENSION)
    if runtime is None:
        runtime = MetaOutboundRuntime.from_app()
    if not isinstance(runtime, MetaOutboundRuntime):
        raise MetaRuntimeError("meta_outbound_runtime_invalid")
    return runtime


def _window(source, now):
    timestamp = source.get("timestamp")
    if type(timestamp) is not int or not 0 <= timestamp <= now or now - timestamp >= _WINDOW:
        raise MetaRuntimeError("meta_service_reply_window_invalid")


def _identity(value):
    required = {"tenant_id", "connection_id", "sender_id", "app_id", "waba_id",
                "phone_number_id", "environment"}
    if not isinstance(value, dict) or set(value) != required:
        raise MetaRuntimeError("meta_outbox_identity_invalid")
    if any(type(value[k]) is not int or value[k] <= 0
           for k in ("tenant_id", "connection_id", "sender_id")):
        raise MetaRuntimeError("meta_outbox_identity_invalid")
    for key in ("app_id", "waba_id", "phone_number_id"):
        from services.meta_whatsapp_credentials import meta_id
        meta_id(value[key])
    if value["environment"] != "production":
        raise MetaRuntimeError("meta_outbox_production_required")
    return cloud.MetaSenderSnapshot(**value)


def _source(value, now):
    required = {"event_key", "message_id", "timestamp", "recipient", "content_type", "content_digest"}
    if (not isinstance(value, dict) or set(value) != required
            or not isinstance(value["event_key"], str) or not _EVENT_KEY.fullmatch(value["event_key"])
            or not isinstance(value["message_id"], str) or not cloud._WAMID.fullmatch(value["message_id"])
            or not isinstance(value["recipient"], str) or not _PHONE.fullmatch(value["recipient"])
            or not isinstance(value["content_type"], str)
            or not isinstance(value["content_digest"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["content_digest"])):
        raise MetaRuntimeError("meta_verified_source_invalid")
    _window(value, now)
    return copy.deepcopy(value)


def verified_source_record(event: MetaWebhookEvent, binding, *, now: int):
    """Internal producer input, never proof supplied by an HTTP JSON actor."""
    if (not isinstance(event, MetaWebhookEvent) or event.kind != "message"
            or not isinstance(binding, cloud.VerifiedMetaBinding)
            or event.sender != binding.sender or binding.sender.environment != "production"
            or binding.authority.messaging_permitted is not True
            or binding.authority.webhook_subscribed is not True
            or not binding.authority.observed_at <= now < binding.authority.valid_until
            or now >= binding.credential.expires_at):
        raise MetaRuntimeError("meta_verified_event_binding_invalid")
    source = _source({"event_key": event.event_key, "message_id": event.message_id,
        "timestamp": event.timestamp, "recipient": event.contact,
        "content_type": event.content_type,
        "content_digest": _canonical({"text": event.text, "selection": event.selection,
            "location": event.location, "media_id": event.media_id})}, now)
    return {"contract_version": SOURCE_CONTRACT,
            "identity": asdict(binding.sender), "source": source}


def verified_inbound_payload(event: MetaWebhookEvent, binding, *, now: int):
    """Readable durable input for the future ingress, separate from its reference."""
    record = verified_source_record(event, binding, now=now)
    value = {"contract_version": INBOUND_CONTRACT, "reference": record,
        "input": {"text": event.text, "selection": event.selection,
                  "location": event.location, "media_id": event.media_id}}
    _inbound_reference(value, now)
    return copy.deepcopy(value)


def _inbound_reference(value, now):
    if (not isinstance(value, dict) or set(value) != {"contract_version", "reference", "input"}
            or value.get("contract_version") != INBOUND_CONTRACT):
        raise MetaRuntimeError("meta_inbound_payload_invalid")
    record = value["reference"]
    if (not isinstance(record, dict) or set(record) != {"contract_version", "identity", "source"}
            or record.get("contract_version") != SOURCE_CONTRACT):
        raise MetaRuntimeError("meta_verified_source_invalid")
    _identity(record["identity"])
    source = _source(record["source"], now)
    incoming = value["input"]
    if (not isinstance(incoming, dict)
            or set(incoming) != {"text", "selection", "location", "media_id"}
            or _canonical(incoming) != source["content_digest"]):
        raise MetaRuntimeError("meta_inbound_payload_invalid")
    kind = source["content_type"]
    if kind == "text":
        valid = (isinstance(incoming["text"], str) and 0 < len(incoming["text"]) <= 4096
                 and all(incoming[k] is None for k in ("selection", "location", "media_id")))
    elif kind == "interactive":
        valid = (isinstance(incoming["selection"], str) and 0 < len(incoming["selection"]) <= 256
                 and all(incoming[k] is None for k in ("text", "location", "media_id")))
    elif kind == "location":
        point = incoming["location"]
        valid = (isinstance(point, (tuple, list)) and len(point) == 2
                 and all(type(x) in (int, float) and math.isfinite(x) for x in point)
                 and -90 <= point[0] <= 90 and -180 <= point[1] <= 180
                 and all(incoming[k] is None for k in ("text", "selection", "media_id")))
    else:
        raise MetaRuntimeError("meta_inbound_content_unsupported")
    if not valid:
        raise MetaRuntimeError("meta_inbound_payload_invalid")
    _canonical(value)
    return copy.deepcopy(record)


def _reply_payload(reply: MetaReplyContent, recipient: str):
    if not isinstance(reply, MetaReplyContent) or not isinstance(reply.kind, str):
        raise MetaRuntimeError("meta_reply_content_required")
    if reply.kind == "text" and not reply.rows and reply.media is None:
        return cloud.text_payload(recipient=recipient, body=reply.body)
    if reply.kind == "interactive_list" and reply.media is None:
        return cloud.list_payload(recipient=recipient, body=reply.body,
                                  rows=tuple(copy.deepcopy(reply.rows)))
    if (reply.kind in {"image", "sticker"} and reply.body == "" and not reply.rows
            and isinstance(reply.media, MetaMediaReference)
            and reply.kind == ("sticker" if reply.media.role == "welcome_sticker" else "image")):
        return cloud.media_payload(recipient=recipient, kind=reply.kind, media_id=reply.media.media_id)
    raise MetaRuntimeError("meta_outbound_media_unsupported")


def _reply_messages(reply, recipient):
    if isinstance(reply, MetaReplyContent):
        contents = (reply,)
    elif isinstance(reply, MetaReplyBundle) and type(reply.messages) is tuple:
        contents = reply.messages
    else:
        raise MetaRuntimeError("meta_reply_bundle_required")
    if len(contents) > _MAX_MESSAGES:
        raise MetaRuntimeError("meta_reply_bundle_too_large")
    # Validate every output before any row is staged. No truncation or partial
    # acceptance if a later item is unsupported, oversized or malformed.
    messages = tuple(_reply_payload(item, recipient) for item in contents)
    _canonical(messages)
    return messages


def _durable_outbound_kind(message):
    # The shared outbox stores a coarse media kind. Meta uses the exact
    # image/sticker type from the validated provider envelope when sending.
    kind = message["type"]
    return "media" if kind in {"image", "sticker"} else kind


def _validate_envelope(payload, now):
    required = {"contract_version", "identity", "credential_revision", "source",
                "authorization_kind", "message", "sequence_no", "bundle_size"}
    media_keys = {"media_reference"} if isinstance(payload, dict) and isinstance(payload.get("message"), dict) and isinstance(payload["message"].get("type"), str) and payload["message"]["type"] in {"image", "sticker"} else set()
    canary_keys = {"canary_scope_digest"} if isinstance(payload, dict) and "canary_scope_digest" in payload else set()
    if (not isinstance(payload, dict) or set(payload) != required | media_keys | canary_keys
            or payload.get("contract_version") != CONTRACT
            or payload.get("authorization_kind") != "inbound_service_reply"
            or type(payload.get("credential_revision")) is not int
            or payload["credential_revision"] < 1
            or type(payload.get("sequence_no")) is not int
            or type(payload.get("bundle_size")) is not int
            or not 1 <= payload["sequence_no"] <= payload["bundle_size"] <= _MAX_MESSAGES):
        raise MetaRuntimeError("meta_outbox_contract_invalid")
    identity = _identity(payload["identity"])
    source = _source(payload["source"], now)
    saved_scope = payload.get("canary_scope_digest")
    if canary_keys and (not isinstance(saved_scope, str) or not re.fullmatch(r"[0-9a-f]{64}", saved_scope)):
        raise MetaRuntimeError("meta_outbox_canary_scope_invalid")
    live_scope = _recipient_canary_scope(identity, source, required=bool(canary_keys))
    if live_scope != saved_scope:
        raise MetaRuntimeError("meta_outbox_canary_scope_changed")
    message = payload["message"]
    if not isinstance(message, dict) or message.get("to") != source["recipient"]:
        raise MetaRuntimeError("meta_reply_recipient_mismatch")
    try:
        if message.get("type") == "text":
            safe = cloud.text_payload(recipient=source["recipient"], body=message["text"]["body"])
        elif message.get("type") == "interactive":
            item = message["interactive"]
            sections = item["action"]["sections"]
            if item["type"] != "list" or len(sections) != 1:
                raise ValueError
            safe = cloud.list_payload(recipient=source["recipient"], body=item["body"]["text"],
                rows=tuple(sections[0]["rows"]), button=item["action"]["button"],
                section=sections[0]["title"])
        elif message.get("type") in {"image", "sticker"}:
            reference = parse_media_reference(payload["media_reference"], now=now)
            if ((reference.tenant_id, reference.connection_id, reference.sender_id,
                    reference.app_id, reference.waba_id, reference.phone_number_id)
                    != (identity.tenant_id, identity.connection_id, identity.sender_id,
                        identity.app_id, identity.waba_id, identity.phone_number_id)
                    or message["type"] != ("sticker" if reference.role == "welcome_sticker" else "image")):
                raise MetaRuntimeError("meta_media_scope_invalid")
            safe = cloud.media_payload(recipient=source["recipient"], kind=message["type"], media_id=reference.media_id)
        else:
            raise MetaRuntimeError("meta_outbound_media_unsupported")
        if message != safe:
            raise ValueError
    except (ValueError, KeyError, IndexError, TypeError):
        raise MetaRuntimeError("meta_outbox_message_invalid") from None
    _canonical(payload)
    return identity, source, copy.deepcopy(safe)


def _check_binding(binding, identity, revision):
    if binding.sender != identity or binding.credential.revision != revision:
        raise MetaRuntimeError("meta_outbound_authority_retired")


@contextmanager
def _transaction(session=None):
    owned = session is None
    session = session if session is not None else Session(bind=db.engine, expire_on_commit=False)
    # begin_nested flushes first: never flush unrelated caller WIP implicitly.
    if session.new or session.dirty or session.deleted:
        if owned:
            session.close()
        raise MetaRuntimeError("meta_outbox_session_has_pending_work")
    nested = session.in_transaction()
    try:
        with (session.begin_nested() if nested else session.begin()):
            yield session, owned or not nested
    finally:
        if owned:
            session.close()


def _canary(tenant_id):
    if tenant_id not in resolve_whatsapp_inbound_queue_tenants(current_app.config, provider="meta"):
        raise MetaRuntimeError("meta_outbox_tenant_not_canary")


def _recipient_canary_scope(identity, source, *, required=False):
    """Bind isolated sends to the exact callback authority, including revocation.

    An isolated envelope keeps this requirement even if isolation is later
    disabled. Legacy envelopes retain their original contract only while the
    explicit isolated rollout is off. No phone normalization occurs here.
    """
    config = current_app.config
    try:
        isolated = meta_provider_isolation_enabled(config)
    except WhatsAppProviderPolicyError:
        raise MetaRuntimeError("meta_outbox_canary_scope_invalid") from None
    if not isolated:
        if required:
            raise MetaRuntimeError("meta_outbox_canary_scope_retired")
        return None
    if config.get("META_WHATSAPP_CALLBACK_ENABLED") is not True:
        raise MetaRuntimeError("meta_outbox_canary_scope_retired")
    # Admission imports this module's producer contract. Resolve its pure
    # allowlist parser lazily to avoid a module-initialization dependency cycle.
    from services.meta_whatsapp_batch_admission import (
        MetaBatchAdmissionError, meta_callback_recipient_scope,
    )
    try:
        scope = meta_callback_recipient_scope(config, identity.app_id)
    except MetaBatchAdmissionError:
        raise MetaRuntimeError("meta_outbox_canary_scope_retired") from None
    allowed = scope.get((identity.waba_id, identity.phone_number_id))
    if (allowed is None or allowed[0] != identity.tenant_id
            or source["recipient"] not in allowed[1]):
        raise MetaRuntimeError("meta_outbox_canary_recipient_denied")
    return _canonical({"app_id": identity.app_id, "waba_id": identity.waba_id,
        "phone_number_id": identity.phone_number_id, "tenant_id": identity.tenant_id,
        "contacts": sorted(allowed[1])})


def _completed_meta_source_matches(turn, reference, *, now):
    """Recognize the exact source or the real privacy scrub's full tombstone.

    A marker alone never authorizes sending. Caller still validates envelope,
    current binding/window/credential, native outbox scope, sequence and lease.
    """
    if not isinstance(reference, dict):
        return False
    identity, source = reference.get("identity"), reference.get("source")
    result = turn.result_json if isinstance(turn.result_json, dict) else {}
    if (not isinstance(identity, dict) or not isinstance(source, dict)
            or reference.get("contract_version") != SOURCE_CONTRACT
            or turn.provider != "meta" or turn.status != WhatsAppInboundTurn.STATUS_COMPLETED
            or (turn.tenant_id, turn.provider_connection_id, turn.provider_sender_id)
            != (identity.get("tenant_id"), identity.get("connection_id"), identity.get("sender_id"))
            or turn.provider_message_sid != source.get("message_id")
            or turn.message_kind != source.get("content_type")
            or result.get("contract_version") != CONTRACT):
        return False
    source_digest = _canonical(reference)
    if result.get("meta_source_digest") != source_digest:
        return False
    if turn.payload_json == reference:
        return True
    payload = turn.payload_json
    if not isinstance(payload, dict) or set(payload) != {"_chatboc_payload_scrub"}:
        return False
    marker = payload["_chatboc_payload_scrub"]
    keys = {"contract_version", "payload_digest_sha256", "message_kind", "original_bytes",
            "original_fields", "reason", "retained_payload", "scrubbed_at"}
    if (not isinstance(marker, dict) or set(marker) != keys
            or marker.get("contract_version") != PAYLOAD_SCRUB_CONTRACT_VERSION
            or marker.get("reason") != "completed" or marker.get("retained_payload") is not False
            or marker.get("message_kind") != turn.message_kind
            or type(marker.get("original_fields")) is not int or marker["original_fields"] != len(reference)
            or type(marker.get("original_bytes")) is not int):
        return False
    encoded = json.dumps(reference, ensure_ascii=False, sort_keys=True, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    if marker["original_bytes"] != len(encoded):
        return False
    original = turn.payload_digest
    if (not isinstance(original, str) or not re.fullmatch(r"[0-9a-f]{64}", original)
            or marker.get("payload_digest_sha256") != original):
        return False
    # New completions preserve only the original hash, never readable input.
    # Older convenience rows can prove it directly from the SOURCEreference.
    if "meta_inbound_payload_digest" in result:
        if result["meta_inbound_payload_digest"] != original:
            return False
    elif original != source_digest:
        return False
    stamp = marker.get("scrubbed_at")
    if not isinstance(stamp, str) or not 1 <= len(stamp) <= 64:
        return False
    try:
        parsed = datetime.fromisoformat(stamp)
        if (parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0
                or parsed.isoformat() != stamp or not isinstance(turn.completed_at, datetime)):
            return False
        completed = turn.completed_at
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=timezone.utc)
        # Runtime authority clocks are integer seconds; a real scrub can carry
        # microseconds in that same observed second. No future second is valid.
        if not completed.timestamp() <= parsed.timestamp() or int(parsed.timestamp()) > now:
            return False
    except (ValueError, TypeError, OverflowError):
        return False
    return True


def _stage_on_turn(session, turn, source_record, reply, runtime, *, claim=None, binding=None):
    """Shared finalizer for the event convenience API and a future claimed inbox."""
    identity = _identity(source_record.get("identity"))
    source = _source(source_record.get("source"), runtime.now())
    if (set(source_record) != {"contract_version", "identity", "source"}
            or source_record.get("contract_version") != SOURCE_CONTRACT or turn.provider != "meta"
            or (turn.tenant_id, turn.provider_connection_id, turn.provider_sender_id)
            != (identity.tenant_id, identity.connection_id, identity.sender_id)
            or turn.provider_message_sid != source["message_id"]):
        raise MetaRuntimeError("meta_turn_binding_mismatch")
    _canary(identity.tenant_id)
    canary_scope = _recipient_canary_scope(identity, source)
    binding = binding or runtime.load_binding(identity, session=session)
    if binding.sender != identity:
        raise MetaRuntimeError("meta_turn_binding_mismatch")
    messages = _reply_messages(reply, source["recipient"])
    contents = (reply,) if isinstance(reply, MetaReplyContent) else reply.messages
    envelopes = []
    for sequence_no, message in enumerate(messages, start=1):
        envelope = {"contract_version": CONTRACT, "identity": asdict(identity),
            "credential_revision": binding.credential.revision, "source": source,
            "authorization_kind": "inbound_service_reply", "message": message,
            "sequence_no": sequence_no, "bundle_size": len(messages)}
        if canary_scope is not None:
            envelope["canary_scope_digest"] = canary_scope
        reference = contents[sequence_no - 1].media
        if reference is not None:
            reference = revalidate_media_reference(reference, identity, session=session, now=runtime.now())
            envelope["media_reference"] = asdict(reference)
        _validate_envelope(envelope, runtime.now())
        envelopes.append(envelope)
    semantic = _canonical({"source_record": source_record, "messages": messages})
    if turn.status == WhatsAppInboundTurn.STATUS_COMPLETED:
        if ((turn.result_json or {}).get("meta_reply_digest") != semantic
                or not _completed_meta_source_matches(turn, source_record, now=runtime.now())):
            raise MetaRuntimeError("meta_reply_event_conflict")
        rows = session.scalars(select(WhatsAppOutboundAttempt).where(
            WhatsAppOutboundAttempt.inbound_turn_id == turn.id,
            WhatsAppOutboundAttempt.tenant_id == turn.tenant_id,
            WhatsAppOutboundAttempt.provider == "meta").order_by(WhatsAppOutboundAttempt.sequence_no)).all()
        if len(rows) != len(envelopes):
            raise MetaRuntimeError("meta_reply_receipt_unavailable")
        for index, (row, envelope) in enumerate(zip(rows, envelopes), start=1):
            # Delivery/lease state may evolve; the original scope and payload
            # must remain identical for a duplicate calculated reply.
            if (row.sequence_no != index or row.payload_json != envelope
                    or row.message_kind != _durable_outbound_kind(envelope["message"])
                    or row.payload_digest != _canonical(envelope)
                    or (row.provider_connection_id, row.provider_sender_id)
                    != (identity.connection_id, identity.sender_id)):
                raise MetaRuntimeError("meta_reply_event_conflict")
        return "duplicate", tuple(row.attempt_id for row in rows)
    if claim is not None:
        if (turn.status != WhatsAppInboundTurn.STATUS_PROCESSING
                or turn.lease_token != claim.lease_token or turn.turn_id != claim.turn_id
                or claim.provider != "meta" or claim.tenant_id != turn.tenant_id
                or turn.leased_until is None
                or _epoch(turn.leased_until) <= runtime.now()):
            raise MetaRuntimeError("meta_turn_lease_not_owned")
        _validate_session_identity_scope(session, tenant_id=turn.tenant_id, provider="meta",
            binding_id=turn.session_identity_binding_id, identity_version=turn.session_identity_version,
            identity_hmac=turn.session_identity_hmac, chat_session_id=turn.chat_session_id)
    elif turn.status != WhatsAppInboundTurn.STATUS_RECEIVED:
        raise MetaRuntimeError("meta_turn_lease_required")
    now = datetime.fromtimestamp(runtime.now(), timezone.utc)
    attempts = []
    for sequence_no, envelope in enumerate(envelopes, start=1):
        attempt = WhatsAppOutboundAttempt(attempt_id=str(uuid.uuid4()), tenant_id=turn.tenant_id,
            inbound_turn_id=turn.id, provider_connection_id=turn.provider_connection_id,
            provider_sender_id=turn.provider_sender_id, provider=turn.provider,
            stream_key=turn.stream_key, sequence_no=sequence_no,
            message_kind=_durable_outbound_kind(envelope["message"]),
            idempotency_key="meta-service-reply:" + turn.turn_id + ":" + str(sequence_no),
            payload_digest=_canonical(envelope), payload_json=copy.deepcopy(envelope),
            status=WhatsAppOutboundAttempt.STATUS_PENDING, provider_status="unknown",
            attempt_count=0, max_attempts=5, available_at=now,
            contract_version=WhatsAppOutboundAttempt.CONTRACT_VERSION, created_at=now, updated_at=now)
        session.add(attempt)
        attempts.append(attempt)
    turn.status = WhatsAppInboundTurn.STATUS_COMPLETED
    turn.completed_at = now
    turn.lease_token = turn.leased_until = None
    turn.last_error_code = None
    turn.result_json = {"contract_version": CONTRACT, "meta_reply_digest": semantic,
                        "meta_source_digest": _canonical(source_record),
                        "meta_inbound_payload_digest": turn.payload_digest,
                        "reply_staged": bool(messages), "outbound_count": len(messages),
                        "delivery_verified": False}
    # Only the bounded source reference remains; never retain the incoming body.
    turn.payload_json = copy.deepcopy(source_record)
    session.add(turn)
    session.flush()
    return "created", tuple(attempt.attempt_id for attempt in attempts)


def produce_meta_service_reply(event: MetaWebhookEvent, reply: MetaReplyContent | MetaReplyBundle, *, runtime=None, session=None):
    """Atomically record an already calculated reply, not a bot/executor completion."""
    runtime = _runtime(runtime)
    if not isinstance(event, MetaWebhookEvent):
        raise MetaRuntimeError("meta_verified_event_required")
    _canary(event.sender.tenant_id)
    with _transaction(session) as (active, committed):
        binding = runtime.load_binding(event.sender, session=active)
        source_record = verified_source_record(event, binding, now=runtime.now())
        query = select(WhatsAppInboundTurn).where(
            WhatsAppInboundTurn.tenant_id == binding.sender.tenant_id,
            WhatsAppInboundTurn.provider == "meta",
            WhatsAppInboundTurn.provider_message_sid == event.message_id).with_for_update()
        turn = active.scalar(query)
        def new_turn():
            now = datetime.fromtimestamp(runtime.now(), timezone.utc)
            return WhatsAppInboundTurn(turn_id=str(uuid.uuid4()), tenant_id=binding.sender.tenant_id,
                provider_connection_id=binding.sender.connection_id, provider_sender_id=binding.sender.sender_id,
                provider="meta", provider_message_sid=event.message_id,
                stream_key=derive_whatsapp_stream_key(secret=whatsapp_provider_config(current_app.config, "meta")["WHATSAPP_INBOUND_HASH_SECRET"],
                    tenant_id=binding.sender.tenant_id, sender=event.contact,
                    destination=f"meta:{binding.sender.app_id}:{binding.sender.waba_id}:{binding.sender.phone_number_id}"),
                message_kind=event.content_type, payload_digest=_canonical(source_record),
                payload_json=source_record, status=WhatsAppInboundTurn.STATUS_RECEIVED,
                attempt_count=0, max_attempts=8, received_at=now, available_at=now,
                contract_version=WhatsAppInboundTurn.CONTRACT_VERSION, created_at=now, updated_at=now)
        if turn is None:
            # The UNIQUE race is recovered only after rolling back our savepoint.
            # Do not rollback a caller's transaction or attempt a second INSERT.
            try:
                with active.begin_nested():
                    turn = new_turn()
                    active.add(turn)
                    active.flush()
                    outcome, ids = _stage_on_turn(active, turn, source_record, reply, runtime,
                                                 binding=binding)
            except IntegrityError as exc:
                original = getattr(exc, "orig", None)
                unique = (getattr(original, "sqlite_errorcode", None) == 2067
                          or getattr(original, "sqlstate", None) == "23505"
                          or getattr(original, "pgcode", None) == "23505")
                if not unique:
                    raise MetaRuntimeError("meta_reply_storage_conflict") from None
                active.expire_all()
                turn = active.scalar(query)
                if turn is None or turn.status != WhatsAppInboundTurn.STATUS_COMPLETED:
                    raise MetaRuntimeError("meta_reply_receipt_unavailable", transient=True) from None
                outcome, ids = _stage_on_turn(active, turn, source_record, reply, runtime,
                                             binding=binding)
        elif turn.status != WhatsAppInboundTurn.STATUS_COMPLETED:
            raise MetaRuntimeError("meta_turn_lease_required")
        else:
            outcome, ids = _stage_on_turn(active, turn, source_record, reply, runtime, binding=binding)
        receipt = MetaReplyReceipt(outcome, turn.turn_id, ids, committed)
    return receipt


def complete_meta_service_reply(claim: InboundTurnClaim, reply: MetaReplyContent | MetaReplyBundle, *, runtime=None, session=None):
    """Future ingress/executor seam: finish the SAME persisted, leased Meta turn."""
    runtime = _runtime(runtime)
    if not isinstance(claim, InboundTurnClaim) or claim.provider != "meta":
        raise MetaRuntimeError("meta_turn_claim_required")
    with _transaction(session) as (active, committed):
        turn = active.scalar(select(WhatsAppInboundTurn).where(
            WhatsAppInboundTurn.turn_id == claim.turn_id,
            WhatsAppInboundTurn.tenant_id == claim.tenant_id,
            WhatsAppInboundTurn.provider == "meta",
            WhatsAppInboundTurn.status == WhatsAppInboundTurn.STATUS_PROCESSING,
            WhatsAppInboundTurn.lease_token == claim.lease_token).with_for_update())
        if turn is None:
            raise MetaRuntimeError("meta_turn_lease_not_owned")
        if (turn.payload_json != claim.payload or turn.payload_digest != claim.payload_digest
                or _canonical(turn.payload_json) != turn.payload_digest):
            raise MetaRuntimeError("meta_turn_payload_changed")
        record = _inbound_reference(copy.deepcopy(turn.payload_json), runtime.now())
        outcome, ids = _stage_on_turn(active, turn, record, reply, runtime, claim=claim)
        receipt = MetaReplyReceipt(outcome, turn.turn_id, ids, committed)
    return receipt


def _epoch(value):
    return int((value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).timestamp())


def _claim_envelope(claim, now):
    if claim.provider != "meta":
        raise MetaRuntimeError("meta_outbox_provider_mismatch")
    with Session(bind=db.engine) as active:
        row = active.scalar(select(WhatsAppOutboundAttempt).where(
            WhatsAppOutboundAttempt.attempt_id == claim.attempt_id,
            WhatsAppOutboundAttempt.tenant_id == claim.tenant_id,
            WhatsAppOutboundAttempt.provider == "meta",
            WhatsAppOutboundAttempt.status == WhatsAppOutboundAttempt.STATUS_SENDING,
            WhatsAppOutboundAttempt.lease_token == claim.lease_token))
        if row is None or row.leased_until is None or _epoch(row.leased_until) <= now:
            raise MetaRuntimeError("meta_outbox_lease_not_owned")
        payload = copy.deepcopy(row.payload_json)
        if (_canonical(payload) != row.payload_digest or row.payload_digest != claim.payload_digest
                or payload != claim.payload):
            raise MetaRuntimeError("meta_outbox_payload_changed")
        identity, source, message = _validate_envelope(payload, now)
        if row.message_kind != _durable_outbound_kind(message) or claim.message_kind != row.message_kind:
            raise MetaRuntimeError("meta_outbox_message_kind_mismatch")
        if "media_reference" in payload:
            revalidate_media_reference(payload["media_reference"], identity, session=active, now=now)
        _canary(identity.tenant_id)
        if row.sequence_no != payload["sequence_no"] or claim.sequence_no != row.sequence_no:
            raise MetaRuntimeError("meta_outbox_sequence_mismatch")
        turn = active.get(WhatsAppInboundTurn, row.inbound_turn_id)
        if (turn is None or turn.provider != "meta" or turn.status != WhatsAppInboundTurn.STATUS_COMPLETED
                or turn.tenant_id != identity.tenant_id
                or (row.tenant_id, row.provider_connection_id, row.provider_sender_id)
                != (identity.tenant_id, identity.connection_id, identity.sender_id)
                or (turn.provider_connection_id, turn.provider_sender_id)
                != (identity.connection_id, identity.sender_id)
                or (turn.result_json or {}).get("outbound_count") != payload["bundle_size"]
                or not _completed_meta_source_matches(turn,
                    {"contract_version": SOURCE_CONTRACT, "identity": asdict(identity), "source": source}, now=now)):
            raise MetaRuntimeError("meta_outbox_turn_scope_mismatch")
    return payload, identity, source, message


@outbox_persistence_operation
def _accept_meta_attempt(claim, message_id):
    if not isinstance(message_id, str) or not cloud._WAMID.fullmatch(message_id):
        raise MetaRuntimeError("meta_provider_ack_invalid")
    now = datetime.now(timezone.utc)
    with Session(bind=db.engine) as active:
        result = active.execute(update(WhatsAppOutboundAttempt).where(
            WhatsAppOutboundAttempt.attempt_id == claim.attempt_id,
            WhatsAppOutboundAttempt.tenant_id == claim.tenant_id,
            WhatsAppOutboundAttempt.provider == "meta",
            WhatsAppOutboundAttempt.status == WhatsAppOutboundAttempt.STATUS_SENDING,
            WhatsAppOutboundAttempt.lease_token == claim.lease_token).values(
                status=WhatsAppOutboundAttempt.STATUS_ACCEPTED, provider_message_sid=message_id,
                provider_status="accepted", lease_token=None, leased_until=None,
                accepted_at=now, completed_at=now, updated_at=now,
                last_error_code=None, last_error_digest=None))
        if result.rowcount != 1:
            active.rollback()
            return False
        active.commit()
        return True


def dispatch_meta_outbound_claim(claim, *, on_post_started, runtime=None):
    """The worker must pass its own hook so outer DB failures retain uncertainty."""
    if not callable(on_post_started):
        raise MetaRuntimeError("meta_outbound_post_hook_required")

    def hold_disabled():
        try:
            released = hold_meta_whatsapp_outbound_before_post(claim)
            status = "held" if released else "lost_lease"
        except Exception:
            # A hold commit might have succeeded. Do not enter worker retry/dead
            # recovery; the lease can become uncertain without any new send.
            status = "hold_uncertain"
        return MetaDispatchOutcome(status, claim.attempt_id, claim.tenant_id,
                                   error_code="meta_outbound_disabled")

    if not meta_whatsapp_outbound_enabled():
        return hold_disabled()
    runtime = _runtime(runtime)
    started = False
    try:
        envelope, identity, source, message = _claim_envelope(claim, runtime.now())
        def loader():
            with Session(bind=db.engine) as active:
                binding = runtime.load_binding(identity, session=active)
            _check_binding(binding, identity, envelope["credential_revision"])
            return binding
        # Resolve remote authority before send_once captures its reference time.
        # Its loader then rechecks persisted local authority without backdating.
        observed = loader()
        if not meta_whatsapp_outbound_enabled():
            return hold_disabled()
        def binding_loader():
            with Session(bind=db.engine) as active:
                return runtime.recheck_local_binding(observed, session=active)
        def policy(sender, payload):
            if sender != identity or payload != message:
                return False
            _claim_envelope(claim, runtime.now())
            from services.whatsapp_enterprise_rules import WhatsAppEnterpriseRulesService
            allowed, _reason = WhatsAppEnterpriseRulesService(identity.tenant_id).reserve_outbound(
                body=message.get("text", {}).get("body") or message.get("interactive", {}).get("body", {}).get("text", ""),
                reservation_key=claim.attempt_id,
                metadata={"recipient": source["recipient"], "provider_sender_id": identity.sender_id,
                          "within_24h_window": True, "is_template": False},
                provider="meta", provider_connection_id=identity.connection_id,
                provider_sender_id=identity.sender_id, source=CONTRACT)
            return allowed is True
        pre_post_failures = []
        def post(url, **kwargs):
            nonlocal started
            # A final local read is still not a provider/DB compare-and-swap.
            # Authority can change after I/O begins; that request cannot be undone.
            try:
                require_outbox_time_remaining()
                _claim_envelope(claim, runtime.now())
                with Session(bind=db.engine) as active:
                    runtime.recheck_local_binding(observed, session=active)
                if not meta_whatsapp_outbound_enabled():
                    raise MetaRuntimeError("meta_outbound_disabled")
            except OutboxExecutionBudgetExceeded:
                failure = MetaRuntimeError("meta_outbound_budget_exhausted", transient=True)
                pre_post_failures.append(failure)
                raise failure from None
            except Exception as exc:
                failure = exc if isinstance(exc, MetaRuntimeError) else MetaRuntimeError(
                    "meta_outbound_authority_retired")
                pre_post_failures.append(failure)
                raise failure from None
            started = True
            on_post_started()
            return runtime.post("POST", url, **kwargs)
        result = cloud.send_once(binding_loader=binding_loader, payload=message,
            now=runtime.now(), policy_check=policy, post=post)
        if result.state == "accepted":
            if _accept_meta_attempt(claim, result.message_id):
                return MetaDispatchOutcome("accepted", claim.attempt_id, claim.tenant_id, result.message_id)
            saved = uncertain_whatsapp_outbound_attempt(claim.attempt_id, claim.lease_token,
                                                        "meta_provider_ack_not_persisted")
            return MetaDispatchOutcome("send_uncertain" if saved else "lost_lease", claim.attempt_id, claim.tenant_id,
                                       error_code="meta_provider_ack_not_persisted")
        if result.state == "rejected" and type(result.error_code) is int and result.error_code > 0:
            state = dead_whatsapp_outbound_attempt(claim.attempt_id, claim.lease_token,
                                                  "meta_provider_rejected")
            return MetaDispatchOutcome(state or "lost_lease", claim.attempt_id, claim.tenant_id,
                                       error_code="meta_provider_rejected")
        # 4xx {} / malformed error has no demonstrable provider rejection.
        if started:
            saved = uncertain_whatsapp_outbound_attempt(claim.attempt_id, claim.lease_token,
                                                        "meta_provider_ack_uncertain")
            return MetaDispatchOutcome("send_uncertain" if saved else "lost_lease", claim.attempt_id, claim.tenant_id,
                                       error_code="meta_provider_ack_uncertain")
        if pre_post_failures:
            raise pre_post_failures[0]
        raise MetaRuntimeError("meta_provider_call_not_started")
    except Exception as exc:
        # Outer worker's same hook is already true if this persistence fails.
        # Never expose raw provider/DB errors in the new Meta surface.
        if started:
            saved = uncertain_whatsapp_outbound_attempt(claim.attempt_id, claim.lease_token,
                                                        "meta_outbound_processing_uncertain")
            return MetaDispatchOutcome("send_uncertain" if saved else "lost_lease", claim.attempt_id, claim.tenant_id,
                                       error_code="meta_outbound_processing_uncertain")
        if ((isinstance(exc, MetaRuntimeError) and exc.code == "meta_outbound_disabled")
                or not meta_whatsapp_outbound_enabled()):
            return hold_disabled()
        transient = isinstance(exc, OutboxExecutionBudgetExceeded) or (isinstance(exc, MetaRuntimeError) and exc.transient)
        transition = retry_whatsapp_outbound_attempt if transient else dead_whatsapp_outbound_attempt
        code = ("meta_outbound_budget_exhausted" if isinstance(exc, OutboxExecutionBudgetExceeded)
                else exc.code if isinstance(exc, MetaRuntimeError) else "meta_outbound_precondition_failed")
        state = transition(claim.attempt_id, claim.lease_token, code)
        return MetaDispatchOutcome(state or "lost_lease", claim.attempt_id, claim.tenant_id, error_code=code)
