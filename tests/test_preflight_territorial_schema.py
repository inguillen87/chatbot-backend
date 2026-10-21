from __future__ import annotations

from typing import Any

import pytest

import scripts.preflight_neon_cutover as preflight
from scripts.preflight_neon_cutover import (
    TERRITORIAL_SCHEMA_REQUIREMENTS,
    _territorial_schema_state,
)


TERRITORIAL_TABLES = {
    "territorial_geocoding_job",
    "territorial_geocoding_attempt",
    "territorial_geocoding_review",
    "territorial_geocoding_sync_receipt",
}


class _MappingResult:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def mappings(self):
        return list(self._rows)


class _TerritorialSchemaConnection:
    def __init__(self, drift: str | None = None):
        self.drift = drift

    def execute(self, statement, parameters):
        sql = " ".join(str(statement).split())
        table_name = parameters["table_name"]
        requirements = TERRITORIAL_SCHEMA_REQUIREMENTS[table_name]

        if "information_schema.referential_constraints" in sql:
            return _MappingResult(self._foreign_key_rows(table_name, requirements))
        if "FROM pg_catalog.pg_class table_relation" in sql:
            return _MappingResult(self._index_rows(table_name, requirements))
        if "FROM information_schema.table_constraints constraints" in sql:
            return _MappingResult(self._constraint_rows(table_name, requirements))
        raise AssertionError(f"unexpected schema query: {sql}")

    def _constraint_rows(self, table_name, requirements):
        rows = []
        for constraint_name, constraint_type in requirements["constraints"].items():
            if (
                self.drift == "constraint"
                and table_name == "territorial_geocoding_job"
                and constraint_name == "ck_territorial_geocoding_job_status"
            ):
                continue
            columns = requirements["constraint_columns"].get(constraint_name) or (
                None,
            )
            for ordinal_position, column_name in enumerate(columns, start=1):
                rows.append(
                    {
                        "constraint_name": constraint_name,
                        "constraint_type": constraint_type,
                        "column_name": column_name,
                        "ordinal_position": (
                            ordinal_position if column_name is not None else None
                        ),
                    }
                )

        primary_key = requirements["primary_key"]
        if self.drift == "primary_key" and table_name == "territorial_geocoding_job":
            primary_key = ("tenant_id", "id")
        for ordinal_position, column_name in enumerate(primary_key, start=1):
            rows.append(
                {
                    "constraint_name": f"{table_name}_pkey",
                    "constraint_type": "PRIMARY KEY",
                    "column_name": column_name,
                    "ordinal_position": ordinal_position,
                }
            )
        return rows

    def _index_rows(self, table_name, requirements):
        rows = []
        for index_name, (columns, is_unique) in requirements["indexes"].items():
            if (
                self.drift == "index"
                and table_name == "territorial_geocoding_sync_receipt"
                and index_name == "ix_territorial_geocoding_sync_tenant_created"
            ):
                continue
            for ordinal_position, column_name in enumerate(columns, start=1):
                rows.append(
                    {
                        "index_name": index_name,
                        "is_unique": is_unique,
                        "column_name": column_name,
                        "ordinal_position": ordinal_position,
                    }
                )
        return rows

    def _foreign_key_rows(self, table_name, requirements):
        rows = []
        for position, foreign_key in enumerate(requirements["foreign_keys"], start=1):
            source_columns, schema, target_table, target_columns, delete_rule = (
                foreign_key
            )
            if (
                self.drift == "foreign_key"
                and table_name == "territorial_geocoding_review"
                and source_columns == ("reviewer_user_id",)
            ):
                delete_rule = "CASCADE"
            for ordinal_position, (source_column, target_column) in enumerate(
                zip(source_columns, target_columns, strict=True),
                start=1,
            ):
                rows.append(
                    {
                        "constraint_name": f"{table_name}_fk_{position}",
                        "source_column": source_column,
                        "referenced_schema": schema,
                        "referenced_table": target_table,
                        "referenced_column": target_column,
                        "delete_rule": delete_rule,
                        "ordinal_position": ordinal_position,
                    }
                )
        return rows


def _counts_without(table_name: str | None = None) -> dict[str, int]:
    return {
        name: position
        for position, name in enumerate(sorted(TERRITORIAL_TABLES), start=1)
        if name != table_name
    }


