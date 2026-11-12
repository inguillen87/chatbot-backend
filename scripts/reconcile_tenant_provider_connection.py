"""Safely reconcile one tenant-owned Twilio WhatsApp connection.

The command is read-only by default and has no Twilio client or messaging
transport.  ``--apply`` is fail-closed behind an approved dry-run digest, a
provider read-only evidence ID, a cutover-window evidence ID, a PostgreSQL
SERIALIZABLE transaction, and a transaction-scoped advisory lock.

Database URLs and the Twilio account SID are read only from explicitly named
environment variables.  ``credentials_ref`` stores an environment-variable
*name* and this module deliberately never reads the referenced secret.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

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
    MANAGED_CONNECTION_CONTRACT_VERSION,
    MANAGED_CONNECTION_MARKER,
    advisory_lock_keys,
)


CONTRACT_VERSION = MANAGED_CONNECTION_CONTRACT_VERSION
DEFAULT_DATABASE_ENVIRONMENT_VARIABLE = "MIGRATIONS_DATABASE_URL"
PROVIDER = "twilio"
CHANNEL = "whatsapp"
ENVIRONMENT = "production"
PENDING_VERIFICATION_STATUS = "pending_provider_verification"
READY_STATUSES = frozenset({"online", "approved", "connected", "active"})
MANAGEMENT_MARKER = MANAGED_CONNECTION_MARKER

_ENVIRONMENT_VARIABLE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_ACCOUNT_SID_PATTERN = re.compile(r"^AC[0-9A-Fa-f]{32}$")
_EVIDENCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ProviderConnectionReconciliationError(RuntimeError):
    """Stable, non-sensitive blocker returned to the operator."""

    def __init__(self, reason_code: str):
        self.reason_code = str(
            reason_code or "provider_connection_reconciliation_blocked"
        )
        super().__init__(self.reason_code)


def _require(condition: bool, reason_code: str) -> None:
    if not condition:
        raise ProviderConnectionReconciliationError(reason_code)


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


def _redacted_identifier(value: Any) -> dict[str, Any]:
    rendered = _clean(value)
    return {
        "configured": bool(rendered),
        "sha256": _sha256_text(rendered) if rendered else None,
        "last4": rendered[-4:] if rendered else None,
    }


def _canonical_account_sid(value: Any) -> str | None:
    rendered = _clean(value)
    if not _ACCOUNT_SID_PATTERN.fullmatch(rendered):
        return None
    return f"AC{rendered[2:].lower()}"


def _environment_variable_name(value: str, *, reason_code: str) -> str:
    rendered = _clean(value)
    _require(bool(_ENVIRONMENT_VARIABLE_PATTERN.fullmatch(rendered)), reason_code)
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


def _evidence_id(value: str | None, *, reason_code: str) -> str:
    rendered = _clean(value)
    _require(bool(_EVIDENCE_ID_PATTERN.fullmatch(rendered)), reason_code)
    return rendered


def _sha256(value: str | None, *, reason_code: str) -> str:
    rendered = _clean(value).lower()
    _require(bool(_SHA256_PATTERN.fullmatch(rendered)), reason_code)
    return rendered


def _database_identity(database_url: str) -> tuple[str, dict[str, Any]]:
    try:
        parsed = make_url(_clean(database_url))
    except Exception as exc:
        raise ProviderConnectionReconciliationError("database_url_invalid") from exc
    _require(parsed.get_backend_name() == "postgresql", "database_postgresql_required")
    host = _clean(parsed.host).lower()
    database = _clean(parsed.database)
    _require(bool(host and database), "database_identity_incomplete")
    sslmode = _clean(parsed.query.get("sslmode")).lower()
    _require(
        sslmode in {"require", "verify-ca", "verify-full"},
        "database_tls_required",
    )
    identity_document = {
        "backend": "postgresql",
        "host": host,
        "port": int(parsed.port or 5432),
        "database": database,
    }
    fingerprint = _canonical_sha256(identity_document)
    return fingerprint, {
        "backend": "postgresql",
        "identity_sha256": fingerprint,
        "tls": True,
    }


def _psycopg_engine_url(database_url: str):
    return make_url(database_url).set(drivername="postgresql+psycopg")


@dataclass(frozen=True)
class ReconciliationRequest:
    tenant_slug: str
    database_identity_sha256: str
    external_account_environment_variable: str
    credentials_environment_variable: str
    external_account_id: str = field(repr=False)
    credentials_ref: str = field(repr=False)
    provider_read_only_evidence_id: str | None = field(default=None, repr=False)
    cutover_window_evidence_id: str | None = field(default=None, repr=False)


@dataclass
class InspectedState:
    tenant: TenantProfile
    connection: ProviderConnection | None
    proposed: dict[str, Any]
    action: str


def build_request(
    *,
    tenant_slug: str,
    database_identity_sha256: str,
    external_account_environment_variable: str,
    credentials_environment_variable: str,
    provider_read_only_evidence_id: str | None = None,
    cutover_window_evidence_id: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ReconciliationRequest:
    source = environ if environ is not None else os.environ
    slug = _clean(tenant_slug)
    _require(bool(slug) and len(slug) <= 80, "tenant_slug_invalid")
    account_env = _environment_variable_name(
        external_account_environment_variable,
        reason_code="external_account_environment_variable_name_invalid",
    )
    account_sid = _required_environment_value(
        source,
        account_env,
        missing_reason="external_account_environment_variable_missing",
    )
    canonical_account_sid = _canonical_account_sid(account_sid)
    _require(bool(canonical_account_sid), "external_account_id_invalid")
    credential_env = _environment_variable_name(
        credentials_environment_variable,
        reason_code="credentials_environment_variable_name_invalid",
    )
    _require(
        credential_env != account_env,
        "credentials_environment_variable_must_differ_from_account",
    )
    # Deliberately do not access source[credential_env].  The referenced value
    # is a runtime secret and its existence/content is outside this DB-only tool.
    credentials_ref = f"env:{credential_env}"
    return ReconciliationRequest(
        tenant_slug=slug,
        database_identity_sha256=_sha256(
            database_identity_sha256,
            reason_code="database_identity_sha256_invalid",
        ),
        external_account_environment_variable=account_env,
        credentials_environment_variable=credential_env,
        external_account_id=canonical_account_sid,
        credentials_ref=credentials_ref,
        provider_read_only_evidence_id=(
            _evidence_id(
                provider_read_only_evidence_id,
                reason_code="provider_read_only_evidence_id_invalid",
            )
            if provider_read_only_evidence_id
            else None
        ),
        cutover_window_evidence_id=(
            _evidence_id(
                cutover_window_evidence_id,
                reason_code="cutover_window_evidence_id_invalid",
            )
            if cutover_window_evidence_id
            else None
        ),
    )


def _query_all(session: Session, model) -> list[Any]:
    return list(session.execute(select(model)).scalars().all())


def _lock_row(session: Session, model, row_id: int) -> Any:
    return session.execute(
        select(model).where(model.id == int(row_id)).with_for_update()
    ).scalar_one()


def _tenant_owner_id(tenant: TenantProfile) -> int:
    owners = {
        int(value)
        for value in (tenant.municipio_id, tenant.pyme_id)
        if value not in (None, "") and int(value) > 0
    }
    _require(len(owners) == 1, "tenant_owner_missing_or_ambiguous")
    return next(iter(owners))


def _connection_scope(connection: ProviderConnection) -> tuple[str, str, str]:
    return (
        _clean(connection.provider).lower(),
        _clean(connection.channel).lower(),
        _clean(connection.environment).lower(),
    )


def inspect_reconciliation_state(
    session: Session,
    request: ReconciliationRequest,
    *,
    lock_rows: bool = False,
) -> InspectedState:
    tenants = _query_all(session, TenantProfile)
    exact = [tenant for tenant in tenants if _clean(tenant.slug) == request.tenant_slug]
    if not exact and any(
        _clean(tenant.slug).lower() == request.tenant_slug.lower()
        for tenant in tenants
    ):
        raise ProviderConnectionReconciliationError("tenant_slug_exact_mismatch")
    _require(len(exact) == 1, "tenant_exactly_one_required")
    tenant = exact[0]
    if lock_rows:
        tenant = _lock_row(session, TenantProfile, tenant.id)
    _require(tenant.is_active is True, "tenant_inactive")
    owner_id = _tenant_owner_id(tenant)
    shared_active_owner = any(
        candidate.id != tenant.id
        and candidate.is_active is True
        and owner_id
        in {
            int(value)
            for value in (candidate.municipio_id, candidate.pyme_id)
            if value not in (None, "")
        }
        for candidate in tenants
    )
    _require(
        not shared_active_owner,
        "tenant_owner_claimed_by_other_active_tenant",
    )

    all_connections = _query_all(session, ProviderConnection)
    expected_scope = (PROVIDER, CHANNEL, ENVIRONMENT)
    tenant_connections = [
        connection
        for connection in all_connections
        if int(connection.tenant_id) == int(tenant.id)
    ]
    scoped = [
        connection
        for connection in tenant_connections
        if _connection_scope(connection) == expected_scope
    ]
    _require(len(scoped) <= 1, "provider_connection_more_than_one")
    connection = scoped[0] if scoped else None
    if connection is None:
        repair_candidates = [
            candidate
            for candidate in tenant_connections
            if _clean(candidate.status).lower() in READY_STATUSES
            and _canonical_account_sid(candidate.external_account_id)
            in (None, request.external_account_id)
            and _clean(candidate.credentials_ref)
            in ("", request.credentials_ref)
            and all(
                not current or current == expected
                for current, expected in zip(
                    _connection_scope(candidate),
                    expected_scope,
                )
            )
        ]
        _require(
            len(repair_candidates) <= 1,
            "provider_connection_repair_candidate_ambiguous",
        )
        connection = repair_candidates[0] if repair_candidates else None
    if lock_rows and connection is not None:
        connection = _lock_row(session, ProviderConnection, connection.id)

    account_claims = [
        candidate
        for candidate in all_connections
        if _canonical_account_sid(candidate.external_account_id)
        == request.external_account_id
    ]
    _require(
        not any(int(candidate.tenant_id) != int(tenant.id) for candidate in account_claims),
        "external_account_claimed_by_other_tenant",
    )
    _require(
        not any(
            (connection is None or candidate.id != connection.id)
            and _connection_scope(candidate) != expected_scope
            for candidate in account_claims
        ),
        "external_account_claimed_by_other_connection_scope",
    )

    if connection is not None:
        current_account = _canonical_account_sid(connection.external_account_id)
        raw_current_account = _clean(connection.external_account_id)
        _require(
            not raw_current_account or current_account == request.external_account_id,
            "provider_connection_external_account_mismatch",
        )
        current_ref = _clean(connection.credentials_ref)
        _require(
            not current_ref or current_ref == request.credentials_ref,
            "provider_connection_credentials_ref_mismatch",
        )
        # An account rebind can redirect already-bound senders.  The checks
        # above make that impossible; this extra assertion documents the
        # dependency and fails closed if the model changes.
        dependent_senders = [
            sender
            for sender in _query_all(session, ProviderSender)
            if int(sender.provider_connection_id or 0) == int(connection.id)
        ]
        _require(
            not dependent_senders or current_account == request.external_account_id,
            "provider_connection_with_senders_cannot_rebind_account",
        )

    current_status = _clean(connection.status).lower() if connection is not None else ""
    existing_config = (
        dict(connection.config)
        if connection is not None and isinstance(connection.config, dict)
        else {}
    )
    management_marker = existing_config.get(MANAGEMENT_MARKER)
    def _sha256_marker(value: Any) -> bool:
        cleaned = _clean(value).lower()
        return len(cleaned) == 64 and all(ch in "0123456789abcdef" for ch in cleaned)

    def _revision_marker(value: Any) -> bool:
        cleaned = _clean(value).lower()
        return 7 <= len(cleaned) <= 64 and all(
            ch in "0123456789abcdef" for ch in cleaned
        )

    managed_complete = (
        isinstance(management_marker, dict)
        and management_marker.get("enabled") is True
        and management_marker.get("contract_version") == CONTRACT_VERSION
        and management_marker.get("promotion_required") is False
        and _sha256_marker(management_marker.get("provider_snapshot_sha256"))
        and _sha256_marker(management_marker.get("credential_attestation_sha256"))
        and _revision_marker(management_marker.get("destination_deployment_revision"))
    )
    identity_complete = (
        connection is not None
        and _connection_scope(connection) == expected_scope
        and _canonical_account_sid(connection.external_account_id)
        == request.external_account_id
        and _clean(connection.credentials_ref) == request.credentials_ref
        and managed_complete
    )
    proposed_status = (
        _clean(connection.status)
        if current_status in READY_STATUSES and identity_complete
        else PENDING_VERIFICATION_STATUS
    )
    proposed_config = dict(existing_config)
    if proposed_status in READY_STATUSES and managed_complete:
        proposed_config[MANAGEMENT_MARKER] = dict(management_marker)
    else:
        proposed_config[MANAGEMENT_MARKER] = {
            "contract_version": CONTRACT_VERSION,
            "enabled": True,
            "promotion_required": True,
        }
    proposed = {
        "provider": PROVIDER,
        "channel": CHANNEL,
        "environment": ENVIRONMENT,
        "status": proposed_status,
        "display_name": (
            connection.display_name
            if connection is not None and _clean(connection.display_name)
            else f"{_clean(tenant.nombre)} - Twilio WhatsApp"[:255]
        ),
        "external_account_id": request.external_account_id,
        "credentials_ref": request.credentials_ref,
        "config": proposed_config,
    }
    if connection is None:
        action = "create"
    else:
        action = (
            "update"
            if any(getattr(connection, key) != value for key, value in proposed.items())
            else "noop"
        )
    return InspectedState(
        tenant=tenant,
        connection=connection,
        proposed=proposed,
        action=action,
    )


def _plan_document(
    state: InspectedState,
    request: ReconciliationRequest,
) -> dict[str, Any]:
    current = state.connection
    return {
        "contract_version": CONTRACT_VERSION,
        "database_identity_sha256": request.database_identity_sha256,
        "tenant": {
            "id": int(state.tenant.id),
            "slug": state.tenant.slug,
            "active": True,
            "owner_unique": True,
            "owner_id": _tenant_owner_id(state.tenant),
            "owner_type": (
                "municipio" if state.tenant.municipio_id is not None else "pyme"
            ),
        },
        "scope": {
            "provider": PROVIDER,
            "channel": CHANNEL,
            "environment": ENVIRONMENT,
        },
        "current": {
            "connection_id": int(current.id) if current is not None else None,
            "external_account": _redacted_identifier(
                current.external_account_id if current is not None else None
            ),
            "credentials_ref_configured": bool(
                current is not None and _clean(current.credentials_ref)
            ),
            "credentials_ref_sha256": (
                _sha256_text(current.credentials_ref)
                if current is not None and _clean(current.credentials_ref)
                else None
            ),
            "status": _clean(current.status) if current is not None else None,
        },
        "proposed": {
            "action": state.action,
            "external_account": _redacted_identifier(request.external_account_id),
            "external_account_source_environment_variable_sha256": _sha256_text(
                request.external_account_environment_variable
            ),
            "credentials_environment_variable_sha256": _sha256_text(
                request.credentials_environment_variable
            ),
            "credentials_ref_sha256": _sha256_text(request.credentials_ref),
            "display_name_sha256": _sha256_text(state.proposed["display_name"]),
            "managed_config_sha256": _canonical_sha256(
                state.proposed["config"]
            ),
            "status": state.proposed["status"],
            "requires_provider_verification": (
                _clean(state.proposed["status"]).lower()
                == PENDING_VERIFICATION_STATUS
            ),
        },
        "evidence_binding": {
            "configuration_evidence_sha256": (
                _sha256_text(request.provider_read_only_evidence_id)
                if request.provider_read_only_evidence_id
                else None
            ),
            "cutover_window_evidence_sha256": (
                _sha256_text(request.cutover_window_evidence_id)
                if request.cutover_window_evidence_id
                else None
            ),
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


def _advisory_lock_keys(request: ReconciliationRequest) -> tuple[int, ...]:
    return advisory_lock_keys(
        database_identity_sha256=request.database_identity_sha256,
        tenant_slug=request.tenant_slug,
        external_account_id=request.external_account_id,
    )


def acquire_postgres_advisory_lock(
    session: Session,
    request: ReconciliationRequest,
) -> None:
    bind = session.get_bind()
    _require(
        bind.dialect.name == "postgresql",
        "apply_postgresql_required",
    )
    isolation_reader = getattr(bind, "get_isolation_level", None)
    isolation = isolation_reader() if callable(isolation_reader) else None
    _require(
        _clean(isolation).upper() == "SERIALIZABLE",
        "apply_serializable_transaction_required",
    )
    # Never wait on a lock after SERIALIZABLE has acquired its snapshot.  A
    # contended run fails and must be retried in a fresh transaction.
    for lock_key in _advisory_lock_keys(request):
        acquired = session.execute(
            text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        ).scalar_one()
        _require(bool(acquired), "advisory_lock_contended_retry_required")


def _apply_state(session: Session, state: InspectedState) -> ProviderConnection:
    connection = state.connection
    if connection is None:
        connection = ProviderConnection(
            tenant_id=int(state.tenant.id),
            **state.proposed,
        )
        session.add(connection)
    else:
        for key, value in state.proposed.items():
            setattr(connection, key, value)
    session.flush()
    return connection


def reconcile_tenant_provider_connection(
    session: Session,
    request: ReconciliationRequest,
    *,
    apply: bool = False,
    approved_plan_sha256: str | None = None,
) -> dict[str, Any]:
    if apply:
        provider_evidence = _evidence_id(
            request.provider_read_only_evidence_id,
            reason_code="provider_read_only_evidence_id_required",
        )
        window_evidence = _evidence_id(
            request.cutover_window_evidence_id,
            reason_code="cutover_window_evidence_id_required",
        )
        _require(
            provider_evidence != window_evidence,
            "provider_and_window_evidence_must_differ",
        )
        approved = _sha256(
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
        writes_planned = state.action != "noop"
        connection = _apply_state(session, state) if writes_planned else state.connection
        _require(connection is not None, "provider_connection_postcondition_missing")
        _require(
            int(connection.tenant_id) == int(state.tenant.id),
            "provider_connection_postcondition_tenant_mismatch",
        )
        _require(
            _connection_scope(connection) == (PROVIDER, CHANNEL, ENVIRONMENT),
            "provider_connection_postcondition_scope_mismatch",
        )
        _require(
            _clean(connection.external_account_id) == request.external_account_id,
            "provider_connection_postcondition_account_mismatch",
        )
        _require(
            _clean(connection.credentials_ref) == request.credentials_ref,
            "provider_connection_postcondition_credentials_mismatch",
        )
        _require(
            _clean(connection.status).lower()
            in ({PENDING_VERIFICATION_STATUS} | READY_STATUSES),
            "provider_connection_postcondition_invalid_status",
        )
        post_state, _, _ = build_reconciliation_plan(session, request)
        _require(
            post_state.action == "noop",
            "provider_connection_postcondition_drift",
        )
        status = "applied" if writes_planned else "already_reconciled"
        writes_performed = writes_planned
    else:
        _, plan, plan_sha256 = build_reconciliation_plan(session, request)
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
            "provider_read_only_evidence_sha256": (
                _sha256_text(request.provider_read_only_evidence_id)
                if request.provider_read_only_evidence_id
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
    dry_run: bool,
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
    parser.add_argument(
        "--database-environment-variable",
        default=DEFAULT_DATABASE_ENVIRONMENT_VARIABLE,
        help="Environment variable containing the PostgreSQL TLS URL.",
    )
    parser.add_argument(
        "--external-account-environment-variable",
        required=True,
        help="Environment variable containing the Twilio AC... account SID.",
    )
    parser.add_argument(
        "--credentials-environment-variable",
        required=True,
        help=(
            "Name of the runtime secret environment variable to store as an "
            "opaque credentials_ref. Its value is never read."
        ),
    )
    parser.add_argument("--provider-read-only-evidence-id")
    parser.add_argument("--cutover-window-evidence-id")
    parser.add_argument("--approved-plan-sha256")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the approved plan atomically; default is read-only.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    engine = None
    connection = None
    transaction = None
    session = None
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
        database_fingerprint, database_summary = _database_identity(database_url)
        request = build_request(
            tenant_slug=args.tenant_slug,
            database_identity_sha256=database_fingerprint,
            external_account_environment_variable=(
                args.external_account_environment_variable
            ),
            credentials_environment_variable=(
                args.credentials_environment_variable
            ),
            provider_read_only_evidence_id=args.provider_read_only_evidence_id,
            cutover_window_evidence_id=args.cutover_window_evidence_id,
            environ=os.environ,
        )
        engine = create_engine(
            _psycopg_engine_url(database_url),
            poolclass=NullPool,
            hide_parameters=True,
            connect_args={
                "application_name": "chatboc_provider_connection_reconciler",
                "connect_timeout": 10,
            },
        )
        isolation = "SERIALIZABLE" if args.apply else "REPEATABLE READ"
        connection = engine.connect().execution_options(isolation_level=isolation)
        transaction = connection.begin()
        if not args.apply:
            connection.execute(text("SET TRANSACTION READ ONLY"))
        connection.execute(text("SET LOCAL statement_timeout = '30s'"))
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        session = Session(bind=connection, autoflush=False, expire_on_commit=False)
        result = reconcile_tenant_provider_connection(
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
    except ProviderConnectionReconciliationError as exc:
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
                    "provider_connection_reconciliation_unexpected_error",
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
