"""Validate a redacted Vercel-to-Render-on-Neon rollback manifest.

This command is intentionally local and side-effect free. It reads one JSON
manifest, validates the evidence bindings for the compute ownership transition,
and emits only aggregate, redacted evidence. It never imports the application,
opens a database connection, changes a deployment, or executes a migration.

Version 2 is the only certifying contract. Version 1 manifests are rejected as
legacy because they could express readiness with booleans that were not bound
to an immutable release, cutover window, authority epoch, storage target, or
provider-ingress evidence.

The required rollback state sequence is::

    vercel_active -> both_fenced -> render_fenced -> render_active

``render_fenced`` represents a verified Render standby that owns neither
writes nor jobs. Render may become active only after that state has passed.
Durable uploads are part of the rollback contract: Vercel and Render must both
prove put/get/delete against the same opaque R2 target identity.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, MutableSet, Sequence
from urllib.parse import urlsplit


CONTRACT_VERSION = "chatboc.compute_rollback_rehearsal.v2"
LEGACY_CONTRACT_VERSION = "chatboc.compute_rollback_rehearsal.v1"
MAX_MANIFEST_BYTES = 256 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
BOUND_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
EVIDENCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
NEON_PROJECT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{5,63}$")
NEON_BRANCH_PATTERN = re.compile(r"^br-[a-z0-9][a-z0-9-]{3,62}$")
DATABASE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]{0,62}$")
MIGRATION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{3,127}$")
STATE_SEQUENCE = (
    "vercel_active",
    "both_fenced",
    "render_fenced",
    "render_active",
)
STATE_CONTRACTS: Mapping[str, Mapping[str, str | int]] = {
    "vercel_active": {
        "freeze_mode": "inactive",
        "authority_owner": "vercel",
        "writer_owner": "vercel",
        "job_owner": "vercel",
        "writer_owner_count": 1,
        "job_owner_count": 1,
    },
    "both_fenced": {
        "freeze_mode": "active",
        "authority_owner": "none",
        "writer_owner": "none",
        "job_owner": "none",
        "writer_owner_count": 0,
        "job_owner_count": 0,
    },
    "render_fenced": {
        "freeze_mode": "active",
        "authority_owner": "none",
        "writer_owner": "none",
        "job_owner": "none",
        "writer_owner_count": 0,
        "job_owner_count": 0,
    },
    "render_active": {
        "freeze_mode": "inactive",
        "authority_owner": "render",
        "writer_owner": "render",
        "job_owner": "render",
        "writer_owner_count": 1,
        "job_owner_count": 1,
    },
}


class RollbackValidationFailure(RuntimeError):
    """A stable, redacted reason code for a blocked rehearsal."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _required_mapping(document: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = document.get(key)
    if not isinstance(value, Mapping):
        raise RollbackValidationFailure(f"{key}_contract_missing")
    return value


def _required_string(
    document: Mapping[str, Any],
    key: str,
    *,
    pattern: re.Pattern[str] | None = None,
    reason_prefix: str | None = None,
) -> str:
    value = document.get(key)
    if not isinstance(value, str):
        raise RollbackValidationFailure(f"{reason_prefix or key}_invalid")
    normalized = value.strip()
    if normalized != value or not normalized:
        raise RollbackValidationFailure(f"{reason_prefix or key}_invalid")
    if pattern is not None and not pattern.fullmatch(normalized):
        raise RollbackValidationFailure(f"{reason_prefix or key}_invalid")
    return normalized


def _required_enum(
    document: Mapping[str, Any],
    key: str,
    allowed: set[str],
    *,
    reason_prefix: str | None = None,
) -> str:
    value = _required_string(document, key, reason_prefix=reason_prefix)
    if value not in allowed:
        raise RollbackValidationFailure(f"{reason_prefix or key}_invalid")
    return value


def _required_nonnegative_int(
    document: Mapping[str, Any],
    key: str,
    *,
    minimum: int = 0,
    reason_prefix: str | None = None,
) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RollbackValidationFailure(f"{reason_prefix or key}_invalid")
    return value


def _required_sha256(document: Mapping[str, Any], key: str) -> str:
    return _required_string(document, key, pattern=SHA256_PATTERN)


