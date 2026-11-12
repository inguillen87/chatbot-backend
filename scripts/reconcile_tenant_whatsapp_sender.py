"""Safely reconcile one tenant-owned production WhatsApp sender.

The command is read-only by default and never calls Twilio.  ``--apply`` is
fail-closed behind an approved dry-run digest, two independent evidence IDs,
a PostgreSQL SERIALIZABLE transaction, and a transaction-scoped advisory lock.
Database URLs and optional Twilio resource identifiers are read only from
explicitly named environment variables; their values are never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import create_engine, func, select, text
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


CONTRACT_VERSION = "chatboc.tenant_whatsapp_sender_reconciliation.v1"
DEFAULT_DATABASE_ENVIRONMENT_VARIABLE = "MIGRATIONS_DATABASE_URL"
_ENVIRONMENT_VARIABLE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_E164_PATTERN = re.compile(r"^\+[1-9][0-9]{7,14}$")
_EVIDENCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SENDER_SID_PATTERN = re.compile(r"^XE[A-Za-z0-9]{8,64}$")
_MESSAGING_SERVICE_SID_PATTERN = re.compile(r"^MG[A-Za-z0-9]{8,64}$")
_READY_SENDER_STATUSES = {"online", "approved", "connected", "active"}


class SenderReconciliationError(RuntimeError):
    """Stable, non-sensitive blocker returned to the operator."""

    def __init__(self, reason_code: str):
        self.reason_code = str(reason_code or "sender_reconciliation_blocked")
        super().__init__(self.reason_code)


def _require(condition: bool, reason_code: str) -> None:
    if not condition:
        raise SenderReconciliationError(reason_code)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _redacted_identifier(value: str | None) -> dict[str, Any]:
    rendered = str(value or "").strip()
    return {
        "configured": bool(rendered),
        "sha256": _sha256_text(rendered) if rendered else None,
        "last4": rendered[-4:] if rendered else None,
    }


def _validate_environment_variable_name(value: str, *, reason_code: str) -> str:
    normalized = str(value or "").strip()
    _require(bool(_ENVIRONMENT_VARIABLE_PATTERN.fullmatch(normalized)), reason_code)
    return normalized


def _validate_e164(value: str) -> str:
    normalized = str(value or "").strip()
    _require(bool(_E164_PATTERN.fullmatch(normalized)), "official_phone_e164_invalid")
    return normalized


def _canonical_e164(value: Any) -> str | None:
    rendered = str(value or "").strip()
    if rendered.lower().startswith("whatsapp:"):
        rendered = rendered.split(":", 1)[1].strip()
    digits = "".join(character for character in rendered if character.isdigit())
    candidate = f"+{digits}" if digits else ""
    return candidate if _E164_PATTERN.fullmatch(candidate) else None


def _validate_evidence_id(value: str | None, *, reason_code: str) -> str:
    normalized = str(value or "").strip()
    _require(bool(_EVIDENCE_ID_PATTERN.fullmatch(normalized)), reason_code)
    return normalized


def _validate_sha256(value: str | None, *, reason_code: str) -> str:
    normalized = str(value or "").strip().lower()
    _require(bool(_SHA256_PATTERN.fullmatch(normalized)), reason_code)
    return normalized


def _optional_external_identifier(
    *,
    environ: Mapping[str, str],
    environment_variable_name: str | None,
    pattern: re.Pattern[str],
    missing_reason: str,
    invalid_reason: str,
) -> tuple[str | None, str | None]:
    if not environment_variable_name:
        return None, None
    env_name = _validate_environment_variable_name(
        environment_variable_name,
        reason_code="external_metadata_environment_variable_name_invalid",
    )
    value = str(environ.get(env_name) or "").strip()
    _require(bool(value), missing_reason)
    _require(bool(pattern.fullmatch(value)), invalid_reason)
    return env_name, value


def _database_identity(database_url: str) -> tuple[str, dict[str, Any]]:
    try:
        parsed = make_url(str(database_url or "").strip())
    except Exception as exc:
        raise SenderReconciliationError("database_url_invalid") from exc
    backend = parsed.get_backend_name()
    _require(backend == "postgresql", "database_postgresql_required")
    host = str(parsed.host or "").strip().lower()
    database = str(parsed.database or "").strip()
    _require(bool(host and database), "database_identity_incomplete")
    sslmode = str(parsed.query.get("sslmode") or "").strip().lower()
    _require(sslmode in {"require", "verify-ca", "verify-full"}, "database_tls_required")
    identity_document = {
        "backend": backend,
        "host": host,
        "port": int(parsed.port or 5432),
        "database": database,
    }
    fingerprint = _canonical_sha256(identity_document)
    return fingerprint, {
        "backend": backend,
        "identity_sha256": fingerprint,
        "tls": True,
    }


def _psycopg_engine_url(database_url: str):
    """Select the installed psycopg v3 driver without changing DSN fields."""

    return make_url(database_url).set(drivername="postgresql+psycopg")


@dataclass(frozen=True)
class ReconciliationRequest:
    tenant_slug: str
    official_phone: str
    provider: str
    environment: str
    database_identity_sha256: str
    sender_sid_environment_variable: str | None = None
    sender_sid: str | None = None
    messaging_service_sid_environment_variable: str | None = None
    messaging_service_sid: str | None = None
    twilio_read_only_evidence_id: str | None = None
    cutover_window_evidence_id: str | None = None


@dataclass
class InspectedState:
    tenant: TenantProfile
    connection: ProviderConnection
    sender: ProviderSender | None
    proposed_sender: dict[str, Any]
    proposed_metadata: dict[str, Any]
    sender_action: str
    profile_action: str


def build_request(
    *,
    tenant_slug: str,
    official_phone: str,
    provider: str,
    environment: str,
    database_identity_sha256: str,
    sender_sid_environment_variable: str | None = None,
    messaging_service_sid_environment_variable: str | None = None,
    twilio_read_only_evidence_id: str | None = None,
    cutover_window_evidence_id: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ReconciliationRequest:
    source = environ if environ is not None else os.environ
    normalized_slug = str(tenant_slug or "").strip()
    _require(bool(normalized_slug), "tenant_slug_invalid")
    _require(len(normalized_slug) <= 80, "tenant_slug_invalid")
    normalized_provider = str(provider or "").strip().lower()
    normalized_environment = str(environment or "").strip().lower()
    _require(normalized_provider == "twilio", "provider_twilio_required")
    _require(normalized_environment == "production", "environment_production_required")
    database_fingerprint = _validate_sha256(
        database_identity_sha256,
        reason_code="database_identity_sha256_invalid",
    )
    sender_env, sender_sid = _optional_external_identifier(
        environ=source,
        environment_variable_name=sender_sid_environment_variable,
        pattern=_SENDER_SID_PATTERN,
        missing_reason="sender_sid_environment_variable_missing",
        invalid_reason="sender_sid_invalid",
    )
    service_env, service_sid = _optional_external_identifier(
        environ=source,
        environment_variable_name=messaging_service_sid_environment_variable,
        pattern=_MESSAGING_SERVICE_SID_PATTERN,
        missing_reason="messaging_service_sid_environment_variable_missing",
        invalid_reason="messaging_service_sid_invalid",
    )
    return ReconciliationRequest(
        tenant_slug=normalized_slug,
        official_phone=_validate_e164(official_phone),
        provider=normalized_provider,
        environment=normalized_environment,
        database_identity_sha256=database_fingerprint,
        sender_sid_environment_variable=sender_env,
        sender_sid=sender_sid,
        messaging_service_sid_environment_variable=service_env,
        messaging_service_sid=service_sid,
        twilio_read_only_evidence_id=(
            _validate_evidence_id(
                twilio_read_only_evidence_id,
                reason_code="twilio_read_only_evidence_id_invalid",
            )
            if twilio_read_only_evidence_id
            else None
        ),
        cutover_window_evidence_id=(
            _validate_evidence_id(
                cutover_window_evidence_id,
                reason_code="cutover_window_evidence_id_invalid",
            )
            if cutover_window_evidence_id
            else None
        ),
    )


def _query_all(session: Session, model, *, lock_rows: bool) -> list[Any]:
    # Conflict discovery intentionally remains non-locking.  Apply mode runs
    # at SERIALIZABLE isolation and locks only the rows it may mutate; locking
    # every tenant/sender row would stall unrelated tenants.
    del lock_rows
    return list(session.execute(select(model)).scalars().all())


def _lock_row(session: Session, model, row_id: int) -> Any:
    return session.execute(
        select(model).where(model.id == int(row_id)).with_for_update()
    ).scalar_one()


def _tenant_config_numbers(tenant: TenantProfile) -> tuple[str, ...]:
    config = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    configured = config.get("whatsapp_numbers")
    if isinstance(configured, (str, int)):
        values = [configured]
    elif isinstance(configured, (list, tuple, set)):
        values = configured
    else:
        values = []
    return tuple(
        normalized
        for normalized in (_canonical_e164(value) for value in values)
        if normalized
    )


def _tenant_owner_id(tenant: TenantProfile) -> int:
    owner_ids = {
        int(value)
        for value in (tenant.municipio_id, tenant.pyme_id)
        if value not in (None, "") and int(value) > 0
    }
    _require(len(owner_ids) == 1, "tenant_owner_missing_or_ambiguous")
    return next(iter(owner_ids))


def _sender_matches_phone(sender: ProviderSender, phone: str) -> bool:
    return phone in {
        _canonical_e164(sender.phone_number),
        _canonical_e164(sender.sender_id),
    }


def _proposed_metadata(
    existing: ProviderSender | None,
    request: ReconciliationRequest,
) -> dict[str, Any]:
    metadata = dict(existing.metadata_json) if existing and isinstance(existing.metadata_json, dict) else {}
    metadata["ownership_reconciliation"] = {
        "contract_version": CONTRACT_VERSION,
        "provider": request.provider,
        "environment": request.environment,
        "database_identity_sha256": request.database_identity_sha256,
        "official_phone_sha256": _sha256_text(request.official_phone),
        "sender_sid_source_environment_variable": request.sender_sid_environment_variable,
        "sender_sid_sha256": _sha256_text(request.sender_sid) if request.sender_sid else None,
        "messaging_service_sid_source_environment_variable": (
            request.messaging_service_sid_environment_variable
        ),
        "messaging_service_sid_sha256": (
            _sha256_text(request.messaging_service_sid)
            if request.messaging_service_sid
            else None
        ),
    }
    return metadata


def inspect_reconciliation_state(
    session: Session,
    request: ReconciliationRequest,
    *,
    lock_rows: bool = False,
) -> InspectedState:
    tenants = _query_all(session, TenantProfile, lock_rows=lock_rows)
    exact_tenants = [tenant for tenant in tenants if str(tenant.slug or "") == request.tenant_slug]
    if not exact_tenants and any(
        str(tenant.slug or "").lower() == request.tenant_slug.lower()
        for tenant in tenants
    ):
        raise SenderReconciliationError("tenant_slug_exact_mismatch")
    _require(len(exact_tenants) == 1, "tenant_exactly_one_required")
    tenant = exact_tenants[0]
    if lock_rows:
        tenant = _lock_row(session, TenantProfile, tenant.id)
    _require(tenant.is_active is True, "tenant_inactive")
    owner_id = _tenant_owner_id(tenant)
    shared_active_owners = [
        candidate
        for candidate in tenants
        if candidate.id != tenant.id
        and candidate.is_active is True
        and owner_id
        in {
            int(value)
            for value in (candidate.municipio_id, candidate.pyme_id)
            if value not in (None, "")
        }
    ]
    _require(not shared_active_owners, "tenant_owner_claimed_by_other_active_tenant")

    phone_claiming_tenants = []
    for candidate in tenants:
        claims = {
            _canonical_e164(candidate.whatsapp_sender_id),
            *_tenant_config_numbers(candidate),
        }
        if request.official_phone in claims:
            phone_claiming_tenants.append(candidate)
    cross_tenant_claims = [candidate for candidate in phone_claiming_tenants if candidate.id != tenant.id]
    _require(not cross_tenant_claims, "official_phone_claimed_by_other_tenant")

    legacy_rows = _query_all(session, WhatsappNumero, lock_rows=lock_rows)
    matching_legacy = [
        row
        for row in legacy_rows
        if _canonical_e164(row.numero_whatsapp) == request.official_phone
    ]
    _require(len(matching_legacy) == 1, "legacy_whatsapp_number_exactly_one_required")
    legacy = matching_legacy[0]
    if lock_rows:
        legacy = _lock_row(session, WhatsappNumero, legacy.id)
    _require(legacy.is_active is True, "legacy_whatsapp_number_inactive")
    _require(int(legacy.user_id) == owner_id, "legacy_whatsapp_number_owner_conflict")

    connections = [
        connection
        for connection in _query_all(session, ProviderConnection, lock_rows=lock_rows)
        if int(connection.tenant_id) == int(tenant.id)
        and str(connection.provider or "").strip().lower() == request.provider
        and str(connection.channel or "").strip().lower() == "whatsapp"
        and str(connection.environment or "").strip().lower() == request.environment
    ]
    _require(len(connections) == 1, "provider_connection_exactly_one_required")
    connection = connections[0]
    if lock_rows:
        connection = _lock_row(session, ProviderConnection, connection.id)
    _require(bool(str(connection.credentials_ref or "").strip()), "provider_connection_credentials_ref_missing")

    all_senders = _query_all(session, ProviderSender, lock_rows=lock_rows)
    matching_senders = [
        sender
        for sender in all_senders
        if str(sender.channel or "").strip().lower() == "whatsapp"
        and _sender_matches_phone(sender, request.official_phone)
    ]
    _require(
        not any(int(sender.tenant_id) != int(tenant.id) for sender in matching_senders),
        "official_phone_claimed_by_other_provider_sender",
    )
    target_matching = [sender for sender in matching_senders if int(sender.tenant_id) == int(tenant.id)]
    _require(len(target_matching) <= 1, "target_provider_sender_ambiguous")
    sender = target_matching[0] if target_matching else None
    if lock_rows and sender is not None:
        sender = _lock_row(session, ProviderSender, sender.id)

    for other in all_senders:
        if sender is not None and other.id == sender.id:
            continue
        if request.sender_sid and str(other.sender_sid or "").strip() == request.sender_sid:
            raise SenderReconciliationError("sender_sid_conflict")
        if (
            request.messaging_service_sid
            and str(other.messaging_service_sid or "").strip() == request.messaging_service_sid
            and int(other.tenant_id) != int(tenant.id)
        ):
            raise SenderReconciliationError("messaging_service_sid_cross_tenant_conflict")
        if (
            int(other.tenant_id) == int(tenant.id)
            and int(other.provider_connection_id or 0) == int(connection.id)
            and str(other.channel or "").strip().lower() == "whatsapp"
            and str(other.status or "").strip().lower() in _READY_SENDER_STATUSES
        ):
            raise SenderReconciliationError("target_production_sender_ready_conflict")

    if sender is not None:
        _require(
            sender.provider_connection_id in (None, connection.id),
            "target_provider_sender_connection_conflict",
        )
        if request.sender_sid and str(sender.sender_sid or "").strip():
            _require(sender.sender_sid == request.sender_sid, "target_sender_sid_mismatch")
        if request.messaging_service_sid and str(sender.messaging_service_sid or "").strip():
            _require(
                sender.messaging_service_sid == request.messaging_service_sid,
                "target_messaging_service_sid_mismatch",
            )

    metadata = _proposed_metadata(sender, request)
    proposed = {
        "provider_connection_id": int(connection.id),
        "channel": "whatsapp",
        "sender_type": "whatsapp_business",
        "phone_number": request.official_phone,
        "sender_id": f"whatsapp:{request.official_phone}",
        "sender_sid": request.sender_sid or (sender.sender_sid if sender else None),
        "messaging_service_sid": (
            request.messaging_service_sid
            or (sender.messaging_service_sid if sender else None)
        ),
        "display_name": (sender.display_name if sender and sender.display_name else tenant.nombre),
        "status": (
            sender.status
            if sender
            else ("registered" if request.sender_sid else "draft")
        ),
        "metadata_json": metadata,
    }
    if sender is None:
        sender_action = "create"
    else:
        changed = any(getattr(sender, key) != value for key, value in proposed.items())
        sender_action = "update" if changed else "noop"
    expected_profile_sender = f"whatsapp:{request.official_phone}"
    profile_action = "noop" if tenant.whatsapp_sender_id == expected_profile_sender else "align"
    return InspectedState(
        tenant=tenant,
        connection=connection,
        sender=sender,
        proposed_sender=proposed,
        proposed_metadata=metadata,
        sender_action=sender_action,
        profile_action=profile_action,
    )


def _plan_document(state: InspectedState, request: ReconciliationRequest) -> dict[str, Any]:
    current_sender = state.sender
    return {
        "contract_version": CONTRACT_VERSION,
        "database_identity_sha256": request.database_identity_sha256,
        "tenant": {
            "id": int(state.tenant.id),
            "slug": state.tenant.slug,
            "active": True,
        },
        "provider": request.provider,
        "environment": request.environment,
        "channel": "whatsapp",
        "official_phone": _redacted_identifier(request.official_phone),
        "connection": {
            "id": int(state.connection.id),
            "credentials_ref_present": True,
            "credentials_ref_sha256": _sha256_text(state.connection.credentials_ref),
            "external_account": _redacted_identifier(
                state.connection.external_account_id
            ),
            "status": str(state.connection.status or ""),
        },
        "current": {
            "provider_sender_id": int(current_sender.id) if current_sender else None,
            "profile_sender": _redacted_identifier(state.tenant.whatsapp_sender_id),
            "sender_sid": _redacted_identifier(current_sender.sender_sid if current_sender else None),
            "messaging_service_sid": _redacted_identifier(
                current_sender.messaging_service_sid if current_sender else None
            ),
        },
        "proposed": {
            "sender_action": state.sender_action,
            "profile_action": state.profile_action,
            "sender_sid": _redacted_identifier(state.proposed_sender.get("sender_sid")),
            "messaging_service_sid": _redacted_identifier(
                state.proposed_sender.get("messaging_service_sid")
            ),
            "status": state.proposed_sender["status"],
        },
        "conflicts": 0,
        "provider_calls_performed": False,
        "messages_sent": False,
        "deletes_planned": 0,
    }


def build_reconciliation_plan(
    session: Session,
    request: ReconciliationRequest,
    *,
    lock_rows: bool = False,
) -> tuple[InspectedState, dict[str, Any], str]:
    state = inspect_reconciliation_state(session, request, lock_rows=lock_rows)
    plan = _plan_document(state, request)
    return state, plan, _canonical_sha256(plan)


def _advisory_lock_key(request: ReconciliationRequest) -> int:
    material = (
        f"{CONTRACT_VERSION}:{request.database_identity_sha256}:"
        f"{request.tenant_slug}:{request.official_phone}"
    )
    key = int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest()[:8], "big")
    return key & ((1 << 63) - 1) or 1


def acquire_postgres_advisory_lock(session: Session, request: ReconciliationRequest) -> None:
    _require(session.get_bind().dialect.name == "postgresql", "apply_postgresql_required")
    session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _advisory_lock_key(request)},
    )


def apply_inspected_state(session: Session, state: InspectedState) -> ProviderSender:
    sender = state.sender
    if sender is None:
        sender = ProviderSender(
            tenant_id=int(state.tenant.id),
            **state.proposed_sender,
        )
        session.add(sender)
    else:
        for key, value in state.proposed_sender.items():
            setattr(sender, key, value)
    state.tenant.whatsapp_sender_id = state.proposed_sender["sender_id"]
    session.flush()
    return sender


def reconcile_tenant_whatsapp_sender(
    session: Session,
    request: ReconciliationRequest,
    *,
    apply: bool = False,
    approved_plan_sha256: str | None = None,
) -> dict[str, Any]:
    if apply:
        _validate_evidence_id(
            request.twilio_read_only_evidence_id,
            reason_code="twilio_read_only_evidence_id_required",
        )
        _validate_evidence_id(
            request.cutover_window_evidence_id,
            reason_code="cutover_window_evidence_id_required",
        )
        approved = _validate_sha256(
            approved_plan_sha256,
            reason_code="approved_plan_sha256_required",
        )
        acquire_postgres_advisory_lock(session, request)
        state, plan, plan_sha256 = build_reconciliation_plan(
            session,
            request,
            lock_rows=True,
        )
        _require(approved == plan_sha256, "approved_plan_sha256_mismatch")
        writes_planned = state.sender_action != "noop" or state.profile_action != "noop"
        sender = apply_inspected_state(session, state) if writes_planned else state.sender
        _require(sender is not None, "provider_sender_postcondition_missing")
        _require(int(sender.tenant_id) == int(state.tenant.id), "provider_sender_postcondition_tenant_mismatch")
        _require(
            _sender_matches_phone(sender, request.official_phone),
            "provider_sender_postcondition_phone_mismatch",
        )
        _require(
            state.tenant.whatsapp_sender_id == f"whatsapp:{request.official_phone}",
            "tenant_profile_sender_postcondition_mismatch",
        )
        post_state, _, _ = build_reconciliation_plan(session, request)
        _require(post_state.sender_action == "noop", "provider_sender_postcondition_drift")
        _require(post_state.profile_action == "noop", "tenant_profile_sender_postcondition_drift")
        status = "applied" if writes_planned else "already_reconciled"
        writes_performed = writes_planned
    else:
        state, plan, plan_sha256 = build_reconciliation_plan(session, request)
        status = "dry_run"
        writes_performed = False

    return {
        "contract_version": CONTRACT_VERSION,
        "status": status,
        "dry_run": not apply,
        "writes_performed": writes_performed,
        "plan_sha256": plan_sha256,
        "plan": plan,
        "evidence": {
            "twilio_read_only_evidence_sha256": (
                _sha256_text(request.twilio_read_only_evidence_id)
                if request.twilio_read_only_evidence_id
                else None
            ),
            "cutover_window_evidence_sha256": (
                _sha256_text(request.cutover_window_evidence_id)
                if request.cutover_window_evidence_id
                else None
            ),
        },
    }


def _failure_payload(
    reason_code: str,
    *,
    dry_run: bool = True,
    error_type: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "status": "blocked",
        "dry_run": bool(dry_run),
        "writes_performed": False,
        "reason_code": reason_code,
        "provider_calls_performed": False,
        "messages_sent": False,
    }
    if error_type:
        payload["error_type"] = error_type
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-slug", required=True)
    parser.add_argument("--official-phone", required=True)
    parser.add_argument("--provider", default="twilio", choices=["twilio"])
    parser.add_argument("--environment", default="production", choices=["production"])
    parser.add_argument(
        "--database-environment-variable",
        default=DEFAULT_DATABASE_ENVIRONMENT_VARIABLE,
        help="Environment variable containing the PostgreSQL TLS URL; its value is never printed.",
    )
    parser.add_argument("--sender-sid-environment-variable")
    parser.add_argument("--messaging-service-sid-environment-variable")
    parser.add_argument("--twilio-read-only-evidence-id")
    parser.add_argument("--cutover-window-evidence-id")
    parser.add_argument("--approved-plan-sha256")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the approved plan atomically. Without this flag the command is read-only.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    engine = None
    connection = None
    transaction = None
    session = None
    try:
        database_env = _validate_environment_variable_name(
            args.database_environment_variable,
            reason_code="database_environment_variable_name_invalid",
        )
        database_url = str(os.environ.get(database_env) or "").strip()
        _require(bool(database_url), "database_environment_variable_missing")
        database_fingerprint, database_summary = _database_identity(database_url)
        request = build_request(
            tenant_slug=args.tenant_slug,
            official_phone=args.official_phone,
            provider=args.provider,
            environment=args.environment,
            database_identity_sha256=database_fingerprint,
            sender_sid_environment_variable=args.sender_sid_environment_variable,
            messaging_service_sid_environment_variable=(
                args.messaging_service_sid_environment_variable
            ),
            twilio_read_only_evidence_id=args.twilio_read_only_evidence_id,
            cutover_window_evidence_id=args.cutover_window_evidence_id,
            environ=os.environ,
        )
        engine_url = _psycopg_engine_url(database_url)
        engine = create_engine(
            engine_url,
            poolclass=NullPool,
            hide_parameters=True,
            connect_args={
                "application_name": "chatboc_sender_reconciler",
                "connect_timeout": 10,
            },
        )
        isolation_level = "SERIALIZABLE" if args.apply else "REPEATABLE READ"
        connection = engine.connect().execution_options(isolation_level=isolation_level)
        transaction = connection.begin()
        if not args.apply:
            connection.execute(text("SET TRANSACTION READ ONLY"))
        connection.execute(text("SET LOCAL statement_timeout = '30s'"))
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        session = Session(bind=connection, autoflush=False, expire_on_commit=False)
        result = reconcile_tenant_whatsapp_sender(
            session,
            request,
            apply=bool(args.apply),
            approved_plan_sha256=args.approved_plan_sha256,
        )
        result["database"] = database_summary
        if args.apply:
            transaction.commit()
        else:
            transaction.rollback()
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except SenderReconciliationError as exc:
        if transaction is not None and transaction.is_active:
            transaction.rollback()
        print(
            json.dumps(
                _failure_payload(exc.reason_code, dry_run=not bool(args.apply)),
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 2
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        if transaction is not None and transaction.is_active:
            transaction.rollback()
        print(
            json.dumps(
                _failure_payload(
                    "sender_reconciliation_unexpected_error",
                    dry_run=not bool(args.apply),
                    error_type=type(exc).__name__,
                ),
                ensure_ascii=True,
                sort_keys=True,
            )
        )
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
