"""Signed, tenant-bound and one-time WhatsApp Flow invocations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
from typing import Any, Iterable, Mapping
from uuid import uuid4

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from models import WhatsAppFlowInteraction, db


TOKEN_VERSION = 1
TOKEN_PURPOSE = "whatsapp_flow"
TOKEN_KEY_ID = "v1"
TOKEN_SALT = "chatboc.whatsapp-flow.invocation.v1"
MINIMUM_KEY_BYTES = 32
DEFAULT_TTL_SECONDS = 48 * 60 * 60
MAX_TTL_SECONDS = 7 * 24 * 60 * 60
_RECIPIENT_CHARACTERS = re.compile(r"^\+[() .\-\d]+$")
_FLOW_ID = re.compile(r"^[A-Za-z0-9_.:\-]{1,120}$")


class WhatsAppFlowTokenError(ValueError):
    """Stable error code that never includes token or recipient values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class IssuedWhatsAppFlowToken:
    token: str
    token_digest: str
    recipient_hash: str
    recipient_hint: str
    expires_at: datetime
    claims: Mapping[str, Any]


def whatsapp_flow_token_key_ready(value: Any) -> bool:
    return len(str(value or "").encode("utf-8")) >= MINIMUM_KEY_BYTES


def normalize_flow_recipient(value: Any) -> str:
    raw = str(value or "").strip()
    if raw.lower().startswith("whatsapp:"):
        raw = raw.split(":", 1)[1].strip()
    # Keep accepting human-formatted E.164 input, but only one leading plus is
    # valid. The previous character class also accepted values such as
    # ``+54+911...`` and silently normalized them to another recipient.
    if not _RECIPIENT_CHARACTERS.fullmatch(raw):
        raise WhatsAppFlowTokenError("invalid_recipient")
    digits = "".join(character for character in raw if character.isdigit())
    if not 8 <= len(digits) <= 15 or digits.startswith("0"):
        raise WhatsAppFlowTokenError("invalid_recipient")
    return f"+{digits}"


def whatsapp_flow_recipient_scope(
    *,
    secret: Any,
    tenant_id: int,
    recipient: Any,
) -> dict[str, str]:
    root_key = _key_bytes(secret)
    normalized = normalize_flow_recipient(recipient)
    return {
        "normalized": normalized,
        "recipient_hash": _recipient_hash(root_key, tenant_id, normalized),
        "recipient_hint": f"***{normalized[-4:]}",
    }


def normalized_flow_ttl(value: Any) -> int:
    try:
        ttl = int(value or DEFAULT_TTL_SECONDS)
    except (TypeError, ValueError) as exc:
        raise WhatsAppFlowTokenError("invalid_token_ttl") from exc
    if ttl < 60 or ttl > MAX_TTL_SECONDS:
        raise WhatsAppFlowTokenError("invalid_token_ttl")
    return ttl


def issue_whatsapp_flow_token(
    *,
    secret: Any,
    tenant_id: int,
    recipient: Any,
    flow_id: Any,
    meta_flow_id: Any,
    provider_sender_id: int,
    ttl_seconds: Any = DEFAULT_TTL_SECONDS,
) -> IssuedWhatsAppFlowToken:
    root_key = _key_bytes(secret)
    normalized_recipient = normalize_flow_recipient(recipient)
    internal_flow_id = _validated_flow_id(flow_id)
    external_flow_id = _validated_flow_id(meta_flow_id)
    ttl = normalized_flow_ttl(ttl_seconds)
    recipient_digest = _recipient_hash(root_key, tenant_id, normalized_recipient)
    claims = {
        "v": TOKEN_VERSION,
        "p": TOKEN_PURPOSE,
        "k": TOKEN_KEY_ID,
        "t": int(tenant_id),
        "f": internal_flow_id,
        "m": external_flow_id,
        "s": int(provider_sender_id),
        "r": recipient_digest,
        "j": uuid4().hex,
    }
    token = _serializer(root_key, tenant_id).dumps(claims)
    now = datetime.now(timezone.utc)
    return IssuedWhatsAppFlowToken(
        token=token,
        token_digest=_token_digest(root_key, token),
        recipient_hash=recipient_digest,
        recipient_hint=f"***{normalized_recipient[-4:]}",
        expires_at=now + timedelta(seconds=ttl),
        claims=claims,
    )


