from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import audit_vercel_production_predeploy as guard


ROOT = Path(__file__).resolve().parents[1]
REVISION = "a" * 40
PROJECT_ID = "nameless-rain-94060889"
BRANCH_ID = "br-production-certified"
SECRET_SENTINEL = "never-print-this-production-secret"
DIRECT_HOST = "ep-certified.us-east-2.aws.neon.tech"
POOLED_HOST = "ep-certified-pooler.us-east-2.aws.neon.tech"
MIGRATION_HEAD = "20260829_global_writer_authority_v1"
MIGRATION_FINGERPRINT = "f" * 64


def _write_project_link(root: Path, *, project_name: str = "chatboc-backend") -> None:
    path = root / ".vercel" / "project.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "projectId": guard.EXPECTED_VERCEL_PROJECT_ID,
                "orgId": guard.EXPECTED_VERCEL_ORG_ID,
                "projectName": project_name,
            }
        ),
        encoding="utf-8",
    )


def _environment() -> dict[str, str]:
    pooled = (
        "postgresql://chatboc:"
        f"{SECRET_SENTINEL}@{POOLED_HOST}/chatboc?sslmode=require"
    )
    direct = (
        "postgresql://chatboc:"
        f"{SECRET_SENTINEL}@{DIRECT_HOST}/chatboc?sslmode=require"
    )
    return {
        "DATABASE_URL": pooled,
        "SQLALCHEMY_DATABASE_URI": pooled,
        "ALEMBIC_DB_URL": direct,
        "MIGRATIONS_DATABASE_URL": direct,
        "EXPECTED_NEON_PROJECT_ID": PROJECT_ID,
        "EXPECTED_NEON_BRANCH_ID": BRANCH_ID,
        "CHATBOC_DEPLOYMENT_REVISION": REVISION,
        "SECRET_KEY": "s" * 32,
        "CRON_SECRET": "c" * 32,
        "TENANT_CLAIM_RECEIPT_SECRET_V1": "r" * 32,
        "RATELIMIT_STORAGE_URI": "rediss://default:redis-secret@redis.invalid:6380/0",
        "SOCKETIO_MESSAGE_QUEUE_URL": (
            "rediss://default:socket-secret@redis.invalid:6380/1"
        ),
        "CUTOVER_WRITER_FENCE_ENABLED": "true",
        "VERCEL_OUTBOX_CRON_ENABLED": "false",
        "VERCEL_MAINTENANCE_CRONS_ENABLED": "false",
        "VERCEL_WEEKLY_ANALYTICS_CRON_ENABLED": "false",
    }


def _write_evidence(
    root: Path,
    *,
    project_id: str = PROJECT_ID,
    branch_id: str = BRANCH_ID,
    database_name: str = "chatboc",
    migration_head: str = MIGRATION_HEAD,
    current_revision: str | None = None,
    ready: bool = True,
    source_revision: str = REVISION,
    source_clean: bool = True,
    migration_fingerprint: str = MIGRATION_FINGERPRINT,
    include_source: bool = True,
    captured_at: datetime | None = None,
    include_captured_at: bool = True,
) -> tuple[Path, str]:
    payload = {
        "contract_version": guard.NEON_PREFLIGHT_CONTRACT,
        "status": "ready" if ready else "blocked",
        "ready": ready,
        "target": {
            "provider": "neon",
            "connection_mode": "direct",
            "tls_required": True,
            "host_fingerprint_sha256": hashlib.sha256(
                DIRECT_HOST.encode("utf-8")
            ).hexdigest(),
        },
        "migration": {
            "current_revisions": [current_revision or migration_head],
            "expected_heads": [migration_head],
            "pending_revisions": [],
            "at_head": True,
        },
        "database": {
            "transaction_read_only": True,
            "neon_identity": {
                "project_id": project_id,
                "branch_id": branch_id,
            },
            "database_name_fingerprint_sha256": hashlib.sha256(
                database_name.encode("utf-8")
            ).hexdigest(),
            "critical_schema": {"ready": True},
            "content_parity_certified": False,
        },
    }
    if include_captured_at:
        payload["captured_at"] = (
            captured_at or datetime.now(timezone.utc)
        ).isoformat().replace("+00:00", "Z")
    if include_source:
        payload["source"] = {
            "source_revision": source_revision,
            "worktree_clean": source_clean,
            "migration_versions_fingerprint_sha256": migration_fingerprint,
        }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path = root / "approved-neon-identity.json"
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def _run(
    root: Path,
    *,
    environ: dict[str, str] | None = None,
    evidence_project_id: str = PROJECT_ID,
    evidence_branch_id: str = BRANCH_ID,
    approved_digest: str | None = None,
    source_dirty: bool = False,
):
    _write_project_link(root)
    evidence_path, digest = _write_evidence(
        root,
        project_id=evidence_project_id,
        branch_id=evidence_branch_id,
    )
    return guard.audit_predeploy(
        target="production",
        project_root=root,
        environ=environ or _environment(),
        expected_revision=REVISION,
        source_revision=REVISION,
        source_dirty=source_dirty,
        local_migration_heads=[MIGRATION_HEAD],
        local_migration_fingerprint=MIGRATION_FINGERPRINT,
        identity_evidence_path=evidence_path,
        approved_evidence_digest=approved_digest or digest,
    )


