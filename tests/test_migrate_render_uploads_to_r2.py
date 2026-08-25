import base64
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import unittest
from unittest import mock

from scripts import inventory_persistent_data as inventory_tools
from scripts import migrate_render_uploads_to_r2 as migration
from scripts.migrate_render_uploads_to_r2 import (
    ApprovedReference,
    MigrationError,
    PosixManifestSourceReader,
    _content_md5,
    _ledger_payload,
    _load_r2_destination,
    _parser,
    destination_key_for,
    migrate_approved_objects,
    normalize_r2_destination,
    seal_approved_reference_map,
    seal_ledger,
    validate_approved_reference_map,
    verify_ledger,
)


TEST_SCOPE = "chatboc:test:backend:disk-data-v1"
TEST_MANIFEST_MAC = "f" * 64
TEST_BUCKET = "private-test-bucket"
TEST_ACCOUNT_ID = "a" * 32
TEST_ENDPOINT = f"https://{TEST_ACCOUNT_ID}.r2.cloudflarestorage.com"


def _encoded_test_key() -> str:
    raw = hashlib.sha256(b"render-r2-migration-test-key").digest()
    return "base64url:" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _keys():
    return inventory_tools.parse_manifest_master_key(
        _encoded_test_key(), "inventory-2026-08-r1"
    )


def _record(payload: bytes = b"approved upload", path_id: str = "a" * 64):
    return {
        "path_id": path_id,
        "path_token": "unused-by-fake-reader",
        "category": "media_review_required",
        "size_bytes": len(payload),
        "mtime_ns": 123456789,
        "mime_type": "image/png",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "hash_status": "verified",
    }


def _approved_pair(payload: bytes = b"approved upload"):
    record = _record(payload)
    tenant = "municipio-junin"
    reference = ApprovedReference(
        path_id=record["path_id"],
        tenant_slug=tenant,
        destination_key=destination_key_for(record["path_id"], tenant),
    )
    return record, reference


def _map_payload_for(records, *, status="approved", reference_status="approved"):
    references = []
    for record in records:
        tenant = "municipio-junin"
        references.append(
            {
                "path_id": record["path_id"],
                "tenant_slug": tenant,
                "destination_key": destination_key_for(record["path_id"], tenant),
                "approval_status": reference_status,
            }
        )
    return {
        "scope_id": TEST_SCOPE,
        "source_manifest_mac": TEST_MANIFEST_MAC,
        "approval_status": status,
        "references": references,
    }


def _map_for(records, *, status="approved", reference_status="approved"):
    return seal_approved_reference_map(
        _map_payload_for(
            records,
            status=status,
            reference_status=reference_status,
        ),
        _keys(),
    )


class FakeClientError(Exception):
    def __init__(self, code: str, status: int):
        super().__init__("provider detail must never be logged")
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.head_calls = []
        self.get_calls = []
        self.put_calls = []
        self.delete_calls = []
        self.race_payload = None
        self.corrupt_put = False

    def head_object(self, *, Bucket, Key):
        self.head_calls.append((Bucket, Key))
        if Key not in self.objects:
            raise FakeClientError("NoSuchKey", 404)
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, *, Bucket, Key):
        self.get_calls.append((Bucket, Key))
        if Key not in self.objects:
            raise FakeClientError("NoSuchKey", 404)
        payload = self.objects[Key]
        return {"Body": io.BytesIO(payload), "ContentLength": len(payload)}

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        key = kwargs["Key"]
        payload = kwargs["Body"].read()
        if self.race_payload is not None:
            self.objects[key] = self.race_payload
            raise FakeClientError("PreconditionFailed", 412)
        if key in self.objects and kwargs.get("IfNoneMatch") == "*":
            raise FakeClientError("PreconditionFailed", 412)
        if self.corrupt_put and payload:
            payload = bytes([payload[0] ^ 1]) + payload[1:]
        self.objects[key] = payload
        return {"ETag": "not-used-as-a-checksum"}


