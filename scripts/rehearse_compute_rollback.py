"""Validate a declarative Vercel-to-Render-on-Neon rollback rehearsal.

This command is intentionally local and side-effect free.  It reads one JSON
manifest, validates the compute ownership transition, and emits only redacted
aggregate evidence.  It never imports the application, opens a database
connection, changes a deployment, or executes a migration.

The required rollback state sequence is::

    vercel_active -> both_fenced -> render_fenced -> render_active

``render_fenced`` represents a verified Render standby that still owns no
writes or jobs.  Render may become active only after that state has passed.
Durable uploads are part of the rollback contract.  A recovered Render
compute must keep R2 mandatory so it cannot silently return to ephemeral local
disk and diverge from Vercel.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


CONTRACT_VERSION = "chatboc.compute_rollback_rehearsal.v1"
MAX_MANIFEST_BYTES = 256 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
STATE_SEQUENCE = (
    "vercel_active",
    "both_fenced",
    "render_fenced",
    "render_active",
)
STATE_CONTRACTS: Mapping[str, Mapping[str, bool | int]] = {
    "vercel_active": {
        "vercel_writer_enabled": True,
        "render_writer_enabled": False,
        "writer_owner_count": 1,
        "freeze_active": False,
    },
    "both_fenced": {
        "vercel_writer_enabled": False,
        "render_writer_enabled": False,
        "writer_owner_count": 0,
        "freeze_active": True,
    },
    "render_fenced": {
        "vercel_writer_enabled": False,
        "render_writer_enabled": False,
        "writer_owner_count": 0,
        "freeze_active": True,
    },
    "render_active": {
        "vercel_writer_enabled": False,
        "render_writer_enabled": True,
        "writer_owner_count": 1,
        "freeze_active": False,
    },
}


class RollbackValidationFailure(RuntimeError):
    """A stable, redacted reason code for a blocked rehearsal."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _required_bool(document: Mapping[str, Any], key: str) -> bool:
    value = document.get(key)
    if not isinstance(value, bool):
        raise RollbackValidationFailure(f"state_{key}_invalid")
    return value


def _required_owner_count(document: Mapping[str, Any]) -> int:
    value = document.get("writer_owner_count")
    # bool is an int subclass, but accepting it would weaken the manifest.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RollbackValidationFailure("state_writer_owner_count_invalid")
    if value > 1:
        raise RollbackValidationFailure("writer_owner_count_exceeds_one")
    return value


def _required_sha256(document: Mapping[str, Any], key: str) -> str:
    value = document.get(key)
    normalized = str(value or "").strip().lower()
    if not SHA256_PATTERN.fullmatch(normalized):
        raise RollbackValidationFailure(f"{key}_invalid")
    return normalized


def _validate_neon_identity(manifest: Mapping[str, Any]) -> None:
    expected = _required_sha256(
        manifest,
        "neon_expected_fingerprint_sha256",
    )
    observed = _required_sha256(
        manifest,
        "neon_observed_fingerprint_sha256",
    )
    if not hmac.compare_digest(expected, observed):
        raise RollbackValidationFailure("neon_fingerprint_mismatch")


def _validate_render_standby(manifest: Mapping[str, Any]) -> None:
    action = (
        str(manifest.get("render_standby_schema_action") or "").strip().lower()
    )
    if action != "verify-only":
        raise RollbackValidationFailure(
            "render_standby_schema_action_not_verify_only"
        )


def _validate_r2(manifest: Mapping[str, Any]) -> None:
    r2 = manifest.get("r2")
    if not isinstance(r2, Mapping):
        raise RollbackValidationFailure("r2_contract_missing")
    if not _required_bool(r2, "ready"):
        raise RollbackValidationFailure("r2_not_ready")
    if not _required_bool(r2, "durable_uploads_required"):
        raise RollbackValidationFailure("r2_durable_uploads_not_required")


