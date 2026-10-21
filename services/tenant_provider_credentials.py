"""Encrypted, tenant-bound Twilio tokens in the existing provider connection.

This is an internal store, not a credential-upload endpoint. Its caller must
authorize the actor and own the surrounding transaction. A store operation and
its audit row commit together; this module never commits or calls a provider.
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


PRIVATE_CONFIG_KEY = "_chatboc_provider_credentials_v1"
VAULT_REF = "vault:twilio:auth_token:v1"
CONTRACT = "chatboc.tenant_provider_credentials.v1"
_KID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ACCOUNT = re.compile(r"^AC[0-9a-fA-F]{32}$")
_TOKEN = re.compile(r"^[0-9a-fA-F]{32}$")


class ProviderCredentialError(RuntimeError):
    """Safe reason code only: no input, plaintext, key or ciphertext in errors."""


@dataclass(frozen=True)
class TenantProviderCredential:
    account_sid: str
    revision: int
    auth_token: str = field(repr=False)


def _positive(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise ProviderCredentialError("provider_credential_binding_invalid")
    return value


def _keyring(config: Mapping[str, Any]) -> tuple[str, dict[str, bytes]]:
    raw = config.get("TENANT_PROVIDER_CREDENTIAL_KEYRING")
    active = config.get("TENANT_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID")
    if not isinstance(raw, str) or not raw or len(raw) > 4096:
        raise ProviderCredentialError("provider_credential_keyring_unavailable")
    if not isinstance(active, str) or not _KID.fullmatch(active):
        raise ProviderCredentialError("provider_credential_keyring_unavailable")
    try:
        def unique_keys(pairs):
            value = {}
            for name, item in pairs:
                if name in value:
                    raise ValueError
                value[name] = item
            return value

        document = json.loads(raw, object_pairs_hook=unique_keys)
        if not isinstance(document, dict) or not 1 <= len(document) <= 16:
            raise ValueError
        keys = {}
        for kid, encoded in document.items():
            if not isinstance(kid, str) or not _KID.fullmatch(kid) or not isinstance(encoded, str) or len(encoded) != 44:
                raise ValueError
            key = base64.b64decode(encoded, validate=True)
            if len(key) != 32:
                raise ValueError
            keys[kid] = key
        if active not in keys:
            raise ValueError
    except (ValueError, TypeError, binascii.Error):
        raise ProviderCredentialError("provider_credential_keyring_unavailable") from None
    return active, keys


def credential_storage_status(config: Mapping[str, Any]) -> dict[str, Any]:
    """Report local cryptographic configuration, never provider readiness."""
    try:
        _keyring(config)
    except ProviderCredentialError:
        return {"ready": False, "status": "unavailable", "reason_code": "twilio_tenant_credential_store_unavailable"}
    return {"ready": True, "status": "configured", "reason_code": None}


def tenant_has_internal_provider_credentials(tenant: Any) -> bool:
    """Read committed columns, without flushing a stale or dirty ORM identity.

    Legacy onboarding cannot use environment credentials for an owned vault.
    A foreign tenant's marker never affects this tenant. No token is opened.
    """
    from flask import has_app_context
    from sqlalchemy import select
    from models import ProviderConnection, db

    if not has_app_context():
        return False
    with db.session.no_autoflush:
        tenant_id = getattr(tenant, "id", None)
    if type(tenant_id) is not int or tenant_id <= 0:
        raise ValueError("provider_credential_binding_invalid")
    rows = db.session.execute(
        select(ProviderConnection.config, ProviderConnection.credentials_ref)
        .where(ProviderConnection.tenant_id == tenant_id,
               ProviderConnection.provider == "twilio",
               ProviderConnection.channel == "whatsapp")
        .execution_options(autoflush=False)
    ).all()
    return any(
        (isinstance(config, dict) and PRIVATE_CONFIG_KEY in config)
        or str(reference or "").strip().startswith("vault:")
        for config, reference in rows
    )


def _binding(connection: Any, tenant_id: int, account_sid: str) -> dict[str, Any]:
    from services.provider_connection_cutover_contract import MANAGED_CONNECTION_MARKER

    tenant_id = _positive(tenant_id)
    if (getattr(connection, "tenant_id", None) != tenant_id
            or getattr(connection, "provider", None) != "twilio"
            or getattr(connection, "channel", None) not in {"whatsapp", "sms"}
            or getattr(connection, "environment", None) not in {"production", "sandbox"}
            or not isinstance(account_sid, str) or not _ACCOUNT.fullmatch(account_sid)
            or getattr(connection, "external_account_id", None) != account_sid):
        raise ProviderCredentialError("provider_credential_binding_invalid")
    config = getattr(connection, "config", None)
    marker = config.get(MANAGED_CONNECTION_MARKER) if isinstance(config, Mapping) else None
    if isinstance(marker, Mapping) and marker.get("enabled") is True:
        raise ProviderCredentialError("provider_credential_managed_migration_required")
    return {"contract": CONTRACT, "tenant_id": tenant_id,
            "connection_id": _positive(getattr(connection, "id", None)),
            "provider": connection.provider, "channel": connection.channel,
            "environment": connection.environment, "account_sid": account_sid}


def _aad(binding: Mapping[str, Any], revision: int, kid: str) -> bytes:
    return json.dumps({**binding, "revision": revision, "key_id": kid}, sort_keys=True, separators=(",", ":")).encode("ascii")


def _envelope_revision(envelope: Any) -> int:
    if not isinstance(envelope, dict) or envelope.get("contract") != CONTRACT:
        raise ProviderCredentialError("provider_credential_envelope_invalid")
    revision = envelope.get("revision")
    if type(revision) is not int or revision <= 0:
        raise ProviderCredentialError("provider_credential_envelope_invalid")
    return revision


def seal_token(*, connection: Any, tenant_id: int, account_sid: str, auth_token: str, revision: int, app_config: Mapping[str, Any]) -> dict[str, Any]:
    binding = _binding(connection, tenant_id, account_sid)
    if not isinstance(auth_token, str) or not _TOKEN.fullmatch(auth_token):
        raise ProviderCredentialError("provider_credential_token_invalid")
    if type(revision) is not int or revision <= 0:
        raise ProviderCredentialError("provider_credential_revision_invalid")
    kid, keys = _keyring(app_config)
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(keys[kid]).encrypt(nonce, auth_token.encode("ascii"), _aad(binding, revision, kid))
    return {"contract": CONTRACT, "revision": revision, "key_id": kid,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii")}


def open_token(*, connection: Any, tenant_id: int, app_config: Mapping[str, Any]) -> TenantProviderCredential:
    binding = _binding(connection, tenant_id, getattr(connection, "external_account_id", None))
    if getattr(connection, "credentials_ref", None) != VAULT_REF:
        raise ProviderCredentialError("provider_credential_reference_invalid")
    cfg = connection.config if isinstance(connection.config, dict) else {}
    envelope = cfg.get(PRIVATE_CONFIG_KEY)
    revision = _envelope_revision(envelope)
    _, keys = _keyring(app_config)
    try:
        kid = envelope["key_id"]
        if not isinstance(kid, str) or kid not in keys:
            raise ValueError
        nonce_raw = envelope["nonce"]
        ciphertext_raw = envelope["ciphertext"]
        if not isinstance(nonce_raw, str) or len(nonce_raw) != 16 or not isinstance(ciphertext_raw, str) or len(ciphertext_raw) != 64:
            raise ValueError
        nonce = base64.b64decode(nonce_raw, validate=True)
        ciphertext = base64.b64decode(ciphertext_raw, validate=True)
        if len(nonce) != 12 or len(ciphertext) != 48:
            raise ValueError
        plaintext = AESGCM(keys[kid]).decrypt(nonce, ciphertext, _aad(binding, revision, kid)).decode("ascii")
        if not _TOKEN.fullmatch(plaintext):
            raise ValueError
    except (KeyError, ValueError, TypeError, binascii.Error, UnicodeError, InvalidTag):
        raise ProviderCredentialError("provider_credential_integrity_failed") from None
    return TenantProviderCredential(account_sid=binding["account_sid"], auth_token=plaintext, revision=revision)


def public_provider_config(value: Any) -> Any:
    """Exclude private envelopes and credential values at every nesting level."""
    from services.public_tenant_config import _is_private_key

    if isinstance(value, Mapping):
        return {key: public_provider_config(item) for key, item in value.items()
                if not _is_private_key(key) and str(key).lower() != PRIVATE_CONFIG_KEY}
    if isinstance(value, (list, tuple)):
        return [public_provider_config(item) for item in value]
    return value


def store_tenant_twilio_token(*, tenant_id: int, connection_id: int, account_sid: str,
                             auth_token: str, expected_revision: int, actor_user_id: int,
                             app_config: Mapping[str, Any], session=None) -> int:
    """CAS update and secret-free audit in the caller's transaction; no commit.

    Managed cutover connections retain their existing env attestation and are
    deliberately excluded. Installing a token does not activate a sender.
    """
    from sqlalchemy import JSON, or_, select, update
    from models import AuditEvent, ProviderConnection, db
    from services.provider_connection_cutover_contract import MANAGED_CONNECTION_MARKER

    tenant_id = _positive(tenant_id); connection_id = _positive(connection_id)
    actor_user_id = _positive(actor_user_id)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ProviderCredentialError("provider_credential_revision_invalid")
    _keyring(app_config)
    effect_session = session or db.session
    connection = effect_session.execute(
        select(ProviderConnection).where(ProviderConnection.id == connection_id,
                                        ProviderConnection.tenant_id == tenant_id)
        .with_for_update().execution_options(populate_existing=True, autoflush=False)
    ).scalar_one_or_none()
    if connection is None:
        raise ProviderCredentialError("provider_credential_binding_invalid")
    _binding(connection, tenant_id, account_sid)
    if connection.config is not None and not isinstance(connection.config, dict):
        raise ProviderCredentialError("provider_credential_envelope_invalid")
    prior = dict(connection.config or {})
    if isinstance(prior.get(MANAGED_CONNECTION_MARKER), dict) and prior[MANAGED_CONNECTION_MARKER].get("enabled") is True:
        raise ProviderCredentialError("provider_credential_managed_migration_required")
    old_envelope = prior.get(PRIVATE_CONFIG_KEY)
    revision = _envelope_revision(old_envelope) if PRIVATE_CONFIG_KEY in prior else 0
    if revision != expected_revision:
        raise ProviderCredentialError("provider_credential_revision_conflict")
    if old_envelope is not None:
        open_token(connection=connection, tenant_id=tenant_id, app_config=app_config)
    elif connection.credentials_ref and connection.credentials_ref.startswith("vault:"):
        raise ProviderCredentialError("provider_credential_reference_invalid")
    next_revision = revision + 1
    envelope = seal_token(connection=connection, tenant_id=tenant_id, account_sid=account_sid,
                          auth_token=auth_token, revision=next_revision, app_config=app_config)
    next_config = {**prior, PRIVATE_CONFIG_KEY: envelope}
    # SQL NULL and JSON null both load as None. Neither carries an envelope.
    condition = (ProviderConnection.config == connection.config if connection.config is not None
                 else or_(ProviderConnection.config.is_(None), ProviderConnection.config == JSON.NULL))
    changed = effect_session.execute(
        update(ProviderConnection).where(ProviderConnection.id == connection_id,
                                        ProviderConnection.tenant_id == tenant_id,
                                        ProviderConnection.external_account_id == account_sid,
                                        ProviderConnection.credentials_ref == connection.credentials_ref,
                                        condition)
        .values(config=next_config, credentials_ref=VAULT_REF)
        .returning(ProviderConnection.id).execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    if changed is None:
        raise ProviderCredentialError("provider_credential_revision_conflict")
    effect_session.add(AuditEvent(tenant_id=tenant_id, actor_user_id=actor_user_id,
                                  event_type="provider_credential.stored", resource_type="provider_connection",
                                  resource_id=str(connection_id), details={"contract": CONTRACT, "revision": next_revision,
                                                                         "key_id": envelope["key_id"], "provider": "twilio"}))
    effect_session.flush()
    effect_session.expire(connection)
    return next_revision
