"""Canonical tenant-scoped channel session identity bindings.

The provider identity is normalized only in memory and persisted exclusively as
an HMAC.  Bindings own a random chat-session UUID, so phone numbers and provider
addresses never become global primary keys.  Legacy context is adopted only
when its tenant/owner/identity membership is unique; ambiguity creates an
isolated context and keeps processing the inbound message.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import re
import uuid
from typing import Any, Mapping, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import db
from models import (
    ChannelSessionIdentityBinding,
    ChatSessionContext,
    TenantProfile,
)


CONTRACT_VERSION = ChannelSessionIdentityBinding.CONTRACT_VERSION
MODE_LEGACY = "legacy"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
MODES = frozenset({MODE_LEGACY, MODE_SHADOW, MODE_ENFORCE})
DEFAULT_IDENTITY_VERSION = "v1"

_SAFE_SCOPE = re.compile(r"^[a-z][a-z0-9_.:-]{0,31}$")
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


class ChannelSessionIdentityError(RuntimeError):
    """Safe boundary error whose code never contains provider identity data."""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


class ChannelSessionIdentityConflict(ChannelSessionIdentityError):
    """A durable binding failed tenant/identity/session verification."""


@dataclass(frozen=True)
class ChannelSessionIdentityResolution:
    binding_id: int
    tenant_id: int
    channel: str
    provider: str
    identity_version: str
    identity_hmac: str
    chat_session_id: str
    outcome: str
    continuity_preserved: bool
    conflict_code: Optional[str] = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def channel_session_identity_mode(config: Mapping[str, Any]) -> str:
    mode = str(config.get("CHANNEL_SESSION_IDENTITY_MODE", MODE_LEGACY) or MODE_LEGACY)
    mode = mode.strip().lower()
    if mode not in MODES:
        raise ChannelSessionIdentityError("session_identity_mode_invalid")
    return mode


def channel_session_identity_enabled(config: Mapping[str, Any]) -> bool:
    return channel_session_identity_mode(config) in {MODE_SHADOW, MODE_ENFORCE}


def resolve_channel_session_identity_secret(
    config: Mapping[str, Any],
    *,
    mode: Optional[str] = None,
) -> bytes:
    resolved_mode = mode or channel_session_identity_mode(config)
    dedicated = str(
        config.get("CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1") or ""
    ).encode("utf-8")
    if len(dedicated) >= 32:
        return dedicated
    if resolved_mode == MODE_ENFORCE:
        raise ChannelSessionIdentityError("session_identity_secret_missing")

    # Shadow rollout may use an already durable platform secret.  The HMAC
    # message is domain separated below; raw key material is never persisted.
    for key in ("WHATSAPP_INBOUND_HASH_SECRET", "SECRET_KEY"):
        fallback = str(config.get(key) or "").encode("utf-8")
        if len(fallback) >= 32:
            return fallback
    raise ChannelSessionIdentityError("session_identity_secret_missing")


def _positive_int(value: Any, code: str) -> int:
    if isinstance(value, bool):
        raise ChannelSessionIdentityError(code)
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ChannelSessionIdentityError(code) from exc
    if normalized < 1:
        raise ChannelSessionIdentityError(code)
    return normalized


def _scope(value: Any, code: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SAFE_SCOPE.fullmatch(normalized):
        raise ChannelSessionIdentityError(code)
    return normalized


def _identity_version(value: Any) -> str:
    return _scope(value or DEFAULT_IDENTITY_VERSION, "session_identity_version_invalid")


def _normalize_provider_identity(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw.startswith("whatsapp:"):
        raw = raw[len("whatsapp:") :].strip()
    compact = re.sub(r"[\s().-]", "", raw)
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    if not _E164.fullmatch(compact):
        raise ChannelSessionIdentityError("session_identity_invalid")
    return compact


def derive_channel_session_identity_hmac(
    *,
    secret: str | bytes,
    tenant_id: Any,
    provider: Any,
    identity_version: Any,
    provider_identity: Any,
) -> str:
    tenant = _positive_int(tenant_id, "session_identity_tenant_invalid")
    provider_scope = _scope(provider, "session_identity_provider_invalid")
    version = _identity_version(identity_version)
    identity = _normalize_provider_identity(provider_identity)
    key = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret or b"")
    if len(key) < 32:
        raise ChannelSessionIdentityError("session_identity_secret_missing")
    material = (
        f"chatboc:channel-session-identity:v1\0{tenant}\0{provider_scope}\0"
        f"{version}\0{identity}"
    ).encode("utf-8")
    return hmac.new(key, material, hashlib.sha256).hexdigest()


def _session_digest(chat_session_id: Any, *, secret: bytes) -> Optional[str]:
    value = str(chat_session_id or "").strip()
    if not value:
        return None
    return hmac.new(
        secret,
        b"chatboc:quarantined-session:v1\0" + value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _owner_belongs_to_tenant(
    session: Session,
    *,
    tenant_id: int,
    owner_user_id: Optional[int],
) -> bool:
    if owner_user_id is None:
        return False
    tenant = session.get(TenantProfile, tenant_id)
    if tenant is None:
        return False
    return int(owner_user_id) in {
        int(candidate)
        for candidate in (tenant.pyme_id, tenant.municipio_id)
        if candidate is not None
    }


def _context_is_owned(
    context: ChatSessionContext,
    *,
    tenant_id: int,
    owner_user_id: Optional[int],
    owner_is_authoritative: bool,
    normalized_identity: str,
) -> bool:
    if str(context.anon_id or "").strip().lower() != normalized_identity:
        return False
    if owner_user_id is None or context.user_id != owner_user_id:
        return False
    if context.tenant_id is not None:
        return int(context.tenant_id) == tenant_id
    return owner_is_authoritative


def _legacy_candidates(
    session: Session,
    *,
    tenant_id: int,
    owner_user_id: Optional[int],
    normalized_identity: str,
    explicit_chat_session_id: Optional[str],
) -> tuple[list[ChatSessionContext], Optional[str]]:
    owner_is_authoritative = _owner_belongs_to_tenant(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
    )
    if explicit_chat_session_id:
        candidate = session.get(ChatSessionContext, str(explicit_chat_session_id))
        if candidate is None:
            return [], None
        if not _context_is_owned(
            candidate,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            owner_is_authoritative=owner_is_authoritative,
            normalized_identity=normalized_identity,
        ):
            return [], "legacy_explicit_scope_conflict"
        return [candidate], None

    if owner_user_id is None:
        return [], None
    statement = select(ChatSessionContext).where(
        ChatSessionContext.anon_id == normalized_identity,
        ChatSessionContext.user_id == owner_user_id,
        or_(
            ChatSessionContext.tenant_id == tenant_id,
            and_(
                ChatSessionContext.tenant_id.is_(None),
                owner_is_authoritative,
            ),
        ),
    )
    candidates = list(session.scalars(statement).all())
    if len(candidates) > 1:
        return candidates, "legacy_membership_ambiguous"
    return candidates, None


def _new_context(
    session: Session,
    *,
    tenant_id: int,
    owner_user_id: Optional[int],
    channel: str,
    provider: str,
    identity_version: str,
    continuity_status: str,
    conflict_code: Optional[str],
) -> ChatSessionContext:
    context = ChatSessionContext(
        chat_session_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        user_id=owner_user_id,
        anon_id=None,
        context_data={
            "session_identity": {
                "contract_version": CONTRACT_VERSION,
                "channel": channel,
                "provider": provider,
                "identity_version": identity_version,
                "continuity_status": continuity_status,
                "conflict_code": conflict_code,
            }
        },
    )
    session.add(context)
    session.flush()
    return context


def _resolution(
    binding: ChannelSessionIdentityBinding,
    *,
    outcome: str,
    continuity_preserved: bool,
    conflict_code: Optional[str] = None,
) -> ChannelSessionIdentityResolution:
    return ChannelSessionIdentityResolution(
        binding_id=int(binding.id),
        tenant_id=int(binding.tenant_id),
        channel=str(binding.channel),
        provider=str(binding.provider),
        identity_version=str(binding.identity_version),
        identity_hmac=str(binding.identity_hmac),
        chat_session_id=str(binding.chat_session_id),
        outcome=outcome,
        continuity_preserved=bool(continuity_preserved),
        conflict_code=conflict_code,
    )


def _binding_statement(
    *,
    tenant_id: int,
    channel: str,
    provider: str,
    identity_version: str,
    identity_hmac: str,
):
    return select(ChannelSessionIdentityBinding).where(
        ChannelSessionIdentityBinding.tenant_id == tenant_id,
        ChannelSessionIdentityBinding.channel == channel,
        ChannelSessionIdentityBinding.provider == provider,
        ChannelSessionIdentityBinding.identity_version == identity_version,
        ChannelSessionIdentityBinding.identity_hmac == identity_hmac,
    )


def _repair_or_return_existing(
    session: Session,
    *,
    binding: ChannelSessionIdentityBinding,
    tenant_id: int,
    owner_user_id: Optional[int],
    secret: bytes,
    now: datetime,
) -> ChannelSessionIdentityResolution:
    context = session.get(ChatSessionContext, binding.chat_session_id)
    if (
        binding.status == ChannelSessionIdentityBinding.STATUS_ACTIVE
        and context is not None
        and context.tenant_id is not None
        and int(context.tenant_id) == tenant_id
    ):
        binding.last_verified_at = now
        binding.updated_at = now
        session.commit()
        return _resolution(
            binding,
            outcome="existing",
            continuity_preserved=True,
        )

    previous_digest = _session_digest(binding.chat_session_id, secret=secret)
    replacement = _new_context(
        session,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        channel=str(binding.channel),
        provider=str(binding.provider),
        identity_version=str(binding.identity_version),
        continuity_status=ChannelSessionIdentityBinding.CONTINUITY_ISOLATED,
        conflict_code="binding_context_scope_conflict",
    )
    binding.chat_session_id = replacement.chat_session_id
    binding.status = ChannelSessionIdentityBinding.STATUS_ACTIVE
    binding.continuity_status = ChannelSessionIdentityBinding.CONTINUITY_ISOLATED
    binding.generation = int(binding.generation or 1) + 1
    binding.last_conflict_code = "binding_context_scope_conflict"
    binding.previous_session_digest = previous_digest
    binding.quarantined_at = now
    binding.last_verified_at = now
    binding.updated_at = now
    session.commit()
    return _resolution(
        binding,
        outcome="isolated",
        continuity_preserved=False,
        conflict_code="binding_context_scope_conflict",
    )


def resolve_channel_session_identity(
    *,
    config: Mapping[str, Any],
    tenant_id: Any,
    channel: Any,
    provider: Any,
    provider_identity: Any,
    owner_user_id: Any = None,
    identity_version: Any = None,
    explicit_legacy_chat_session_id: Any = None,
    create_if_missing: bool = True,
) -> Optional[ChannelSessionIdentityResolution]:
    """Resolve, safely adopt, or isolate one canonical channel context."""

    mode = channel_session_identity_mode(config)
    if mode == MODE_LEGACY:
        raise ChannelSessionIdentityError("session_identity_mode_legacy")
    tenant = _positive_int(tenant_id, "session_identity_tenant_invalid")
    channel_scope = _scope(channel, "session_identity_channel_invalid")
    provider_scope = _scope(provider, "session_identity_provider_invalid")
    version = _identity_version(
        identity_version or config.get("CHANNEL_SESSION_IDENTITY_VERSION_V1")
    )
    normalized_identity = _normalize_provider_identity(provider_identity)
    owner_id = (
        _positive_int(owner_user_id, "session_identity_owner_invalid")
        if owner_user_id is not None
        else None
    )
    explicit_legacy_id = str(explicit_legacy_chat_session_id or "").strip() or None
    secret = resolve_channel_session_identity_secret(config, mode=mode)
    identity_digest = derive_channel_session_identity_hmac(
        secret=secret,
        tenant_id=tenant,
        provider=provider_scope,
        identity_version=version,
        provider_identity=normalized_identity,
    )
    now = _utc_now()

    for attempt in range(2):
        with Session(bind=db.engine, expire_on_commit=False) as session:
            binding = session.scalar(
                _binding_statement(
                    tenant_id=tenant,
                    channel=channel_scope,
                    provider=provider_scope,
                    identity_version=version,
                    identity_hmac=identity_digest,
                ).with_for_update()
            )
            if binding is not None:
                return _repair_or_return_existing(
                    session,
                    binding=binding,
                    tenant_id=tenant,
                    owner_user_id=owner_id,
                    secret=secret,
                    now=now,
                )

            candidates, conflict_code = _legacy_candidates(
                session,
                tenant_id=tenant,
                owner_user_id=owner_id,
                normalized_identity=normalized_identity,
                explicit_chat_session_id=explicit_legacy_id,
            )
            if not candidates and not conflict_code and not create_if_missing:
                session.rollback()
                return None

            previous_digest = None
            if len(candidates) == 1 and not conflict_code:
                context = candidates[0]
                if context.tenant_id is None:
                    context.tenant_id = tenant
                continuity_status = ChannelSessionIdentityBinding.CONTINUITY_ADOPTED
                outcome = "adopted"
                continuity_preserved = True
            else:
                if candidates:
                    previous_digest = hmac.new(
                        secret,
                        b"chatboc:quarantined-session-set:v1\0"
                        + "\0".join(
                            sorted(str(item.chat_session_id) for item in candidates)
                        ).encode("utf-8"),
                        hashlib.sha256,
                    ).hexdigest()
                continuity_status = (
                    ChannelSessionIdentityBinding.CONTINUITY_ISOLATED
                    if conflict_code
                    else ChannelSessionIdentityBinding.CONTINUITY_NEW
                )
                outcome = "isolated" if conflict_code else "created"
                continuity_preserved = False
                context = _new_context(
                    session,
                    tenant_id=tenant,
                    owner_user_id=owner_id,
                    channel=channel_scope,
                    provider=provider_scope,
                    identity_version=version,
                    continuity_status=continuity_status,
                    conflict_code=conflict_code,
                )

            binding = ChannelSessionIdentityBinding(
                tenant_id=tenant,
                channel=channel_scope,
                provider=provider_scope,
                identity_version=version,
                identity_hmac=identity_digest,
                chat_session_id=context.chat_session_id,
                status=ChannelSessionIdentityBinding.STATUS_ACTIVE,
                continuity_status=continuity_status,
                generation=1,
                last_conflict_code=conflict_code,
                previous_session_digest=previous_digest,
                quarantined_at=now if conflict_code else None,
                last_verified_at=now,
                contract_version=CONTRACT_VERSION,
                created_at=now,
                updated_at=now,
            )
            session.add(binding)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                if attempt == 0:
                    continue
                raise ChannelSessionIdentityConflict("session_identity_binding_race")
            return _resolution(
                binding,
                outcome=outcome,
                continuity_preserved=continuity_preserved,
                conflict_code=conflict_code,
            )
    raise ChannelSessionIdentityConflict("session_identity_binding_race")


def verify_channel_session_identity_binding(
    *,
    tenant_id: Any,
    binding_id: Any,
    channel: Any,
    provider: Any,
    identity_version: Any,
    identity_hmac: Any,
    chat_session_id: Any,
) -> ChannelSessionIdentityResolution:
    """Fail closed when a durable replay no longer matches its ingest binding."""

    tenant = _positive_int(tenant_id, "session_identity_tenant_invalid")
    resolved_binding_id = _positive_int(binding_id, "session_identity_binding_invalid")
    channel_scope = _scope(channel, "session_identity_channel_invalid")
    provider_scope = _scope(provider, "session_identity_provider_invalid")
    version = _identity_version(identity_version)
    digest = str(identity_hmac or "").strip().lower()
    expected_session = str(chat_session_id or "").strip()
    if not _HEX_DIGEST.fullmatch(digest) or not expected_session:
        raise ChannelSessionIdentityConflict("session_identity_replay_incomplete")

    with Session(bind=db.engine, expire_on_commit=False) as session:
        binding = session.scalar(
            select(ChannelSessionIdentityBinding).where(
                ChannelSessionIdentityBinding.id == resolved_binding_id,
                ChannelSessionIdentityBinding.tenant_id == tenant,
            )
        )
        if binding is None:
            raise ChannelSessionIdentityConflict("session_identity_binding_missing")
        comparisons = (
            binding.status == ChannelSessionIdentityBinding.STATUS_ACTIVE,
            str(binding.channel) == channel_scope,
            str(binding.provider) == provider_scope,
            str(binding.identity_version) == version,
            hmac.compare_digest(str(binding.identity_hmac or ""), digest),
            hmac.compare_digest(str(binding.chat_session_id or ""), expected_session),
        )
        context = session.get(ChatSessionContext, binding.chat_session_id)
        context_valid = bool(
            context is not None
            and context.tenant_id is not None
            and int(context.tenant_id) == tenant
        )
        if not all(comparisons) or not context_valid:
            raise ChannelSessionIdentityConflict("session_identity_replay_scope_conflict")
        return _resolution(
            binding,
            outcome="verified",
            continuity_preserved=True,
        )


__all__ = [
    "ChannelSessionIdentityConflict",
    "ChannelSessionIdentityError",
    "ChannelSessionIdentityResolution",
    "MODE_ENFORCE",
    "MODE_LEGACY",
    "MODE_SHADOW",
    "channel_session_identity_enabled",
    "channel_session_identity_mode",
    "derive_channel_session_identity_hmac",
    "resolve_channel_session_identity",
    "resolve_channel_session_identity_secret",
    "verify_channel_session_identity_binding",
]