def test_ready_report_is_redacted_and_does_not_authorize_cutover(tmp_path: Path) -> None:
    report, exit_code = _run(tmp_path)

    assert exit_code == 0
    assert report["status"] == "ready"
    assert report["ready"] is True
    assert report["database"]["identity_evidence_verified"] is True
    assert report["source"]["revision_matches"] is True
    assert report["safety"] == {
        "network_requests": False,
        "database_connections": False,
        "writes_attempted": False,
        "cutover_authorized": False,
        "writer_ownership_acquired": False,
    }
    assert SECRET_SENTINEL not in json.dumps(report, sort_keys=True)


@pytest.mark.parametrize(
    "name",
    [
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
    ],
)
def test_blank_critical_environment_value_blocks(name: str, tmp_path: Path) -> None:
    environ = _environment()
    environ[name] = "   "

    report, exit_code = _run(tmp_path, environ=environ)

    assert exit_code == 2
    assert f"environment_value_missing:{name}" in report["reason_codes"]
    assert SECRET_SENTINEL not in json.dumps(report, sort_keys=True)


@pytest.mark.parametrize(
    ("name", "unsafe_value"),
    [
        ("CUTOVER_WRITER_FENCE_ENABLED", "false"),
        ("VERCEL_OUTBOX_CRON_ENABLED", "true"),
        ("VERCEL_MAINTENANCE_CRONS_ENABLED", "true"),
        ("VERCEL_WEEKLY_ANALYTICS_CRON_ENABLED", "true"),
    ],
)
def test_fenced_candidate_rejects_unsafe_writer_flags(
    name: str,
    unsafe_value: str,
    tmp_path: Path,
) -> None:
    environ = _environment()
    environ[name] = unsafe_value

    report, exit_code = _run(tmp_path, environ=environ)

    assert exit_code == 2
    assert f"environment_flag_unsafe:{name}" in report["reason_codes"]


def test_runtime_and_migration_must_identify_same_neon_database(tmp_path: Path) -> None:
    environ = _environment()
    environ["MIGRATIONS_DATABASE_URL"] = environ["MIGRATIONS_DATABASE_URL"].replace(
        "/chatboc?", "/wrong_database?"
    )
    environ["ALEMBIC_DB_URL"] = environ["MIGRATIONS_DATABASE_URL"]

    report, exit_code = _run(tmp_path, environ=environ)

    assert exit_code == 2
    assert "runtime_migration_database_mismatch" in report["reason_codes"]


def test_identity_evidence_must_match_expected_branch(tmp_path: Path) -> None:
    report, exit_code = _run(
        tmp_path,
        evidence_branch_id="br-other-production-branch",
    )

    assert exit_code == 2
    assert "identity_evidence_branch_mismatch" in report["reason_codes"]


def test_identity_evidence_must_match_logical_database(tmp_path: Path) -> None:
    _write_project_link(tmp_path)
    evidence_path, digest = _write_evidence(
        tmp_path,
        database_name="another_database",
    )

    report, exit_code = guard.audit_predeploy(
        target="production",
        project_root=tmp_path,
        environ=_environment(),
        expected_revision=REVISION,
        source_revision=REVISION,
        source_dirty=False,
        local_migration_heads=[MIGRATION_HEAD],
        local_migration_fingerprint=MIGRATION_FINGERPRINT,
        identity_evidence_path=evidence_path,
        approved_evidence_digest=digest,
    )

    assert exit_code == 2
    assert "identity_evidence_database_mismatch" in report["reason_codes"]


