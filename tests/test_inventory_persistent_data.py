import base64
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path, PurePath
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from scripts.inventory_persistent_data import (
    INVENTORY_CONTRACT_VERSION,
    InventoryError,
    _descriptor_traversal_available,
    _encrypt_relative_path,
    _load_and_verify_manifest,
    _load_manifest,
    _path_id,
    _publish_no_replace,
    _scan_path_fallback,
    build_inventory,
    classify_relative_path,
    decrypt_relative_path,
    main,
    parse_manifest_master_key,
    public_summary,
    seal_manifest,
    verify_manifest,
    write_operator_review,
    write_private_manifest,
)


def _encoded_test_key(seed: bytes = b"inventory-test-key") -> str:
    value = hashlib.sha256(seed).digest()
    return "base64url:" + base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _test_keys(key_id: str = "inventory-2026-08"):
    return parse_manifest_master_key(_encoded_test_key(), key_id)


TEST_SCOPE_ID = "chatboc:test:disk-data-v1"
TEST_ROOT_IDENTITY = (101, 202)


def _scope(scope_id: str = TEST_SCOPE_ID, root_identity=TEST_ROOT_IDENTITY):
    return {
        "scope_id": scope_id,
        "root_device": root_identity[0],
        "root_inode": root_identity[1],
    }


def _manual_inventory(
    *,
    status: str = "complete",
    records=None,
    scope_id: str = TEST_SCOPE_ID,
    root_identity=TEST_ROOT_IDENTITY,
):
    return {
        "contract_version": INVENTORY_CONTRACT_VERSION,
        "generated_at": "2026-08-24T00:00:00+00:00",
        "scope": _scope(scope_id, root_identity),
        "status": status,
        "summary": {
            "files": len(records or []),
            "bytes": 0,
            "categories": {},
            "issues": {},
            "review_required_files": 0,
            "consistency_passes": 2,
            "content_hashes_requested": True,
            "content_hashes_verified": 0,
            "secret_hashes_skipped": 0,
        },
        "records": records or [],
    }


def _inventory_for_record(record: dict):
    category = record["category"]
    review_required = (
        1
        if category in {"media_review_required", "secret_review_required", "unknown"}
        else 0
    )
    secret = category in {"secret", "secret_review_required"}
    return {
        "contract_version": INVENTORY_CONTRACT_VERSION,
        "generated_at": "2026-08-24T00:00:00+00:00",
        "scope": _scope(),
        "status": "review_required" if review_required else "complete",
        "summary": {
            "files": 1,
            "bytes": record["size_bytes"],
            "categories": {category: {"files": 1, "bytes": record["size_bytes"]}},
            "issues": {},
            "review_required_files": review_required,
            "consistency_passes": 2,
            "content_hashes_requested": True,
            "content_hashes_verified": 0 if secret else 1,
            "secret_hashes_skipped": 1 if secret else 0,
        },
        "records": [record],
    }


def _verify(
    envelope: dict,
    keys,
    *,
    scope_id: str = TEST_SCOPE_ID,
    root_identity=TEST_ROOT_IDENTITY,
    expected_manifest_mac: str | None = None,
):
    return verify_manifest(
        envelope,
        keys,
        expected_scope_id=scope_id,
        expected_root_identity=root_identity,
        expected_manifest_mac=expected_manifest_mac or envelope["mac"],
    )


def _bind_inventory_to_root(inventory: dict, root: Path) -> dict:
    metadata = root.lstat()
    inventory["scope"] = _scope(TEST_SCOPE_ID, (metadata.st_dev, metadata.st_ino))
    return inventory


def _create_directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode != 0:
            raise OSError("junction unavailable")
    else:
        link.symlink_to(target, target_is_directory=True)


def _remove_directory_link(link: Path) -> None:
    if link.is_symlink():
        link.unlink()
    elif link.exists() or getattr(os.path, "isjunction", lambda _: False)(link):
        os.rmdir(link)