class FakeSourceReader:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def read_verified(self, record):
        self.calls.append(record["path_id"])
        payload = self.payloads[record["path_id"]]
        if len(payload) != record["size_bytes"]:
            raise MigrationError("source_manifest_size_mismatch")
        if hashlib.sha256(payload).hexdigest() != record["sha256"]:
            raise MigrationError("source_manifest_checksum_mismatch")
        return payload


def _destination():
    return normalize_r2_destination(TEST_ENDPOINT, TEST_BUCKET)


def _empty_ledger(map_mac="1" * 64, destination_fingerprint=None):
    return _ledger_payload(
        scope_id=TEST_SCOPE,
        source_manifest_mac=TEST_MANIFEST_MAC,
        approved_map_mac=map_mac,
        destination_fingerprint=destination_fingerprint
        or _destination().fingerprint,
    )


class MigrationObjectTests(unittest.TestCase):
    def test_default_and_copy_missing_without_execute_are_dry_run(self):
        parser = _parser()
        common = [
            "--root",
            "/data",
            "--manifest",
            "/private/manifest.json",
            "--approved-reference-map",
            "/private/map.json",
            "--ledger",
            "/private/ledger.json",
            "--key-id",
            _keys().key_id,
            "--scope-id",
            TEST_SCOPE,
        ]
        default_args = parser.parse_args(common)
        copy_args = parser.parse_args([*common, "copy-missing"])
        execute_args = parser.parse_args([*common, "copy-missing", "--execute"])
        self.assertIsNone(default_args.command)
        self.assertFalse(default_args.execute)
        self.assertEqual(copy_args.command, "copy-missing")
        self.assertFalse(copy_args.execute)
        self.assertTrue(execute_args.execute)

    def test_dry_run_verifies_source_and_missing_without_put_or_checkpoint(self):
        payload = b"dry-run"
        record, reference = _approved_pair(payload)
        client = FakeS3()
        checkpoints = []
        summary = migrate_approved_objects(
            [(record, reference)],
            source_reader=FakeSourceReader({record["path_id"]: payload}),
            client=client,
            bucket=TEST_BUCKET,
            ledger_payload=_empty_ledger(),
            checkpoint=lambda value: checkpoints.append(deepcopy(value)),
            execute=False,
        )
        self.assertEqual(summary["status"], "complete")
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["objects_initially_missing"], 1)
        self.assertEqual(client.put_calls, [])
        self.assertEqual(checkpoints, [])
        self.assertEqual(client.delete_calls, [])

    def test_missing_object_is_conditionally_copied_and_fully_rehashed(self):
        payload = b"new-object"
        record, reference = _approved_pair(payload)
        client = FakeS3()
        ledger = _empty_ledger()
        checkpoints = []
        summary = migrate_approved_objects(
            [(record, reference)],
            source_reader=FakeSourceReader({record["path_id"]: payload}),
            client=client,
            bucket=TEST_BUCKET,
            ledger_payload=ledger,
            checkpoint=lambda value: checkpoints.append(deepcopy(value)),
            execute=True,
        )
        self.assertEqual(summary["objects_copied_verified"], 1)
        self.assertEqual(len(client.put_calls), 1)
        put = client.put_calls[0]
        self.assertEqual(put["IfNoneMatch"], "*")
        self.assertEqual(put["ContentMD5"], _content_md5(payload))
        self.assertEqual(put["ContentLength"], len(payload))
        self.assertEqual(put["Metadata"]["source-path-id"], record["path_id"])
        self.assertGreaterEqual(len(client.head_calls), 2)
        self.assertEqual(len(client.get_calls), 1)
        self.assertEqual(
            checkpoints[-1]["records"][record["path_id"]]["state"],
            "copied_verified",
        )
        self.assertEqual(client.delete_calls, [])

    def test_resume_rechecks_source_head_and_get_without_another_put(self):
        payload = b"resume"
        record, reference = _approved_pair(payload)
        client = FakeS3()
        client.objects[reference.destination_key] = payload
        ledger = _empty_ledger()
        ledger["records"][record["path_id"]] = {
            "destination_key": reference.destination_key,
            "size_bytes": len(payload),
            "sha256": record["sha256"],
            "state": "copied_verified",
        }
        checkpoints = []
        reader = FakeSourceReader({record["path_id"]: payload})
        summary = migrate_approved_objects(
            [(record, reference)],
            source_reader=reader,
            client=client,
            bucket=TEST_BUCKET,
            ledger_payload=ledger,
            checkpoint=lambda value: checkpoints.append(deepcopy(value)),
            execute=True,
        )
        self.assertEqual(summary["objects_resumed_verified"], 1)
        self.assertEqual(reader.calls, [record["path_id"]])
        self.assertEqual(len(client.get_calls), 1)
        self.assertEqual(client.put_calls, [])
        self.assertEqual(checkpoints, [])

    def test_existing_matching_object_is_never_overwritten(self):
        payload = b"same"
        record, reference = _approved_pair(payload)
        client = FakeS3()
        client.objects[reference.destination_key] = payload
        checkpoints = []
        summary = migrate_approved_objects(
            [(record, reference)],
            source_reader=FakeSourceReader({record["path_id"]: payload}),
            client=client,
            bucket=TEST_BUCKET,
            ledger_payload=_empty_ledger(),
            checkpoint=lambda value: checkpoints.append(deepcopy(value)),
            execute=True,
        )
        self.assertEqual(summary["objects_existing_verified"], 1)
        self.assertEqual(client.put_calls, [])
        self.assertEqual(
            checkpoints[-1]["records"][record["path_id"]]["state"],
            "existing_verified",
        )

    def test_existing_collision_fails_without_put_or_overwrite(self):
        payload = b"expected"
        record, reference = _approved_pair(payload)
        client = FakeS3()
        client.objects[reference.destination_key] = b"collide!"
        with self.assertRaisesRegex(MigrationError, "destination_checksum_mismatch"):
            migrate_approved_objects(
                [(record, reference)],
                source_reader=FakeSourceReader({record["path_id"]: payload}),
                client=client,
                bucket=TEST_BUCKET,
                ledger_payload=_empty_ledger(),
                checkpoint=lambda _value: self.fail("must not checkpoint collision"),
                execute=True,
            )
        self.assertEqual(client.put_calls, [])
        self.assertEqual(client.objects[reference.destination_key], b"collide!")
        self.assertEqual(client.delete_calls, [])

    def test_412_race_reheads_gets_and_accepts_only_matching_object(self):
        payload = b"won-by-another-worker"
        record, reference = _approved_pair(payload)
        client = FakeS3()
        client.race_payload = payload
        checkpoints = []
        summary = migrate_approved_objects(
            [(record, reference)],
            source_reader=FakeSourceReader({record["path_id"]: payload}),
            client=client,
            bucket=TEST_BUCKET,
            ledger_payload=_empty_ledger(),
            checkpoint=lambda value: checkpoints.append(deepcopy(value)),
            execute=True,
        )
        self.assertEqual(summary["objects_race_verified"], 1)
        self.assertGreaterEqual(len(client.head_calls), 2)
        self.assertEqual(len(client.get_calls), 1)
        self.assertEqual(
            checkpoints[-1]["records"][record["path_id"]]["state"],
            "race_verified",
        )

    def test_412_race_with_different_bytes_is_a_collision(self):
        payload = b"expected-race-winner"
        record, reference = _approved_pair(payload)
        client = FakeS3()
        client.race_payload = b"different-race-bytes"
        checkpoints = []
        with self.assertRaisesRegex(MigrationError, "destination_checksum_mismatch"):
            migrate_approved_objects(
                [(record, reference)],
                source_reader=FakeSourceReader({record["path_id"]: payload}),
                client=client,
                bucket=TEST_BUCKET,
                ledger_payload=_empty_ledger(),
                checkpoint=lambda value: checkpoints.append(deepcopy(value)),
                execute=True,
            )
        self.assertEqual(checkpoints, [])
        self.assertEqual(client.delete_calls, [])

    def test_destination_checksum_mismatch_never_completes_ledger(self):
        payload = b"must-remain-exact"
        record, reference = _approved_pair(payload)
        client = FakeS3()
        client.corrupt_put = True
        ledger = _empty_ledger()
        checkpoints = []
        with self.assertRaisesRegex(MigrationError, "destination_checksum_mismatch"):
            migrate_approved_objects(
                [(record, reference)],
                source_reader=FakeSourceReader({record["path_id"]: payload}),
                client=client,
                bucket=TEST_BUCKET,
                ledger_payload=ledger,
                checkpoint=lambda value: checkpoints.append(deepcopy(value)),
                execute=True,
            )
        self.assertEqual(ledger["records"], {})
        self.assertEqual(checkpoints, [])
        self.assertEqual(client.delete_calls, [])

    def test_completed_ledger_with_missing_destination_fails_closed(self):
        payload = b"deleted-after-checkpoint"
        record, reference = _approved_pair(payload)
        ledger = _empty_ledger()
        ledger["records"][record["path_id"]] = {
            "destination_key": reference.destination_key,
            "size_bytes": len(payload),
            "sha256": record["sha256"],
            "state": "copied_verified",
        }
        client = FakeS3()
        with self.assertRaisesRegex(MigrationError, "ledger_destination_missing"):
            migrate_approved_objects(
                [(record, reference)],
                source_reader=FakeSourceReader({record["path_id"]: payload}),
                client=client,
                bucket=TEST_BUCKET,
                ledger_payload=ledger,
                checkpoint=lambda _value: self.fail("must fail closed"),
                execute=True,
            )
        self.assertEqual(client.put_calls, [])

    def test_source_checksum_failure_happens_before_any_provider_call(self):
        expected = b"expected-source"
        record, reference = _approved_pair(expected)
        client = FakeS3()
        with self.assertRaisesRegex(
            MigrationError, "source_manifest_checksum_mismatch"
        ):
            migrate_approved_objects(
                [(record, reference)],
                source_reader=FakeSourceReader(
                    {record["path_id"]: b"tampered-source"}
                ),
                client=client,
                bucket=TEST_BUCKET,
                ledger_payload=_empty_ledger(),
                checkpoint=lambda _value: self.fail("must not checkpoint"),
                execute=True,
            )
        self.assertEqual(client.head_calls, [])
        self.assertEqual(client.put_calls, [])

    def test_ledger_cannot_carry_an_unapproved_extra_record(self):
        payload = b"approved"
        record, reference = _approved_pair(payload)
        extra_id = "b" * 64
        ledger = _empty_ledger()
        ledger["records"][extra_id] = {
            "destination_key": destination_key_for(extra_id, "other-tenant"),
            "size_bytes": 1,
            "sha256": "c" * 64,
            "state": "copied_verified",
        }
        client = FakeS3()
        with self.assertRaisesRegex(MigrationError, "ledger_has_unapproved_record"):
            migrate_approved_objects(
                [(record, reference)],
                source_reader=FakeSourceReader({record["path_id"]: payload}),
                client=client,
                bucket=TEST_BUCKET,
                ledger_payload=ledger,
                checkpoint=lambda _value: self.fail("must not checkpoint"),
                execute=True,
            )
        self.assertEqual(client.head_calls, [])