def verify_whatsapp_flow_token(
    token: Any,
    *,
    secret: Any,
    tenant_id: int,
    recipient: Any,
    provider_sender_id: int | None,
    allowed_flow_ids: Iterable[str],
    ttl_seconds: Any = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    root_key = _key_bytes(secret)
    normalized_recipient = normalize_flow_recipient(recipient)
    ttl = normalized_flow_ttl(ttl_seconds)
    raw_token = str(token or "").strip()
    if not raw_token or len(raw_token) > 4096:
        raise WhatsAppFlowTokenError("invalid_flow_token")
    try:
        claims = _serializer(root_key, tenant_id).loads(raw_token, max_age=ttl)
    except SignatureExpired as exc:
        raise WhatsAppFlowTokenError("expired_flow_token") from exc
    except BadSignature as exc:
        raise WhatsAppFlowTokenError("invalid_flow_token") from exc
    if not isinstance(claims, dict):
        raise WhatsAppFlowTokenError("invalid_flow_token_claims")

    expected_recipient_hash = _recipient_hash(root_key, tenant_id, normalized_recipient)
    expected = {
        "v": TOKEN_VERSION,
        "p": TOKEN_PURPOSE,
        "k": TOKEN_KEY_ID,
        "t": int(tenant_id),
        "r": expected_recipient_hash,
    }
    for claim, value in expected.items():
        if claims.get(claim) != value:
            raise WhatsAppFlowTokenError("flow_token_scope_mismatch")
    if provider_sender_id is not None and claims.get("s") != int(provider_sender_id):
        raise WhatsAppFlowTokenError("flow_token_sender_mismatch")

    flow_id = _validated_flow_id(claims.get("f"))
    meta_flow_id = _validated_flow_id(claims.get("m"))
    allowed = {str(item or "").strip().lower() for item in allowed_flow_ids if str(item or "").strip()}
    if not allowed or (flow_id.lower() not in allowed and meta_flow_id.lower() not in allowed):
        raise WhatsAppFlowTokenError("unrecognized_flow")

    token_digest = _token_digest(root_key, raw_token)
    interaction = WhatsAppFlowInteraction.query.filter_by(
        tenant_id=int(tenant_id),
        token_digest=token_digest,
    ).first()
    if interaction is None:
        raise WhatsAppFlowTokenError("unknown_flow_invocation")
    now = datetime.now(timezone.utc)
    expires_at = _as_utc(interaction.expires_at)
    if expires_at is None or expires_at <= now:
        raise WhatsAppFlowTokenError("expired_flow_invocation")
    if interaction.status not in {"sent", "send_uncertain", "consumed"}:
        raise WhatsAppFlowTokenError("inactive_flow_invocation")
    if (
        interaction.flow_id != flow_id
        or interaction.meta_flow_id != meta_flow_id
        or interaction.provider_sender_id != int(claims.get("s") or 0)
        or not hmac.compare_digest(interaction.recipient_hash, expected_recipient_hash)
    ):
        raise WhatsAppFlowTokenError("flow_invocation_scope_mismatch")

    return {
        "interaction_id": interaction.id,
        "token_digest": token_digest,
        "tenant_id": interaction.tenant_id,
        "flow_id": interaction.flow_id,
        "meta_flow_id": interaction.meta_flow_id,
        "provider_sender_id": interaction.provider_sender_id,
        "data_contract": _normalized_data_contract(interaction.data_contract),
        "already_consumed": interaction.consumed_at is not None,
        "expires_at": expires_at.isoformat(),
    }


def verify_whatsapp_flow_endpoint_token(
    token: Any,
    *,
    secret: Any,
    tenant_id: int,
    allowed_flow_ids: Iterable[str],
    ttl_seconds: Any = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """Verify a Data Exchange token without trusting recipient data from the request."""

    root_key = _key_bytes(secret)
    ttl = normalized_flow_ttl(ttl_seconds)
    raw_token = str(token or "").strip()
    if not raw_token or len(raw_token) > 4096:
        raise WhatsAppFlowTokenError("invalid_flow_token")
    try:
        claims = _serializer(root_key, tenant_id).loads(raw_token, max_age=ttl)
    except SignatureExpired as exc:
        raise WhatsAppFlowTokenError("expired_flow_token") from exc
    except BadSignature as exc:
        raise WhatsAppFlowTokenError("invalid_flow_token") from exc
    if not isinstance(claims, dict):
        raise WhatsAppFlowTokenError("invalid_flow_token_claims")

    expected = {
        "v": TOKEN_VERSION,
        "p": TOKEN_PURPOSE,
        "k": TOKEN_KEY_ID,
        "t": int(tenant_id),
    }
    for claim, value in expected.items():
        if claims.get(claim) != value:
            raise WhatsAppFlowTokenError("flow_token_scope_mismatch")

    flow_id = _validated_flow_id(claims.get("f"))
    meta_flow_id = _validated_flow_id(claims.get("m"))
    allowed = {
        str(item or "").strip().lower()
        for item in allowed_flow_ids
        if str(item or "").strip()
    }
    if not allowed or (flow_id.lower() not in allowed and meta_flow_id.lower() not in allowed):
        raise WhatsAppFlowTokenError("unrecognized_flow")

    token_digest = _token_digest(root_key, raw_token)
    interaction = WhatsAppFlowInteraction.query.filter_by(
        tenant_id=int(tenant_id),
        token_digest=token_digest,
    ).first()
    if interaction is None:
        raise WhatsAppFlowTokenError("unknown_flow_invocation")
    now = datetime.now(timezone.utc)
    expires_at = _as_utc(interaction.expires_at)
    if expires_at is None or expires_at <= now:
        raise WhatsAppFlowTokenError("expired_flow_invocation")
    if interaction.status not in {"sent", "send_uncertain", "consumed"}:
        raise WhatsAppFlowTokenError("inactive_flow_invocation")

    signed_recipient_hash = str(claims.get("r") or "")
    if (
        interaction.flow_id != flow_id
        or interaction.meta_flow_id != meta_flow_id
        or interaction.provider_sender_id != int(claims.get("s") or 0)
        or not signed_recipient_hash
        or not hmac.compare_digest(interaction.recipient_hash, signed_recipient_hash)
    ):
        raise WhatsAppFlowTokenError("flow_invocation_scope_mismatch")

    return {
        "interaction_id": interaction.id,
        "token_digest": token_digest,
        "tenant_id": interaction.tenant_id,
        "flow_id": interaction.flow_id,
        "meta_flow_id": interaction.meta_flow_id,
        "provider_sender_id": interaction.provider_sender_id,
        "recipient_hash": interaction.recipient_hash,
        "data_contract": _normalized_data_contract(interaction.data_contract),
        "already_consumed": interaction.consumed_at is not None,
        "expires_at": expires_at.isoformat(),
    }


def consume_whatsapp_flow_interaction(
    *,
    interaction_id: int,
    tenant_id: int,
    inbound_message_sid: Any = None,
    commit: bool = True,
) -> bool:
    """Atomically consume one invocation before conversational side effects.

    ``commit=False`` lets a caller persist the one-time transition and its CRM
    writeback in the same database transaction.
    """

    now = datetime.now(timezone.utc)
    update_values: dict[str, Any] = {
        WhatsAppFlowInteraction.status: "consumed",
        WhatsAppFlowInteraction.consumed_at: now,
        WhatsAppFlowInteraction.updated_at: now,
    }
    safe_message_sid = str(inbound_message_sid or "").strip()
    if safe_message_sid:
        update_values[WhatsAppFlowInteraction.inbound_message_sid] = safe_message_sid[:180]
    updated = (
        WhatsAppFlowInteraction.query.filter(
            WhatsAppFlowInteraction.id == int(interaction_id),
            WhatsAppFlowInteraction.tenant_id == int(tenant_id),
            WhatsAppFlowInteraction.consumed_at.is_(None),
            WhatsAppFlowInteraction.expires_at > now,
            WhatsAppFlowInteraction.status.in_(("sent", "send_uncertain")),
        )
        .update(update_values, synchronize_session=False)
    )
    if updated != 1:
        db.session.rollback()
        return False
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return True


def _key_bytes(value: Any) -> bytes:
    key = str(value or "").encode("utf-8")
    if len(key) < MINIMUM_KEY_BYTES:
        raise WhatsAppFlowTokenError("flow_token_key_not_configured")
    return key


def _validated_flow_id(value: Any) -> str:
    flow_id = str(value or "").strip()
    if not _FLOW_ID.fullmatch(flow_id):
        raise WhatsAppFlowTokenError("invalid_flow_identity")
    return flow_id


def _serializer(root_key: bytes, tenant_id: int) -> URLSafeTimedSerializer:
    tenant_key = hmac.new(
        root_key,
        f"tenant:{int(tenant_id)}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return URLSafeTimedSerializer(
        tenant_key,
        salt=TOKEN_SALT,
        signer_kwargs={"digest_method": hashlib.sha256},
    )


def _recipient_hash(root_key: bytes, tenant_id: int, recipient: str) -> str:
    return hmac.new(
        root_key,
        f"recipient:{int(tenant_id)}:{recipient}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _token_digest(root_key: bytes, token: str) -> str:
    return hmac.new(root_key, token.encode("utf-8"), hashlib.sha256).hexdigest()


def _normalized_data_contract(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    normalized: list[str] = []
    for item in value:
        field_name = str(item or "").strip()
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.\-]{0,79}", field_name):
            normalized.append(field_name)
    return list(dict.fromkeys(normalized))[:32]


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
