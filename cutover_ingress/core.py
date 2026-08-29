"""Cryptographic persistence and replay primitives for cutover ingress.

Security properties:

* the configured database is independent from the primary application DB;
* the complete signed form is encrypted with AES-256-GCM before persistence;
* immutable envelope metadata and ciphertext have a separate SHA-256 HMAC;
* ``MessageSid`` is the database primary key and conflicting replays fail;
* workers use expiring, HMAC-bound compare-and-swap leases;
* target delivery is at-least-once and relies on the existing target
  ``MessageSid`` idempotency boundary after a crash between target commit and
  buffer completion.

Nothing in this module sends provider messages or invokes LLM/business logic.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
import re
import secrets
from types import MappingProxyType
from typing import Any, Optional
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Engine, and_, create_engine, exists, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

from .migration import assert_cutover_ingress_schema
from .schema import (
    TWILIO_IDEMPOTENCY_HMAC_VERSION,
    buffered_whatsapp_ingress,
    twilio_idempotency_evidence,
)


CONTRACT_VERSION = "chatboc.cutover_whatsapp_ingress.v1"
PROVIDER = "twilio"
STATUS_BUFFERED = "buffered"
STATUS_REPLAYING = "replaying"
STATUS_RETRY_WAIT = "retry_wait"
STATUS_REPLAYED = "replayed"
STATUS_DEAD = "dead"
NONTERMINAL_STATUSES = (STATUS_BUFFERED, STATUS_REPLAYING, STATUS_RETRY_WAIT)

DEFAULT_MAX_PAYLOAD_BYTES = 64 * 1024
DEFAULT_MAX_ATTEMPTS = 12
DEFAULT_LEASE_SECONDS = 120
MAX_FORM_FIELDS = 128
MAX_FIELD_BYTES = 16 * 1024
MAX_IDEMPOTENCY_TOKEN_BYTES = 512
_IDEMPOTENCY_TOKEN_HMAC_DOMAIN = (
    b"chatboc.cutover-ingress.twilio-idempotency-token:hmac-sha256.v1:\0"
)

_ACCOUNT_SID = re.compile(r"^AC[0-9a-fA-F]{32}$")
_MESSAGE_SID = re.compile(r"^SM[0-9a-fA-F]{32}$")
_WHATSAPP_ADDRESS = re.compile(r"^whatsapp:\+[1-9][0-9]{7,14}$", re.I)
_KEY_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,48}$")
_FORM_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
_SAFE_ERROR = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,95}$")
_TWILIO_IDEMPOTENCY_TOKEN = re.compile(r"^[\x21-\x7e]{1,512}$")


class CutoverIngressConfigurationError(RuntimeError):
    pass


class CutoverIngressConflict(RuntimeError):
    pass


class CutoverIngressIntegrityError(RuntimeError):
    pass


class CutoverIngressStateError(RuntimeError):
    pass


class CutoverIngressReplayError(RuntimeError):
    pass


def _utc(value: Optional[datetime] = None) -> datetime:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        return resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _decode_b64_secret(value: Any, *, code: str, exact: int | None = None) -> bytes:
    raw = str(value or "").strip()
    if not raw:
        raise CutoverIngressConfigurationError(code)
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise CutoverIngressConfigurationError(code) from exc
    if exact is not None and len(decoded) != exact:
        raise CutoverIngressConfigurationError(code)
    if exact is None and len(decoded) < 32:
        raise CutoverIngressConfigurationError(code)
    return decoded


_UNSAFE_DATABASE_TARGET_QUERY_KEYS = {
    "database",
    "dbname",
    "host",
    "hostaddr",
    "options",
    "port",
    "search_path",
    "service",
    "target_session_attrs",
}


def _strict_database_url(value: str):
    try:
        url = make_url(value)
    except Exception as exc:
        raise CutoverIngressConfigurationError(
            "cutover_ingress_database_url_invalid"
        ) from exc
    query_keys = {str(key).lower() for key in url.query}
    if query_keys & _UNSAFE_DATABASE_TARGET_QUERY_KEYS:
        raise CutoverIngressConfigurationError(
            "cutover_ingress_database_target_options_forbidden"
        )
    return url


def _database_identity(value: str) -> tuple[str, str, int, str]:
    url = _strict_database_url(value)
    backend = url.get_backend_name().lower()
    if backend == "sqlite":
        return backend, "", 0, os.path.abspath(str(url.database or ""))
    host = str(url.host or "").lower()
    # Neon uses sibling pooled/unpooled hostnames for the same branch.
    host = re.sub(r"-pooler(?=\.)", "", host)
    return backend, host, int(url.port or 5432), str(url.database or "")


def _normalize_database_driver(value: str) -> str:
    """Pin PostgreSQL URLs to the only driver shipped by the ingress image."""

    url = _strict_database_url(value)
    if url.get_backend_name().lower() == "postgresql" and url.drivername != "postgresql+psycopg":
        url = url.set(drivername="postgresql+psycopg")
    return url.render_as_string(hide_password=False)


def _require_separate_database(ingress_url: str, primary_url: str | None) -> None:
    if not primary_url:
        return
    try:
        ingress_identity = _database_identity(ingress_url)
        primary_identity = _database_identity(primary_url)
    except Exception as exc:
        raise CutoverIngressConfigurationError(
            "cutover_ingress_database_url_invalid"
        ) from exc
    if ingress_identity == primary_identity:
        raise CutoverIngressConfigurationError(
            "cutover_ingress_database_must_be_separate"
        )


def _require_runtime_postgresql_database(ingress_url: str) -> None:
    """Require a durable TLS PostgreSQL database for the public runtime.

    SQLite remains useful for explicitly injected unit-test factories, but an
    environment-driven WSGI process must never be able to acknowledge a
    provider webhook into an ephemeral filesystem.  A Neon pooled endpoint is
    valid for ordinary transactions and partial unique indexes and prevents
    Fluid Compute instances from multiplying direct backend connections.
    """

    url = _strict_database_url(ingress_url)
    if url.get_backend_name().lower() != "postgresql":
        raise CutoverIngressConfigurationError(
            "cutover_ingress_runtime_database_must_be_postgresql"
        )
    host = str(url.host or "").strip().lower()
    if not host or not str(url.database or "").strip() or not str(url.username or "").strip():
        raise CutoverIngressConfigurationError(
            "cutover_ingress_database_url_invalid"
        )
    if not re.search(r"(?:^|[-.])pooler(?=\.|$)", host):
        raise CutoverIngressConfigurationError(
            "cutover_ingress_runtime_database_must_be_pooled"
        )
    sslmode = str(url.query.get("sslmode") or "").strip().lower()
    if sslmode not in {"require", "verify-ca", "verify-full"}:
        raise CutoverIngressConfigurationError(
            "cutover_ingress_database_tls_required"
        )


@dataclass(frozen=True)
class CutoverIngressSettings:
    database_url: str
    public_webhook_url: str
    twilio_auth_token: str
    twilio_account_sid: str
    expected_to: str
    tenant_id: int
    stream_hash_secret: bytes
    active_encryption_key_id: str
    encryption_keys: Mapping[str, bytes]
    envelope_hmac_key: bytes
    idempotency_token_hmac_key: bytes | None = None
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    lease_seconds: int = DEFAULT_LEASE_SECONDS

    def __post_init__(self) -> None:
        database_url = str(self.database_url or "").strip()
        _strict_database_url(database_url)
        parts = urlsplit(str(self.public_webhook_url or "").strip())
        if (
            parts.scheme != "https"
            or not parts.netloc
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.port is not None
            or parts.hostname != parts.hostname.lower()
            or parts.netloc != parts.hostname
            or ":" in parts.hostname
            or parts.query
            or parts.fragment
            or not parts.path
        ):
            raise CutoverIngressConfigurationError(
                "cutover_ingress_public_webhook_url_invalid"
            )
        if len(str(self.twilio_auth_token or "").encode("utf-8")) < 16:
            raise CutoverIngressConfigurationError(
                "cutover_ingress_twilio_auth_token_invalid"
            )
        if not _ACCOUNT_SID.fullmatch(str(self.twilio_account_sid or "")):
            raise CutoverIngressConfigurationError(
                "cutover_ingress_twilio_account_sid_invalid"
            )
        if not _WHATSAPP_ADDRESS.fullmatch(str(self.expected_to or "")):
            raise CutoverIngressConfigurationError(
                "cutover_ingress_expected_to_invalid"
            )
        if not isinstance(self.tenant_id, int) or isinstance(self.tenant_id, bool) or self.tenant_id < 1:
            raise CutoverIngressConfigurationError(
                "cutover_ingress_tenant_id_invalid"
            )
        if len(bytes(self.stream_hash_secret or b"")) < 32:
            raise CutoverIngressConfigurationError(
                "cutover_ingress_stream_hash_secret_invalid"
            )
        if len(bytes(self.envelope_hmac_key or b"")) < 32:
            raise CutoverIngressConfigurationError(
                "cutover_ingress_hmac_key_invalid"
            )
        key_id = str(self.active_encryption_key_id or "")
        if not _KEY_ID.fullmatch(key_id):
            raise CutoverIngressConfigurationError(
                "cutover_ingress_active_key_id_invalid"
            )
        keys = dict(self.encryption_keys or {})
        if key_id not in keys or any(
            not _KEY_ID.fullmatch(str(candidate)) or len(bytes(key)) != 32
            for candidate, key in keys.items()
        ):
            raise CutoverIngressConfigurationError(
                "cutover_ingress_encryption_keys_invalid"
            )
        if any(
            hmac.compare_digest(bytes(key), bytes(self.envelope_hmac_key))
            or hmac.compare_digest(bytes(key), bytes(self.stream_hash_secret))
            for key in keys.values()
        ) or hmac.compare_digest(
            bytes(self.envelope_hmac_key), bytes(self.stream_hash_secret)
        ):
            raise CutoverIngressConfigurationError(
                "cutover_ingress_cryptographic_keys_not_separated"
            )
        idempotency_key = (
            bytes(self.idempotency_token_hmac_key)
            if self.idempotency_token_hmac_key is not None
            else None
        )
        if idempotency_key is not None:
            if len(idempotency_key) < 32:
                raise CutoverIngressConfigurationError(
                    "cutover_ingress_idempotency_token_hmac_key_invalid"
                )
            separated_values = (
                bytes(self.envelope_hmac_key),
                bytes(self.stream_hash_secret),
                str(self.twilio_auth_token).encode("utf-8"),
                *tuple(bytes(key) for key in keys.values()),
            )
            if any(
                hmac.compare_digest(idempotency_key, candidate)
                for candidate in separated_values
            ):
                raise CutoverIngressConfigurationError(
                    "cutover_ingress_cryptographic_keys_not_separated"
                )
        if not 1024 <= int(self.max_payload_bytes) <= 1024 * 1024:
            raise CutoverIngressConfigurationError(
                "cutover_ingress_max_payload_bytes_invalid"
            )
        if not 1 <= int(self.max_attempts) <= 100:
            raise CutoverIngressConfigurationError(
                "cutover_ingress_max_attempts_invalid"
            )
        if not 15 <= int(self.lease_seconds) <= 900:
            raise CutoverIngressConfigurationError(
                "cutover_ingress_lease_seconds_invalid"
            )
        object.__setattr__(
            self,
            "database_url",
            _normalize_database_driver(database_url),
        )
        object.__setattr__(self, "public_webhook_url", parts.geturl())
        object.__setattr__(self, "expected_to", self.expected_to.lower())
        object.__setattr__(self, "encryption_keys", MappingProxyType(keys))
        object.__setattr__(self, "idempotency_token_hmac_key", idempotency_key)

    @classmethod
    def from_environ(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "CutoverIngressSettings":
        env = os.environ if environ is None else environ
        ingress_url = str(env.get("CUTOVER_INGRESS_DATABASE_URL") or "").strip()
        if not ingress_url:
            raise CutoverIngressConfigurationError(
                "cutover_ingress_database_url_missing"
            )
        _require_runtime_postgresql_database(ingress_url)
        for primary_name in (
            "DATABASE_URL",
            "SQLALCHEMY_DATABASE_URI",
            "MIGRATIONS_DATABASE_URL",
        ):
            _require_separate_database(ingress_url, env.get(primary_name))
        try:
            raw_keys = json.loads(
                str(env.get("CUTOVER_INGRESS_ENCRYPTION_KEYS_JSON") or "")
            )
        except json.JSONDecodeError as exc:
            raise CutoverIngressConfigurationError(
                "cutover_ingress_encryption_keys_invalid"
            ) from exc
        if not isinstance(raw_keys, dict) or not raw_keys:
            raise CutoverIngressConfigurationError(
                "cutover_ingress_encryption_keys_invalid"
            )
        decoded_keys = {
            str(key_id): _decode_b64_secret(
                value,
                code="cutover_ingress_encryption_keys_invalid",
                exact=32,
            )
            for key_id, value in raw_keys.items()
        }
        # Use the canonical backend secret name.  A second independently named
        # secret can silently split FIFO ordering between buffered replay and
        # direct post-cutover webhook delivery.
        raw_stream_secret = str(env.get("WHATSAPP_INBOUND_HASH_SECRET") or "")
        stream_secret = raw_stream_secret.encode("utf-8")
        if len(stream_secret) < 32:
            raise CutoverIngressConfigurationError(
                "cutover_ingress_stream_hash_secret_invalid"
            )
        configured_fingerprint = str(
            env.get("CUTOVER_INGRESS_STREAM_SECRET_SHA256") or ""
        ).strip().lower()
        calculated_fingerprint = hashlib.sha256(stream_secret).hexdigest()
        if not re.fullmatch(
            r"[0-9a-f]{64}", configured_fingerprint
        ) or not hmac.compare_digest(
            configured_fingerprint,
            calculated_fingerprint,
        ):
            raise CutoverIngressConfigurationError(
                "cutover_ingress_stream_secret_fingerprint_mismatch"
            )
        try:
            tenant_id = int(str(env.get("CUTOVER_INGRESS_TENANT_ID") or ""))
            max_payload = int(
                env.get("CUTOVER_INGRESS_MAX_PAYLOAD_BYTES")
                or DEFAULT_MAX_PAYLOAD_BYTES
            )
            max_attempts = int(
                env.get("CUTOVER_INGRESS_MAX_ATTEMPTS") or DEFAULT_MAX_ATTEMPTS
            )
            lease_seconds = int(
                env.get("CUTOVER_INGRESS_LEASE_SECONDS") or DEFAULT_LEASE_SECONDS
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise CutoverIngressConfigurationError(
                "cutover_ingress_numeric_configuration_invalid"
            ) from exc
        return cls(
            database_url=ingress_url,
            public_webhook_url=str(
                env.get("CUTOVER_INGRESS_PUBLIC_WEBHOOK_URL") or ""
            ),
            twilio_auth_token=str(
                env.get("CUTOVER_INGRESS_TWILIO_AUTH_TOKEN") or ""
            ),
            twilio_account_sid=str(
                env.get("CUTOVER_INGRESS_TWILIO_ACCOUNT_SID") or ""
            ),
            expected_to=str(env.get("CUTOVER_INGRESS_EXPECTED_TO") or ""),
            tenant_id=tenant_id,
            stream_hash_secret=stream_secret,
            active_encryption_key_id=str(
                env.get("CUTOVER_INGRESS_ACTIVE_ENCRYPTION_KEY_ID") or ""
            ),
            encryption_keys=decoded_keys,
            envelope_hmac_key=_decode_b64_secret(
                env.get("CUTOVER_INGRESS_HMAC_KEY_B64"),
                code="cutover_ingress_hmac_key_invalid",
            ),
            idempotency_token_hmac_key=(
                _decode_b64_secret(
                    env.get("CUTOVER_INGRESS_IDEMPOTENCY_TOKEN_HMAC_KEY_B64"),
                    code="cutover_ingress_idempotency_token_hmac_key_invalid",
                )
                if str(
                    env.get("CUTOVER_INGRESS_IDEMPOTENCY_TOKEN_HMAC_KEY_B64")
                    or ""
                ).strip()
                else None
            ),
            max_payload_bytes=max_payload,
            max_attempts=max_attempts,
            lease_seconds=lease_seconds,
        )

    def build_engine(self) -> Engine:
        options: dict[str, Any] = {"future": True}
        if self.database_url.startswith("sqlite"):
            options.update(
                pool_pre_ping=True,
                connect_args={"check_same_thread": False, "timeout": 5},
            )
        else:
            # Neon/PgBouncer owns the reusable pool. NullPool avoids stacking a
            # persistent SQLAlchemy pool in every autoscaled Vercel instance.
            options.update(pool_pre_ping=False, poolclass=NullPool)
        return create_engine(self.database_url, **options)

    @property
    def stream_secret_sha256(self) -> str:
        """Redacted equality fingerprint shared with the target runtime."""

        return hashlib.sha256(self.stream_hash_secret).hexdigest()


@dataclass(frozen=True)
class BufferedIngressReceipt:
    outcome: str
    message_sid: str
    tenant_id: int
    stream_key: str
    payload_digest: str

    @property
    def created(self) -> bool:
        return self.outcome == "created"


@dataclass(frozen=True)
class BufferedIngressClaim:
    message_sid: str
    tenant_id: int
    stream_key: str
    payload: dict[str, str]
    payload_digest: str
    received_at: datetime
    attempt_count: int
    lease_token: str
    lease_expires_at: datetime
    claim_hmac: str


def normalize_signed_form(
    payload: Mapping[str, Any],
    *,
    max_payload_bytes: int,
) -> tuple[dict[str, str], bytes, str]:
    if not isinstance(payload, Mapping) or not payload:
        raise CutoverIngressIntegrityError("signed_form_invalid")
    if len(payload) > MAX_FORM_FIELDS:
        raise CutoverIngressIntegrityError("signed_form_too_many_fields")
    normalized: dict[str, str] = {}
    for key, value in payload.items():
        name = str(key or "")
        if not _FORM_KEY.fullmatch(name):
            raise CutoverIngressIntegrityError("signed_form_field_invalid")
        if isinstance(value, (list, tuple, dict, set)):
            raise CutoverIngressIntegrityError("signed_form_duplicate_field")
        text_value = str(value or "")
        if len(text_value.encode("utf-8")) > MAX_FIELD_BYTES:
            raise CutoverIngressIntegrityError("signed_form_field_too_large")
        normalized[name] = text_value
    encoded = _canonical_json(normalized)
    if len(encoded) > max_payload_bytes:
        raise CutoverIngressIntegrityError("signed_form_too_large")
    return normalized, encoded, hashlib.sha256(encoded).hexdigest()


def _derive_stream_key(
    *, secret: bytes, tenant_id: int, sender: str, destination: str
) -> str:
    material = (
        f"whatsapp-stream:v1:{tenant_id}:{sender.lower()}:{destination.lower()}"
    ).encode("utf-8")
    return hmac.new(secret, material, hashlib.sha256).hexdigest()


def _aad(row: Mapping[str, Any]) -> bytes:
    return _canonical_json(
        {
            "account_sid": str(row["account_sid"]),
            "contract_version": str(row["contract_version"]),
            "message_sid": str(row["message_sid"]),
            "payload_digest": str(row["payload_digest"]),
            "provider": str(row["provider"]),
            "stream_key": str(row["stream_key"]),
            "tenant_id": int(row["tenant_id"]),
        }
    )


def _envelope_mac_input(row: Mapping[str, Any]) -> bytes:
    return _canonical_json(
        {
            "aad_sha256": hashlib.sha256(_aad(row)).hexdigest(),
            "ciphertext": base64.b64encode(bytes(row["ciphertext"])).decode("ascii"),
            "encryption_key_id": str(row["encryption_key_id"]),
            "nonce": base64.b64encode(bytes(row["nonce"])).decode("ascii"),
        }
    )


def _safe_error_code(value: Any) -> str:
    code = str(value or "target_ingest_failed")
    return code if _SAFE_ERROR.fullmatch(code) else "target_ingest_failed"


class CutoverIngressStore:
    def __init__(self, engine: Engine, settings: CutoverIngressSettings):
        self.engine = engine
        self.settings = settings
        assert_cutover_ingress_schema(engine)

    def _encrypt(self, *, identity: dict[str, Any], plaintext: bytes) -> dict[str, Any]:
        key_id = self.settings.active_encryption_key_id
        nonce = secrets.token_bytes(12)
        envelope = dict(identity)
        envelope.update(
            encryption_key_id=key_id,
            nonce=nonce,
        )
        ciphertext = AESGCM(self.settings.encryption_keys[key_id]).encrypt(
            nonce,
            plaintext,
            _aad(envelope),
        )
        envelope["ciphertext"] = ciphertext
        envelope["envelope_hmac"] = hmac.new(
            self.settings.envelope_hmac_key,
            _envelope_mac_input(envelope),
            hashlib.sha256,
        ).hexdigest()
        return envelope

    def _decrypt(self, row: Mapping[str, Any]) -> dict[str, str]:
        expected_mac = hmac.new(
            self.settings.envelope_hmac_key,
            _envelope_mac_input(row),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected_mac, str(row["envelope_hmac"])):
            raise CutoverIngressIntegrityError("encrypted_envelope_hmac_invalid")
        key = self.settings.encryption_keys.get(str(row["encryption_key_id"]))
        if key is None:
            raise CutoverIngressIntegrityError("encrypted_envelope_key_unavailable")
        try:
            plaintext = AESGCM(key).decrypt(
                bytes(row["nonce"]),
                bytes(row["ciphertext"]),
                _aad(row),
            )
            payload = json.loads(plaintext.decode("utf-8"))
        except Exception as exc:
            raise CutoverIngressIntegrityError(
                "encrypted_envelope_decryption_failed"
            ) from exc
        normalized, encoded, digest = normalize_signed_form(
            payload,
            max_payload_bytes=self.settings.max_payload_bytes,
        )
        if not hmac.compare_digest(digest, str(row["payload_digest"])):
            raise CutoverIngressIntegrityError("encrypted_payload_digest_invalid")
        # Canonical re-encoding also rejects JSON tricks with non-string values.
        if not hmac.compare_digest(encoded, _canonical_json(normalized)):
            raise CutoverIngressIntegrityError("encrypted_payload_canonical_invalid")
        return normalized

    def _idempotency_token_evidence(
        self,
        token: str | None,
    ) -> tuple[str, str] | None:
        if token is None:
            return None
        key = self.settings.idempotency_token_hmac_key
        if key is None:
            raise CutoverIngressConfigurationError(
                "cutover_ingress_idempotency_token_hmac_key_missing"
            )
        rendered = str(token)
        encoded = rendered.encode("utf-8")
        if (
            len(encoded) > MAX_IDEMPOTENCY_TOKEN_BYTES
            or not _TWILIO_IDEMPOTENCY_TOKEN.fullmatch(rendered)
        ):
            raise CutoverIngressIntegrityError(
                "twilio_idempotency_token_invalid"
            )
        digest = hmac.new(
            key,
            _IDEMPOTENCY_TOKEN_HMAC_DOMAIN + encoded,
            hashlib.sha256,
        ).hexdigest()
        return TWILIO_IDEMPOTENCY_HMAC_VERSION, digest

    def _record_idempotency_token_evidence(
        self,
        connection,
        *,
        message_sid: str,
        evidence: tuple[str, str] | None,
        now: datetime,
    ) -> None:
        if evidence is None:
            return
        version, digest = evidence
        values = {
            "message_sid": message_sid,
            "hmac_version": version,
            "token_hmac": digest,
            "first_observed_at": now,
            "last_observed_at": now,
            "observation_count": 1,
        }
        index_elements = [
            twilio_idempotency_evidence.c.message_sid,
            twilio_idempotency_evidence.c.hmac_version,
            twilio_idempotency_evidence.c.token_hmac,
        ]
        if connection.dialect.name == "postgresql":
            statement = postgresql_insert(twilio_idempotency_evidence).values(
                **values
            )
        elif connection.dialect.name == "sqlite":
            statement = sqlite_insert(twilio_idempotency_evidence).values(**values)
        else:
            raise CutoverIngressConfigurationError(
                "cutover_ingress_idempotency_evidence_database_unsupported"
            )
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=index_elements,
                set_={
                    "last_observed_at": now,
                    "observation_count": (
                        twilio_idempotency_evidence.c.observation_count + 1
                    ),
                },
            )
        )

    def persist(
        self,
        payload: Mapping[str, Any],
        *,
        idempotency_token: str | None = None,
        now: datetime | None = None,
    ) -> BufferedIngressReceipt:
        normalized, encoded, digest = normalize_signed_form(
            payload,
            max_payload_bytes=self.settings.max_payload_bytes,
        )
        message_sid = normalized.get("MessageSid") or normalized.get("SmsMessageSid") or ""
        account_sid = normalized.get("AccountSid") or ""
        sender = normalized.get("From") or ""
        destination = normalized.get("To") or ""
        if not _MESSAGE_SID.fullmatch(message_sid):
            raise CutoverIngressIntegrityError("message_sid_invalid")
        if not _ACCOUNT_SID.fullmatch(account_sid):
            raise CutoverIngressIntegrityError("account_sid_invalid")
        if not hmac.compare_digest(account_sid, self.settings.twilio_account_sid):
            raise CutoverIngressIntegrityError("account_sid_mismatch")
        if not _WHATSAPP_ADDRESS.fullmatch(sender):
            raise CutoverIngressIntegrityError("sender_invalid")
        if not _WHATSAPP_ADDRESS.fullmatch(destination):
            raise CutoverIngressIntegrityError("destination_invalid")
        if not hmac.compare_digest(destination.lower(), self.settings.expected_to):
            raise CutoverIngressIntegrityError("destination_mismatch")
        idempotency_evidence = self._idempotency_token_evidence(
            idempotency_token
        )
        stream_key = _derive_stream_key(
            secret=self.settings.stream_hash_secret,
            tenant_id=self.settings.tenant_id,
            sender=sender,
            destination=destination,
        )
        operation_now = _utc(now)
        identity = {
            "provider": PROVIDER,
            "account_sid": account_sid,
            "message_sid": message_sid,
            "tenant_id": self.settings.tenant_id,
            "stream_key": stream_key,
            "payload_digest": digest,
            "contract_version": CONTRACT_VERSION,
        }
        envelope = self._encrypt(identity=identity, plaintext=encoded)
        values = {
            **identity,
            **{
                key: envelope[key]
                for key in (
                    "encryption_key_id",
                    "nonce",
                    "ciphertext",
                    "envelope_hmac",
                )
            },
            "status": STATUS_BUFFERED,
            "attempt_count": 0,
            "max_attempts": self.settings.max_attempts,
            "available_at": operation_now,
            "received_at": operation_now,
            "row_version": 1,
            "created_at": operation_now,
            "updated_at": operation_now,
        }
        try:
            with self.engine.begin() as connection:
                connection.execute(buffered_whatsapp_ingress.insert().values(**values))
                self._record_idempotency_token_evidence(
                    connection,
                    message_sid=message_sid,
                    evidence=idempotency_evidence,
                    now=operation_now,
                )
        except IntegrityError:
            with self.engine.begin() as connection:
                existing = connection.execute(
                    select(buffered_whatsapp_ingress).where(
                        buffered_whatsapp_ingress.c.message_sid == message_sid
                    )
                ).mappings().one_or_none()
                if existing is None:
                    raise
                immutable_match = (
                    hmac.compare_digest(str(existing["payload_digest"]), digest)
                    and hmac.compare_digest(str(existing["account_sid"]), account_sid)
                    and int(existing["tenant_id"]) == self.settings.tenant_id
                    and hmac.compare_digest(str(existing["stream_key"]), stream_key)
                )
                if not immutable_match:
                    raise CutoverIngressConflict(
                        "message_sid_payload_conflict"
                    ) from None
                # Verify the persisted envelope as well; an exact provider
                # replay must not hide at-rest corruption. Evidence is recorded
                # only after that verification and in the same transaction.
                self._decrypt(existing)
                self._record_idempotency_token_evidence(
                    connection,
                    message_sid=message_sid,
                    evidence=idempotency_evidence,
                    now=operation_now,
                )
            return BufferedIngressReceipt(
                outcome="duplicate",
                message_sid=message_sid,
                tenant_id=self.settings.tenant_id,
                stream_key=stream_key,
                payload_digest=digest,
            )
        return BufferedIngressReceipt(
            outcome="created",
            message_sid=message_sid,
            tenant_id=self.settings.tenant_id,
            stream_key=stream_key,
            payload_digest=digest,
        )

    def _lease_token_hmac(self, token: str) -> str:
        return hmac.new(
            self.settings.envelope_hmac_key,
            f"cutover-ingress-lease:v1:{token}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _claim_hmac(
        self,
        *,
        message_sid: str,
        payload_digest: str,
        attempt_count: int,
        lease_token: str,
        lease_expires_at: datetime,
    ) -> str:
        material = _canonical_json(
            {
                "attempt_count": attempt_count,
                "lease_expires_at": _utc(lease_expires_at).isoformat(),
                "lease_token": lease_token,
                "message_sid": message_sid,
                "payload_digest": payload_digest,
            }
        )
        return hmac.new(
            self.settings.envelope_hmac_key,
            b"cutover-ingress-claim:v1:" + material,
            hashlib.sha256,
        ).hexdigest()

    def _candidate_rows(self, now: datetime) -> list[Mapping[str, Any]]:
        due = or_(
            and_(
                buffered_whatsapp_ingress.c.status.in_(
                    (STATUS_BUFFERED, STATUS_RETRY_WAIT)
                ),
                buffered_whatsapp_ingress.c.available_at <= now,
            ),
            and_(
                buffered_whatsapp_ingress.c.status == STATUS_REPLAYING,
                buffered_whatsapp_ingress.c.lease_expires_at <= now,
            ),
        )
        with self.engine.connect() as connection:
            return list(
                connection.execute(
                    select(buffered_whatsapp_ingress)
                    .where(
                        due,
                        buffered_whatsapp_ingress.c.attempt_count
                        < buffered_whatsapp_ingress.c.max_attempts,
                    )
                    .order_by(
                        buffered_whatsapp_ingress.c.received_at,
                        buffered_whatsapp_ingress.c.message_sid,
                    )
                    .limit(50)
                ).mappings()
            )

    def _quarantine_corrupt(self, row: Mapping[str, Any], *, now: datetime) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(buffered_whatsapp_ingress)
                .where(
                    buffered_whatsapp_ingress.c.message_sid == row["message_sid"],
                    buffered_whatsapp_ingress.c.row_version == row["row_version"],
                    buffered_whatsapp_ingress.c.status.in_(NONTERMINAL_STATUSES),
                )
                .values(
                    status=STATUS_DEAD,
                    lease_token_hmac=None,
                    lease_expires_at=None,
                    last_error_code="encrypted_envelope_invalid",
                    row_version=buffered_whatsapp_ingress.c.row_version + 1,
                    updated_at=now,
                )
            )

    def claim_next(self, *, now: datetime | None = None) -> BufferedIngressClaim | None:
        operation_now = _utc(now)
        # Expired final attempts are terminal; otherwise they would block their
        # FIFO stream forever.
        with self.engine.begin() as connection:
            connection.execute(
                update(buffered_whatsapp_ingress)
                .where(
                    buffered_whatsapp_ingress.c.status == STATUS_REPLAYING,
                    buffered_whatsapp_ingress.c.lease_expires_at <= operation_now,
                    buffered_whatsapp_ingress.c.attempt_count
                    >= buffered_whatsapp_ingress.c.max_attempts,
                )
                .values(
                    status=STATUS_DEAD,
                    lease_token_hmac=None,
                    lease_expires_at=None,
                    last_error_code="replay_attempts_exhausted",
                    row_version=buffered_whatsapp_ingress.c.row_version + 1,
                    updated_at=operation_now,
                )
            )

        for row in self._candidate_rows(operation_now):
            try:
                payload = self._decrypt(row)
            except CutoverIngressIntegrityError:
                self._quarantine_corrupt(row, now=operation_now)
                raise

            lease_token = secrets.token_urlsafe(32)
            lease_hash = self._lease_token_hmac(lease_token)
            lease_expires = operation_now + timedelta(
                seconds=self.settings.lease_seconds
            )
            older = buffered_whatsapp_ingress.alias("older_ingress")
            older_unfinished = exists(
                select(1).select_from(older).where(
                    older.c.tenant_id == int(row["tenant_id"]),
                    older.c.stream_key == str(row["stream_key"]),
                    older.c.status.in_(NONTERMINAL_STATUSES),
                    or_(
                        older.c.received_at < row["received_at"],
                        and_(
                            older.c.received_at == row["received_at"],
                            older.c.message_sid < str(row["message_sid"]),
                        ),
                    ),
                )
            )
            due = or_(
                and_(
                    buffered_whatsapp_ingress.c.status.in_(
                        (STATUS_BUFFERED, STATUS_RETRY_WAIT)
                    ),
                    buffered_whatsapp_ingress.c.available_at <= operation_now,
                ),
                and_(
                    buffered_whatsapp_ingress.c.status == STATUS_REPLAYING,
                    buffered_whatsapp_ingress.c.lease_expires_at <= operation_now,
                ),
            )
            try:
                with self.engine.begin() as connection:
                    result = connection.execute(
                        update(buffered_whatsapp_ingress)
                        .where(
                            buffered_whatsapp_ingress.c.message_sid
                            == row["message_sid"],
                            buffered_whatsapp_ingress.c.row_version
                            == row["row_version"],
                            buffered_whatsapp_ingress.c.attempt_count
                            < buffered_whatsapp_ingress.c.max_attempts,
                            due,
                            ~older_unfinished,
                        )
                        .values(
                            status=STATUS_REPLAYING,
                            attempt_count=buffered_whatsapp_ingress.c.attempt_count
                            + 1,
                            lease_token_hmac=lease_hash,
                            lease_expires_at=lease_expires,
                            last_error_code=None,
                            row_version=buffered_whatsapp_ingress.c.row_version + 1,
                            updated_at=operation_now,
                        )
                    )
                    if result.rowcount != 1:
                        continue
            except IntegrityError:
                # The partial unique stream lease closes the last race between
                # workers that selected adjacent rows concurrently.
                continue

            attempt_count = int(row["attempt_count"]) + 1
            claim_hmac = self._claim_hmac(
                message_sid=str(row["message_sid"]),
                payload_digest=str(row["payload_digest"]),
                attempt_count=attempt_count,
                lease_token=lease_token,
                lease_expires_at=lease_expires,
            )
            return BufferedIngressClaim(
                message_sid=str(row["message_sid"]),
                tenant_id=int(row["tenant_id"]),
                stream_key=str(row["stream_key"]),
                payload=payload,
                payload_digest=str(row["payload_digest"]),
                received_at=_utc(row["received_at"]),
                attempt_count=attempt_count,
                lease_token=lease_token,
                lease_expires_at=lease_expires,
                claim_hmac=claim_hmac,
            )
        return None

    def assert_claim(self, claim: BufferedIngressClaim, *, now: datetime | None = None) -> str:
        operation_now = _utc(now)
        expected_claim_hmac = self._claim_hmac(
            message_sid=claim.message_sid,
            payload_digest=claim.payload_digest,
            attempt_count=claim.attempt_count,
            lease_token=claim.lease_token,
            lease_expires_at=claim.lease_expires_at,
        )
        if not hmac.compare_digest(expected_claim_hmac, claim.claim_hmac):
            raise CutoverIngressIntegrityError("claim_hmac_invalid")
        if _utc(claim.lease_expires_at) <= operation_now:
            raise CutoverIngressStateError("claim_lease_expired")
        lease_hash = self._lease_token_hmac(claim.lease_token)
        with self.engine.connect() as connection:
            row = connection.execute(
                select(
                    buffered_whatsapp_ingress.c.status,
                    buffered_whatsapp_ingress.c.lease_token_hmac,
                    buffered_whatsapp_ingress.c.payload_digest,
                    buffered_whatsapp_ingress.c.attempt_count,
                ).where(
                    buffered_whatsapp_ingress.c.message_sid == claim.message_sid
                )
            ).mappings().one_or_none()
        if (
            row is None
            or row["status"] != STATUS_REPLAYING
            or not hmac.compare_digest(str(row["lease_token_hmac"] or ""), lease_hash)
            or not hmac.compare_digest(str(row["payload_digest"]), claim.payload_digest)
            or int(row["attempt_count"]) != claim.attempt_count
        ):
            raise CutoverIngressStateError("claim_lease_mismatch")
        return lease_hash

    def complete_claim(
        self,
        claim: BufferedIngressClaim,
        *,
        target_turn_id: Any,
        target_outcome: Any,
        now: datetime | None = None,
    ) -> None:
        operation_now = _utc(now)
        lease_hash = self.assert_claim(claim, now=operation_now)
        turn_id = str(target_turn_id or "").strip()
        outcome = str(target_outcome or "").strip().lower()
        if not re.fullmatch(r"[A-Za-z0-9-]{1,36}", turn_id):
            raise CutoverIngressIntegrityError("target_turn_id_invalid")
        if outcome not in {"created", "duplicate"}:
            raise CutoverIngressIntegrityError("target_outcome_invalid")
        with self.engine.begin() as connection:
            result = connection.execute(
                update(buffered_whatsapp_ingress)
                .where(
                    buffered_whatsapp_ingress.c.message_sid == claim.message_sid,
                    buffered_whatsapp_ingress.c.status == STATUS_REPLAYING,
                    buffered_whatsapp_ingress.c.lease_token_hmac == lease_hash,
                )
                .values(
                    status=STATUS_REPLAYED,
                    lease_token_hmac=None,
                    lease_expires_at=None,
                    replayed_at=operation_now,
                    target_turn_id=turn_id,
                    target_outcome=outcome,
                    last_error_code=None,
                    row_version=buffered_whatsapp_ingress.c.row_version + 1,
                    updated_at=operation_now,
                )
            )
            if result.rowcount != 1:
                raise CutoverIngressStateError("claim_completion_race")

    def retry_claim(
        self,
        claim: BufferedIngressClaim,
        *,
        error_code: str = "target_ingest_failed",
        now: datetime | None = None,
    ) -> None:
        operation_now = _utc(now)
        lease_hash = self.assert_claim(claim, now=operation_now)
        terminal = claim.attempt_count >= self.settings.max_attempts
        delay = min(3600, 15 * (2 ** max(0, claim.attempt_count - 1)))
        with self.engine.begin() as connection:
            result = connection.execute(
                update(buffered_whatsapp_ingress)
                .where(
                    buffered_whatsapp_ingress.c.message_sid == claim.message_sid,
                    buffered_whatsapp_ingress.c.status == STATUS_REPLAYING,
                    buffered_whatsapp_ingress.c.lease_token_hmac == lease_hash,
                )
                .values(
                    status=STATUS_DEAD if terminal else STATUS_RETRY_WAIT,
                    available_at=operation_now + timedelta(seconds=delay),
                    lease_token_hmac=None,
                    lease_expires_at=None,
                    last_error_code=_safe_error_code(error_code),
                    row_version=buffered_whatsapp_ingress.c.row_version + 1,
                    updated_at=operation_now,
                )
            )
            if result.rowcount != 1:
                raise CutoverIngressStateError("claim_retry_race")

    def get_record(self, message_sid: str) -> Mapping[str, Any] | None:
        """Testing/operations helper returning metadata and encrypted bytes only."""
        with self.engine.connect() as connection:
            return connection.execute(
                select(buffered_whatsapp_ingress).where(
                    buffered_whatsapp_ingress.c.message_sid == message_sid
                )
            ).mappings().one_or_none()


def _receipt_field(receipt: Any, name: str) -> Any:
    if isinstance(receipt, Mapping):
        return receipt.get(name)
    return getattr(receipt, name, None)


def replay_buffered_claim(
    store: CutoverIngressStore,
    claim: BufferedIngressClaim,
    ingest: Callable[..., Any],
    *,
    now: datetime | None = None,
) -> Any:
    """Replay one authenticated claim into the target durable intake only.

    The caller injects the persistence function.  This module never imports or
    invokes the webhook route, worker, provider transport, or responder.
    """

    operation_now = _utc(now)
    store.assert_claim(claim, now=operation_now)
    try:
        receipt = ingest(
            tenant_id=claim.tenant_id,
            provider=PROVIDER,
            provider_message_sid=claim.message_sid,
            stream_key=claim.stream_key,
            payload=claim.payload,
            received_at=claim.received_at,
            now=operation_now,
        )
        if (
            str(_receipt_field(receipt, "provider_message_sid") or "")
            != claim.message_sid
            or int(_receipt_field(receipt, "tenant_id") or 0) != claim.tenant_id
            or str(_receipt_field(receipt, "stream_key") or "") != claim.stream_key
        ):
            raise CutoverIngressIntegrityError("target_receipt_scope_mismatch")
        store.complete_claim(
            claim,
            target_turn_id=_receipt_field(receipt, "turn_id"),
            target_outcome=_receipt_field(receipt, "outcome"),
            now=operation_now,
        )
        return receipt
    except Exception as exc:
        try:
            store.retry_claim(
                claim,
                error_code="target_ingest_failed",
                now=operation_now,
            )
        except (CutoverIngressStateError, CutoverIngressIntegrityError):
            pass
        raise CutoverIngressReplayError("target_ingest_failed") from None


def replay_next_buffered_ingress(
    store: CutoverIngressStore,
    ingest: Callable[..., Any],
    *,
    now: datetime | None = None,
) -> Any | None:
    claim = store.claim_next(now=now)
    if claim is None:
        return None
    return replay_buffered_claim(store, claim, ingest, now=now)


def replay_next_into_chatboc_intake(
    store: CutoverIngressStore,
    *,
    now: datetime | None = None,
) -> Any | None:
    """Replay into Chatboc's durable intake, never into the webhook/worker.

    A Chatboc Flask application context must already be active so the existing
    intake writes to the explicitly selected target database.  The lazy import
    keeps the independent HTTP ingress process free of application models and
    runtime side effects.
    """

    from flask import current_app, has_app_context

    if not has_app_context():
        raise CutoverIngressConfigurationError(
            "cutover_ingress_target_app_context_missing"
        )
    # Replay is a target write and therefore requires the same shared control
    # authority as HTTP and background writers.  Check before claiming a
    # buffered row so a non-owner cannot consume a lease or mutate the target.
    from global_writer_authority import global_writer_authority_enabled
    from services.global_writer_authority import evaluate_global_writer_authority

    if not global_writer_authority_enabled(current_app.config):
        raise CutoverIngressConfigurationError(
            "cutover_ingress_global_writer_authority_required"
        )
    authority_decision = evaluate_global_writer_authority(current_app.config)
    if not authority_decision.allowed:
        raise CutoverIngressConfigurationError(
            "cutover_ingress_global_writer_authority_denied"
        )
    target_stream_secret = current_app.config.get("WHATSAPP_INBOUND_HASH_SECRET")
    target_secret_bytes = (
        target_stream_secret.encode("utf-8")
        if isinstance(target_stream_secret, str)
        else bytes(target_stream_secret or b"")
    )
    target_fingerprint = hashlib.sha256(target_secret_bytes).hexdigest()
    if len(target_secret_bytes) < 32 or not hmac.compare_digest(
        target_fingerprint,
        store.settings.stream_secret_sha256,
    ):
        # Check before claiming: a configuration mismatch must not consume a
        # lease or split one sender across two FIFO stream keys.
        raise CutoverIngressConfigurationError(
            "cutover_ingress_target_stream_secret_mismatch"
        )

    from services.whatsapp_inbound_turns import ingest_whatsapp_inbound_turn

    return replay_next_buffered_ingress(
        store,
        ingest_whatsapp_inbound_turn,
        now=now,
    )
