#!/usr/bin/env python3
"""Build a signed Render-to-R2 approval map without touching either system.

The builder consumes the signed private inventory, its operator review and a
read-only, basename-hash-only Neon evidence snapshot.  Clear source paths are
used only in memory.  The output map contains opaque path identifiers and R2
keys; the companion audit contains aggregate counts and cryptographic digests.

This command never connects to a database, Render, or R2.  It refuses to
overwrite any output and emits only log-safe aggregate JSON on stdout.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from collections.abc import Mapping, Sequence
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Any

try:
    from scripts import inventory_persistent_data as inventory_tools
    from scripts import migrate_render_uploads_to_r2 as migration_tools
except ImportError:  # pragma: no cover - direct script execution
    import inventory_persistent_data as inventory_tools
    import migrate_render_uploads_to_r2 as migration_tools


EVIDENCE_CONTRACT_VERSION = "storage.r2.neon-reference-evidence.v1"
AUDIT_CONTRACT_VERSION = "storage.r2.approved-reference-map.audit.v1"
AUDIT_INTEGRITY_ALGORITHM = "HMAC-SHA256"
MAX_PRIVATE_INPUT_BYTES = 16 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TENANT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
REVISION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
LOG_ERROR_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
THUMB_SUFFIX = "_thumb.webp"
CRYPTPROTECT_UI_FORBIDDEN = 0x1
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class ApprovalBuildError(RuntimeError):
    """A stable, path-free failure code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RepairPolicy:
    revision: str
    attachment_counts: dict[int, int]
    source_slug: str
    source_type: str
    target_slug: str
    target_type: str
    file_sha256: str


@dataclass(frozen=True)
class TenantAnchor:
    slug: str
    tenant_id: int
    tenant_type: str
    owner_kind: str
    owner_id: int


@dataclass(frozen=True)
class EvidenceRow:
    reference_hash: str
    attachment_id: int
    actor_explicit_tenant_slug: str | None
    actor_structural_tenant_slugs: tuple[str, ...]
    session_tenant_slug: str | None
    municipio_ticket_id: int | None
    municipio_ticket_tenant_slug: str | None
    municipio_ticket_tenant_type: str | None
    pyme_ticket_id: int | None
    pyme_ticket_tenant_slug: str | None
    pyme_ticket_tenant_type: str | None


@dataclass(frozen=True)
class Decision:
    tenant_slug: str | None
    approval_status: str
    reason_code: str


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _strict_json_loads(payload: str, *, code: str) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ApprovalBuildError(code)
            result[key] = value
        return result

    try:
        return json.loads(payload, object_pairs_hook=reject_duplicates)
    except ApprovalBuildError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ApprovalBuildError(code) from exc


