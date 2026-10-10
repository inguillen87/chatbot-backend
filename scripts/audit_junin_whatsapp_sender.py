"""Read-only cutover audit for Junin's official Twilio WhatsApp sender.

The command has no write/apply mode and no messaging transport.  Sensitive
values are accepted only through explicitly named environment variables.  A
provider adapter is injected as a *read-only snapshot source* so tests and
operators can prove the database/provider binding without giving this module
any capability to send a message or mutate a provider resource.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("ENABLE_RUNTIME_SCHEMA_SYNC", "0")
os.environ.setdefault("ENABLE_RUNTIME_TENANT_INIT", "0")

from models import (  # noqa: E402
    ProviderConnection,
    ProviderSender,
    TenantProfile,
    WhatsappNumero,
)


CONTRACT_VERSION = "chatboc.junin_whatsapp_sender_cutover_audit.v1"
PROVIDER_SNAPSHOT_CONTRACT = "twilio.read_only.sender_snapshot.v1"
TENANT_SLUG = "junin"
EXPECTED_OFFICIAL_PHONE_LAST4 = "3718"
DEFAULT_DATABASE_ENVIRONMENT_VARIABLE = "MIGRATIONS_DATABASE_URL"
DEFAULT_PHONE_ENVIRONMENT_VARIABLE = "JUNIN_OFFICIAL_WHATSAPP_E164"
DEFAULT_WEBHOOK_ENVIRONMENT_VARIABLE = "JUNIN_EXPECTED_WHATSAPP_WEBHOOK_URL"
DEFAULT_CALLBACK_ENVIRONMENT_VARIABLE = (
    "JUNIN_EXPECTED_WHATSAPP_STATUS_CALLBACK_URL"
)
DEFAULT_PROVIDER_SNAPSHOT_ENVIRONMENT_VARIABLE = (
    "JUNIN_TWILIO_READ_ONLY_SNAPSHOT_JSON"
)

_ENVIRONMENT_VARIABLE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_E164_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
_ACCOUNT_SID_RE = re.compile(r"^AC[A-Za-z0-9]{8,64}$")
_SENDER_SID_RE = re.compile(r"^XE[A-Za-z0-9]{8,64}$")
_MESSAGING_SERVICE_SID_RE = re.compile(r"^MG[A-Za-z0-9]{8,64}$")
_EVIDENCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_READY_STATUSES = frozenset({"online", "approved", "connected", "active"})


class SenderAuditError(RuntimeError):
    """Stable blocker that never embeds a sensitive value."""

    def __init__(self, reason_code: str):
        self.reason_code = str(reason_code or "junin_sender_audit_blocked")
        super().__init__(self.reason_code)


def _require(condition: bool, reason_code: str) -> None:
    if not condition:
        raise SenderAuditError(reason_code)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _sha256_text(value: Any) -> str:
    return hashlib.sha256(_clean(value).encode("utf-8")).hexdigest()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _redacted(value: Any, *, include_last4: bool = True) -> dict[str, Any]:
    rendered = _clean(value)
    return {
        "configured": bool(rendered),
        "sha256": _sha256_text(rendered) if rendered else None,
        "last4": rendered[-4:] if rendered and include_last4 else None,
    }


def _environment_variable_name(value: str, *, reason_code: str) -> str:
    rendered = _clean(value)
    _require(bool(_ENVIRONMENT_VARIABLE_RE.fullmatch(rendered)), reason_code)
    return rendered


def _required_environment_value(
    environ: Mapping[str, str],
    variable_name: str,
    *,
    missing_reason: str,
) -> str:
    name = _environment_variable_name(
        variable_name,
        reason_code="environment_variable_name_invalid",
    )
    value = _clean(environ.get(name))
    _require(bool(value), missing_reason)
    return value


def _canonical_e164(value: Any) -> str | None:
    rendered = _clean(value)
    if rendered.lower().startswith("whatsapp:"):
        rendered = rendered.split(":", 1)[1].strip()
    return rendered if _E164_RE.fullmatch(rendered) else None


def _canonical_https_url(value: Any, *, reason_code: str) -> str:
    rendered = _clean(value)
    _require(bool(rendered) and len(rendered) <= 500, reason_code)
    _require(not any(ord(character) < 32 for character in rendered), reason_code)
    try:
        parsed = urlsplit(rendered)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise SenderAuditError(reason_code) from exc
    _require(parsed.scheme.lower() == "https", reason_code)
    _require(bool(hostname), reason_code)
    _require(parsed.username is None and parsed.password is None, reason_code)
    _require(not parsed.fragment, reason_code)
    _require(port in (None, 443), reason_code)
    netloc = hostname
    if ":" in hostname and not hostname.startswith("["):
        netloc = f"[{hostname}]"
    path = parsed.path or "/"
    return urlunsplit(("https", netloc, path, parsed.query, ""))


def _owner_id(tenant: TenantProfile) -> int:
    owner_ids = {
        int(value)
        for value in (tenant.municipio_id, tenant.pyme_id)
        if value not in (None, "") and int(value) > 0
    }
    _require(len(owner_ids) == 1, "tenant_owner_missing_or_ambiguous")
    return next(iter(owner_ids))


def _sender_matches_phone(sender: ProviderSender, expected_phone: str) -> bool:
    return expected_phone in {
        _canonical_e164(sender.phone_number),
        _canonical_e164(sender.sender_id),
    }


@dataclass(frozen=True)
class AuditRequest:
    expected_phone: str
    expected_webhook_url: str
    expected_status_callback_url: str
    database_identity_sha256: str
    maximum_snapshot_age_seconds: int = 900


class ReadOnlyProviderSnapshotSource(Protocol):
    """Deliberately tiny provider capability: one read, no send/update/delete."""

    def fetch_sender_snapshot(
        self,
        *,
        credential_reference: str,
        expected_phone: str,
    ) -> Mapping[str, Any]: ...


class EnvironmentProviderSnapshotSource:
    """Load an independently captured provider GET snapshot from an env var.

    This source performs no network request.  Production certification still
    requires the injected snapshot to have been captured through provider API
    read credentials and to satisfy the freshness/evidence contract.
    """

    def __init__(self, *, environment_variable: str, environ: Mapping[str, str]):
        self._environment_variable = _environment_variable_name(
            environment_variable,
            reason_code="provider_snapshot_environment_variable_name_invalid",
        )
        self._environ = environ

    def fetch_sender_snapshot(
        self,
        *,
        credential_reference: str,
        expected_phone: str,
    ) -> Mapping[str, Any]:
        del credential_reference, expected_phone
        payload = _required_environment_value(
            self._environ,
            self._environment_variable,
            missing_reason="provider_snapshot_environment_variable_missing",
        )
        try:
            decoded = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise SenderAuditError("provider_snapshot_json_invalid") from exc
        _require(isinstance(decoded, dict), "provider_snapshot_json_invalid")
        return decoded


def _database_identity(database_url: str) -> tuple[str, dict[str, Any]]:
    try:
        parsed = make_url(_clean(database_url))
    except Exception as exc:
        raise SenderAuditError("database_url_invalid") from exc
    _require(parsed.get_backend_name() == "postgresql", "database_postgresql_required")
    host = _clean(parsed.host).lower()
    database = _clean(parsed.database)
    _require(bool(host and database), "database_identity_incomplete")
    sslmode = _clean(parsed.query.get("sslmode")).lower()
    _require(sslmode in {"require", "verify-ca", "verify-full"}, "database_tls_required")
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


def build_request_from_environment(
    *,
    database_identity_sha256: str,
    phone_environment_variable: str = DEFAULT_PHONE_ENVIRONMENT_VARIABLE,
    webhook_environment_variable: str = DEFAULT_WEBHOOK_ENVIRONMENT_VARIABLE,
    callback_environment_variable: str = DEFAULT_CALLBACK_ENVIRONMENT_VARIABLE,
    maximum_snapshot_age_seconds: int = 900,
    environ: Mapping[str, str] | None = None,
) -> AuditRequest:
    source = environ if environ is not None else os.environ
    phone = _required_environment_value(
        source,
        phone_environment_variable,
        missing_reason="expected_phone_environment_variable_missing",
    )
    _require(bool(_E164_RE.fullmatch(phone)), "expected_phone_e164_invalid")
    _require(
        phone.endswith(EXPECTED_OFFICIAL_PHONE_LAST4),
        "expected_phone_not_junin_official_last4",
    )
    webhook = _required_environment_value(
        source,
        webhook_environment_variable,
        missing_reason="expected_webhook_environment_variable_missing",
    )
    callback = _required_environment_value(
        source,
        callback_environment_variable,
        missing_reason="expected_callback_environment_variable_missing",
    )
    database_fingerprint = _clean(database_identity_sha256).lower()
    _require(
        bool(re.fullmatch(r"[0-9a-f]{64}", database_fingerprint)),
        "database_identity_sha256_invalid",
    )
    _require(
        60 <= int(maximum_snapshot_age_seconds) <= 3600,
        "provider_snapshot_maximum_age_invalid",
    )
    return AuditRequest(
        expected_phone=phone,
        expected_webhook_url=_canonical_https_url(
            webhook,
            reason_code="expected_webhook_url_invalid",
        ),
        expected_status_callback_url=_canonical_https_url(
            callback,
            reason_code="expected_callback_url_invalid",
        ),
        database_identity_sha256=database_fingerprint,
        maximum_snapshot_age_seconds=int(maximum_snapshot_age_seconds),
    )


def _parse_observed_at(value: Any, *, now: datetime) -> datetime:
    rendered = _clean(value)
    try:
        observed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SenderAuditError("provider_snapshot_observed_at_invalid") from exc
    _require(observed.tzinfo is not None, "provider_snapshot_observed_at_invalid")
    observed = observed.astimezone(timezone.utc)
    age = (now.astimezone(timezone.utc) - observed).total_seconds()
    _require(age >= -60, "provider_snapshot_from_future")
    return observed


def _validate_provider_snapshot(
    snapshot: Mapping[str, Any],
    *,
    request: AuditRequest,
    connection: ProviderConnection,
    sender: ProviderSender,
    now: datetime,
) -> dict[str, Any]:
    _require(snapshot.get("contract_version") == PROVIDER_SNAPSHOT_CONTRACT, "provider_snapshot_contract_mismatch")
    _require(snapshot.get("source_kind") == "provider_api_read", "provider_snapshot_source_not_provider_read")
    _require(snapshot.get("read_only") is True, "provider_snapshot_not_read_only")
    _require(snapshot.get("messages_sent") is False, "provider_snapshot_send_detected")
    _require(snapshot.get("mutations_performed") is False, "provider_snapshot_mutation_detected")
    _require(int(snapshot.get("resource_count") or 0) == 1, "provider_sender_resource_count_mismatch")
    _require(_clean(snapshot.get("provider")).lower() == "twilio", "provider_snapshot_provider_mismatch")
    _require(_clean(snapshot.get("channel")).lower() == "whatsapp", "provider_snapshot_channel_mismatch")
    _require(_clean(snapshot.get("environment")).lower() == "production", "provider_snapshot_environment_mismatch")

    evidence_id = _clean(snapshot.get("evidence_id"))
    _require(bool(_EVIDENCE_ID_RE.fullmatch(evidence_id)), "provider_snapshot_evidence_id_invalid")
    observed = _parse_observed_at(snapshot.get("observed_at"), now=now)
    age = (now.astimezone(timezone.utc) - observed).total_seconds()
    _require(age <= request.maximum_snapshot_age_seconds, "provider_snapshot_stale")

    account_sid = _clean(snapshot.get("account_sid"))
    credential_account_sid = _clean(snapshot.get("credential_account_sid"))
    _require(bool(_ACCOUNT_SID_RE.fullmatch(account_sid)), "provider_account_sid_invalid")
    _require(bool(_ACCOUNT_SID_RE.fullmatch(credential_account_sid)), "provider_credential_account_sid_invalid")
    expected_account = _clean(connection.external_account_id)
    _require(bool(_ACCOUNT_SID_RE.fullmatch(expected_account)), "provider_connection_external_account_invalid")
    _require(account_sid == expected_account, "provider_account_ownership_mismatch")
    _require(credential_account_sid == expected_account, "provider_credential_ownership_mismatch")

    phone = _canonical_e164(snapshot.get("phone_number"))
    _require(phone == request.expected_phone, "provider_phone_mismatch")
    status = _clean(snapshot.get("status")).lower()
    _require(status in _READY_STATUSES, "provider_sender_not_ready")

    sender_sid = _clean(snapshot.get("sender_sid"))
    service_sid = _clean(snapshot.get("messaging_service_sid"))
    _require(bool(_SENDER_SID_RE.fullmatch(sender_sid)), "provider_sender_sid_invalid")
    _require(bool(_MESSAGING_SERVICE_SID_RE.fullmatch(service_sid)), "provider_messaging_service_sid_invalid")
    _require(sender_sid == _clean(sender.sender_sid), "provider_sender_sid_mismatch")
    _require(service_sid == _clean(sender.messaging_service_sid), "provider_messaging_service_sid_mismatch")

    webhook = _canonical_https_url(snapshot.get("webhook_url"), reason_code="provider_webhook_url_invalid")
    callback = _canonical_https_url(snapshot.get("status_callback_url"), reason_code="provider_callback_url_invalid")
    _require(webhook == request.expected_webhook_url, "provider_webhook_url_mismatch")
    _require(callback == request.expected_status_callback_url, "provider_callback_url_mismatch")

    return {
        "snapshot_sha256": _canonical_sha256(snapshot),
        "evidence_id_sha256": _sha256_text(evidence_id),
        "observed_at_sha256": _sha256_text(observed.isoformat()),
        "fresh_within_seconds": request.maximum_snapshot_age_seconds,
        "account": _redacted(account_sid),
        "credential_account": _redacted(credential_account_sid),
        "sender_sid": _redacted(sender_sid),
        "messaging_service_sid": _redacted(service_sid),
        "phone": _redacted(phone),
        "status": status,
        "webhook_url": _redacted(webhook, include_last4=False),
        "status_callback_url": _redacted(callback, include_last4=False),
    }


def audit_junin_sender(
    session: Session,
    request: AuditRequest,
    provider_source: ReadOnlyProviderSnapshotSource,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Certify an exact DB/provider binding without flushing or committing."""

    _require(isinstance(request, AuditRequest), "audit_request_invalid")
    _require(
        _canonical_e164(request.expected_phone) == request.expected_phone,
        "expected_phone_e164_invalid",
    )
    _require(
        request.expected_phone.endswith(EXPECTED_OFFICIAL_PHONE_LAST4),
        "expected_phone_not_junin_official_last4",
    )
    expected_now = now or datetime.now(timezone.utc)
    _require(expected_now.tzinfo is not None, "audit_clock_invalid")

    tenants = list(session.execute(select(TenantProfile)).scalars().all())
    exact = [tenant for tenant in tenants if _clean(tenant.slug) == TENANT_SLUG]
    if not exact and any(_clean(tenant.slug).lower() == TENANT_SLUG for tenant in tenants):
        raise SenderAuditError("tenant_slug_exact_mismatch")
    _require(len(exact) == 1, "tenant_junin_exactly_one_required")
    tenant = exact[0]
    _require(tenant.is_active is True, "tenant_junin_inactive")
    owner_id = _owner_id(tenant)

    profile_sender = _canonical_e164(tenant.whatsapp_sender_id)
    _require(profile_sender == request.expected_phone, "tenant_profile_sender_mismatch")
    claiming_tenants = [
        candidate
        for candidate in tenants
        if _canonical_e164(candidate.whatsapp_sender_id) == request.expected_phone
    ]
    _require(
        len(claiming_tenants) == 1 and claiming_tenants[0].id == tenant.id,
        "official_phone_claimed_by_other_tenant",
    )

    legacy_rows = list(session.execute(select(WhatsappNumero)).scalars().all())
    routing_matches = [
        row
        for row in legacy_rows
        if _canonical_e164(row.numero_whatsapp) == request.expected_phone
    ]
    _require(len(routing_matches) == 1, "inbound_routing_number_exactly_one_required")
    routing = routing_matches[0]
    _require(routing.is_active is True, "inbound_routing_number_inactive")
    _require(int(routing.user_id) == owner_id, "inbound_routing_owner_mismatch")

    connections = [
        item
        for item in session.execute(select(ProviderConnection)).scalars().all()
        if int(item.tenant_id) == int(tenant.id)
        and _clean(item.provider).lower() == "twilio"
        and _clean(item.channel).lower() == "whatsapp"
        and _clean(item.environment).lower() == "production"
    ]
    _require(len(connections) == 1, "provider_connection_exactly_one_required")
    connection = connections[0]
    _require(_clean(connection.status).lower() in _READY_STATUSES, "provider_connection_not_ready")
    credential_reference = _clean(connection.credentials_ref)
    _require(bool(credential_reference), "provider_connection_credentials_ref_missing")

    all_senders = list(session.execute(select(ProviderSender)).scalars().all())
    phone_claims = [
        candidate
        for candidate in all_senders
        if _sender_matches_phone(candidate, request.expected_phone)
    ]
    _require(
        not any(int(candidate.tenant_id) != int(tenant.id) for candidate in phone_claims),
        "official_phone_claimed_by_other_provider_sender",
    )
    tenant_senders = [
        candidate
        for candidate in all_senders
        if int(candidate.tenant_id) == int(tenant.id)
        and _clean(candidate.channel).lower() == "whatsapp"
    ]
    ready_senders = [
        candidate
        for candidate in tenant_senders
        if _clean(candidate.status).lower() in _READY_STATUSES
    ]
    _require(len(ready_senders) == 1, "provider_sender_ready_exactly_one_required")
    sender = ready_senders[0]
    _require(_sender_matches_phone(sender, request.expected_phone), "provider_sender_phone_mismatch")
    _require(
        int(sender.provider_connection_id or 0) == int(connection.id),
        "provider_sender_connection_mismatch",
    )

    db_webhook = _canonical_https_url(sender.webhook_url, reason_code="database_sender_webhook_url_invalid")
    db_callback = _canonical_https_url(sender.status_callback_url, reason_code="database_sender_callback_url_invalid")
    _require(db_webhook == request.expected_webhook_url, "database_sender_webhook_url_mismatch")
    _require(db_callback == request.expected_status_callback_url, "database_sender_callback_url_mismatch")

    snapshot = provider_source.fetch_sender_snapshot(
        credential_reference=credential_reference,
        expected_phone=request.expected_phone,
    )
    _require(isinstance(snapshot, Mapping), "provider_snapshot_invalid")
    provider = _validate_provider_snapshot(
        snapshot,
        request=request,
        connection=connection,
        sender=sender,
        now=expected_now,
    )

    report = {
        "contract_version": CONTRACT_VERSION,
        "status": "certified",
        "tenant": {
            "slug_sha256": _sha256_text(TENANT_SLUG),
            "active": True,
        },
        "official_phone": _redacted(request.expected_phone),
        "database_identity_sha256": request.database_identity_sha256,
        "database": {
            "connection_count": 1,
            "ready_sender_count": 1,
            "inbound_routing_count": 1,
            "credentials_ref": _redacted(credential_reference, include_last4=False),
            "external_account": _redacted(connection.external_account_id),
            "sender_sid": _redacted(sender.sender_sid),
            "messaging_service_sid": _redacted(sender.messaging_service_sid),
            "webhook_url": _redacted(db_webhook, include_last4=False),
            "status_callback_url": _redacted(db_callback, include_last4=False),
        },
        "provider": provider,
        "checks": {
            "tenant_exact": True,
            "profile_sender_exact": True,
            "employee_and_profile_contact_phones_excluded": True,
            "inbound_route_owned": True,
            "single_ready_sender": True,
            "provider_and_credential_owner_exact": True,
            "webhook_and_callback_exact": True,
        },
        "audit_controls": {
            "database_read_only": True,
            "provider_read_only": True,
            "messages_sent": False,
            "database_mutations": False,
            "provider_mutations": False,
        },
    }
    report["report_sha256"] = _canonical_sha256(report)
    return report


