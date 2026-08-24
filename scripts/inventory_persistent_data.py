#!/usr/bin/env python3
"""Inventory persistent storage without turning the inventory into a new leak.

The public result contains aggregate counters only. Reviewable manifests use
encrypted relative paths, a keyed path identifier, and an HMAC over the whole
canonical payload. A manifest is never written from an unsafe traversal.
"""

from __future__ import annotations

import argparse
import base64
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import hmac
import json
import math
import mimetypes
import os
from pathlib import Path, PurePath, PurePosixPath
import re
import secrets
import stat
import sys
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.exceptions import InvalidTag


INVENTORY_CONTRACT_VERSION = "storage.inventory.v2"
SUMMARY_CONTRACT_VERSION = "storage.inventory.summary.v2"
MANIFEST_CONTRACT_VERSION = "storage.inventory.manifest.v2"
REVIEW_CONTRACT_VERSION = "storage.inventory.review.v1"
DEFAULT_HMAC_KEY_ENV = "STORAGE_INVENTORY_MASTER_KEY"
MASTER_KEY_BYTES = 32
HASH_CHUNK_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024 * 1024
KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$")
SCOPE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{7,127}$")

SECRET_FILENAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "google_service_key.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "service-account.json",
    "service_account.json",
    "vision_service_key.json",
}
SECRET_SUFFIXES = {
    ".jks",
    ".kdbx",
    ".key",
    ".keystore",
    ".p12",
    ".pem",
    ".pfx",
    ".ppk",
}
SECRET_PARTS = {
    ".aws",
    ".docker",
    ".gnupg",
    ".kube",
    ".ssh",
    "credential",
    "credentials",
    "secret",
    "secrets",
}
SECRET_NAME_MARKERS = {
    "api-key",
    "api_key",
    "apikey",
    "auth-token",
    "auth_token",
    "oauth-token",
    "oauth_token",
    "passwd",
    "password",
    "service-key",
    "service_key",
}
DATABASE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
DATABASE_SIDECAR_SUFFIXES = {
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3-journal",
    ".sqlite3-shm",
    ".sqlite3-wal",
}
CACHE_PARTS = {"cache", "caches", "temp", "tmp"}
MEDIA_PARTS = {
    "archivo",
    "archivos",
    "flyer",
    "flyers",
    "media",
    "upload",
    "uploads",
}
CONFIG_PARTS = {"config", "municipios", "pyme", "rubros", "tenant", "tenants"}
REVIEW_REQUIRED_CATEGORIES = {
    "media_review_required",
    "secret_review_required",
    "unknown",
}
NO_HASH_CATEGORIES = {"secret", "secret_review_required"}
ALL_CATEGORIES = {
    "secret",
    "database",
    "cache_or_temp",
    "media_review_required",
    "secret_review_required",
    "tenant_config",
    "unknown",
}


class InventoryError(RuntimeError):
    """A stable, log-safe code for a fail-closed inventory condition."""


@dataclass(frozen=True)
class ManifestKeys:
    key_id: str
    path_id_key: bytes
    path_encryption_key: bytes
    manifest_mac_key: bytes


@dataclass(frozen=True)
class FileSnapshot:
    relative_path: PurePath
    metadata: os.stat_result
    sha256: str | None
    hash_status: str


@dataclass
class ScanResult:
    files: dict[str, FileSnapshot]
    issues: dict[str, int]


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str, *, reason_code: str) -> bytes:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise InventoryError(reason_code)
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as exc:
        raise InventoryError(reason_code) from exc
    if _b64url_encode(decoded) != value:
        raise InventoryError(reason_code)
    return decoded


def _estimated_entropy_bits_per_byte(value: bytes) -> float:
    counts: dict[int, int] = defaultdict(int)
    for byte in value:
        counts[byte] += 1
    length = len(value)
    return -sum(
        (count / length) * math.log2(count / length) for count in counts.values()
    )


def parse_manifest_master_key(raw_value: str, key_id: str) -> ManifestKeys:
    """Decode a generated 256-bit key and derive domain-separated subkeys.

    Randomness cannot be proven from one sample. The format and conservative
    diversity checks reject passwords, repeated bytes, and common handcrafted
    sequences; the runbook requires generation by a CSPRNG.
    """

    if not KEY_ID_PATTERN.fullmatch(key_id):
        raise InventoryError("manifest_key_id_invalid")
    prefix = "base64url:"
    if not raw_value.startswith(prefix):
        raise InventoryError("manifest_master_key_format_invalid")
    master_key = _b64url_decode(
        raw_value[len(prefix) :], reason_code="manifest_master_key_format_invalid"
    )
    if len(master_key) != MASTER_KEY_BYTES:
        raise InventoryError("manifest_master_key_length_invalid")
    if len(set(master_key)) < 16 or _estimated_entropy_bits_per_byte(master_key) < 4.0:
        raise InventoryError("manifest_master_key_randomness_unverified")
    step = (master_key[1] - master_key[0]) % 256
    if all(
        (master_key[index] - master_key[index - 1]) % 256 == step
        for index in range(2, len(master_key))
    ):
        raise InventoryError("manifest_master_key_randomness_unverified")

    def derive(label: bytes) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"chatboc-storage-inventory-v2",
            info=label + b":" + key_id.encode("ascii"),
        ).derive(master_key)

    return ManifestKeys(
        key_id=key_id,
        path_id_key=derive(b"path-id"),
        path_encryption_key=derive(b"path-encryption"),
        manifest_mac_key=derive(b"manifest-mac"),
    )


def _normalized_parts(relative_path: PurePath) -> tuple[str, ...]:
    return tuple(part.casefold() for part in relative_path.parts)