class ApprovedMapAndLedgerTests(unittest.TestCase):
    def test_approved_map_requires_every_media_mapping_and_opaque_destination(self):
        first = _record(b"one", "a" * 64)
        second = _record(b"two", "b" * 64)
        value = _map_for([first])
        with self.assertRaisesRegex(MigrationError, "pending_mappings"):
            validate_approved_reference_map(
                value,
                inventory={"records": [first, second]},
                keys=_keys(),
                scope_id=TEST_SCOPE,
                source_manifest_mac=TEST_MANIFEST_MAC,
                expected_approved_map_mac=value["mac"],
            )
        payload = _map_payload_for([first, second])
        payload["references"][0]["destination_key"] = "uploads/plain-name.png"
        value = seal_approved_reference_map(payload, _keys())
        with self.assertRaisesRegex(MigrationError, "destination_invalid"):
            validate_approved_reference_map(
                value,
                inventory={"records": [first, second]},
                keys=_keys(),
                scope_id=TEST_SCOPE,
                source_manifest_mac=TEST_MANIFEST_MAC,
                expected_approved_map_mac=value["mac"],
            )

    def test_unapproved_or_excluded_mapping_is_rejected(self):
        record = _record()
        value = _map_for([record], reference_status="pending")
        with self.assertRaisesRegex(MigrationError, "unapproved_reference"):
            validate_approved_reference_map(
                value,
                inventory={"records": [record]},
                keys=_keys(),
                scope_id=TEST_SCOPE,
                source_manifest_mac=TEST_MANIFEST_MAC,
                expected_approved_map_mac=value["mac"],
            )
        secret = {**record, "category": "secret", "sha256": None}
        value = _map_for([record])
        with self.assertRaisesRegex(MigrationError, "category_excluded"):
            validate_approved_reference_map(
                value,
                inventory={"records": [secret]},
                keys=_keys(),
                scope_id=TEST_SCOPE,
                source_manifest_mac=TEST_MANIFEST_MAC,
                expected_approved_map_mac=value["mac"],
            )

    def test_approved_map_hmac_and_independent_expected_mac_are_both_required(self):
        record = _record()
        value = _map_for([record])
        tampered = deepcopy(value)
        tampered["approved_payload"]["references"][0]["tenant_slug"] = "other"
        with self.assertRaisesRegex(MigrationError, "integrity_check_failed"):
            validate_approved_reference_map(
                tampered,
                inventory={"records": [record]},
                keys=_keys(),
                scope_id=TEST_SCOPE,
                source_manifest_mac=TEST_MANIFEST_MAC,
                expected_approved_map_mac=value["mac"],
            )
        with self.assertRaisesRegex(MigrationError, "expected_mac_mismatch"):
            validate_approved_reference_map(
                value,
                inventory={"records": [record]},
                keys=_keys(),
                scope_id=TEST_SCOPE,
                source_manifest_mac=TEST_MANIFEST_MAC,
                expected_approved_map_mac="0" * 64,
            )

    def test_ledger_mac_detects_tamper_and_binding_changes(self):
        keys = _keys()
        payload = _empty_ledger()
        envelope = seal_ledger(payload, keys)
        verified = verify_ledger(
            envelope,
            keys,
            scope_id=TEST_SCOPE,
            source_manifest_mac=TEST_MANIFEST_MAC,
            approved_map_mac="1" * 64,
            destination_fingerprint=_destination().fingerprint,
        )
        self.assertEqual(verified, payload)
        tampered = deepcopy(envelope)
        tampered["payload"]["records"]["a" * 64] = {
            "destination_key": destination_key_for("a" * 64, "junin"),
            "size_bytes": 1,
            "sha256": "b" * 64,
            "state": "copied_verified",
        }
        with self.assertRaisesRegex(MigrationError, "integrity_check_failed"):
            verify_ledger(
                tampered,
                keys,
                scope_id=TEST_SCOPE,
                source_manifest_mac=TEST_MANIFEST_MAC,
                approved_map_mac="1" * 64,
                destination_fingerprint=_destination().fingerprint,
            )
        with self.assertRaisesRegex(MigrationError, "binding_mismatch"):
            verify_ledger(
                envelope,
                keys,
                scope_id=TEST_SCOPE,
                source_manifest_mac="0" * 64,
                approved_map_mac="1" * 64,
                destination_fingerprint=_destination().fingerprint,
            )
        with self.assertRaisesRegex(MigrationError, "binding_mismatch"):
            verify_ledger(
                envelope,
                keys,
                scope_id=TEST_SCOPE,
                source_manifest_mac=TEST_MANIFEST_MAC,
                approved_map_mac="1" * 64,
                destination_fingerprint="0" * 64,
            )

    def test_ledger_contains_no_plaintext_source_paths_or_secrets(self):
        envelope = seal_ledger(_empty_ledger(), _keys())
        serialized = json.dumps(envelope)
        self.assertNotIn("uploads/citizen-name/document.png", serialized)
        self.assertNotIn("R2_SECRET_ACCESS_KEY", serialized)
        self.assertNotIn("STORAGE_INVENTORY_MASTER_KEY", serialized)

    def test_validly_maced_ledger_still_rejects_plaintext_destination(self):
        payload = _empty_ledger()
        payload["records"]["a" * 64] = {
            "destination_key": "uploads/citizen-name/document.png",
            "size_bytes": 1,
            "sha256": "b" * 64,
            "state": "copied_verified",
        }
        envelope = seal_ledger(payload, _keys())
        with self.assertRaisesRegex(MigrationError, "ledger_record_invalid"):
            verify_ledger(
                envelope,
                _keys(),
                scope_id=TEST_SCOPE,
                source_manifest_mac=TEST_MANIFEST_MAC,
                approved_map_mac="1" * 64,
                destination_fingerprint=_destination().fingerprint,
            )


