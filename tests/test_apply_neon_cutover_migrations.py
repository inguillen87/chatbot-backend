from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import scripts.apply_neon_cutover_migrations as cutover


ROOT = Path(__file__).resolve().parents[1]
DIRECT_HOST = "ep-safe-branch.us-east-2.aws.neon.tech"
DIRECT_NEON_URL = (
    "postgresql://cutover_user:cutover_password@"
    f"{DIRECT_HOST}/neondb?sslmode=require"
)
HOST_FINGERPRINT = hashlib.sha256(DIRECT_HOST.encode()).hexdigest()
PROJECT_ID = "nameless-rain-94060889"
PROJECT_FINGERPRINT = hashlib.sha256(PROJECT_ID.encode()).hexdigest()
BRANCH_ID = "br-safe-cutover-acmnikpq"
BRANCH_FINGERPRINT = hashlib.sha256(BRANCH_ID.encode()).hexdigest()


def test_database_url_is_loaded_only_from_the_explicit_environment_name():
    environ = {
        "DATABASE_URL": "postgresql://implicit:secret@wrong.invalid/db",
        "CUTOVER_NEON_DIRECT_URL": DIRECT_NEON_URL,
    }

    assert (
        cutover._load_database_url(environ, "CUTOVER_NEON_DIRECT_URL")
        == DIRECT_NEON_URL
    )

    with pytest.raises(cutover.CutoverMigrationFailure) as captured:
        cutover._load_database_url(
            {"DATABASE_URL": environ["DATABASE_URL"]},
            "CUTOVER_NEON_DIRECT_URL",
        )
    assert captured.value.reason_code == "database_environment_variable_missing"

    with pytest.raises(cutover.CutoverMigrationFailure) as captured:
        cutover._load_database_url(environ, None)
    assert captured.value.reason_code == "database_environment_variable_name_required"


def test_target_requires_matching_direct_tls_neon_host_fingerprint():
    parsed, fingerprint = cutover._validate_target_url(
        DIRECT_NEON_URL,
        expected_host_fingerprint_sha256=HOST_FINGERPRINT,
    )

    assert parsed.host == DIRECT_HOST
    assert fingerprint == HOST_FINGERPRINT


@pytest.mark.parametrize(
    ("url", "reason_code"),
    [
        (
            "postgresql://user:secret@ep-safe-pooler.us-east-2.aws.neon.tech/db?sslmode=require",
            "database_connection_not_direct",
        ),
        (
            "postgresql://user:secret@ep-safe.us-east-2.aws.neon.tech/db",
            "database_tls_not_required",
        ),
        (
            "postgresql://user:secret@postgres.example.com/db?sslmode=require",
            "database_provider_not_neon",
        ),
        (
            "postgresql://user@ep-safe.us-east-2.aws.neon.tech/db?sslmode=require",
            "database_password_missing",
        ),
        (
            "postgresql://user:secret@ep-safe.us-east-2.aws.neon.tech/db"
            "?sslmode=require&service=ambient",
            "database_indirect_connection_parameter_forbidden",
        ),
    ],
)
def test_target_rejects_unsafe_database_urls_without_echoing_them(url, reason_code):
    with pytest.raises(cutover.CutoverMigrationFailure) as captured:
        cutover._validate_target_url(
            url,
            expected_host_fingerprint_sha256=HOST_FINGERPRINT,
        )

    assert captured.value.reason_code == reason_code
    assert "secret" not in str(captured.value)
    assert url not in str(captured.value)


def test_target_rejects_a_valid_neon_url_with_the_wrong_host_fingerprint():
    with pytest.raises(cutover.CutoverMigrationFailure) as captured:
        cutover._validate_target_url(
            DIRECT_NEON_URL,
            expected_host_fingerprint_sha256="0" * 64,
        )

    assert captured.value.reason_code == "database_neon_host_fingerprint_mismatch"
    assert DIRECT_HOST not in str(captured.value)


