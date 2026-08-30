from __future__ import annotations

from pathlib import Path

import pytest

from scripts.preflight_neon_cutover import (
    PreflightFailure,
    _failure_payload,
    _load_migration_directory,
    _migration_state,
    _wal_position,
    _validate_environment_variable_name,
    _validate_neon_identity_value,
    _validate_neon_direct_url,
    run_preflight,
)


ROOT = Path(__file__).resolve().parents[1]
DIRECT_NEON_URL = (
    "postgresql://cutover_user:cutover_password@"
    "ep-safe-branch.us-east-2.aws.neon.tech/neondb?sslmode=require"
)


def test_accepts_only_a_direct_tls_neon_postgres_target():
    parsed = _validate_neon_direct_url(DIRECT_NEON_URL)

    assert parsed.get_backend_name() == "postgresql"
    assert parsed.host == "ep-safe-branch.us-east-2.aws.neon.tech"


@pytest.mark.parametrize(
    ("url", "reason_code"),
    [
        (
            "postgresql://user:secret@ep-safe-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require",
            "database_connection_not_direct",
        ),
        (
            "postgresql://user:secret@postgres.example.com/neondb?sslmode=require",
            "database_provider_not_neon",
        ),
        (
            "postgresql://user:secret@ep-safe.us-east-2.aws.neon.tech/neondb",
            "database_tls_not_required",
        ),
        ("sqlite:///unsafe.db", "database_backend_not_postgresql"),
    ],
)
def test_rejects_unsafe_database_targets_without_echoing_the_url(url, reason_code):
    with pytest.raises(PreflightFailure) as captured:
        _validate_neon_direct_url(url)

    assert captured.value.reason_code == reason_code
    assert "secret" not in str(captured.value)
    assert url not in str(captured.value)


def test_environment_variable_name_is_strict():
    assert _validate_environment_variable_name("MIGRATIONS_DATABASE_URL") == (
        "MIGRATIONS_DATABASE_URL"
    )

    with pytest.raises(PreflightFailure) as captured:
        _validate_environment_variable_name("MIGRATIONS_DATABASE_URL;Get-ChildItem")

    assert captured.value.reason_code == "database_environment_variable_invalid"


def test_expected_neon_identity_is_explicit_and_strict():
    assert _validate_neon_identity_value(
        "nameless-rain-94060889",
        kind="project",
    ) == "nameless-rain-94060889"
    assert _validate_neon_identity_value(
        "br-falling-wind-actm4mcu",
        kind="branch",
    ) == "br-falling-wind-actm4mcu"

    with pytest.raises(PreflightFailure) as captured:
        _validate_neon_identity_value("main", kind="branch")

    assert captured.value.reason_code == "expected_neon_branch_id_invalid"


def test_current_documented_revision_produces_the_complete_upgrade_plan():
    script = _load_migration_directory(ROOT)

    state = _migration_state(
        script,
        ["20260820_survey_content_jurisdiction_v1"],
    )

    assert state == {
        "current_revisions": ["20260820_survey_content_jurisdiction_v1"],
        "expected_heads": ["20260830_geo_sync_v1"],
        "pending_revisions": [
            "20260825_demo_survey_participation_v1",
            "20260825_legacy_municipio_ticket_scope_repair_v1",
            "20260825_chat_idempotency_v1",
            "20260829_inbound_fifo_v2",
            "20260829_global_writer_authority_v1",
            "20260830_territorial_geocoding_v1",
            "20260830_geo_review_v1",
            "20260830_geo_sync_v1",
        ],
        "at_head": False,
    }


def test_current_head_has_no_pending_revisions():
    script = _load_migration_directory(ROOT)

    previous_head = _migration_state(
        script,
        ["20260829_global_writer_authority_v1"],
    )
    assert previous_head == {
        "current_revisions": ["20260829_global_writer_authority_v1"],
        "expected_heads": ["20260830_geo_sync_v1"],
        "pending_revisions": [
            "20260830_territorial_geocoding_v1",
            "20260830_geo_review_v1",
            "20260830_geo_sync_v1",
        ],
        "at_head": False,
    }

    state = _migration_state(script, ["20260830_geo_sync_v1"])

    assert state["at_head"] is True
    assert state["pending_revisions"] == []


def test_unknown_database_revision_fails_closed():
    script = _load_migration_directory(ROOT)

    with pytest.raises(PreflightFailure) as captured:
        _migration_state(script, ["unknown-production-revision"])

    assert captured.value.reason_code == "database_migration_revision_unknown"


def test_failure_payload_never_contains_provider_detail():
    secret = "postgresql://user:password@private.neon.tech/neondb"
    payload = _failure_payload(
        "database_preflight_failed",
        error_type="OperationalError",
    )

    assert payload == {
        "contract_version": "chatboc.neon_cutover_preflight.v1",
        "status": "blocked",
        "ready": False,
        "reason_code": "database_preflight_failed",
        "error_type": "OperationalError",
    }
    assert secret not in str(payload)


@pytest.mark.parametrize(
    ("in_recovery", "wal_lsn", "expected_source", "expected_function"),
    [
        (False, "0/4C2D9B8", "current", "pg_current_wal_lsn()"),
        (True, "0/4C2D790", "replay", "pg_last_wal_replay_lsn()"),
    ],
)
def test_wal_position_supports_primary_and_read_replica(
    in_recovery,
    wal_lsn,
    expected_source,
    expected_function,
):
    statements = []

    class FakeResult:
        def __init__(self, value):
            self.value = value

        def scalar_one(self):
            return self.value

        def scalar_one_or_none(self):
            return self.value

    class FakeConnection:
        def execute(self, statement):
            sql = str(statement)
            statements.append(sql)
            if "pg_is_in_recovery" in sql:
                return FakeResult(in_recovery)
            return FakeResult(wal_lsn)

    state = _wal_position(FakeConnection())

    assert state == {
        "wal_lsn": wal_lsn,
        "wal_lsn_source": expected_source,
        "in_recovery": in_recovery,
    }
    assert expected_function in statements[1]


def test_wal_position_fails_closed_when_replica_has_not_replayed_wal():
    class FakeResult:
        def __init__(self, value):
            self.value = value

        def scalar_one(self):
            return self.value

        def scalar_one_or_none(self):
            return self.value

    class FakeConnection:
        calls = 0

        def execute(self, _statement):
            self.calls += 1
            return FakeResult(True if self.calls == 1 else None)

    with pytest.raises(PreflightFailure) as captured:
        _wal_position(FakeConnection())

    assert captured.value.reason_code == "database_wal_lsn_missing"


def test_run_preflight_selects_psycopg_v3_explicitly(monkeypatch):
    captured = {}

    class FakeEngine:
        def connect(self):
            raise RuntimeError("stop_after_driver_assertion")

        def dispose(self):
            pass

    def fake_create_engine(url, **kwargs):
        captured["drivername"] = url.drivername
        captured["kwargs"] = kwargs
        return FakeEngine()

    monkeypatch.setattr(
        "scripts.preflight_neon_cutover.create_engine",
        fake_create_engine,
    )

    with pytest.raises(RuntimeError, match="stop_after_driver_assertion"):
        run_preflight(
            database_url=DIRECT_NEON_URL,
            project_root=ROOT,
            expected_project_id="nameless-rain-94060889",
            expected_branch_id="br-falling-wind-actm4mcu",
        )

    assert captured == {
        "drivername": "postgresql+psycopg",
        "kwargs": {"pool_pre_ping": True, "pool_recycle": 300},
    }
