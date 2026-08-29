from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.rehearse_compute_rollback import (
    CONTRACT_VERSION,
    LEGACY_CONTRACT_VERSION,
    RollbackValidationFailure,
    load_manifest,
    main,
    validate_manifest,
)


FINGERPRINT = "a" * 64
R2_TARGET = "b" * 64
QUEUE_TARGET = "c" * 64
INGRESS_FINGERPRINT = "d" * 64
BACKEND_REVISION = "e7aaa4f3ca971d5465d0142e6d8bb6a2eb3eac7b"
WINDOW_ID = "cutover-window-20260829-001"
RELEASE_ID = "chatboc-backend-rollback-20260829-001"
WINDOW_START = "2026-08-29T15:00:00Z"
ROLLBACK_DEADLINE = "2026-08-29T17:00:00Z"


def _evidence(
    kind: str,
    suffix: str,
    *,
    captured_at: str = "2026-08-29T15:05:00Z",
) -> dict[str, str]:
    return {
        "id": f"evidence:{suffix}",
        "kind": kind,
        "sha256": "f" * 64,
        "captured_at": captured_at,
        "window_id": WINDOW_ID,
        "release_id": RELEASE_ID,
        "backend_revision": BACKEND_REVISION,
    }


def _state(
    name: str,
    *,
    freeze_mode: str,
    owner: str,
    job_owner: str,
    epoch: int,
    captured_at: str,
) -> dict[str, object]:
    state: dict[str, object] = {
        "state": name,
        "freeze_mode": freeze_mode,
        "writer_owner": owner,
        "job_owner": job_owner,
        "authority": {
            "owner": owner,
            "epoch": epoch,
            "evidence": _evidence(
                f"authority_state_{name}",
                f"authority-{name}",
                captured_at=captured_at,
            ),
        },
        "ingress_evidence_id": "evidence:ingress-endpoint",
        "queue_evidence_id": "evidence:ingress-queue",
    }
    if name == "render_active":
        state["replay_evidence_id"] = "evidence:ingress-replay"
    return state