def classify_relative_path(relative_path: PurePath) -> str:
    """Conservatively classify a path without opening the file."""

    parts = _normalized_parts(relative_path)
    name = parts[-1] if parts else ""
    suffixes = tuple(suffix.casefold() for suffix in PurePath(name).suffixes)
    is_env_file = name == ".env" or name.startswith(".env.")
    sensitive_name = (
        name in SECRET_FILENAMES
        or is_env_file
        or any(marker in name for marker in SECRET_NAME_MARKERS)
        or "service_account" in name
        or "service-account" in name
        or "credential" in name
        or "secret" in name
        or "private_key" in name
        or "private-key" in name
        or "privatekey" in name
        or name == "token.json"
        or name.endswith("-token.json")
        or name.endswith("_token.json")
        or re.search(r"(^|[-_.])token([-_.]|$)", name) is not None
    )
    secret_path = (
        sensitive_name
        or any(part in SECRET_PARTS for part in parts[:-1])
        or any(name.endswith(suffix) for suffix in SECRET_SUFFIXES)
    )
    if secret_path:
        if any(part in MEDIA_PARTS for part in parts[:-1]):
            return "secret_review_required"
        return "secret"
    if any(name.endswith(suffix) for suffix in DATABASE_SIDECAR_SUFFIXES):
        return "database"
    if suffixes and suffixes[-1] in DATABASE_SUFFIXES:
        return "database"
    if any(part in CACHE_PARTS for part in parts[:-1]):
        return "cache_or_temp"
    if any(part in MEDIA_PARTS for part in parts[:-1]):
        return "media_review_required"
    if any(part in CONFIG_PARTS for part in parts[:-1]) or (
        suffixes and suffixes[-1] in {".yaml", ".yml", ".toml", ".ini"}
    ):
        return "tenant_config"
    return "unknown"


