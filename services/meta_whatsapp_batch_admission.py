"""Unwired RAW Meta admission in the caller's one outer transaction.

The caller supplies the current concrete db.session and owns COMMIT/ROLLBACK.
This port returns only committed=False; it is not an HTTP ACK or an executor.
All Graph GETs and whole-batch syntax validation precede database locks/DML.
Any failure poisons this outer transaction for COMMIT until its owner rolls back.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import copy
import hashlib
import hmac
import json
import math
import re
import uuid
from typing import Mapping

from sqlalchemy import event as sql_events, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, SessionTransaction

from database import db
from models import ProviderConnection, ProviderSender, TenantProfile
from models import WhatsAppInboundTurn, WhatsAppOutboundAttempt
from services import meta_whatsapp_cloud as cloud
from services.channel_session_identity import stage_meta_channel_session_identity
from services.meta_whatsapp_credentials import meta_id
from services.meta_whatsapp_runtime import APP_SECRETS_KEY, MetaOutboundRuntime
from services.meta_whatsapp_webhook import _unique_pairs, parse_webhook, verify_signature
from services.tenant_provider_credentials import ProviderCredentialError
from services.meta_whatsapp_outbox import (
    CONTRACT as OUTBOX_CONTRACT, INBOUND_CONTRACT, SOURCE_CONTRACT, _canonical,
    _completed_meta_source_matches, verified_inbound_payload,
)
from services.whatsapp_inbound_durability import resolve_whatsapp_inbound_queue_tenants
from services.whatsapp_provider_policy import whatsapp_provider_config
from services.whatsapp_inbound_turns import (
    PAYLOAD_SCRUB_CONTRACT_VERSION, derive_whatsapp_stream_key,
)


CONTRACT = "meta.raw_batch_admission.v1"
_PHONE = re.compile(r"[1-9][0-9]{7,14}\Z")
_SUPPORTED = frozenset({"text", "interactive", "location"})
_PROGRESS = {"accepted": 0, "sent": 1, "delivered": 2, "read": 3}
_STATE_KEY = "chatboc.meta_raw_batch_admission.rollback_required.v1"
_HOOK_KEY = "chatboc.meta_raw_batch_admission.guard_installed.v1"
CANARY_RECIPIENTS_KEY = "META_WHATSAPP_CALLBACK_CANARY_RECIPIENTS_JSON"
_CANARY_FIELDS = {"app_id", "waba_id", "phone_number_id", "tenant_id", "contact"}


class MetaBatchAdmissionError(RuntimeError):
    """Finite codes only, never raw payload/credential/provider/SQL text."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def meta_callback_recipient_scope(config, app_id):
    """Exact app/WABA/phone/tenant to WhatsApp contact allowlist for a canary."""
    raw = config.get(CANARY_RECIPIENTS_KEY)
    if not isinstance(raw, str) or not 1 <= len(raw) <= 16 * 1024:
        raise MetaBatchAdmissionError("meta_batch_canary_scope_unavailable")
    try:
        rows = json.loads(raw, object_pairs_hook=_unique_pairs)
        if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
            raise ValueError
        bindings = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != _CANARY_FIELDS:
                raise ValueError
            key = tuple(meta_id(row[field]) for field in ("app_id", "waba_id", "phone_number_id"))
            tenant_id, contact = row["tenant_id"], row["contact"]
            if (type(tenant_id) is not int or not 1 <= tenant_id <= 2_147_483_647
                    or not isinstance(contact, str) or not _PHONE.fullmatch(contact)):
                raise ValueError
            configured_tenant, contacts = bindings.setdefault(key, (tenant_id, set()))
            if configured_tenant != tenant_id or contact in contacts or len(contacts) >= 5:
                raise ValueError
            contacts.add(contact)
        scope = {(waba, phone): (tenant, frozenset(contacts))
                 for (configured_app, waba, phone), (tenant, contacts) in bindings.items()
                 if configured_app == app_id}
        if scope:
            return scope
    except (ValueError, RecursionError, ProviderCredentialError):
        pass
    raise MetaBatchAdmissionError("meta_batch_canary_scope_unavailable")