def _valid_manifest() -> dict[str, object]:
    states = [
        _state(
            "vercel_active",
            freeze_mode="inactive",
            owner="vercel",
            job_owner="vercel",
            epoch=41,
            captured_at="2026-08-29T15:20:00Z",
        ),
        _state(
            "both_fenced",
            freeze_mode="active",
            owner="none",
            job_owner="none",
            epoch=42,
            captured_at="2026-08-29T15:30:00Z",
        ),
        _state(
            "render_fenced",
            freeze_mode="active",
            owner="none",
            job_owner="none",
            epoch=42,
            captured_at="2026-08-29T15:40:00Z",
        ),
        _state(
            "render_active",
            freeze_mode="inactive",
            owner="render",
            job_owner="render",
            epoch=43,
            captured_at="2026-08-29T15:50:00Z",
        ),
    ]
    return {
        "contract_version": CONTRACT_VERSION,
        "window": {
            "window_id": WINDOW_ID,
            "starts_at": WINDOW_START,
            "rollback_deadline": ROLLBACK_DEADLINE,
            "approval_evidence": _evidence(
                "window_approval",
                "window-approval",
                captured_at="2026-08-29T14:50:00Z",
            ),
        },
        "release": {
            "release_id": RELEASE_ID,
            "backend_revision": BACKEND_REVISION,
            "identity_evidence": _evidence(
                "release_identity",
                "release-identity",
                captured_at="2026-08-29T14:55:00Z",
            ),
        },
        "neon": {
            "project_id": "nameless-rain-94060889",
            "branch_id": "br-floral-unit-acgqawl6",
            "database": "chatboc_cutover",
            "migration_head": "20260829_global_writer_authority_v1",
            "identity_evidence": _evidence(
                "neon_identity", "neon-identity"
            ),
            "fingerprint": {
                "expected_sha256": FINGERPRINT,
                "observed_sha256": FINGERPRINT,
                "expected_evidence": _evidence(
                    "neon_expected_fingerprint", "neon-fingerprint-expected"
                ),
                "observed_evidence": _evidence(
                    "neon_observed_fingerprint", "neon-fingerprint-observed"
                ),
            },
        },
        "render_standby": {
            "schema_action": "verify-only",
            "backend_revision": BACKEND_REVISION,
            "neon": {
                "project_id": "nameless-rain-94060889",
                "branch_id": "br-floral-unit-acgqawl6",
                "database": "chatboc_cutover",
                "migration_head": "20260829_global_writer_authority_v1",
                "fingerprint_sha256": FINGERPRINT,
            },
            "verification_evidence": _evidence(
                "render_standby_verify_only", "render-standby"
            ),
        },
        "r2": {
            "durable_upload_policy": "required",
            "target_identity_sha256": R2_TARGET,
            "vercel": {
                "compute": "vercel",
                "operation": "put-get-delete",
                "result": "verified",
                "target_identity_sha256": R2_TARGET,
                "backend_revision": BACKEND_REVISION,
                "evidence": _evidence(
                    "r2_vercel_put_get_delete", "r2-vercel"
                ),
            },
            "render": {
                "compute": "render",
                "operation": "put-get-delete",
                "result": "verified",
                "target_identity_sha256": R2_TARGET,
                "backend_revision": BACKEND_REVISION,
                "evidence": _evidence(
                    "r2_render_put_get_delete", "r2-render"
                ),
            },
        },
        "ingress": {
            "endpoint": "https://chatboc-cutover-ingress.vercel.app/health",
            "source_fingerprint_sha256": INGRESS_FINGERPRINT,
            "endpoint_evidence": _evidence(
                "ingress_endpoint", "ingress-endpoint"
            ),
            "queue": {
                "target_identity_sha256": QUEUE_TARGET,
                "schema_revision": "20260829_inbound_fifo_v2",
                "depth_before": 0,
                "depth_after": 0,
                "evidence": _evidence("ingress_queue", "ingress-queue"),
            },
            "replay": {
                "result": "verified",
                "queue_target_identity_sha256": QUEUE_TARGET,
                "persisted_count": 3,
                "replayed_count": 3,
                "failed_count": 0,
                "evidence": _evidence("ingress_replay", "ingress-replay"),
            },
        },
        "states": states,
        "transitions": [
            {
                "from_state": "vercel_active",
                "to_state": "both_fenced",
                "authority_epoch_before": 41,
                "authority_epoch_after": 42,
                "authority_owner_before": "vercel",
                "authority_owner_after": "none",
                "evidence": _evidence(
                    "transition_vercel_active_to_both_fenced",
                    "transition-vercel-both-fenced",
                    captured_at="2026-08-29T15:25:00Z",
                ),
            },
            {
                "from_state": "both_fenced",
                "to_state": "render_fenced",
                "authority_epoch_before": 42,
                "authority_epoch_after": 42,
                "authority_owner_before": "none",
                "authority_owner_after": "none",
                "evidence": _evidence(
                    "transition_both_fenced_to_render_fenced",
                    "transition-both-render-fenced",
                    captured_at="2026-08-29T15:35:00Z",
                ),
            },
            {
                "from_state": "render_fenced",
                "to_state": "render_active",
                "authority_epoch_before": 42,
                "authority_epoch_after": 43,
                "authority_owner_before": "none",
                "authority_owner_after": "render",
                "evidence": _evidence(
                    "transition_render_fenced_to_render_active",
                    "transition-render-active",
                    captured_at="2026-08-29T15:45:00Z",
                ),
            },
        ],
    }


def _blocked_reason(manifest: dict[str, object]) -> str:
    with pytest.raises(RollbackValidationFailure) as captured:
        validate_manifest(manifest)
    return captured.value.reason_code


def test_v2_validates_full_evidence_bound_rollback_without_external_actions():
    payload = validate_manifest(_valid_manifest())

    assert payload["contract_version"] == CONTRACT_VERSION
    assert payload["status"] == "ready"
    assert payload["ready"] is True
    assert payload["evidence_count"] == 18
    assert [state["state"] for state in payload["states"]] == [
        "vercel_active",
        "both_fenced",
        "render_fenced",
        "render_active",
    ]
    assert [state["job_owner_count"] for state in payload["states"]] == [
        1,
        0,
        0,
        1,
    ]
    assert payload["safety"] == {
        "single_writer_enforced": True,
        "exact_job_owner_enforced": True,
        "zero_jobs_during_freeze": True,
        "authority_epoch_bound": True,
        "external_actions_performed": False,
    }


def test_v1_is_explicitly_legacy_and_non_certifying():
    manifest = _valid_manifest()
    manifest["contract_version"] = LEGACY_CONTRACT_VERSION

    assert _blocked_reason(manifest) == "legacy_contract_non_certifying"