class PersistentDataInventoryTests(unittest.TestCase):
    def test_classification_is_conservative_for_sensitive_and_review_paths(self):
        secret_paths = (
            PurePath("vision_service_key.json"),
            PurePath("google_service_key.json"),
            PurePath(".env.production"),
            PurePath("cache", "auth-token.json"),
            PurePath("secrets", "token.txt"),
            PurePath("client_secret.json"),
            PurePath("jwt_secret.txt"),
            PurePath("private_key.json"),
            PurePath("secret.json"),
            PurePath(".npmrc"),
            PurePath(".netrc"),
            PurePath("id_rsa"),
            PurePath(".docker", "config.json"),
            PurePath("keys", "signing.ppk"),
        )
        for path in secret_paths:
            with self.subTest(path=path):
                self.assertEqual(classify_relative_path(path), "secret")
        self.assertEqual(
            classify_relative_path(PurePath("database.db-wal")), "database"
        )
        self.assertEqual(
            classify_relative_path(PurePath("database.sqlite3-wal")), "database"
        )
        self.assertEqual(
            classify_relative_path(PurePath("uploads", "photo.jpg")),
            "media_review_required",
        )
        self.assertEqual(
            classify_relative_path(PurePath("uploads", "uuid_password.jpg")),
            "secret_review_required",
        )
        self.assertEqual(
            classify_relative_path(PurePath("uploads", "uuid_client_secret.json")),
            "secret_review_required",
        )
        self.assertEqual(
            classify_relative_path(PurePath("flyers", "promo.png")),
            "media_review_required",
        )
        self.assertEqual(
            classify_relative_path(PurePath("tenants", "acme", "profile.json")),
            "tenant_config",
        )
        self.assertEqual(
            classify_relative_path(PurePath("cache", "voice.mp3")), "cache_or_temp"
        )
        self.assertEqual(
            classify_relative_path(PurePath("unclassified.bin")), "unknown"
        )

    def test_generated_key_format_key_id_and_domain_separation(self):
        keys = _test_keys()
        self.assertEqual(keys.key_id, "inventory-2026-08")
        self.assertEqual(len(keys.path_id_key), 32)
        self.assertEqual(len(keys.path_encryption_key), 32)
        self.assertEqual(len(keys.manifest_mac_key), 32)
        self.assertEqual(
            len({keys.path_id_key, keys.path_encryption_key, keys.manifest_mac_key}), 3
        )

    def test_handcrafted_or_malformed_keys_fail_closed(self):
        weak = "base64url:" + base64.urlsafe_b64encode(b"a" * 32).rstrip(b"=").decode()
        sequential = (
            "base64url:"
            + base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
        )
        cases = (
            (
                "plain-text-example",
                "inventory-2026-08",
                "manifest_master_key_format_invalid",
            ),
            (weak, "inventory-2026-08", "manifest_master_key_randomness_unverified"),
            (
                sequential,
                "inventory-2026-08",
                "manifest_master_key_randomness_unverified",
            ),
            (_encoded_test_key(), "short", "manifest_key_id_invalid"),
        )
        for raw, key_id, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(
                InventoryError, reason
            ):
                parse_manifest_master_key(raw, key_id)

    def test_key_rotation_changes_all_derived_keys(self):
        first = _test_keys("inventory-2026-08")
        second = _test_keys("inventory-2026-09")
        self.assertNotEqual(first.path_id_key, second.path_id_key)
        self.assertNotEqual(first.path_encryption_key, second.path_encryption_key)
        self.assertNotEqual(first.manifest_mac_key, second.manifest_mac_key)

    def test_encrypted_path_is_reviewable_only_with_matching_key(self):
        keys = _test_keys()
        path = PurePath("uploads", "citizen-name.jpg")
        token = _encrypt_relative_path(keys, path)
        self.assertNotIn("citizen", token)
        self.assertEqual(decrypt_relative_path(keys, token), path.as_posix())
        with self.assertRaisesRegex(InventoryError, "manifest_path_token_invalid"):
            decrypt_relative_path(_test_keys("inventory-2026-09"), token)

    def test_manifest_mac_covers_payload_header_and_rejects_extra_fields(self):
        keys = _test_keys()
        envelope = seal_manifest(_manual_inventory(), keys)
        self.assertEqual(_verify(envelope, keys)["status"], "complete")

        tampered_payload = dict(envelope)
        last = tampered_payload["signed_payload"][-1]
        tampered_payload["signed_payload"] = tampered_payload["signed_payload"][:-1] + (
            "A" if last != "A" else "B"
        )
        with self.assertRaisesRegex(
            InventoryError, "manifest_integrity_check_failed|manifest_payload_invalid"
        ):
            _verify(tampered_payload, keys, expected_manifest_mac=envelope["mac"])

        tampered_header = dict(envelope, key_id="inventory-2026-09")
        with self.assertRaisesRegex(
            InventoryError, "manifest_key_or_contract_mismatch"
        ):
            _verify(tampered_header, keys, expected_manifest_mac=envelope["mac"])

        with_extra = dict(envelope, unauthenticated="value")
        with self.assertRaisesRegex(InventoryError, "manifest_envelope_invalid"):
            _verify(with_extra, keys, expected_manifest_mac=envelope["mac"])

    def test_manifest_replay_requires_expected_mac_scope_and_current_root_identity(
        self,
    ):
        keys = _test_keys()
        envelope = seal_manifest(_manual_inventory(), keys)
        with self.assertRaisesRegex(InventoryError, "manifest_expected_mac_mismatch"):
            _verify(envelope, keys, expected_manifest_mac="0" * 64)
        with self.assertRaisesRegex(InventoryError, "manifest_scope_mismatch"):
            _verify(envelope, keys, scope_id="chatboc:staging:disk-data-v1")
        with self.assertRaisesRegex(InventoryError, "manifest_scope_mismatch"):
            _verify(envelope, keys, root_identity=(303, 404))

    @unittest.skipUnless(os.name == "posix", "POSIX private manifest input")
    def test_manifest_parser_rejects_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "data"
            private = base / "private"
            root.mkdir()
            private.mkdir(mode=0o700)
            path = private / "manifest.json"
            path.write_text('{"key_id":"first","key_id":"second"}', encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaisesRegex(InventoryError, "manifest_input_invalid"):
                _load_manifest(path, inventory_root=root)

    def test_manifest_input_inside_inventory_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "manifest.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(
                InventoryError, "manifest_input_must_be_outside_inventory_root"
            ):
                _load_manifest(path, inventory_root=root)

    def test_manifest_input_fails_closed_without_posix_acl_contract(self):
        if os.name == "posix" and _descriptor_traversal_available():
            self.skipTest("platform can prove the private manifest input contract")
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "data"
            root.mkdir()
            path = base / "manifest.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(
                InventoryError, "manifest_input_acl_unverified"
            ):
                _load_manifest(path, inventory_root=root)

    def test_manifest_boundary_rejects_caller_supplied_plaintext_path_record(self):
        keys = _test_keys()
        inventory = _manual_inventory(
            records=[
                {
                    "relative_path": "uploads/citizen-name.jpg",
                    "category": "media_review_required",
                }
            ]
        )
        envelope = seal_manifest(inventory, keys)
        with self.assertRaisesRegex(
            InventoryError, "manifest_record_structure_invalid"
        ):
            _verify(envelope, keys)

    def test_manifest_boundary_reconciles_encrypted_identity_and_secret_hash_policy(
        self,
    ):
        keys = _test_keys()
        relative = PurePath("tenants", "acme", "config.yaml")
        record = {
            "path_id": _path_id(keys, relative),
            "path_token": _encrypt_relative_path(keys, relative),
            "category": "tenant_config",
            "size_bytes": 7,
            "mtime_ns": 0,
            "mime_type": "application/yaml",
            "sha256": "a" * 64,
            "hash_status": "verified",
        }
        inventory = _inventory_for_record(record)
        self.assertEqual(_verify(seal_manifest(inventory, keys), keys), inventory)

        wrong_category = dict(record, category="unknown")
        with self.assertRaisesRegex(InventoryError, "manifest_record_identity_invalid"):
            _verify(seal_manifest(_inventory_for_record(wrong_category), keys), keys)

        secret_path = PurePath("google_service_key.json")
        hashed_secret = {
            **record,
            "path_id": _path_id(keys, secret_path),
            "path_token": _encrypt_relative_path(keys, secret_path),
            "category": "secret",
        }
        with self.assertRaisesRegex(
            InventoryError, "manifest_secret_hash_policy_invalid"
        ):
            _verify(seal_manifest(_inventory_for_record(hashed_secret), keys), keys)

        review_secret_path = PurePath("uploads", "uuid_password.jpg")
        review_secret = {
            **record,
            "path_id": _path_id(keys, review_secret_path),
            "path_token": _encrypt_relative_path(keys, review_secret_path),
            "category": "secret_review_required",
            "sha256": None,
            "hash_status": "skipped_secret",
        }
        reviewed_inventory = _inventory_for_record(review_secret)
        self.assertEqual(
            _verify(seal_manifest(reviewed_inventory, keys), keys), reviewed_inventory
        )

    def test_dry_run_summary_never_contains_plaintext_paths(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "uploads").mkdir()
            (root / "uploads" / "citizen-name.jpg").write_bytes(b"image")
            (root / "vision_service_key.json").write_text(
                "super-secret", encoding="utf-8"
            )
            (root / "database.db").write_bytes(b"sqlite")

            inventory = build_inventory(root)
            summary = public_summary(inventory, manifest_written=False)
            serialized = json.dumps(summary)

            expected_status = (
                "review_required" if _descriptor_traversal_available() else "unsafe"
            )
            self.assertEqual(summary["status"], expected_status)
            self.assertFalse(summary["go_for_cutover"])
            self.assertEqual(summary["files"], 3)
            self.assertEqual(summary["categories"]["secret"]["files"], 1)
            self.assertEqual(summary["categories"]["media_review_required"]["files"], 1)
            self.assertEqual(inventory["records"], [])
            self.assertNotIn("citizen-name", serialized)
            self.assertNotIn("vision_service_key", serialized)
            self.assertNotIn(str(root), serialized)

    def test_complete_inventory_gate_still_cannot_authorize_cutover_alone(self):
        summary = public_summary(_manual_inventory(), manifest_written=False)
        self.assertTrue(summary["inventory_gate_passed"])
        self.assertFalse(summary["go_for_cutover"])

    def test_unknown_is_never_complete_and_cli_returns_nonzero_without_path_leak(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            filename = "operator-private-unknown.bin"
            (root / filename).write_bytes(b"unknown")
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(["--root", str(root), "--dry-run", "--redact-paths"])
            public = json.loads(output.getvalue())

            self.assertNotEqual(exit_code, 0)
            self.assertFalse(public["go_for_cutover"])
            self.assertEqual(public["categories"]["unknown"]["files"], 1)
            self.assertNotIn(filename, output.getvalue())
            self.assertNotIn(str(root), output.getvalue())

    def test_cli_entrypoint_emits_one_redacted_json_line(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            filename = "citizen-private-unknown.bin"
            (root / filename).write_bytes(b"unknown")
            script = (
                Path(__file__).parents[1] / "scripts" / "inventory_persistent_data.py"
            )
            result = subprocess.run(
                [sys.executable, str(script), "--root", str(root), "--dry-run"],
                capture_output=True,
                check=False,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stderr, "")
            self.assertEqual(len(result.stdout.splitlines()), 1)
            self.assertIsInstance(json.loads(result.stdout), dict)
            self.assertNotIn(filename, result.stdout)
            self.assertNotIn(str(root), result.stdout)

    def test_cli_manifest_modes_require_scope_and_independent_expected_mac(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "data"
            root.mkdir()
            private_name = "operator-private-manifest.json"
            manifest = base / private_name
            environment = {"STORAGE_INVENTORY_MASTER_KEY": _encoded_test_key()}

            output = io.StringIO()
            with mock.patch.dict(os.environ, environment, clear=False), redirect_stdout(
                output
            ):
                exit_code = main(
                    [
                        "--root",
                        str(root),
                        "--manifest-out",
                        str(manifest),
                        "--key-id",
                        "inventory-2026-08",
                    ]
                )
            self.assertEqual(exit_code, 2)
            self.assertEqual(
                json.loads(output.getvalue())["reason_code"],
                "manifest_scope_id_required",
            )
            self.assertNotIn(private_name, output.getvalue())
            self.assertNotIn(str(root), output.getvalue())

            output = io.StringIO()
            with mock.patch.dict(os.environ, environment, clear=False), redirect_stdout(
                output
            ):
                exit_code = main(
                    [
                        "--root",
                        str(root),
                        "--verify-manifest",
                        str(manifest),
                        "--key-id",
                        "inventory-2026-08",
                        "--scope-id",
                        TEST_SCOPE_ID,
                    ]
                )
            self.assertEqual(exit_code, 2)
            self.assertEqual(
                json.loads(output.getvalue())["reason_code"],
                "expected_manifest_mac_required",
            )
            self.assertNotIn(private_name, output.getvalue())
            self.assertNotIn(str(root), output.getvalue())

    def test_manifest_build_fails_closed_without_descriptor_relative_traversal(self):
        if _descriptor_traversal_available():
            self.skipTest("platform has the required descriptor-relative traversal")
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(
                InventoryError, "secure_root_identity_unavailable"
            ):
                build_inventory(
                    Path(temporary_directory),
                    manifest_keys=_test_keys(),
                    scope_id=TEST_SCOPE_ID,
                )

    def test_windows_private_output_fails_closed_when_acl_cannot_be_proven(self):
        if os.name != "nt":
            self.skipTest("Windows-specific fail-closed ACL policy")
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "data"
            root.mkdir()
            output = base / "manifest.json"
            with self.assertRaisesRegex(
                InventoryError, "private_output_acl_unverified"
            ):
                write_private_manifest(
                    output,
                    _bind_inventory_to_root(_manual_inventory(), root),
                    keys=_test_keys(),
                    inventory_root=root,
                    expected_scope_id=TEST_SCOPE_ID,
                )
            self.assertFalse(output.exists())

    def test_private_manifest_must_be_outside_inventory_root(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "manifest.json"
            with self.assertRaisesRegex(
                InventoryError, "private_output_must_be_outside_inventory_root"
            ):
                write_private_manifest(
                    output,
                    _bind_inventory_to_root(_manual_inventory(), root),
                    keys=_test_keys(),
                    inventory_root=root,
                    expected_scope_id=TEST_SCOPE_ID,
                )
            self.assertFalse(output.exists())

    def test_existing_private_target_never_overwritten_concurrently(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            first = base / "first.tmp"
            second = base / "second.tmp"
            output = base / "manifest.json"
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")
            barrier = threading.Barrier(2)
            results: list[str] = []

            def publish(source: Path) -> None:
                barrier.wait(timeout=5)
                try:
                    _publish_no_replace(None, source, output)
                    results.append("published")
                except InventoryError as exc:
                    results.append(str(exc))

            threads = [
                threading.Thread(target=publish, args=(first,)),
                threading.Thread(target=publish, args=(second,)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertEqual(results.count("published"), 1)
            self.assertEqual(results.count("private_output_exists"), 1)
            self.assertIn(output.read_text(encoding="utf-8"), {"first", "second"})

    def test_outside_directory_link_is_skipped_and_never_counted(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "root"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "outside-secret.txt").write_text("do-not-read", encoding="utf-8")
            link = root / "escape"
            try:
                _create_directory_link(link, outside)
            except OSError as exc:
                self.skipTest(str(exc))
            try:
                inventory = build_inventory(root)
                self.assertEqual(inventory["summary"]["files"], 0)
                issues = inventory["summary"]["issues"]
                self.assertTrue(
                    issues.get("symlink_skipped", 0)
                    or issues.get("symlink_or_reparse_skipped", 0)
                )
                self.assertEqual(inventory["status"], "unsafe")
            finally:
                _remove_directory_link(link)

    def test_link_or_junction_root_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            target = base / "target"
            target.mkdir()
            link = base / "root-link"
            try:
                _create_directory_link(link, target)
            except OSError as exc:
                self.skipTest(str(exc))
            try:
                with self.assertRaisesRegex(
                    InventoryError,
                    "root_ancestry_unsafe|root_must_not_be_symlink_or_reparse",
                ):
                    build_inventory(link)
            finally:
                _remove_directory_link(link)

    def test_root_identity_change_is_reported_unsafe(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with mock.patch(
                "scripts.inventory_persistent_data._identity",
                side_effect=[(1, 1), (2, 2), (1, 1)],
            ):
                result = _scan_path_fallback(root)
            self.assertEqual(result.issues.get("root_changed_during_inventory"), 1)

    def test_private_output_parent_link_is_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "root"
            destination = base / "destination"
            root.mkdir()
            destination.mkdir()
            link = base / "linked-output"
            try:
                _create_directory_link(link, destination)
            except OSError as exc:
                self.skipTest(str(exc))
            try:
                with self.assertRaisesRegex(
                    InventoryError, "private_output_ancestry_unsafe"
                ):
                    write_private_manifest(
                        link / "manifest.json",
                        _bind_inventory_to_root(_manual_inventory(), root),
                        keys=_test_keys(),
                        inventory_root=root,
                        expected_scope_id=TEST_SCOPE_ID,
                    )
                self.assertFalse((destination / "manifest.json").exists())
            finally:
                _remove_directory_link(link)

    @unittest.skipUnless(os.name == "posix", "POSIX descriptor-relative input")
    def test_private_manifest_input_parent_link_is_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "root"
            destination = base / "destination"
            root.mkdir()
            destination.mkdir(mode=0o700)
            manifest = destination / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            manifest.chmod(0o600)
            link = base / "linked-input"
            link.symlink_to(destination, target_is_directory=True)
            with self.assertRaisesRegex(
                InventoryError, "manifest_input_ancestry_unsafe"
            ):
                _load_manifest(link / manifest.name, inventory_root=root)

    @unittest.skipUnless(os.name == "posix", "POSIX concurrent output cleanup")
    def test_concurrent_private_target_replacement_is_never_deleted_after_publish(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "root"
            private = base / "private"
            root.mkdir()
            private.mkdir(mode=0o700)
            output = private / "manifest.json"
            original_stat = os.stat
            replaced = False

            def replace_before_post_publish_stat(
                path, *, dir_fd=None, follow_symlinks=True
            ):
                nonlocal replaced
                if path == output.name and dir_fd is not None and not replaced:
                    replaced = True
                    os.unlink(path, dir_fd=dir_fd)
                    replacement_fd = os.open(
                        path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=dir_fd,
                    )
                    try:
                        os.write(replacement_fd, b"concurrent-target")
                        os.fsync(replacement_fd)
                    finally:
                        os.close(replacement_fd)
                return original_stat(
                    path, dir_fd=dir_fd, follow_symlinks=follow_symlinks
                )

            with mock.patch(
                "scripts.inventory_persistent_data._descriptor_traversal_available",
                return_value=True,
            ), mock.patch(
                "scripts.inventory_persistent_data.os.stat",
                side_effect=replace_before_post_publish_stat,
            ), self.assertRaisesRegex(
                InventoryError, "private_output_permissions_unverified"
            ):
                write_private_manifest(
                    output,
                    _bind_inventory_to_root(_manual_inventory(), root),
                    keys=_test_keys(),
                    inventory_root=root,
                    expected_scope_id=TEST_SCOPE_ID,
                )

            self.assertTrue(replaced)
            self.assertEqual(output.read_bytes(), b"concurrent-target")

    def test_unsafe_inventory_is_refused_before_any_manifest_write(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "root"
            root.mkdir()
            output = base / "manifest.json"
            with self.assertRaisesRegex(
                InventoryError, "unsafe_inventory_manifest_refused"
            ):
                write_private_manifest(
                    output,
                    _manual_inventory(status="unsafe"),
                    keys=_test_keys(),
                    inventory_root=root,
                    expected_scope_id=TEST_SCOPE_ID,
                )
            self.assertFalse(output.exists())

    def test_explicit_overwrite_is_also_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "root"
            root.mkdir()
            output = base / "manifest.json"
            output.write_text("preserve-me", encoding="utf-8")
            with self.assertRaisesRegex(
                InventoryError, "private_output_overwrite_not_supported"
            ):
                write_private_manifest(
                    output,
                    _bind_inventory_to_root(_manual_inventory(), root),
                    keys=_test_keys(),
                    inventory_root=root,
                    expected_scope_id=TEST_SCOPE_ID,
                    overwrite=True,
                )
            self.assertEqual(output.read_text(encoding="utf-8"), "preserve-me")

    @unittest.skipUnless(os.name == "posix", "POSIX permissions and dir-fsync contract")
    def test_posix_manifest_is_private_verified_and_non_clobbering(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "data"
            output_dir = base / "private"
            root.mkdir()
            output_dir.mkdir()
            output_dir.chmod(0o700)
            output = output_dir / "manifest.json"
            keys = _test_keys()
            inventory = _bind_inventory_to_root(_manual_inventory(), root)
            manifest_mac = write_private_manifest(
                output,
                inventory,
                keys=keys,
                inventory_root=root,
                expected_scope_id=TEST_SCOPE_ID,
            )
            self.assertEqual(oct(output.stat().st_mode & 0o777), "0o600")
            self.assertEqual(
                _verify(
                    json.loads(output.read_text()),
                    keys,
                    scope_id=TEST_SCOPE_ID,
                    root_identity=(root.stat().st_dev, root.stat().st_ino),
                    expected_manifest_mac=manifest_mac,
                ),
                inventory,
            )
            with self.assertRaisesRegex(InventoryError, "private_output_exists"):
                write_private_manifest(
                    output,
                    inventory,
                    keys=keys,
                    inventory_root=root,
                    expected_scope_id=TEST_SCOPE_ID,
                )

    @unittest.skipUnless(os.name == "posix", "POSIX hard-link rejection")
    def test_posix_hardlinked_secret_and_media_fail_closed_before_inventory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "data"
            uploads = root / "uploads"
            root.mkdir()
            uploads.mkdir()
            secret = root / "google_service_key.json"
            secret_alias = root / "service_account.json"
            media = uploads / "citizen-photo.jpg"
            media_alias = uploads / "citizen-photo-copy.jpg"
            secret.write_bytes(b"must-never-be-read-or-recorded")
            media.write_bytes(b"private-media")
            os.link(secret, secret_alias)
            os.link(media, media_alias)

            result = build_inventory(
                root,
                manifest_keys=_test_keys(),
                scope_id=TEST_SCOPE_ID,
            )

            self.assertEqual(result["status"], "unsafe")
            self.assertEqual(result["records"], [])
            self.assertEqual(result["summary"]["files"], 0)
            self.assertGreaterEqual(
                result["summary"]["issues"].get("hardlinked_file_skipped", 0),
                4,
            )
            self.assertNotIn(
                "must-never-be-read-or-recorded",
                json.dumps(result),
            )

    @unittest.skipUnless(os.name == "posix", "POSIX root-bound publication")
    def test_posix_root_swap_during_manifest_write_never_returns_an_approved_mac(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "data"
            private = base / "private"
            root.mkdir()
            private.mkdir(mode=0o700)
            output = private / "manifest.json"
            inventory = _bind_inventory_to_root(_manual_inventory(), root)
            root_identity = (root.stat().st_dev, root.stat().st_ino)
            changed_identity = (root_identity[0], root_identity[1] + 1)

            with mock.patch(
                "scripts.inventory_persistent_data._capture_current_root_identity",
                side_effect=[root_identity, root_identity, changed_identity],
            ), self.assertRaisesRegex(
                InventoryError, "root_changed_during_manifest_write"
            ):
                write_private_manifest(
                    output,
                    inventory,
                    keys=_test_keys(),
                    inventory_root=root,
                    expected_scope_id=TEST_SCOPE_ID,
                )

            self.assertTrue(output.exists())
            self.assertEqual(oct(output.stat().st_mode & 0o777), "0o600")

    @unittest.skipUnless(os.name == "posix", "POSIX root-bound manifest load")
    def test_posix_root_swap_during_manifest_load_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "data"
            parked_root = base / "parked-data"
            private = base / "private"
            root.mkdir()
            private.mkdir(mode=0o700)
            output = private / "manifest.json"
            keys = _test_keys()
            inventory = _bind_inventory_to_root(_manual_inventory(), root)
            manifest_mac = write_private_manifest(
                output,
                inventory,
                keys=keys,
                inventory_root=root,
                expected_scope_id=TEST_SCOPE_ID,
            )
            original_load = _load_manifest

            def load_then_swap(path: Path, *, inventory_root: Path):
                envelope = original_load(path, inventory_root=inventory_root)
                root.rename(parked_root)
                root.mkdir()
                return envelope

            try:
                with mock.patch(
                    "scripts.inventory_persistent_data._load_manifest",
                    side_effect=load_then_swap,
                ), self.assertRaisesRegex(
                    InventoryError, "root_changed_during_manifest_load"
                ):
                    _load_and_verify_manifest(
                        output,
                        keys=keys,
                        inventory_root=root,
                        expected_scope_id=TEST_SCOPE_ID,
                        expected_manifest_mac=manifest_mac,
                    )
            finally:
                if root.exists():
                    root.rmdir()
                if parked_root.exists():
                    parked_root.rename(root)

    @unittest.skipUnless(os.name == "posix", "POSIX root-bound review output")
    def test_posix_root_swap_during_review_write_never_reports_success(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "data"
            private = base / "private"
            root.mkdir()
            private.mkdir(mode=0o700)
            (root / "unknown.bin").write_bytes(b"review")
            keys = _test_keys()
            inventory = build_inventory(
                root, manifest_keys=keys, scope_id=TEST_SCOPE_ID
            )
            root_identity = (root.stat().st_dev, root.stat().st_ino)
            changed_identity = (root_identity[0], root_identity[1] + 1)
            manifest = private / "manifest.json"
            review = private / "review.json"
            manifest_mac = write_private_manifest(
                manifest,
                inventory,
                keys=keys,
                inventory_root=root,
                expected_scope_id=TEST_SCOPE_ID,
            )

            with mock.patch(
                "scripts.inventory_persistent_data._capture_current_root_identity",
                side_effect=[
                    root_identity,
                    root_identity,
                    root_identity,
                    root_identity,
                    changed_identity,
                ],
            ), self.assertRaisesRegex(
                InventoryError, "root_changed_during_review_write"
            ):
                write_operator_review(
                    manifest,
                    review,
                    keys=keys,
                    inventory_root=root,
                    expected_scope_id=TEST_SCOPE_ID,
                    expected_manifest_mac=manifest_mac,
                )

            self.assertTrue(review.exists())
            self.assertEqual(oct(review.stat().st_mode & 0o777), "0o600")

    @unittest.skipUnless(os.name == "posix", "POSIX descriptor-relative traversal")
    def test_posix_secret_is_not_hashed_and_manifest_has_no_plaintext_path(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "data"
            output_dir = base / "private"
            root.mkdir()
            output_dir.mkdir(mode=0o700)
            secret_names = (
                "google_service_key.json",
                "client_secret.json",
                ".env.production",
                ".npmrc",
            )
            for secret_name in secret_names:
                (root / secret_name).write_text(
                    f"credential-content:{secret_name}", encoding="utf-8"
                )
            (root / ".docker").mkdir()
            (root / ".docker" / "config.json").write_text(
                "docker-credential-content", encoding="utf-8"
            )
            (root / "tenants").mkdir()
            (root / "tenants" / "config.yaml").write_text(
                "enabled: true", encoding="utf-8"
            )
            (root / "uploads").mkdir()
            upload_secret_names = (
                "uuid_password.jpg",
                "uuid_client_secret.json",
            )
            for secret_name in upload_secret_names:
                (root / "uploads" / secret_name).write_text(
                    f"uploaded-secret:{secret_name}", encoding="utf-8"
                )
            keys = _test_keys()

            inventory = build_inventory(
                root, manifest_keys=keys, scope_id=TEST_SCOPE_ID
            )
            secret_records = [
                record
                for record in inventory["records"]
                if record["category"] in {"secret", "secret_review_required"}
            ]
            self.assertEqual(len(secret_records), 7)
            self.assertEqual(inventory["summary"]["categories"]["secret"]["files"], 5)
            self.assertEqual(
                inventory["summary"]["categories"]["secret_review_required"]["files"],
                2,
            )
            self.assertEqual(inventory["status"], "review_required")
            for record in secret_records:
                self.assertIsNone(record["sha256"])
                self.assertEqual(record["hash_status"], "skipped_secret")
            output = output_dir / "manifest.json"
            manifest_mac = write_private_manifest(
                output,
                inventory,
                keys=keys,
                inventory_root=root,
                expected_scope_id=TEST_SCOPE_ID,
            )
            serialized = output.read_text(encoding="utf-8")
            for secret_name in (*secret_names, *upload_secret_names, "config.json"):
                self.assertNotIn(secret_name, serialized)
            self.assertNotIn("credential-content", serialized)
            self.assertNotIn("uploaded-secret", serialized)
            self.assertEqual(
                _verify(
                    json.loads(serialized),
                    keys,
                    scope_id=TEST_SCOPE_ID,
                    root_identity=(root.stat().st_dev, root.stat().st_ino),
                    expected_manifest_mac=manifest_mac,
                ),
                inventory,
            )

    @unittest.skipUnless(os.name == "posix", "POSIX descriptor-relative traversal")
    def test_posix_stat_to_open_symlink_swap_never_follows_outside_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "root"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            inside = root / "inside"
            inside.mkdir()
            (inside / "inside.yaml").write_text("safe: true", encoding="utf-8")
            (outside / "outside-secret.bin").write_bytes(b"do-not-read")
            original_open = os.open
            swapped = False

            def swap_before_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if path == "inside" and dir_fd is not None and not swapped:
                    swapped = True
                    inside.rename(root / "parked")
                    inside.symlink_to(outside, target_is_directory=True)
                return original_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch(
                "scripts.inventory_persistent_data._descriptor_traversal_available",
                return_value=True,
            ), mock.patch(
                "scripts.inventory_persistent_data.os.open",
                side_effect=swap_before_open,
            ):
                inventory = build_inventory(root)

            self.assertTrue(swapped)
            self.assertEqual(inventory["status"], "unsafe")
            self.assertLessEqual(inventory["summary"]["files"], 1)
            self.assertTrue(
                inventory["summary"]["issues"].get("directory_open_failed", 0)
                or inventory["summary"]["issues"].get("symlink_skipped", 0)
            )

    @unittest.skipUnless(os.name == "posix", "POSIX root identity anchoring")
    def test_posix_persistent_root_swap_before_open_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "root"
            original = base / "original-root"
            root.mkdir()
            (root / "database.db").write_bytes(b"authoritative")
            original_open = os.open
            swapped = False

            def swap_root_before_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if path == root.name and dir_fd is not None and not swapped:
                    swapped = True
                    root.rename(original)
                    root.mkdir()
                    (root / "decoy.yaml").write_text("decoy: true", encoding="utf-8")
                return original_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch(
                "scripts.inventory_persistent_data._descriptor_traversal_available",
                return_value=True,
            ), mock.patch(
                "scripts.inventory_persistent_data.os.open",
                side_effect=swap_root_before_open,
            ):
                with self.assertRaisesRegex(InventoryError, "root_changed_before_scan"):
                    build_inventory(root)

            self.assertTrue(swapped)

    @unittest.skipUnless(os.name == "posix", "POSIX operator-only review output")
    def test_posix_review_required_manifest_can_be_mapped_without_stdout_paths(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "data"
            private = base / "private"
            root.mkdir()
            private.mkdir(mode=0o700)
            (root / "uploads").mkdir()
            relative = "uploads/citizen-name.jpg"
            secret_name = "google_service_key.json"
            (root / "uploads" / "citizen-name.jpg").write_bytes(b"image")
            (root / secret_name).write_text("credential", encoding="utf-8")
            keys = _test_keys()
            inventory = build_inventory(
                root, manifest_keys=keys, scope_id=TEST_SCOPE_ID
            )
            self.assertEqual(inventory["status"], "review_required")

            manifest_path = private / "manifest.json"
            review_path = private / "review.json"
            manifest_mac = write_private_manifest(
                manifest_path,
                inventory,
                keys=keys,
                inventory_root=root,
                expected_scope_id=TEST_SCOPE_ID,
            )
            self.assertNotIn(relative, manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                write_operator_review(
                    manifest_path,
                    review_path,
                    keys=keys,
                    inventory_root=root,
                    expected_scope_id=TEST_SCOPE_ID,
                    expected_manifest_mac=manifest_mac,
                ),
                1,
            )
            self.assertEqual(oct(review_path.stat().st_mode & 0o777), "0o600")
            self.assertIn(relative, review_path.read_text(encoding="utf-8"))
            self.assertNotIn(secret_name, review_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