@dataclass(frozen=True)
class MetaBatchEventReceipt:
    event_key: str
    kind: str
    outcome: str
    tenant_id: int
    record_id: int


@dataclass(frozen=True)
class MetaBatchStaging:
    contract_version: str
    events: tuple[MetaBatchEventReceipt, ...]
    committed: bool = False


def _require_transaction(session, transaction, *, clean=False):
    if not isinstance(session, Session) or session is not db.session():
        raise MetaBatchAdmissionError("meta_batch_session_mismatch")
    if (not isinstance(transaction, SessionTransaction) or transaction.nested
            or transaction is not session.get_transaction() or not transaction.is_active
            or session.in_nested_transaction()):
        raise MetaBatchAdmissionError("meta_batch_outer_transaction_required")
    if session.info.get(_STATE_KEY) is transaction:
        raise MetaBatchAdmissionError("meta_batch_owner_rollback_required")
    if clean and (session.new or session.dirty or session.deleted):
        raise MetaBatchAdmissionError("meta_batch_pending_caller_work")
    connection = session.connection()
    if (connection.dialect.name == "sqlite"
            and connection.connection.driver_connection.in_transaction is not True):
        raise MetaBatchAdmissionError("meta_batch_physical_transaction_required")


def _install_commit_guard(session):
    # Listeners are installed once on this Session, not on the global class.
    # No mutation of SQLAlchemy's listener list from inside an event callback.
    if session.info.get(_HOOK_KEY):
        return
    def before_commit(active):
        if active.info.get(_STATE_KEY) is active.get_transaction():
            raise MetaBatchAdmissionError("meta_batch_owner_rollback_required")
    def after_transaction_end(active, ended):
        if active.info.get(_STATE_KEY) is ended:
            active.info.pop(_STATE_KEY, None)
    sql_events.listen(session, "before_commit", before_commit)
    sql_events.listen(session, "after_transaction_end", after_transaction_end)
    session.info[_HOOK_KEY] = True


def _projection(item, binding, now):
    """Existing readable schema, with no grant/renewal of an outbound window.

    New rows additionally pass the official producer at the CURRENT clock.
    Old exact duplicates can be compared after 24h without backdating that
    producer. Unsupported media/opaque contact identity is an explicit HOLD.
    """
    if (item.kind != "message" or item.status is not None or item.error_codes
            or item.sender != binding.sender or item.sender.environment != "production"
            or item.contact_identity is not None or item.content_type not in _SUPPORTED
            or not cloud._WAMID.fullmatch(item.message_id)
            or not _PHONE.fullmatch(item.contact)
            or type(item.timestamp) is not int or not 0 <= item.timestamp <= now
            or any(value is not None for value in
                   (item.media_id, item.media_mime_type, item.media_sha256))):
        raise MetaBatchAdmissionError("meta_batch_message_unsupported")
    incoming = {"text": item.text, "selection": item.selection,
                "location": item.location, "media_id": item.media_id}
    if item.content_type == "text":
        valid = (isinstance(item.text, str) and bool(item.text.strip())
                 and len(item.text) <= 4096 and item.selection is None and item.location is None)
    elif item.content_type == "interactive":
        valid = (isinstance(item.selection, str) and 1 <= len(item.selection) <= 256
                 and item.text is None and item.location is None)
    else:
        point = item.location
        valid = (isinstance(point, (tuple, list)) and len(point) == 2
                 and all(type(n) in (int, float) and math.isfinite(n) for n in point)
                 and -90 <= point[0] <= 90 and -180 <= point[1] <= 180
                 and item.text is None and item.selection is None)
    if not valid:
        raise MetaBatchAdmissionError("meta_batch_message_content_invalid")
    incoming = json.loads(json.dumps(incoming, ensure_ascii=False, allow_nan=False))
    material = [item.sender.tenant_id, item.sender.connection_id,
                item.sender.sender_id, "message", item.message_id, None]
    expected = "meta:" + hashlib.sha256(json.dumps(material, separators=(",", ":")).encode()).hexdigest()
    if not hmac.compare_digest(item.event_key, expected):
        raise MetaBatchAdmissionError("meta_batch_event_key_mismatch")
    reference = {"contract_version": SOURCE_CONTRACT, "identity": asdict(item.sender),
        "source": {"event_key": item.event_key, "message_id": item.message_id,
            "timestamp": item.timestamp, "recipient": item.contact,
            "content_type": item.content_type, "content_digest": _canonical(incoming)}}
    result = {"contract_version": INBOUND_CONTRACT, "reference": reference, "input": incoming}
    _canonical(result)
    return result


