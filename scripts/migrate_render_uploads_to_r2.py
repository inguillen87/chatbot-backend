#!/usr/bin/env python3
"""Copy an approved, signed Render upload inventory to private R2 objects.

The command is deliberately narrower than a storage cutover.  It never deletes
source or destination objects and never updates application/database references.
Without both the ``copy-missing`` subcommand and ``--execute`` it is read-only.
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import hashlib
import hmac
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import sys
from typing import Any, Protocol
from urllib.parse import urlsplit

try:
    from scripts import inventory_persistent_data as inventory_tools
except ImportError:  # pragma: no cover - direct ``python scripts/...`` execution
    import inventory_persistent_data as inventory_tools


APPROVED_MAP_CONTRACT_VERSION = "storage.r2.approved-reference-map.v1"
LEDGER_CONTRACT_VERSION = "storage.r2.migration-ledger.v1"
SUMMARY_CONTRACT_VERSION = "storage.r2.migration-summary.v1"
DESTINATION_CONTRACT_VERSION = "storage.r2.destination-fingerprint.v1"
APPROVED_MAP_INTEGRITY_ALGORITHM = "HMAC-SHA256"
LEDGER_INTEGRITY_ALGORITHM = "HMAC-SHA256"
DEFAULT_EXPECTED_MAC_ENV = "STORAGE_EXPECTED_MANIFEST_MAC"
DEFAULT_EXPECTED_APPROVED_MAP_MAC_ENV = "STORAGE_EXPECTED_APPROVED_MAP_MAC"
DEFAULT_EXPECTED_DESTINATION_FINGERPRINT_ENV = (
    "STORAGE_EXPECTED_R2_DESTINATION_FINGERPRINT"
)
DEFAULT_MAX_OBJECT_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_OBJECTS = 1000
MAX_PRIVATE_INPUT_BYTES = 256 * 1024 * 1024

PATH_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SHA256_PATTERN = PATH_ID_PATTERN
TENANT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
ACCOUNT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
R2_JURISDICTIONS = frozenset({"eu", "fedramp", "us"})
MIGRATABLE_CATEGORIES = frozenset({"media_review_required"})
EXCLUDED_CATEGORIES = frozenset(
    {"secret", "secret_review_required", "database", "unknown"}
)
NOT_FOUND_CODES = frozenset({"404", "nosuchkey", "nosuchobject", "notfound"})
PRECONDITION_CODES = frozenset(
    {"412", "conditionalrequestconflict", "preconditionfailed"}
)


class MigrationError(RuntimeError):
    """A log-safe migration failure code, optionally with aggregate progress."""

    def __init__(self, code: str, summary: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.summary = summary


class S3Client(Protocol):
    def head_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def get_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def put_object(self, **kwargs: Any) -> Mapping[str, Any]: ...


class SourceReader(Protocol):
    def read_verified(self, record: Mapping[str, Any]) -> bytes: ...


@dataclass(frozen=True)
class ApprovedReference:
    path_id: str
    tenant_slug: str
    destination_key: str


@dataclass(frozen=True)
class R2Destination:
    endpoint_url: str
    hostname: str
    account_id: str
    jurisdiction: str | None
    bucket: str
    region: str
    fingerprint: str


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _artifact_mac_key(keys: inventory_tools.ManifestKeys) -> bytes:
    return hmac.new(
        keys.manifest_mac_key,
        b"chatboc-render-r2-ledger-v1\x00" + keys.key_id.encode("ascii"),
        hashlib.sha256,
    ).digest()


def _approved_map_mac_key(keys: inventory_tools.ManifestKeys) -> bytes:
    return hmac.new(
        keys.manifest_mac_key,
        b"chatboc-render-r2-approved-map-v1\x00" + keys.key_id.encode("ascii"),
        hashlib.sha256,
    ).digest()


def normalize_r2_destination(
    endpoint: str, bucket: str, *, region: str = "auto"
) -> R2Destination:
    try:
        endpoint_parts = urlsplit(endpoint)
        endpoint_port = endpoint_parts.port
    except (TypeError, ValueError) as exc:
        raise MigrationError("r2_endpoint_invalid") from exc
    hostname = (endpoint_parts.hostname or "").lower()
    labels = hostname.split(".")
    suffix = ["r2", "cloudflarestorage", "com"]
    prefix = labels[: -len(suffix)] if labels[-len(suffix) :] == suffix else []
    if len(prefix) == 1:
        account_id = prefix[0]
        jurisdiction = None
    elif len(prefix) == 2:
        account_id, jurisdiction = prefix
    else:
        account_id = ""
        jurisdiction = None
    if (
        endpoint_parts.scheme.lower() != "https"
        or not ACCOUNT_ID_PATTERN.fullmatch(account_id)
        or (jurisdiction is not None and jurisdiction not in R2_JURISDICTIONS)
        or endpoint_parts.username is not None
        or endpoint_parts.password is not None
        or endpoint_port not in {None, 443}
        or endpoint_parts.path not in {"", "/"}
        or endpoint_parts.query
        or endpoint_parts.fragment
    ):
        raise MigrationError("r2_endpoint_invalid")
    if not isinstance(bucket, str) or not BUCKET_PATTERN.fullmatch(bucket):
        raise MigrationError("r2_bucket_name_invalid")
    normalized_region = str(region or "auto").strip().lower()
    if normalized_region not in {"auto", "us-east-1"}:
        raise MigrationError("r2_region_invalid")
    normalized_endpoint = f"https://{hostname}"
    fingerprint_payload = {
        "contract_version": DESTINATION_CONTRACT_VERSION,
        "hostname": hostname,
        "account_id": account_id,
        "jurisdiction": jurisdiction,
        "bucket": bucket,
    }
    fingerprint = hashlib.sha256(_canonical_json(fingerprint_payload)).hexdigest()
    return R2Destination(
        endpoint_url=normalized_endpoint,
        hostname=hostname,
        account_id=account_id,
        jurisdiction=jurisdiction,
        bucket=bucket,
        region="auto" if normalized_region == "us-east-1" else normalized_region,
        fingerprint=fingerprint,
    )


def destination_key_for(path_id: str, tenant_slug: str) -> str:
    """Return the only accepted tenant-scoped opaque destination key."""

    if not PATH_ID_PATTERN.fullmatch(path_id):
        raise MigrationError("approved_map_path_id_invalid")
    if not TENANT_PATTERN.fullmatch(tenant_slug):
        raise MigrationError("approved_map_tenant_invalid")
    return f"render-import-v1/{tenant_slug}/{path_id[:2]}/{path_id}"


def seal_approved_reference_map(
    approved_payload: Mapping[str, Any], keys: inventory_tools.ManifestKeys
) -> dict[str, Any]:
    header = {
        "contract_version": APPROVED_MAP_CONTRACT_VERSION,
        "key_id": keys.key_id,
        "integrity_algorithm": APPROVED_MAP_INTEGRITY_ALGORITHM,
    }
    authenticated = (
        _canonical_json(header) + b"\x00" + _canonical_json(approved_payload)
    )
    return {
        **header,
        "approved_payload": dict(approved_payload),
        "mac": hmac.new(
            _approved_map_mac_key(keys), authenticated, hashlib.sha256
        ).hexdigest(),
    }


def validate_approved_reference_map(
    value: Mapping[str, Any],
    *,
    inventory: Mapping[str, Any],
    keys: inventory_tools.ManifestKeys,
    scope_id: str,
    source_manifest_mac: str,
    expected_approved_map_mac: str,
) -> tuple[list[tuple[dict[str, Any], ApprovedReference]], str]:
    """Validate an operator-approved mapping and bind it to the signed manifest."""

    envelope_fields = {
        "contract_version",
        "key_id",
        "integrity_algorithm",
        "approved_payload",
        "mac",
    }
    if set(value) != envelope_fields:
        raise MigrationError("approved_map_structure_invalid")
    map_mac = value.get("mac")
    if (
        value.get("contract_version") != APPROVED_MAP_CONTRACT_VERSION
        or value.get("key_id") != keys.key_id
        or value.get("integrity_algorithm") != APPROVED_MAP_INTEGRITY_ALGORITHM
        or not isinstance(map_mac, str)
        or not SHA256_PATTERN.fullmatch(map_mac)
        or not isinstance(expected_approved_map_mac, str)
        or not SHA256_PATTERN.fullmatch(expected_approved_map_mac)
    ):
        raise MigrationError("approved_map_envelope_invalid")
    if not hmac.compare_digest(map_mac, expected_approved_map_mac):
        raise MigrationError("approved_map_expected_mac_mismatch")
    approved_payload = value.get("approved_payload")
    if not isinstance(approved_payload, dict):
        raise MigrationError("approved_map_structure_invalid")
    header = {
        "contract_version": APPROVED_MAP_CONTRACT_VERSION,
        "key_id": keys.key_id,
        "integrity_algorithm": APPROVED_MAP_INTEGRITY_ALGORITHM,
    }
    authenticated = (
        _canonical_json(header) + b"\x00" + _canonical_json(approved_payload)
    )
    calculated_mac = hmac.new(
        _approved_map_mac_key(keys), authenticated, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(map_mac, calculated_mac):
        raise MigrationError("approved_map_integrity_check_failed")

    expected_payload_fields = {
        "scope_id",
        "source_manifest_mac",
        "approval_status",
        "references",
    }
    if set(approved_payload) != expected_payload_fields:
        raise MigrationError("approved_map_structure_invalid")
    if (
        approved_payload.get("scope_id") != scope_id
        or approved_payload.get("source_manifest_mac") != source_manifest_mac
    ):
        raise MigrationError("approved_map_binding_mismatch")
    if approved_payload.get("approval_status") != "approved":
        raise MigrationError("approved_map_not_approved")

    manifest_records = inventory.get("records")
    references = approved_payload.get("references")
    if not isinstance(manifest_records, list) or not isinstance(references, list):
        raise MigrationError("approved_map_structure_invalid")

    records_by_id: dict[str, dict[str, Any]] = {}
    required_ids: set[str] = set()
    for item in manifest_records:
        if not isinstance(item, dict):
            raise MigrationError("manifest_records_invalid")
        path_id = item.get("path_id")
        category = item.get("category")
        if not isinstance(path_id, str) or not PATH_ID_PATTERN.fullmatch(path_id):
            raise MigrationError("manifest_records_invalid")
        if path_id in records_by_id:
            raise MigrationError("manifest_records_invalid")
        records_by_id[path_id] = item
        if category in MIGRATABLE_CATEGORIES:
            required_ids.add(path_id)

    mapped_ids: set[str] = set()
    approved: list[tuple[dict[str, Any], ApprovedReference]] = []
    entry_fields = {
        "path_id",
        "tenant_slug",
        "destination_key",
        "approval_status",
    }
    for item in references:
        if not isinstance(item, dict) or set(item) != entry_fields:
            raise MigrationError("approved_map_reference_invalid")
        path_id = item.get("path_id")
        tenant_slug = item.get("tenant_slug")
        destination_key = item.get("destination_key")
        if not isinstance(path_id, str) or not PATH_ID_PATTERN.fullmatch(path_id):
            raise MigrationError("approved_map_path_id_invalid")
        if path_id in mapped_ids:
            raise MigrationError("approved_map_path_id_duplicate")
        if item.get("approval_status") != "approved":
            raise MigrationError("approved_map_has_unapproved_reference")
        if not isinstance(tenant_slug, str) or not TENANT_PATTERN.fullmatch(
            tenant_slug
        ):
            raise MigrationError("approved_map_tenant_invalid")
        expected_destination_key = destination_key_for(path_id, tenant_slug)
        if (
            not isinstance(destination_key, str)
            or destination_key != expected_destination_key
        ):
            raise MigrationError("approved_map_destination_invalid")
        record = records_by_id.get(path_id)
        if record is None:
            raise MigrationError("approved_map_path_id_not_in_manifest")
        category = record.get("category")
        if category in EXCLUDED_CATEGORIES:
            raise MigrationError("approved_map_category_excluded")
        if category not in MIGRATABLE_CATEGORIES:
            raise MigrationError("approved_map_category_not_migratable")
        mapped_ids.add(path_id)
        approved.append(
            (
                record,
                ApprovedReference(
                    path_id=path_id,
                    tenant_slug=tenant_slug,
                    destination_key=destination_key,
                ),
            )
        )

    if mapped_ids != required_ids:
        raise MigrationError("approved_map_has_pending_mappings")
    approved.sort(key=lambda pair: pair[1].path_id)
    return approved, map_mac


def _ledger_payload(
    *,
    scope_id: str,
    source_manifest_mac: str,
    approved_map_mac: str,
    destination_fingerprint: str,
) -> dict[str, Any]:
    return {
        "scope_id": scope_id,
        "source_manifest_mac": source_manifest_mac,
        "approved_map_mac": approved_map_mac,
        "destination_fingerprint": destination_fingerprint,
        "records": {},
    }


def seal_ledger(
    payload: Mapping[str, Any], keys: inventory_tools.ManifestKeys
) -> dict[str, Any]:
    header = {
        "contract_version": LEDGER_CONTRACT_VERSION,
        "key_id": keys.key_id,
        "integrity_algorithm": LEDGER_INTEGRITY_ALGORITHM,
    }
    authenticated = _canonical_json(header) + b"\x00" + _canonical_json(payload)
    return {
        **header,
        "payload": dict(payload),
        "mac": hmac.new(
            _artifact_mac_key(keys), authenticated, hashlib.sha256
        ).hexdigest(),
    }


def verify_ledger(
    envelope: Mapping[str, Any],
    keys: inventory_tools.ManifestKeys,
    *,
    scope_id: str,
    source_manifest_mac: str,
    approved_map_mac: str,
    destination_fingerprint: str,
) -> dict[str, Any]:
    expected_fields = {
        "contract_version",
        "key_id",
        "integrity_algorithm",
        "payload",
        "mac",
    }
    if set(envelope) != expected_fields:
        raise MigrationError("ledger_structure_invalid")
    if (
        envelope.get("contract_version") != LEDGER_CONTRACT_VERSION
        or envelope.get("key_id") != keys.key_id
        or envelope.get("integrity_algorithm") != LEDGER_INTEGRITY_ALGORITHM
    ):
        raise MigrationError("ledger_contract_or_key_mismatch")
    payload = envelope.get("payload")
    mac = envelope.get("mac")
    if (
        not isinstance(payload, dict)
        or not isinstance(mac, str)
        or not SHA256_PATTERN.fullmatch(mac)
    ):
        raise MigrationError("ledger_structure_invalid")
    header = {
        "contract_version": LEDGER_CONTRACT_VERSION,
        "key_id": keys.key_id,
        "integrity_algorithm": LEDGER_INTEGRITY_ALGORITHM,
    }
    expected = hmac.new(
        _artifact_mac_key(keys),
        _canonical_json(header) + b"\x00" + _canonical_json(payload),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(mac, expected):
        raise MigrationError("ledger_integrity_check_failed")

    if set(payload) != {
        "scope_id",
        "source_manifest_mac",
        "approved_map_mac",
        "destination_fingerprint",
        "records",
    }:
        raise MigrationError("ledger_payload_invalid")
    if (
        not isinstance(payload.get("approved_map_mac"), str)
        or not SHA256_PATTERN.fullmatch(payload["approved_map_mac"])
        or not isinstance(payload.get("destination_fingerprint"), str)
        or not SHA256_PATTERN.fullmatch(payload["destination_fingerprint"])
    ):
        raise MigrationError("ledger_payload_invalid")
    if (
        payload.get("scope_id") != scope_id
        or payload.get("source_manifest_mac") != source_manifest_mac
        or payload.get("approved_map_mac") != approved_map_mac
        or payload.get("destination_fingerprint") != destination_fingerprint
    ):
        raise MigrationError("ledger_binding_mismatch")
    records = payload.get("records")
    if not isinstance(records, dict):
        raise MigrationError("ledger_payload_invalid")
    record_fields = {
        "destination_key",
        "size_bytes",
        "sha256",
        "state",
    }
    for path_id, item in records.items():
        destination_key = (
            item.get("destination_key") if isinstance(item, dict) else None
        )
        destination_parts = (
            destination_key.split("/") if isinstance(destination_key, str) else []
        )
        if (
            not isinstance(path_id, str)
            or not PATH_ID_PATTERN.fullmatch(path_id)
            or not isinstance(item, dict)
            or set(item) != record_fields
            or not isinstance(item.get("destination_key"), str)
            or len(destination_parts) != 4
            or destination_parts[0] != "render-import-v1"
            or not TENANT_PATTERN.fullmatch(destination_parts[1])
            or destination_parts[2] != path_id[:2]
            or destination_parts[3] != path_id
            or not isinstance(item.get("size_bytes"), int)
            or isinstance(item.get("size_bytes"), bool)
            or item["size_bytes"] < 0
            or not isinstance(item.get("sha256"), str)
            or not SHA256_PATTERN.fullmatch(item["sha256"])
            or item.get("state")
            not in {"copied_verified", "existing_verified", "race_verified"}
        ):
            raise MigrationError("ledger_record_invalid")
    return payload


class PosixManifestSourceReader:
    """Read manifest paths with descriptor-relative, no-follow traversal."""

    def __init__(
        self,
        root: Path,
        keys: inventory_tools.ManifestKeys,
        root_identity: tuple[int, int],
        *,
        max_object_bytes: int,
    ) -> None:
        self.root = Path(root)
        self.keys = keys
        self.root_identity = root_identity
        self.max_object_bytes = max_object_bytes

    @staticmethod
    def _stable(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    def read_verified(self, record: Mapping[str, Any]) -> bytes:
        if not inventory_tools._descriptor_traversal_available():
            raise MigrationError("secure_source_open_unavailable")
        path_id = record.get("path_id")
        path_token = record.get("path_token")
        expected_size = record.get("size_bytes")
        expected_mtime = record.get("mtime_ns")
        expected_sha256 = record.get("sha256")
        if (
            not isinstance(path_id, str)
            or not PATH_ID_PATTERN.fullmatch(path_id)
            or not isinstance(path_token, str)
            or not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
            or not isinstance(expected_mtime, int)
            or isinstance(expected_mtime, bool)
            or not isinstance(expected_sha256, str)
            or not SHA256_PATTERN.fullmatch(expected_sha256)
            or record.get("hash_status") != "verified"
        ):
            raise MigrationError("manifest_source_record_invalid")
        if expected_size > self.max_object_bytes:
            raise MigrationError("source_object_size_limit_exceeded")

        recovered = inventory_tools.decrypt_relative_path(self.keys, path_token)
        relative_path = PurePosixPath(recovered)
        if inventory_tools._path_id(self.keys, relative_path) != path_id:
            raise MigrationError("manifest_source_identity_mismatch")
        if inventory_tools.classify_relative_path(relative_path) != record.get(
            "category"
        ):
            raise MigrationError("manifest_source_category_mismatch")

        try:
            inventory_tools._require_current_root_identity(
                self.root,
                self.root_identity,
                reason_code="source_root_identity_mismatch",
            )
            parent_fd = inventory_tools._open_directory_chain_no_follow(
                self.root, reason_code="source_root_open_failed"
            )
            try:
                root_metadata = os.fstat(parent_fd)
                if (
                    not stat.S_ISDIR(root_metadata.st_mode)
                    or (root_metadata.st_dev, root_metadata.st_ino)
                    != self.root_identity
                ):
                    raise MigrationError("source_root_identity_mismatch")
                root_device = root_metadata.st_dev
                for component in relative_path.parts[:-1]:
                    metadata = os.stat(
                        component, dir_fd=parent_fd, follow_symlinks=False
                    )
                    if (
                        stat.S_ISLNK(metadata.st_mode)
                        or not stat.S_ISDIR(metadata.st_mode)
                        or metadata.st_dev != root_device
                    ):
                        raise MigrationError("source_path_unsafe")
                    next_fd = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=parent_fd,
                    )
                    opened_directory = os.fstat(next_fd)
                    if (
                        not stat.S_ISDIR(opened_directory.st_mode)
                        or (opened_directory.st_dev, opened_directory.st_ino)
                        != (metadata.st_dev, metadata.st_ino)
                    ):
                        os.close(next_fd)
                        raise MigrationError("source_path_changed")
                    os.close(parent_fd)
                    parent_fd = next_fd

                name = relative_path.parts[-1]
                before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    stat.S_ISLNK(before.st_mode)
                    or not stat.S_ISREG(before.st_mode)
                    or before.st_dev != root_device
                ):
                    raise MigrationError("source_path_unsafe")
                if before.st_nlink != 1:
                    raise MigrationError("source_hardlink_refused")
                if (
                    before.st_size != expected_size
                    or before.st_mtime_ns != expected_mtime
                ):
                    raise MigrationError("source_manifest_metadata_mismatch")
                file_fd = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_BINARY", 0)
                    | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
                try:
                    opened = os.fstat(file_fd)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or (opened.st_dev, opened.st_ino)
                        != (before.st_dev, before.st_ino)
                    ):
                        raise MigrationError("source_path_changed")
                    if opened.st_nlink != 1:
                        raise MigrationError("source_hardlink_refused")
                    with os.fdopen(os.dup(file_fd), "rb") as handle:
                        payload = handle.read(expected_size + 1)
                    after = os.fstat(file_fd)
                    if (
                        after.st_nlink != 1
                        or self._stable(after) != self._stable(opened)
                    ):
                        raise MigrationError("source_changed_during_read")
                finally:
                    os.close(file_fd)
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if current.st_nlink != 1:
                    raise MigrationError("source_hardlink_refused")
                if self._stable(current) != self._stable(before):
                    raise MigrationError("source_changed_after_read")
            finally:
                os.close(parent_fd)
            inventory_tools._require_current_root_identity(
                self.root,
                self.root_identity,
                reason_code="source_root_identity_mismatch",
            )
        except MigrationError:
            raise
        except inventory_tools.InventoryError as exc:
            raise MigrationError(str(exc)) from exc
        except OSError as exc:
            raise MigrationError("source_open_or_read_failed") from exc

        if len(payload) != expected_size:
            raise MigrationError("source_manifest_size_mismatch")
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise MigrationError("source_manifest_checksum_mismatch")
        return payload


def _error_code_and_status(exc: BaseException) -> tuple[str, int]:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return "", 0
    error = response.get("Error")
    metadata = response.get("ResponseMetadata")
    code = str(error.get("Code") if isinstance(error, dict) else "").lower()
    try:
        status = int(
            metadata.get("HTTPStatusCode", 0) if isinstance(metadata, dict) else 0
        )
    except (TypeError, ValueError):
        status = 0
    return code, status


def _head_or_none(client: S3Client, bucket: str, key: str) -> Mapping[str, Any] | None:
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:  # provider SDK types are intentionally lazy
        code, status = _error_code_and_status(exc)
        if status == 404 or code in NOT_FOUND_CODES:
            return None
        raise MigrationError("destination_head_failed") from exc


def _read_body(
    response: Mapping[str, Any], *, maximum_bytes: int
) -> tuple[int, str]:
    body = response.get("Body")
    if body is None or not hasattr(body, "read"):
        raise MigrationError("destination_get_response_invalid")
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = body.read(1024 * 1024)
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise MigrationError("destination_get_response_invalid")
            digest.update(chunk)
            size += len(chunk)
            if size > maximum_bytes:
                raise MigrationError("destination_get_size_mismatch")
    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationError("destination_get_failed") from exc
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    return size, digest.hexdigest()


def _verify_destination(
    client: S3Client,
    bucket: str,
    key: str,
    *,
    expected_size: int,
    expected_sha256: str,
    known_head: Mapping[str, Any] | None = None,
) -> None:
    head = known_head if known_head is not None else _head_or_none(client, bucket, key)
    if head is None:
        raise MigrationError("destination_missing_after_operation")
    content_length = head.get("ContentLength")
    if (
        not isinstance(content_length, int)
        or isinstance(content_length, bool)
        or content_length != expected_size
    ):
        raise MigrationError("destination_size_mismatch")
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        raise MigrationError("destination_get_failed") from exc
    actual_size, actual_sha256 = _read_body(
        response, maximum_bytes=expected_size
    )
    response_length = response.get("ContentLength")
    if response_length is not None and response_length != actual_size:
        raise MigrationError("destination_get_size_mismatch")
    if actual_size != expected_size:
        raise MigrationError("destination_size_mismatch")
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise MigrationError("destination_checksum_mismatch")


def _content_md5(payload: bytes) -> str:
    try:
        digest = hashlib.md5(payload, usedforsecurity=False).digest()
    except TypeError:  # pragma: no cover - legacy Python fallback
        digest = hashlib.md5(payload).digest()  # noqa: S324 - S3 transport checksum
    return base64.b64encode(digest).decode("ascii")


def _completed_ledger_record(
    *, reference: ApprovedReference, record: Mapping[str, Any], state: str
) -> dict[str, Any]:
    return {
        "destination_key": reference.destination_key,
        "size_bytes": record["size_bytes"],
        "sha256": record["sha256"],
        "state": state,
    }


def _validate_resume_record(
    value: Mapping[str, Any],
    *,
    reference: ApprovedReference,
    record: Mapping[str, Any],
) -> None:
    if (
        value.get("destination_key") != reference.destination_key
        or value.get("size_bytes") != record.get("size_bytes")
        or value.get("sha256") != record.get("sha256")
    ):
        raise MigrationError("ledger_record_binding_mismatch")


def _new_summary(*, execute: bool, total: int) -> dict[str, Any]:
    return {
        "contract_version": SUMMARY_CONTRACT_VERSION,
        "status": "running",
        "operation": "copy-missing" if execute else "dry-run",
        "dry_run": not execute,
        "go_for_cutover": False,
        "objects_total": total,
        "sources_verified": 0,
        "objects_initially_missing": 0,
        "objects_existing_verified": 0,
        "objects_copied_verified": 0,
        "objects_race_verified": 0,
        "objects_resumed_verified": 0,
        "bytes_verified": 0,
        "failures": 0,
        "error_code": None,
    }


def migrate_approved_objects(
    approved: Iterable[tuple[dict[str, Any], ApprovedReference]],
    *,
    source_reader: SourceReader,
    client: S3Client,
    bucket: str,
    ledger_payload: dict[str, Any],
    checkpoint: Callable[[dict[str, Any]], None],
    execute: bool,
) -> dict[str, Any]:
    """Verify every source/destination and conditionally create missing objects."""

    items = list(approved)
    summary = _new_summary(execute=execute, total=len(items))
    ledger_records = ledger_payload.get("records")
    if not isinstance(ledger_records, dict):
        raise MigrationError("ledger_payload_invalid")
    approved_ids = {reference.path_id for _record, reference in items}
    if len(approved_ids) != len(items):
        raise MigrationError("approved_plan_path_id_duplicate")
    if not set(ledger_records).issubset(approved_ids):
        raise MigrationError("ledger_has_unapproved_record")

    try:
        for record, reference in items:
            payload = source_reader.read_verified(record)
            summary["sources_verified"] += 1
            expected_size = record["size_bytes"]
            expected_sha256 = record["sha256"]
            previous = ledger_records.get(reference.path_id)
            if previous is not None:
                if not isinstance(previous, dict):
                    raise MigrationError("ledger_record_invalid")
                _validate_resume_record(
                    previous, reference=reference, record=record
                )

            head = _head_or_none(client, bucket, reference.destination_key)
            if head is not None:
                _verify_destination(
                    client,
                    bucket,
                    reference.destination_key,
                    expected_size=expected_size,
                    expected_sha256=expected_sha256,
                    known_head=head,
                )
                if previous is not None:
                    summary["objects_resumed_verified"] += 1
                else:
                    summary["objects_existing_verified"] += 1
                    if execute:
                        ledger_records[reference.path_id] = _completed_ledger_record(
                            reference=reference,
                            record=record,
                            state="existing_verified",
                        )
                        checkpoint(ledger_payload)
                summary["bytes_verified"] += expected_size
                continue

            if previous is not None:
                raise MigrationError("ledger_destination_missing")
            summary["objects_initially_missing"] += 1
            if not execute:
                continue

            try:
                client.put_object(
                    Bucket=bucket,
                    Key=reference.destination_key,
                    Body=io.BytesIO(payload),
                    ContentLength=expected_size,
                    ContentMD5=_content_md5(payload),
                    ContentType=record.get("mime_type") or "application/octet-stream",
                    IfNoneMatch="*",
                    Metadata={
                        "source-path-id": reference.path_id,
                        "source-sha256": expected_sha256,
                    },
                )
                state = "copied_verified"
            except Exception as exc:
                code, status = _error_code_and_status(exc)
                if status != 412 and code not in PRECONDITION_CODES:
                    raise MigrationError("destination_put_failed") from exc
                state = "race_verified"

            # R2's S3 API does not expose a whole-object SHA-256 for this PUT.
            # A fresh HEAD plus full GET is the authoritative completion gate.
            _verify_destination(
                client,
                bucket,
                reference.destination_key,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
            )
            ledger_records[reference.path_id] = _completed_ledger_record(
                reference=reference,
                record=record,
                state=state,
            )
            checkpoint(ledger_payload)
            if state == "copied_verified":
                summary["objects_copied_verified"] += 1
            else:
                summary["objects_race_verified"] += 1
            summary["bytes_verified"] += expected_size
    except MigrationError as exc:
        summary["status"] = "failed"
        summary["failures"] += 1
        summary["error_code"] = exc.code
        exc.summary = summary
        raise

    summary["status"] = "complete"
    return summary


def _write_private_atomic_replace(
    path: Path,
    value: Mapping[str, Any],
    *,
    inventory_root: Path,
) -> None:
    """Atomically create/replace an owner-only resumable ledger on POSIX."""

    if not inventory_tools._descriptor_traversal_available():
        raise MigrationError("ledger_private_write_unavailable")
    output = inventory_tools._absolute_without_resolution(path)
    root = inventory_tools._absolute_without_resolution(inventory_root)
    if inventory_tools._path_is_within(output, root):
        raise MigrationError("ledger_must_be_outside_inventory_root")
    if not output.parent.exists():
        raise MigrationError("ledger_parent_must_exist")
    payload = _canonical_json(value) + b"\n"
    if len(payload) > MAX_PRIVATE_INPUT_BYTES:
        raise MigrationError("ledger_too_large")

    try:
        parent_fd = inventory_tools._open_directory_chain_no_follow(
            output.parent, reason_code="ledger_parent_unsafe"
        )
    except inventory_tools.InventoryError as exc:
        raise MigrationError(str(exc)) from exc
    temporary_name = f".{output.name}.{secrets.token_hex(16)}.tmp"
    descriptor = -1
    try:
        parent_metadata = os.fstat(parent_fd)
        if (
            parent_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(parent_metadata.st_mode) & 0o077
        ):
            raise MigrationError("ledger_parent_not_private")
        try:
            existing = os.stat(output.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            stat.S_ISLNK(existing.st_mode)
            or not stat.S_ISREG(existing.st_mode)
            or existing.st_uid != os.geteuid()
            or stat.S_IMODE(existing.st_mode) & 0o077
            or existing.st_nlink != 1
        ):
            raise MigrationError("ledger_existing_file_unsafe")
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise MigrationError("ledger_write_failed")
            offset += written
        os.fsync(descriptor)
        temporary_metadata = os.fstat(descriptor)
        os.replace(
            temporary_name,
            output.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        written_metadata = os.stat(
            output.name, dir_fd=parent_fd, follow_symlinks=False
        )
        if (
            not stat.S_ISREG(written_metadata.st_mode)
            or (written_metadata.st_dev, written_metadata.st_ino)
            != (temporary_metadata.st_dev, temporary_metadata.st_ino)
            or written_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(written_metadata.st_mode) != 0o600
            or written_metadata.st_nlink != 1
        ):
            raise MigrationError("ledger_write_verification_failed")
        os.fsync(parent_fd)
    except MigrationError:
        raise
    except OSError as exc:
        raise MigrationError("ledger_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        os.close(parent_fd)


def _load_private_json(path: Path, *, inventory_root: Path) -> dict[str, Any]:
    try:
        return inventory_tools._load_manifest(path, inventory_root=inventory_root)
    except inventory_tools.InventoryError as exc:
        raise MigrationError(str(exc)) from exc


def _validate_ledger_target(path: Path, *, inventory_root: Path) -> Path:
    output = inventory_tools._absolute_without_resolution(path)
    root = inventory_tools._absolute_without_resolution(inventory_root)
    if inventory_tools._path_is_within(output, root):
        raise MigrationError("ledger_must_be_outside_inventory_root")
    if not output.parent.exists():
        raise MigrationError("ledger_parent_must_exist")
    if not inventory_tools._descriptor_traversal_available():
        raise MigrationError("ledger_private_target_unavailable")
    try:
        parent_fd = inventory_tools._open_directory_chain_no_follow(
            output.parent, reason_code="ledger_parent_unsafe"
        )
    except inventory_tools.InventoryError as exc:
        raise MigrationError(str(exc)) from exc
    try:
        parent_metadata = os.fstat(parent_fd)
        if (
            parent_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(parent_metadata.st_mode) & 0o077
        ):
            raise MigrationError("ledger_parent_not_private")
    finally:
        os.close(parent_fd)
    return output


def _load_or_create_ledger(
    path: Path,
    *,
    inventory_root: Path,
    keys: inventory_tools.ManifestKeys,
    scope_id: str,
    source_manifest_mac: str,
    approved_map_mac: str,
    destination_fingerprint: str,
) -> dict[str, Any]:
    path = _validate_ledger_target(path, inventory_root=inventory_root)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return _ledger_payload(
            scope_id=scope_id,
            source_manifest_mac=source_manifest_mac,
            approved_map_mac=approved_map_mac,
            destination_fingerprint=destination_fingerprint,
        )
    except OSError as exc:
        raise MigrationError("ledger_input_invalid") from exc
    envelope = _load_private_json(path, inventory_root=inventory_root)
    return verify_ledger(
        envelope,
        keys,
        scope_id=scope_id,
        source_manifest_mac=source_manifest_mac,
        approved_map_mac=approved_map_mac,
        destination_fingerprint=destination_fingerprint,
    )


def _load_r2_destination() -> R2Destination:
    endpoint = os.environ.get("R2_ENDPOINT_URL")
    bucket = os.environ.get("R2_BUCKET_NAME")
    region = os.environ.get("R2_REGION") or "auto"
    if not endpoint or not bucket:
        raise MigrationError("r2_configuration_incomplete")
    return normalize_r2_destination(endpoint, bucket, region=region)


def _create_s3_client(destination: R2Destination) -> S3Client:
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not access_key or not secret_key:
        raise MigrationError("r2_configuration_incomplete")
    try:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=destination.endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=destination.region,
            config=Config(signature_version="s3v4"),
        )
    except Exception as exc:
        raise MigrationError("r2_client_initialization_failed") from exc
    return client


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify an approved Render upload manifest and copy only missing R2 "
            "objects. Default mode is read-only."
        )
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--approved-reference-map", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--scope-id", required=True)
    parser.add_argument("--expected-manifest-mac")
    parser.add_argument("--expected-approved-map-mac")
    parser.add_argument("--expected-destination-fingerprint")
    parser.add_argument(
        "--manifest-key-env", default=inventory_tools.DEFAULT_HMAC_KEY_ENV
    )
    parser.add_argument(
        "--expected-manifest-mac-env", default=DEFAULT_EXPECTED_MAC_ENV
    )
    parser.add_argument(
        "--expected-approved-map-mac-env",
        default=DEFAULT_EXPECTED_APPROVED_MAP_MAC_ENV,
    )
    parser.add_argument(
        "--expected-destination-fingerprint-env",
        default=DEFAULT_EXPECTED_DESTINATION_FINGERPRINT_ENV,
    )
    parser.add_argument(
        "--max-object-bytes", type=int, default=DEFAULT_MAX_OBJECT_BYTES
    )
    parser.add_argument("--max-objects", type=int, default=DEFAULT_MAX_OBJECTS)
    parser.set_defaults(execute=False)
    subparsers = parser.add_subparsers(dest="command")
    copy_parser = subparsers.add_parser(
        "copy-missing", help="conditionally copy missing approved objects"
    )
    copy_parser.add_argument(
        "--execute",
        action="store_true",
        help="enable conditional PutObject calls (otherwise remains a dry run)",
    )
    return parser


def _failure_summary(
    error: MigrationError,
    *,
    execute: bool,
) -> dict[str, Any]:
    if error.summary is not None:
        return error.summary
    summary = _new_summary(execute=execute, total=0)
    summary.update(status="failed", failures=1, error_code=error.code)
    return summary


def _expected_hex_value(
    explicit_value: str | None, env_name: str, *, error_code: str
) -> str:
    value = explicit_value or os.environ.get(env_name, "")
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise MigrationError(error_code)
    return value


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    execute = bool(args.command == "copy-missing" and args.execute)
    try:
        if args.max_object_bytes <= 0 or args.max_objects <= 0:
            raise MigrationError("migration_bound_invalid")
        raw_key = os.environ.get(args.manifest_key_env, "")
        keys = inventory_tools.parse_manifest_master_key(raw_key, args.key_id)
        expected_mac = _expected_hex_value(
            args.expected_manifest_mac,
            args.expected_manifest_mac_env,
            error_code="manifest_expected_mac_invalid",
        )
        expected_approved_map_mac = _expected_hex_value(
            args.expected_approved_map_mac,
            args.expected_approved_map_mac_env,
            error_code="approved_map_expected_mac_invalid",
        )
        expected_destination_fingerprint = _expected_hex_value(
            args.expected_destination_fingerprint,
            args.expected_destination_fingerprint_env,
            error_code="destination_expected_fingerprint_invalid",
        )
        envelope, signed_inventory, root_identity = (
            inventory_tools._load_and_verify_manifest(
                args.manifest,
                keys=keys,
                inventory_root=args.root,
                expected_scope_id=args.scope_id,
                expected_manifest_mac=expected_mac,
            )
        )
        approved_map = _load_private_json(
            args.approved_reference_map, inventory_root=args.root
        )
        approved, approved_map_mac = validate_approved_reference_map(
            approved_map,
            inventory=signed_inventory,
            keys=keys,
            scope_id=args.scope_id,
            source_manifest_mac=envelope["mac"],
            expected_approved_map_mac=expected_approved_map_mac,
        )
        if len(approved) > args.max_objects:
            raise MigrationError("approved_map_object_limit_exceeded")
        destination = _load_r2_destination()
        if not hmac.compare_digest(
            destination.fingerprint, expected_destination_fingerprint
        ):
            raise MigrationError("destination_fingerprint_mismatch")
        ledger_payload = _load_or_create_ledger(
            args.ledger,
            inventory_root=args.root,
            keys=keys,
            scope_id=args.scope_id,
            source_manifest_mac=envelope["mac"],
            approved_map_mac=approved_map_mac,
            destination_fingerprint=destination.fingerprint,
        )
        client = _create_s3_client(destination)
        reader = PosixManifestSourceReader(
            args.root,
            keys,
            root_identity,
            max_object_bytes=args.max_object_bytes,
        )

        def checkpoint(payload: dict[str, Any]) -> None:
            if not execute:
                raise MigrationError("dry_run_checkpoint_forbidden")
            _write_private_atomic_replace(
                args.ledger,
                seal_ledger(payload, keys),
                inventory_root=args.root,
            )

        summary = migrate_approved_objects(
            approved,
            source_reader=reader,
            client=client,
            bucket=destination.bucket,
            ledger_payload=ledger_payload,
            checkpoint=checkpoint,
            execute=execute,
        )
    except inventory_tools.InventoryError as exc:
        error = MigrationError(str(exc))
        print(json.dumps(_failure_summary(error, execute=execute), sort_keys=True))
        return 2
    except MigrationError as exc:
        print(json.dumps(_failure_summary(exc, execute=execute), sort_keys=True))
        return 2
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