def _read_private_bytes(path: Path, *, code: str) -> bytes:
    try:
        metadata = path.lstat()
        if not path.is_file() or metadata.st_size > MAX_PRIVATE_INPUT_BYTES:
            raise ApprovalBuildError(code)
        if getattr(metadata, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
            raise ApprovalBuildError(code)
        with path.open("rb") as handle:
            payload = handle.read(MAX_PRIVATE_INPUT_BYTES + 1)
        if len(payload) > MAX_PRIVATE_INPUT_BYTES:
            raise ApprovalBuildError(code)
        after = path.lstat()
        if (
            metadata.st_size != after.st_size
            or metadata.st_mtime_ns != after.st_mtime_ns
            or getattr(after, "st_file_attributes", 0)
            & FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ApprovalBuildError(code)
        return payload
    except ApprovalBuildError:
        raise
    except OSError as exc:
        raise ApprovalBuildError(code) from exc


def _read_private_json(path: Path, *, code: str) -> dict[str, Any]:
    payload = _read_private_bytes(path, code=code)
    try:
        value = _strict_json_loads(payload.decode("utf-8"), code=code)
    except UnicodeError as exc:
        raise ApprovalBuildError(code) from exc
    if not isinstance(value, dict):
        raise ApprovalBuildError(code)
    return value


def _dpapi_unprotect(blob: bytes, *, entropy: bytes) -> bytes:
    if os.name != "nt":
        raise ApprovalBuildError("dpapi_windows_required")
    if not blob:
        raise ApprovalBuildError("dpapi_input_invalid")
    crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    input_buffer = ctypes.create_string_buffer(blob, len(blob))
    input_blob = _DataBlob(
        len(blob), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    entropy_buffer = ctypes.create_string_buffer(entropy, len(entropy))
    entropy_blob = _DataBlob(
        len(entropy),
        ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    output_blob = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    if not ok:
        raise ApprovalBuildError("dpapi_unprotect_failed")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _dpapi_protect(payload: bytes, *, entropy: bytes) -> bytes:
    if os.name != "nt":
        raise ApprovalBuildError("dpapi_windows_required")
    if not payload:
        raise ApprovalBuildError("dpapi_output_input_invalid")
    crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    input_buffer = ctypes.create_string_buffer(payload, len(payload))
    input_blob = _DataBlob(
        len(payload), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    entropy_buffer = ctypes.create_string_buffer(entropy, len(entropy))
    entropy_blob = _DataBlob(
        len(entropy),
        ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    output_blob = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    if not ok:
        raise ApprovalBuildError("dpapi_protect_failed")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _positive_int(value: Any, *, code: str) -> int:
    if isinstance(value, bool):
        raise ApprovalBuildError(code)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ApprovalBuildError(code) from exc
    if parsed <= 0:
        raise ApprovalBuildError(code)
    return parsed


def _optional_positive_int(value: Any, *, code: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, code=code)


def _optional_tenant(value: Any, *, code: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not TENANT_PATTERN.fullmatch(value):
        raise ApprovalBuildError(code)
    return value


def _entropy_label_bytes(value: str) -> bytes:
    if (
        not isinstance(value, str)
        or not 8 <= len(value) <= 128
        or not value.isascii()
        or any(character in value for character in "\r\n\x00")
    ):
        raise ApprovalBuildError("dpapi_entropy_label_invalid")
    return value.encode("ascii")


def _log_safe_error_code(value: Any, *, fallback: str) -> str:
    if isinstance(value, str) and LOG_ERROR_CODE_PATTERN.fullmatch(value):
        return value
    return fallback


def _load_manifest_offline(
    manifest_path: Path,
    *,
    master_key_dpapi_path: Path,
    expected_mac_dpapi_path: Path,
    master_key_entropy: bytes,
    manifest_mac_entropy: bytes,
    expected_scope_id: str,
) -> tuple[dict[str, Any], dict[str, Any], inventory_tools.ManifestKeys]:
    envelope = _read_private_json(
        manifest_path, code="source_manifest_input_invalid"
    )
    key_id = envelope.get("key_id")
    if not isinstance(key_id, str):
        raise ApprovalBuildError("source_manifest_key_id_invalid")
    try:
        master_key_text = _dpapi_unprotect(
            _read_private_bytes(master_key_dpapi_path, code="master_key_input_invalid"),
            entropy=master_key_entropy,
        ).decode("ascii")
        expected_mac = _dpapi_unprotect(
            _read_private_bytes(
                expected_mac_dpapi_path, code="manifest_mac_input_invalid"
            ),
            entropy=manifest_mac_entropy,
        ).decode("ascii")
    except UnicodeError as exc:
        raise ApprovalBuildError("dpapi_plaintext_invalid") from exc
    master_key_text = master_key_text.strip()
    expected_mac = expected_mac.strip().lower()
    if not SHA256_PATTERN.fullmatch(expected_mac):
        raise ApprovalBuildError("manifest_expected_mac_invalid")
    try:
        keys = inventory_tools.parse_manifest_master_key(master_key_text, key_id)
        encoded_payload = envelope.get("signed_payload")
        if not isinstance(encoded_payload, str):
            raise ApprovalBuildError("source_manifest_envelope_invalid")
        payload = inventory_tools._b64url_decode(
            encoded_payload, reason_code="manifest_payload_invalid"
        )
        untrusted_inventory = inventory_tools._strict_json_loads(
            payload.decode("utf-8"), reason_code="manifest_payload_invalid"
        )
        scope = (
            untrusted_inventory.get("scope")
            if isinstance(untrusted_inventory, dict)
            else None
        )
        if not isinstance(scope, dict):
            raise ApprovalBuildError("source_manifest_scope_invalid")
        root_identity = (
            _positive_int(scope.get("root_device"), code="source_manifest_scope_invalid"),
            _positive_int(scope.get("root_inode"), code="source_manifest_scope_invalid"),
        )
        inventory = inventory_tools.verify_manifest(
            envelope,
            keys,
            expected_scope_id=expected_scope_id,
            expected_root_identity=root_identity,
            expected_manifest_mac=expected_mac,
        )
    except ApprovalBuildError:
        raise
    except (inventory_tools.InventoryError, UnicodeError) as exc:
        raise ApprovalBuildError(str(exc)) from exc
    return envelope, inventory, keys


def _validate_review(
    review: Mapping[str, Any],
    *,
    envelope: Mapping[str, Any],
    inventory: Mapping[str, Any],
    keys: inventory_tools.ManifestKeys,
) -> list[dict[str, Any]]:
    if set(review) != {
        "contract_version",
        "key_id",
        "source_manifest_mac",
        "records",
    }:
        raise ApprovalBuildError("operator_review_structure_invalid")
    if (
        review.get("contract_version") != inventory_tools.REVIEW_CONTRACT_VERSION
        or review.get("key_id") != keys.key_id
        or review.get("source_manifest_mac") != envelope.get("mac")
    ):
        raise ApprovalBuildError("operator_review_binding_invalid")
    review_records = review.get("records")
    manifest_records = inventory.get("records")
    if not isinstance(review_records, list) or not isinstance(manifest_records, list):
        raise ApprovalBuildError("operator_review_structure_invalid")
    manifest_media: dict[str, dict[str, Any]] = {}
    for record in manifest_records:
        if not isinstance(record, dict):
            raise ApprovalBuildError("source_manifest_records_invalid")
        if record.get("category") == "media_review_required":
            path_id = record.get("path_id")
            if not isinstance(path_id, str) or path_id in manifest_media:
                raise ApprovalBuildError("source_manifest_records_invalid")
            manifest_media[path_id] = record

    expected_fields = {
        "relative_path",
        "path_id",
        "category",
        "size_bytes",
        "hash_status",
        "sha256",
    }
    validated: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for item in review_records:
        if not isinstance(item, dict) or set(item) != expected_fields:
            raise ApprovalBuildError("operator_review_record_invalid")
        relative = item.get("relative_path")
        path_id = item.get("path_id")
        if (
            not isinstance(relative, str)
            or not isinstance(path_id, str)
            or not SHA256_PATTERN.fullmatch(path_id)
            or relative in seen_paths
            or path_id in seen_ids
        ):
            raise ApprovalBuildError("operator_review_record_invalid")
        relative_path = PurePosixPath(relative)
        if (
            relative_path.is_absolute()
            or not relative_path.parts
            or any(part in {"", ".", ".."} for part in relative_path.parts)
            or "\\" in relative
            or "\x00" in relative
        ):
            raise ApprovalBuildError("operator_review_path_invalid")
        manifest_record = manifest_media.get(path_id)
        if manifest_record is None:
            raise ApprovalBuildError("operator_review_path_id_not_in_manifest")
        try:
            decrypted = inventory_tools.decrypt_relative_path(
                keys, manifest_record["path_token"]
            )
            derived_path_id = inventory_tools._path_id(keys, relative_path)
        except (KeyError, inventory_tools.InventoryError) as exc:
            raise ApprovalBuildError("operator_review_manifest_identity_invalid") from exc
        if decrypted != relative or derived_path_id != path_id:
            raise ApprovalBuildError("operator_review_manifest_identity_invalid")
        for field in ("category", "size_bytes", "hash_status", "sha256"):
            if item.get(field) != manifest_record.get(field):
                raise ApprovalBuildError("operator_review_manifest_metadata_mismatch")
        if (
            item.get("category") != "media_review_required"
            or item.get("hash_status") != "verified"
            or not isinstance(item.get("sha256"), str)
            or not SHA256_PATTERN.fullmatch(item["sha256"])
        ):
            raise ApprovalBuildError("operator_review_media_not_verified")
        seen_ids.add(path_id)
        seen_paths.add(relative)
        validated.append(dict(item))
    if seen_ids != set(manifest_media):
        raise ApprovalBuildError("operator_review_media_coverage_mismatch")
    return validated


def load_repair_policy(path: Path) -> RepairPolicy:
    payload = _read_private_bytes(path, code="repair_migration_input_invalid")
    digest = hashlib.sha256(payload).hexdigest()
    try:
        module = ast.parse(payload.decode("utf-8"))
    except (UnicodeError, SyntaxError) as exc:
        raise ApprovalBuildError("repair_migration_input_invalid") from exc
    values: dict[str, Any] = {}
    wanted = {
        "revision",
        "_EXPECTED_ATTACHMENT_COUNTS",
        "_SOURCE_TENANT_SLUG",
        "_SOURCE_TENANT_TYPE",
        "_TARGET_TENANT_SLUG",
        "_TARGET_TENANT_TYPE",
    }
    for node in module.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value_node = node.value
        for target in targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                try:
                    values[target.id] = ast.literal_eval(value_node)
                except (ValueError, TypeError) as exc:
                    raise ApprovalBuildError("repair_migration_policy_invalid") from exc
    if set(values) != wanted:
        raise ApprovalBuildError("repair_migration_policy_invalid")
    raw_counts = values["_EXPECTED_ATTACHMENT_COUNTS"]
    if not isinstance(raw_counts, dict) or not raw_counts:
        raise ApprovalBuildError("repair_migration_policy_invalid")
    counts: dict[int, int] = {}
    for raw_ticket, raw_count in raw_counts.items():
        ticket = _positive_int(raw_ticket, code="repair_migration_policy_invalid")
        count = _positive_int(raw_count, code="repair_migration_policy_invalid")
        if ticket in counts:
            raise ApprovalBuildError("repair_migration_policy_invalid")
        counts[ticket] = count
    revision = values["revision"]
    if not isinstance(revision, str) or not REVISION_PATTERN.fullmatch(revision):
        raise ApprovalBuildError("repair_migration_policy_invalid")
    strings = [
        values["_SOURCE_TENANT_SLUG"],
        values["_SOURCE_TENANT_TYPE"],
        values["_TARGET_TENANT_SLUG"],
        values["_TARGET_TENANT_TYPE"],
    ]
    if not all(isinstance(value, str) and value for value in strings):
        raise ApprovalBuildError("repair_migration_policy_invalid")
    if not TENANT_PATTERN.fullmatch(strings[0]) or not TENANT_PATTERN.fullmatch(
        strings[2]
    ):
        raise ApprovalBuildError("repair_migration_policy_invalid")
    return RepairPolicy(
        revision=revision,
        attachment_counts=counts,
        source_slug=strings[0],
        source_type=strings[1],
        target_slug=strings[2],
        target_type=strings[3],
        file_sha256=digest,
    )


def _validate_anchor(value: Mapping[str, Any]) -> TenantAnchor:
    expected_fields = {
        "slug",
        "tenant_id",
        "tenant_type",
        "owner_kind",
        "owner_id",
        "is_active",
        "slug_match_count",
        "owner_match_count",
    }
    if set(value) != expected_fields:
        raise ApprovalBuildError("neon_tenant_anchor_invalid")
    slug = value.get("slug")
    tenant_type = value.get("tenant_type")
    owner_kind = value.get("owner_kind")
    if (
        not isinstance(slug, str)
        or not TENANT_PATTERN.fullmatch(slug)
        or tenant_type not in {"municipio", "pyme"}
        or owner_kind != tenant_type
        or value.get("is_active") is not True
        or value.get("slug_match_count") != 1
        or value.get("owner_match_count") != 1
    ):
        raise ApprovalBuildError("neon_tenant_anchor_invalid")
    return TenantAnchor(
        slug=slug,
        tenant_id=_positive_int(
            value.get("tenant_id"), code="neon_tenant_anchor_invalid"
        ),
        tenant_type=tenant_type,
        owner_kind=owner_kind,
        owner_id=_positive_int(
            value.get("owner_id"), code="neon_tenant_anchor_invalid"
        ),
    )


def _validate_evidence_row(value: Mapping[str, Any]) -> EvidenceRow:
    expected_fields = {
        "reference_hash",
        "attachment_id",
        "actor_explicit_tenant_slug",
        "actor_structural_tenant_slugs",
        "session_tenant_slug",
        "municipio_ticket_id",
        "municipio_ticket_tenant_slug",
        "municipio_ticket_tenant_type",
        "pyme_ticket_id",
        "pyme_ticket_tenant_slug",
        "pyme_ticket_tenant_type",
    }
    if set(value) != expected_fields:
        raise ApprovalBuildError("neon_attachment_evidence_invalid")
    reference_hash = value.get("reference_hash")
    structural = value.get("actor_structural_tenant_slugs")
    if (
        not isinstance(reference_hash, str)
        or not SHA256_PATTERN.fullmatch(reference_hash)
        or not isinstance(structural, list)
    ):
        raise ApprovalBuildError("neon_attachment_evidence_invalid")
    normalized_structural: list[str] = []
    for item in structural:
        tenant = _optional_tenant(item, code="neon_attachment_evidence_invalid")
        if tenant is None or tenant in normalized_structural:
            raise ApprovalBuildError("neon_attachment_evidence_invalid")
        normalized_structural.append(tenant)
    if normalized_structural != sorted(normalized_structural):
        raise ApprovalBuildError("neon_attachment_evidence_invalid")
    municipio_ticket_id = _optional_positive_int(
        value.get("municipio_ticket_id"), code="neon_attachment_evidence_invalid"
    )
    pyme_ticket_id = _optional_positive_int(
        value.get("pyme_ticket_id"), code="neon_attachment_evidence_invalid"
    )
    municipio_slug = _optional_tenant(
        value.get("municipio_ticket_tenant_slug"),
        code="neon_attachment_evidence_invalid",
    )
    pyme_slug = _optional_tenant(
        value.get("pyme_ticket_tenant_slug"),
        code="neon_attachment_evidence_invalid",
    )
    municipio_type = value.get("municipio_ticket_tenant_type")
    pyme_type = value.get("pyme_ticket_tenant_type")
    if municipio_ticket_id is None:
        if municipio_slug is not None or municipio_type is not None:
            raise ApprovalBuildError("neon_attachment_evidence_invalid")
    elif municipio_type not in {"municipio", "pyme", None}:
        raise ApprovalBuildError("neon_attachment_evidence_invalid")
    if pyme_ticket_id is None:
        if pyme_slug is not None or pyme_type is not None:
            raise ApprovalBuildError("neon_attachment_evidence_invalid")
    elif pyme_type not in {"municipio", "pyme", None}:
        raise ApprovalBuildError("neon_attachment_evidence_invalid")
    if municipio_ticket_id is not None and pyme_ticket_id is not None:
        raise ApprovalBuildError("neon_attachment_evidence_invalid")
    return EvidenceRow(
        reference_hash=reference_hash,
        attachment_id=_positive_int(
            value.get("attachment_id"), code="neon_attachment_evidence_invalid"
        ),
        actor_explicit_tenant_slug=_optional_tenant(
            value.get("actor_explicit_tenant_slug"),
            code="neon_attachment_evidence_invalid",
        ),
        actor_structural_tenant_slugs=tuple(normalized_structural),
        session_tenant_slug=_optional_tenant(
            value.get("session_tenant_slug"),
            code="neon_attachment_evidence_invalid",
        ),
        municipio_ticket_id=municipio_ticket_id,
        municipio_ticket_tenant_slug=municipio_slug,
        municipio_ticket_tenant_type=municipio_type,
        pyme_ticket_id=pyme_ticket_id,
        pyme_ticket_tenant_slug=pyme_slug,
        pyme_ticket_tenant_type=pyme_type,
    )


def validate_neon_evidence(
    value: Mapping[str, Any],
    *,
    required_slugs: set[str],
    expected_project_id: str,
    expected_branch_id: str,
    expected_database_name: str,
) -> tuple[dict[str, TenantAnchor], dict[str, EvidenceRow]]:
    expected_fields = {
        "contract_version",
        "project_id",
        "branch_id",
        "database_name",
        "observed_at",
        "tenant_anchors",
        "attachment_rows",
    }
    if set(value) != expected_fields:
        raise ApprovalBuildError("neon_evidence_structure_invalid")
    if (
        value.get("contract_version") != EVIDENCE_CONTRACT_VERSION
        or value.get("project_id") != expected_project_id
        or value.get("branch_id") != expected_branch_id
        or value.get("database_name") != expected_database_name
        or not isinstance(value.get("observed_at"), str)
        or not value.get("observed_at")
        or not isinstance(value.get("tenant_anchors"), list)
        or not isinstance(value.get("attachment_rows"), list)
    ):
        raise ApprovalBuildError("neon_evidence_structure_invalid")
    anchors: dict[str, TenantAnchor] = {}
    for raw in value["tenant_anchors"]:
        if not isinstance(raw, dict):
            raise ApprovalBuildError("neon_tenant_anchor_invalid")
        anchor = _validate_anchor(raw)
        if anchor.slug in anchors:
            raise ApprovalBuildError("neon_tenant_anchor_invalid")
        anchors[anchor.slug] = anchor
    if set(anchors) != required_slugs:
        raise ApprovalBuildError("neon_tenant_anchor_set_mismatch")
    rows: dict[str, EvidenceRow] = {}
    attachment_ids: set[int] = set()
    for raw in value["attachment_rows"]:
        if not isinstance(raw, dict):
            raise ApprovalBuildError("neon_attachment_evidence_invalid")
        row = _validate_evidence_row(raw)
        if row.reference_hash in rows or row.attachment_id in attachment_ids:
            raise ApprovalBuildError("neon_attachment_evidence_duplicate")
        rows[row.reference_hash] = row
        attachment_ids.add(row.attachment_id)
    return anchors, rows


def _relative_parts(record: Mapping[str, Any]) -> tuple[str, ...]:
    relative = record.get("relative_path")
    if not isinstance(relative, str):
        raise ApprovalBuildError("operator_review_path_invalid")
    return tuple(part.lower() for part in PurePosixPath(relative).parts)


def _basename_hash(record: Mapping[str, Any]) -> str:
    relative = record.get("relative_path")
    if not isinstance(relative, str):
        raise ApprovalBuildError("operator_review_path_invalid")
    name = PurePosixPath(relative).name
    if not name or not name.isascii():
        raise ApprovalBuildError("operator_review_basename_normalization_unsafe")
    return hashlib.sha256(name.lower().encode("utf-8")).hexdigest()


def _thumbnail_parent_map(
    records: Sequence[dict[str, Any]],
) -> tuple[dict[str, str], set[str], set[str]]:
    originals_by_key: dict[tuple[tuple[str, ...], str], list[str]] = {}
    original_ids: set[str] = set()
    thumbnail_ids: set[str] = set()
    thumbnail_keys: dict[str, tuple[tuple[str, ...], str]] = {}
    for record in records:
        path_id = record["path_id"]
        relative = PurePosixPath(record["relative_path"])
        name = relative.name
        parent = tuple(part.lower() for part in relative.parent.parts)
        if name.lower().endswith(THUMB_SUFFIX):
            stem = name[: -len(THUMB_SUFFIX)].lower()
            if not stem:
                raise ApprovalBuildError("thumbnail_name_invalid")
            thumbnail_ids.add(path_id)
            thumbnail_keys[path_id] = (parent, stem)
            continue
        stem = PurePosixPath(name).stem.lower()
        originals_by_key.setdefault((parent, stem), []).append(path_id)
        original_ids.add(path_id)
    parent_by_thumbnail: dict[str, str] = {}
    for path_id, key in thumbnail_keys.items():
        candidates = originals_by_key.get(key, [])
        if len(candidates) != 1:
            raise ApprovalBuildError("thumbnail_parent_not_unique")
        parent_by_thumbnail[path_id] = candidates[0]
    return parent_by_thumbnail, original_ids, thumbnail_ids


def _recognized_tenant_values(row: EvidenceRow) -> set[str]:
    values = {
        row.actor_explicit_tenant_slug,
        row.session_tenant_slug,
        row.municipio_ticket_tenant_slug,
        row.pyme_ticket_tenant_slug,
        *row.actor_structural_tenant_slugs,
    }
    return {value for value in values if value is not None}


def _municipal_decision(
    row: EvidenceRow,
    *,
    target_slug: str,
    source_slug: str,
    repair: RepairPolicy,
) -> Decision:
    if row.pyme_ticket_id is not None:
        raise ApprovalBuildError("municipal_media_pyme_ticket_conflict")
    if (
        row.actor_explicit_tenant_slug not in {None, target_slug}
        or row.actor_structural_tenant_slugs not in {(), (target_slug,)}
        or row.session_tenant_slug not in {None, target_slug}
    ):
        raise ApprovalBuildError("municipal_media_tenant_conflict")
    explicit_target = row.actor_explicit_tenant_slug == target_slug
    dual_target = (
        row.actor_structural_tenant_slugs == (target_slug,)
        and row.session_tenant_slug == target_slug
    )
    if not explicit_target and not dual_target:
        raise ApprovalBuildError("municipal_media_target_evidence_missing")
    if row.municipio_ticket_id is None:
        if row.municipio_ticket_tenant_slug is not None:
            raise ApprovalBuildError("municipal_media_ticket_evidence_invalid")
        reason = "municipal_actor_explicit" if explicit_target else "municipal_actor_session"
        return Decision(target_slug, migration_tools.TENANT_APPROVAL_STATUS, reason)
    ticket_id = row.municipio_ticket_id
    ticket_slug = row.municipio_ticket_tenant_slug
    if ticket_slug == target_slug:
        if row.municipio_ticket_tenant_type != repair.target_type:
            raise ApprovalBuildError("municipal_media_ticket_scope_conflict")
        return Decision(
            target_slug,
            migration_tools.TENANT_APPROVAL_STATUS,
            "municipal_ticket_target",
        )
    if ticket_slug == source_slug and ticket_id in repair.attachment_counts:
        if row.municipio_ticket_tenant_type != repair.source_type:
            raise ApprovalBuildError("municipal_media_ticket_scope_conflict")
        return Decision(
            target_slug,
            migration_tools.TENANT_APPROVAL_STATUS,
            "municipal_bounded_repair",
        )
    raise ApprovalBuildError("municipal_media_ticket_scope_conflict")


def _pyme_decision(
    row: EvidenceRow | None,
    *,
    target_slug: str,
) -> Decision:
    if row is not None:
        if row.actor_explicit_tenant_slug not in {None, target_slug}:
            raise ApprovalBuildError("pyme_media_tenant_conflict")
        if row.actor_structural_tenant_slugs not in {(), (target_slug,)}:
            raise ApprovalBuildError("pyme_media_tenant_conflict")
        if row.municipio_ticket_id is not None:
            raise ApprovalBuildError("pyme_media_municipal_ticket_conflict")
        if row.pyme_ticket_tenant_slug not in {None, target_slug}:
            raise ApprovalBuildError("pyme_media_ticket_scope_conflict")
    return Decision(
        target_slug,
        migration_tools.TENANT_APPROVAL_STATUS,
        "pyme_private_owner_scope",
    )


def build_approval_plan(
    review_records: Sequence[dict[str, Any]],
    *,
    evidence: Mapping[str, Any],
    repair: RepairPolicy,
    municipal_target_slug: str,
    pyme_target_slug: str,
    legacy_source_slug: str,
    expected_total: int,
    expected_tenant_counts: Mapping[str, int],
    expected_quarantine_count: int,
    expected_neon_project_id: str,
    expected_neon_branch_id: str,
    expected_neon_database_name: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if (
        repair.source_slug != legacy_source_slug
        or repair.target_slug != municipal_target_slug
        or repair.source_type != "pyme"
        or repair.target_type != "municipio"
    ):
        raise ApprovalBuildError("repair_policy_scope_mismatch")
    if (
        expected_total <= 0
        or expected_quarantine_count < 0
        or set(expected_tenant_counts)
        != {municipal_target_slug, pyme_target_slug}
        or any(count <= 0 for count in expected_tenant_counts.values())
        or sum(expected_tenant_counts.values()) + expected_quarantine_count
        != expected_total
    ):
        raise ApprovalBuildError("approval_expectations_invalid")
    if len(review_records) != expected_total:
        raise ApprovalBuildError("approval_media_total_mismatch")

    anchors, evidence_rows = validate_neon_evidence(
        evidence,
        required_slugs={
            municipal_target_slug,
            pyme_target_slug,
            legacy_source_slug,
        },
        expected_project_id=expected_neon_project_id,
        expected_branch_id=expected_neon_branch_id,
        expected_database_name=expected_neon_database_name,
    )
    municipal_anchor = anchors[municipal_target_slug]
    pyme_anchor = anchors[pyme_target_slug]
    source_anchor = anchors[legacy_source_slug]
    if (
        municipal_anchor.tenant_type != "municipio"
        or municipal_anchor.owner_kind != "municipio"
        or pyme_anchor.tenant_type != "pyme"
        or pyme_anchor.owner_kind != "pyme"
        or source_anchor.tenant_type != repair.source_type
        or source_anchor.owner_kind != repair.source_type
    ):
        raise ApprovalBuildError("neon_tenant_anchor_scope_mismatch")

    parent_by_thumbnail, original_ids, thumbnail_ids = _thumbnail_parent_map(
        review_records
    )
    records_by_id = {record["path_id"]: record for record in review_records}
    if len(records_by_id) != len(review_records):
        raise ApprovalBuildError("operator_review_path_id_duplicate")
    original_hashes: dict[str, str] = {}
    for path_id in original_ids:
        digest = _basename_hash(records_by_id[path_id])
        if digest in original_hashes:
            raise ApprovalBuildError("operator_review_basename_hash_duplicate")
        original_hashes[digest] = path_id
    if not set(evidence_rows).issubset(original_hashes):
        raise ApprovalBuildError("neon_evidence_outside_review")

    legacy_marker = f"municipio_{source_anchor.owner_id}"
    pyme_marker = f"empresa_{pyme_anchor.owner_id}"
    decisions: dict[str, Decision] = {}
    repair_observed: Counter[int] = Counter()
    pyme_corrobated = 0
    for path_id in sorted(original_ids):
        record = records_by_id[path_id]
        parts = _relative_parts(record)
        has_legacy_marker = legacy_marker in parts
        has_pyme_marker = pyme_marker in parts
        if has_legacy_marker and has_pyme_marker:
            raise ApprovalBuildError("source_scope_markers_conflict")
        digest = _basename_hash(record)
        row = evidence_rows.get(digest)
        if has_legacy_marker:
            if row is None:
                raise ApprovalBuildError("municipal_media_database_evidence_missing")
            decision = _municipal_decision(
                row,
                target_slug=municipal_target_slug,
                source_slug=legacy_source_slug,
                repair=repair,
            )
            if decision.reason_code == "municipal_bounded_repair":
                repair_observed[int(row.municipio_ticket_id)] += 1
            decisions[path_id] = decision
            continue
        if has_pyme_marker:
            decision = _pyme_decision(row, target_slug=pyme_target_slug)
            if row is not None and target_slug_in_evidence(row, pyme_target_slug):
                pyme_corrobated += 1
            decisions[path_id] = decision
            continue
        if row is not None:
            target_values = _recognized_tenant_values(row) & {
                municipal_target_slug,
                pyme_target_slug,
            }
            if target_values:
                raise ApprovalBuildError("tenant_evidence_source_marker_mismatch")
        decisions[path_id] = Decision(
            None,
            migration_tools.QUARANTINE_APPROVAL_STATUS,
            "tenant_unresolved",
        )

    if dict(repair_observed) != repair.attachment_counts:
        raise ApprovalBuildError("bounded_repair_attachment_counts_mismatch")
    repair_ids_in_evidence = Counter(
        row.municipio_ticket_id
        for row in evidence_rows.values()
        if row.municipio_ticket_id in repair.attachment_counts
    )
    if dict(repair_ids_in_evidence) != repair.attachment_counts:
        raise ApprovalBuildError("bounded_repair_evidence_set_mismatch")
    if pyme_corrobated < 1:
        raise ApprovalBuildError("pyme_owner_scope_not_corrobated")

    for thumbnail_id in sorted(thumbnail_ids):
        parent_id = parent_by_thumbnail[thumbnail_id]
        parent_decision = decisions.get(parent_id)
        if parent_decision is None:
            raise ApprovalBuildError("thumbnail_parent_decision_missing")
        decisions[thumbnail_id] = Decision(
            parent_decision.tenant_slug,
            parent_decision.approval_status,
            f"thumbnail_inherited:{parent_decision.reason_code}",
        )
    if set(decisions) != set(records_by_id):
        raise ApprovalBuildError("approval_decision_coverage_mismatch")

    tenant_counts = Counter(
        decision.tenant_slug
        for decision in decisions.values()
        if decision.approval_status == migration_tools.TENANT_APPROVAL_STATUS
    )
    quarantine_count = sum(
        decision.approval_status
        == migration_tools.QUARANTINE_APPROVAL_STATUS
        for decision in decisions.values()
    )
    if dict(tenant_counts) != dict(expected_tenant_counts):
        raise ApprovalBuildError("approval_tenant_counts_mismatch")
    if quarantine_count != expected_quarantine_count:
        raise ApprovalBuildError("approval_quarantine_count_mismatch")

    references: list[dict[str, Any]] = []
    for path_id in sorted(decisions):
        decision = decisions[path_id]
        if decision.approval_status == migration_tools.TENANT_APPROVAL_STATUS:
            tenant_slug = str(decision.tenant_slug)
            references.append(
                {
                    "path_id": path_id,
                    "destination_key": migration_tools.destination_key_for(
                        path_id, tenant_slug
                    ),
                    "approval_status": migration_tools.TENANT_APPROVAL_STATUS,
                    "tenant_slug": tenant_slug,
                }
            )
        else:
            references.append(
                {
                    "path_id": path_id,
                    "destination_key": migration_tools.quarantine_destination_key_for(
                        path_id
                    ),
                    "approval_status": migration_tools.QUARANTINE_APPROVAL_STATUS,
                    "quarantine_reason": (
                        migration_tools.QUARANTINE_REASON_TENANT_UNRESOLVED
                    ),
                }
            )

    reason_counts = Counter(decision.reason_code for decision in decisions.values())
    aggregates = {
        "media_total": len(decisions),
        "originals_total": len(original_ids),
        "thumbnails_total": len(thumbnail_ids),
        "thumbnails_unique_parent_total": len(parent_by_thumbnail),
        "tenant_approved_total": sum(tenant_counts.values()),
        "tenant_counts": dict(sorted(tenant_counts.items())),
        "quarantine_approved_total": quarantine_count,
        "repair_attachment_counts": {
            str(key): repair_observed[key] for key in sorted(repair_observed)
        },
        "decision_reason_counts": dict(sorted(reason_counts.items())),
        "neon_attachment_evidence_rows": len(evidence_rows),
    }
    return references, aggregates


def target_slug_in_evidence(row: EvidenceRow, slug: str) -> bool:
    return slug in _recognized_tenant_values(row)


def seal_audit(
    payload: Mapping[str, Any], keys: inventory_tools.ManifestKeys
) -> dict[str, Any]:
    header = {
        "contract_version": AUDIT_CONTRACT_VERSION,
        "key_id": keys.key_id,
        "integrity_algorithm": AUDIT_INTEGRITY_ALGORITHM,
    }
    audit_key = hmac.new(
        keys.manifest_mac_key,
        b"chatboc-render-r2-approved-map-audit-v1\x00"
        + keys.key_id.encode("ascii"),
        hashlib.sha256,
    ).digest()
    authenticated = _canonical_json(header) + b"\x00" + _canonical_json(payload)
    return {
        **header,
        "audit_payload": dict(payload),
        "mac": hmac.new(audit_key, authenticated, hashlib.sha256).hexdigest(),
    }


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _require_private_output_dir(private_root: Path, output_dir: Path) -> Path:
    try:
        root = private_root.resolve(strict=True)
        output = output_dir.resolve(strict=True)
        root_meta = root.lstat()
        output_meta = output.lstat()
    except OSError as exc:
        raise ApprovalBuildError("private_output_directory_invalid") from exc
    if (
        not root.is_dir()
        or not output.is_dir()
        or not _is_within(output, root)
        or getattr(root_meta, "st_file_attributes", 0)
        & FILE_ATTRIBUTE_REPARSE_POINT
        or getattr(output_meta, "st_file_attributes", 0)
        & FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ApprovalBuildError("private_output_directory_invalid")
    return output


def _windows_identity() -> str:
    if os.name != "nt":
        raise ApprovalBuildError("private_output_windows_acl_required")
    try:
        result = subprocess.run(
            ["whoami"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ApprovalBuildError("private_output_identity_unavailable") from exc
    identity = result.stdout.strip()
    if result.returncode != 0 or not identity or "\n" in identity or "\r" in identity:
        raise ApprovalBuildError("private_output_identity_unavailable")
    return identity


def _tighten_windows_acl(path: Path, identity: str) -> None:
    try:
        result = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{identity}:(F)",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ApprovalBuildError("private_output_acl_failed") from exc
    if result.returncode != 0:
        raise ApprovalBuildError("private_output_acl_failed")


def _write_new_private_file(path: Path, payload: bytes, *, identity: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = -1
    created = False
    try:
        descriptor = os.open(path, flags, 0o600)
        created = True
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise ApprovalBuildError("private_output_write_failed")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        _tighten_windows_acl(path, identity)
        metadata = path.lstat()
        if (
            not path.is_file()
            or getattr(metadata, "st_file_attributes", 0)
            & FILE_ATTRIBUTE_REPARSE_POINT
            or metadata.st_size != len(payload)
        ):
            raise ApprovalBuildError("private_output_verification_failed")
    except FileExistsError as exc:
        raise ApprovalBuildError("private_output_exists") from exc
    except ApprovalBuildError:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    except OSError as exc:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise ApprovalBuildError("private_output_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def write_private_outputs(
    *,
    private_root: Path,
    output_dir: Path,
    map_name: str,
    audit_name: str,
    map_mac_name: str,
    approved_map: Mapping[str, Any],
    audit: Mapping[str, Any],
    map_mac_entropy: bytes,
) -> tuple[Path, Path, Path]:
    if any(
        not name
        or Path(name).name != name
        or name in {".", ".."}
        for name in (map_name, audit_name, map_mac_name)
    ):
        raise ApprovalBuildError("private_output_name_invalid")
    output = _require_private_output_dir(private_root, output_dir)
    paths = (
        output / map_name,
        output / audit_name,
        output / map_mac_name,
    )
    if len(set(paths)) != 3 or any(path.exists() for path in paths):
        raise ApprovalBuildError("private_output_exists")
    identity = _windows_identity()
    payloads = (
        _canonical_json(approved_map) + b"\n",
        _canonical_json(audit) + b"\n",
        _dpapi_protect(
            str(approved_map["mac"]).encode("ascii"), entropy=map_mac_entropy
        ),
    )
    created: list[Path] = []
    try:
        for path, payload in zip(paths, payloads, strict=True):
            _write_new_private_file(path, payload, identity=identity)
            created.append(path)
    except Exception:
        for path in created:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return paths


def _parse_expected_tenant_counts(values: Sequence[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for raw in values:
        slug, separator, raw_count = raw.partition("=")
        if (
            separator != "="
            or not TENANT_PATTERN.fullmatch(slug)
            or slug in result
        ):
            raise ApprovalBuildError("approval_expectations_invalid")
        result[slug] = _positive_int(
            raw_count, code="approval_expectations_invalid"
        )
    return result


def _load_evidence_argument(value: str) -> dict[str, Any]:
    if value == "-":
        payload = sys.stdin.buffer.readline(MAX_PRIVATE_INPUT_BYTES + 1)
        if len(payload) > MAX_PRIVATE_INPUT_BYTES:
            raise ApprovalBuildError("neon_evidence_input_invalid")
        try:
            result = _strict_json_loads(
                payload.decode("utf-8"), code="neon_evidence_input_invalid"
            )
        except UnicodeError as exc:
            raise ApprovalBuildError("neon_evidence_input_invalid") from exc
        if not isinstance(result, dict):
            raise ApprovalBuildError("neon_evidence_input_invalid")
        return result
    return _read_private_json(Path(value), code="neon_evidence_input_invalid")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a signed, private Render-to-R2 reference approval map."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--master-key-dpapi", type=Path, required=True)
    parser.add_argument("--manifest-mac-dpapi", type=Path, required=True)
    parser.add_argument("--master-key-entropy-label", required=True)
    parser.add_argument("--manifest-mac-entropy-label", required=True)
    parser.add_argument("--map-mac-entropy-label", required=True)
    parser.add_argument("--repair-migration", type=Path, required=True)
    parser.add_argument("--neon-evidence", required=True)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--scope-id", required=True)
    parser.add_argument("--municipal-target-slug", required=True)
    parser.add_argument("--pyme-target-slug", required=True)
    parser.add_argument("--legacy-source-slug", required=True)
    parser.add_argument("--expected-neon-project-id", required=True)
    parser.add_argument("--expected-neon-branch-id", required=True)
    parser.add_argument("--expected-neon-database-name", required=True)
    parser.add_argument("--expected-media-total", type=int, required=True)
    parser.add_argument(
        "--expected-tenant-count", action="append", default=[], required=True
    )
    parser.add_argument("--expected-quarantine-count", type=int, required=True)
    parser.add_argument(
        "--map-name", default="approved-reference-map.v2.json"
    )
    parser.add_argument(
        "--audit-name", default="approved-reference-map.v2.audit.json"
    )
    parser.add_argument(
        "--map-mac-name", default="approved-reference-map.v2.mac.dpapi"
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate and seal in memory without writing private outputs",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if not TENANT_PATTERN.fullmatch(args.municipal_target_slug):
            raise ApprovalBuildError("approval_tenant_slug_invalid")
        if not TENANT_PATTERN.fullmatch(args.pyme_target_slug):
            raise ApprovalBuildError("approval_tenant_slug_invalid")
        if not TENANT_PATTERN.fullmatch(args.legacy_source_slug):
            raise ApprovalBuildError("approval_tenant_slug_invalid")
        expected_counts = _parse_expected_tenant_counts(
            args.expected_tenant_count
        )
        master_key_entropy = _entropy_label_bytes(args.master_key_entropy_label)
        manifest_mac_entropy = _entropy_label_bytes(
            args.manifest_mac_entropy_label
        )
        map_mac_entropy = _entropy_label_bytes(args.map_mac_entropy_label)
        envelope, inventory, keys = _load_manifest_offline(
            args.manifest,
            master_key_dpapi_path=args.master_key_dpapi,
            expected_mac_dpapi_path=args.manifest_mac_dpapi,
            master_key_entropy=master_key_entropy,
            manifest_mac_entropy=manifest_mac_entropy,
            expected_scope_id=args.scope_id,
        )
        review_value = _read_private_json(
            args.review, code="operator_review_input_invalid"
        )
        review_records = _validate_review(
            review_value,
            envelope=envelope,
            inventory=inventory,
            keys=keys,
        )
        repair = load_repair_policy(args.repair_migration)
        evidence = _load_evidence_argument(args.neon_evidence)
        references, aggregates = build_approval_plan(
            review_records,
            evidence=evidence,
            repair=repair,
            municipal_target_slug=args.municipal_target_slug,
            pyme_target_slug=args.pyme_target_slug,
            legacy_source_slug=args.legacy_source_slug,
            expected_total=args.expected_media_total,
            expected_tenant_counts=expected_counts,
            expected_quarantine_count=args.expected_quarantine_count,
            expected_neon_project_id=args.expected_neon_project_id,
            expected_neon_branch_id=args.expected_neon_branch_id,
            expected_neon_database_name=args.expected_neon_database_name,
        )
        approved_payload = {
            "scope_id": args.scope_id,
            "source_manifest_mac": envelope["mac"],
            "approval_status": "approved",
            "quarantine_policy": dict(migration_tools.QUARANTINE_POLICY),
            "references": references,
        }
        approved_map = migration_tools.seal_approved_reference_map(
            approved_payload, keys
        )
        migration_tools.validate_approved_reference_map(
            approved_map,
            inventory=inventory,
            keys=keys,
            scope_id=args.scope_id,
            source_manifest_mac=envelope["mac"],
            expected_approved_map_mac=approved_map["mac"],
        )
        audit_payload = {
            "scope_id": args.scope_id,
            "source_manifest_mac": envelope["mac"],
            "approved_map_mac": approved_map["mac"],
            "approval_status": "approved",
            "review_sha256": hashlib.sha256(
                _canonical_json(review_value)
            ).hexdigest(),
            "neon_evidence_contract": evidence["contract_version"],
            "neon_evidence_binding": {
                "project_id": evidence["project_id"],
                "branch_id": evidence["branch_id"],
                "database_name": evidence["database_name"],
            },
            "neon_evidence_observed_at": evidence["observed_at"],
            "neon_evidence_sha256": hashlib.sha256(
                _canonical_json(evidence)
            ).hexdigest(),
            "repair_revision": repair.revision,
            "repair_migration_sha256": repair.file_sha256,
            "aggregates": aggregates,
            "contains_clear_paths": False,
            "contains_pii": False,
            "database_writes": False,
            "r2_writes": False,
            "render_writes": False,
        }
        audit = seal_audit(audit_payload, keys)
        if not args.validate_only:
            write_private_outputs(
                private_root=args.private_root,
                output_dir=args.output_dir,
                map_name=args.map_name,
                audit_name=args.audit_name,
                map_mac_name=args.map_mac_name,
                approved_map=approved_map,
                audit=audit,
                map_mac_entropy=map_mac_entropy,
            )
        print(
            json.dumps(
                {
                    "status": (
                        "approval_plan_validated"
                        if args.validate_only
                        else "approved_map_written"
                    ),
                    "media_total": aggregates["media_total"],
                    "tenant_approved_total": aggregates[
                        "tenant_approved_total"
                    ],
                    "tenant_counts": aggregates["tenant_counts"],
                    "quarantine_approved_total": aggregates[
                        "quarantine_approved_total"
                    ],
                    "thumbnails_unique_parent_total": aggregates[
                        "thumbnails_unique_parent_total"
                    ],
                    "go_for_cutover": False,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    except ApprovalBuildError as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error_code": _log_safe_error_code(
                        exc.code, fallback="approval_validation_failed"
                    ),
                    "go_for_cutover": False,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 2
    except (inventory_tools.InventoryError, migration_tools.MigrationError) as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error_code": _log_safe_error_code(
                        str(exc), fallback="dependency_validation_failed"
                    ),
                    "go_for_cutover": False,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error_code": "internal_error",
                    "go_for_cutover": False,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