def _stream(config, item):
    config = whatsapp_provider_config(config, "meta")
    secret = config.get("WHATSAPP_INBOUND_HASH_SECRET")
    if not isinstance(secret, str) or len(secret.encode()) < 32:
        raise MetaBatchAdmissionError("meta_batch_stream_secret_missing")
    return derive_whatsapp_stream_key(secret=secret, tenant_id=item.sender.tenant_id,
        sender=item.contact, destination=f"meta:{item.sender.app_id}:{item.sender.waba_id}:{item.sender.phone_number_id}")


def _dead_scrub_matches(row, payload, now):
    stored = row.payload_json
    if row.status != "dead" or not isinstance(stored, dict) or set(stored) != {"_chatboc_payload_scrub"}:
        return False
    marker = stored["_chatboc_payload_scrub"]
    keys = {"contract_version", "payload_digest_sha256", "message_kind", "original_bytes",
            "original_fields", "reason", "retained_payload", "scrubbed_at"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False,
                         separators=(",", ":")).encode()
    if (not isinstance(marker, dict) or set(marker) != keys
            or marker.get("contract_version") != PAYLOAD_SCRUB_CONTRACT_VERSION
            or marker.get("reason") != "dead_retention_expired" or marker.get("retained_payload") is not False
            or marker.get("payload_digest_sha256") != _canonical(payload)
            or marker.get("message_kind") != row.message_kind
            or type(marker.get("original_bytes")) is not int or marker["original_bytes"] != len(encoded)
            or type(marker.get("original_fields")) is not int or marker["original_fields"] != len(payload)):
        return False
    stamp = marker.get("scrubbed_at")
    try:
        parsed = datetime.fromisoformat(stamp)
        completed = row.completed_at
        if completed is not None and completed.tzinfo is None:
            completed = completed.replace(tzinfo=timezone.utc)
        return bool(parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0
            and parsed.isoformat() == stamp and completed is not None
            and completed.timestamp() <= parsed.timestamp() and int(parsed.timestamp()) <= now)
    except (ValueError, TypeError, OverflowError, AttributeError):
        return False


def _duplicate(row, item, payload, stream, now):
    if ((row.tenant_id, row.provider_connection_id, row.provider_sender_id, row.provider)
            != (item.sender.tenant_id, item.sender.connection_id, item.sender.sender_id, "meta")
            or row.provider_message_sid != item.message_id or row.stream_key != stream
            or row.message_kind != item.content_type):
        raise MetaBatchAdmissionError("meta_batch_duplicate_scope_conflict")
    digest = _canonical(payload)
    if row.payload_digest == digest and row.payload_json == payload:
        return
    reference = payload["reference"]
    if row.status == "completed" and _completed_meta_source_matches(row, reference, now=now):
        # Full-input originals require the exact original digest. Convenience
        # legacy SOURCE-only rows can prove only the SOURCE, never new identity.
        result = row.result_json or {}
        if (row.payload_digest == digest or
                ("meta_inbound_payload_digest" not in result and row.payload_digest == _canonical(reference))):
            return
    if row.payload_digest == digest and _dead_scrub_matches(row, payload, now):
        return
    raise MetaBatchAdmissionError("meta_batch_duplicate_payload_conflict")