def _required_git_sha(document: Mapping[str, Any], key: str) -> str:
    return _required_string(document, key, pattern=GIT_SHA_PATTERN)


def _required_utc_timestamp(
    document: Mapping[str, Any],
    key: str,
    *,
    reason_prefix: str | None = None,
) -> datetime:
    value = _required_string(document, key, reason_prefix=reason_prefix)
    if not value.endswith("Z"):
        raise RollbackValidationFailure(f"{reason_prefix or key}_invalid")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as exc:
        raise RollbackValidationFailure(
            f"{reason_prefix or key}_invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise RollbackValidationFailure(f"{reason_prefix or key}_invalid")
    return parsed


def _validate_https_endpoint(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RollbackValidationFailure("ingress_endpoint_invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise RollbackValidationFailure("ingress_endpoint_invalid")


def _same(left: str, right: str, reason_code: str) -> None:
    if not hmac.compare_digest(left, right):
        raise RollbackValidationFailure(reason_code)


def _validate_evidence(
    raw_evidence: Any,
    *,
    expected_kind: str,
    window_id: str,
    release_id: str,
    backend_revision: str,
    window_start: datetime,
    rollback_deadline: datetime,
    seen_evidence_ids: MutableSet[str],
    timing: str = "window",
) -> dict[str, Any]:
    if not isinstance(raw_evidence, Mapping):
        raise RollbackValidationFailure(f"{expected_kind}_evidence_missing")

    evidence_id = _required_string(
        raw_evidence,
        "id",
        pattern=EVIDENCE_ID_PATTERN,
        reason_prefix="evidence_id",
    )
    if evidence_id in seen_evidence_ids:
        raise RollbackValidationFailure("evidence_id_reused")
    seen_evidence_ids.add(evidence_id)

    kind = _required_string(raw_evidence, "kind", reason_prefix="evidence_kind")
    if kind != expected_kind:
        raise RollbackValidationFailure("evidence_kind_mismatch")
    _required_sha256(raw_evidence, "sha256")
    captured_at = _required_utc_timestamp(
        raw_evidence,
        "captured_at",
        reason_prefix="evidence_captured_at",
    )
    _same(
        _required_string(raw_evidence, "window_id", pattern=BOUND_ID_PATTERN),
        window_id,
        "evidence_window_id_mismatch",
    )
    _same(
        _required_string(raw_evidence, "release_id", pattern=BOUND_ID_PATTERN),
        release_id,
        "evidence_release_id_mismatch",
    )
    _same(
        _required_git_sha(raw_evidence, "backend_revision"),
        backend_revision,
        "evidence_backend_revision_mismatch",
    )

    if timing == "pre_window":
        if captured_at > window_start:
            raise RollbackValidationFailure("pre_window_evidence_timing_invalid")
    elif timing == "window":
        if captured_at < window_start or captured_at > rollback_deadline:
            raise RollbackValidationFailure("window_evidence_timing_invalid")
    else:
        raise RollbackValidationFailure("evidence_timing_contract_invalid")

    return {"id": evidence_id, "captured_at": captured_at}


def _validate_window_and_release(
    manifest: Mapping[str, Any],
    seen_evidence_ids: MutableSet[str],
) -> dict[str, Any]:
    window = _required_mapping(manifest, "window")
    release = _required_mapping(manifest, "release")
    window_id = _required_string(window, "window_id", pattern=BOUND_ID_PATTERN)
    release_id = _required_string(release, "release_id", pattern=BOUND_ID_PATTERN)
    backend_revision = _required_git_sha(release, "backend_revision")
    window_start = _required_utc_timestamp(window, "starts_at")
    rollback_deadline = _required_utc_timestamp(window, "rollback_deadline")
    if rollback_deadline <= window_start:
        raise RollbackValidationFailure("rollback_window_invalid")

    evidence_context = {
        "window_id": window_id,
        "release_id": release_id,
        "backend_revision": backend_revision,
        "window_start": window_start,
        "rollback_deadline": rollback_deadline,
        "seen_evidence_ids": seen_evidence_ids,
        "timing": "pre_window",
    }
    _validate_evidence(
        window.get("approval_evidence"),
        expected_kind="window_approval",
        **evidence_context,
    )
    _validate_evidence(
        release.get("identity_evidence"),
        expected_kind="release_identity",
        **evidence_context,
    )
    return {
        "window_id": window_id,
        "release_id": release_id,
        "backend_revision": backend_revision,
        "window_start": window_start,
        "rollback_deadline": rollback_deadline,
    }


def _evidence_context(
    context: Mapping[str, Any],
    seen_evidence_ids: MutableSet[str],
) -> dict[str, Any]:
    return {
        "window_id": context["window_id"],
        "release_id": context["release_id"],
        "backend_revision": context["backend_revision"],
        "window_start": context["window_start"],
        "rollback_deadline": context["rollback_deadline"],
        "seen_evidence_ids": seen_evidence_ids,
    }


def _validate_neon(
    manifest: Mapping[str, Any],
    context: Mapping[str, Any],
    seen_evidence_ids: MutableSet[str],
) -> dict[str, str]:
    neon = _required_mapping(manifest, "neon")
    project_id = _required_string(
        neon, "project_id", pattern=NEON_PROJECT_PATTERN
    )
    branch_id = _required_string(neon, "branch_id", pattern=NEON_BRANCH_PATTERN)
    database = _required_string(neon, "database", pattern=DATABASE_PATTERN)
    migration_head = _required_string(
        neon, "migration_head", pattern=MIGRATION_PATTERN
    )
    fingerprint = _required_mapping(neon, "fingerprint")
    expected = _required_sha256(fingerprint, "expected_sha256")
    observed = _required_sha256(fingerprint, "observed_sha256")
    _same(expected, observed, "neon_fingerprint_mismatch")

    evidence_context = _evidence_context(context, seen_evidence_ids)
    _validate_evidence(
        neon.get("identity_evidence"),
        expected_kind="neon_identity",
        **evidence_context,
    )
    _validate_evidence(
        fingerprint.get("expected_evidence"),
        expected_kind="neon_expected_fingerprint",
        **evidence_context,
    )
    _validate_evidence(
        fingerprint.get("observed_evidence"),
        expected_kind="neon_observed_fingerprint",
        **evidence_context,
    )
    return {
        "project_id": project_id,
        "branch_id": branch_id,
        "database": database,
        "migration_head": migration_head,
        "fingerprint_sha256": expected,
    }


def _validate_render_standby(
    manifest: Mapping[str, Any],
    context: Mapping[str, Any],
    neon_identity: Mapping[str, str],
    seen_evidence_ids: MutableSet[str],
) -> None:
    standby = _required_mapping(manifest, "render_standby")
    if _required_string(standby, "schema_action") != "verify-only":
        raise RollbackValidationFailure(
            "render_standby_schema_action_not_verify_only"
        )
    _same(
        _required_git_sha(standby, "backend_revision"),
        context["backend_revision"],
        "render_standby_backend_revision_mismatch",
    )
    standby_neon = _required_mapping(standby, "neon")
    identity_fields = {
        "project_id": NEON_PROJECT_PATTERN,
        "branch_id": NEON_BRANCH_PATTERN,
        "database": DATABASE_PATTERN,
        "migration_head": MIGRATION_PATTERN,
        "fingerprint_sha256": SHA256_PATTERN,
    }
    for key, pattern in identity_fields.items():
        observed = _required_string(standby_neon, key, pattern=pattern)
        _same(
            observed,
            neon_identity[key],
            f"render_standby_neon_{key}_mismatch",
        )
    _validate_evidence(
        standby.get("verification_evidence"),
        expected_kind="render_standby_verify_only",
        **_evidence_context(context, seen_evidence_ids),
    )


def _validate_r2_verification(
    raw: Any,
    *,
    compute: str,
    target_identity_sha256: str,
    context: Mapping[str, Any],
    seen_evidence_ids: MutableSet[str],
) -> None:
    if not isinstance(raw, Mapping):
        raise RollbackValidationFailure(f"r2_{compute}_verification_missing")
    if _required_string(raw, "compute") != compute:
        raise RollbackValidationFailure(f"r2_{compute}_compute_mismatch")
    if _required_string(raw, "operation") != "put-get-delete":
        raise RollbackValidationFailure(f"r2_{compute}_operation_invalid")
    if _required_string(raw, "result") != "verified":
        raise RollbackValidationFailure(f"r2_{compute}_not_verified")
    _same(
        _required_sha256(raw, "target_identity_sha256"),
        target_identity_sha256,
        "r2_target_identity_mismatch",
    )
    _same(
        _required_git_sha(raw, "backend_revision"),
        context["backend_revision"],
        f"r2_{compute}_backend_revision_mismatch",
    )
    _validate_evidence(
        raw.get("evidence"),
        expected_kind=f"r2_{compute}_put_get_delete",
        **_evidence_context(context, seen_evidence_ids),
    )


def _validate_r2(
    manifest: Mapping[str, Any],
    context: Mapping[str, Any],
    seen_evidence_ids: MutableSet[str],
) -> None:
    r2 = _required_mapping(manifest, "r2")
    if _required_string(r2, "durable_upload_policy") != "required":
        raise RollbackValidationFailure("r2_durable_upload_policy_invalid")
    target_identity_sha256 = _required_sha256(r2, "target_identity_sha256")
    for compute in ("vercel", "render"):
        _validate_r2_verification(
            r2.get(compute),
            compute=compute,
            target_identity_sha256=target_identity_sha256,
            context=context,
            seen_evidence_ids=seen_evidence_ids,
        )


def _validate_ingress(
    manifest: Mapping[str, Any],
    context: Mapping[str, Any],
    seen_evidence_ids: MutableSet[str],
) -> dict[str, str]:
    ingress = _required_mapping(manifest, "ingress")
    endpoint = _required_string(ingress, "endpoint")
    _validate_https_endpoint(endpoint)
    source_fingerprint = _required_sha256(ingress, "source_fingerprint_sha256")
    evidence_context = _evidence_context(context, seen_evidence_ids)
    endpoint_evidence = _validate_evidence(
        ingress.get("endpoint_evidence"),
        expected_kind="ingress_endpoint",
        **evidence_context,
    )

    queue = _required_mapping(ingress, "queue")
    queue_target = _required_sha256(queue, "target_identity_sha256")
    _required_string(queue, "schema_revision", pattern=MIGRATION_PATTERN)
    if _required_nonnegative_int(queue, "depth_before") != 0:
        raise RollbackValidationFailure("ingress_queue_not_empty_before_drill")
    if _required_nonnegative_int(queue, "depth_after") != 0:
        raise RollbackValidationFailure("ingress_queue_not_empty_after_drill")
    queue_evidence = _validate_evidence(
        queue.get("evidence"),
        expected_kind="ingress_queue",
        **evidence_context,
    )

    replay = _required_mapping(ingress, "replay")
    if _required_string(replay, "result") != "verified":
        raise RollbackValidationFailure("ingress_replay_not_verified")
    _same(
        _required_sha256(replay, "queue_target_identity_sha256"),
        queue_target,
        "ingress_replay_queue_identity_mismatch",
    )
    persisted_count = _required_nonnegative_int(
        replay, "persisted_count", minimum=1
    )
    replayed_count = _required_nonnegative_int(replay, "replayed_count")
    if replayed_count != persisted_count:
        raise RollbackValidationFailure("ingress_replay_count_mismatch")
    if _required_nonnegative_int(replay, "failed_count") != 0:
        raise RollbackValidationFailure("ingress_replay_failures_present")
    replay_evidence = _validate_evidence(
        replay.get("evidence"),
        expected_kind="ingress_replay",
        **evidence_context,
    )
    return {
        "endpoint_evidence_id": endpoint_evidence["id"],
        "queue_evidence_id": queue_evidence["id"],
        "replay_evidence_id": replay_evidence["id"],
        "source_fingerprint_sha256": source_fingerprint,
    }


def _validate_evidence_reference(
    document: Mapping[str, Any],
    key: str,
    expected_id: str,
    reason_code: str,
) -> None:
    observed = _required_string(
        document,
        key,
        pattern=EVIDENCE_ID_PATTERN,
        reason_prefix=key,
    )
    _same(observed, expected_id, reason_code)


def _validate_state(
    raw_state: Any,
    *,
    expected_name: str,
    context: Mapping[str, Any],
    ingress_evidence: Mapping[str, str],
    seen_evidence_ids: MutableSet[str],
) -> dict[str, Any]:
    if not isinstance(raw_state, Mapping):
        raise RollbackValidationFailure("state_not_object")
    state_name = _required_string(raw_state, "state")
    if state_name != expected_name:
        raise RollbackValidationFailure("state_sequence_invalid")

    expected = STATE_CONTRACTS[expected_name]
    freeze_mode = _required_enum(
        raw_state, "freeze_mode", {"active", "inactive"}
    )
    writer_owner = _required_enum(
        raw_state, "writer_owner", {"none", "vercel", "render"}
    )
    job_owner = _required_enum(
        raw_state, "job_owner", {"none", "vercel", "render"}
    )
    authority = _required_mapping(raw_state, "authority")
    authority_owner = _required_enum(
        authority, "owner", {"none", "vercel", "render"}
    )
    authority_epoch = _required_nonnegative_int(
        authority,
        "epoch",
        minimum=1,
        reason_prefix="authority_epoch",
    )
    for key, observed in (
        ("freeze_mode", freeze_mode),
        ("writer_owner", writer_owner),
        ("job_owner", job_owner),
        ("authority_owner", authority_owner),
    ):
        if observed != expected[key]:
            raise RollbackValidationFailure(f"{expected_name}_{key}_invalid")
    if writer_owner != authority_owner:
        raise RollbackValidationFailure("writer_authority_owner_mismatch")

    authority_evidence = _validate_evidence(
        authority.get("evidence"),
        expected_kind=f"authority_state_{expected_name}",
        **_evidence_context(context, seen_evidence_ids),
    )
    _validate_evidence_reference(
        raw_state,
        "ingress_evidence_id",
        ingress_evidence["endpoint_evidence_id"],
        "state_ingress_evidence_mismatch",
    )
    _validate_evidence_reference(
        raw_state,
        "queue_evidence_id",
        ingress_evidence["queue_evidence_id"],
        "state_queue_evidence_mismatch",
    )
    if expected_name == "render_active":
        _validate_evidence_reference(
            raw_state,
            "replay_evidence_id",
            ingress_evidence["replay_evidence_id"],
            "state_replay_evidence_mismatch",
        )

    return {
        "state": state_name,
        "authority_epoch": authority_epoch,
        "authority_owner": authority_owner,
        "writer_owner_count": expected["writer_owner_count"],
        "job_owner_count": expected["job_owner_count"],
        "freeze_mode": freeze_mode,
        "evidence_captured_at": authority_evidence["captured_at"],
    }


def _validate_state_epochs(states: Sequence[Mapping[str, Any]]) -> None:
    epochs = [state["authority_epoch"] for state in states]
    if epochs[0] < 1:
        raise RollbackValidationFailure("authority_epoch_invalid")
    if epochs[1] <= epochs[0]:
        raise RollbackValidationFailure("authority_fence_epoch_not_advanced")
    if epochs[2] != epochs[1]:
        raise RollbackValidationFailure("render_fenced_authority_epoch_changed")
    if epochs[3] <= epochs[2]:
        raise RollbackValidationFailure("render_activation_epoch_not_advanced")


def _validate_transitions(
    manifest: Mapping[str, Any],
    states: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any],
    seen_evidence_ids: MutableSet[str],
) -> list[dict[str, Any]]:
    raw_transitions = manifest.get("transitions")
    if not isinstance(raw_transitions, list):
        raise RollbackValidationFailure("transitions_not_array")
    if len(raw_transitions) != len(states) - 1:
        raise RollbackValidationFailure("transition_sequence_invalid")

    validated: list[dict[str, Any]] = []
    for index, raw_transition in enumerate(raw_transitions):
        if not isinstance(raw_transition, Mapping):
            raise RollbackValidationFailure("transition_not_object")
        before = states[index]
        after = states[index + 1]
        from_state = _required_string(raw_transition, "from_state")
        to_state = _required_string(raw_transition, "to_state")
        if from_state != before["state"] or to_state != after["state"]:
            raise RollbackValidationFailure("transition_sequence_invalid")
        before_epoch = _required_nonnegative_int(
            raw_transition,
            "authority_epoch_before",
            minimum=1,
        )
        after_epoch = _required_nonnegative_int(
            raw_transition,
            "authority_epoch_after",
            minimum=1,
        )
        if (
            before_epoch != before["authority_epoch"]
            or after_epoch != after["authority_epoch"]
        ):
            raise RollbackValidationFailure("transition_authority_epoch_mismatch")
        before_owner = _required_enum(
            raw_transition,
            "authority_owner_before",
            {"none", "vercel", "render"},
        )
        after_owner = _required_enum(
            raw_transition,
            "authority_owner_after",
            {"none", "vercel", "render"},
        )
        if (
            before_owner != before["authority_owner"]
            or after_owner != after["authority_owner"]
        ):
            raise RollbackValidationFailure("transition_authority_owner_mismatch")
        evidence = _validate_evidence(
            raw_transition.get("evidence"),
            expected_kind=f"transition_{from_state}_to_{to_state}",
            **_evidence_context(context, seen_evidence_ids),
        )
        if not (
            before["evidence_captured_at"]
            <= evidence["captured_at"]
            <= after["evidence_captured_at"]
        ):
            raise RollbackValidationFailure("transition_evidence_order_invalid")
        validated.append(
            {
                "from_state": from_state,
                "to_state": to_state,
                "authority_epoch_advanced": after_epoch > before_epoch,
            }
        )
    return validated


def validate_manifest(manifest: Any) -> dict[str, Any]:
    """Validate one rollback manifest without performing external I/O."""

    if not isinstance(manifest, Mapping):
        raise RollbackValidationFailure("manifest_not_object")
    version = manifest.get("contract_version")
    if version == LEGACY_CONTRACT_VERSION:
        raise RollbackValidationFailure("legacy_contract_non_certifying")
    if version != CONTRACT_VERSION:
        raise RollbackValidationFailure("manifest_contract_version_mismatch")

    seen_evidence_ids: set[str] = set()
    context = _validate_window_and_release(manifest, seen_evidence_ids)
    neon_identity = _validate_neon(manifest, context, seen_evidence_ids)
    _validate_render_standby(
        manifest,
        context,
        neon_identity,
        seen_evidence_ids,
    )
    _validate_r2(manifest, context, seen_evidence_ids)
    ingress_evidence = _validate_ingress(manifest, context, seen_evidence_ids)

    raw_states = manifest.get("states")
    if not isinstance(raw_states, list):
        raise RollbackValidationFailure("states_not_array")
    if len(raw_states) != len(STATE_SEQUENCE):
        raise RollbackValidationFailure("state_sequence_invalid")
    states = [
        _validate_state(
            raw_state,
            expected_name=expected_name,
            context=context,
            ingress_evidence=ingress_evidence,
            seen_evidence_ids=seen_evidence_ids,
        )
        for raw_state, expected_name in zip(raw_states, STATE_SEQUENCE)
    ]
    _validate_state_epochs(states)
    transitions = _validate_transitions(
        manifest,
        states,
        context,
        seen_evidence_ids,
    )

    # Exclude identifiers, endpoints, raw fingerprints, evidence IDs, and
    # arbitrary manifest fields. A secret-bearing input must never be reflected
    # into terminal or CI logs. The digest is sufficient to bind the redacted
    # verdict to the exact local input file.
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
        "bindings": {
            "window": True,
            "release": True,
            "backend_revision_full_sha": True,
            "neon_identity": True,
            "authority_epochs": True,
            "evidence_context": True,
        },
        "neon": {
            "identity_bound": True,
            "fingerprint_match": True,
            "render_standby_match": True,
        },
        "r2": {
            "same_target_identity": True,
            "vercel_put_get_delete_verified": True,
            "render_put_get_delete_verified": True,
        },
        "ingress": {
            "endpoint_bound": True,
            "source_fingerprint_bound": True,
            "queue_evidence_bound": True,
            "replay_evidence_bound": True,
        },
        "states": [
            {
                "state": state["state"],
                "writer_owner_count": state["writer_owner_count"],
                "job_owner_count": state["job_owner_count"],
                "freeze_mode": state["freeze_mode"],
            }
            for state in states
        ],
        "transitions": transitions,
        "evidence_count": len(seen_evidence_ids),
        "safety": {
            "single_writer_enforced": True,
            "exact_job_owner_enforced": True,
            "zero_jobs_during_freeze": True,
            "authority_epoch_bound": True,
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