def test_release_requires_a_full_backend_git_sha():
    manifest = _valid_manifest()
    manifest["release"]["backend_revision"] = BACKEND_REVISION[:12]

    assert _blocked_reason(manifest) == "backend_revision_invalid"


@pytest.mark.parametrize(
    ("field", "value", "reason_code"),
    [
        ("window_id", "another-window-20260829", "evidence_window_id_mismatch"),
        ("release_id", "another-release-20260829", "evidence_release_id_mismatch"),
        ("backend_revision", "1" * 40, "evidence_backend_revision_mismatch"),
    ],
)
def test_every_evidence_artifact_is_bound_to_window_release_and_revision(
    field, value, reason_code
):
    manifest = _valid_manifest()
    manifest["ingress"]["replay"]["evidence"][field] = value

    assert _blocked_reason(manifest) == reason_code


def test_evidence_ids_cannot_be_reused_across_gates():
    manifest = _valid_manifest()
    manifest["r2"]["render"]["evidence"]["id"] = manifest["r2"]["vercel"][
        "evidence"
    ]["id"]

    assert _blocked_reason(manifest) == "evidence_id_reused"


def test_operational_evidence_must_be_inside_the_approved_window():
    manifest = _valid_manifest()
    manifest["neon"]["identity_evidence"]["captured_at"] = (
        "2026-08-29T14:59:59Z"
    )

    assert _blocked_reason(manifest) == "window_evidence_timing_invalid"


def test_neon_identity_includes_exact_project_branch_database_and_migration_head():
    manifest = _valid_manifest()
    manifest["render_standby"]["neon"]["database"] = "another_database"

    assert _blocked_reason(manifest) == "render_standby_neon_database_mismatch"


def test_neon_expected_and_independently_observed_fingerprints_must_match():
    manifest = _valid_manifest()
    manifest["neon"]["fingerprint"]["observed_sha256"] = "1" * 64

    assert _blocked_reason(manifest) == "neon_fingerprint_mismatch"


def test_render_standby_must_use_exact_backend_release_and_verify_only_schema():
    wrong_revision = _valid_manifest()
    wrong_revision["render_standby"]["backend_revision"] = "1" * 40
    wrong_action = _valid_manifest()
    wrong_action["render_standby"]["schema_action"] = "upgrade-head"

    assert _blocked_reason(wrong_revision) == (
        "render_standby_backend_revision_mismatch"
    )
    assert _blocked_reason(wrong_action) == (
        "render_standby_schema_action_not_verify_only"
    )


def test_r2_requires_verified_put_get_delete_from_both_computes():
    manifest = _valid_manifest()
    manifest["r2"]["render"]["result"] = "configured"

    assert _blocked_reason(manifest) == "r2_render_not_verified"


def test_r2_vercel_and_render_must_use_the_same_opaque_target_identity():
    manifest = _valid_manifest()
    manifest["r2"]["render"]["target_identity_sha256"] = "1" * 64

    assert _blocked_reason(manifest) == "r2_target_identity_mismatch"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://chatboc-cutover-ingress.vercel.app/health",
        "https://user:secret@chatboc-cutover-ingress.vercel.app/health",
        "https://chatboc-cutover-ingress.vercel.app/health?token=secret",
    ],
)
def test_ingress_endpoint_must_be_https_and_secret_free(endpoint):
    manifest = _valid_manifest()
    manifest["ingress"]["endpoint"] = endpoint

    assert _blocked_reason(manifest) == "ingress_endpoint_invalid"


def test_ingress_queue_and_replay_must_share_target_and_end_empty():
    wrong_target = _valid_manifest()
    wrong_target["ingress"]["replay"]["queue_target_identity_sha256"] = "1" * 64
    not_empty = _valid_manifest()
    not_empty["ingress"]["queue"]["depth_after"] = 2

    assert _blocked_reason(wrong_target) == (
        "ingress_replay_queue_identity_mismatch"
    )
    assert _blocked_reason(not_empty) == "ingress_queue_not_empty_after_drill"


def test_ingress_replay_requires_nonzero_matched_counts_and_zero_failures():
    missing_replay = _valid_manifest()
    missing_replay["ingress"]["replay"]["replayed_count"] = 2
    failures = _valid_manifest()
    failures["ingress"]["replay"]["failed_count"] = 1

    assert _blocked_reason(missing_replay) == "ingress_replay_count_mismatch"
    assert _blocked_reason(failures) == "ingress_replay_failures_present"