def _message_row(session, item, *, lock):
    statement = select(WhatsAppInboundTurn).where(
        WhatsAppInboundTurn.tenant_id == item.sender.tenant_id,
        WhatsAppInboundTurn.provider == "meta",
        WhatsAppInboundTurn.provider_message_sid == item.message_id)
    if lock:
        statement = statement.with_for_update()
    return session.scalar(statement.execution_options(populate_existing=True, autoflush=False))


def _status_row(session, item, *, lock):
    sender = item.sender
    if (item.kind != "status" or item.status not in {"sent", "delivered", "read", "failed"}
            or not _PHONE.fullmatch(item.contact) or item.contact_identity is not None
            or type(item.timestamp) is not int or item.timestamp < 0):
        raise MetaBatchAdmissionError("meta_batch_status_unsupported")
    statement = select(WhatsAppOutboundAttempt).where(
        WhatsAppOutboundAttempt.tenant_id == sender.tenant_id,
        WhatsAppOutboundAttempt.provider == "meta",
        WhatsAppOutboundAttempt.provider_connection_id == sender.connection_id,
        WhatsAppOutboundAttempt.provider_sender_id == sender.sender_id,
        WhatsAppOutboundAttempt.provider_message_sid == item.message_id).order_by(WhatsAppOutboundAttempt.id).limit(2)
    if lock:
        statement = statement.with_for_update()
    rows = session.scalars(statement.execution_options(populate_existing=True, autoflush=False)).all()
    if len(rows) != 1:
        raise MetaBatchAdmissionError("meta_batch_status_wamid_unknown_or_ambiguous")
    row = rows[0]
    envelope = row.payload_json
    parent = session.get(WhatsAppInboundTurn, row.inbound_turn_id, populate_existing=True)
    if (row.status not in {"accepted", "failed"} or row.lease_token is not None
            or row.leased_until is not None or row.provider_status not in {*_PROGRESS, "failed"}
            or (row.status == "failed") != (row.provider_status == "failed")
            or row.accepted_at is None or row.completed_at is None
            or row.contract_version != WhatsAppOutboundAttempt.CONTRACT_VERSION
            or not isinstance(envelope, dict) or envelope.get("contract_version") != OUTBOX_CONTRACT
            or envelope.get("identity") != asdict(sender) or row.payload_digest != _canonical(envelope)
            or not isinstance(envelope.get("message"), dict) or envelope["message"].get("to") != item.contact
            or parent is None or (parent.tenant_id, parent.provider, parent.provider_connection_id,
                parent.provider_sender_id, parent.stream_key) != (sender.tenant_id, "meta", sender.connection_id,
                    sender.sender_id, row.stream_key)):
        raise MetaBatchAdmissionError("meta_batch_status_native_scope_conflict")
    return row


def _lock_scopes(session, bindings, runtime):
    senders = sorted((value.sender for value in bindings.values()),
                     key=lambda value: (value.tenant_id, value.connection_id, value.sender_id))
    # Global table/id order for ALL scopes in this batch, never input order.
    for model, ids in ((ProviderConnection, {s.connection_id for s in senders}),
                       (ProviderSender, {s.sender_id for s in senders}),
                       (TenantProfile, {s.tenant_id for s in senders})):
        for identifier in sorted(ids):
            if session.scalar(select(model.id).where(model.id == identifier).with_for_update()) is None:
                raise MetaBatchAdmissionError("meta_batch_scope_retired")
    allowed = resolve_whatsapp_inbound_queue_tenants(runtime.config, provider="meta")
    owners = {}
    for binding in sorted(bindings.values(), key=lambda value: (value.sender.tenant_id,
            value.sender.connection_id, value.sender.sender_id)):
        sender = binding.sender
        profile = session.execute(select(TenantProfile.id, TenantProfile.tipo,
            TenantProfile.pyme_id, TenantProfile.municipio_id, TenantProfile.is_active)
            .where(TenantProfile.id == sender.tenant_id)).one_or_none()
        if (sender.tenant_id not in allowed or profile is None or profile.is_active is not True):
            raise MetaBatchAdmissionError("meta_batch_tenant_not_enabled")
        owner = profile.municipio_id if profile.tipo == "municipio" else profile.pyme_id
        if type(owner) is not int or owner <= 0:
            raise MetaBatchAdmissionError("meta_batch_owner_scope_missing")
        runtime.recheck_local_binding(binding, session=session)  # NO Graph callback.
        owners[sender.tenant_id] = owner
    return owners


