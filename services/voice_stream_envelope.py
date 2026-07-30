"""Signed, short-lived authorization envelopes for Twilio Media Streams.

The public Media Streams WebSocket cannot validate Twilio's HTTP webhook
signature.  The preceding TwiML webhook therefore mints a small HMAC envelope
that binds the call metadata consumed by the WebSocket handler.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse


VOICE_STREAM_ENVELOPE_VERSION = "v1"
VOICE_STREAM_ENVELOPE_DEFAULT_TTL_SECONDS = 120
VOICE_STREAM_ENVELOPE_MAX_TTL_SECONDS = 600
VOICE_STREAM_ENVELOPE_FUTURE_SKEW_SECONDS = 30

_SIGNING_DOMAIN = b"chatboc:twilio-media-stream-envelope:v1"
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_SIGNED_FIELDS = (
    "version",
    "call_sid",
    "from_number",
    "to_number",
    "tenant_slug",
    "vertical",
    "intent",
    "chat_session_id",
    "demo",
    "max_call_seconds",
    "ts",
    "nonce",
)
_FIELD_LIMITS = {
    "call_sid": 80,
    "from_number": 80,
    "to_number": 80,
    "tenant_slug": 128,
    "vertical": 64,
    "intent": 160,
    "chat_session_id": 160,
}
_REPLAY_KEY_PREFIX = "chatboc:voice-stream-envelope:v1:nonce"
_REPLAY_STATE_LOCK = threading.Lock()
_TEST_REPLAY_EXPIRATIONS: dict[str, int] = {}
_REDIS_CLIENTS: dict[str, Any] = {}


class VoiceStreamEnvelopeError(ValueError):
    """Safe, non-secret error raised for an invalid stream envelope."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _configured_value(config: Mapping[str, Any] | None, name: str) -> str | None:
    value = config.get(name) if config is not None else None
    if value in (None, ""):
        value = os.environ.get(name)
    if value in (None, ""):
        return None
    return str(value).strip() or None


def resolve_voice_stream_signing_key(
    config: Mapping[str, Any] | None = None,
) -> bytes | None:
    """Return a domain-separated key without exposing its source secret.

    A dedicated secret is preferred.  A Twilio auth token is a compatibility
    fallback, but is never used directly as the HMAC key.
    """

    dedicated_secret = _configured_value(config, "VOICE_STREAM_SIGNING_SECRET")
    if dedicated_secret is not None:
        if len(dedicated_secret.encode("utf-8")) < 32:
            raise VoiceStreamEnvelopeError("signing_secret_too_short")
        return hmac.new(
            dedicated_secret.encode("utf-8"),
            _SIGNING_DOMAIN + b":dedicated",
            hashlib.sha256,
        ).digest()

    twilio_auth_token = _configured_value(config, "TWILIO_AUTH_TOKEN")
    if twilio_auth_token is None:
        return None
    if len(twilio_auth_token.encode("utf-8")) < 16:
        raise VoiceStreamEnvelopeError("twilio_fallback_secret_too_short")
    return hmac.new(
        twilio_auth_token.encode("utf-8"),
        _SIGNING_DOMAIN + b":twilio-auth-token-fallback",
        hashlib.sha256,
    ).digest()


def voice_stream_envelope_ttl_seconds(
    config: Mapping[str, Any] | None = None,
) -> int:
    raw = _configured_value(config, "VOICE_STREAM_ENVELOPE_TTL_SECONDS")
    try:
        ttl = int(raw) if raw is not None else VOICE_STREAM_ENVELOPE_DEFAULT_TTL_SECONDS
    except (TypeError, ValueError) as exc:
        raise VoiceStreamEnvelopeError("invalid_ttl_configuration") from exc
    if ttl < 15 or ttl > VOICE_STREAM_ENVELOPE_MAX_TTL_SECONDS:
        raise VoiceStreamEnvelopeError("invalid_ttl_configuration")
    return ttl


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _replay_redis_url(config: Mapping[str, Any] | None) -> str | None:
    for name in (
        "VOICE_STREAM_REPLAY_REDIS_URL",
        "SOCKETIO_MESSAGE_QUEUE_URL",
        "SOCKETIO_REDIS_URL",
    ):
        value = _configured_value(config, name)
        if value:
            return value

    # Celery is safe to reuse under a separate key namespace only when the
    # operator explicitly supplied its URL.  Do not inherit Config's local
    # development default in production.
    explicit_celery_url = os.environ.get("CELERY_BROKER_URL")
    return str(explicit_celery_url).strip() if explicit_celery_url else None


