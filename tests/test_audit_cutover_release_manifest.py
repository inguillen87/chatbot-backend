from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone

import pytest

from scripts import audit_cutover_release_manifest as release_gate


NOW = datetime(2026, 9, 5, 23, 30, tzinfo=timezone.utc)
REVISION = "6e12160030c834baef285995b75b4c93ce378e2f"
DEPLOYMENT_ID = "dpl_3jB7wdEhYekXMQGwqu7PtWyQKHzP"
DEPLOYMENT_HOST = (
    "chatboc-backend-jlei83bzv-marcelos-projects-c26aa499.vercel.app"
)


def _claims_for(gate: str) -> dict[str, object]:
    spec = release_gate.GATE_SPECS[gate]
    claims: dict[str, object] = dict(spec.get("booleans", {}))
    for key, values in spec.get("set_claims", {}).items():
        claims[key] = sorted(values)
    if spec.get("cold_start") is True:
        claims.update(
            {
                "sample_count": 5,
                "threshold_seconds": 15.0,
                "p95_start_seconds": 12.5,
                "readiness_success_rate": 1.0,
            }
        )
    return claims


def _receipt(gate: str) -> dict[str, object]:
    digest = hashlib.sha256(f"evidence:{gate}".encode("ascii")).hexdigest()
    return {
        "contract_version": release_gate.CERTIFICATION_CONTRACT_VERSION,
        "gate": gate,
        "status": "certified",
        "release_id": "release-cbs35-0001",
        "cutover_window_id": "window-cbs35-0001",
        "project_name": release_gate.EXPECTED_PROJECT_NAME,
        "deployment_id": DEPLOYMENT_ID,
        "deployment_host": f"https://{DEPLOYMENT_HOST}",
        "revision": REVISION,
        "producer_contract_version": release_gate.GATE_SPECS[gate][
            "producer"
        ],
        "evidence_sha256": digest,
        "observed_at": "2026-09-05T23:00:00Z",
        "expires_at": "2026-09-06T01:00:00Z",
        "evidence_archived": True,
        "secret_values_redacted": True,
        "production_mutations_performed": release_gate.GATE_SPECS[gate].get(
            "production_mutations_performed",
            False,
        ),
        "claims": _claims_for(gate),
    }


def _manifest() -> dict[str, object]:
    return {
        "contract_version": release_gate.MANIFEST_CONTRACT_VERSION,
        "release_id": "release-cbs35-0001",
        "cutover_window_id": "window-cbs35-0001",
        "candidate": {
            "project_name": release_gate.EXPECTED_PROJECT_NAME,
            "deployment_id": DEPLOYMENT_ID,
            "deployment_host": DEPLOYMENT_HOST,
            "revision": REVISION,
            "target": "production",
        },
        "certifications": {
            gate: _receipt(gate) for gate in release_gate.GATE_SPECS
        },
    }


def _reason_codes(report: dict[str, object]) -> set[tuple[str, str]]:
    return {
        (issue["gate"], issue["reason_code"])
        for issue in report["issues"]
    }


def test_complete_manifest_is_ready_and_redacted() -> None:
    manifest = _manifest()

    report = release_gate.audit_manifest(manifest, now=NOW)

    assert report["ready"] is True
    assert report["pre_switch_ready"] is True
    assert report["status"] == "closure_ready_for_operator_review"
    assert report["certified_gate_count"] == len(release_gate.GATE_SPECS)
    assert all(report["critical_replacements"].values())
    assert report["issues"] == []
    assert report["safety"] == {
        "read_only": True,
        "external_actions_performed": False,
        "production_mutations_performed": False,
        "secret_values_emitted": False,
        "authorizes_cutover": False,
    }
    serialized = json.dumps(report, sort_keys=True)
    assert "release-cbs35-0001" not in serialized
    assert "window-cbs35-0001" not in serialized


def test_pre_switch_can_be_ready_without_claiming_cutover_closure() -> None:
    manifest = _manifest()
    del manifest["certifications"]["live_provider_canary"]

    report = release_gate.audit_manifest(manifest, now=NOW)

    assert report["pre_switch_ready"] is True
    assert report["ready"] is False
    assert report["status"] == "awaiting_live_provider_canary"
    assert (
        "live_provider_canary",
        "certification_missing",
    ) in _reason_codes(report)


@pytest.mark.parametrize("gate", release_gate.CRITICAL_REPLACEMENT_GATES)
def test_missing_replacement_certification_fails_closed(gate: str) -> None:
    manifest = _manifest()
    del manifest["certifications"][gate]

    report = release_gate.audit_manifest(manifest, now=NOW)

    assert report["ready"] is False
    assert report["critical_replacements"][gate] is False
    assert (gate, "certification_missing") in _reason_codes(report)