def _failure_payload(reason_code: str) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "status": "blocked",
        "reason_code": reason_code,
        "audit_controls": {
            "messages_sent": False,
            "database_mutations": False,
            "provider_mutations": False,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-environment-variable",
        default=DEFAULT_DATABASE_ENVIRONMENT_VARIABLE,
        help="Environment variable containing the TLS PostgreSQL URL.",
    )
    parser.add_argument(
        "--expected-phone-environment-variable",
        default=DEFAULT_PHONE_ENVIRONMENT_VARIABLE,
    )
    parser.add_argument(
        "--expected-webhook-environment-variable",
        default=DEFAULT_WEBHOOK_ENVIRONMENT_VARIABLE,
    )
    parser.add_argument(
        "--expected-callback-environment-variable",
        default=DEFAULT_CALLBACK_ENVIRONMENT_VARIABLE,
    )
    parser.add_argument(
        "--provider-snapshot-environment-variable",
        default=DEFAULT_PROVIDER_SNAPSHOT_ENVIRONMENT_VARIABLE,
    )
    parser.add_argument(
        "--maximum-provider-snapshot-age-seconds",
        type=int,
        default=900,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    engine = connection = transaction = session = None
    try:
        database_env = _environment_variable_name(
            args.database_environment_variable,
            reason_code="database_environment_variable_name_invalid",
        )
        database_url = _required_environment_value(
            os.environ,
            database_env,
            missing_reason="database_environment_variable_missing",
        )
        database_fingerprint, _database_summary = _database_identity(database_url)
        request = build_request_from_environment(
            database_identity_sha256=database_fingerprint,
            phone_environment_variable=args.expected_phone_environment_variable,
            webhook_environment_variable=args.expected_webhook_environment_variable,
            callback_environment_variable=args.expected_callback_environment_variable,
            maximum_snapshot_age_seconds=args.maximum_provider_snapshot_age_seconds,
        )
        source = EnvironmentProviderSnapshotSource(
            environment_variable=args.provider_snapshot_environment_variable,
            environ=os.environ,
        )
        engine = create_engine(
            _psycopg_engine_url(database_url),
            poolclass=NullPool,
            future=True,
        )
        connection = engine.connect()
        transaction = connection.begin()
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        session = Session(bind=connection, autoflush=False, expire_on_commit=False)
        result = audit_junin_sender(session, request, source)
        # Always roll back even though the audit contract contains no writes.
        transaction.rollback()
        transaction = None
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except SenderAuditError as exc:
        if transaction is not None and transaction.is_active:
            transaction.rollback()
        print(json.dumps(_failure_payload(exc.reason_code), ensure_ascii=True, sort_keys=True))
        return 2
    except Exception:
        if transaction is not None and transaction.is_active:
            transaction.rollback()
        print(json.dumps(_failure_payload("unexpected_audit_failure"), ensure_ascii=True, sort_keys=True))
        return 2
    finally:
        if session is not None:
            session.close()
        if connection is not None:
            connection.close()
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
