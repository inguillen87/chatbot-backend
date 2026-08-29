"""Final, read-only Render SQLite to Neon content-parity auditor.

The command opens an already-fenced SQLite snapshot with ``mode=ro`` and
``immutable=1``, forces ``PRAGMA query_only=ON``, and reads Neon inside one
repeatable-read/read-only transaction. It emits one canonical JSON object and
never serializes a DSN, filesystem path, row value, primary key, or secret.

Parity means source inclusion, not database equality: destination-only tables,
columns, and rows are allowed. Every non-excluded source table must exist in
Neon; every legacy primary key (or unkeyed row multiset) must be present; and
all policy-governed common cells must match. Any exception must be reviewed in
the checked-in, fingerprinted policy file.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import sys
import tomllib
import unicodedata
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Protocol, Sequence

from sqlalchemy import create_engine, inspect as sa_inspect, text
from sqlalchemy.engine import Connection, URL

try:  # Works both as ``python -m scripts...`` and ``python scripts/...``.
    from scripts.preflight_neon_cutover import (
        PreflightFailure,
        _validate_environment_variable_name,
        _validate_neon_direct_url,
        _validate_neon_identity_value,
        _wal_position,
    )
except ModuleNotFoundError:  # pragma: no cover - direct-script import mode
    from preflight_neon_cutover import (  # type: ignore[no-redef]
        PreflightFailure,
        _validate_environment_variable_name,
        _validate_neon_direct_url,
        _validate_neon_identity_value,
        _wal_position,
    )


CONTRACT_VERSION = "chatboc.render_neon_final_parity.v1"
DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "render_neon_parity_policy.v1.toml"
)
DEFAULT_DATABASE_ENVIRONMENT_VARIABLE = "MIGRATIONS_DATABASE_URL"
DEFAULT_PROJECT_ID_ENVIRONMENT_VARIABLE = "EXPECTED_NEON_PROJECT_ID"
DEFAULT_BRANCH_ID_ENVIRONMENT_VARIABLE = "EXPECTED_NEON_BRANCH_ID"
DEFAULT_FINGERPRINT_KEY_ENVIRONMENT_VARIABLE = "PARITY_AUDIT_HMAC_KEY"
DEFAULT_MAX_ROWS_PER_TABLE = 1_000_000
SAFE_EVIDENCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,127}$")
POLICY_KEYS = {"contract_version", "excluded_tables", "table_rules"}
TABLE_RULE_KEYS = {
    "destination_authoritative_columns",
    "source_only_null_columns",
}

EXIT_CERTIFIED = 0
EXIT_CONFIGURATION_OR_RUNTIME_BLOCKED = 2
EXIT_PARITY_MISMATCH = 3
EXIT_ATTESTATION_REQUIRED = 4


class ParityAuditFailure(RuntimeError):
    """Stable, redacted failure reason safe to serialize."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    type_name: str
    pk_ordinal: int = 0


class SnapshotReader(Protocol):
    provider: str

    def table_names(self) -> list[str]: ...

    def columns(self, table_name: str) -> list[ColumnSpec]: ...

    def count(self, table_name: str) -> int: ...

    def rows(
        self,
        table_name: str,
        column_names: Sequence[str],
    ) -> Iterator[tuple[Any, ...]]: ...


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _aggregate_hmac(key: bytes, domain: str, items: Iterable[bytes]) -> str:
    digest = hmac.new(key, digestmod=hashlib.sha256)
    domain_bytes = domain.encode("utf-8")
    digest.update(len(domain_bytes).to_bytes(4, "big"))
    digest.update(domain_bytes)
    for item in sorted(items):
        digest.update(len(item).to_bytes(8, "big"))
        digest.update(item)
    return digest.hexdigest()


def _validate_evidence_id(value: str, *, reason_code: str) -> str:
    normalized = str(value or "").strip()
    if not SAFE_EVIDENCE_ID_PATTERN.fullmatch(normalized):
        raise ParityAuditFailure(reason_code)
    return normalized


def _validate_identifier(value: str, *, reason_code: str) -> str:
    normalized = str(value or "").strip()
    if not SAFE_IDENTIFIER_PATTERN.fullmatch(normalized):
        raise ParityAuditFailure(reason_code)
    return normalized


def _translate_preflight_failure(callback, *args, **kwargs):
    try:
        return callback(*args, **kwargs)
    except PreflightFailure as exc:
        raise ParityAuditFailure(exc.reason_code) from exc