def _apply_status(session, item, row, instant):
    previous = row.provider_status
    if (previous == item.status or previous == "failed"
            or (item.status in _PROGRESS and _PROGRESS[item.status] < _PROGRESS.get(previous, -1))
            or (item.status == "failed" and _PROGRESS.get(previous, -1) >= _PROGRESS["delivered"])):
        return "duplicate_or_stale"
    row.provider_status, row.updated_at = item.status, instant
    if item.status == "failed":
        row.status, row.completed_at = "failed", instant
        row.last_error_code = "meta_delivery_failed"
        row.last_error_digest = _canonical({"codes": list(item.error_codes)})
        # Later unsent rows of this same Meta bundle are cancelled, never Twilio.
        siblings = session.scalars(select(WhatsAppOutboundAttempt).where(
            WhatsAppOutboundAttempt.tenant_id == row.tenant_id,
            WhatsAppOutboundAttempt.inbound_turn_id == row.inbound_turn_id,
            WhatsAppOutboundAttempt.provider == "meta",
            WhatsAppOutboundAttempt.provider_connection_id == row.provider_connection_id,
            WhatsAppOutboundAttempt.provider_sender_id == row.provider_sender_id,
            WhatsAppOutboundAttempt.sequence_no > row.sequence_no,
            WhatsAppOutboundAttempt.status.in_(("pending", "retry_wait")))
            .order_by(WhatsAppOutboundAttempt.id)).all()
        for sibling in siblings:
            sibling.status, sibling.provider_status = "cancelled", "cancelled"
            sibling.completed_at, sibling.updated_at = instant, instant
            sibling.last_error_code = "meta_predecessor_delivery_failed"
    return "staged"