def test_active_states_require_exactly_one_matching_job_owner():
    manifest = _valid_manifest()
    manifest["states"][0]["job_owner"] = "none"

    assert _blocked_reason(manifest) == "vercel_active_job_owner_invalid"


def test_freeze_states_require_zero_job_owners():
    manifest = _valid_manifest()
    manifest["states"][1]["job_owner"] = "vercel"

    assert _blocked_reason(manifest) == "both_fenced_job_owner_invalid"


def test_authority_owner_must_match_writer_owner_for_every_state():
    manifest = _valid_manifest()
    manifest["states"][3]["authority"]["owner"] = "none"

    assert _blocked_reason(manifest) == "render_active_authority_owner_invalid"


def test_fencing_and_render_activation_must_advance_authority_epoch():
    fence = _valid_manifest()
    fence["states"][1]["authority"]["epoch"] = 41
    activation = _valid_manifest()
    activation["states"][3]["authority"]["epoch"] = 42

    assert _blocked_reason(fence) == "authority_fence_epoch_not_advanced"
    assert _blocked_reason(activation) == "render_activation_epoch_not_advanced"


def test_render_fenced_attestation_cannot_change_the_authority_epoch():
    manifest = _valid_manifest()
    manifest["states"][2]["authority"]["epoch"] = 43

    assert _blocked_reason(manifest) == "render_fenced_authority_epoch_changed"


def test_each_transition_is_bound_to_matching_authority_epochs_and_owners():
    manifest = _valid_manifest()
    manifest["transitions"][2]["authority_epoch_after"] = 44

    assert _blocked_reason(manifest) == "transition_authority_epoch_mismatch"


def test_transition_evidence_must_be_chronologically_between_state_attestations():
    manifest = _valid_manifest()
    manifest["transitions"][0]["evidence"]["captured_at"] = (
        "2026-08-29T15:35:00Z"
    )

    assert _blocked_reason(manifest) == "transition_evidence_order_invalid"


def test_state_ingress_queue_and_final_replay_references_must_match_evidence():
    manifest = _valid_manifest()
    manifest["states"][3]["replay_evidence_id"] = "evidence:unrelated-replay"

    assert _blocked_reason(manifest) == "state_replay_evidence_mismatch"


def test_rejects_missing_or_reordered_states_and_transitions():
    missing = _valid_manifest()
    missing["states"].pop()
    reordered = _valid_manifest()
    reordered["transitions"][0], reordered["transitions"][1] = (
        reordered["transitions"][1],
        reordered["transitions"][0],
    )

    assert _blocked_reason(missing) == "state_sequence_invalid"
    assert _blocked_reason(reordered) == "transition_sequence_invalid"


def test_cli_requires_validate_only_and_never_echoes_manifest_path(tmp_path, capsys):
    secret_path = tmp_path / "provider-secret-in-filename.json"
    secret_path.write_text(json.dumps(_valid_manifest()), encoding="utf-8")

    assert main(["--manifest", str(secret_path)]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["reason_code"] == "validate_only_required"
    assert "provider-secret" not in json.dumps(payload)
    assert payload["external_actions_performed"] is False


def test_cli_emits_only_redacted_aggregate_evidence(tmp_path, capsys):
    path = tmp_path / "rollback.json"
    manifest = _valid_manifest()
    manifest["accidental_secret"] = "postgresql://user:password@private.invalid/db"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    assert main(["--manifest", str(path), "--validate-only"]) == 0
    raw_output = capsys.readouterr().out
    payload = json.loads(raw_output)

    assert payload["ready"] is True
    assert payload["mode"] == "validate_only"
    assert "password" not in raw_output
    assert "private.invalid" not in raw_output
    assert WINDOW_ID not in raw_output
    assert RELEASE_ID not in raw_output
    assert BACKEND_REVISION not in raw_output
    assert "chatboc-cutover-ingress" not in raw_output


def test_loader_rejects_duplicate_keys(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text(
        '{"contract_version":"first","contract_version":"second"}',
        encoding="utf-8",
    )

    with pytest.raises(RollbackValidationFailure) as captured:
        load_manifest(Path(path))

    assert captured.value.reason_code == "manifest_duplicate_key"


def test_manifest_copy_remains_independent_for_adversarial_mutations():
    original = _valid_manifest()
    mutated = deepcopy(original)
    mutated["r2"]["render"]["target_identity_sha256"] = "1" * 64

    assert validate_manifest(original)["ready"] is True
    assert _blocked_reason(mutated) == "r2_target_identity_mismatch"