def test_identity_evidence_requires_operator_approved_digest(tmp_path: Path) -> None:
    report, exit_code = _run(tmp_path, approved_digest="0" * 64)

    assert exit_code == 2
    assert "identity_evidence_digest_mismatch" in report["reason_codes"]


@pytest.mark.parametrize(
    ("captured_at", "include_captured_at", "reason"),
    [
        (
            datetime.now(timezone.utc)
            - timedelta(seconds=guard.MAX_IDENTITY_EVIDENCE_AGE_SECONDS + 1),
            True,
            "identity_evidence_stale",
        ),
        (
            datetime.now(timezone.utc)
            + timedelta(seconds=guard.MAX_IDENTITY_EVIDENCE_FUTURE_SKEW_SECONDS + 1),
            True,
            "identity_evidence_captured_in_future",
        ),
        (None, False, "identity_evidence_captured_at_invalid"),
    ],
)
def test_identity_evidence_requires_a_fresh_utc_capture(
    tmp_path: Path,
    captured_at: datetime | None,
    include_captured_at: bool,
    reason: str,
) -> None:
    _write_project_link(tmp_path)
    evidence_path, digest = _write_evidence(
        tmp_path,
        captured_at=captured_at,
        include_captured_at=include_captured_at,
    )

    report, exit_code = guard.audit_predeploy(
        target="production",
        project_root=tmp_path,
        environ=_environment(),
        expected_revision=REVISION,
        source_revision=REVISION,
        source_dirty=False,
        local_migration_heads=[MIGRATION_HEAD],
        local_migration_fingerprint=MIGRATION_FINGERPRINT,
        identity_evidence_path=evidence_path,
        approved_evidence_digest=digest,
    )

    assert exit_code == 2
    assert reason in report["reason_codes"]


def test_old_ready_evidence_cannot_certify_new_checkout_migration_graph(
    tmp_path: Path,
) -> None:
    _write_project_link(tmp_path)
    evidence_path, digest = _write_evidence(
        tmp_path,
        migration_head="20260829_inbound_fifo_v2",
    )

    report, exit_code = guard.audit_predeploy(
        target="production",
        project_root=tmp_path,
        environ=_environment(),
        expected_revision=REVISION,
        source_revision=REVISION,
        source_dirty=False,
        local_migration_heads=[MIGRATION_HEAD],
        local_migration_fingerprint=MIGRATION_FINGERPRINT,
        identity_evidence_path=evidence_path,
        approved_evidence_digest=digest,
    )

    assert exit_code == 2
    assert "identity_evidence_migration_graph_mismatch" in report["reason_codes"]
    assert report["database"]["identity_evidence_verified"] is False


def test_evidence_current_revision_must_equal_checkout_head(tmp_path: Path) -> None:
    _write_project_link(tmp_path)
    evidence_path, digest = _write_evidence(
        tmp_path,
        migration_head=MIGRATION_HEAD,
        current_revision="20260829_inbound_fifo_v2",
    )

    report, exit_code = guard.audit_predeploy(
        target="production",
        project_root=tmp_path,
        environ=_environment(),
        expected_revision=REVISION,
        source_revision=REVISION,
        source_dirty=False,
        local_migration_heads=[MIGRATION_HEAD],
        local_migration_fingerprint=MIGRATION_FINGERPRINT,
        identity_evidence_path=evidence_path,
        approved_evidence_digest=digest,
    )

    assert exit_code == 2
    assert "identity_evidence_migration_revision_mismatch" in report["reason_codes"]