def test_local_graph_is_exactly_the_seven_reviewed_revisions():
    plan = cutover._load_exact_migration_plan(ROOT)

    assert list(plan.source_fingerprints_sha256) == list(cutover.MIGRATION_STEPS)
    assert dict(plan.source_fingerprints_sha256) == (
        cutover.EXPECTED_MIGRATION_SOURCE_SHA256
    )
    assert all(len(value) == 64 for value in plan.source_fingerprints_sha256.values())
    assert len(plan.graph_fingerprint_sha256) == 64
    assert plan.script.get_heads() == [cutover.TERRITORIAL_GEOCODING_SYNC_REVISION]


def test_territorial_schema_contract_requires_exact_columns_keys_and_indexes(
    monkeypatch,
):
    monkeypatch.setattr(cutover, "_table_exists", lambda *_args: True)
    monkeypatch.setattr(
        cutover,
        "_column_names",
        lambda _connection, table_name: cutover.EXPECTED_TERRITORIAL_SCHEMA[
            table_name
        ]["columns"],
    )
    monkeypatch.setattr(
        cutover,
        "_constraint_names",
        lambda _connection, table_name: cutover.EXPECTED_TERRITORIAL_SCHEMA[
            table_name
        ]["constraints"],
    )
    monkeypatch.setattr(
        cutover,
        "_foreign_key_contract",
        lambda _connection, table_name: cutover.EXPECTED_TERRITORIAL_SCHEMA[
            table_name
        ]["foreign_keys"],
    )

    def exact_index(_connection, *, table_name, index_name):
        columns, unique = cutover.EXPECTED_TERRITORIAL_SCHEMA[table_name]["indexes"][
            index_name
        ]
        return {
            "columns": columns,
            "is_unique": unique,
            "is_valid": True,
            "is_ready": True,
            "is_unfiltered": True,
            "has_plain_columns": True,
            "has_no_included_columns": True,
        }

    monkeypatch.setattr(cutover, "_index_contract", exact_index)

    for table_name in cutover.EXPECTED_TERRITORIAL_SCHEMA:
        assert all(cutover._territorial_table_contract(object(), table_name).values())

    def wrong_order(_connection, *, table_name, index_name):
        result = exact_index(
            _connection,
            table_name=table_name,
            index_name=index_name,
        )
        if index_name == "ix_territorial_geocoding_job_tenant_status_created":
            result["columns"] = ("tenant_id", "created_at", "status", "id")
        return result

    monkeypatch.setattr(cutover, "_index_contract", wrong_order)
    contract = cutover._territorial_table_contract(
        object(),
        "territorial_geocoding_job",
    )
    assert contract["exact_indexes_valid"] is False


def test_territorial_schema_postcheck_rejects_out_of_order_future_table(monkeypatch):
    valid_contract = {
        "table_present": True,
        "exact_columns_present": True,
        "expected_constraints_present": True,
        "exact_foreign_keys_present": True,
        "exact_indexes_valid": True,
    }
    monkeypatch.setattr(
        cutover,
        "_territorial_table_contract",
        lambda *_args: valid_contract,
    )
    monkeypatch.setattr(
        cutover,
        "_table_exists",
        lambda _connection, table_name: table_name
        == "territorial_geocoding_sync_receipt",
    )

    with pytest.raises(cutover.CutoverMigrationFailure) as captured:
        cutover._territorial_schema_postcheck(
            object(),
            required_tables=("territorial_geocoding_job",),
            absent_tables=("territorial_geocoding_sync_receipt",),
            reason_code="database_territorial_geocoding_contract_postcheck_failed",
        )
    assert (
        captured.value.reason_code
        == "database_territorial_geocoding_contract_postcheck_failed"
    )


def test_inbound_fifo_postcheck_requires_the_exact_ordered_plain_index(monkeypatch):
    prior = {
        "demo_survey_contract_valid": True,
        "legacy_ticket_scope": {"rows_present": 3},
        "idempotency_contract": {"table_present": True},
    }
    monkeypatch.setattr(cutover, "_assert_after_idempotency", lambda _connection: prior)
    valid = {
        "columns": cutover.EXPECTED_INBOUND_FIFO_COLUMNS,
        "is_unique": False,
        "is_valid": True,
        "is_ready": True,
        "is_unfiltered": True,
        "has_plain_columns": True,
        "has_no_included_columns": True,
    }
    monkeypatch.setattr(cutover, "_index_contract", lambda *_args, **_kwargs: valid)

    state = cutover._assert_after_inbound_fifo(object())
    assert state["inbound_fifo_index"] == valid

    wrong_order = {
        **valid,
        "columns": ("tenant_id", "stream_key", "id", "received_at"),
    }
    monkeypatch.setattr(
        cutover,
        "_index_contract",
        lambda *_args, **_kwargs: wrong_order,
    )
    with pytest.raises(cutover.CutoverMigrationFailure) as captured:
        cutover._assert_after_inbound_fifo(object())
    assert captured.value.reason_code == "database_inbound_fifo_index_postcheck_failed"