@pytest.mark.parametrize("gate", release_gate.CRITICAL_REPLACEMENT_GATES)
def test_pending_replacement_certification_fails_closed(gate: str) -> None:
    manifest = _manifest()
    manifest["certifications"][gate]["status"] = "pending"

    report = release_gate.audit_manifest(manifest, now=NOW)

    assert report["ready"] is False
    assert report["critical_replacements"][gate] is False
    assert (gate, "certification_not_certified") in _reason_codes(report)


@pytest.mark.parametrize(
    ("gate", "claim"),
    (
        ("workers_queues", "no_worker_overlap"),
        ("vercel_crons", "global_no_overlap"),
        ("whatsapp_twilio_webhooks", "provider_ownership_unique"),
    ),
)
def test_critical_replacement_claim_must_be_true(
    gate: str,
    claim: str,
) -> None:
    manifest = _manifest()
    manifest["certifications"][gate]["claims"][claim] = False

    report = release_gate.audit_manifest(manifest, now=NOW)

    assert report["ready"] is False
    assert report["critical_replacements"][gate] is False
    assert (gate, f"claim_{claim}_invalid") in _reason_codes(report)


@pytest.mark.parametrize(
    ("gate", "claim"),
    (
        ("workers_queues", "components"),
        ("vercel_crons", "paths"),
        ("whatsapp_twilio_webhooks", "channels"),
    ),
)
def test_replacement_inventory_must_be_exact(gate: str, claim: str) -> None:
    manifest = _manifest()
    manifest["certifications"][gate]["claims"][claim].pop()

    report = release_gate.audit_manifest(manifest, now=NOW)

    assert report["ready"] is False
    assert (gate, f"claim_{claim}_invalid") in _reason_codes(report)


def test_preview_candidate_cannot_pass_final_release_gate() -> None:
    manifest = _manifest()
    manifest["candidate"]["target"] = "preview"

    report = release_gate.audit_manifest(manifest, now=NOW)

    assert report["ready"] is False
    assert ("candidate", "candidate_target_not_production") in _reason_codes(
        report
    )


def test_certification_must_bind_exact_candidate_revision() -> None:
    manifest = _manifest()
    manifest["certifications"]["content_parity"]["revision"] = "a" * 40

    report = release_gate.audit_manifest(manifest, now=NOW)

    assert report["ready"] is False
    assert (
        "content_parity",
        "certification_revision_mismatch",
    ) in _reason_codes(report)


def test_expired_certification_fails_closed() -> None:
    manifest = _manifest()
    manifest["certifications"]["neon_migrations"]["expires_at"] = (
        "2026-09-05T23:29:59Z"
    )

    report = release_gate.audit_manifest(manifest, now=NOW)

    assert report["ready"] is False
    assert (
        "neon_migrations",
        "certification_expired",
    ) in _reason_codes(report)


def test_same_evidence_digest_cannot_certify_two_gates() -> None:
    manifest = _manifest()
    manifest["certifications"]["vercel_crons"]["evidence_sha256"] = (
        manifest["certifications"]["workers_queues"]["evidence_sha256"]
    )

    report = release_gate.audit_manifest(manifest, now=NOW)

    assert report["ready"] is False
    assert (
        "vercel_crons",
        "evidence_digest_reused",
    ) in _reason_codes(report)


def test_cold_start_p95_must_meet_declared_bounded_threshold() -> None:
    manifest = _manifest()
    claims = manifest["certifications"]["cold_start"]["claims"]
    claims["threshold_seconds"] = 15.0
    claims["p95_start_seconds"] = 15.1

    report = release_gate.audit_manifest(manifest, now=NOW)

    assert report["ready"] is False
    assert (
        "cold_start",
        "cold_start_p95_exceeds_threshold",
    ) in _reason_codes(report)


def test_duplicate_json_key_is_rejected_without_echoing_values(tmp_path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        '{"contract_version":"first","contract_version":"secret-value"}',
        encoding="utf-8",
    )

    with pytest.raises(release_gate.CutoverManifestFailure) as exc_info:
        release_gate.load_manifest(manifest_path)

    assert exc_info.value.reason_code == "manifest_duplicate_key"
    assert "secret-value" not in str(exc_info.value)


def test_cli_requires_explicit_audit_only_and_redacts_manifest(
    tmp_path,
    capsys,
) -> None:
    manifest = copy.deepcopy(_manifest())
    manifest["operator_secret"] = "never-print-this-value"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    exit_code = release_gate.main(["--manifest", str(manifest_path)])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["reason_code"] == "audit_only_required"
    assert "never-print-this-value" not in json.dumps(payload)
