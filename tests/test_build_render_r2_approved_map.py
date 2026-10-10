import base64
from collections import Counter
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import unittest
from unittest.mock import patch

from scripts import inventory_persistent_data as inventory_tools
from scripts import migrate_render_uploads_to_r2 as migration_tools
from scripts.build_render_r2_approved_map import (
    EVIDENCE_CONTRACT_VERSION,
    ApprovalBuildError,
    RepairPolicy,
    build_approval_plan,
    main,
    seal_audit,
)


EXPECTED_MEDIA_TOTAL = 106
EXPECTED_ORIGINALS = 65
EXPECTED_THUMBNAILS = 41
EXPECTED_TENANT_COUNTS = {"junin": 90, "cuatro-fincas": 7}
EXPECTED_QUARANTINE = 9
EXPECTED_REPAIR_COUNTS = {322: 15, 344: 1, 347: 1}
EXPECTED_NEON_PROJECT_ID = "synthetic-neon-project"
EXPECTED_NEON_BRANCH_ID = "br-synthetic-approval-test"
EXPECTED_NEON_DATABASE_NAME = "synthetic-neondb"


def _test_keys():
    raw = hashlib.sha256(b"render-r2-approval-builder-test-key").digest()
    encoded = "base64url:" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode(
        "ascii"
    )
    return inventory_tools.parse_manifest_master_key(
        encoded, "inventory-2026-08-r1"
    )


def _path_id(relative_path: str) -> str:
    return hashlib.sha256(f"path-id:{relative_path}".encode("utf-8")).hexdigest()


def _reference_hash(relative_path: str) -> str:
    basename = relative_path.rsplit("/", 1)[-1]
    return hashlib.sha256(basename.lower().encode("utf-8")).hexdigest()


def _record(relative_path: str) -> dict:
    return {
        "relative_path": relative_path,
        "path_id": _path_id(relative_path),
        "category": "media_review_required",
        "size_bytes": 128,
        "hash_status": "verified",
        "sha256": hashlib.sha256(
            f"synthetic-content:{relative_path}".encode("utf-8")
        ).hexdigest(),
    }


def _anchor(
    slug: str,
    *,
    tenant_id: int,
    tenant_type: str,
    owner_id: int,
) -> dict:
    return {
        "slug": slug,
        "tenant_id": tenant_id,
        "tenant_type": tenant_type,
        "owner_kind": tenant_type,
        "owner_id": owner_id,
        "is_active": True,
        "slug_match_count": 1,
        "owner_match_count": 1,
    }


def _evidence_row(
    relative_path: str,
    *,
    attachment_id: int,
    actor_explicit_tenant_slug=None,
    actor_structural_tenant_slugs=None,
    session_tenant_slug=None,
    municipio_ticket_id=None,
    municipio_ticket_tenant_slug=None,
    municipio_ticket_tenant_type=None,
    pyme_ticket_id=None,
    pyme_ticket_tenant_slug=None,
    pyme_ticket_tenant_type=None,
) -> dict:
    return {
        "reference_hash": _reference_hash(relative_path),
        "attachment_id": attachment_id,
        "actor_explicit_tenant_slug": actor_explicit_tenant_slug,
        "actor_structural_tenant_slugs": actor_structural_tenant_slugs or [],
        "session_tenant_slug": session_tenant_slug,
        "municipio_ticket_id": municipio_ticket_id,
        "municipio_ticket_tenant_slug": municipio_ticket_tenant_slug,
        "municipio_ticket_tenant_type": municipio_ticket_tenant_type,
        "pyme_ticket_id": pyme_ticket_id,
        "pyme_ticket_tenant_slug": pyme_ticket_tenant_slug,
        "pyme_ticket_tenant_type": pyme_ticket_tenant_type,
    }


