from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.audit_render_neon_parity import (
    DEFAULT_POLICY_PATH,
    EXIT_ATTESTATION_REQUIRED,
    EXIT_CERTIFIED,
    EXIT_CONFIGURATION_OR_RUNTIME_BLOCKED,
    EXIT_PARITY_MISMATCH,
    ColumnSpec,
    ParityAuditFailure,
    SqliteSnapshotReader,
    _canonical_sha256,
    _failure_payload,
    _fingerprint_key,
    _load_policy,
    _open_sqlite_snapshot,
    _sqlite_file_state,
    _status_and_exit,
    compare_snapshots,
    main,
)


HMAC_KEY = b"parity-test-key-that-is-at-least-32-bytes"


class FakeReader:
    def __init__(self, provider, schemas, rows):
        self.provider = provider
        self.schemas = schemas
        self.data = rows

    def table_names(self):
        return sorted(self.schemas)

    def columns(self, table_name):
        return list(self.schemas[table_name])

    def count(self, table_name):
        return len(self.data[table_name])

    def rows(self, table_name, column_names):
        schema_names = [item.name for item in self.schemas[table_name]]
        indexes = [schema_names.index(item) for item in column_names]
        for row in self.data[table_name]:
            yield tuple(row[index] for index in indexes)


def _policy():
    return _load_policy(DEFAULT_POLICY_PATH)


def _reader_pair(*, destination_secret="dato privado", include_extra=True):
    source_schema = {
        "example": [
            ColumnSpec("id", "INTEGER", 1),
            ColumnSpec("secret", "TEXT"),
            ColumnSpec("active", "BOOLEAN"),
            ColumnSpec("payload", "JSON"),
            ColumnSpec("created_at", "DATETIME"),
        ],
        "alembic_version": [ColumnSpec("version_num", "TEXT", 1)],
    }
    destination_schema = {
        "example": [
            ColumnSpec("id", "BIGINT", 1),
            ColumnSpec("secret", "TEXT"),
            ColumnSpec("active", "BOOLEAN"),
            ColumnSpec("payload", "JSONB"),
            ColumnSpec("created_at", "TIMESTAMP WITH TIME ZONE"),
            ColumnSpec("destination_only", "TEXT"),
        ],
        "neon_only": [ColumnSpec("id", "INTEGER", 1)],
    }
    destination_rows = [
        (
            1,
            destination_secret,
            True,
            {"answer": 42, "nested": ["x"]},
            datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc),
            "new surface",
        )
    ]
    if include_extra:
        destination_rows.append(
            (
                2,
                "destination-only row",
                False,
                {"answer": 7},
                datetime(2026, 8, 29, 13, 0, tzinfo=timezone.utc),
                "new surface",
            )
        )
    source = FakeReader(
        "render_sqlite_snapshot",
        source_schema,
        {
            "example": [
                (
                    1,
                    "dato privado",
                    1,
                    '{"nested":["x"],"answer":42}',
                    "2026-08-29 12:00:00",
                )
            ],
            "alembic_version": [("legacy",)],
        },
    )
    destination = FakeReader(
        "neon",
        destination_schema,
        {"example": destination_rows, "neon_only": [(1,)]},
    )
    return source, destination


def test_default_policy_is_semantically_versioned_and_strict():
    policy = _policy()

    assert policy["document"]["contract_version"] == (
        "chatboc.render_neon_parity_policy.v1"
    )
    assert policy["document"]["excluded_tables"] == {
        "alembic_version": (
            "Neon schema history is validated by the separate cutover preflight."
        )
    }
    assert len(policy["sha256"]) == 64


def test_source_inclusion_allows_destination_growth_and_never_emits_cell_values():
    source, destination = _reader_pair()

    result = compare_snapshots(
        source=source,
        destination=destination,
        policy=_policy(),
        hmac_key=HMAC_KEY,
        max_rows_per_table=100,
    )

    assert result["parity"] is True
    assert result["summary"] == {
        "tables_checked": 1,
        "tables_matching": 1,
        "source_primary_keys_or_rows_missing": 0,
        "cells_checked": 4,
        "cells_mismatched": 0,
    }
    assert result["tables"][0]["primary_key"]["matched_count"] == 1
    serialized = json.dumps(result, sort_keys=True)
    assert "dato privado" not in serialized
    assert "destination-only row" not in serialized


def test_cell_mismatch_is_counted_without_disclosing_the_value():
    source, destination = _reader_pair(destination_secret="otro secreto")

    result = compare_snapshots(
        source=source,
        destination=destination,
        policy=_policy(),
        hmac_key=HMAC_KEY,
        max_rows_per_table=100,
    )

    table = result["tables"][0]
    assert result["parity"] is False
    assert table["cells"]["mismatch_count"] == 1
    assert table["cells"]["mismatch_count_by_column"] == {"secret": 1}
    assert "otro secreto" not in json.dumps(result)