def _load_policy(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        parsed = tomllib.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ParityAuditFailure("parity_policy_unreadable") from exc
    if not isinstance(parsed, dict) or set(parsed) != POLICY_KEYS:
        raise ParityAuditFailure("parity_policy_schema_invalid")
    if parsed.get("contract_version") != "chatboc.render_neon_parity_policy.v1":
        raise ParityAuditFailure("parity_policy_contract_invalid")

    excluded_tables = parsed.get("excluded_tables")
    table_rules = parsed.get("table_rules")
    if not isinstance(excluded_tables, dict) or not isinstance(table_rules, dict):
        raise ParityAuditFailure("parity_policy_schema_invalid")

    for table_name, rationale in excluded_tables.items():
        _validate_identifier(table_name, reason_code="parity_policy_identifier_invalid")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ParityAuditFailure("parity_policy_rationale_missing")

    for table_name, rule in table_rules.items():
        _validate_identifier(table_name, reason_code="parity_policy_identifier_invalid")
        if not isinstance(rule, dict) or not set(rule).issubset(TABLE_RULE_KEYS):
            raise ParityAuditFailure("parity_policy_schema_invalid")
        for key in TABLE_RULE_KEYS:
            columns = rule.get(key, {})
            if not isinstance(columns, dict):
                raise ParityAuditFailure("parity_policy_schema_invalid")
            for column_name, rationale in columns.items():
                _validate_identifier(
                    column_name,
                    reason_code="parity_policy_identifier_invalid",
                )
                if not isinstance(rationale, str) or not rationale.strip():
                    raise ParityAuditFailure("parity_policy_rationale_missing")

    return {
        "document": parsed,
        # Hash the semantic document, not checkout-specific CRLF/LF bytes.
        "sha256": _canonical_sha256(parsed),
    }


def _policy_summary(policy: Mapping[str, Any]) -> dict[str, Any]:
    document = policy["document"]
    rules: dict[str, Any] = {}
    for table_name, rule in sorted(document["table_rules"].items()):
        rules[table_name] = {
            "destination_authoritative_columns": sorted(
                rule.get("destination_authoritative_columns", {})
            ),
            "source_only_null_columns": sorted(
                rule.get("source_only_null_columns", {})
            ),
        }
    return {
        "contract_version": document["contract_version"],
        "sha256": policy["sha256"],
        "excluded_tables": sorted(document["excluded_tables"]),
        "table_rules": rules,
    }


def _fingerprint_key(raw_key: str) -> bytes:
    encoded = str(raw_key or "").encode("utf-8")
    if len(encoded) < 32:
        raise ParityAuditFailure("fingerprint_hmac_key_missing_or_too_short")
    return encoded


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_file_state(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise ParityAuditFailure("source_snapshot_unreadable") from exc
    if not path.is_file() or stat.st_size < 100:
        raise ParityAuditFailure("source_snapshot_invalid")
    for suffix in ("-wal", "-journal"):
        if Path(f"{path}{suffix}").exists():
            raise ParityAuditFailure("source_snapshot_sidecar_present")
    return {
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "sha256": _file_sha256(path),
    }


def _assert_file_stable(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    if dict(before) != dict(after):
        raise ParityAuditFailure("source_snapshot_changed_during_audit")


def _quote_sqlite_identifier(value: str) -> str:
    _validate_identifier(value, reason_code="database_identifier_invalid")
    return '"' + value.replace('"', '""') + '"'


class SqliteSnapshotReader:
    provider = "render_sqlite_snapshot"

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def table_names(self) -> list[str]:
        return [
            str(row[0])
            for row in self.connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        ]

    def columns(self, table_name: str) -> list[ColumnSpec]:
        quoted = _quote_sqlite_identifier(table_name)
        return [
            ColumnSpec(str(row[1]), str(row[2] or ""), int(row[5] or 0))
            for row in self.connection.execute(f"PRAGMA table_info({quoted})")
        ]

    def count(self, table_name: str) -> int:
        quoted = _quote_sqlite_identifier(table_name)
        return int(self.connection.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0])

    def rows(
        self,
        table_name: str,
        column_names: Sequence[str],
    ) -> Iterator[tuple[Any, ...]]:
        table = _quote_sqlite_identifier(table_name)
        columns = ",".join(_quote_sqlite_identifier(item) for item in column_names)
        yield from self.connection.execute(f"SELECT {columns} FROM {table}")


class PostgresSnapshotReader:
    provider = "neon"

    def __init__(self, connection: Connection):
        self.connection = connection
        self.inspector = sa_inspect(connection)
        self.preparer = connection.dialect.identifier_preparer

    def _table(self, table_name: str) -> str:
        _validate_identifier(table_name, reason_code="database_identifier_invalid")
        quote = self.preparer.quote_identifier
        return f"{quote('public')}.{quote(table_name)}"

    def _column(self, column_name: str) -> str:
        _validate_identifier(column_name, reason_code="database_identifier_invalid")
        return self.preparer.quote_identifier(column_name)

    def table_names(self) -> list[str]:
        return sorted(self.inspector.get_table_names(schema="public"))

    def columns(self, table_name: str) -> list[ColumnSpec]:
        pk = self.inspector.get_pk_constraint(table_name, schema="public")
        constrained = list(pk.get("constrained_columns") or [])
        pk_order = {name: index + 1 for index, name in enumerate(constrained)}
        return [
            ColumnSpec(
                str(item["name"]),
                str(item.get("type") or ""),
                pk_order.get(str(item["name"]), 0),
            )
            for item in self.inspector.get_columns(table_name, schema="public")
        ]

    def count(self, table_name: str) -> int:
        return int(
            self.connection.execute(
                text(f"SELECT count(*) FROM {self._table(table_name)}")
            ).scalar_one()
        )

    def rows(
        self,
        table_name: str,
        column_names: Sequence[str],
    ) -> Iterator[tuple[Any, ...]]:
        columns = ",".join(self._column(item) for item in column_names)
        result = self.connection.execute(
            text(f"SELECT {columns} FROM {self._table(table_name)}")
        )
        for row in result:
            yield tuple(row)


@contextmanager
def _open_sqlite_snapshot(path: Path):
    resolved = path.resolve(strict=True)
    uri = f"{resolved.as_uri()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        if query_only != 1:
            raise ParityAuditFailure("source_database_not_query_only")
        yield connection
    finally:
        connection.close()


def _semantic_kind(source_type: str, destination_type: str) -> str:
    joined = f"{source_type} {destination_type}".upper()
    if "JSON" in joined:
        return "json"
    if "BOOL" in joined:
        return "boolean"
    if "TIMESTAMP" in joined or "DATETIME" in joined:
        return "datetime"
    if re.search(r"\bDATE\b", joined):
        return "date"
    if re.search(r"\bTIME\b", joined):
        return "time"
    if any(
        token in joined
        for token in ("INT", "NUMERIC", "DECIMAL", "REAL", "FLOAT", "DOUBLE")
    ):
        return "number"
    if any(token in joined for token in ("BYTEA", "BLOB", "BINARY")):
        return "bytes"
    return "text"


def _normalize_decimal(value: Any) -> str:
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "infinity" if value > 0 else "-infinity"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ParityAuditFailure("cell_number_normalization_failed") from exc
    if number == 0:
        return "0"
    normalized = format(number.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def _normalize_datetime(value: Any, *, kind: str) -> str:
    parsed: date | datetime | time
    if isinstance(value, (datetime, date, time)):
        parsed = value
    else:
        raw = str(value).strip()
        try:
            if kind == "datetime":
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            elif kind == "date":
                parsed = date.fromisoformat(raw)
            else:
                parsed = time.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ParityAuditFailure("cell_datetime_normalization_failed") from exc

    if isinstance(parsed, datetime):
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(parsed, time):
        if parsed.tzinfo is not None:
            anchor = datetime.combine(date(2000, 1, 1), parsed)
            parsed = anchor.astimezone(timezone.utc).timetz()
        return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return parsed.isoformat()


def _json_compatible(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return {"$number": _normalize_decimal(value)}
    if isinstance(value, Decimal):
        return {"$number": _normalize_decimal(value)}
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (date, datetime, time)):
        kind = (
            "datetime"
            if isinstance(value, datetime)
            else "time"
            if isinstance(value, time)
            else "date"
        )
        return {"$datetime": _normalize_datetime(value, kind=kind)}
    if isinstance(value, Mapping):
        return {
            unicodedata.normalize("NFC", str(key)): _json_compatible(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    return unicodedata.normalize("NFC", str(value))


def _canonical_cell(value: Any, *, semantic_kind: str) -> bytes:
    if value is None:
        normalized: Any = {"type": "null"}
    elif semantic_kind == "boolean":
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered not in {"0", "1", "false", "true"}:
                raise ParityAuditFailure("cell_boolean_normalization_failed")
            boolean_value = lowered in {"1", "true"}
        else:
            boolean_value = bool(value)
        normalized = {"type": "boolean", "value": boolean_value}
    elif semantic_kind == "number":
        normalized = {"type": "number", "value": _normalize_decimal(value)}
    elif semantic_kind in {"datetime", "date", "time"}:
        normalized = {
            "type": semantic_kind,
            "value": _normalize_datetime(value, kind=semantic_kind),
        }
    elif semantic_kind == "json":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ParityAuditFailure("cell_json_normalization_failed") from exc
        normalized = {"type": "json", "value": _json_compatible(value)}
    elif semantic_kind == "bytes":
        raw = value if isinstance(value, bytes) else bytes(value)
        normalized = {
            "type": "bytes",
            "value": base64.b64encode(raw).decode("ascii"),
        }
    else:
        normalized = {
            "type": "text",
            "value": unicodedata.normalize("NFC", str(value)),
        }
    return _canonical_json_bytes(normalized)


def _column_map(columns: Sequence[ColumnSpec]) -> dict[str, ColumnSpec]:
    return {item.name: item for item in columns}


def _pk_columns(columns: Sequence[ColumnSpec]) -> list[str]:
    return [
        item.name
        for item in sorted(columns, key=lambda column: column.pk_ordinal or 1_000_000)
        if item.pk_ordinal
    ]


def _key_token(
    row: Sequence[Any],
    *,
    table_name: str,
    pk_columns: Sequence[str],
    column_kinds: Mapping[str, str],
) -> bytes:
    return _canonical_json_bytes(
        {
            "table": table_name,
            "primary_key": [
                json.loads(
                    _canonical_cell(value, semantic_kind=column_kinds[column]).decode(
                        "utf-8"
                    )
                )
                for column, value in zip(pk_columns, row)
            ],
        }
    )


def _cell_token(
    *,
    table_name: str,
    key_token: bytes,
    column_name: str,
    value: Any,
    semantic_kind: str,
) -> bytes:
    return _canonical_json_bytes(
        {
            "table": table_name,
            "key_sha256": hashlib.sha256(key_token).hexdigest(),
            "column": column_name,
            "value": json.loads(
                _canonical_cell(value, semantic_kind=semantic_kind).decode("utf-8")
            ),
        }
    )


def _row_token(
    *,
    table_name: str,
    columns: Sequence[str],
    row: Sequence[Any],
    column_kinds: Mapping[str, str],
) -> bytes:
    return _canonical_json_bytes(
        {
            "table": table_name,
            "row": {
                column: json.loads(
                    _canonical_cell(value, semantic_kind=column_kinds[column]).decode(
                        "utf-8"
                    )
                )
                for column, value in zip(columns, row)
            },
        }
    )


def _assert_no_duplicate_keys(tokens: Sequence[bytes]) -> None:
    if len(tokens) != len(set(tokens)):
        raise ParityAuditFailure("primary_key_normalization_collision")


def _table_rule(policy_document: Mapping[str, Any], table_name: str) -> Mapping[str, Any]:
    return policy_document.get("table_rules", {}).get(table_name, {})


def _audit_table(
    *,
    source: SnapshotReader,
    destination: SnapshotReader,
    table_name: str,
    source_count: int,
    destination_count: int,
    source_columns: Sequence[ColumnSpec],
    destination_columns: Sequence[ColumnSpec],
    policy_document: Mapping[str, Any],
    hmac_key: bytes,
) -> dict[str, Any]:
    source_map = _column_map(source_columns)
    destination_map = _column_map(destination_columns)
    rule = _table_rule(policy_document, table_name)
    destination_authoritative = set(
        rule.get("destination_authoritative_columns", {})
    )
    allowed_null_source_only = set(rule.get("source_only_null_columns", {}))
    source_only = sorted(set(source_map) - set(destination_map))
    unexpected_source_only = sorted(set(source_only) - allowed_null_source_only)
    allowed_null_checks: dict[str, bool] = {}
    for column_name in sorted(set(source_only) & allowed_null_source_only):
        allowed_null_checks[column_name] = all(
            row[0] is None for row in source.rows(table_name, [column_name])
        )

    common = sorted(set(source_map) & set(destination_map))
    invalid_exclusions = sorted(destination_authoritative - set(common))
    compared_columns = sorted(set(common) - destination_authoritative)
    source_pk = _pk_columns(source_columns)
    destination_pk = _pk_columns(destination_columns)
    pk_contract_match = source_pk == destination_pk
    column_kinds = {
        column: _semantic_kind(
            source_map[column].type_name,
            destination_map[column].type_name,
        )
        for column in common
    }

    table: dict[str, Any] = {
        "name": table_name,
        "rows": {
            "source": source_count,
            "destination": destination_count,
            "destination_includes_source_count": destination_count >= source_count,
        },
        "schema": {
            "source_column_count": len(source_columns),
            "destination_column_count": len(destination_columns),
            "common_column_count": len(common),
            "compared_columns": compared_columns,
            "destination_authoritative_columns": sorted(destination_authoritative),
            "source_only_columns": source_only,
            "unexpected_source_only_columns": unexpected_source_only,
            "allowed_source_only_null_checks": allowed_null_checks,
            "invalid_policy_exclusions": invalid_exclusions,
        },
        "primary_key": {
            "columns": source_pk,
            "destination_contract_match": pk_contract_match,
            "source_count": source_count,
            "matched_count": 0,
            "missing_count": source_count,
            "source_fingerprint_hmac_sha256": None,
            "matched_fingerprint_hmac_sha256": None,
        },
        "cells": {
            "checked_count": 0,
            "matched_count": 0,
            "mismatch_count": 0,
            "mismatch_count_by_column": {},
            "source_fingerprint_hmac_sha256": None,
            "destination_fingerprint_hmac_sha256": None,
        },
    }

    schema_ready = (
        not unexpected_source_only
        and all(allowed_null_checks.values())
        and not invalid_exclusions
        and bool(common)
        and pk_contract_match
    )
    if not schema_ready:
        table["parity"] = False
        table["comparison_mode"] = "schema_blocked"
        return table

    if source_pk:
        selected = source_pk + [item for item in compared_columns if item not in source_pk]
        source_rows = list(source.rows(table_name, selected))
        destination_rows = list(destination.rows(table_name, selected))
        key_width = len(source_pk)
        source_keys = [
            _key_token(
                row[:key_width],
                table_name=table_name,
                pk_columns=source_pk,
                column_kinds=column_kinds,
            )
            for row in source_rows
        ]
        destination_keys = [
            _key_token(
                row[:key_width],
                table_name=table_name,
                pk_columns=source_pk,
                column_kinds=column_kinds,
            )
            for row in destination_rows
        ]
        _assert_no_duplicate_keys(source_keys)
        _assert_no_duplicate_keys(destination_keys)
        destination_by_key = dict(zip(destination_keys, destination_rows))
        matched_keys = [key for key in source_keys if key in destination_by_key]

        source_cell_tokens: list[bytes] = []
        destination_cell_tokens: list[bytes] = []
        mismatch_by_column: Counter[str] = Counter()
        checked_count = 0
        matched_count = 0
        for source_row, key in zip(source_rows, source_keys):
            destination_row = destination_by_key.get(key)
            for index, column_name in enumerate(selected[key_width:], start=key_width):
                source_token = _cell_token(
                    table_name=table_name,
                    key_token=key,
                    column_name=column_name,
                    value=source_row[index],
                    semantic_kind=column_kinds[column_name],
                )
                source_cell_tokens.append(source_token)
                checked_count += 1
                if destination_row is None:
                    mismatch_by_column[column_name] += 1
                    continue
                destination_token = _cell_token(
                    table_name=table_name,
                    key_token=key,
                    column_name=column_name,
                    value=destination_row[index],
                    semantic_kind=column_kinds[column_name],
                )
                destination_cell_tokens.append(destination_token)
                if hmac.compare_digest(source_token, destination_token):
                    matched_count += 1
                else:
                    mismatch_by_column[column_name] += 1

        table["comparison_mode"] = "primary_key_inclusion"
        table["primary_key"].update(
            {
                "matched_count": len(matched_keys),
                "missing_count": len(source_keys) - len(matched_keys),
                "source_fingerprint_hmac_sha256": _aggregate_hmac(
                    hmac_key, f"{table_name}:source-primary-keys", source_keys
                ),
                "matched_fingerprint_hmac_sha256": _aggregate_hmac(
                    hmac_key, f"{table_name}:source-primary-keys", matched_keys
                ),
            }
        )
        table["cells"].update(
            {
                "checked_count": checked_count,
                "matched_count": matched_count,
                "mismatch_count": checked_count - matched_count,
                "mismatch_count_by_column": dict(sorted(mismatch_by_column.items())),
                "source_fingerprint_hmac_sha256": _aggregate_hmac(
                    hmac_key, f"{table_name}:source-cells", source_cell_tokens
                ),
                "destination_fingerprint_hmac_sha256": _aggregate_hmac(
                    hmac_key, f"{table_name}:source-cells", destination_cell_tokens
                ),
            }
        )
        table["parity"] = (
            destination_count >= source_count
            and table["primary_key"]["missing_count"] == 0
            and table["cells"]["mismatch_count"] == 0
        )
        return table

    source_rows = list(source.rows(table_name, compared_columns))
    destination_rows = list(destination.rows(table_name, compared_columns))
    source_tokens = [
        _row_token(
            table_name=table_name,
            columns=compared_columns,
            row=row,
            column_kinds=column_kinds,
        )
        for row in source_rows
    ]
    destination_tokens = [
        _row_token(
            table_name=table_name,
            columns=compared_columns,
            row=row,
            column_kinds=column_kinds,
        )
        for row in destination_rows
    ]
    source_multiset = Counter(source_tokens)
    destination_multiset = Counter(destination_tokens)
    missing_rows = sum(
        max(0, count - destination_multiset.get(token, 0))
        for token, count in source_multiset.items()
    )
    matched_tokens: list[bytes] = []
    for token, count in source_multiset.items():
        matched_tokens.extend([token] * min(count, destination_multiset.get(token, 0)))
    table["comparison_mode"] = "unkeyed_row_multiset_inclusion"
    table["primary_key"].update(
        {
            "source_count": source_count,
            "matched_count": source_count - missing_rows,
            "missing_count": missing_rows,
            "source_fingerprint_hmac_sha256": _aggregate_hmac(
                hmac_key, f"{table_name}:source-rows", source_tokens
            ),
            "matched_fingerprint_hmac_sha256": _aggregate_hmac(
                hmac_key, f"{table_name}:source-rows", matched_tokens
            ),
        }
    )
    checked_count = source_count * len(compared_columns)
    matched_count = (source_count - missing_rows) * len(compared_columns)
    table["cells"].update(
        {
            "checked_count": checked_count,
            "matched_count": matched_count,
            "mismatch_count": checked_count - matched_count,
            "source_fingerprint_hmac_sha256": _aggregate_hmac(
                hmac_key, f"{table_name}:source-rows", source_tokens
            ),
            "destination_fingerprint_hmac_sha256": _aggregate_hmac(
                hmac_key, f"{table_name}:source-rows", matched_tokens
            ),
        }
    )
    table["parity"] = destination_count >= source_count and missing_rows == 0
    return table


def compare_snapshots(
    *,
    source: SnapshotReader,
    destination: SnapshotReader,
    policy: Mapping[str, Any],
    hmac_key: bytes,
    max_rows_per_table: int,
) -> dict[str, Any]:
    if max_rows_per_table < 1:
        raise ParityAuditFailure("max_rows_per_table_invalid")
    source_tables = source.table_names()
    destination_tables = destination.table_names()
    excluded = set(policy["document"]["excluded_tables"])
    audited_tables = sorted(set(source_tables) - excluded)
    missing_tables = sorted(set(audited_tables) - set(destination_tables))
    source_counts = {table: source.count(table) for table in source_tables}
    destination_counts = {table: destination.count(table) for table in destination_tables}

    oversized = sorted(
        table
        for table in audited_tables
        if source_counts[table] > max_rows_per_table
        or destination_counts.get(table, 0) > max_rows_per_table
    )
    if oversized:
        raise ParityAuditFailure("table_row_limit_exceeded")

    table_evidence: list[dict[str, Any]] = []
    for table_name in audited_tables:
        if table_name in missing_tables:
            table_evidence.append(
                {
                    "name": table_name,
                    "parity": False,
                    "comparison_mode": "destination_table_missing",
                    "rows": {
                        "source": source_counts[table_name],
                        "destination": None,
                        "destination_includes_source_count": False,
                    },
                }
            )
            continue
        table_evidence.append(
            _audit_table(
                source=source,
                destination=destination,
                table_name=table_name,
                source_count=source_counts[table_name],
                destination_count=destination_counts[table_name],
                source_columns=source.columns(table_name),
                destination_columns=destination.columns(table_name),
                policy_document=policy["document"],
                hmac_key=hmac_key,
            )
        )

    checked_cells = sum(
        int((item.get("cells") or {}).get("checked_count") or 0)
        for item in table_evidence
    )
    mismatched_cells = sum(
        int((item.get("cells") or {}).get("mismatch_count") or 0)
        for item in table_evidence
    )
    missing_keys = sum(
        int((item.get("primary_key") or {}).get("missing_count") or 0)
        for item in table_evidence
    )
    parity = not missing_tables and all(item["parity"] for item in table_evidence)
    return {
        "parity": parity,
        "inventory": {
            "source": {
                "provider": source.provider,
                "table_count": len(source_tables),
                "row_count": sum(source_counts.values()),
                "table_names": source_tables,
                "row_count_inventory_sha256": _canonical_sha256(source_counts),
            },
            "destination": {
                "provider": destination.provider,
                "table_count": len(destination_tables),
                "row_count": sum(destination_counts.values()),
                "table_names": destination_tables,
                "row_count_inventory_sha256": _canonical_sha256(destination_counts),
            },
            "audited_source_table_count": len(audited_tables),
            "missing_destination_tables": missing_tables,
            "excluded_source_tables_present": sorted(set(source_tables) & excluded),
        },
        "summary": {
            "tables_checked": len(table_evidence),
            "tables_matching": sum(bool(item["parity"]) for item in table_evidence),
            "source_primary_keys_or_rows_missing": missing_keys,
            "cells_checked": checked_cells,
            "cells_mismatched": mismatched_cells,
        },
        "tables": table_evidence,
    }


def _destination_state(
    connection: Connection,
    *,
    expected_project_id: str,
    expected_branch_id: str,
) -> dict[str, Any]:
    connection.execute(
        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    )
    connection.execute(text("SET LOCAL statement_timeout = '120s'"))
    connection.execute(text("SET LOCAL lock_timeout = '2s'"))
    connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
    read_only = str(
        connection.execute(text("SHOW transaction_read_only")).scalar_one()
    ).lower() == "on"
    isolation = str(
        connection.execute(text("SHOW transaction_isolation")).scalar_one()
    ).lower()
    if not read_only or isolation != "repeatable read":
        raise ParityAuditFailure("destination_transaction_not_read_only_snapshot")
    identity = connection.execute(
        text(
            """
            SELECT
                current_setting('neon.project_id', true) AS project_id,
                current_setting('neon.branch_id', true) AS branch_id
            """
        )
    ).mappings().one()
    project_id = str(identity["project_id"] or "").strip().lower()
    branch_id = str(identity["branch_id"] or "").strip().lower()
    if project_id != expected_project_id:
        raise ParityAuditFailure("database_neon_project_mismatch")
    if branch_id != expected_branch_id:
        raise ParityAuditFailure("database_neon_branch_mismatch")
    return {
        "provider": "neon",
        "project_id": project_id,
        "branch_id": branch_id,
        "transaction_read_only": read_only,
        "transaction_isolation": isolation,
        **_translate_preflight_failure(_wal_position, connection),
    }


def _status_and_exit(*, parity: bool, writers_fenced: bool) -> tuple[str, int]:
    if not parity:
        return "parity_mismatch", EXIT_PARITY_MISMATCH
    if not writers_fenced:
        return "writer_fence_attestation_required", EXIT_ATTESTATION_REQUIRED
    return "certified", EXIT_CERTIFIED


def run_audit(
    *,
    source_path: Path,
    source_snapshot_id: str,
    writers_fenced: bool,
    writer_fence_evidence_id: str | None,
    database_url: str,
    expected_project_id: str,
    expected_branch_id: str,
    fingerprint_key: bytes,
    fingerprint_key_id: str,
    policy_path: Path,
    max_rows_per_table: int = DEFAULT_MAX_ROWS_PER_TABLE,
) -> tuple[dict[str, Any], int]:
    source_snapshot_id = _validate_evidence_id(
        source_snapshot_id,
        reason_code="source_snapshot_id_invalid",
    )
    fingerprint_key_id = _validate_evidence_id(
        fingerprint_key_id,
        reason_code="fingerprint_key_id_invalid",
    )
    if writers_fenced:
        writer_fence_evidence_id = _validate_evidence_id(
            writer_fence_evidence_id or "",
            reason_code="writer_fence_evidence_id_invalid",
        )
    elif writer_fence_evidence_id:
        raise ParityAuditFailure("writer_fence_attestation_inconsistent")

    parsed: URL = _translate_preflight_failure(_validate_neon_direct_url, database_url)
    expected_project_id = _translate_preflight_failure(
        _validate_neon_identity_value,
        expected_project_id,
        kind="project",
    )
    expected_branch_id = _translate_preflight_failure(
        _validate_neon_identity_value,
        expected_branch_id,
        kind="branch",
    )
    policy = _load_policy(policy_path)
    source_before = _sqlite_file_state(source_path)
    host = str(parsed.host or "").lower().rstrip(".")
    engine = create_engine(
        parsed.set(drivername="postgresql+psycopg"),
        pool_pre_ping=True,
        pool_recycle=300,
    )
    try:
        with _open_sqlite_snapshot(source_path) as sqlite_connection:
            source = SqliteSnapshotReader(sqlite_connection)
            with engine.connect() as connection:
                with connection.begin():
                    destination_state = _destination_state(
                        connection,
                        expected_project_id=expected_project_id,
                        expected_branch_id=expected_branch_id,
                    )
                    comparison = compare_snapshots(
                        source=source,
                        destination=PostgresSnapshotReader(connection),
                        policy=policy,
                        hmac_key=fingerprint_key,
                        max_rows_per_table=max_rows_per_table,
                    )
    finally:
        engine.dispose()
    source_after = _sqlite_file_state(source_path)
    _assert_file_stable(source_before, source_after)

    status, exit_code = _status_and_exit(
        parity=bool(comparison["parity"]),
        writers_fenced=writers_fenced,
    )
    evidence: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "status": status,
        "certified": status == "certified",
        "scope": "render_sqlite_source_inclusion_in_neon",
        "read_only_guarantees": {
            "source_mode": "sqlite_uri_mode_ro_immutable_query_only",
            "source_sidecars_absent": True,
            "source_stable_during_audit": True,
            "destination_transaction_read_only": True,
            "destination_transaction_isolation": "repeatable read",
        },
        "attestations": {
            "writers_fenced": writers_fenced,
            "writer_fence_evidence_id": writer_fence_evidence_id,
            "source_snapshot_id": source_snapshot_id,
        },
        "source_snapshot": {
            "path_redacted": True,
            "size_bytes": source_before["size_bytes"],
            "sha256": source_before["sha256"],
        },
        "destination": {
            **destination_state,
            "connection_mode": "direct",
            "tls_required": True,
            "host_fingerprint_sha256": hashlib.sha256(host.encode("utf-8")).hexdigest(),
        },
        "fingerprints": {
            "algorithm": "hmac-sha256",
            "key_id": fingerprint_key_id,
            "key_serialized": False,
            "normalization_contract": "chatboc.cross_database_cells.v1",
        },
        "policy": _policy_summary(policy),
        "limits": {"max_rows_per_table": max_rows_per_table},
        "comparison": comparison,
        "signing": {
            "canonical_json": "utf8-json-sort-keys-compact-ascii",
            "externally_signed": False,
        },
    }
    evidence["evidence_sha256"] = _canonical_sha256(evidence)
    return evidence, exit_code


def _failure_payload(reason_code: str, *, error_type: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "status": "blocked",
        "certified": False,
        "reason_code": reason_code,
    }
    if error_type:
        payload["error_type"] = error_type
    payload["evidence_sha256"] = _canonical_sha256(payload)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sqlite", required=True)
    parser.add_argument("--source-snapshot-id", required=True)
    parser.add_argument("--writers-fenced", action="store_true")
    parser.add_argument("--writer-fence-evidence-id")
    parser.add_argument("--fingerprint-key-id", required=True)
    parser.add_argument("--policy", default=str(DEFAULT_POLICY_PATH))
    parser.add_argument(
        "--database-environment-variable",
        default=DEFAULT_DATABASE_ENVIRONMENT_VARIABLE,
    )
    parser.add_argument(
        "--project-id-environment-variable",
        default=DEFAULT_PROJECT_ID_ENVIRONMENT_VARIABLE,
    )
    parser.add_argument(
        "--branch-id-environment-variable",
        default=DEFAULT_BRANCH_ID_ENVIRONMENT_VARIABLE,
    )
    parser.add_argument(
        "--fingerprint-key-environment-variable",
        default=DEFAULT_FINGERPRINT_KEY_ENVIRONMENT_VARIABLE,
    )
    parser.add_argument(
        "--max-rows-per-table",
        type=int,
        default=DEFAULT_MAX_ROWS_PER_TABLE,
    )
    args = parser.parse_args(argv)

    try:
        database_env = _translate_preflight_failure(
            _validate_environment_variable_name,
            args.database_environment_variable,
        )
        project_env = _translate_preflight_failure(
            _validate_environment_variable_name,
            args.project_id_environment_variable,
        )
        branch_env = _translate_preflight_failure(
            _validate_environment_variable_name,
            args.branch_id_environment_variable,
        )
        fingerprint_env = _translate_preflight_failure(
            _validate_environment_variable_name,
            args.fingerprint_key_environment_variable,
        )
        database_url = os.environ.get(database_env, "")
        expected_project_id = os.environ.get(project_env, "")
        expected_branch_id = os.environ.get(branch_env, "")
        raw_fingerprint_key = os.environ.get(fingerprint_env, "")
        if not database_url:
            raise ParityAuditFailure("database_environment_variable_missing")
        if not expected_project_id:
            raise ParityAuditFailure("expected_neon_project_id_missing")
        if not expected_branch_id:
            raise ParityAuditFailure("expected_neon_branch_id_missing")
        fingerprint_key = _fingerprint_key(raw_fingerprint_key)
        payload, exit_code = run_audit(
            source_path=Path(args.source_sqlite),
            source_snapshot_id=args.source_snapshot_id,
            writers_fenced=bool(args.writers_fenced),
            writer_fence_evidence_id=args.writer_fence_evidence_id,
            database_url=database_url,
            expected_project_id=expected_project_id,
            expected_branch_id=expected_branch_id,
            fingerprint_key=fingerprint_key,
            fingerprint_key_id=args.fingerprint_key_id,
            policy_path=Path(args.policy),
            max_rows_per_table=args.max_rows_per_table,
        )
    except ParityAuditFailure as exc:
        payload = _failure_payload(exc.reason_code)
        exit_code = EXIT_CONFIGURATION_OR_RUNTIME_BLOCKED
    except Exception as exc:  # Never serialize connection/provider details.
        payload = _failure_payload(
            "parity_audit_failed",
            error_type=type(exc).__name__,
        )
        exit_code = EXIT_CONFIGURATION_OR_RUNTIME_BLOCKED

    print(_canonical_json_bytes(payload).decode("utf-8"))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