def test_global_writer_authority_postcheck_requires_fenced_singleton(monkeypatch):
    prior = {"inbound_fifo_index": {"is_valid": True}}
    monkeypatch.setattr(
        cutover,
        "_assert_after_inbound_fifo",
        lambda _connection: prior,
    )
    monkeypatch.setattr(cutover, "_table_exists", lambda *_args: True)
    monkeypatch.setattr(
        cutover,
        "_column_names",
        lambda *_args: cutover.EXPECTED_GLOBAL_WRITER_AUTHORITY_COLUMNS,
    )
    monkeypatch.setattr(
        cutover,
        "_constraint_names",
        lambda *_args: cutover.EXPECTED_GLOBAL_WRITER_AUTHORITY_CONSTRAINTS,
    )

    class MappingRows:
        def mappings(self):
            return [
                {
                    "authority_key": "primary",
                    "owner_runtime": None,
                    "epoch": 0,
                    "render_fenced": True,
                    "vercel_fenced": True,
                }
            ]

    class Connection:
        def execute(self, *_args, **_kwargs):
            return MappingRows()

    state = cutover._assert_after_global_writer_authority(Connection())
    assert state["global_writer_authority_contract"] == {
        "table_present": True,
        "expected_columns_present": True,
        "expected_constraints_present": True,
        "singleton_state_valid": True,
        "singleton_bootstrap_fenced": True,
        "required_state_valid": True,
    }

    class UnsafeConnection:
        def execute(self, *_args, **_kwargs):
            result = MappingRows()
            result.mappings = lambda: [
                {
                    "authority_key": "primary",
                    "owner_runtime": "render",
                    "epoch": 0,
                    "render_fenced": False,
                    "vercel_fenced": True,
                }
            ]
            return result

    with pytest.raises(cutover.CutoverMigrationFailure) as captured:
        cutover._assert_after_global_writer_authority(UnsafeConnection())
    assert (
        captured.value.reason_code
        == "database_global_writer_authority_contract_postcheck_failed"
    )

    class UsedStateConnection:
        def execute(self, *_args, **_kwargs):
            result = MappingRows()
            result.mappings = lambda: [
                {
                    "authority_key": "primary",
                    "owner_runtime": "render",
                    "epoch": 23,
                    "render_fenced": False,
                    "vercel_fenced": True,
                }
            ]
            return result

    used_contract = cutover._global_writer_authority_contract(
        UsedStateConnection(),
        require_bootstrap=False,
    )
    assert used_contract["singleton_state_valid"] is True
    assert used_contract["singleton_bootstrap_fenced"] is False
    assert used_contract["required_state_valid"] is True


def test_apply_requires_three_distinct_approved_evidence_ids():
    assert (
        cutover._approval_evidence(
            apply=False,
            writer_fence_evidence_id=None,
            snapshot_evidence_id=None,
            parity_evidence_id=None,
        )
        is None
    )

    with pytest.raises(cutover.CutoverMigrationFailure) as captured:
        cutover._approval_evidence(
            apply=True,
            writer_fence_evidence_id=None,
            snapshot_evidence_id="snapshot-20260829",
            parity_evidence_id="parity-20260829",
        )
    assert captured.value.reason_code == "approved_writer_fence_evidence_id_required"

    with pytest.raises(cutover.CutoverMigrationFailure) as captured:
        cutover._approval_evidence(
            apply=True,
            writer_fence_evidence_id="evidence-20260829",
            snapshot_evidence_id="evidence-20260829",
            parity_evidence_id="parity-20260829",
        )
    assert captured.value.reason_code == "approved_evidence_ids_must_be_distinct"

    evidence = cutover._approval_evidence(
        apply=True,
        writer_fence_evidence_id="writer-fence-20260829",
        snapshot_evidence_id="snapshot-20260829",
        parity_evidence_id="parity-20260829",
    )
    assert evidence is not None
    assert set(evidence.fingerprints()) == {
        "writer_fence_sha256",
        "snapshot_sha256",
        "parity_sha256",
    }
    assert "writer-fence-20260829" not in str(evidence.fingerprints())