def _is_link_or_reparse(path: Path, metadata: os.stat_result) -> bool:
    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    return bool(
        path.is_symlink()
        or is_junction(path)
        or stat.S_ISLNK(metadata.st_mode)
        or (
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
    )


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _stable_metadata(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _absolute_without_resolution(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_is_within(path: Path, root: Path) -> bool:
    path_value = os.path.normcase(os.path.abspath(os.fspath(path)))
    root_value = os.path.normcase(os.path.abspath(os.fspath(root)))
    try:
        return os.path.commonpath((path_value, root_value)) == root_value
    except ValueError:
        return False


def _validate_path_ancestors_no_links(path: Path, *, reason_code: str) -> None:
    absolute = _absolute_without_resolution(path)
    chain = list(reversed(absolute.parents)) + [absolute]
    for component in chain:
        try:
            metadata = component.lstat()
        except OSError as exc:
            raise InventoryError(reason_code) from exc
        if _is_link_or_reparse(component, metadata):
            raise InventoryError(reason_code)


def _descriptor_traversal_available() -> bool:
    supports_dir_fd = getattr(os, "supports_dir_fd", set())
    supports_fd = getattr(os, "supports_fd", set())
    return bool(
        os.name == "posix"
        and os.open in supports_dir_fd
        and os.stat in supports_dir_fd
        and os.unlink in supports_dir_fd
        and os.link in supports_dir_fd
        and os.listdir in supports_fd
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
    )


def _open_directory_chain_no_follow(path: Path, *, reason_code: str) -> int:
    if not _descriptor_traversal_available():
        raise InventoryError("descriptor_relative_traversal_unavailable")
    absolute = _absolute_without_resolution(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(os.path.sep, flags)
    try:
        for component in absolute.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise InventoryError(reason_code) from exc


def _validate_scope_id(scope_id: str) -> str:
    if not isinstance(scope_id, str) or not SCOPE_ID_PATTERN.fullmatch(scope_id):
        raise InventoryError("manifest_scope_id_invalid")
    return scope_id


def _capture_current_root_identity(
    root: Path, *, require_descriptor: bool
) -> tuple[int, int]:
    root = _absolute_without_resolution(root)
    _validate_path_ancestors_no_links(root, reason_code="root_ancestry_unsafe")
    try:
        preflight = root.lstat()
    except OSError as exc:
        raise InventoryError("root_identity_unavailable") from exc
    if _is_link_or_reparse(root, preflight) or not stat.S_ISDIR(preflight.st_mode):
        raise InventoryError("root_is_not_a_safe_directory")
    expected_identity = _identity(preflight)

    if _descriptor_traversal_available():
        descriptor = _open_directory_chain_no_follow(
            root, reason_code="root_ancestry_unsafe"
        )
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or _identity(opened) != expected_identity
            ):
                raise InventoryError("root_changed_before_scan")
        finally:
            os.close(descriptor)
    elif require_descriptor:
        raise InventoryError("secure_root_identity_unavailable")

    try:
        postflight = root.lstat()
    except OSError as exc:
        raise InventoryError("root_changed_before_scan") from exc
    if (
        _is_link_or_reparse(root, postflight)
        or not stat.S_ISDIR(postflight.st_mode)
        or _identity(postflight) != expected_identity
    ):
        raise InventoryError("root_changed_before_scan")
    return expected_identity


def _require_current_root_identity(
    root: Path,
    expected_identity: tuple[int, int],
    *,
    reason_code: str,
) -> None:
    current_identity = _capture_current_root_identity(root, require_descriptor=True)
    if current_identity != expected_identity:
        raise InventoryError(reason_code)


def _path_id(keys: ManifestKeys, relative_path: PurePath) -> str:
    normalized = relative_path.as_posix().encode("utf-8", errors="surrogatepass")
    return hmac.new(keys.path_id_key, normalized, hashlib.sha256).hexdigest()


def _encrypt_relative_path(keys: ManifestKeys, relative_path: PurePath) -> str:
    plaintext = relative_path.as_posix().encode("utf-8", errors="surrogatepass")
    nonce = secrets.token_bytes(12)
    aad = f"{INVENTORY_CONTRACT_VERSION}:{keys.key_id}".encode("ascii")
    ciphertext = AESGCM(keys.path_encryption_key).encrypt(nonce, plaintext, aad)
    return f"v1.{_b64url_encode(nonce)}.{_b64url_encode(ciphertext)}"


def decrypt_relative_path(keys: ManifestKeys, token: str) -> str:
    try:
        version, nonce_value, ciphertext_value = token.split(".", 2)
    except ValueError as exc:
        raise InventoryError("manifest_path_token_invalid") from exc
    if version != "v1":
        raise InventoryError("manifest_path_token_invalid")
    nonce = _b64url_decode(nonce_value, reason_code="manifest_path_token_invalid")
    ciphertext = _b64url_decode(
        ciphertext_value, reason_code="manifest_path_token_invalid"
    )
    if len(nonce) != 12:
        raise InventoryError("manifest_path_token_invalid")
    aad = f"{INVENTORY_CONTRACT_VERSION}:{keys.key_id}".encode("ascii")
    try:
        plaintext = AESGCM(keys.path_encryption_key).decrypt(nonce, ciphertext, aad)
        decoded = plaintext.decode("utf-8", errors="surrogatepass")
    except (InvalidTag, ValueError, UnicodeError) as exc:
        raise InventoryError("manifest_path_token_invalid") from exc
    candidate = PurePosixPath(decoded)
    if (
        candidate.is_absolute()
        or ".." in candidate.parts
        or candidate.as_posix() != decoded
    ):
        raise InventoryError("manifest_path_token_invalid")
    return decoded


def _hash_open_file(descriptor: int, before: os.stat_result) -> str:
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
        raise InventoryError("file_changed_before_hash")
    digest = hashlib.sha256()
    with os.fdopen(os.dup(descriptor), "rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    after = os.fstat(descriptor)
    if _stable_metadata(after) != _stable_metadata(opened):
        raise InventoryError("file_changed_during_hash")
    return digest.hexdigest()


def _hash_file_at(parent_fd: int, name: str, before: os.stat_result) -> str:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        digest = _hash_open_file(descriptor, before)
        after_path = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(after_path.st_mode) or _stable_metadata(
            after_path
        ) != _stable_metadata(before):
            raise InventoryError("file_changed_after_hash")
        return digest
    finally:
        os.close(descriptor)


def _increment(issues: dict[str, int], kind: str) -> None:
    issues[kind] = issues.get(kind, 0) + 1


def _scan_posix(
    root: Path,
    *,
    expected_root_identity: tuple[int, int],
    include_content_hashes: bool,
) -> ScanResult:
    root_fd = _open_directory_chain_no_follow(root, reason_code="root_ancestry_unsafe")
    issues: dict[str, int] = {}
    files: dict[str, FileSnapshot] = {}
    root_metadata = os.fstat(root_fd)
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or _identity(root_metadata) != expected_root_identity
    ):
        os.close(root_fd)
        raise InventoryError("root_changed_before_scan")
    root_device = root_metadata.st_dev
    visited_directories = {_identity(root_metadata)}

    def walk(directory_fd: int, relative_directory: PurePath, depth: int) -> None:
        if depth > 256:
            _increment(issues, "directory_depth_limit_exceeded")
            return
        before_directory = os.fstat(directory_fd)
        try:
            names = sorted(os.listdir(directory_fd), key=str.casefold)
        except OSError:
            _increment(issues, "directory_unreadable")
            return
        for name in names:
            if name in {".", ".."} or "/" in name or "\x00" in name:
                _increment(issues, "entry_name_invalid")
                continue
            relative_path = relative_directory / name
            try:
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                _increment(issues, "entry_unreadable")
                continue
            if stat.S_ISLNK(metadata.st_mode):
                _increment(issues, "symlink_skipped")
                continue
            if metadata.st_dev != root_device:
                _increment(issues, "filesystem_boundary_skipped")
                continue
            if stat.S_ISDIR(metadata.st_mode):
                try:
                    child_fd = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                    opened = os.fstat(child_fd)
                    if _identity(opened) != _identity(metadata):
                        os.close(child_fd)
                        _increment(issues, "directory_changed_before_open")
                        continue
                    if _identity(opened) in visited_directories:
                        os.close(child_fd)
                        _increment(issues, "directory_cycle_or_alias_skipped")
                        continue
                    visited_directories.add(_identity(opened))
                except OSError:
                    _increment(issues, "directory_open_failed")
                    continue
                try:
                    walk(child_fd, relative_path, depth + 1)
                finally:
                    os.close(child_fd)
                try:
                    after_child = os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False
                    )
                    if not stat.S_ISDIR(after_child.st_mode) or _identity(
                        after_child
                    ) != _identity(opened):
                        _increment(issues, "directory_changed_after_scan")
                except OSError:
                    _increment(issues, "directory_changed_after_scan")
                continue
            if not stat.S_ISREG(metadata.st_mode):
                _increment(issues, "special_file_skipped")
                continue
            category = classify_relative_path(relative_path)
            sha256: str | None = None
            hash_status = (
                "skipped_secret" if category in NO_HASH_CATEGORIES else "not_requested"
            )
            if include_content_hashes and category not in NO_HASH_CATEGORIES:
                try:
                    sha256 = _hash_file_at(directory_fd, name, metadata)
                    hash_status = "verified"
                except (InventoryError, OSError) as exc:
                    code = (
                        str(exc)
                        if isinstance(exc, InventoryError)
                        else "file_hash_failed"
                    )
                    _increment(issues, code)
                    hash_status = "failed"
            else:
                try:
                    after_file = os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False
                    )
                    if not stat.S_ISREG(after_file.st_mode) or _stable_metadata(
                        after_file
                    ) != _stable_metadata(metadata):
                        raise InventoryError("file_changed_without_hash")
                except (InventoryError, OSError) as exc:
                    code = (
                        str(exc)
                        if isinstance(exc, InventoryError)
                        else "file_changed_without_hash"
                    )
                    _increment(issues, code)
                    hash_status = "failed"
            files[relative_path.as_posix()] = FileSnapshot(
                relative_path=relative_path,
                metadata=metadata,
                sha256=sha256,
                hash_status=hash_status,
            )
        after_directory = os.fstat(directory_fd)
        if _stable_metadata(after_directory) != _stable_metadata(before_directory):
            _increment(issues, "directory_changed_during_inventory")

    try:
        walk(root_fd, PurePath(), 0)
    finally:
        os.close(root_fd)
    try:
        path_metadata = root.lstat()
        if _identity(path_metadata) != _identity(root_metadata) or _is_link_or_reparse(
            root, path_metadata
        ):
            _increment(issues, "root_changed_during_inventory")
    except OSError:
        _increment(issues, "root_changed_during_inventory")
    return ScanResult(files=files, issues=issues)


def _scan_path_fallback(
    root: Path, *, expected_root_identity: tuple[int, int] | None = None
) -> ScanResult:
    """Best-effort aggregate scan; it can never authorize a manifest or cutover."""

    issues: dict[str, int] = {"descriptor_relative_traversal_unavailable": 1}
    files: dict[str, FileSnapshot] = {}
    root_metadata = root.lstat()
    if _is_link_or_reparse(root, root_metadata):
        raise InventoryError("root_must_not_be_symlink_or_reparse")
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise InventoryError("root_is_not_a_directory")
    if (
        expected_root_identity is not None
        and _identity(root_metadata) != expected_root_identity
    ):
        raise InventoryError("root_changed_before_scan")
    root_absolute = _absolute_without_resolution(root)
    visited_directories = {_identity(root_metadata)}
    pending: list[tuple[Path, PurePath]] = [(root_absolute, PurePath())]
    while pending:
        directory, relative_directory = pending.pop()
        try:
            before_directory = directory.lstat()
            if _is_link_or_reparse(directory, before_directory):
                _increment(issues, "symlink_or_reparse_skipped")
                continue
            if directory != root_absolute:
                directory_identity = _identity(before_directory)
                if directory_identity in visited_directories:
                    _increment(issues, "directory_cycle_or_alias_skipped")
                    continue
                visited_directories.add(directory_identity)
            resolved_directory = directory.resolve(strict=True)
            if not _path_is_within(resolved_directory, root_absolute):
                _increment(issues, "directory_resolved_outside_root")
                continue
            with os.scandir(directory) as scanned:
                entries = sorted(scanned, key=lambda entry: entry.name.casefold())
        except OSError:
            _increment(issues, "directory_unreadable")
            continue
        for entry in entries:
            path = Path(entry.path)
            relative_path = relative_directory / entry.name
            try:
                metadata = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or _is_link_or_reparse(path, metadata):
                    _increment(issues, "symlink_or_reparse_skipped")
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append((path, relative_path))
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    _increment(issues, "special_file_skipped")
                    continue
            except OSError:
                _increment(issues, "entry_unreadable")
                continue
            files[relative_path.as_posix()] = FileSnapshot(
                relative_path=relative_path,
                metadata=metadata,
                sha256=None,
                hash_status="skipped_unverified_platform",
            )
        try:
            after_directory = directory.lstat()
            if _stable_metadata(after_directory) != _stable_metadata(before_directory):
                _increment(issues, "directory_changed_during_inventory")
        except OSError:
            _increment(issues, "directory_changed_during_inventory")
    try:
        final_root = root.lstat()
        if _identity(final_root) != _identity(root_metadata) or _is_link_or_reparse(
            root, final_root
        ):
            _increment(issues, "root_changed_during_inventory")
    except OSError:
        _increment(issues, "root_changed_during_inventory")
    return ScanResult(files=files, issues=issues)


def _scan_signature(result: ScanResult) -> dict[str, tuple[Any, ...]]:
    return {
        relative: (
            snapshot.metadata.st_dev,
            snapshot.metadata.st_ino,
            snapshot.metadata.st_size,
            snapshot.metadata.st_mtime_ns,
            snapshot.sha256,
            snapshot.hash_status,
        )
        for relative, snapshot in result.files.items()
    }


def _merge_issues(*issue_sets: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = defaultdict(int)
    for issue_set in issue_sets:
        for kind, count in issue_set.items():
            merged[kind] += count
    return dict(merged)


def build_inventory(
    root: Path,
    *,
    manifest_keys: ManifestKeys | None = None,
    scope_id: str | None = None,
    include_content_hashes: bool = True,
) -> dict[str, Any]:
    """Collect two consistency passes and return only pseudonymous records."""

    root = _absolute_without_resolution(root)
    if manifest_keys is not None and scope_id is None:
        raise InventoryError("manifest_scope_id_required")
    if scope_id is not None:
        scope_id = _validate_scope_id(scope_id)
    expected_root_identity = _capture_current_root_identity(
        root, require_descriptor=manifest_keys is not None
    )
    if _descriptor_traversal_available():
        first = _scan_posix(
            root,
            expected_root_identity=expected_root_identity,
            include_content_hashes=include_content_hashes,
        )
        second = _scan_posix(
            root,
            expected_root_identity=expected_root_identity,
            include_content_hashes=include_content_hashes,
        )
    else:
        first = _scan_path_fallback(root, expected_root_identity=expected_root_identity)
        second = _scan_path_fallback(
            root, expected_root_identity=expected_root_identity
        )
    try:
        postflight_root = root.lstat()
    except OSError as exc:
        raise InventoryError("root_changed_across_inventory") from exc
    if (
        _is_link_or_reparse(root, postflight_root)
        or not stat.S_ISDIR(postflight_root.st_mode)
        or _identity(postflight_root) != expected_root_identity
    ):
        raise InventoryError("root_changed_across_inventory")
    issues = _merge_issues(first.issues, second.issues)
    if _scan_signature(first) != _scan_signature(second):
        _increment(issues, "root_changed_between_consistency_passes")

    category_counts: dict[str, int] = defaultdict(int)
    category_bytes: dict[str, int] = defaultdict(int)
    records: list[dict[str, Any]] = []
    for relative in sorted(second.files, key=str.casefold):
        snapshot = second.files[relative]
        category = classify_relative_path(snapshot.relative_path)
        category_counts[category] += 1
        category_bytes[category] += snapshot.metadata.st_size
        if manifest_keys is None:
            continue
        records.append(
            {
                "path_id": _path_id(manifest_keys, snapshot.relative_path),
                "path_token": _encrypt_relative_path(
                    manifest_keys, snapshot.relative_path
                ),
                "category": category,
                "size_bytes": snapshot.metadata.st_size,
                "mtime_ns": snapshot.metadata.st_mtime_ns,
                "mime_type": mimetypes.guess_type(snapshot.relative_path.name)[0],
                "sha256": snapshot.sha256,
                "hash_status": snapshot.hash_status,
            }
        )

    categories = {
        category: {
            "files": category_counts[category],
            "bytes": category_bytes[category],
        }
        for category in sorted(category_counts)
    }
    sorted_issues = {kind: issues[kind] for kind in sorted(issues) if issues[kind]}
    review_required = sum(
        category_counts[category] for category in REVIEW_REQUIRED_CATEGORIES
    )
    if sorted_issues:
        status_value = "unsafe"
    elif review_required:
        status_value = "review_required"
    else:
        status_value = "complete"
    return {
        "contract_version": INVENTORY_CONTRACT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": (
            {
                "scope_id": scope_id,
                "root_device": expected_root_identity[0],
                "root_inode": expected_root_identity[1],
            }
            if scope_id is not None
            else None
        ),
        "status": status_value,
        "summary": {
            "files": len(second.files),
            "bytes": sum(
                snapshot.metadata.st_size for snapshot in second.files.values()
            ),
            "categories": categories,
            "issues": sorted_issues,
            "review_required_files": review_required,
            "consistency_passes": 2,
            "content_hashes_requested": include_content_hashes,
            "content_hashes_verified": sum(
                snapshot.hash_status == "verified" for snapshot in second.files.values()
            ),
            "secret_hashes_skipped": sum(
                snapshot.hash_status == "skipped_secret"
                for snapshot in second.files.values()
            ),
        },
        "records": records,
    }


def public_summary(
    inventory: dict[str, Any],
    *,
    manifest_written: bool,
    manifest_mac: str | None = None,
) -> dict[str, Any]:
    """Return the log-safe subset; records and paths are intentionally absent."""

    return {
        "contract_version": SUMMARY_CONTRACT_VERSION,
        "status": inventory["status"],
        "inventory_gate_passed": inventory["status"] == "complete",
        "go_for_cutover": False,
        "dry_run": not manifest_written,
        "manifest_written": manifest_written,
        "manifest_mac": manifest_mac if manifest_written else None,
        "scope_id": (
            inventory.get("scope", {}).get("scope_id")
            if manifest_written and isinstance(inventory.get("scope"), dict)
            else None
        ),
        **inventory["summary"],
    }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _strict_json_loads(payload: str, *, reason_code: str) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise InventoryError(reason_code)
            result[key] = value
        return result

    try:
        return json.loads(payload, object_pairs_hook=reject_duplicate_keys)
    except InventoryError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise InventoryError(reason_code) from exc


def seal_manifest(inventory: dict[str, Any], keys: ManifestKeys) -> dict[str, Any]:
    payload = _canonical_json(inventory)
    header = {
        "contract_version": MANIFEST_CONTRACT_VERSION,
        "key_id": keys.key_id,
        "integrity_algorithm": "HMAC-SHA256",
    }
    authenticated = _canonical_json(header) + b"\x00" + payload
    return {
        **header,
        "signed_payload": _b64url_encode(payload),
        "mac": hmac.new(
            keys.manifest_mac_key, authenticated, hashlib.sha256
        ).hexdigest(),
    }


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_inventory_for_manifest(
    inventory: dict[str, Any],
    keys: ManifestKeys,
    *,
    expected_scope_id: str,
    expected_root_identity: tuple[int, int],
) -> None:
    expected_scope_id = _validate_scope_id(expected_scope_id)
    if set(inventory) != {
        "contract_version",
        "generated_at",
        "scope",
        "status",
        "summary",
        "records",
    }:
        raise InventoryError("manifest_inventory_structure_invalid")
    if inventory.get("contract_version") != INVENTORY_CONTRACT_VERSION:
        raise InventoryError("manifest_inventory_structure_invalid")
    try:
        generated_at = datetime.fromisoformat(str(inventory["generated_at"]))
    except (TypeError, ValueError) as exc:
        raise InventoryError("manifest_inventory_structure_invalid") from exc
    if generated_at.tzinfo is None:
        raise InventoryError("manifest_inventory_structure_invalid")

    scope = inventory.get("scope")
    if (
        not isinstance(scope, dict)
        or set(scope) != {"scope_id", "root_device", "root_inode"}
        or not isinstance(scope.get("scope_id"), str)
        or not _is_non_negative_int(scope.get("root_device"))
        or not _is_non_negative_int(scope.get("root_inode"))
    ):
        raise InventoryError("manifest_scope_invalid")
    _validate_scope_id(scope["scope_id"])
    if (
        scope["scope_id"] != expected_scope_id
        or (
            scope["root_device"],
            scope["root_inode"],
        )
        != expected_root_identity
    ):
        raise InventoryError("manifest_scope_mismatch")

    records = inventory.get("records")
    summary = inventory.get("summary")
    if not isinstance(records, list) or not isinstance(summary, dict):
        raise InventoryError("manifest_inventory_structure_invalid")
    expected_record_fields = {
        "path_id",
        "path_token",
        "category",
        "size_bytes",
        "mtime_ns",
        "mime_type",
        "sha256",
        "hash_status",
    }
    path_ids: set[str] = set()
    recovered_paths: set[str] = set()
    category_counts: dict[str, int] = defaultdict(int)
    category_bytes: dict[str, int] = defaultdict(int)
    verified_hashes = 0
    skipped_secret_hashes = 0
    for record in records:
        if not isinstance(record, dict) or set(record) != expected_record_fields:
            raise InventoryError("manifest_record_structure_invalid")
        path_id = record.get("path_id")
        path_token = record.get("path_token")
        category = record.get("category")
        if (
            not isinstance(path_id, str)
            or not re.fullmatch(r"[0-9a-f]{64}", path_id)
            or not isinstance(path_token, str)
            or category not in ALL_CATEGORIES
            or not _is_non_negative_int(record.get("size_bytes"))
            or not _is_int(record.get("mtime_ns"))
            or not (
                record.get("mime_type") is None
                or isinstance(record.get("mime_type"), str)
            )
        ):
            raise InventoryError("manifest_record_structure_invalid")
        recovered = decrypt_relative_path(keys, path_token)
        recovered_path = PurePosixPath(recovered)
        if (
            _path_id(keys, recovered_path) != path_id
            or classify_relative_path(recovered_path) != category
            or path_id in path_ids
            or recovered in recovered_paths
        ):
            raise InventoryError("manifest_record_identity_invalid")
        path_ids.add(path_id)
        recovered_paths.add(recovered)

        content_hash = record.get("sha256")
        hash_status = record.get("hash_status")
        if category in NO_HASH_CATEGORIES:
            if content_hash is not None or hash_status != "skipped_secret":
                raise InventoryError("manifest_secret_hash_policy_invalid")
            skipped_secret_hashes += 1
        else:
            if (
                not isinstance(content_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", content_hash)
                or hash_status != "verified"
            ):
                raise InventoryError("manifest_content_hash_required")
            verified_hashes += 1
        category_counts[category] += 1
        category_bytes[category] += record["size_bytes"]

    expected_categories = {
        category: {
            "files": category_counts[category],
            "bytes": category_bytes[category],
        }
        for category in sorted(category_counts)
    }
    review_required = sum(
        category_counts[category] for category in REVIEW_REQUIRED_CATEGORIES
    )
    expected_status = "review_required" if review_required else "complete"
    expected_summary = {
        "files": len(records),
        "bytes": sum(record["size_bytes"] for record in records),
        "categories": expected_categories,
        "issues": {},
        "review_required_files": review_required,
        "consistency_passes": 2,
        "content_hashes_requested": True,
        "content_hashes_verified": verified_hashes,
        "secret_hashes_skipped": skipped_secret_hashes,
    }
    if inventory.get("status") != expected_status or summary != expected_summary:
        raise InventoryError("manifest_inventory_summary_invalid")


def verify_manifest(
    envelope: dict[str, Any],
    keys: ManifestKeys,
    *,
    expected_scope_id: str,
    expected_root_identity: tuple[int, int],
    expected_manifest_mac: str,
) -> dict[str, Any]:
    expected_fields = {
        "contract_version",
        "key_id",
        "integrity_algorithm",
        "signed_payload",
        "mac",
    }
    if set(envelope) != expected_fields:
        raise InventoryError("manifest_envelope_invalid")
    if (
        not isinstance(envelope.get("signed_payload"), str)
        or not isinstance(envelope.get("mac"), str)
        or re.fullmatch(r"[0-9a-f]{64}", envelope["mac"]) is None
        or not isinstance(expected_manifest_mac, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_manifest_mac) is None
    ):
        raise InventoryError("manifest_envelope_invalid")
    if not hmac.compare_digest(envelope["mac"], expected_manifest_mac):
        raise InventoryError("manifest_expected_mac_mismatch")
    if (
        envelope.get("contract_version") != MANIFEST_CONTRACT_VERSION
        or envelope.get("integrity_algorithm") != "HMAC-SHA256"
        or envelope.get("key_id") != keys.key_id
    ):
        raise InventoryError("manifest_key_or_contract_mismatch")
    payload = _b64url_decode(
        envelope["signed_payload"], reason_code="manifest_payload_invalid"
    )
    header = {
        "contract_version": envelope["contract_version"],
        "key_id": envelope["key_id"],
        "integrity_algorithm": envelope["integrity_algorithm"],
    }
    expected_mac = hmac.new(
        keys.manifest_mac_key,
        _canonical_json(header) + b"\x00" + payload,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(envelope["mac"], expected_mac):
        raise InventoryError("manifest_integrity_check_failed")
    try:
        inventory = _strict_json_loads(
            payload.decode("utf-8"), reason_code="manifest_payload_invalid"
        )
    except UnicodeError as exc:
        raise InventoryError("manifest_payload_invalid") from exc
    if (
        not isinstance(inventory, dict)
        or inventory.get("contract_version") != INVENTORY_CONTRACT_VERSION
    ):
        raise InventoryError("manifest_payload_invalid")
    _validate_inventory_for_manifest(
        inventory,
        keys,
        expected_scope_id=expected_scope_id,
        expected_root_identity=expected_root_identity,
    )
    return inventory


def _load_manifest(path: Path, *, inventory_root: Path) -> dict[str, Any]:
    path = _absolute_without_resolution(path)
    root = _absolute_without_resolution(inventory_root)
    if _path_is_within(path, root):
        raise InventoryError("manifest_input_must_be_outside_inventory_root")
    if os.name != "posix" or not _descriptor_traversal_available():
        raise InventoryError("manifest_input_acl_unverified")

    parent_fd = _open_directory_chain_no_follow(
        path.parent, reason_code="manifest_input_ancestry_unsafe"
    )
    try:
        parent_metadata = os.fstat(parent_fd)
        parent_identity = _identity(parent_metadata)
        if (
            parent_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(parent_metadata.st_mode) & 0o077
        ):
            raise InventoryError("manifest_input_parent_not_private")
        metadata = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise InventoryError("manifest_input_unsafe")
        if (
            metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_nlink != 1
        ):
            raise InventoryError("manifest_input_permissions_unsafe")
        if metadata.st_size > MAX_MANIFEST_BYTES:
            raise InventoryError("manifest_input_too_large")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | os.O_NOFOLLOW
        descriptor = os.open(path.name, flags, dir_fd=parent_fd)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(
                metadata
            ):
                raise InventoryError("manifest_input_changed")
            with os.fdopen(os.dup(descriptor), "rb") as handle:
                payload = handle.read(MAX_MANIFEST_BYTES + 1)
            if _stable_metadata(os.fstat(descriptor)) != _stable_metadata(opened):
                raise InventoryError("manifest_input_changed")
        finally:
            os.close(descriptor)
        if len(payload) > MAX_MANIFEST_BYTES:
            raise InventoryError("manifest_input_too_large")
        current_path = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(current_path.st_mode) or _stable_metadata(
            current_path
        ) != _stable_metadata(opened):
            raise InventoryError("manifest_input_changed")
        current_parent = path.parent.lstat()
        if _identity(current_parent) != parent_identity or _is_link_or_reparse(
            path.parent, current_parent
        ):
            raise InventoryError("manifest_input_ancestry_changed")
        value = _strict_json_loads(
            payload.decode("utf-8"), reason_code="manifest_input_invalid"
        )
    except InventoryError:
        raise
    except (OSError, UnicodeError) as exc:
        raise InventoryError("manifest_input_invalid") from exc
    finally:
        os.close(parent_fd)
    if not isinstance(value, dict):
        raise InventoryError("manifest_input_invalid")
    return value


def _load_and_verify_manifest(
    path: Path,
    *,
    keys: ManifestKeys,
    inventory_root: Path,
    expected_scope_id: str,
    expected_manifest_mac: str,
) -> tuple[dict[str, Any], dict[str, Any], tuple[int, int]]:
    root_identity_before = _capture_current_root_identity(
        inventory_root, require_descriptor=True
    )
    envelope = _load_manifest(path, inventory_root=inventory_root)
    root_identity_after_load = _capture_current_root_identity(
        inventory_root, require_descriptor=True
    )
    if root_identity_after_load != root_identity_before:
        raise InventoryError("root_changed_during_manifest_load")
    inventory = verify_manifest(
        envelope,
        keys,
        expected_scope_id=expected_scope_id,
        expected_root_identity=root_identity_after_load,
        expected_manifest_mac=expected_manifest_mac,
    )
    _require_current_root_identity(
        inventory_root,
        root_identity_after_load,
        reason_code="root_changed_during_manifest_verification",
    )
    return envelope, inventory, root_identity_after_load


def _publish_no_replace(
    parent_fd: int | None, temporary: Path | str, output: Path | str
) -> None:
    """Atomically publish through a hard link; an existing target always wins."""

    try:
        if parent_fd is None:
            os.link(temporary, output, follow_symlinks=False)
        else:
            os.link(
                temporary,
                output,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
    except FileExistsError as exc:
        raise InventoryError("private_output_exists") from exc
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise InventoryError("private_output_exists") from exc
        raise InventoryError("private_output_publish_failed") from exc


def _write_private_json(
    path: Path,
    value: dict[str, Any],
    *,
    inventory_root: Path,
    overwrite: bool,
    before_publish_check: Callable[[], None] | None = None,
    after_publish_check: Callable[[], None] | None = None,
) -> None:
    """Write owner-only JSON without path races or implicit replacement."""

    if overwrite:
        raise InventoryError("private_output_overwrite_not_supported")
    output = _absolute_without_resolution(path)
    root = _absolute_without_resolution(inventory_root)
    _validate_path_ancestors_no_links(
        root, reason_code="inventory_root_ancestry_unsafe"
    )
    if _path_is_within(output, root):
        raise InventoryError("private_output_must_be_outside_inventory_root")
    if not output.parent.exists():
        raise InventoryError("private_output_parent_must_exist")
    _validate_path_ancestors_no_links(
        output.parent, reason_code="private_output_ancestry_unsafe"
    )
    if os.name != "posix" or not _descriptor_traversal_available():
        raise InventoryError("private_output_acl_unverified")
    payload = _canonical_json(value) + b"\n"
    if len(payload) > MAX_MANIFEST_BYTES:
        raise InventoryError("private_output_too_large")

    parent_fd = _open_directory_chain_no_follow(
        output.parent, reason_code="private_output_ancestry_unsafe"
    )
    parent_metadata = os.fstat(parent_fd)
    parent_identity = _identity(parent_metadata)
    if (
        parent_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(parent_metadata.st_mode) & 0o077
    ):
        os.close(parent_fd)
        raise InventoryError("private_output_parent_not_private")
    temporary_name = f".{output.name}.{secrets.token_hex(16)}.tmp"
    descriptor = -1
    try:
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
                raise InventoryError("private_output_write_failed")
            offset += written
        os.fsync(descriptor)
        temporary_metadata = os.fstat(descriptor)
        if before_publish_check is not None:
            before_publish_check()
        _publish_no_replace(parent_fd, temporary_name, output.name)
        written_metadata = os.stat(output.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(written_metadata.st_mode)
            or _identity(written_metadata) != _identity(temporary_metadata)
            or stat.S_IMODE(written_metadata.st_mode) != 0o600
            or written_metadata.st_uid != os.geteuid()
        ):
            raise InventoryError("private_output_permissions_unverified")
        os.unlink(temporary_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        try:
            current_parent = output.parent.lstat()
            if _identity(current_parent) != parent_identity or _is_link_or_reparse(
                output.parent, current_parent
            ):
                raise InventoryError("private_output_parent_changed")
        except OSError as exc:
            raise InventoryError("private_output_parent_changed") from exc
        final_metadata = os.stat(output.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _identity(final_metadata) != _identity(temporary_metadata)
            or final_metadata.st_nlink != 1
        ):
            raise InventoryError("private_output_changed_after_publish")
        if after_publish_check is not None:
            after_publish_check()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def write_private_manifest(
    path: Path,
    inventory: dict[str, Any],
    *,
    keys: ManifestKeys,
    inventory_root: Path,
    expected_scope_id: str,
    overwrite: bool = False,
) -> str:
    if inventory.get("status") == "unsafe":
        raise InventoryError("unsafe_inventory_manifest_refused")
    current_root_identity = _capture_current_root_identity(
        inventory_root, require_descriptor=False
    )
    _validate_inventory_for_manifest(
        inventory,
        keys,
        expected_scope_id=expected_scope_id,
        expected_root_identity=current_root_identity,
    )
    envelope = seal_manifest(inventory, keys)

    def require_unchanged_root() -> None:
        _require_current_root_identity(
            inventory_root,
            current_root_identity,
            reason_code="root_changed_during_manifest_write",
        )

    _write_private_json(
        path,
        envelope,
        inventory_root=inventory_root,
        overwrite=overwrite,
        before_publish_check=require_unchanged_root,
        after_publish_check=require_unchanged_root,
    )
    return envelope["mac"]


def write_operator_review(
    manifest_path: Path,
    review_path: Path,
    *,
    keys: ManifestKeys,
    inventory_root: Path,
    expected_scope_id: str,
    expected_manifest_mac: str,
    overwrite: bool = False,
) -> int:
    envelope, inventory, current_root_identity = _load_and_verify_manifest(
        manifest_path,
        keys=keys,
        inventory_root=inventory_root,
        expected_scope_id=expected_scope_id,
        expected_manifest_mac=expected_manifest_mac,
    )
    records = inventory.get("records")
    if not isinstance(records, list):
        raise InventoryError("manifest_payload_invalid")
    review_records = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(
            record.get("path_token"), str
        ):
            raise InventoryError("manifest_payload_invalid")
        if record.get("category") not in REVIEW_REQUIRED_CATEGORIES:
            continue
        review_records.append(
            {
                "relative_path": decrypt_relative_path(keys, record["path_token"]),
                "path_id": record.get("path_id"),
                "category": record.get("category"),
                "size_bytes": record.get("size_bytes"),
                "hash_status": record.get("hash_status"),
                "sha256": record.get("sha256"),
            }
        )

    def require_unchanged_root() -> None:
        _require_current_root_identity(
            inventory_root,
            current_root_identity,
            reason_code="root_changed_during_review_write",
        )

    _write_private_json(
        review_path,
        {
            "contract_version": REVIEW_CONTRACT_VERSION,
            "key_id": keys.key_id,
            "source_manifest_mac": envelope["mac"],
            "records": review_records,
        },
        inventory_root=inventory_root,
        overwrite=overwrite,
        before_publish_check=require_unchanged_root,
        after_publish_check=require_unchanged_root,
    )
    return len(review_records)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inventory persistent data without exposing paths or file contents.",
    )
    parser.add_argument("--root", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--manifest-out", type=Path)
    mode.add_argument("--verify-manifest", type=Path)
    mode.add_argument("--review-manifest", type=Path)
    parser.add_argument("--review-out", type=Path)
    parser.add_argument("--key-id")
    parser.add_argument("--scope-id")
    parser.add_argument("--expected-manifest-mac")
    parser.add_argument("--manifest-key-env", default=DEFAULT_HMAC_KEY_ENV)
    parser.add_argument(
        "--redact-paths",
        action="store_true",
        help="Compatibility flag; public output is always path-redacted.",
    )
    parser.add_argument("--skip-content-hashes", action="store_true")
    parser.add_argument("--overwrite-private-output", action="store_true")
    return parser


def _status_exit_code(status_value: str) -> int:
    if status_value == "complete":
        return 0
    if status_value == "review_required":
        return 3
    return 2


def _print_failure(reason_code: str) -> None:
    print(
        json.dumps(
            {
                "contract_version": SUMMARY_CONTRACT_VERSION,
                "status": "failed",
                "inventory_gate_passed": False,
                "go_for_cutover": False,
                "reason_code": reason_code,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.root is None:
        parser.error("--root is required")
    if (
        args.overwrite_private_output
        and args.manifest_out is None
        and args.review_out is None
    ):
        parser.error(
            "--overwrite-private-output requires --manifest-out or --review-out"
        )
    if args.review_manifest is not None and args.review_out is None:
        parser.error("--review-manifest requires --review-out")
    if args.review_out is not None and args.review_manifest is None:
        parser.error("--review-out requires --review-manifest")
    if args.manifest_out is not None and args.skip_content_hashes:
        parser.error("--skip-content-hashes is not allowed with --manifest-out")

    needs_key = any(
        option is not None
        for option in (args.manifest_out, args.verify_manifest, args.review_manifest)
    )
    keys: ManifestKeys | None = None
    try:
        if needs_key:
            if not args.key_id:
                raise InventoryError("manifest_key_id_required")
            if not args.scope_id:
                raise InventoryError("manifest_scope_id_required")
            _validate_scope_id(args.scope_id)
            if (
                args.verify_manifest is not None or args.review_manifest is not None
            ) and not args.expected_manifest_mac:
                raise InventoryError("expected_manifest_mac_required")
            keys = parse_manifest_master_key(
                os.getenv(args.manifest_key_env, ""), args.key_id
            )
        if args.verify_manifest is not None:
            if keys is None:
                raise InventoryError("manifest_key_state_invalid")
            _, inventory, verified_root_identity = _load_and_verify_manifest(
                args.verify_manifest,
                keys=keys,
                inventory_root=args.root,
                expected_scope_id=args.scope_id,
                expected_manifest_mac=args.expected_manifest_mac,
            )
            _require_current_root_identity(
                args.root,
                verified_root_identity,
                reason_code="root_changed_before_verification_success",
            )
            print(
                json.dumps(
                    {
                        "contract_version": SUMMARY_CONTRACT_VERSION,
                        "status": "verified",
                        "inventory_gate_passed": inventory.get("status") == "complete",
                        "go_for_cutover": False,
                        "inventory_status": inventory.get("status"),
                        "files": inventory.get("summary", {}).get("files"),
                        "key_id": keys.key_id,
                        "scope_id": args.scope_id,
                        "manifest_mac": args.expected_manifest_mac,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return _status_exit_code(str(inventory.get("status")))
        if args.review_manifest is not None:
            if keys is None:
                raise InventoryError("manifest_key_state_invalid")
            count = write_operator_review(
                args.review_manifest,
                args.review_out,
                keys=keys,
                inventory_root=args.root,
                expected_scope_id=args.scope_id,
                expected_manifest_mac=args.expected_manifest_mac,
                overwrite=args.overwrite_private_output,
            )
            print(
                json.dumps(
                    {
                        "contract_version": SUMMARY_CONTRACT_VERSION,
                        "status": "review_written",
                        "inventory_gate_passed": False,
                        "go_for_cutover": False,
                        "review_records": count,
                        "key_id": keys.key_id,
                        "scope_id": args.scope_id,
                        "manifest_mac": args.expected_manifest_mac,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        inventory = build_inventory(
            args.root,
            manifest_keys=keys,
            scope_id=args.scope_id if keys is not None else None,
            include_content_hashes=not args.skip_content_hashes,
        )
        manifest_written = False
        manifest_mac: str | None = None
        if args.manifest_out is not None:
            if keys is None:
                raise InventoryError("manifest_key_state_invalid")
            manifest_mac = write_private_manifest(
                args.manifest_out,
                inventory,
                keys=keys,
                inventory_root=args.root,
                expected_scope_id=args.scope_id,
                overwrite=args.overwrite_private_output,
            )
            manifest_written = True
        print(
            json.dumps(
                public_summary(
                    inventory,
                    manifest_written=manifest_written,
                    manifest_mac=manifest_mac,
                ),
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return _status_exit_code(str(inventory["status"]))
    except (InventoryError, OSError) as exc:
        reason_code = (
            str(exc) if isinstance(exc, InventoryError) else "inventory_io_failed"
        )
        _print_failure(reason_code)
        return 2


if __name__ == "__main__":
    sys.exit(main())
