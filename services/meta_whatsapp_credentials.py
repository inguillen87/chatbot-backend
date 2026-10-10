"""Offline Meta token envelope; caller owns authorization and persistence.

Uses the existing provider keyring/private envelope location, with a distinct
provider contract. No environment token fallback, database writes or network.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import json
import re
import secrets
from typing import Any, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from services.tenant_provider_credentials import (
    PRIVATE_CONFIG_KEY, ProviderCredentialError, _keyring, _positive,
)
from services.provider_connection_cutover_contract import MANAGED_CONNECTION_MARKER

CONTRACT = "chatboc.meta_whatsapp_credentials.v1"
VAULT_REF = "vault:meta:whatsapp_access_token:v1"
_ID = re.compile(r"^[1-9][0-9]{0,39}$")
_TOKEN = re.compile(r"^[A-Za-z0-9._~-]{16,4096}$")


@dataclass(frozen=True)
class MetaCredential:
    revision: int
    expires_at: int
    access_token: str = field(repr=False)


def meta_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ProviderCredentialError("meta_binding_invalid")
    return value


def _binding(connection: Any, tenant_id: int) -> dict:
    tenant_id = _positive(tenant_id)
    cfg = getattr(connection, "config", None)
    marker = cfg.get(MANAGED_CONNECTION_MARKER) if isinstance(cfg, Mapping) else None
    if isinstance(marker, Mapping) and marker.get("enabled") is True:
        raise ProviderCredentialError("meta_managed_migration_required")
    if (getattr(connection, "tenant_id", None) != tenant_id
            or getattr(connection, "provider", None) != "meta"
            or getattr(connection, "channel", None) != "whatsapp"
            or getattr(connection, "environment", None) not in {"production", "sandbox"}):
        raise ProviderCredentialError("meta_binding_invalid")
    return dict(contract=CONTRACT, tenant_id=tenant_id,
                connection_id=_positive(getattr(connection, "id", None)),
                provider="meta", channel="whatsapp", environment=connection.environment,
                app_id=meta_id(getattr(connection, "external_app_id", None)),
                waba_id=meta_id(getattr(connection, "external_account_id", None)))


def _aad(binding: Mapping, revision: int, expires_at: int, kid: str) -> bytes:
    return json.dumps({**binding, "revision": revision, "expires_at": expires_at,
                       "key_id": kid}, sort_keys=True, separators=(",", ":")).encode("ascii")


def seal_token(*, connection: Any, tenant_id: int, access_token: str,
               revision: int, expires_at: int, now: int, app_config: Mapping) -> dict:
    """Return ciphertext only; this does not install/verify/activate a token."""
    binding = _binding(connection, tenant_id)
    if not isinstance(access_token, str) or not _TOKEN.fullmatch(access_token):
        raise ProviderCredentialError("meta_token_invalid")
    if (type(revision) is not int or revision <= 0 or type(now) is not int
            or now < 0 or type(expires_at) is not int or expires_at <= now):
        raise ProviderCredentialError("meta_token_expiry_or_revision_invalid")
    kid, keys = _keyring(app_config)
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(keys[kid]).encrypt(
        nonce, access_token.encode("ascii"), _aad(binding, revision, expires_at, kid))
    return dict(contract=CONTRACT, revision=revision, expires_at=expires_at, key_id=kid,
                nonce=base64.b64encode(nonce).decode("ascii"),
                ciphertext=base64.b64encode(ciphertext).decode("ascii"))


def open_token(*, connection: Any, tenant_id: int, now: int,
               app_config: Mapping) -> MetaCredential:
    binding = _binding(connection, tenant_id)
    if getattr(connection, "credentials_ref", None) != VAULT_REF:
        raise ProviderCredentialError("meta_credential_reference_invalid")
    cfg = connection.config if isinstance(connection.config, dict) else {}
    envelope = cfg.get(PRIVATE_CONFIG_KEY)
    if not isinstance(envelope, dict) or envelope.get("contract") != CONTRACT:
        raise ProviderCredentialError("meta_credential_envelope_invalid")
    revision, expires_at = envelope.get("revision"), envelope.get("expires_at")
    if (type(revision) is not int or revision <= 0 or type(now) is not int or now < 0
            or type(expires_at) is not int or expires_at <= now):
        raise ProviderCredentialError("meta_credential_expired_or_invalid")
    _, keys = _keyring(app_config)
    try:
        kid = envelope["key_id"]
        nonce_raw, ciphertext_raw = envelope["nonce"], envelope["ciphertext"]
        if (not isinstance(kid, str) or kid not in keys
                or not isinstance(nonce_raw, str) or len(nonce_raw) != 16
                or not isinstance(ciphertext_raw, str) or not 44 <= len(ciphertext_raw) <= 5484):
            raise ValueError
        nonce = base64.b64decode(nonce_raw, validate=True)
        ciphertext = base64.b64decode(ciphertext_raw, validate=True)
        if len(nonce) != 12 or not 32 <= len(ciphertext) <= 4112:
            raise ValueError
        token = AESGCM(keys[kid]).decrypt(
            nonce, ciphertext, _aad(binding, revision, expires_at, kid)).decode("ascii")
        if not _TOKEN.fullmatch(token):
            raise ValueError
    except (KeyError, ValueError, TypeError, binascii.Error, UnicodeError, InvalidTag):
        raise ProviderCredentialError("meta_credential_integrity_failed") from None
    return MetaCredential(revision=revision, expires_at=expires_at, access_token=token)
