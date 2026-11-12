"""Explicit owner of one existing Meta conversation transaction.

Server-only capability, not authentication or a serializable context field.
This module does not install an ingress, allocate a Session, send a provider
message, or make unported business writers transactional. Their internal commit
is rejected and poisons this unit; it is never converted silently into a flush.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
import hmac
import re
import time
from typing import Callable

from sqlalchemy import event, select
from sqlalchemy.orm import Session, SessionTransaction, object_session
from sqlalchemy.orm.exc import UnmappedInstanceError

from extensions import db
from flask import current_app
from models import WhatsAppInboundTurn, TenantProfile, ChatSessionContext, ChannelSessionIdentityBinding
from services.whatsapp_inbound_turns import InboundTurnClaim
from services.whatsapp_provider_policy import WhatsAppProviderPolicyError, whatsapp_provider_config


class ConversationUnitOfWorkError(RuntimeError):
    """A fixed reason code only; never raw SQL/provider input."""


class ConversationCommitUncertain(ConversationUnitOfWorkError):
    """The owner's commit failed/acknowledgement is uncertain. Never auto-replay."""


@dataclass(frozen=True)
class ConversationCommitResult:
    receipt: object
    committed: bool
    after_commit_failures: tuple[str, ...]


class ConversationUnitOfWork:
    """Own the supplied concrete scoped Session and its current outer TX.

    The caller creates/owns the outer transaction before construction, with no
    unrelated pending ORM work. SQLite callers must begin a physical transaction
    explicitly; this class never changes engine policy or emits an implicit BEGIN.
    Only the finalizer's one particular nested transaction may release a savepoint.
    Connection-level commits or other Sessions are outside this guard and must
    not be used by the supported writer graph.
    """
    def __init__(self, *, session: Session, transaction: SessionTransaction,
                 claim: InboundTurnClaim, clock: Callable[[], int] | None = None):
        if not isinstance(session, Session) or session is not db.session():
            raise ConversationUnitOfWorkError("conversation_uow_session_not_scoped")
        if session.new or session.dirty or session.deleted:
            raise ConversationUnitOfWorkError("conversation_uow_pending_caller_work")
        if (not isinstance(transaction, SessionTransaction) or transaction.nested
                or transaction is not session.get_transaction()
                or not transaction.is_active or session.in_nested_transaction()):
            raise ConversationUnitOfWorkError("conversation_uow_outer_transaction_required")
        if (not isinstance(claim, InboundTurnClaim) or claim.provider != "meta"
                or type(claim.tenant_id) is not int or claim.tenant_id <= 0):
            raise ConversationUnitOfWorkError("conversation_uow_meta_claim_required")
        connection = session.connection()
        if connection.dialect.name == "sqlite":
            driver = connection.connection.driver_connection
            if getattr(driver, "in_transaction", None) is not True:
                raise ConversationUnitOfWorkError("conversation_uow_physical_transaction_required")
        self.session, self.transaction, self.claim = session, transaction, claim
        self.clock = clock or (lambda: int(time.time()))
        self._phase, self._violation, self._entered = "created", None, False
        self._finalizer_savepoint = None
        self._after_commit = []
        self._listeners = (
            ("before_commit", self._before_commit),
            ("after_transaction_create", self._transaction_created),
            ("after_transaction_end", self._transaction_ended),
        )

    def __repr__(self):
        return "<ConversationUnitOfWork server-only>"

    def __enter__(self):
        if self._entered or self._phase != "created":
            raise ConversationUnitOfWorkError("conversation_uow_already_entered")
        self._entered, self._phase = True, "open"
        for name, listener in self._listeners:
            event.listen(self.session, name, listener)
        try:
            self._check_claim()
        except BaseException:
            self._abort()
            self._detach()
            raise
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if self._phase not in {"committed", "commit_uncertain"}:
                self._abort()
                if exc_type is None:
                    raise ConversationUnitOfWorkError("conversation_uow_not_finalized")
        finally:
            self._detach()
        return False

    def _detach(self):
        for name, listener in self._listeners:
            if event.contains(self.session, name, listener):
                event.remove(self.session, name, listener)

    def _poison(self, code):
        self._violation = self._violation or code
        raise ConversationUnitOfWorkError(self._violation) from None

    def _assert_owner(self):
        if self._violation:
            raise ConversationUnitOfWorkError(self._violation)
        if (not self._entered or self._phase != "open" or self.session is not db.session()
                or self.session.get_transaction() is not self.transaction
                or not self.transaction.is_active or self.session.in_nested_transaction()):
            self._poison("conversation_uow_transaction_changed")

    def _before_commit(self, session):
        nested = session.get_nested_transaction()
        if self._violation:
            raise ConversationUnitOfWorkError(self._violation)
        if (self._phase == "finalizing" and nested is self._finalizer_savepoint
                and nested is not None and nested.parent is self.transaction):
            return
        if (self._phase == "committing" and nested is None
                and session.get_transaction() is self.transaction):
            return
        self._poison("conversation_uow_unexpected_commit")

    def _transaction_created(self, session, transaction):
        if not transaction.nested:
            return  # SQLAlchemy's internal flush subtransaction is not a SAVEPOINT.
        if (self._phase == "finalizing" and self._finalizer_savepoint is None
                and transaction.parent is self.transaction):
            self._finalizer_savepoint = transaction
            return
        self._poison("conversation_uow_unexpected_savepoint")

    def _transaction_ended(self, session, transaction):
        if transaction is self.transaction and self._phase not in {"committing", "aborting"}:
            self._violation = self._violation or "conversation_uow_outer_transaction_ended"

    def _check_claim(self):
        self._assert_owner()
        now = self.clock()
        if type(now) is not int or now < 0:
            self._poison("conversation_uow_clock_invalid")
        c = self.claim
        from services.meta_whatsapp_outbox import _canonical
        if _canonical(c.payload) != c.payload_digest:
            self._poison("conversation_uow_claim_payload_changed")
        with self.session.no_autoflush:
            row = self.session.execute(select(
                WhatsAppInboundTurn.tenant_id, WhatsAppInboundTurn.provider,
                WhatsAppInboundTurn.provider_connection_id, WhatsAppInboundTurn.provider_sender_id,
                WhatsAppInboundTurn.provider_message_sid, WhatsAppInboundTurn.payload_digest,
                WhatsAppInboundTurn.status, WhatsAppInboundTurn.lease_token,
                WhatsAppInboundTurn.leased_until, WhatsAppInboundTurn.chat_session_id,
                WhatsAppInboundTurn.session_identity_binding_id,
                WhatsAppInboundTurn.session_identity_version, WhatsAppInboundTurn.session_identity_hmac,
            ).where(WhatsAppInboundTurn.turn_id == c.turn_id)).one_or_none()
        expected = (c.tenant_id, "meta", c.provider_connection_id, c.provider_sender_id,
                    c.provider_message_sid, c.payload_digest, "processing", c.lease_token)
        if row is None or tuple(row[:8]) != expected:
            self._poison("conversation_uow_claim_scope_changed")
        until = row.leased_until
        if until is None or int(until.replace(tzinfo=until.tzinfo or timezone.utc).timestamp()) <= now:
            self._poison("conversation_uow_lease_not_owned")
        if tuple(row[9:]) != (c.chat_session_id, c.session_identity_binding_id,
                              c.session_identity_version, c.session_identity_hmac):
            self._poison("conversation_uow_identity_changed")
        return self._bound_identity(now)

    def _bound_identity(self, now):
        """Same-Session partition and HMAC coherence, not provider authentication."""
        from services.meta_whatsapp_outbox import _inbound_reference, MetaRuntimeError
        from services.channel_session_identity import (
            derive_channel_session_identity_hmac, resolve_channel_session_identity_secret,
            ChannelSessionIdentityError,
        )
        from services.whatsapp_inbound_turns import (
            _validate_session_identity_scope, WhatsAppTurnValidationError,
        )
        c = self.claim
        try:
            record = _inbound_reference(c.payload, now)
            identity, source = record["identity"], record["source"]
            if ((identity["tenant_id"], identity["connection_id"], identity["sender_id"])
                    != (c.tenant_id, c.provider_connection_id, c.provider_sender_id)
                    or source["message_id"] != c.provider_message_sid):
                self._poison("conversation_uow_source_partition_mismatch")
            canonical_phone = "+" + source["recipient"]
            if not all((c.session_identity_binding_id, c.session_identity_version,
                        c.session_identity_hmac, c.chat_session_id)):
                self._poison("conversation_uow_identity_incomplete")
            with self.session.no_autoflush:
                _validate_session_identity_scope(self.session, tenant_id=c.tenant_id,
                    provider="meta", binding_id=c.session_identity_binding_id,
                    identity_version=c.session_identity_version,
                    identity_hmac=c.session_identity_hmac, chat_session_id=c.chat_session_id)
                # The shared validator also supports legacy callers and reads
                # ORM objects. Read persisted columns here so a cached instance
                # cannot hide a revoked/repointed binding in this transaction.
                binding = self.session.execute(select(ChannelSessionIdentityBinding.tenant_id,
                    ChannelSessionIdentityBinding.channel, ChannelSessionIdentityBinding.provider,
                    ChannelSessionIdentityBinding.identity_version, ChannelSessionIdentityBinding.identity_hmac,
                    ChannelSessionIdentityBinding.chat_session_id, ChannelSessionIdentityBinding.status)
                    .where(ChannelSessionIdentityBinding.id == c.session_identity_binding_id)).one_or_none()
                binding_object = self.session.get(ChannelSessionIdentityBinding, c.session_identity_binding_id)
                binding_object_values = None if binding_object is None else tuple(
                    getattr(binding_object, key) for key in ("tenant_id", "channel", "provider",
                    "identity_version", "identity_hmac", "chat_session_id", "status"))
                profile = self.session.execute(select(TenantProfile.id, TenantProfile.slug,
                    TenantProfile.municipio_id, TenantProfile.pyme_id, TenantProfile.is_active)
                    .where(TenantProfile.id == c.tenant_id)).one_or_none()
                context = self.session.execute(select(ChatSessionContext.chat_session_id,
                    ChatSessionContext.tenant_id, ChatSessionContext.user_id, ChatSessionContext.anon_id)
                    .where(ChatSessionContext.chat_session_id == c.chat_session_id)).one_or_none()
                # Every Unit API (including direct flush/finalize/writer calls)
                # must reject reparenting before any flush. context_data WIP is
                # deliberately excluded: it is conversation state, not scope.
                profile_object = self.session.get(TenantProfile, c.tenant_id)
                context_object = self.session.get(ChatSessionContext, c.chat_session_id)
                profile_object_values = None if profile_object is None else tuple(
                    getattr(profile_object, key) for key in
                    ("id", "slug", "municipio_id", "pyme_id", "is_active"))
                context_object_values = None if context_object is None else tuple(
                    getattr(context_object, key) for key in
                    ("chat_session_id", "tenant_id", "user_id", "anon_id"))
                def same_persisted_scope(values, row):
                    return (values is not None and row is not None
                            and all(type(actual) is type(stored) and actual == stored
                                    for actual, stored in zip(values, row)))
                if (not same_persisted_scope(profile_object_values, profile)
                        or not same_persisted_scope(context_object_values, context)
                        or profile_object in self.session.deleted or context_object in self.session.deleted):
                    self._poison("conversation_uow_orm_scope_changed")
            owners = set() if profile is None else {
                value for value in (profile.municipio_id, profile.pyme_id)
                if type(value) is int and value > 0}
            expected_binding = (c.tenant_id, "whatsapp", "meta", c.session_identity_version,
                                c.session_identity_hmac, c.chat_session_id, "active")
            if (binding is None or tuple(binding) != expected_binding
                    or binding_object is None or binding_object in self.session.dirty
                    or binding_object in self.session.deleted
                    or binding_object_values != tuple(binding)):
                self._poison("conversation_uow_binding_partition_mismatch")
            if (profile is None or profile.is_active is not True or context is None
                    or context.tenant_id != c.tenant_id or context.user_id not in owners
                    or (context.anon_id is not None and context.anon_id != canonical_phone)):
                self._poison("conversation_uow_context_partition_mismatch")
            # A new canonical context intentionally has anon_id=None. The
            # binding HMAC, version and recipient establish this partition
            # without copying the raw phone back into context storage.
            expected = derive_channel_session_identity_hmac(
                secret=resolve_channel_session_identity_secret(whatsapp_provider_config(current_app.config, "meta")),
                tenant_id=c.tenant_id, provider="meta", identity_version=c.session_identity_version,
                provider_identity=canonical_phone)
            if not hmac.compare_digest(expected, c.session_identity_hmac):
                self._poison("conversation_uow_recipient_binding_mismatch")
        except (MetaRuntimeError, ChannelSessionIdentityError, WhatsAppTurnValidationError,
                WhatsAppProviderPolicyError):
            self._poison("conversation_uow_identity_scope_mismatch")
        return profile, context, canonical_phone

    def require_turn(self, turn):
        """Partition/ownership checks only, never actor authentication by DTO."""
        profile_row, context_row, canonical_phone = self._check_claim()
        with self.session.no_autoflush:
            context = turn.session_context
            profile = turn.tenant_profile
            def belongs(value):
                try:
                    return object_session(value) is self.session
                except UnmappedInstanceError:
                    return False
            if (turn.unit_of_work is not self or turn.durable_claim != self.claim
                    or turn.tenant_id != self.claim.tenant_id
                    or type(turn.tenant_id) is not int
                    or not belongs(context) or not belongs(profile)
                    or not belongs(turn.owner_user)
                    or (turn.end_user is not None and not belongs(turn.end_user))
                    or type(profile.id) is not int or type(context.tenant_id) is not int
                    or type(context.user_id) is not int or type(turn.owner_user.id) is not int
                    or profile.id != self.claim.tenant_id or context.tenant_id != self.claim.tenant_id
                    or context.chat_session_id != self.claim.chat_session_id
                    or turn.chat_session_id != self.claim.chat_session_id
                    or turn.source_event_id != self.claim.provider_message_sid
                    or turn.contact_identity != canonical_phone
                    or turn.owner_user.id != context_row.user_id
                    or context.user_id != context_row.user_id or context.anon_id != context_row.anon_id
                    or profile.slug != profile_row.slug or profile.municipio_id != profile_row.municipio_id
                    or profile.pyme_id != profile_row.pyme_id or profile.is_active != profile_row.is_active
                    or turn.owner_user in self.session.dirty or profile in self.session.dirty):
                self._poison("conversation_uow_turn_scope_mismatch")
            if turn.end_user is not None:
                from services.contact_service import resolve_contact, current_contact_phone, sanitize_profile_name
                fresh = resolve_contact(canonical_phone, None, tenant=profile)
                user = turn.end_user
                if (user in self.session.new or user in self.session.dirty or user in self.session.deleted
                        or fresh.get("is_known_contact") is not True
                        or (fresh.get("_scope") or {}).get("source") != "User"
                        or type(fresh.get("user_id")) is not int or fresh["user_id"] != user.id
                        or current_contact_phone(user.telefono) != canonical_phone
                        or sanitize_profile_name(user.name) != fresh.get("nombre")
                        or user.email != fresh.get("email")):
                    # Contact precedence or ambiguity requires an explicit
                    # end_user=None, not adoption of a different User's PII.
                    self._poison("conversation_uow_end_user_scope_mismatch")

    def flush(self):
        self._check_claim()
        self.session.flush()
        self._assert_owner()

    def require_writer_scope(self, *, session, tenant_id, turn_id, effect_key=None):
        """Writer partition/transaction checks, not actor or input authorization."""
        self._check_claim()
        if (session is not self.session or session is not db.session()
                or type(tenant_id) is not int or tenant_id != self.claim.tenant_id
                or not isinstance(turn_id, str) or turn_id != self.claim.turn_id):
            self._poison("conversation_uow_writer_scope_mismatch")
        if effect_key is not None:
            prefix = f"whatsapp:{self.claim.tenant_id}:{self.claim.turn_id}:"
            if (not isinstance(effect_key, str) or not effect_key.startswith(prefix)
                    or len(effect_key) == len(prefix)):
                self._poison("conversation_uow_writer_effect_scope_mismatch")
        # Existing writers retain their own payload/actor/effect-key validation.

    @property
    def contact_identity(self):
        """Canonical current recipient partition, not ownership/consent proof.

        Derived only from the persisted source whose digest and same-Session
        binding HMAC/version have just been checked. No setter or JSON field.
        """
        return self._check_claim()[2]

    def fail(self, code, cause=None):
        del cause  # Never retain/log a raw provider or SQL exception.
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
            code = "conversation_uow_operation_failed"
        self._poison(code)

    def defer_after_commit(self, label, callback):
        """Best-effort realtime/wakeup only. Not durable or a provider-send API."""
        self._assert_owner()
        if (not isinstance(label, str) or label not in {"crm_contact_update", "live_chat_message", "municipal_ticket_update"}
                or not callable(callback)):
            self._poison("conversation_uow_after_commit_invalid")
        self._after_commit.append((label, callback))

    def commit_reply(self, reply, *, runtime):
        from services.meta_whatsapp_outbox import complete_meta_service_reply
        self.flush()  # Explicit flush of the unit's work, not hidden in begin_nested.
        self._phase = "finalizing"
        try:
            receipt = complete_meta_service_reply(self.claim, reply, runtime=runtime, session=self.session)
            if (receipt.committed is not False or receipt.turn_id != self.claim.turn_id
                    or self._finalizer_savepoint is None
                    or self.session.get_nested_transaction() is not None
                    or self.session.get_transaction() is not self.transaction
                    or self._violation):
                self._poison("conversation_uow_finalizer_not_owned")
        except BaseException:
            self._phase = "open"
            raise
        self._phase = "committing"
        try:
            self.session.commit()
        except BaseException:
            self._phase = "commit_uncertain"
            self._after_commit.clear()
            self._detach()
            # Rollback may itself fail after a committed-but-unacknowledged DB
            # transaction. It does not establish that the commit did not occur.
            try:
                self.session.rollback()
            except Exception:
                pass
            raise ConversationCommitUncertain("conversation_uow_commit_uncertain") from None
        self._phase = "committed"
        self._detach()
        failures = []
        callbacks, self._after_commit = self._after_commit, []
        for label, callback in callbacks:
            try:
                callback()
            except Exception:
                failures.append(label)
        return ConversationCommitResult(receipt, True, tuple(failures))

    def _abort(self):
        self._after_commit.clear()
        self._phase = "aborting"
        try:
            self.session.rollback()
        finally:
            self._phase = "aborted"