def test_transaction_mode_and_all_timeouts_are_fixed():
    class FakeConnection:
        def __init__(self):
            self.statements = []

        def exec_driver_sql(self, statement):
            self.statements.append(statement)

    dry_connection = FakeConnection()
    cutover._configure_transaction(dry_connection, apply=False)
    assert dry_connection.statements == [
        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE, READ ONLY",
        "SET LOCAL statement_timeout = '120000ms'",
        "SET LOCAL lock_timeout = '5000ms'",
        "SET LOCAL idle_in_transaction_session_timeout = '60000ms'",
        "SET LOCAL search_path = public, pg_catalog",
    ]

    apply_connection = FakeConnection()
    cutover._configure_transaction(apply_connection, apply=True)
    assert apply_connection.statements[0] == (
        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE, READ WRITE"
    )


def test_database_identity_compares_project_and_branch_only_as_fingerprints():
    class FakeMappings:
        def one(self):
            return {
                "project_id": PROJECT_ID,
                "branch_id": BRANCH_ID,
                "in_recovery": False,
            }

    class FakeResult:
        def __init__(self, *, mapping=False):
            self.mapping = mapping

        def mappings(self):
            assert self.mapping
            return FakeMappings()

        def scalar_one(self):
            return "off"

    class FakeConnection:
        def execute(self, statement, _parameters=None):
            return FakeResult(mapping="neon.project_id" in str(statement))

    state = cutover._assert_database_identity(
        FakeConnection(),
        expected_project_fingerprint_sha256=PROJECT_FINGERPRINT,
        expected_branch_fingerprint_sha256=BRANCH_FINGERPRINT,
    )

    assert state == {
        "project_fingerprint_sha256": PROJECT_FINGERPRINT,
        "branch_fingerprint_sha256": BRANCH_FINGERPRINT,
        "writable_primary": True,
    }
    assert PROJECT_ID not in str(state)
    assert BRANCH_ID not in str(state)


def test_baseline_requires_all_three_known_tickets_at_the_source(monkeypatch):
    monkeypatch.setattr(cutover, "_require_base_tables", lambda _connection: None)
    monkeypatch.setattr(
        cutover,
        "_demo_contract",
        lambda _connection: {
            "table_present": True,
            "expected_indexes_present": True,
            "immutability_trigger_present": True,
        },
    )
    monkeypatch.setattr(
        cutover,
        "_idempotency_contract",
        lambda _connection: {"table_present": False},
    )
    expected = {
        "rows_present": 3,
        "rows_scoped_to_source": 3,
        "rows_scoped_to_target": 0,
    }
    monkeypatch.setattr(cutover, "_repair_state", lambda _connection: expected)

    state = cutover._assert_baseline_schema(object())
    assert state["legacy_ticket_scope"] == expected

    monkeypatch.setattr(
        cutover,
        "_repair_state",
        lambda _connection: {
            "rows_present": 3,
            "rows_scoped_to_source": 2,
            "rows_scoped_to_target": 1,
        },
    )
    with pytest.raises(cutover.CutoverMigrationFailure) as captured:
        cutover._assert_baseline_schema(object())
    assert captured.value.reason_code == "database_legacy_ticket_repair_baseline_drift"


