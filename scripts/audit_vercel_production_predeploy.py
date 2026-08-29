"""Fail-closed, offline gate for a Chatboc backend Production redeploy.

The guard consumes environment variables already injected into the current
process.  It never calls Vercel, Neon or any other network service and never
prints environment values.  Database identity is checked against a redacted
``preflight_neon_cutover`` artifact whose SHA-256 digest must be supplied by
the release operator.

This command certifies only that a *fenced candidate* is reproducible.  It does
not authorize a production cutover, writer ownership or cron ownership.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

try:
    from scripts.release_checkout_identity import CheckoutIdentityFailure, checkout_identity
except ModuleNotFoundError:  # direct script execution
    from release_checkout_identity import CheckoutIdentityFailure, checkout_identity


CONTRACT_VERSION = "chatboc.vercel_production_predeploy_guard.v1"
NEON_PREFLIGHT_CONTRACT = "chatboc.neon_cutover_preflight.v1"
EXPECTED_VERCEL_PROJECT_NAME = "chatboc-backend"
EXPECTED_VERCEL_PROJECT_ID = "prj_mHwkeg5AIgXHPnt7QLRlXur7KBNF"
EXPECTED_VERCEL_ORG_ID = "team_BV1xuY6BnEzGanfok8GAyjZv"
APPROVED_EVIDENCE_DIGEST_ENV = "APPROVED_NEON_IDENTITY_EVIDENCE_SHA256"
MAX_IDENTITY_EVIDENCE_AGE_SECONDS = 15 * 60
MAX_IDENTITY_EVIDENCE_FUTURE_SKEW_SECONDS = 60

REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
NEON_PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
NEON_BRANCH_ID_PATTERN = re.compile(r"^br-[a-z0-9-]{3,63}$")

REQUIRED_NONEMPTY_ENVIRONMENT = (
    "DATABASE_URL",
    "SQLALCHEMY_DATABASE_URI",
    "ALEMBIC_DB_URL",
    "MIGRATIONS_DATABASE_URL",
    "EXPECTED_NEON_PROJECT_ID",
    "EXPECTED_NEON_BRANCH_ID",
    "CHATBOC_DEPLOYMENT_REVISION",
    "SECRET_KEY",
    "CRON_SECRET",
    "TENANT_CLAIM_RECEIPT_SECRET_V1",
    "RATELIMIT_STORAGE_URI",
    "SOCKETIO_MESSAGE_QUEUE_URL",
)

FENCED_CANDIDATE_FLAGS = {
    "CUTOVER_WRITER_FENCE_ENABLED": True,
    "VERCEL_OUTBOX_CRON_ENABLED": False,
    "VERCEL_MAINTENANCE_CRONS_ENABLED": False,
    "VERCEL_WEEKLY_ANALYTICS_CRON_ENABLED": False,
}

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True)
class DatabaseEndpoint:
    """Credential-bearing endpoint metadata kept in memory and never emitted."""

    host: str
    base_host: str
    database: str
    username: str
    password: str
    pooled: bool


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _clean(value: object) -> str:
    return str(value or "").strip()


def _environment_value(environ: Mapping[str, str], name: str) -> str:
    return _clean(environ.get(name))


def _parse_boolean(value: object) -> bool | None:
    normalized = _clean(value).lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return None


def _base_neon_host(host: str) -> str:
    labels = host.split(".")
    if labels and labels[0].endswith("-pooler"):
        labels[0] = labels[0][: -len("-pooler")]
    return ".".join(labels)


def _parse_database_endpoint(raw_value: str, *, pooled: bool) -> DatabaseEndpoint:
    """Validate a Neon TLS DSN without returning it or serializing failures."""

    try:
        parsed = urlsplit(raw_value)
        port = parsed.port
    except Exception as exc:
        raise ValueError("database_url_invalid") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"postgres", "postgresql", "postgresql+psycopg"}:
        raise ValueError("database_backend_not_postgresql")
    host = _clean(parsed.hostname).lower().rstrip(".")
    if not host.endswith(".neon.tech"):
        raise ValueError("database_provider_not_neon")
    is_pooled = "-pooler." in host
    if is_pooled is not pooled:
        raise ValueError(
            "database_connection_not_pooled"
            if pooled
            else "database_connection_not_direct"
        )
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("database_port_invalid")

    query = parse_qs(parsed.query, keep_blank_values=True)
    sslmode = _clean((query.get("sslmode") or [""])[-1]).lower()
    if sslmode not in {"require", "verify-ca", "verify-full"}:
        raise ValueError("database_tls_not_required")

    database = unquote(parsed.path.lstrip("/")).strip()
    username = unquote(parsed.username or "").strip()
    password = unquote(parsed.password or "")
    if not database:
        raise ValueError("database_name_missing")
    if not username:
        raise ValueError("database_username_missing")
    if not password:
        raise ValueError("database_password_missing")

    return DatabaseEndpoint(
        host=host,
        base_host=_base_neon_host(host),
        database=database,
        username=username,
        password=password,
        pooled=pooled,
    )


def _same_database(left: DatabaseEndpoint, right: DatabaseEndpoint) -> bool:
    return (
        left.base_host == right.base_host
        and left.database == right.database
        and left.username == right.username
        and left.password == right.password
    )


def _parse_redis_endpoint(raw_value: str) -> None:
    try:
        parsed = urlsplit(raw_value)
        port = parsed.port
    except Exception as exc:
        raise ValueError("redis_url_invalid") from exc
    if parsed.scheme.lower() != "rediss":
        raise ValueError("redis_tls_required")
    if not parsed.hostname:
        raise ValueError("redis_host_missing")
    if not parsed.password:
        raise ValueError("redis_credential_missing")
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("redis_port_invalid")


def _load_project_link(project_root: Path) -> dict[str, Any]:
    path = project_root / ".vercel" / "project.json"
    if not path.is_file() or path.is_symlink():
        raise ValueError("vercel_project_link_missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("vercel_project_link_invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("vercel_project_link_invalid")
    if payload.get("projectName") != EXPECTED_VERCEL_PROJECT_NAME:
        raise ValueError("vercel_project_name_mismatch")
    if payload.get("projectId") != EXPECTED_VERCEL_PROJECT_ID:
        raise ValueError("vercel_project_id_mismatch")
    if payload.get("orgId") != EXPECTED_VERCEL_ORG_ID:
        raise ValueError("vercel_org_id_mismatch")
    return payload


def _load_identity_evidence(
    evidence_path: Path,
    *,
    approved_digest: str,
    direct_endpoint: DatabaseEndpoint,
    expected_project_id: str,
    expected_branch_id: str,
    local_migration_heads: Sequence[str],
    expected_source_revision: str,
    local_migration_fingerprint: str,
    now: datetime | None = None,
) -> None:
    if not SHA256_PATTERN.fullmatch(approved_digest):
        raise ValueError("approved_identity_evidence_digest_invalid")
    if not evidence_path.is_file() or evidence_path.is_symlink():
        raise ValueError("identity_evidence_missing")
    try:
        size = evidence_path.stat().st_size
    except OSError as exc:
        raise ValueError("identity_evidence_unreadable") from exc
    if size <= 0 or size > 1_048_576:
        raise ValueError("identity_evidence_size_invalid")
    try:
        raw = evidence_path.read_bytes()
    except OSError as exc:
        raise ValueError("identity_evidence_unreadable") from exc
    if _sha256_bytes(raw) != approved_digest:
        raise ValueError("identity_evidence_digest_mismatch")
    try:
        payload = json.loads(raw)
    except Exception as exc:
        raise ValueError("identity_evidence_invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("identity_evidence_invalid")

    target = payload.get("target")
    database = payload.get("database")
    migration = payload.get("migration")
    if not isinstance(target, dict) or not isinstance(database, dict):
        raise ValueError("identity_evidence_contract_invalid")
    if not isinstance(migration, dict):
        raise ValueError("identity_evidence_contract_invalid")
    if payload.get("contract_version") != NEON_PREFLIGHT_CONTRACT:
        raise ValueError("identity_evidence_contract_invalid")
    captured_at_raw = payload.get("captured_at")
    if not isinstance(captured_at_raw, str) or not captured_at_raw.strip():
        raise ValueError("identity_evidence_captured_at_invalid")
    try:
        captured_at = datetime.fromisoformat(
            captured_at_raw.strip().replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError("identity_evidence_captured_at_invalid") from exc
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("identity_evidence_captured_at_invalid")
    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None or reference_time.utcoffset() is None:
        raise ValueError("identity_evidence_reference_time_invalid")
    evidence_age_seconds = (
        reference_time.astimezone(timezone.utc)
        - captured_at.astimezone(timezone.utc)
    ).total_seconds()
    if evidence_age_seconds < -MAX_IDENTITY_EVIDENCE_FUTURE_SKEW_SECONDS:
        raise ValueError("identity_evidence_captured_in_future")
    if evidence_age_seconds > MAX_IDENTITY_EVIDENCE_AGE_SECONDS:
        raise ValueError("identity_evidence_stale")
    if payload.get("status") != "ready" or payload.get("ready") is not True:
        raise ValueError("identity_evidence_not_ready")
    source = payload.get("source")
    if not isinstance(source, dict):
        raise ValueError("identity_evidence_source_contract_invalid")
    evidence_revision = _clean(source.get("source_revision")).lower()
    evidence_fingerprint = _clean(
        source.get("migration_versions_fingerprint_sha256")
    ).lower()
    if not REVISION_PATTERN.fullmatch(evidence_revision):
        raise ValueError("identity_evidence_source_contract_invalid")
    if source.get("worktree_clean") is not True:
        raise ValueError("identity_evidence_source_dirty")
    if not SHA256_PATTERN.fullmatch(evidence_fingerprint):
        raise ValueError("identity_evidence_source_contract_invalid")
    if evidence_revision != expected_source_revision:
        raise ValueError("identity_evidence_source_revision_mismatch")
    if evidence_fingerprint != local_migration_fingerprint:
        raise ValueError("identity_evidence_migration_fingerprint_mismatch")
    if (
        target.get("provider") != "neon"
        or target.get("connection_mode") != "direct"
        or target.get("tls_required") is not True
    ):
        raise ValueError("identity_evidence_target_invalid")
    expected_host_fingerprint = _sha256_bytes(direct_endpoint.host.encode("utf-8"))
    if target.get("host_fingerprint_sha256") != expected_host_fingerprint:
        raise ValueError("identity_evidence_host_mismatch")
    if database.get("transaction_read_only") is not True:
        raise ValueError("identity_evidence_not_read_only")
    neon_identity = database.get("neon_identity")
    if not isinstance(neon_identity, dict):
        raise ValueError("identity_evidence_contract_invalid")
    if _clean(neon_identity.get("project_id")).lower() != expected_project_id:
        raise ValueError("identity_evidence_project_mismatch")
    if _clean(neon_identity.get("branch_id")).lower() != expected_branch_id:
        raise ValueError("identity_evidence_branch_mismatch")
    expected_database_fingerprint = _sha256_bytes(
        direct_endpoint.database.encode("utf-8")
    )
    if (
        database.get("database_name_fingerprint_sha256")
        != expected_database_fingerprint
    ):
        raise ValueError("identity_evidence_database_mismatch")
    if isinstance(local_migration_heads, (str, bytes)):
        raise ValueError("local_migration_heads_ambiguous")
    normalized_local_heads = sorted(
        _clean(revision) for revision in local_migration_heads
    )
    if len(normalized_local_heads) != 1:
        raise ValueError("local_migration_heads_ambiguous")
    raw_expected_heads = migration.get("expected_heads")
    raw_current_revisions = migration.get("current_revisions")
    if not isinstance(raw_expected_heads, list) or not all(
        isinstance(revision, str) and _clean(revision)
        for revision in raw_expected_heads
    ):
        raise ValueError("identity_evidence_contract_invalid")
    if not isinstance(raw_current_revisions, list) or not all(
        isinstance(revision, str) and _clean(revision)
        for revision in raw_current_revisions
    ):
        raise ValueError("identity_evidence_contract_invalid")
    evidence_expected_heads = sorted(_clean(value) for value in raw_expected_heads)
    evidence_current_revisions = sorted(
        _clean(value) for value in raw_current_revisions
    )
    if evidence_expected_heads != normalized_local_heads:
        raise ValueError("identity_evidence_migration_graph_mismatch")
    if evidence_current_revisions != normalized_local_heads:
        raise ValueError("identity_evidence_migration_revision_mismatch")
    if migration.get("at_head") is not True or migration.get("pending_revisions") != []:
        raise ValueError("identity_evidence_migration_not_at_head")
    critical_schema = database.get("critical_schema")
    if not isinstance(critical_schema, dict) or critical_schema.get("ready") is not True:
        raise ValueError("identity_evidence_schema_not_ready")


def _local_migration_heads(project_root: Path) -> list[str]:
    """Read the checkout's Alembic graph without loading app config or a DB."""

    try:
        from alembic.config import Config as AlembicConfig
        from alembic.script import ScriptDirectory

        config = AlembicConfig(str(project_root / "alembic.ini"))
        config.set_main_option("script_location", str(project_root / "migrations"))
        heads = sorted(ScriptDirectory.from_config(config).get_heads())
    except Exception as exc:
        raise ValueError("local_migration_graph_unavailable") from exc
    if len(heads) != 1 or not _clean(heads[0]):
        raise ValueError("local_migration_heads_ambiguous")
    return heads