def _synthetic_contract_fixture() -> tuple[list[dict], dict, RepairPolicy]:
    """Return the bounded 106-object contract without using private evidence."""

    records: list[dict] = []
    evidence_rows: list[dict] = []
    repair_ticket_ids = [322] * 15 + [344, 347]

    # 55 originals + 35 thumbnails = 90 Junin objects. The first 17
    # originals reproduce the bounded legacy ticket repair contract.
    for index in range(55):
        relative = f"uploads/municipio_1/junin-{index:02d}.jpg"
        records.append(_record(relative))
        if index < 35:
            records.append(
                _record(f"uploads/municipio_1/junin-{index:02d}_thumb.webp")
            )
        if index < len(repair_ticket_ids):
            evidence_rows.append(
                _evidence_row(
                    relative,
                    attachment_id=1000 + index,
                    actor_structural_tenant_slugs=["junin"],
                    session_tenant_slug="junin",
                    municipio_ticket_id=repair_ticket_ids[index],
                    municipio_ticket_tenant_slug="almacen",
                    municipio_ticket_tenant_type="pyme",
                )
            )
        else:
            evidence_rows.append(
                _evidence_row(
                    relative,
                    attachment_id=1000 + index,
                    actor_explicit_tenant_slug="junin",
                    municipio_ticket_id=5000 + index,
                    municipio_ticket_tenant_slug="junin",
                    municipio_ticket_tenant_type="municipio",
                )
            )

    # 5 originals + 2 thumbnails = 7 Cuatro Fincas objects.
    for index in range(5):
        relative = f"uploads/empresa_77/cuatro-fincas-{index:02d}.png"
        records.append(_record(relative))
        if index < 2:
            records.append(
                _record(
                    f"uploads/empresa_77/cuatro-fincas-{index:02d}_thumb.webp"
                )
            )
        evidence_rows.append(
            _evidence_row(
                relative,
                attachment_id=2000 + index,
                actor_explicit_tenant_slug="cuatro-fincas",
                pyme_ticket_id=6000 + index,
                pyme_ticket_tenant_slug="cuatro-fincas",
                pyme_ticket_tenant_type="pyme",
            )
        )

    # 5 originals + 4 thumbnails = 9 explicitly unresolved objects.
    for index in range(5):
        relative = f"uploads/unresolved/orphan-{index:02d}.jpeg"
        records.append(_record(relative))
        if index < 4:
            records.append(
                _record(f"uploads/unresolved/orphan-{index:02d}_thumb.webp")
            )

    evidence = {
        "contract_version": EVIDENCE_CONTRACT_VERSION,
        "project_id": EXPECTED_NEON_PROJECT_ID,
        "branch_id": EXPECTED_NEON_BRANCH_ID,
        "database_name": EXPECTED_NEON_DATABASE_NAME,
        "observed_at": "2026-08-25T12:00:00+00:00",
        "tenant_anchors": [
            _anchor(
                "junin",
                tenant_id=22,
                tenant_type="municipio",
                owner_id=4,
            ),
            _anchor(
                "cuatro-fincas",
                tenant_id=44,
                tenant_type="pyme",
                owner_id=77,
            ),
            _anchor(
                "almacen",
                tenant_id=1,
                tenant_type="pyme",
                owner_id=1,
            ),
        ],
        "attachment_rows": evidence_rows,
    }
    repair = RepairPolicy(
        revision="20260825_legacy_municipio_ticket_scope_repair_v1",
        attachment_counts=dict(EXPECTED_REPAIR_COUNTS),
        source_slug="almacen",
        source_type="pyme",
        target_slug="junin",
        target_type="municipio",
        file_sha256="a" * 64,
    )

    assert len(records) == EXPECTED_MEDIA_TOTAL
    return records, evidence, repair


def _build(records, evidence, repair, **overrides):
    arguments = {
        "municipal_target_slug": "junin",
        "pyme_target_slug": "cuatro-fincas",
        "legacy_source_slug": "almacen",
        "expected_total": EXPECTED_MEDIA_TOTAL,
        "expected_tenant_counts": EXPECTED_TENANT_COUNTS,
        "expected_quarantine_count": EXPECTED_QUARANTINE,
        "expected_neon_project_id": EXPECTED_NEON_PROJECT_ID,
        "expected_neon_branch_id": EXPECTED_NEON_BRANCH_ID,
        "expected_neon_database_name": EXPECTED_NEON_DATABASE_NAME,
    }
    arguments.update(overrides)
    return build_approval_plan(
        records,
        evidence=evidence,
        repair=repair,
        **arguments,
    )