def stage_meta_webhook_batch(session: Session, transaction: SessionTransaction, *,
        raw_body: bytes, signature: str, app_id: str, runtime: MetaOutboundRuntime) -> MetaBatchStaging:
    """Stage one signed batch; its owner MUST commit before any success ACK.

    Unscoped contacts, unknown statuses, unsupported media, conflicting duplicates
    and storage errors abort the whole batch. No retry, savepoint, internal commit/rollback,
    provider POST, route wiring or HTTP response is performed by this port.
    """
    _require_transaction(session, transaction, clean=True)
    _install_commit_guard(session)
    try:
        if not isinstance(runtime, MetaOutboundRuntime) or not isinstance(runtime.config, Mapping):
            raise MetaBatchAdmissionError("meta_batch_runtime_required")
        meta_id(app_id)
        registry = runtime.config.get(APP_SECRETS_KEY)
        secret = registry.get(app_id) if isinstance(registry, Mapping) else None
        canary_scope = meta_callback_recipient_scope(runtime.config, app_id)
        now = runtime.now()
        bindings = {}
        def binding_resolver(phone_number_id, observed_app_id, waba_id):
            scoped = canary_scope.get((waba_id, phone_number_id))
            if scoped is None or observed_app_id != app_id:
                raise cloud.MetaContractError("meta_webhook_binding_unavailable")
            # The parser validates every binding against the same batch clock.
            # Conservatively start each authority window at that instant, then
            # recheck its actual expiry before staging under the local locks.
            binding = cloud.resolve_sender(phone_number_id=phone_number_id, app_id=observed_app_id,
                waba_id=waba_id, environment="production", now=now, app_config=runtime.config,
                authority_verifier=lambda sender, credential: runtime.authority(
                    sender, credential, observed_at=now), expected_tenant_id=scoped[0], session=session)
            bindings[(phone_number_id, observed_app_id, waba_id)] = binding
            return binding
        # HMAC and WHOLE JSON validation first. Authority Graph GETs occur here,
        # before the first FOR UPDATE, identity mutation or inbox/status write.
        with session.no_autoflush:
            events = parse_webhook(raw_body=raw_body, signature=signature, app_secret=secret,
                app_id=app_id, now=now, binding_resolver=binding_resolver,
                allow_sandbox_content=True)
            if not events:
                raise MetaBatchAdmissionError("meta_batch_empty")
            for item in events:
                tenant, contacts = canary_scope[(item.sender.waba_id, item.sender.phone_number_id)]
                # Inbound `from` and status `recipient_id` both describe the
                # contact. Neither the business phone ID nor opaque IDs suffice.
                if (item.sender.tenant_id != tenant or item.contact_identity is not None
                        or item.contact not in contacts):
                    raise MetaBatchAdmissionError("meta_batch_canary_contact_denied")
            payloads, streams = {}, {}
            for item in events:
                if item.timestamp > runtime.now():
                    raise MetaBatchAdmissionError("meta_batch_future_timestamp")
                binding = bindings[(item.sender.phone_number_id, item.sender.app_id, item.sender.waba_id)]
                if item.kind == "message":
                    payloads[item.event_key] = _projection(item, binding, runtime.now())
                    streams[item.event_key] = _stream(runtime.config, item)
                    existing = _message_row(session, item, lock=False)
                    if existing is None:
                        if _canonical(verified_inbound_payload(item, binding, now=runtime.now())) != _canonical(payloads[item.event_key]):
                            raise MetaBatchAdmissionError("meta_batch_producer_mismatch")
                    else:
                        _duplicate(existing, item, payloads[item.event_key], streams[item.event_key], runtime.now())
                else:
                    _status_row(session, item, lock=False)
            _require_transaction(session, transaction, clean=True)
            owners = _lock_scopes(session, bindings, runtime)
            if meta_callback_recipient_scope(runtime.config, app_id) != canary_scope:
                raise MetaBatchAdmissionError("meta_batch_canary_scope_retired")
            # Verify that a config replacement did not silently retire this RAW
            # signature while remote GETs/lock waits were in flight.
            current = runtime.config.get(APP_SECRETS_KEY)
            active_secret = current.get(app_id) if isinstance(current, Mapping) else None
            verify_signature(raw_body=raw_body, signature=signature, app_secret=active_secret)
            if not isinstance(active_secret, str) or not hmac.compare_digest(secret, active_secret):
                raise MetaBatchAdmissionError("meta_batch_app_secret_retired")
            ordered = sorted(events, key=lambda item: (item.sender.tenant_id,
                item.sender.connection_id, item.sender.sender_id, item.kind, item.contact,
                item.timestamp, item.message_id, item.event_key))
            existing_messages, status_rows = {}, {}
            for item in ordered:
                if item.kind == "message":
                    existing = _message_row(session, item, lock=True)
                    if existing is not None:
                        _duplicate(existing, item, payloads[item.event_key], streams[item.event_key], runtime.now())
                    else:
                        binding = bindings[(item.sender.phone_number_id, item.sender.app_id, item.sender.waba_id)]
                        verified_inbound_payload(item, binding, now=runtime.now())
                    existing_messages[item.event_key] = existing
                else:
                    status_rows[item.event_key] = _status_row(session, item, lock=False)
            # Lock every existing outbox row touched by the status fold,
            # including a failed bundle's unsent tail, in global numeric order.
            parent_ids = {row.inbound_turn_id for row in status_rows.values()}
            if parent_ids:
                session.scalars(select(WhatsAppOutboundAttempt).where(
                    WhatsAppOutboundAttempt.inbound_turn_id.in_(parent_ids),
                    WhatsAppOutboundAttempt.provider == "meta")
                    .order_by(WhatsAppOutboundAttempt.id).with_for_update()).all()
                for item in ordered:
                    if item.kind == "status":
                        status_rows[item.event_key] = _status_row(session, item, lock=False)
        receipts = {}
        for item in ordered:
            _require_transaction(session, transaction, clean=True)
            binding = bindings[(item.sender.phone_number_id, item.sender.app_id, item.sender.waba_id)]
            runtime.recheck_local_binding(binding, session=session)
            instant = datetime.fromtimestamp(runtime.now(), timezone.utc)
            if item.kind == "status":
                row = status_rows[item.event_key]
                with session.no_autoflush:
                    outcome = _apply_status(session, item, row, instant)
            else:
                row = existing_messages[item.event_key]
                if row is not None:
                    outcome = "duplicate"
                else:
                    # Identity BEFORE pending inbox; caller TX, no nested retry.
                    identity = stage_meta_channel_session_identity(session, transaction,
                        config=runtime.config, tenant_id=item.sender.tenant_id,
                        owner_user_id=owners[item.sender.tenant_id], provider_identity="+" + item.contact,
                        now=instant).resolution
                    payload = verified_inbound_payload(item, binding, now=runtime.now())
                    row = WhatsAppInboundTurn(turn_id=str(uuid.uuid4()), tenant_id=item.sender.tenant_id,
                        provider_connection_id=item.sender.connection_id, provider_sender_id=item.sender.sender_id,
                        provider="meta", provider_message_sid=item.message_id, stream_key=streams[item.event_key],
                        chat_session_id=identity.chat_session_id, session_identity_binding_id=identity.binding_id,
                        session_identity_version=identity.identity_version, session_identity_hmac=identity.identity_hmac,
                        message_kind=item.content_type, payload_digest=_canonical(payload), payload_json=copy.deepcopy(payload),
                        status="received", attempt_count=0, max_attempts=8, available_at=instant,
                        received_at=instant, created_at=instant, updated_at=instant,
                        contract_version=WhatsAppInboundTurn.CONTRACT_VERSION)
                    session.add(row)
                    outcome = "staged"
            session.flush()  # Explicit inter-event boundary; no hidden flush of caller WIP.
            receipts[item.event_key] = MetaBatchEventReceipt(item.event_key, item.kind,
                outcome, item.sender.tenant_id, row.id)
        with session.no_autoflush:
            _lock_scopes(session, bindings, runtime)  # local recheck only, no new Graph GET.
            if meta_callback_recipient_scope(runtime.config, app_id) != canary_scope:
                raise MetaBatchAdmissionError("meta_batch_canary_scope_retired")
            if runtime.now() < now:
                raise MetaBatchAdmissionError("meta_batch_clock_regressed")
            _require_transaction(session, transaction, clean=True)
        return MetaBatchStaging(CONTRACT, tuple(receipts[item.event_key] for item in events), committed=False)
    except SQLAlchemyError:
        session.info[_STATE_KEY] = transaction
        raise MetaBatchAdmissionError("meta_batch_storage_failed") from None
    except MetaBatchAdmissionError:
        session.info[_STATE_KEY] = transaction
        raise
    except Exception:
        session.info[_STATE_KEY] = transaction
        raise MetaBatchAdmissionError("meta_batch_rejected") from None
    except BaseException:
        # Also poison on interruptions after a partial flush. The owner alone
        # rolls back/releases locks; a caught exception cannot commit half a batch.
        session.info[_STATE_KEY] = transaction
        raise