class CliGateTests(unittest.TestCase):
    def _argv(self, *, map_mac="e" * 64, fingerprint=None):
        return [
            "--root",
            "/data",
            "--manifest",
            "/private/manifest.json",
            "--approved-reference-map",
            "/private/map.json",
            "--ledger",
            "/private/ledger.json",
            "--key-id",
            _keys().key_id,
            "--scope-id",
            TEST_SCOPE,
            "--expected-manifest-mac",
            TEST_MANIFEST_MAC,
            "--expected-approved-map-mac",
            map_mac,
            "--expected-destination-fingerprint",
            fingerprint or _destination().fingerprint,
            "copy-missing",
            "--execute",
        ]

    def test_manifest_mac_root_scope_gate_runs_before_r2_client(self):
        output = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {"STORAGE_INVENTORY_MASTER_KEY": _encoded_test_key()},
                clear=False,
            ),
            mock.patch.object(
                migration.inventory_tools,
                "_load_and_verify_manifest",
                side_effect=inventory_tools.InventoryError(
                    "manifest_expected_mac_mismatch"
                ),
            ) as verifier,
            mock.patch.object(migration, "_create_s3_client") as create_client,
            redirect_stdout(output),
        ):
            exit_code = migration.main(self._argv())
        self.assertEqual(exit_code, 2)
        verifier.assert_called_once()
        create_client.assert_not_called()
        public = json.loads(output.getvalue())
        self.assertEqual(public["error_code"], "manifest_expected_mac_mismatch")
        self.assertNotIn("/private/manifest.json", output.getvalue())

    def test_unapproved_map_blocks_before_r2_client(self):
        record = _record()
        unapproved = _map_for([record], status="pending")
        output = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {"STORAGE_INVENTORY_MASTER_KEY": _encoded_test_key()},
                clear=False,
            ),
            mock.patch.object(
                migration.inventory_tools,
                "_load_and_verify_manifest",
                return_value=(
                    {"mac": TEST_MANIFEST_MAC},
                    {"records": [record]},
                    (1, 2),
                ),
            ),
            mock.patch.object(
                migration, "_load_private_json", return_value=unapproved
            ),
            mock.patch.object(migration, "_create_s3_client") as create_client,
            redirect_stdout(output),
        ):
            exit_code = migration.main(self._argv(map_mac=unapproved["mac"]))
        self.assertEqual(exit_code, 2)
        create_client.assert_not_called()
        public = json.loads(output.getvalue())
        self.assertEqual(public["error_code"], "approved_map_not_approved")

    def test_r2_endpoint_gate_refuses_non_cloudflare_exfiltration_target(self):
        environment = {
            "R2_ENDPOINT_URL": "https://attacker.example.test",
            "R2_ACCESS_KEY_ID": "access",
            "R2_SECRET_ACCESS_KEY": "secret",
            "R2_BUCKET_NAME": "private-bucket",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(MigrationError, "r2_endpoint_invalid"):
                _load_r2_destination()

    def test_r2_bucket_gate_refuses_path_like_bucket(self):
        environment = {
            "R2_ENDPOINT_URL": TEST_ENDPOINT,
            "R2_ACCESS_KEY_ID": "access",
            "R2_SECRET_ACCESS_KEY": "secret",
            "R2_BUCKET_NAME": "private/bucket",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(MigrationError, "bucket_name_invalid"):
                _load_r2_destination()

    def test_destination_fingerprint_binds_account_hostname_and_bucket(self):
        canonical = normalize_r2_destination(
            f"{TEST_ENDPOINT}/", TEST_BUCKET, region="us-east-1"
        )
        same = normalize_r2_destination(TEST_ENDPOINT, TEST_BUCKET, region="auto")
        other_account = normalize_r2_destination(
            f"https://{'b' * 32}.r2.cloudflarestorage.com",
            TEST_BUCKET,
        )
        other_bucket = normalize_r2_destination(TEST_ENDPOINT, "other-bucket")
        self.assertEqual(canonical.endpoint_url, TEST_ENDPOINT)
        self.assertEqual(canonical.account_id, TEST_ACCOUNT_ID)
        self.assertEqual(canonical.fingerprint, same.fingerprint)
        self.assertNotEqual(canonical.fingerprint, other_account.fingerprint)
        self.assertNotEqual(canonical.fingerprint, other_bucket.fingerprint)

    def test_destination_expected_fingerprint_mismatch_blocks_before_client(self):
        record = _record()
        approved_map = _map_for([record])
        output = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {"STORAGE_INVENTORY_MASTER_KEY": _encoded_test_key()},
                clear=False,
            ),
            mock.patch.object(
                migration.inventory_tools,
                "_load_and_verify_manifest",
                return_value=(
                    {"mac": TEST_MANIFEST_MAC},
                    {"records": [record]},
                    (1, 2),
                ),
            ),
            mock.patch.object(
                migration, "_load_private_json", return_value=approved_map
            ),
            mock.patch.object(
                migration, "_load_r2_destination", return_value=_destination()
            ),
            mock.patch.object(migration, "_load_or_create_ledger") as load_ledger,
            mock.patch.object(migration, "_create_s3_client") as create_client,
            redirect_stdout(output),
        ):
            exit_code = migration.main(
                self._argv(
                    map_mac=approved_map["mac"],
                    fingerprint="0" * 64,
                )
            )
        self.assertEqual(exit_code, 2)
        load_ledger.assert_not_called()
        create_client.assert_not_called()
        public = json.loads(output.getvalue())
        self.assertEqual(public["error_code"], "destination_fingerprint_mismatch")

    def test_missing_independent_map_mac_blocks_before_manifest_and_client(self):
        argv = self._argv()
        index = argv.index("--expected-approved-map-mac")
        del argv[index : index + 2]
        output = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {"STORAGE_INVENTORY_MASTER_KEY": _encoded_test_key()},
                clear=True,
            ),
            mock.patch.object(
                migration.inventory_tools, "_load_and_verify_manifest"
            ) as verifier,
            mock.patch.object(migration, "_create_s3_client") as create_client,
            redirect_stdout(output),
        ):
            exit_code = migration.main(argv)
        self.assertEqual(exit_code, 2)
        verifier.assert_not_called()
        create_client.assert_not_called()
        self.assertEqual(
            json.loads(output.getvalue())["error_code"],
            "approved_map_expected_mac_invalid",
        )

    def test_missing_independent_destination_fingerprint_blocks_before_manifest(self):
        argv = self._argv()
        index = argv.index("--expected-destination-fingerprint")
        del argv[index : index + 2]
        output = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {"STORAGE_INVENTORY_MASTER_KEY": _encoded_test_key()},
                clear=True,
            ),
            mock.patch.object(
                migration.inventory_tools, "_load_and_verify_manifest"
            ) as verifier,
            mock.patch.object(migration, "_create_s3_client") as create_client,
            redirect_stdout(output),
        ):
            exit_code = migration.main(argv)
        self.assertEqual(exit_code, 2)
        verifier.assert_not_called()
        create_client.assert_not_called()
        self.assertEqual(
            json.loads(output.getvalue())["error_code"],
            "destination_expected_fingerprint_invalid",
        )