def test_apply_orchestration_runs_each_exact_revision_and_postcheck(monkeypatch):
    calls = []
    current = {"revision": cutover.INITIAL_REVISION}

    class ScalarResult:
        def scalar_one(self):
            return True

    class FakeConnection:
        def __init__(self):
            self.driver_statements = []

        def exec_driver_sql(self, statement):
            self.driver_statements.append(statement)

        def execute(self, statement, _parameters=None):
            assert "pg_try_advisory_xact_lock" in str(statement)
            return ScalarResult()

    connection = FakeConnection()
    monkeypatch.setattr(
        cutover,
        "_assert_database_identity",
        lambda *_args, **_kwargs: {
            "project_fingerprint_sha256": PROJECT_FINGERPRINT,
            "branch_fingerprint_sha256": BRANCH_FINGERPRINT,
            "writable_primary": True,
        },
    )

    def require_revision(_connection, expected):
        assert current["revision"] == expected
        return expected

    def apply_exact(
        _connection,
        *,
        plan,
        expected_current_revision,
        target_revision,
    ):
        assert plan is migration_plan
        assert current["revision"] == expected_current_revision
        assert target_revision not in {"head", "heads"}
        calls.append(target_revision)
        current["revision"] = target_revision

    monkeypatch.setattr(cutover, "_require_revision", require_revision)
    monkeypatch.setattr(
        cutover,
        "_require_allowlisted_cutover_revision",
        lambda _connection: current["revision"],
    )
    monkeypatch.setattr(
        cutover,
        "_assert_contract_for_revision",
        lambda _connection, revision: {"revision": revision},
    )
    monkeypatch.setattr(
        cutover,
        "_assert_after_repair",
        lambda _connection: {"repair": True},
    )
    monkeypatch.setattr(
        cutover,
        "_assert_after_idempotency",
        lambda _connection: {"idempotency": True},
    )
    monkeypatch.setattr(
        cutover,
        "_assert_after_inbound_fifo",
        lambda _connection: {"inbound_fifo": True},
    )
    monkeypatch.setattr(
        cutover,
        "_assert_after_global_writer_authority",
        lambda _connection: {"global_writer_authority": True},
    )
    monkeypatch.setattr(
        cutover,
        "_assert_after_territorial_geocoding",
        lambda _connection: {"territorial_geocoding": True},
    )
    monkeypatch.setattr(
        cutover,
        "_assert_after_territorial_review",
        lambda _connection: {"territorial_review": True},
    )
    monkeypatch.setattr(
        cutover,
        "_assert_after_territorial_sync",
        lambda _connection: {"territorial_sync": True},
    )
    monkeypatch.setattr(cutover, "_apply_exact_revision", apply_exact)
    migration_plan = cutover._load_exact_migration_plan(ROOT)

    state = cutover._execute_cutover_transaction(
        connection,
        apply=True,
        plan=migration_plan,
        expected_project_fingerprint_sha256=PROJECT_FINGERPRINT,
        expected_branch_fingerprint_sha256=BRANCH_FINGERPRINT,
    )

    assert calls == list(cutover.MIGRATION_STEPS)
    assert state["revision_before"] == cutover.INITIAL_REVISION
    assert state["revision_after"] == cutover.TERRITORIAL_GEOCODING_SYNC_REVISION
    assert state["advisory_lock_acquired"] is True
    assert [step["revision"] for step in state["steps"]] == list(
        cutover.MIGRATION_STEPS
    )
    assert connection.driver_statements[0].endswith("READ WRITE")