class BuildRenderR2ApprovedMapTests(unittest.TestCase):
    def setUp(self):
        self.records, self.evidence, self.repair = _synthetic_contract_fixture()

    def assert_build_error(
        self,
        expected_code,
        records=None,
        evidence=None,
        repair=None,
        **overrides,
    ):
        with self.assertRaises(ApprovalBuildError) as caught:
            _build(
                records if records is not None else self.records,
                evidence if evidence is not None else self.evidence,
                repair if repair is not None else self.repair,
                **overrides,
            )
        self.assertEqual(caught.exception.code, expected_code)

    def test_exact_approval_contract_succeeds(self):
        references, aggregates = _build(
            self.records, self.evidence, self.repair
        )

        reference_statuses = Counter(
            reference["approval_status"] for reference in references
        )
        tenant_counts = Counter(
            reference.get("tenant_slug")
            for reference in references
            if reference["approval_status"]
            == migration_tools.TENANT_APPROVAL_STATUS
        )

        self.assertEqual(len(references), EXPECTED_MEDIA_TOTAL)
        self.assertEqual(tenant_counts, Counter(EXPECTED_TENANT_COUNTS))
        self.assertEqual(
            reference_statuses,
            Counter(
                {
                    migration_tools.TENANT_APPROVAL_STATUS: 97,
                    migration_tools.QUARANTINE_APPROVAL_STATUS: 9,
                }
            ),
        )
        self.assertEqual(aggregates["media_total"], EXPECTED_MEDIA_TOTAL)
        self.assertEqual(aggregates["originals_total"], EXPECTED_ORIGINALS)
        self.assertEqual(aggregates["thumbnails_total"], EXPECTED_THUMBNAILS)
        self.assertEqual(
            aggregates["thumbnails_unique_parent_total"], EXPECTED_THUMBNAILS
        )
        self.assertEqual(
            aggregates["tenant_counts"], EXPECTED_TENANT_COUNTS
        )
        self.assertEqual(aggregates["quarantine_approved_total"], 9)
        self.assertEqual(
            aggregates["repair_attachment_counts"],
            {"322": 15, "344": 1, "347": 1},
        )
        self.assertEqual(aggregates["neon_attachment_evidence_rows"], 60)

    def test_thumbnail_without_parent_fails_closed(self):
        records = deepcopy(self.records)
        thumbnail = next(
            record
            for record in records
            if record["relative_path"].endswith("junin-00_thumb.webp")
        )
        thumbnail["relative_path"] = (
            "uploads/municipio_1/no-parent_thumb.webp"
        )

        self.assert_build_error(
            "thumbnail_parent_not_unique", records=records
        )

    def test_thumbnail_with_ambiguous_parent_fails_closed(self):
        records = deepcopy(self.records)
        replacement = next(
            record
            for record in records
            if record["relative_path"] == "uploads/municipio_1/junin-54.jpg"
        )
        replacement["relative_path"] = "uploads/municipio_1/junin-00.png"

        self.assert_build_error(
            "thumbnail_parent_not_unique", records=records
        )

    def test_missing_municipal_database_evidence_fails_closed(self):
        evidence = deepcopy(self.evidence)
        del evidence["attachment_rows"][20]

        self.assert_build_error(
            "municipal_media_database_evidence_missing", evidence=evidence
        )

    def test_conflicting_municipal_tenant_evidence_fails_closed(self):
        evidence = deepcopy(self.evidence)
        evidence["attachment_rows"][0][
            "actor_explicit_tenant_slug"
        ] = "cuatro-fincas"

        self.assert_build_error(
            "municipal_media_tenant_conflict", evidence=evidence
        )

    def test_repair_attachment_count_drift_fails_closed(self):
        repair = RepairPolicy(
            revision=self.repair.revision,
            attachment_counts={322: 14, 344: 1, 347: 1},
            source_slug=self.repair.source_slug,
            source_type=self.repair.source_type,
            target_slug=self.repair.target_slug,
            target_type=self.repair.target_type,
            file_sha256=self.repair.file_sha256,
        )

        self.assert_build_error(
            "bounded_repair_attachment_counts_mismatch", repair=repair
        )

    def test_expected_aggregate_drift_fails_closed(self):
        self.assert_build_error(
            "approval_tenant_counts_mismatch",
            expected_tenant_counts={"junin": 89, "cuatro-fincas": 7},
            expected_quarantine_count=10,
        )

    def test_neon_evidence_outside_operator_review_fails_closed(self):
        evidence = deepcopy(self.evidence)
        evidence["attachment_rows"].append(
            _evidence_row(
                "uploads/municipio_1/not-in-review.jpg",
                attachment_id=9999,
                actor_explicit_tenant_slug="junin",
            )
        )

        self.assert_build_error(
            "neon_evidence_outside_review", evidence=evidence
        )

    def test_sealed_map_and_audit_contain_no_clear_source_paths(self):
        references, aggregates = _build(
            self.records, self.evidence, self.repair
        )
        keys = _test_keys()
        approved_map = migration_tools.seal_approved_reference_map(
            {
                "scope_id": "chatboc:test:disk-data-v1",
                "source_manifest_mac": "f" * 64,
                "approval_status": "approved",
                "quarantine_policy": dict(migration_tools.QUARANTINE_POLICY),
                "references": references,
            },
            keys,
        )
        audit = seal_audit(
            {
                "scope_id": "chatboc:test:disk-data-v1",
                "source_manifest_mac": "f" * 64,
                "approved_map_mac": approved_map["mac"],
                "approval_status": "approved",
                "aggregates": aggregates,
                "contains_clear_paths": False,
                "contains_pii": False,
            },
            keys,
        )
        serialized_outputs = json.dumps(
            {"map": approved_map, "audit": audit},
            sort_keys=True,
        )

        self.assertNotIn("relative_path", serialized_outputs)
        self.assertNotIn("uploads/", serialized_outputs)
        for record in self.records:
            self.assertNotIn(record["relative_path"], serialized_outputs)
            self.assertNotIn(
                record["relative_path"].rsplit("/", 1)[-1],
                serialized_outputs,
            )

    def test_unexpected_cli_failure_is_redacted_and_fails_closed(self):
        argv = [
            "--manifest",
            "manifest.json",
            "--review",
            "review.json",
            "--master-key-dpapi",
            "master-key.dpapi",
            "--manifest-mac-dpapi",
            "manifest-mac.dpapi",
            "--master-key-entropy-label",
            "synthetic-master-key-entropy",
            "--manifest-mac-entropy-label",
            "synthetic-manifest-mac-entropy",
            "--map-mac-entropy-label",
            "synthetic-map-mac-entropy",
            "--repair-migration",
            "repair.py",
            "--neon-evidence",
            "evidence.json",
            "--private-root",
            "private",
            "--output-dir",
            "private/artifacts",
            "--scope-id",
            "chatboc:test:disk-data-v1",
            "--municipal-target-slug",
            "junin",
            "--pyme-target-slug",
            "cuatro-fincas",
            "--legacy-source-slug",
            "almacen",
            "--expected-neon-project-id",
            EXPECTED_NEON_PROJECT_ID,
            "--expected-neon-branch-id",
            EXPECTED_NEON_BRANCH_ID,
            "--expected-neon-database-name",
            EXPECTED_NEON_DATABASE_NAME,
            "--expected-media-total",
            str(EXPECTED_MEDIA_TOTAL),
            "--expected-tenant-count",
            "junin=90",
            "--expected-tenant-count",
            "cuatro-fincas=7",
            "--expected-quarantine-count",
            str(EXPECTED_QUARANTINE),
            "--validate-only",
        ]
        sensitive_exception = RuntimeError(
            r"C:\private\uploads\citizen-name.jpg user@example.test"
        )
        output = io.StringIO()
        with patch(
            "scripts.build_render_r2_approved_map._load_manifest_offline",
            side_effect=sensitive_exception,
        ), redirect_stdout(output):
            result = main(argv)

        self.assertEqual(result, 2)
        payload = output.getvalue().strip()
        self.assertEqual(
            json.loads(payload),
            {
                "status": "blocked",
                "error_code": "internal_error",
                "go_for_cutover": False,
            },
        )
        self.assertNotIn("private", payload.lower())
        self.assertNotIn("uploads", payload.lower())
        self.assertNotIn("example", payload.lower())


if __name__ == "__main__":
    unittest.main()