def test_territorial_preflight_covers_all_four_migration_tables():
    assert set(TERRITORIAL_SCHEMA_REQUIREMENTS) == TERRITORIAL_TABLES
    assert TERRITORIAL_SCHEMA_REQUIREMENTS["territorial_geocoding_job"][
        "constraint_columns"
    ]["uq_territorial_geocoding_job_candidate"] == (
        "tenant_id",
        "candidate_fingerprint",
    )
    assert TERRITORIAL_SCHEMA_REQUIREMENTS["territorial_geocoding_review"][
        "foreign_keys"
    ][-1] == (
        ("reviewer_user_id",),
        "public",
        "user",
        ("id",),
        "RESTRICT",
    )


def test_territorial_preflight_accepts_the_complete_head_schema():
    state = _territorial_schema_state(
        _TerritorialSchemaConnection(),
        _counts_without(),
    )

    assert state["ready"] is True
    assert state["checks"] == {
        "tables_present": True,
        "constraints_valid": True,
        "indexes_valid": True,
        "primary_keys_valid": True,
        "foreign_keys_valid": True,
    }
    assert set(state["tables"]) == TERRITORIAL_TABLES
    assert all(item["ready"] for item in state["tables"].values())


@pytest.mark.parametrize(
    ("drift", "check_name", "table_name", "table_check"),
    [
        (
            "constraint",
            "constraints_valid",
            "territorial_geocoding_job",
            "constraints_valid",
        ),
        (
            "index",
            "indexes_valid",
            "territorial_geocoding_sync_receipt",
            "indexes_valid",
        ),
        (
            "primary_key",
            "primary_keys_valid",
            "territorial_geocoding_job",
            "primary_key_valid",
        ),
        (
            "foreign_key",
            "foreign_keys_valid",
            "territorial_geocoding_review",
            "foreign_keys_valid",
        ),
    ],
)
def test_territorial_preflight_fails_closed_on_structural_drift(
    drift,
    check_name,
    table_name,
    table_check,
):
    state = _territorial_schema_state(
        _TerritorialSchemaConnection(drift),
        _counts_without(),
    )

    assert state["ready"] is False
    assert state["checks"][check_name] is False
    assert state["tables"][table_name][table_check] is False
    assert state["tables"][table_name]["ready"] is False


def test_territorial_preflight_fails_closed_when_a_table_is_missing():
    missing_table = "territorial_geocoding_attempt"

    state = _territorial_schema_state(
        _TerritorialSchemaConnection(),
        _counts_without(missing_table),
    )

    assert state["ready"] is False
    assert state["checks"]["tables_present"] is False
    assert state["tables"][missing_table] == {
        "present": False,
        "rows": None,
        "constraints_valid": False,
        "indexes_valid": False,
        "primary_key_valid": False,
        "foreign_keys_valid": False,
        "missing_constraints": sorted(
            TERRITORIAL_SCHEMA_REQUIREMENTS[missing_table]["constraints"]
        ),
        "missing_indexes": sorted(
            TERRITORIAL_SCHEMA_REQUIREMENTS[missing_table]["indexes"]
        ),
        "missing_foreign_keys": sorted(
            [
                "job_id->public.territorial_geocoding_job.id[CASCADE]",
                "tenant_id->public.tenant_profile.id[CASCADE]",
            ]
        ),
        "ready": False,
    }


def test_critical_schema_exposes_each_territorial_gate(monkeypatch):
    territorial_state = _territorial_schema_state(
        _TerritorialSchemaConnection(),
        _counts_without(),
    )
    monkeypatch.setattr(
        preflight,
        "_territorial_schema_state",
        lambda _connection, _counts: territorial_state,
    )

    class _NoSchemaQueryConnection:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("no legacy schema query was expected")

    state = preflight._critical_schema_state(
        _NoSchemaQueryConnection(),
        _counts_without(),
    )

    assert state["territorial_schema"] is territorial_state
    assert state["checks"]["territorial_tables_present"] is True
    assert state["checks"]["territorial_constraints_valid"] is True
    assert state["checks"]["territorial_indexes_valid"] is True
    assert state["checks"]["territorial_primary_keys_valid"] is True
    assert state["checks"]["territorial_foreign_keys_valid"] is True