def _replay_key(payload: Mapping[str, str]) -> str:
    identity = ":".join(
        (
            str(payload.get("version") or ""),
            str(payload.get("call_sid") or ""),
            str(payload.get("nonce") or ""),
        )
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{_REPLAY_KEY_PREFIX}:{digest}"


def _claim_test_replay_key(key: str, *, expires_at: int, now: int) -> bool:
    with _REPLAY_STATE_LOCK:
        expired = [
            stored_key
            for stored_key, expiry in _TEST_REPLAY_EXPIRATIONS.items()
            if expiry < now
        ]
        for stored_key in expired:
            _TEST_REPLAY_EXPIRATIONS.pop(stored_key, None)
        if key in _TEST_REPLAY_EXPIRATIONS:
            return False
        _TEST_REPLAY_EXPIRATIONS[key] = expires_at
        return True


def _redis_client(redis_url: str):
    parsed = urlparse(redis_url)
    if parsed.scheme.lower() not in {"redis", "rediss"} or not parsed.hostname:
        raise VoiceStreamEnvelopeError("replay_store_invalid_url")
    with _REPLAY_STATE_LOCK:
        client = _REDIS_CLIENTS.get(redis_url)
        if client is None:
            from redis import Redis

            client = Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
            _REDIS_CLIENTS[redis_url] = client
        return client


def consume_voice_stream_envelope_once(
    payload: Mapping[str, str],
    *,
    config: Mapping[str, Any] | None = None,
    now: float | int | None = None,
) -> None:
    """Atomically consume a verified envelope across backend workers.

    Production requires a shared Redis store and fails closed when it is
    absent or unavailable.  A process-local store exists only behind an
    explicit TESTING-only switch so unit tests never imply a distributed
    replay guarantee.
    """

    current_time = int(time.time() if now is None else now)
    ttl = voice_stream_envelope_ttl_seconds(config)
    try:
        signed_at = int(payload.get("ts") or 0)
    except (TypeError, ValueError) as exc:
        raise VoiceStreamEnvelopeError("invalid_timestamp") from exc
    expires_at = signed_at + ttl + VOICE_STREAM_ENVELOPE_FUTURE_SKEW_SECONDS
    remaining_ttl = max(1, expires_at - current_time)
    key = _replay_key(payload)

    testing = _truthy(config.get("TESTING")) if config is not None else False
    allow_test_store = (
        _truthy(config.get("VOICE_STREAM_REPLAY_ALLOW_IN_MEMORY_TEST_STORE"))
        if config is not None
        else False
    )
    if testing and allow_test_store:
        if not _claim_test_replay_key(key, expires_at=expires_at, now=current_time):
            raise VoiceStreamEnvelopeError("envelope_replayed")
        return

    redis_url = _replay_redis_url(config)
    if redis_url:
        try:
            claimed = bool(
                _redis_client(redis_url).set(
                    key,
                    "1",
                    nx=True,
                    ex=remaining_ttl,
                )
            )
        except VoiceStreamEnvelopeError:
            raise
        except Exception as exc:
            raise VoiceStreamEnvelopeError("replay_store_unavailable") from exc
        if not claimed:
            raise VoiceStreamEnvelopeError("envelope_replayed")
        return

    raise VoiceStreamEnvelopeError("replay_store_missing")


def _bounded_text(field: str, value: Any, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise VoiceStreamEnvelopeError(f"missing_{field}")
    if len(text) > _FIELD_LIMITS[field]:
        raise VoiceStreamEnvelopeError(f"invalid_{field}")
    return text


def _canonical_max_seconds(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise VoiceStreamEnvelopeError("invalid_max_call_seconds") from exc
    if seconds < 15 or seconds > 7200:
        raise VoiceStreamEnvelopeError("invalid_max_call_seconds")
    return str(seconds)


def _canonical_demo(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return "1"
    if normalized in {"", "0", "false", "no", "off"}:
        return "0"
    raise VoiceStreamEnvelopeError("invalid_demo")


def _canonical_payload(
    *,
    call_sid: Any,
    from_number: Any,
    to_number: Any,
    tenant_slug: Any = None,
    vertical: Any = None,
    intent: Any = None,
    chat_session_id: Any = None,
    demo: Any = False,
    max_call_seconds: Any = None,
    timestamp: Any,
    nonce: Any,
) -> dict[str, str]:
    try:
        ts = str(int(timestamp))
    except (TypeError, ValueError) as exc:
        raise VoiceStreamEnvelopeError("invalid_timestamp") from exc
    nonce_text = str(nonce or "").strip()
    if not _NONCE_RE.fullmatch(nonce_text):
        raise VoiceStreamEnvelopeError("invalid_nonce")

    return {
        "version": VOICE_STREAM_ENVELOPE_VERSION,
        "call_sid": _bounded_text("call_sid", call_sid, required=True),
        "from_number": _bounded_text("from_number", from_number, required=True),
        "to_number": _bounded_text("to_number", to_number, required=True),
        "tenant_slug": _bounded_text("tenant_slug", tenant_slug),
        "vertical": _bounded_text("vertical", vertical),
        "intent": _bounded_text("intent", intent),
        "chat_session_id": _bounded_text("chat_session_id", chat_session_id),
        "demo": _canonical_demo(demo),
        "max_call_seconds": _canonical_max_seconds(max_call_seconds),
        "ts": ts,
        "nonce": nonce_text,
    }


def _serialized_payload(payload: Mapping[str, str]) -> bytes:
    canonical = {field: str(payload.get(field) or "") for field in _SIGNED_FIELDS}
    return json.dumps(
        canonical,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _signature(payload: Mapping[str, str], key: bytes) -> str:
    return hmac.new(key, _serialized_payload(payload), hashlib.sha256).hexdigest()


def create_voice_stream_envelope(
    *,
    call_sid: Any,
    from_number: Any,
    to_number: Any,
    tenant_slug: Any = None,
    vertical: Any = None,
    intent: Any = None,
    chat_session_id: Any = None,
    demo: Any = False,
    max_call_seconds: Any = None,
    config: Mapping[str, Any] | None = None,
    now: float | int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    key = resolve_voice_stream_signing_key(config)
    if key is None:
        raise VoiceStreamEnvelopeError("signing_secret_missing")
    timestamp = int(time.time() if now is None else now)
    payload = _canonical_payload(
        call_sid=call_sid,
        from_number=from_number,
        to_number=to_number,
        tenant_slug=tenant_slug,
        vertical=vertical,
        intent=intent,
        chat_session_id=chat_session_id,
        demo=demo,
        max_call_seconds=max_call_seconds,
        timestamp=timestamp,
        nonce=nonce or secrets.token_urlsafe(18),
    )
    return {
        **payload,
        "signature": _signature(payload, key),
    }


def _consistent_alias(custom: Mapping[str, Any], primary: str, alias: str) -> None:
    primary_value = str(custom.get(primary) or "").strip()
    alias_value = str(custom.get(alias) or "").strip()
    if primary_value and alias_value and primary_value != alias_value:
        raise VoiceStreamEnvelopeError(f"inconsistent_{primary}")


def _normalized_transport_phone(value: Any) -> str:
    return str(value or "").replace("whatsapp:", "").replace(" ", "").strip()


def verify_voice_stream_envelope(
    custom_parameters: Mapping[str, Any] | None,
    *,
    start: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
    now: float | int | None = None,
) -> dict[str, str]:
    if not isinstance(custom_parameters, Mapping) or not isinstance(start, Mapping):
        raise VoiceStreamEnvelopeError("invalid_start_payload")

    custom = custom_parameters
    if str(custom.get("version") or "").strip() != VOICE_STREAM_ENVELOPE_VERSION:
        raise VoiceStreamEnvelopeError("unsupported_version")
    _consistent_alias(custom, "tenant_slug", "tenant")
    _consistent_alias(custom, "vertical", "sector")
    _consistent_alias(custom, "chat_session_id", "source_chat_session_id")

    signature = str(custom.get("signature") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise VoiceStreamEnvelopeError("invalid_signature")

    payload = _canonical_payload(
        call_sid=custom.get("call_sid"),
        from_number=custom.get("from_number"),
        to_number=custom.get("to_number"),
        tenant_slug=custom.get("tenant_slug") or custom.get("tenant"),
        vertical=custom.get("vertical") or custom.get("sector"),
        intent=custom.get("intent"),
        chat_session_id=custom.get("chat_session_id") or custom.get("source_chat_session_id"),
        demo=custom.get("demo"),
        max_call_seconds=custom.get("max_call_seconds"),
        timestamp=custom.get("ts"),
        nonce=custom.get("nonce"),
    )
    key = resolve_voice_stream_signing_key(config)
    if key is None:
        raise VoiceStreamEnvelopeError("signing_secret_missing")
    if not hmac.compare_digest(signature, _signature(payload, key)):
        raise VoiceStreamEnvelopeError("signature_mismatch")

    current_time = int(time.time() if now is None else now)
    timestamp = int(payload["ts"])
    ttl = voice_stream_envelope_ttl_seconds(config)
    if timestamp > current_time + VOICE_STREAM_ENVELOPE_FUTURE_SKEW_SECONDS:
        raise VoiceStreamEnvelopeError("timestamp_in_future")
    if current_time - timestamp > ttl:
        raise VoiceStreamEnvelopeError("envelope_expired")

    start_call_sid = str(start.get("callSid") or "").strip()
    stream_sid = str(start.get("streamSid") or "").strip()
    if not start_call_sid or not stream_sid:
        raise VoiceStreamEnvelopeError("missing_start_identity")
    if start_call_sid != payload["call_sid"]:
        raise VoiceStreamEnvelopeError("inconsistent_call_sid")

    for canonical_field, start_names in (
        ("from_number", ("from", "From")),
        ("to_number", ("to", "To")),
    ):
        start_value = next((start.get(name) for name in start_names if start.get(name)), None)
        if start_value and _normalized_transport_phone(start_value) != _normalized_transport_phone(
            payload[canonical_field]
        ):
            raise VoiceStreamEnvelopeError(f"inconsistent_{canonical_field}")

    return payload