@unittest.skipUnless(
    inventory_tools._descriptor_traversal_available(),
    "secure descriptor-relative source reader is POSIX-only",
)
class PosixSourceReaderTests(unittest.TestCase):
    def test_source_reader_checks_manifest_size_mtime_and_sha(self):
        keys = _keys()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uploads = root / "uploads"
            uploads.mkdir()
            source = uploads / "image.bin"
            payload = b"source-bytes"
            source.write_bytes(payload)
            metadata = source.stat()
            relative = PurePosixPath("uploads/image.bin")
            record = {
                "path_id": inventory_tools._path_id(keys, relative),
                "path_token": inventory_tools._encrypt_relative_path(keys, relative),
                "category": "media_review_required",
                "size_bytes": len(payload),
                "mtime_ns": metadata.st_mtime_ns,
                "mime_type": "application/octet-stream",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "hash_status": "verified",
            }
            root_metadata = root.stat()
            reader = PosixManifestSourceReader(
                root,
                keys,
                (root_metadata.st_dev, root_metadata.st_ino),
                max_object_bytes=1024,
            )
            self.assertEqual(reader.read_verified(record), payload)
            source.write_bytes(b"tampered-data")
            with self.assertRaises(MigrationError):
                reader.read_verified(record)

    def test_source_reader_refuses_symlink(self):
        keys = _keys()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uploads = root / "uploads"
            uploads.mkdir()
            real = root / "real.bin"
            real.write_bytes(b"source")
            link = uploads / "link.bin"
            link.symlink_to(real)
            metadata = real.stat()
            relative = PurePosixPath("uploads/link.bin")
            record = {
                "path_id": inventory_tools._path_id(keys, relative),
                "path_token": inventory_tools._encrypt_relative_path(keys, relative),
                "category": "media_review_required",
                "size_bytes": metadata.st_size,
                "mtime_ns": metadata.st_mtime_ns,
                "mime_type": None,
                "sha256": hashlib.sha256(b"source").hexdigest(),
                "hash_status": "verified",
            }
            root_metadata = root.stat()
            reader = PosixManifestSourceReader(
                root,
                keys,
                (root_metadata.st_dev, root_metadata.st_ino),
                max_object_bytes=1024,
            )
            with self.assertRaisesRegex(MigrationError, "source_path_unsafe"):
                reader.read_verified(record)

    def test_source_reader_refuses_hardlinked_media(self):
        keys = _keys()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uploads = root / "uploads"
            uploads.mkdir()
            source = uploads / "citizen-document.bin"
            alias = uploads / "citizen-document-copy.bin"
            payload = b"private-media"
            source.write_bytes(payload)
            os.link(source, alias)
            metadata = source.stat()
            relative = PurePosixPath("uploads/citizen-document.bin")
            record = {
                "path_id": inventory_tools._path_id(keys, relative),
                "path_token": inventory_tools._encrypt_relative_path(keys, relative),
                "category": "media_review_required",
                "size_bytes": metadata.st_size,
                "mtime_ns": metadata.st_mtime_ns,
                "mime_type": "application/octet-stream",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "hash_status": "verified",
            }
            root_metadata = root.stat()
            reader = PosixManifestSourceReader(
                root,
                keys,
                (root_metadata.st_dev, root_metadata.st_ino),
                max_object_bytes=1024,
            )
            with self.assertRaisesRegex(MigrationError, "source_hardlink_refused"):
                reader.read_verified(record)


if __name__ == "__main__":
    unittest.main()