@pytest.mark.parametrize(
    ("evidence_kwargs", "reason"),
    [
        (
            {"migration_fingerprint": "e" * 64},
            "identity_evidence_migration_fingerprint_mismatch",
        ),
        (
            {"source_revision": "b" * 40},
            "identity_evidence_source_revision_mismatch",
        ),
        (
            {"source_clean": False},
            "identity_evidence_source_dirty",
        ),
        (
            {"include_source": False},
            "identity_evidence_source_contract_invalid",
        ),
    ],
)
def test_identity_evidence_is_bound_to_exact_clean_checkout_content(
    tmp_path: Path,
    evidence_kwargs: dict[str, object],
    reason: str,
) -> None:
    _write_project_link(tmp_path)
    evidence_path, digest = _write_evidence(tmp_path, **evidence_kwargs)

    report, exit_code = guard.audit_predeploy(
        target="production",
        project_root=tmp_path,
        environ=_environment(),
        expected_revision=REVISION,
        source_revision=REVISION,
        source_dirty=False,
        local_migration_heads=[MIGRATION_HEAD],
        local_migration_fingerprint=MIGRATION_FINGERPRINT,
        identity_evidence_path=evidence_path,
        approved_evidence_digest=digest,
    )

    assert exit_code == 2
    assert reason in report["reason_codes"]
    assert report["database"]["identity_evidence_verified"] is False


def test_local_migration_graph_has_one_certifiable_head() -> None:
    assert guard._local_migration_heads(ROOT) == [MIGRATION_HEAD]


@pytest.mark.parametrize(
    ("name", "unsafe_url"),
    [
        (
            "RATELIMIT_STORAGE_URI",
            "redis://default:secret@redis.invalid:6379/0",
        ),
        (
            "SOCKETIO_MESSAGE_QUEUE_URL",
            "rediss://redis.invalid:6380/1",
        ),
    ],
)
def test_production_redis_requires_tls_and_credential(
    name: str,
    unsafe_url: str,
    tmp_path: Path,
) -> None:
    environ = _environment()
    environ[name] = unsafe_url

    report, exit_code = _run(tmp_path, environ=environ)

    assert exit_code == 2
    assert f"environment_redis_invalid:{name}" in report["reason_codes"]


def test_wrong_linked_vercel_project_blocks(tmp_path: Path) -> None:
    _write_project_link(tmp_path, project_name="another-project")
    evidence_path, digest = _write_evidence(tmp_path)

    report, exit_code = guard.audit_predeploy(
        target="production",
        project_root=tmp_path,
        environ=_environment(),
        expected_revision=REVISION,
        source_revision=REVISION,
        source_dirty=False,
        local_migration_heads=[MIGRATION_HEAD],
        local_migration_fingerprint=MIGRATION_FINGERPRINT,
        identity_evidence_path=evidence_path,
        approved_evidence_digest=digest,
    )

    assert exit_code == 2
    assert "vercel_project_name_mismatch" in report["reason_codes"]


def test_dirty_or_different_source_revision_blocks(tmp_path: Path) -> None:
    report, exit_code = _run(tmp_path, source_dirty=True)

    assert exit_code == 2
    assert "source_worktree_dirty" in report["reason_codes"]

    _write_project_link(tmp_path)
    evidence_path, digest = _write_evidence(tmp_path)
    report, exit_code = guard.audit_predeploy(
        target="production",
        project_root=tmp_path,
        environ=_environment(),
        expected_revision=REVISION,
        source_revision="b" * 40,
        source_dirty=False,
        local_migration_heads=[MIGRATION_HEAD],
        local_migration_fingerprint=MIGRATION_FINGERPRINT,
        identity_evidence_path=evidence_path,
        approved_evidence_digest=digest,
    )
    assert exit_code == 2
    assert "source_revision_mismatch" in report["reason_codes"]


def test_cli_output_never_serializes_injected_values(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _write_project_link(tmp_path)
    evidence_path, digest = _write_evidence(tmp_path)
    for name, value in _environment().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        guard,
        "checkout_identity",
        lambda _root: {
            "source_revision": REVISION,
            "worktree_clean": True,
            "migration_versions_fingerprint_sha256": MIGRATION_FINGERPRINT,
        },
    )
    monkeypatch.setattr(
        guard,
        "_local_migration_heads",
        lambda _root: [MIGRATION_HEAD],
    )

    exit_code = guard.main(
        [
            "production",
            "--project-root",
            str(tmp_path),
            "--expected-revision",
            REVISION,
            "--identity-evidence",
            str(evidence_path),
            "--approved-evidence-sha256",
            digest,
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert json.loads(output)["ready"] is True
    assert SECRET_SENTINEL not in output
    assert "redis-secret" not in output
    assert "socket-secret" not in output