def test_incremental_apply_from_review_runs_only_exact_sync_child(monkeypatch):
    current = {"revision": cutover.TERRITORIAL_GEOCODING_REVIEW_REVISION}
    calls = []

    class ScalarResult:
        def scalar_one(self):
            return True

    class Connection:
        def exec_driver_sql(self, _statement):
            return None

        def execute(self, statement, _parameters=None):
            assert "pg_try_advisory_xact_lock" in str(statement)
            return ScalarResult()

    monkeypatch.setattr(
        cutover,
        "_assert_database_identity",
        lambda *_args, **_kwargs: {"writable_primary": True},
    )
    monkeypatch.setattr(
        cutover,
        "_require_allowlisted_cutover_revision",
        lambda _connection: current["revision"],
    )
    monkeypatch.setattr(
        cutover,
        "_assert_contract_for_revision",
        lambda _connection, revision: {"revision": revision, "valid": True},
    )

    def apply_exact(
        _connection,
        *,
        plan,
        expected_current_revision,
        target_revision,
    ):
        assert plan is migration_plan
        assert (
            expected_current_revision
            == cutover.TERRITORIAL_GEOCODING_REVIEW_REVISION
        )
        assert target_revision == cutover.TERRITORIAL_GEOCODING_SYNC_REVISION
        calls.append(target_revision)
        current["revision"] = target_revision

    monkeypatch.setattr(cutover, "_apply_exact_revision", apply_exact)
    monkeypatch.setattr(
        cutover,
        "_require_revision",
        lambda _connection, expected: (
            expected
            if current["revision"] == expected
            else pytest.fail("unexpected revision")
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_postcheck_for_applied_revision",
        lambda _connection, revision: {"revision": revision, "valid": True},
    )
    migration_plan = cutover._load_exact_migration_plan(ROOT)

    state = cutover._execute_cutover_transaction(
        Connection(),
        apply=True,
        plan=migration_plan,
        expected_project_fingerprint_sha256=PROJECT_FINGERPRINT,
        expected_branch_fingerprint_sha256=BRANCH_FINGERPRINT,
    )

    assert calls == [cutover.TERRITORIAL_GEOCODING_SYNC_REVISION]
    assert state["revision_before"] == cutover.TERRITORIAL_GEOCODING_REVIEW_REVISION
    assert state["revision_after"] == cutover.TERRITORIAL_GEOCODING_SYNC_REVISION
    assert [item["revision"] for item in state["steps"]] == calls


def test_exact_revision_runner_rejects_symbolic_or_unreviewed_targets():
    plan = cutover._load_exact_migration_plan(ROOT)

    for target in ("head", "heads", "20260825_unreviewed_revision"):
        with pytest.raises(cutover.CutoverMigrationFailure) as captured:
            cutover._apply_exact_revision(
                object(),
                plan=plan,
                expected_current_revision=cutover.INITIAL_REVISION,
                target_revision=target,
            )
        assert captured.value.reason_code == "migration_target_not_allowlisted"


def test_default_dry_run_never_locks_or_invokes_an_upgrade(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.driver_statements = []

        def exec_driver_sql(self, statement):
            self.driver_statements.append(statement)

        def execute(self, *_args, **_kwargs):
            raise AssertionError("dry-run must not acquire the advisory write lock")

    connection = FakeConnection()
    monkeypatch.setattr(
        cutover,
        "_assert_database_identity",
        lambda *_args, **_kwargs: {
            "project_fingerprint_sha256": PROJECT_FINGERPRINT,
            "branch_fingerprint_sha256": BRANCH_FINGERPRINT,
            "writable_primary": True,
        },
    )
    monkeypatch.setattr(
        cutover,
        "_require_allowlisted_cutover_revision",
        lambda _connection: cutover.INITIAL_REVISION,
    )
    monkeypatch.setattr(
        cutover,
        "_assert_contract_for_revision",
        lambda _connection, revision: {"revision": revision},
    )
    monkeypatch.setattr(
        cutover,
        "_apply_exact_revision",
        lambda *_args, **_kwargs: pytest.fail("dry-run invoked an upgrade"),
    )

    state = cutover._execute_cutover_transaction(
        connection,
        apply=False,
        plan=cutover._load_exact_migration_plan(ROOT),
        expected_project_fingerprint_sha256=PROJECT_FINGERPRINT,
        expected_branch_fingerprint_sha256=BRANCH_FINGERPRINT,
    )

    assert state["revision_before"] == state["revision_after"]
    assert state["steps"] == []
    assert state["advisory_lock_acquired"] is False
    assert connection.driver_statements[0].endswith("READ ONLY")


def test_run_cutover_reports_territorial_sync_as_final_revision(monkeypatch):
    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def begin(self):
            return self

    class FakeEngine:
        def __init__(self):
            self.disposed = False

        def connect(self):
            return FakeConnection()

        def dispose(self):
            self.disposed = True

    engine = FakeEngine()
    monkeypatch.setattr(cutover, "create_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(
        cutover,
        "_execute_cutover_transaction",
        lambda *_args, **_kwargs: {
            "identity": {
                "project_fingerprint_sha256": PROJECT_FINGERPRINT,
                "branch_fingerprint_sha256": BRANCH_FINGERPRINT,
                "writable_primary": True,
            }
        },
    )

    report = cutover.run_cutover(
        database_url=DIRECT_NEON_URL,
        project_root=ROOT,
        apply=False,
        expected_host_fingerprint_sha256=HOST_FINGERPRINT,
        expected_project_fingerprint_sha256=PROJECT_FINGERPRINT,
        expected_branch_fingerprint_sha256=BRANCH_FINGERPRINT,
    )

    assert (
        report["plan"]["final_revision"]
        == cutover.TERRITORIAL_GEOCODING_SYNC_REVISION
    )
    assert engine.disposed is True


def test_failure_inside_apply_escapes_the_transaction_for_rollback(monkeypatch):
    transaction_state = {"rolled_back": False, "committed": False}

    class FakeTransaction:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, _exc, _traceback):
            transaction_state["rolled_back"] = exc_type is not None
            transaction_state["committed"] = exc_type is None
            return False

    class FakeConnection:
        def begin(self):
            return FakeTransaction()

    class FakeConnectionContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, *_args):
            return False

    class FakeEngine:
        disposed = False

        def connect(self):
            return FakeConnectionContext()

        def dispose(self):
            self.disposed = True

    engine = FakeEngine()
    monkeypatch.setattr(cutover, "create_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(
        cutover,
        "_execute_cutover_transaction",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            cutover.CutoverMigrationFailure("postcheck_failed")
        ),
    )

    with pytest.raises(cutover.CutoverMigrationFailure):
        cutover.run_cutover(
            database_url=DIRECT_NEON_URL,
            project_root=ROOT,
            apply=True,
            expected_host_fingerprint_sha256=HOST_FINGERPRINT,
            expected_project_fingerprint_sha256=PROJECT_FINGERPRINT,
            expected_branch_fingerprint_sha256=BRANCH_FINGERPRINT,
            writer_fence_evidence_id="writer-fence-20260829",
            snapshot_evidence_id="snapshot-20260829",
            parity_evidence_id="parity-20260829",
        )

    assert transaction_state == {"rolled_back": True, "committed": False}
    assert engine.disposed is True