def _validate_state(
    raw_state: Any,
    *,
    expected_name: str,
) -> dict[str, Any]:
    if not isinstance(raw_state, Mapping):
        raise RollbackValidationFailure("state_not_object")

    state_name = str(raw_state.get("state") or "").strip().lower()
    if state_name != expected_name:
        raise RollbackValidationFailure("state_sequence_invalid")

    writer_owner_count = _required_owner_count(raw_state)
    vercel_writer_enabled = _required_bool(raw_state, "vercel_writer_enabled")
    render_writer_enabled = _required_bool(raw_state, "render_writer_enabled")
    computed_owner_count = int(vercel_writer_enabled) + int(render_writer_enabled)
    if writer_owner_count != computed_owner_count:
        raise RollbackValidationFailure("writer_owner_count_mismatch")

    freeze_active = _required_bool(raw_state, "freeze_active")
    expected_contract = STATE_CONTRACTS[expected_name]
    for key, observed in (
        ("vercel_writer_enabled", vercel_writer_enabled),
        ("render_writer_enabled", render_writer_enabled),
        ("writer_owner_count", writer_owner_count),
        ("freeze_active", freeze_active),
    ):
        if observed != expected_contract[key]:
            raise RollbackValidationFailure(f"{expected_name}_{key}_invalid")

    vercel_jobs_enabled = _required_bool(raw_state, "vercel_jobs_enabled")
    render_jobs_enabled = _required_bool(raw_state, "render_jobs_enabled")
    if vercel_jobs_enabled and render_jobs_enabled:
        raise RollbackValidationFailure("jobs_overlap")
    if freeze_active and (vercel_jobs_enabled or render_jobs_enabled):
        raise RollbackValidationFailure("jobs_active_during_freeze")
    if vercel_jobs_enabled and not vercel_writer_enabled:
        raise RollbackValidationFailure("vercel_jobs_without_writer_ownership")
    if render_jobs_enabled and not render_writer_enabled:
        raise RollbackValidationFailure("render_jobs_without_writer_ownership")

    ingress_ready = _required_bool(raw_state, "ingress_ready")
    buffer_ready = _required_bool(raw_state, "buffer_ready")
    if freeze_active and not ingress_ready:
        raise RollbackValidationFailure("ingress_not_ready_during_freeze")
    if freeze_active and not buffer_ready:
        raise RollbackValidationFailure("buffer_not_ready_during_freeze")

    return {
        "state": state_name,
        "writer_owner_count": writer_owner_count,
        "job_owner_count": int(vercel_jobs_enabled) + int(render_jobs_enabled),
        "freeze_active": freeze_active,
        "ingress_ready": ingress_ready,
        "buffer_ready": buffer_ready,
    }


def validate_manifest(manifest: Any) -> dict[str, Any]:
    """Validate a rollback manifest without performing external I/O."""

    if not isinstance(manifest, Mapping):
        raise RollbackValidationFailure("manifest_not_object")
    if manifest.get("contract_version") != CONTRACT_VERSION:
        raise RollbackValidationFailure("manifest_contract_version_mismatch")

    _validate_neon_identity(manifest)
    _validate_render_standby(manifest)
    _validate_r2(manifest)

    raw_states = manifest.get("states")
    if not isinstance(raw_states, list):
        raise RollbackValidationFailure("states_not_array")
    if len(raw_states) != len(STATE_SEQUENCE):
        raise RollbackValidationFailure("state_sequence_invalid")
    states = [
        _validate_state(raw_state, expected_name=expected_name)
        for raw_state, expected_name in zip(raw_states, STATE_SEQUENCE)
    ]

    # This evidence intentionally excludes identifiers, raw fingerprints and
    # arbitrary manifest fields.  A mistakenly secret-bearing manifest must
    # never be reflected into terminal or CI logs.
    manifest_digest = hashlib.sha256(
        json.dumps(
            manifest,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "contract_version": CONTRACT_VERSION,
        "status": "ready",
        "ready": True,
        "mode": "validate_only",
        "manifest_sha256": manifest_digest,
        "neon": {"fingerprint_match": True},
        "render_standby": {"schema_action": "verify-only"},
        "r2": {"ready": True, "durable_uploads_required": True},
        "states": states,
        "safety": {
            "single_writer_enforced": True,
            "jobs_overlap_rejected": True,
            "freeze_ingress_and_buffer_required": True,
            "r2_required": True,
            "external_actions_performed": False,
        },
    }


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RollbackValidationFailure("manifest_duplicate_key")
        result[key] = value
    return result


def load_manifest(path: Path) -> Any:
    """Load one bounded JSON file without returning its contents on failure."""

    try:
        if not path.is_file():
            raise RollbackValidationFailure("manifest_path_not_file")
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise RollbackValidationFailure("manifest_too_large")
        text = path.read_text(encoding="utf-8")
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except RollbackValidationFailure:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RollbackValidationFailure("manifest_json_invalid") from exc
    except OSError as exc:
        raise RollbackValidationFailure("manifest_read_failed") from exc


def _failure_payload(reason_code: str) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "status": "blocked",
        "ready": False,
        "mode": "validate_only",
        "reason_code": reason_code,
        "external_actions_performed": False,
    }


class _RedactedArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise RollbackValidationFailure("command_arguments_invalid")


def _parser() -> argparse.ArgumentParser:
    parser = _RedactedArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Required safety flag; this command has no mutating mode.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if not args.validate_only:
            raise RollbackValidationFailure("validate_only_required")
        payload = validate_manifest(load_manifest(Path(args.manifest)))
        exit_code = 0
    except RollbackValidationFailure as exc:
        payload = _failure_payload(exc.reason_code)
        exit_code = 2
    except Exception:
        # Never serialize a filesystem/provider exception or manifest detail.
        payload = _failure_payload("rollback_rehearsal_validation_failed")
        exit_code = 2

    print(
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