def test_missing_primary_key_fails_even_when_destination_count_is_not_smaller():
    source, destination = _reader_pair(include_extra=False)
    source.data["example"].append(
        (2, "legacy second", 0, '{"answer":7}', "2026-08-29 13:00:00")
    )
    destination.data["example"].append(
        (
            3,
            "different key",
            False,
            {"answer": 7},
            datetime(2026, 8, 29, 13, 0, tzinfo=timezone.utc),
            "new surface",
        )
    )

    result = compare_snapshots(
        source=source,
        destination=destination,
        policy=_policy(),
        hmac_key=HMAC_KEY,
        max_rows_per_table=100,
    )

    assert result["parity"] is False
    assert result["tables"][0]["rows"]["destination_includes_source_count"] is True
    assert result["tables"][0]["primary_key"]["missing_count"] == 1


@pytest.mark.parametrize(("legacy_value", "expected"), [(None, True), ("url", False)])
def test_documented_source_only_column_is_allowed_only_when_null(legacy_value, expected):
    source = FakeReader(
        "render_sqlite_snapshot",
        {
                "municipio_ticket": [
                    ColumnSpec("id", "INTEGER", 1),
                    ColumnSpec("estado", "TEXT"),
                    ColumnSpec("archivo_url", "TEXT"),
                ]
            },
        {"municipio_ticket": [(1, "nuevo", legacy_value)]},
    )
    destination = FakeReader(
        "neon",
        {
            "municipio_ticket": [
                ColumnSpec("id", "BIGINT", 1),
                ColumnSpec("estado", "TEXT"),
            ]
        },
        {"municipio_ticket": [(1, "cerrado")]},
    )

    result = compare_snapshots(
        source=source,
        destination=destination,
        policy=_policy(),
        hmac_key=HMAC_KEY,
        max_rows_per_table=100,
    )

    assert result["parity"] is expected
    assert result["tables"][0]["schema"]["allowed_source_only_null_checks"] == {
        "archivo_url": expected
    }


def test_unkeyed_tables_use_multiset_inclusion_and_preserve_duplicates():
    schema = {"legacy_log": [ColumnSpec("event", "TEXT")]}
    source = FakeReader(
        "render_sqlite_snapshot",
        schema,
        {"legacy_log": [("same",), ("same",)]},
    )
    destination = FakeReader(
        "neon",
        schema,
        {"legacy_log": [("same",), ("same",), ("new",)]},
    )

    result = compare_snapshots(
        source=source,
        destination=destination,
        policy=_policy(),
        hmac_key=HMAC_KEY,
        max_rows_per_table=100,
    )

    assert result["parity"] is True
    assert result["tables"][0]["comparison_mode"] == (
        "unkeyed_row_multiset_inclusion"
    )
    assert result["tables"][0]["primary_key"]["matched_count"] == 2


def test_sqlite_snapshot_connection_is_query_only(tmp_path):
    path = tmp_path / "render snapshot.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE legacy (id INTEGER PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO legacy (value) VALUES ('private')")
    connection.commit()
    connection.close()

    state = _sqlite_file_state(path)
    assert state["size_bytes"] > 100
    assert len(state["sha256"]) == 64

    with _open_sqlite_snapshot(path) as readonly:
        reader = SqliteSnapshotReader(readonly)
        assert reader.count("legacy") == 1
        assert readonly.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            readonly.execute("INSERT INTO legacy (value) VALUES ('forbidden')")


def test_sqlite_snapshot_with_wal_sidecar_fails_closed(tmp_path):
    path = tmp_path / "database.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE legacy (id INTEGER PRIMARY KEY)")
    connection.close()
    Path(f"{path}-wal").write_bytes(b"not a frozen standalone snapshot")

    with pytest.raises(ParityAuditFailure) as captured:
        _sqlite_file_state(path)

    assert captured.value.reason_code == "source_snapshot_sidecar_present"


def test_status_codes_distinguish_parity_attestation_and_certification():
    assert _status_and_exit(parity=False, writers_fenced=True) == (
        "parity_mismatch",
        EXIT_PARITY_MISMATCH,
    )
    assert _status_and_exit(parity=True, writers_fenced=False) == (
        "writer_fence_attestation_required",
        EXIT_ATTESTATION_REQUIRED,
    )
    assert _status_and_exit(parity=True, writers_fenced=True) == (
        "certified",
        EXIT_CERTIFIED,
    )


def test_hmac_key_is_required_and_never_returned_by_failure_payload():
    secret = "database-and-hmac-secret-that-must-never-appear"
    assert _fingerprint_key(secret) == secret.encode("utf-8")
    with pytest.raises(ParityAuditFailure):
        _fingerprint_key("short")

    payload = _failure_payload("parity_audit_failed", error_type="RuntimeError")
    assert secret not in json.dumps(payload)
    digest = payload.pop("evidence_sha256")
    assert digest == _canonical_sha256(payload)


def test_cli_missing_environment_is_redacted(capsys, monkeypatch):
    for name in (
        "MIGRATIONS_DATABASE_URL",
        "EXPECTED_NEON_PROJECT_ID",
        "EXPECTED_NEON_BRANCH_ID",
        "PARITY_AUDIT_HMAC_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    private_path = "C:/private/operator/render-database.db"

    exit_code = main(
        [
            "--source-sqlite",
            private_path,
            "--source-snapshot-id",
            "render-final-20260829-001",
            "--fingerprint-key-id",
            "parity-hmac-2026-08",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == EXIT_CONFIGURATION_OR_RUNTIME_BLOCKED
    assert private_path not in output
    assert "database_environment_variable_missing" in output