def test_main_failure_json_never_serializes_dsn_or_exception_text(monkeypatch, capsys):
    def fail_with_secret(**_kwargs):
        raise RuntimeError(
            "driver leaked cutover_password at " + DIRECT_HOST + " project=" + PROJECT_ID
        )

    monkeypatch.setattr(cutover, "run_cutover", fail_with_secret)
    exit_code = cutover.main(
        [
            "--environment-variable",
            "CUTOVER_NEON_DIRECT_URL",
            "--expected-host-fingerprint-sha256",
            HOST_FINGERPRINT,
            "--expected-project-fingerprint-sha256",
            PROJECT_FINGERPRINT,
            "--expected-branch-fingerprint-sha256",
            BRANCH_FINGERPRINT,
        ],
        environ={
            "DATABASE_URL": "postgresql://implicit:implicit@wrong.invalid/db",
            "CUTOVER_NEON_DIRECT_URL": DIRECT_NEON_URL,
        },
    )

    payload_text = capsys.readouterr().out.strip()
    payload = json.loads(payload_text)
    assert exit_code == 2
    assert payload["reason_code"] == "cutover_migration_runtime_failed"
    assert payload["error_type"] == "RuntimeError"
    assert "cutover_password" not in payload_text
    assert DIRECT_HOST not in payload_text
    assert PROJECT_ID not in payload_text
    assert "wrong.invalid" not in payload_text


def test_main_does_not_fall_back_to_database_url(monkeypatch, capsys):
    called = False

    def unexpected_call(**_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(cutover, "run_cutover", unexpected_call)
    exit_code = cutover.main([], environ={"DATABASE_URL": DIRECT_NEON_URL})

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert called is False
    assert payload["reason_code"] == "database_environment_variable_name_required"


def test_argument_errors_do_not_echo_a_mistaken_dsn(capsys):
    exit_code = cutover.main(
        ["--database-url", DIRECT_NEON_URL, "--apply"],
        environ={},
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 2
    assert payload["reason_code"] == "command_arguments_invalid"
    assert payload["mode"] == "apply"
    assert DIRECT_HOST not in captured.out
    assert "cutover_password" not in captured.out
    assert captured.err == ""
