"""Small, side-effect-free primitives for cutover evidence producers.

The evidence producers deliberately share only canonicalization and credential
binding helpers.  Provider collection and destination runtime attestation use
different signing keys; a third domain-specific key lets the promoter prove
that both processes observed the same high-entropy credential without ever
placing that credential in an evidence document.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Mapping

from sqlalchemy.engine import make_url


ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ACCOUNT_SID_RE = re.compile(r"^AC[0-9A-Fa-f]{32}$")
EVIDENCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")


class CutoverEvidenceError(RuntimeError):
    """Stable, non-sensitive fail-closed reason."""

    def __init__(self, reason_code: str):
        self.reason_code = str(reason_code or "cutover_evidence_blocked")
        super().__init__(self.reason_code)


def require(condition: bool, reason_code: str) -> None:
    if not condition:
        raise CutoverEvidenceError(reason_code)


def clean(value: Any) -> str:
    return str(value or "").strip()


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def environment_name(value: Any, *, reason_code: str) -> str:
    rendered = clean(value)
    require(bool(ENV_NAME_RE.fullmatch(rendered)), reason_code)
    return rendered


def evidence_id(value: Any, *, reason_code: str) -> str:
    rendered = clean(value)
    require(bool(EVIDENCE_ID_RE.fullmatch(rendered)), reason_code)
    return rendered


def account_sid(value: Any, *, reason_code: str) -> str:
    rendered = clean(value)
    require(bool(ACCOUNT_SID_RE.fullmatch(rendered)), reason_code)
    return f"AC{rendered[2:].lower()}"


def sha256_value(value: Any, *, reason_code: str) -> str:
    rendered = clean(value).lower()
    require(bool(SHA256_RE.fullmatch(rendered)), reason_code)
    return rendered


def required_environment_value(
    environ: Mapping[str, str],
    variable_name: str,
    *,
    missing_reason: str,
) -> str:
    safe_name = environment_name(
        variable_name,
        reason_code="environment_variable_name_invalid",
    )
    value = clean(environ.get(safe_name))
    require(bool(value), missing_reason)
    return value


def credential_binding(
    *,
    binding_key: str,
    account_sid_value: str,
    credential_value: str,
) -> str:
    """Return a domain-separated opaque binding, never the credential value."""

    require(
        len(binding_key.encode("utf-8")) >= 32,
        "credential_binding_key_too_short",
    )
    require(bool(credential_value), "credential_missing")
    account = account_sid(
        account_sid_value,
        reason_code="credential_account_sid_invalid",
    )
    material = b"chatboc.cutover.credential-binding.v1\0" + account.encode(
        "ascii"
    ) + b"\0" + credential_value.encode("utf-8")
    return hmac.new(binding_key.encode("utf-8"), material, hashlib.sha256).hexdigest()


def signed_envelope(document: Mapping[str, Any], *, signing_key: str) -> dict[str, Any]:
    require(len(signing_key.encode("utf-8")) >= 32, "evidence_signing_key_too_short")
    payload = canonical_json(document)
    return {
        "document": dict(document),
        "document_sha256": hashlib.sha256(payload).hexdigest(),
        "signature_hmac_sha256": hmac.new(
            signing_key.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest(),
    }


def database_identity(database_url: str) -> tuple[str, dict[str, Any]]:
    """Fingerprint a concrete TLS PostgreSQL target without credentials."""

    try:
        parsed = make_url(clean(database_url))
    except Exception as exc:
        raise CutoverEvidenceError("database_url_invalid") from exc
    require(parsed.get_backend_name() == "postgresql", "database_postgresql_required")
    host = clean(parsed.host).lower()
    database = clean(parsed.database)
    require(bool(host and database), "database_identity_incomplete")
    require(
        clean(parsed.query.get("sslmode")).lower()
        in {"require", "verify-ca", "verify-full"},
        "database_tls_required",
    )
    identity = {
        "backend": "postgresql",
        "host": host,
        "port": int(parsed.port or 5432),
        "database": database,
    }
    digest = canonical_sha256(identity)
    return digest, {
        "backend": "postgresql",
        "identity_sha256": digest,
        "tls": True,
    }


__all__ = [
    "CutoverEvidenceError",
    "account_sid",
    "canonical_json",
    "canonical_sha256",
    "clean",
    "credential_binding",
    "database_identity",
    "environment_name",
    "evidence_id",
    "require",
    "required_environment_value",
    "sha256_value",
    "signed_envelope",
]