def audit_predeploy(
    *,
    target: str,
    project_root: Path,
    environ: Mapping[str, str],
    expected_revision: str,
    source_revision: str,
    source_dirty: bool,
    local_migration_heads: Sequence[str],
    local_migration_fingerprint: str,
    identity_evidence_path: Path,
    approved_evidence_digest: str,
) -> tuple[dict[str, Any], int]:
    """Return a redacted report; no function in this path performs network I/O."""

    reasons: list[str] = []

    if _clean(target).lower() != "production":
        reasons.append("target_not_production")

    expected_revision = _clean(expected_revision).lower()
    source_revision = _clean(source_revision).lower()
    if not REVISION_PATTERN.fullmatch(expected_revision):
        reasons.append("expected_revision_invalid")
    if not REVISION_PATTERN.fullmatch(source_revision):
        reasons.append("source_revision_invalid")
    elif expected_revision != source_revision:
        reasons.append("source_revision_mismatch")
    if source_dirty:
        reasons.append("source_worktree_dirty")

    project_link_verified = False
    try:
        _load_project_link(project_root)
        project_link_verified = True
    except ValueError as exc:
        reasons.append(str(exc))

    missing = [
        name
        for name in REQUIRED_NONEMPTY_ENVIRONMENT
        if not _environment_value(environ, name)
    ]
    reasons.extend(f"environment_value_missing:{name}" for name in missing)

    deployment_revision = _environment_value(
        environ, "CHATBOC_DEPLOYMENT_REVISION"
    ).lower()
    if deployment_revision and not REVISION_PATTERN.fullmatch(deployment_revision):
        reasons.append("deployment_revision_invalid")
    elif deployment_revision and expected_revision and deployment_revision != expected_revision:
        reasons.append("deployment_revision_mismatch")

    secret_key = _environment_value(environ, "SECRET_KEY")
    if secret_key and len(secret_key.encode("utf-8")) < 24:
        reasons.append("secret_key_too_short")
    cron_secret = _environment_value(environ, "CRON_SECRET")
    if cron_secret and len(cron_secret.encode("utf-8")) < 32:
        reasons.append("cron_secret_too_short")
    receipt_secret = _environment_value(
        environ, "TENANT_CLAIM_RECEIPT_SECRET_V1"
    )
    if receipt_secret and len(receipt_secret.encode("utf-8")) < 32:
        reasons.append("tenant_claim_receipt_secret_too_short")

    for name, expected_value in FENCED_CANDIDATE_FLAGS.items():
        actual = _parse_boolean(environ.get(name))
        if actual is None:
            reasons.append(f"environment_flag_invalid:{name}")
        elif actual is not expected_value:
            reasons.append(f"environment_flag_unsafe:{name}")

    expected_project_id = _environment_value(
        environ, "EXPECTED_NEON_PROJECT_ID"
    ).lower()
    expected_branch_id = _environment_value(environ, "EXPECTED_NEON_BRANCH_ID").lower()
    if expected_project_id and not NEON_PROJECT_ID_PATTERN.fullmatch(expected_project_id):
        reasons.append("expected_neon_project_id_invalid")
    if expected_branch_id and not NEON_BRANCH_ID_PATTERN.fullmatch(expected_branch_id):
        reasons.append("expected_neon_branch_id_invalid")

    for redis_name in ("RATELIMIT_STORAGE_URI", "SOCKETIO_MESSAGE_QUEUE_URL"):
        value = _environment_value(environ, redis_name)
        if value:
            try:
                _parse_redis_endpoint(value)
            except ValueError:
                reasons.append(f"environment_redis_invalid:{redis_name}")

    runtime_endpoint: DatabaseEndpoint | None = None
    sqlalchemy_endpoint: DatabaseEndpoint | None = None
    migration_endpoint: DatabaseEndpoint | None = None
    alembic_endpoint: DatabaseEndpoint | None = None
    endpoint_specs = (
        ("DATABASE_URL", True, "runtime"),
        ("SQLALCHEMY_DATABASE_URI", True, "sqlalchemy"),
        ("MIGRATIONS_DATABASE_URL", False, "migration"),
        ("ALEMBIC_DB_URL", False, "alembic"),
    )
    parsed_endpoints: dict[str, DatabaseEndpoint] = {}
    for name, pooled, alias in endpoint_specs:
        value = _environment_value(environ, name)
        if not value:
            continue
        try:
            parsed_endpoints[alias] = _parse_database_endpoint(value, pooled=pooled)
        except ValueError as exc:
            reasons.append(f"environment_database_invalid:{name}:{exc}")
    runtime_endpoint = parsed_endpoints.get("runtime")
    sqlalchemy_endpoint = parsed_endpoints.get("sqlalchemy")
    migration_endpoint = parsed_endpoints.get("migration")
    alembic_endpoint = parsed_endpoints.get("alembic")
    database_aliases_consistent = len(parsed_endpoints) == len(endpoint_specs)
    if runtime_endpoint and sqlalchemy_endpoint and not _same_database(
        runtime_endpoint, sqlalchemy_endpoint
    ):
        reasons.append("runtime_database_alias_mismatch")
        database_aliases_consistent = False
    if migration_endpoint and alembic_endpoint and not _same_database(
        migration_endpoint, alembic_endpoint
    ):
        reasons.append("migration_database_alias_mismatch")
        database_aliases_consistent = False
    if runtime_endpoint and migration_endpoint and not _same_database(
        runtime_endpoint, migration_endpoint
    ):
        reasons.append("runtime_migration_database_mismatch")
        database_aliases_consistent = False
    if any(reason.startswith("environment_database_invalid") for reason in reasons):
        database_aliases_consistent = False

    identity_evidence_verified = False
    if (
        migration_endpoint is not None
        and NEON_PROJECT_ID_PATTERN.fullmatch(expected_project_id)
        and NEON_BRANCH_ID_PATTERN.fullmatch(expected_branch_id)
    ):
        try:
            _load_identity_evidence(
                identity_evidence_path,
                approved_digest=approved_evidence_digest,
                direct_endpoint=migration_endpoint,
                expected_project_id=expected_project_id,
                expected_branch_id=expected_branch_id,
                local_migration_heads=local_migration_heads,
                expected_source_revision=expected_revision,
                local_migration_fingerprint=local_migration_fingerprint,
            )
            identity_evidence_verified = True
        except ValueError as exc:
            reasons.append(str(exc))
    else:
        reasons.append("identity_evidence_prerequisites_missing")

    reasons = sorted(set(reasons))
    ready = not reasons
    report = {
        "contract_version": CONTRACT_VERSION,
        "status": "ready" if ready else "blocked",
        "ready": ready,
        "target": "production",
        "release_mode": "fenced-candidate",
        "reason_codes": reasons,
        "source": {
            "expected_revision": (
                expected_revision
                if REVISION_PATTERN.fullmatch(expected_revision)
                else None
            ),
            "revision_matches": bool(
                REVISION_PATTERN.fullmatch(expected_revision)
                and expected_revision == source_revision == deployment_revision
            ),
            "worktree_clean": not source_dirty,
        },
        "vercel": {
            "project": EXPECTED_VERCEL_PROJECT_NAME,
            "linked_project_verified": project_link_verified,
            "environment_values_nonempty": not missing,
        },
        "database": {
            "provider": "neon",
            "runtime_connection_mode": "pooled",
            "migration_connection_mode": "direct",
            "aliases_consistent": database_aliases_consistent,
            "identity_evidence_verified": identity_evidence_verified,
        },
        "safety": {
            "network_requests": False,
            "database_connections": False,
            "writes_attempted": False,
            "cutover_authorized": False,
            "writer_ownership_acquired": False,
        },
    }
    return report, 0 if ready else 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=("production",))
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--identity-evidence", type=Path, required=True)
    parser.add_argument(
        "--approved-evidence-sha256",
        help=(
            "Approved SHA-256 of the redacted identity evidence. Defaults to "
            f"{APPROVED_EVIDENCE_DIGEST_ENV}."
        ),
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args(argv)

    try:
        source = checkout_identity(args.project_root)
        source_revision = str(source["source_revision"])
        source_dirty = not bool(source["worktree_clean"])
        local_migration_fingerprint = str(
            source["migration_versions_fingerprint_sha256"]
        )
        local_migration_heads = _local_migration_heads(args.project_root)
        approved_digest = _clean(
            args.approved_evidence_sha256
            or os.environ.get(APPROVED_EVIDENCE_DIGEST_ENV)
        ).lower()
        report, exit_code = audit_predeploy(
            target=args.target,
            project_root=args.project_root.resolve(),
            environ=os.environ,
            expected_revision=args.expected_revision,
            source_revision=source_revision,
            source_dirty=source_dirty,
            local_migration_heads=local_migration_heads,
            local_migration_fingerprint=local_migration_fingerprint,
            identity_evidence_path=args.identity_evidence.resolve(),
            approved_evidence_digest=approved_digest,
        )
    except Exception as exc:
        reason = (
            exc.reason_code
            if isinstance(exc, CheckoutIdentityFailure)
            else _clean(exc)
            if isinstance(exc, ValueError)
            else "predeploy_guard_failed"
        )
        report = {
            "contract_version": CONTRACT_VERSION,
            "status": "blocked",
            "ready": False,
            "target": "production",
            "release_mode": "fenced-candidate",
            "reason_codes": [reason or "predeploy_guard_failed"],
            "safety": {
                "network_requests": False,
                "database_connections": False,
                "writes_attempted": False,
                "cutover_authorized": False,
                "writer_ownership_acquired": False,
            },
        }
        exit_code = 2

    print(json.dumps(report, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
