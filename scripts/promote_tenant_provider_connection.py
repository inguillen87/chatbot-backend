"""Promote a configured Junin Twilio connection from signed read-only evidence.

This command never calls Twilio.  It verifies two HMAC-authenticated canonical
evidence envelopes supplied through environment variables: a provider
read-only snapshot and a destination runtime credential-presence attestation.
When the provider credential is available to the operator process, it is read
only for constant-time key-separation checks and is never returned or logged.
Dry-run is the default.  Apply also requires an exact plan digest, a
cutover-window evidence ID, PostgreSQL TLS, SERIALIZABLE isolation, and a
transaction-scoped advisory lock.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("ENABLE_RUNTIME_SCHEMA_SYNC", "0")
os.environ.setdefault("ENABLE_RUNTIME_TENANT_INIT", "0")

from models import ProviderConnection, ProviderSender, TenantProfile  # noqa: E402
from services.provider_connection_cutover_contract import (  # noqa: E402
    MANAGED_CONNECTION_MARKER,
    advisory_lock_keys,
)


CONTRACT_VERSION = "chatboc.tenant_provider_connection_promotion.v1"
SNAPSHOT_CONTRACT = "twilio.read_only.connection_snapshot.v1"
ATTESTATION_CONTRACT = "chatboc.runtime_credential_attestation.v1"
DEFAULT_DATABASE_ENVIRONMENT_VARIABLE = "MIGRATIONS_DATABASE_URL"
TENANT_SLUG = "junin"
OFFICIAL_PHONE = "+17432643718"
PENDING_STATUS = "pending_provider_verification"
READY_STATUSES = frozenset({"online", "approved", "connected", "active"})
MANAGEMENT_MARKER = MANAGED_CONNECTION_MARKER

_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{7,64}$")
_ACCOUNT_SID_RE = re.compile(r"^AC[0-9A-Fa-f]{32}$")
_SENDER_SID_RE = re.compile(r"^XE[0-9A-Fa-f]{32}$")
_SERVICE_SID_RE = re.compile(r"^MG[0-9A-Fa-f]{32}$")
_VERCEL_PROJECT_RE = re.compile(r"^prj_[A-Za-z0-9]{8,128}$")
_VERCEL_DEPLOYMENT_RE = re.compile(r"^dpl_[A-Za-z0-9]{8,128}$")
_ENVELOPE_KEYS = frozenset(
    {"document", "document_sha256", "signature_hmac_sha256"}
)
_SNAPSHOT_KEYS = frozenset(
    {
        "contract_version",
        "source_kind",
        "read_only",
        "mutations_performed",
        "messages_sent",
        "evidence_id",
        "observed_at",
        "tenant_slug",
        "tenant_id",
        "provider",
        "channel",
        "environment",
        "resource_count",
        "account_sid",
        "credential_account_sid",
        "credential_environment_variable",
        "signing_key_environment_variable",
        "credential_binding_key_environment_variable",
        "credential_binding_hmac_sha256",
        "sender_count",
        "sender_sid",
        "messaging_service_sid",
        "phone_number",
        "sender_status",
        "webhook_url",
        "status_callback_url",
        "destination_project_id",
        "destination_deployment_id",
        "destination_deployment_revision",
        "database_identity_sha256",
        "challenge_nonce",
        "cutover_window_evidence_id",
    }
)
_ATTESTATION_KEYS = frozenset(
    {
        "contract_version",
        "source_kind",
        "runtime_platform",
        "read_only",
        "mutations_performed",
        "provider_calls_performed",
        "evidence_id",
        "observed_at",
        "tenant_slug",
        "tenant_id",
        "environment",
        "vercel_project_id",
        "vercel_deployment_id",
        "vercel_url",
        "destination_deployment_revision",
        "database_environment_variable",
        "database_identity_sha256",
        "external_account_id",
        "account_sid_environment_variable",
        "credential_environment_variable",
        "signing_key_environment_variable",
        "credential_binding_key_environment_variable",
        "credential_binding_hmac_sha256",
        "resolved_credential_scope",
        "secret_present",
        "secret_value_disclosed",
        "challenge_nonce",
        "cutover_window_evidence_id",
    }
)


class ProviderConnectionPromotionError(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = str(reason_code or "provider_connection_promotion_blocked")
        super().__init__(self.reason_code)


def _require(condition: bool, reason_code: str) -> None:
    if not condition:
        raise ProviderConnectionPromotionError(reason_code)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256_text(value: Any) -> str:
    return hashlib.sha256(_clean(value).encode("utf-8")).hexdigest()


def _redacted(value: Any) -> dict[str, Any]:
    rendered = _clean(value)
    return {
        "configured": bool(rendered),
        "sha256": _sha256_text(rendered) if rendered else None,
        "last4": rendered[-4:] if rendered else None,
    }


def _env_name(value: str, *, reason_code: str) -> str:
    rendered = _clean(value)
    _require(bool(_ENV_NAME_RE.fullmatch(rendered)), reason_code)
    return rendered


def _required_env(
    environ: Mapping[str, str],
    name: str,
    *,
    missing_reason: str,
) -> str:
    safe_name = _env_name(name, reason_code="environment_variable_name_invalid")
    value = _clean(environ.get(safe_name))
    _require(bool(value), missing_reason)
    return value


def _evidence_id(value: Any, *, reason_code: str) -> str:
    rendered = _clean(value)
    _require(bool(_EVIDENCE_ID_RE.fullmatch(rendered)), reason_code)
    return rendered


def _sha256(value: Any, *, reason_code: str) -> str:
    rendered = _clean(value).lower()
    _require(bool(_SHA256_RE.fullmatch(rendered)), reason_code)
    return rendered


def _revision(value: Any, *, reason_code: str) -> str:
    rendered = _clean(value).lower()
    _require(bool(_REVISION_RE.fullmatch(rendered)), reason_code)
    return rendered


def _canonical_sid(value: Any, pattern: re.Pattern[str], prefix: str) -> str | None:
    rendered = _clean(value)
    if not pattern.fullmatch(rendered):
        return None
    return f"{prefix}{rendered[2:].lower()}"


def _https_url(value: Any, *, reason_code: str) -> str:
    rendered = _clean(value)
    _require(bool(rendered) and len(rendered) <= 500, reason_code)
    _require(not any(ord(character) < 32 for character in rendered), reason_code)
    try:
        parsed = urlsplit(rendered)
        host = _clean(parsed.hostname).lower()
        port = parsed.port
    except ValueError as exc:
        raise ProviderConnectionPromotionError(reason_code) from exc
    _require(parsed.scheme.lower() == "https", reason_code)
    _require(bool(host), reason_code)
    _require(parsed.username is None and parsed.password is None, reason_code)
    _require(not parsed.fragment and port in (None, 443), reason_code)
    netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


def _observed_at(value: Any, *, now: datetime, maximum_age_seconds: int) -> datetime:
    rendered = _clean(value)
    try:
        observed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderConnectionPromotionError("evidence_observed_at_invalid") from exc
    _require(observed.tzinfo is not None, "evidence_observed_at_invalid")
    observed = observed.astimezone(timezone.utc)
    age = (now.astimezone(timezone.utc) - observed).total_seconds()
    _require(age >= -60, "evidence_observed_at_from_future")
    _require(age <= maximum_age_seconds, "evidence_stale")
    return observed


def _database_identity(database_url: str) -> tuple[str, dict[str, Any]]:
    try:
        parsed = make_url(_clean(database_url))
    except Exception as exc:
        raise ProviderConnectionPromotionError("database_url_invalid") from exc
    _require(parsed.get_backend_name() == "postgresql", "database_postgresql_required")
    host = _clean(parsed.host).lower()
    database = _clean(parsed.database)
    _require(bool(host and database), "database_identity_incomplete")
    _require(
        _clean(parsed.query.get("sslmode")).lower()
        in {"require", "verify-ca", "verify-full"},
        "database_tls_required",
    )
    document = {
        "backend": "postgresql",
        "host": host,
        "port": int(parsed.port or 5432),
        "database": database,
    }
    fingerprint = _canonical_sha256(document)
    return fingerprint, {
        "backend": "postgresql",
        "identity_sha256": fingerprint,
        "tls": True,
    }


def _psycopg_engine_url(database_url: str):
    return make_url(database_url).set(drivername="postgresql+psycopg")


@dataclass(frozen=True)
class VerifiedEnvelope:
    document: Mapping[str, Any] = field(repr=False)
    document_sha256: str


@dataclass(frozen=True)
class PromotionRequest:
    database_identity_sha256: str
    destination_project_id: str
    destination_deployment_id: str
    destination_deployment_url: str
    destination_deployment_revision: str
    runtime_attestation_nonce: str
    expected_webhook_url: str = field(repr=False)
    expected_status_callback_url: str = field(repr=False)
    cutover_window_evidence_id: str = field(repr=False)
    snapshot: VerifiedEnvelope = field(repr=False)
    credential_attestation: VerifiedEnvelope = field(repr=False)
    snapshot_hmac_environment_variable: str
    attestation_hmac_environment_variable: str
    maximum_evidence_age_seconds: int = 900


@dataclass
class PromotionState:
    tenant: TenantProfile
    connection: ProviderConnection
    sender: ProviderSender
    proposed_connection_status: str
    proposed_connection_config: dict[str, Any]
    proposed_sender_status: str
    proposed_sender_values: dict[str, Any]
    proposed_tenant_configuration: dict[str, Any]
    connection_action: str
    sender_action: str
    tenant_configuration_action: str


def _verified_envelope(
    raw_json: str,
    *,
    hmac_secret: str,
    expected_keys: frozenset[str],
    envelope_reason_prefix: str,
) -> VerifiedEnvelope:
    try:
        envelope = json.loads(raw_json)
    except (TypeError, ValueError) as exc:
        raise ProviderConnectionPromotionError(
            f"{envelope_reason_prefix}_json_invalid"
        ) from exc
    _require(isinstance(envelope, dict), f"{envelope_reason_prefix}_json_invalid")
    _require(
        frozenset(envelope) == _ENVELOPE_KEYS,
        f"{envelope_reason_prefix}_envelope_shape_invalid",
    )
    document = envelope.get("document")
    _require(isinstance(document, dict), f"{envelope_reason_prefix}_document_invalid")
    _require(
        frozenset(document) == expected_keys,
        f"{envelope_reason_prefix}_document_shape_invalid",
    )
    expected_digest = _canonical_sha256(document)
    supplied_digest = _sha256(
        envelope.get("document_sha256"),
        reason_code=f"{envelope_reason_prefix}_digest_invalid",
    )
    _require(
        hmac.compare_digest(expected_digest, supplied_digest),
        f"{envelope_reason_prefix}_digest_mismatch",
    )
    signature = _sha256(
        envelope.get("signature_hmac_sha256"),
        reason_code=f"{envelope_reason_prefix}_signature_invalid",
    )
    expected_signature = hmac.new(
        hmac_secret.encode("utf-8"),
        _canonical_json(document),
        hashlib.sha256,
    ).hexdigest()
    _require(
        hmac.compare_digest(signature, expected_signature),
        f"{envelope_reason_prefix}_signature_mismatch",
    )
    return VerifiedEnvelope(document=document, document_sha256=expected_digest)


def build_request_from_environment(
    *,
    database_identity_sha256: str,
    snapshot_environment_variable: str,
    credential_attestation_environment_variable: str,
    snapshot_hmac_secret_environment_variable: str,
    attestation_hmac_secret_environment_variable: str,
    webhook_environment_variable: str,
    callback_environment_variable: str,
    destination_project_id: str,
    destination_deployment_id: str,
    destination_deployment_url: str,
    destination_deployment_revision: str,
    runtime_attestation_nonce: str,
    cutover_window_evidence_id: str,
    maximum_evidence_age_seconds: int = 900,
    environ: Mapping[str, str] | None = None,
) -> PromotionRequest:
    source = environ if environ is not None else os.environ
    snapshot_hmac_env = _env_name(
        snapshot_hmac_secret_environment_variable,
        reason_code="snapshot_hmac_environment_variable_name_invalid",
    )
    attestation_hmac_env = _env_name(
        attestation_hmac_secret_environment_variable,
        reason_code="attestation_hmac_environment_variable_name_invalid",
    )
    _require(
        snapshot_hmac_env != attestation_hmac_env,
        "evidence_hmac_environment_variables_must_differ",
    )
    snapshot_hmac_secret = _required_env(
        source,
        snapshot_hmac_env,
        missing_reason="snapshot_hmac_secret_environment_variable_missing",
    )
    attestation_hmac_secret = _required_env(
        source,
        attestation_hmac_env,
        missing_reason="attestation_hmac_secret_environment_variable_missing",
    )
    _require(
        len(snapshot_hmac_secret.encode("utf-8")) >= 32,
        "snapshot_hmac_secret_too_short",
    )
    _require(
        len(attestation_hmac_secret.encode("utf-8")) >= 32,
        "attestation_hmac_secret_too_short",
    )
    _require(
        not hmac.compare_digest(snapshot_hmac_secret, attestation_hmac_secret),
        "evidence_hmac_secret_values_must_differ",
    )
    snapshot = _verified_envelope(
        _required_env(
            source,
            snapshot_environment_variable,
            missing_reason="provider_snapshot_environment_variable_missing",
        ),
        hmac_secret=snapshot_hmac_secret,
        expected_keys=_SNAPSHOT_KEYS,
        envelope_reason_prefix="provider_snapshot",
    )
    attestation = _verified_envelope(
        _required_env(
            source,
            credential_attestation_environment_variable,
            missing_reason="credential_attestation_environment_variable_missing",
        ),
        hmac_secret=attestation_hmac_secret,
        expected_keys=_ATTESTATION_KEYS,
        envelope_reason_prefix="credential_attestation",
    )
    _require(
        60 <= int(maximum_evidence_age_seconds) <= 3600,
        "maximum_evidence_age_invalid",
    )
    snapshot_credential_env = _env_name(
        snapshot.document.get("credential_environment_variable"),
        reason_code="provider_snapshot_credential_environment_invalid",
    )
    attestation_credential_env = _env_name(
        attestation.document.get("credential_environment_variable"),
        reason_code="credential_attestation_environment_variable_invalid",
    )
    _require(
        snapshot_credential_env == attestation_credential_env,
        "evidence_credential_environment_variable_mismatch",
    )
    _require(
        snapshot_hmac_env != snapshot_credential_env
        and attestation_hmac_env != snapshot_credential_env,
        "evidence_hmac_must_differ_from_runtime_credential",
    )
    runtime_credential = _clean(source.get(snapshot_credential_env))
    if runtime_credential:
        _require(
            not hmac.compare_digest(snapshot_hmac_secret, runtime_credential)
            and not hmac.compare_digest(attestation_hmac_secret, runtime_credential),
            "evidence_hmac_secret_value_must_differ_from_runtime_credential",
        )
    project_id = _clean(destination_project_id)
    deployment_id = _clean(destination_deployment_id)
    _require(bool(_VERCEL_PROJECT_RE.fullmatch(project_id)), "destination_project_id_invalid")
    _require(bool(_VERCEL_DEPLOYMENT_RE.fullmatch(deployment_id)), "destination_deployment_id_invalid")
    return PromotionRequest(
        database_identity_sha256=_sha256(
            database_identity_sha256,
            reason_code="database_identity_sha256_invalid",
        ),
        destination_project_id=project_id,
        destination_deployment_id=deployment_id,
        destination_deployment_url=_https_url(
            destination_deployment_url,
            reason_code="destination_deployment_url_invalid",
        ),
        destination_deployment_revision=_revision(
            destination_deployment_revision,
            reason_code="destination_deployment_revision_invalid",
        ),
        runtime_attestation_nonce=_evidence_id(
            runtime_attestation_nonce,
            reason_code="runtime_attestation_nonce_invalid",
        ),
        expected_webhook_url=_https_url(
            _required_env(
                source,
                webhook_environment_variable,
                missing_reason="expected_webhook_environment_variable_missing",
            ),
            reason_code="expected_webhook_url_invalid",
        ),
        expected_status_callback_url=_https_url(
            _required_env(
                source,
                callback_environment_variable,
                missing_reason="expected_callback_environment_variable_missing",
            ),
            reason_code="expected_callback_url_invalid",
        ),
        cutover_window_evidence_id=_evidence_id(
            cutover_window_evidence_id,
            reason_code="cutover_window_evidence_id_invalid",
        ),
        snapshot=snapshot,
        credential_attestation=attestation,
        snapshot_hmac_environment_variable=snapshot_hmac_env,
        attestation_hmac_environment_variable=attestation_hmac_env,
        maximum_evidence_age_seconds=int(maximum_evidence_age_seconds),
    )


def _tenant_owner_id(tenant: TenantProfile) -> int:
    owners = {
        int(value)
        for value in (tenant.municipio_id, tenant.pyme_id)
        if value not in (None, "") and int(value) > 0
    }
    _require(len(owners) == 1, "tenant_owner_missing_or_ambiguous")
    return next(iter(owners))


def _sender_phone(sender: ProviderSender) -> str | None:
    for value in (sender.phone_number, sender.sender_id):
        rendered = _clean(value)
        if rendered.lower().startswith("whatsapp:"):
            rendered = rendered.split(":", 1)[1].strip()
        if rendered == OFFICIAL_PHONE:
            return rendered
    return None


def _validate_documents(
    request: PromotionRequest,
    *,
    tenant: TenantProfile,
    connection: ProviderConnection,
    now: datetime,
) -> tuple[Mapping[str, Any], Mapping[str, Any], str, str, str]:
    snapshot = request.snapshot.document
    attestation = request.credential_attestation.document
    _require(snapshot["contract_version"] == SNAPSHOT_CONTRACT, "provider_snapshot_contract_mismatch")
    _require(snapshot["source_kind"] == "provider_api_read", "provider_snapshot_source_invalid")
    _require(snapshot["read_only"] is True, "provider_snapshot_not_read_only")
    _require(snapshot["mutations_performed"] is False, "provider_snapshot_mutation_detected")
    _require(snapshot["messages_sent"] is False, "provider_snapshot_message_detected")
    snapshot_evidence_id = _evidence_id(snapshot["evidence_id"], reason_code="provider_snapshot_evidence_id_invalid")
    _observed_at(snapshot["observed_at"], now=now, maximum_age_seconds=request.maximum_evidence_age_seconds)
    _require(snapshot["tenant_slug"] == TENANT_SLUG, "provider_snapshot_tenant_slug_mismatch")
    _require(int(snapshot["tenant_id"] or 0) == int(tenant.id), "provider_snapshot_tenant_id_mismatch")
    _require(_clean(snapshot["provider"]).lower() == "twilio", "provider_snapshot_provider_mismatch")
    _require(_clean(snapshot["channel"]).lower() == "whatsapp", "provider_snapshot_channel_mismatch")
    _require(_clean(snapshot["environment"]).lower() == "production", "provider_snapshot_environment_mismatch")
    _require(int(snapshot["resource_count"] or 0) == 1, "provider_snapshot_resource_count_mismatch")
    account_sid = _canonical_sid(snapshot["account_sid"], _ACCOUNT_SID_RE, "AC")
    credential_account = _canonical_sid(snapshot["credential_account_sid"], _ACCOUNT_SID_RE, "AC")
    expected_account = _canonical_sid(connection.external_account_id, _ACCOUNT_SID_RE, "AC")
    _require(bool(expected_account), "provider_connection_external_account_invalid")
    _require(account_sid == expected_account, "provider_snapshot_account_mismatch")
    _require(credential_account == expected_account, "provider_snapshot_credential_account_mismatch")
    credential_env = _env_name(snapshot["credential_environment_variable"], reason_code="provider_snapshot_credential_environment_invalid")
    _require(connection.credentials_ref == f"env:{credential_env}", "provider_snapshot_credentials_ref_mismatch")
    snapshot_signing_env = _env_name(
        snapshot["signing_key_environment_variable"],
        reason_code="provider_snapshot_signing_environment_invalid",
    )
    _require(
        snapshot_signing_env == request.snapshot_hmac_environment_variable,
        "provider_snapshot_signing_environment_mismatch",
    )
    snapshot_binding_env = _env_name(
        snapshot["credential_binding_key_environment_variable"],
        reason_code="provider_snapshot_binding_environment_invalid",
    )
    _require(
        len({credential_env, snapshot_signing_env, snapshot_binding_env}) == 3,
        "provider_snapshot_key_environment_variables_must_differ",
    )
    snapshot_binding = _sha256(
        snapshot["credential_binding_hmac_sha256"],
        reason_code="provider_snapshot_credential_binding_invalid",
    )
    _require(int(snapshot["sender_count"] or 0) == 1, "provider_snapshot_sender_count_mismatch")
    sender_sid = _canonical_sid(snapshot["sender_sid"], _SENDER_SID_RE, "XE")
    service_sid = _canonical_sid(snapshot["messaging_service_sid"], _SERVICE_SID_RE, "MG")
    _require(bool(sender_sid), "provider_snapshot_sender_sid_invalid")
    _require(bool(service_sid), "provider_snapshot_messaging_service_sid_invalid")
    _require(snapshot["phone_number"] == OFFICIAL_PHONE, "provider_snapshot_phone_mismatch")
    _require(
        snapshot["sender_status"] == "ONLINE",
        "provider_snapshot_sender_status_must_be_online",
    )
    _require(
        _https_url(snapshot["webhook_url"], reason_code="provider_snapshot_webhook_invalid")
        == request.expected_webhook_url,
        "provider_snapshot_webhook_mismatch",
    )
    _require(
        _https_url(snapshot["status_callback_url"], reason_code="provider_snapshot_callback_invalid")
        == request.expected_status_callback_url,
        "provider_snapshot_callback_mismatch",
    )
    _require(
        _revision(snapshot["destination_deployment_revision"], reason_code="provider_snapshot_revision_invalid")
        == request.destination_deployment_revision,
        "provider_snapshot_revision_mismatch",
    )
    _require(
        snapshot["destination_project_id"] == request.destination_project_id,
        "provider_snapshot_project_mismatch",
    )
    _require(
        snapshot["destination_deployment_id"] == request.destination_deployment_id,
        "provider_snapshot_deployment_mismatch",
    )
    _require(
        _sha256(snapshot["database_identity_sha256"], reason_code="provider_snapshot_database_identity_invalid")
        == request.database_identity_sha256,
        "provider_snapshot_database_identity_mismatch",
    )
    _require(
        _evidence_id(snapshot["challenge_nonce"], reason_code="provider_snapshot_nonce_invalid")
        == request.runtime_attestation_nonce,
        "provider_snapshot_nonce_mismatch",
    )
    _require(
        _evidence_id(snapshot["cutover_window_evidence_id"], reason_code="provider_snapshot_window_invalid")
        == request.cutover_window_evidence_id,
        "provider_snapshot_window_mismatch",
    )

    _require(attestation["contract_version"] == ATTESTATION_CONTRACT, "credential_attestation_contract_mismatch")
    _require(attestation["source_kind"] == "destination_runtime_configuration_read", "credential_attestation_source_invalid")
    _require(attestation["runtime_platform"] == "vercel", "credential_attestation_runtime_platform_mismatch")
    _require(attestation["read_only"] is True, "credential_attestation_not_read_only")
    _require(attestation["mutations_performed"] is False, "credential_attestation_mutation_detected")
    _require(attestation["provider_calls_performed"] is False, "credential_attestation_provider_call_detected")
    attestation_evidence_id = _evidence_id(attestation["evidence_id"], reason_code="credential_attestation_evidence_id_invalid")
    _observed_at(attestation["observed_at"], now=now, maximum_age_seconds=request.maximum_evidence_age_seconds)
    _require(attestation["tenant_slug"] == TENANT_SLUG, "credential_attestation_tenant_slug_mismatch")
    _require(int(attestation["tenant_id"] or 0) == int(tenant.id), "credential_attestation_tenant_id_mismatch")
    _require(_clean(attestation["environment"]).lower() == "production", "credential_attestation_environment_mismatch")
    _require(
        attestation["vercel_project_id"] == request.destination_project_id,
        "credential_attestation_project_mismatch",
    )
    _require(
        attestation["vercel_deployment_id"] == request.destination_deployment_id,
        "credential_attestation_deployment_mismatch",
    )
    _require(
        _https_url(attestation["vercel_url"], reason_code="credential_attestation_vercel_url_invalid")
        == request.destination_deployment_url,
        "credential_attestation_vercel_url_mismatch",
    )
    _require(
        _revision(attestation["destination_deployment_revision"], reason_code="credential_attestation_revision_invalid")
        == request.destination_deployment_revision,
        "credential_attestation_revision_mismatch",
    )
    _require(
        _sha256(attestation["database_identity_sha256"], reason_code="credential_attestation_database_identity_invalid")
        == request.database_identity_sha256,
        "credential_attestation_database_identity_mismatch",
    )
    _env_name(
        attestation["database_environment_variable"],
        reason_code="credential_attestation_database_environment_invalid",
    )
    _require(
        _canonical_sid(attestation["external_account_id"], _ACCOUNT_SID_RE, "AC")
        == expected_account,
        "credential_attestation_account_mismatch",
    )
    _require(
        _env_name(attestation["credential_environment_variable"], reason_code="credential_attestation_environment_variable_invalid")
        == credential_env,
        "credential_attestation_environment_variable_mismatch",
    )
    account_env = _env_name(
        attestation["account_sid_environment_variable"],
        reason_code="credential_attestation_account_environment_invalid",
    )
    attestation_signing_env = _env_name(
        attestation["signing_key_environment_variable"],
        reason_code="credential_attestation_signing_environment_invalid",
    )
    _require(
        attestation_signing_env == request.attestation_hmac_environment_variable,
        "credential_attestation_signing_environment_mismatch",
    )
    attestation_binding_env = _env_name(
        attestation["credential_binding_key_environment_variable"],
        reason_code="credential_attestation_binding_environment_invalid",
    )
    _require(
        snapshot_binding_env == attestation_binding_env,
        "credential_binding_environment_mismatch",
    )
    _require(
        len(
            {
                credential_env,
                account_env,
                snapshot_signing_env,
                attestation_signing_env,
                attestation_binding_env,
            }
        )
        == 5,
        "promotion_evidence_environment_variables_must_differ",
    )
    attestation_binding = _sha256(
        attestation["credential_binding_hmac_sha256"],
        reason_code="credential_attestation_binding_invalid",
    )
    _require(
        hmac.compare_digest(snapshot_binding, attestation_binding),
        "credential_binding_mismatch",
    )
    _require(
        attestation["resolved_credential_scope"] == "subaccount",
        "credential_attestation_scope_mismatch",
    )
    _require(attestation["secret_present"] is True, "credential_attestation_secret_missing")
    _require(
        attestation["secret_value_disclosed"] is False,
        "credential_attestation_secret_value_disclosed",
    )
    _require(
        _evidence_id(attestation["challenge_nonce"], reason_code="credential_attestation_nonce_invalid")
        == request.runtime_attestation_nonce,
        "credential_attestation_nonce_mismatch",
    )
    _require(
        _evidence_id(attestation["cutover_window_evidence_id"], reason_code="credential_attestation_window_invalid")
        == request.cutover_window_evidence_id,
        "credential_attestation_window_mismatch",
    )
    evidence_ids = {
        snapshot_evidence_id,
        attestation_evidence_id,
        request.cutover_window_evidence_id,
        request.runtime_attestation_nonce,
    }
    _require(len(evidence_ids) == 4, "promotion_evidence_ids_must_be_independent")
    return (
        snapshot,
        attestation,
        sender_sid,
        service_sid,
        snapshot["sender_status"].lower(),
    )


def inspect_promotion_state(
    session: Session,
    request: PromotionRequest,
    *,
    now: datetime | None = None,
    lock_rows: bool = False,
) -> PromotionState:
    expected_now = now or datetime.now(timezone.utc)
    _require(expected_now.tzinfo is not None, "promotion_clock_invalid")
    tenants = list(session.execute(select(TenantProfile)).scalars().all())
    exact = [tenant for tenant in tenants if _clean(tenant.slug) == TENANT_SLUG]
    if not exact and any(_clean(tenant.slug).lower() == TENANT_SLUG for tenant in tenants):
        raise ProviderConnectionPromotionError("tenant_slug_exact_mismatch")
    _require(len(exact) == 1, "tenant_exactly_one_required")
    tenant = exact[0]
    if lock_rows:
        tenant = session.execute(select(TenantProfile).where(TenantProfile.id == tenant.id).with_for_update()).scalar_one()
    _require(tenant.is_active is True, "tenant_inactive")
    owner_id = _tenant_owner_id(tenant)
    _require(
        not any(
            candidate.id != tenant.id
            and candidate.is_active is True
            and owner_id in {int(value) for value in (candidate.municipio_id, candidate.pyme_id) if value not in (None, "")}
            for candidate in tenants
        ),
        "tenant_owner_claimed_by_other_active_tenant",
    )
    all_connections = list(
        session.execute(select(ProviderConnection)).scalars().all()
    )
    connections = [
        item
        for item in all_connections
        if int(item.tenant_id) == int(tenant.id)
        and _clean(item.provider).lower() == "twilio"
        and _clean(item.channel).lower() == "whatsapp"
        and _clean(item.environment).lower() == "production"
    ]
    _require(len(connections) == 1, "provider_connection_exactly_one_required")
    connection = connections[0]
    if lock_rows:
        connection = session.execute(select(ProviderConnection).where(ProviderConnection.id == connection.id).with_for_update()).scalar_one()
    status = _clean(connection.status).lower()
    _require(status == PENDING_STATUS or status in READY_STATUSES, "provider_connection_not_pending_verification")
    snapshot, _, sender_sid, service_sid, provider_sender_status = _validate_documents(
        request,
        tenant=tenant,
        connection=connection,
        now=expected_now,
    )
    current_connection_config = (
        dict(connection.config) if isinstance(connection.config, dict) else {}
    )
    current_management = current_connection_config.get(MANAGEMENT_MARKER)
    _require(
        isinstance(current_management, dict)
        and current_management.get("enabled") is True,
        "provider_connection_management_marker_missing",
    )
    expected_account = _canonical_sid(
        snapshot["account_sid"], _ACCOUNT_SID_RE, "AC"
    )
    _require(
        not any(
            other.id != connection.id
            and int(other.tenant_id) != int(tenant.id)
            and _canonical_sid(other.external_account_id, _ACCOUNT_SID_RE, "AC")
            == expected_account
            for other in all_connections
        ),
        "external_account_claimed_by_other_tenant",
    )
    credential_env = _env_name(
        snapshot["credential_environment_variable"],
        reason_code="provider_snapshot_credential_environment_invalid",
    )
    tenant_configuration = (
        dict(tenant.configuracion)
        if isinstance(tenant.configuracion, dict)
        else {}
    )
    current_runtime_state = tenant_configuration.get("twilio_tech_provider")
    current_runtime_state = (
        dict(current_runtime_state)
        if isinstance(current_runtime_state, dict)
        else {}
    )
    configured_account = _clean(current_runtime_state.get("twilio_account_sid"))
    _require(
        not configured_account
        or _canonical_sid(configured_account, _ACCOUNT_SID_RE, "AC")
        == expected_account,
        "tenant_runtime_account_mismatch",
    )
    configured_token_ref = _clean(
        current_runtime_state.get("twilio_subaccount_token_ref")
    )
    _require(
        not configured_token_ref or configured_token_ref == credential_env,
        "tenant_runtime_credential_ref_mismatch",
    )
    proposed_runtime_state = dict(current_runtime_state)
    proposed_runtime_state.update(
        {
            "twilio_account_sid": expected_account,
            "twilio_subaccount_token_ref": credential_env,
            "sender_status": provider_sender_status,
        }
    )
    proposed_tenant_configuration = dict(tenant_configuration)
    proposed_tenant_configuration["twilio_tech_provider"] = proposed_runtime_state
    tenant_configuration_action = (
        "update"
        if proposed_tenant_configuration != tenant_configuration
        else "noop"
    )

    all_senders = list(session.execute(select(ProviderSender)).scalars().all())
    tenant_senders = [
        sender
        for sender in all_senders
        if int(sender.tenant_id) == int(tenant.id)
        and _clean(sender.channel).lower() == "whatsapp"
        and _sender_phone(sender) == OFFICIAL_PHONE
    ]
    _require(len(tenant_senders) == 1, "provider_sender_exactly_one_required")
    sender = tenant_senders[0]
    if lock_rows:
        sender = session.execute(select(ProviderSender).where(ProviderSender.id == sender.id).with_for_update()).scalar_one()
    _require(sender.provider_connection_id in (None, connection.id), "provider_sender_connection_mismatch")
    for current, expected, code in (
        (_clean(sender.sender_sid), sender_sid, "provider_sender_sid_mismatch"),
        (_clean(sender.messaging_service_sid), service_sid, "provider_sender_service_sid_mismatch"),
        (_clean(sender.webhook_url), request.expected_webhook_url, "provider_sender_webhook_mismatch"),
        (_clean(sender.status_callback_url), request.expected_status_callback_url, "provider_sender_callback_mismatch"),
    ):
        _require(not current or current == expected, code)
    _require(
        not any(
            other.id != sender.id
            and (
                _clean(other.sender_sid) == sender_sid
                or _clean(other.messaging_service_sid) == service_sid
                or _sender_phone(other) == OFFICIAL_PHONE
            )
            and int(other.tenant_id) != int(tenant.id)
            for other in all_senders
        ),
        "provider_sender_cross_tenant_conflict",
    )

    proposed_connection_status = status if status in READY_STATUSES else "online"
    proposed_connection_config = dict(current_connection_config)
    proposed_management = dict(current_management)
    proposed_management.update(
        {
            "enabled": True,
            "promotion_required": False,
            "destination_deployment_revision": (
                request.destination_deployment_revision
            ),
            "provider_snapshot_sha256": request.snapshot.document_sha256,
            "credential_attestation_sha256": (
                request.credential_attestation.document_sha256
            ),
        }
    )
    proposed_connection_config[MANAGEMENT_MARKER] = proposed_management
    proposed_sender_status = provider_sender_status
    connection_action = (
        "noop"
        if status in READY_STATUSES
        and proposed_connection_config == current_connection_config
        else "promote"
    )
    proposed_sender_values = {
        "provider_connection_id": int(connection.id),
        "sender_sid": sender_sid,
        "messaging_service_sid": service_sid,
        "phone_number": OFFICIAL_PHONE,
        "sender_id": f"whatsapp:{OFFICIAL_PHONE}",
        "webhook_url": request.expected_webhook_url,
        "status_callback_url": request.expected_status_callback_url,
        "status": proposed_sender_status,
    }
    sender_action = (
        "update"
        if any(getattr(sender, key) != value for key, value in proposed_sender_values.items())
        else "noop"
    )
    return PromotionState(
        tenant=tenant,
        connection=connection,
        sender=sender,
        proposed_connection_status=proposed_connection_status,
        proposed_connection_config=proposed_connection_config,
        proposed_sender_status=proposed_sender_status,
        proposed_sender_values=proposed_sender_values,
        proposed_tenant_configuration=proposed_tenant_configuration,
        connection_action=connection_action,
        sender_action=sender_action,
        tenant_configuration_action=tenant_configuration_action,
    )


def _plan_document(state: PromotionState, request: PromotionRequest) -> dict[str, Any]:
    snapshot = request.snapshot.document
    attestation = request.credential_attestation.document
    return {
        "contract_version": CONTRACT_VERSION,
        "database_identity_sha256": request.database_identity_sha256,
        "tenant": {"id": int(state.tenant.id), "slug": TENANT_SLUG, "active": True},
        "scope": {"provider": "twilio", "channel": "whatsapp", "environment": "production"},
        "current": {
            "connection_id": int(state.connection.id),
            "connection_status": _clean(state.connection.status),
            "sender_id": int(state.sender.id),
            "sender_status": _clean(state.sender.status),
        },
        "proposed": {
            "connection_action": state.connection_action,
            "connection_status": state.proposed_connection_status,
            "connection_config_sha256": _canonical_sha256(
                state.proposed_connection_config
            ),
            "sender_action": state.sender_action,
            "sender_status": state.proposed_sender_status,
            "tenant_configuration_action": state.tenant_configuration_action,
            "tenant_configuration_sha256": _canonical_sha256(
                state.proposed_tenant_configuration
            ),
            "external_account": _redacted(state.connection.external_account_id),
            "sender_sid": _redacted(snapshot["sender_sid"]),
            "messaging_service_sid": _redacted(snapshot["messaging_service_sid"]),
            "phone": _redacted(OFFICIAL_PHONE),
        },
        "evidence_binding": {
            "provider_snapshot_sha256": request.snapshot.document_sha256,
            "provider_evidence_id_sha256": _sha256_text(snapshot["evidence_id"]),
            "credential_attestation_sha256": request.credential_attestation.document_sha256,
            "credential_attestation_evidence_id_sha256": _sha256_text(attestation["evidence_id"]),
            "cutover_window_evidence_sha256": _sha256_text(request.cutover_window_evidence_id),
            "runtime_attestation_nonce_sha256": _sha256_text(
                request.runtime_attestation_nonce
            ),
            "destination_project_id_sha256": _sha256_text(
                request.destination_project_id
            ),
            "destination_deployment_id_sha256": _sha256_text(
                request.destination_deployment_id
            ),
            "destination_deployment_url_sha256": _sha256_text(
                request.destination_deployment_url
            ),
            "destination_deployment_revision_sha256": _sha256_text(request.destination_deployment_revision),
            "credential_binding_sha256": _sha256_text(
                snapshot["credential_binding_hmac_sha256"]
            ),
            "maximum_evidence_age_seconds": request.maximum_evidence_age_seconds,
            "snapshot_hmac_environment_variable_sha256": _sha256_text(
                request.snapshot_hmac_environment_variable
            ),
            "attestation_hmac_environment_variable_sha256": _sha256_text(
                request.attestation_hmac_environment_variable
            ),
        },
        "conflicts": 0,
        "provider_calls_performed": False,
        "messages_sent": False,
        "credential_secret_disclosed": False,
        "credential_secret_compared_when_available": True,
    }


def build_promotion_plan(
    session: Session,
    request: PromotionRequest,
    *,
    now: datetime | None = None,
    lock_rows: bool = False,
) -> tuple[PromotionState, dict[str, Any], str]:
    state = inspect_promotion_state(session, request, now=now, lock_rows=lock_rows)
    plan = _plan_document(state, request)
    return state, plan, _canonical_sha256(plan)


def acquire_postgres_advisory_lock(session: Session, request: PromotionRequest) -> None:
    bind = session.get_bind()
    _require(bind.dialect.name == "postgresql", "apply_postgresql_required")
    reader = getattr(bind, "get_isolation_level", None)
    isolation = reader() if callable(reader) else None
    _require(_clean(isolation).upper() == "SERIALIZABLE", "apply_serializable_transaction_required")
    lock_keys = advisory_lock_keys(
        database_identity_sha256=request.database_identity_sha256,
        tenant_slug=TENANT_SLUG,
        external_account_id=request.snapshot.document["account_sid"],
    )
    for lock_key in lock_keys:
        acquired = session.execute(
            text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        ).scalar_one()
        _require(bool(acquired), "advisory_lock_contended_retry_required")


def promote_tenant_provider_connection(
    session: Session,
    request: PromotionRequest,
    *,
    apply: bool = False,
    approved_plan_sha256: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if apply:
        approved = _sha256(approved_plan_sha256, reason_code="approved_plan_sha256_required")
        acquire_postgres_advisory_lock(session, request)
        state, plan, plan_sha256 = build_promotion_plan(session, request, now=now, lock_rows=True)
        _require(approved == plan_sha256, "approved_plan_sha256_mismatch")
        writes = (
            state.connection_action != "noop"
            or state.sender_action != "noop"
            or state.tenant_configuration_action != "noop"
        )
        state.connection.status = state.proposed_connection_status
        state.connection.config = state.proposed_connection_config
        state.tenant.configuracion = state.proposed_tenant_configuration
        for key, value in state.proposed_sender_values.items():
            setattr(state.sender, key, value)
        if writes:
            session.flush()
        post_state, _, _ = build_promotion_plan(session, request, now=now)
        _require(post_state.connection_action == "noop", "provider_connection_promotion_postcondition_drift")
        _require(post_state.sender_action == "noop", "provider_sender_promotion_postcondition_drift")
        _require(
            post_state.tenant_configuration_action == "noop",
            "tenant_runtime_configuration_postcondition_drift",
        )
        status = "promoted" if writes else "already_promoted"
    else:
        _, plan, plan_sha256 = build_promotion_plan(session, request, now=now)
        status = "dry_run"
        writes = False
    return {
        "contract_version": CONTRACT_VERSION,
        "status": status,
        "dry_run": not apply,
        "writes_performed": writes,
        "plan_sha256": plan_sha256,
        "plan": plan,
    }


def _failure(reason_code: str, *, dry_run: bool, error_type: str | None = None) -> dict[str, Any]:
    result = {
        "contract_version": CONTRACT_VERSION,
        "status": "blocked",
        "dry_run": dry_run,
        "writes_performed": False,
        "reason_code": reason_code,
        "provider_calls_performed": False,
        "messages_sent": False,
        "credential_secret_disclosed": False,
        "credential_secret_compared_when_available": True,
    }
    if error_type:
        result["error_type"] = error_type
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-environment-variable", default=DEFAULT_DATABASE_ENVIRONMENT_VARIABLE)
    parser.add_argument("--provider-snapshot-environment-variable", required=True)
    parser.add_argument("--credential-attestation-environment-variable", required=True)
    parser.add_argument("--snapshot-hmac-secret-environment-variable", required=True)
    parser.add_argument("--attestation-hmac-secret-environment-variable", required=True)
    parser.add_argument("--expected-webhook-environment-variable", required=True)
    parser.add_argument("--expected-callback-environment-variable", required=True)
    parser.add_argument("--destination-project-id", required=True)
    parser.add_argument("--destination-deployment-id", required=True)
    parser.add_argument("--destination-deployment-url", required=True)
    parser.add_argument("--destination-deployment-revision", required=True)
    parser.add_argument("--runtime-attestation-nonce", required=True)
    parser.add_argument("--cutover-window-evidence-id", required=True)
    parser.add_argument("--maximum-evidence-age-seconds", type=int, default=900)
    parser.add_argument("--approved-plan-sha256")
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    engine = connection = transaction = session = None
    try:
        database_env = _env_name(args.database_environment_variable, reason_code="database_environment_variable_name_invalid")
        database_url = _required_env(os.environ, database_env, missing_reason="database_environment_variable_missing")
        database_fingerprint, database_summary = _database_identity(database_url)
        request = build_request_from_environment(
            database_identity_sha256=database_fingerprint,
            snapshot_environment_variable=args.provider_snapshot_environment_variable,
            credential_attestation_environment_variable=args.credential_attestation_environment_variable,
            snapshot_hmac_secret_environment_variable=(
                args.snapshot_hmac_secret_environment_variable
            ),
            attestation_hmac_secret_environment_variable=(
                args.attestation_hmac_secret_environment_variable
            ),
            webhook_environment_variable=args.expected_webhook_environment_variable,
            callback_environment_variable=args.expected_callback_environment_variable,
            destination_project_id=args.destination_project_id,
            destination_deployment_id=args.destination_deployment_id,
            destination_deployment_url=args.destination_deployment_url,
            destination_deployment_revision=args.destination_deployment_revision,
            runtime_attestation_nonce=args.runtime_attestation_nonce,
            cutover_window_evidence_id=args.cutover_window_evidence_id,
            maximum_evidence_age_seconds=args.maximum_evidence_age_seconds,
            environ=os.environ,
        )
        engine = create_engine(
            _psycopg_engine_url(database_url),
            poolclass=NullPool,
            hide_parameters=True,
            connect_args={"application_name": "chatboc_provider_connection_promoter", "connect_timeout": 10},
        )
        isolation = "SERIALIZABLE" if args.apply else "REPEATABLE READ"
        connection = engine.connect().execution_options(isolation_level=isolation)
        transaction = connection.begin()
        if not args.apply:
            connection.execute(text("SET TRANSACTION READ ONLY"))
        connection.execute(text("SET LOCAL statement_timeout = '30s'"))
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        session = Session(bind=connection, autoflush=False, expire_on_commit=False)
        result = promote_tenant_provider_connection(
            session,
            request,
            apply=bool(args.apply),
            approved_plan_sha256=args.approved_plan_sha256,
        )
        result["database"] = database_summary
        transaction.commit() if args.apply else transaction.rollback()
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except ProviderConnectionPromotionError as exc:
        if transaction is not None and transaction.is_active:
            transaction.rollback()
        print(json.dumps(_failure(exc.reason_code, dry_run=not bool(args.apply)), ensure_ascii=True, sort_keys=True))
        return 2
    except Exception as exc:  # pragma: no cover
        if transaction is not None and transaction.is_active:
            transaction.rollback()
        print(json.dumps(_failure("provider_connection_promotion_unexpected_error", dry_run=not bool(args.apply), error_type=type(exc).__name__), ensure_ascii=True, sort_keys=True))
        return 3
    finally:
        if session is not None:
            session.close()
        if connection is not None:
            connection.close()
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
