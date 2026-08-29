from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.rehearse_compute_rollback import (
    CONTRACT_VERSION,
    RollbackValidationFailure,
    load_manifest,
    main,
    validate_manifest,
)


FINGERPRINT = "a" * 64


def _state(
    name: str,
    *,
    vercel_writer: bool,
    render_writer: bool,
    freeze: bool,
    vercel_jobs: bool = False,
    render_jobs: bool = False,
    ingress_ready: bool = True,
    buffer_ready: bool = True,
) -> dict[str, object]:
    return {
        "state": name,
        "writer_owner_count": int(vercel_writer) + int(render_writer),
        "vercel_writer_enabled": vercel_writer,
        "render_writer_enabled": render_writer,
        "vercel_jobs_enabled": vercel_jobs,
        "render_jobs_enabled": render_jobs,
        "freeze_active": freeze,
        "ingress_ready": ingress_ready,
        "buffer_ready": buffer_ready,
    }


def _valid_manifest() -> dict[str, object]:
    return {
        "contract_version": CONTRACT_VERSION,
        "neon_expected_fingerprint_sha256": FINGERPRINT,
        "neon_observed_fingerprint_sha256": FINGERPRINT,
        "render_standby_schema_action": "verify-only",
        "r2": {"ready": True, "durable_uploads_required": True},
        "states": [
            _state(
                "vercel_active",
                vercel_writer=True,
                render_writer=False,
                freeze=False,
                vercel_jobs=True,
            ),
            _state(
                "both_fenced",
                vercel_writer=False,
                render_writer=False,
                freeze=True,
            ),
            _state(
                "render_fenced",
                vercel_writer=False,
                render_writer=False,
                freeze=True,
            ),
            _state(
                "render_active",
                vercel_writer=False,
                render_writer=True,
                freeze=False,
                render_jobs=True,
            ),
        ],
    }


def _blocked_reason(manifest: dict[str, object]) -> str:
    with pytest.raises(RollbackValidationFailure) as captured:
        validate_manifest(manifest)
    return captured.value.reason_code


def test_validates_the_complete_rollback_sequence_without_external_actions():
    payload = validate_manifest(_valid_manifest())

    assert payload["status"] == "ready"
    assert payload["ready"] is True
    assert [state["state"] for state in payload["states"]] == [
        "vercel_active",
        "both_fenced",
        "render_fenced",
        "render_active",
    ]
    assert payload["safety"] == {
        "single_writer_enforced": True,
        "jobs_overlap_rejected": True,
        "freeze_ingress_and_buffer_required": True,
        "r2_required": True,
        "external_actions_performed": False,
    }


def test_r2_contract_is_required_for_compute_rollback():
    manifest = _valid_manifest()
    manifest.pop("r2")

    assert _blocked_reason(manifest) == "r2_contract_missing"


@pytest.mark.parametrize(
    ("field", "reason_code"),
    [
        ("ready", "r2_not_ready"),
        ("durable_uploads_required", "r2_durable_uploads_not_required"),
    ],
)
def test_r2_must_be_ready_and_mandatory(field, reason_code):
    manifest = _valid_manifest()
    manifest["r2"][field] = False

    assert _blocked_reason(manifest) == reason_code


def test_rejects_more_than_one_declared_writer_owner_before_other_checks():
    manifest = _valid_manifest()
    state = manifest["states"][1]
    state["writer_owner_count"] = 2
    state["vercel_writer_enabled"] = True
    state["render_writer_enabled"] = True

    assert _blocked_reason(manifest) == "writer_owner_count_exceeds_one"


def test_rejects_declared_writer_count_that_disagrees_with_compute_flags():
    manifest = _valid_manifest()
    manifest["states"][0]["writer_owner_count"] = 0

    assert _blocked_reason(manifest) == "writer_owner_count_mismatch"


def test_rejects_a_neon_identity_mismatch():
    manifest = _valid_manifest()
    manifest["neon_observed_fingerprint_sha256"] = "b" * 64

    assert _blocked_reason(manifest) == "neon_fingerprint_mismatch"


def test_rejects_overlapping_jobs():
    manifest = _valid_manifest()
    state = manifest["states"][0]
    state["vercel_jobs_enabled"] = True
    state["render_jobs_enabled"] = True

    assert _blocked_reason(manifest) == "jobs_overlap"


@pytest.mark.parametrize(
    ("field", "reason_code"),
    [
        ("ingress_ready", "ingress_not_ready_during_freeze"),
        ("buffer_ready", "buffer_not_ready_during_freeze"),
    ],
)
def test_freeze_requires_ready_ingress_and_durable_buffer(field, reason_code):
    manifest = _valid_manifest()
    manifest["states"][1][field] = False

    assert _blocked_reason(manifest) == reason_code


@pytest.mark.parametrize("action", ["upgrade-head", "migrate", "apply"])
def test_render_standby_must_be_verify_only(action):
    manifest = _valid_manifest()
    manifest["render_standby_schema_action"] = action

    assert _blocked_reason(manifest) == (
        "render_standby_schema_action_not_verify_only"
    )


def test_rejects_missing_or_reordered_states():
    missing = _valid_manifest()
    missing["states"].pop()
    reordered = _valid_manifest()
    reordered["states"][1], reordered["states"][2] = (
        reordered["states"][2],
        reordered["states"][1],
    )

    assert _blocked_reason(missing) == "state_sequence_invalid"
    assert _blocked_reason(reordered) == "state_sequence_invalid"


def test_cli_requires_validate_only_and_never_echoes_manifest_path(tmp_path, capsys):
    secret_path = tmp_path / "provider-secret-in-filename.json"
    secret_path.write_text(json.dumps(_valid_manifest()), encoding="utf-8")

    assert main(["--manifest", str(secret_path)]) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["reason_code"] == "validate_only_required"
    assert "provider-secret" not in json.dumps(payload)
    assert payload["external_actions_performed"] is False


def test_cli_validates_a_file_and_emits_only_redacted_evidence(tmp_path, capsys):
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


def test_loader_rejects_duplicate_keys(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text(
        '{"contract_version":"first","contract_version":"second"}',
        encoding="utf-8",
    )

    with pytest.raises(RollbackValidationFailure) as captured:
        load_manifest(Path(path))

    assert captured.value.reason_code == "manifest_duplicate_key"
