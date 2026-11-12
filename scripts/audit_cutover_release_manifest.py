"""Validate the final Render-to-Vercel cutover evidence manifest offline.

The command is deliberately side-effect free.  It consumes one bounded JSON
manifest, binds every certification to the same release window and immutable
Vercel deployment, and emits only a redacted decision.  It never connects to
Render, Vercel, Neon, Twilio, Redis, or an application endpoint.

This is an aggregation gate, not an evidence producer.  A digest or a checked
box is not sufficient by itself: every required gate needs a fresh normalized
certification receipt from the named producer contract.  In particular,
workers/queues, Vercel crons, and WhatsApp/Twilio webhooks cannot be inferred
from repository configuration or a READY deployment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


CONTRACT_VERSION = "chatboc.cutover_release_gate.v1"
MANIFEST_CONTRACT_VERSION = "chatboc.cutover_release_manifest.v1"
CERTIFICATION_CONTRACT_VERSION = "chatboc.cutover_gate_certification.v1"
EXPECTED_PROJECT_NAME = "chatboc-backend"
MAX_MANIFEST_BYTES = 512 * 1024
MAX_CERTIFICATION_LIFETIME = timedelta(hours=6)
MAX_FUTURE_SKEW = timedelta(minutes=2)
MAX_COLD_START_THRESHOLD_SECONDS = 30.0

_FULL_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_DEPLOYMENT_ID_RE = re.compile(r"^dpl_[A-Za-z0-9]{16,64}$")
_EVIDENCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_CRON_PATHS = frozenset(
    {
        "/api/internal/cron/outbox-reconciliation",
        "/api/internal/cron/survey-privacy-retention",
        "/api/internal/cron/weekly-analytics-report",
        "/api/internal/cron/whatsapp-payload-retention",
    }
)
_WORKER_COMPONENTS = frozenset(
    {"whatsapp", "domain_effects", "survey_effects"}
)
_WEBHOOK_CHANNELS = frozenset(
    {"whatsapp_inbound", "twilio_status_callback"}
)


GATE_SPECS: Mapping[str, Mapping[str, Any]] = {
    "writer_fence": {
        "producer": "chatboc.cutover_writer_fence_certification.v1",
        "booleans": {
            "source_fenced": True,
            "destination_fenced": True,
            "single_writer_certified": True,
            "source_stable": True,
        },
    },
    "content_parity": {
        "producer": "chatboc.render_neon_final_parity.v1",
        "booleans": {
            "strict_mode": True,
            "content_match": True,
            "source_snapshot_bound": True,
            "destination_snapshot_bound": True,
        },
    },
    "neon_migrations": {
        "producer": "chatboc.neon_cutover_migrations.v1",
        "booleans": {
            "exact_migrations_applied": True,
            "direct_tls_verified": True,
            "single_head_verified": True,
            "post_migration_preflight_ready": True,
        },
    },
    "workers_queues": {
        "producer": "chatboc.worker_queue_replacement_certification.v1",
        "booleans": {
            "durable_queues_verified": True,
            "replacement_drain_rehearsed": True,
            "source_workers_disabled_or_fenced": True,
            "destination_workers_fenced_until_transfer": True,
            "no_worker_overlap": True,
        },
        "set_claims": {"components": _WORKER_COMPONENTS},
    },
    "vercel_crons": {
        "producer": "chatboc.vercel_cron_replacement_certification.v1",
        "booleans": {
            "registry_exact": True,
            "runtime_fail_closed": True,
            "source_schedulers_disabled_or_fenced": True,
            "global_no_overlap": True,
            "rollback_retarget_plan_verified": True,
        },
        "set_claims": {"paths": _CRON_PATHS},
    },
    "whatsapp_twilio_webhooks": {
        "producer": (
            "chatboc.whatsapp_twilio_webhook_replacement_certification.v1"
        ),
        "booleans": {
            "provider_snapshot_verified": True,
            "twilio_signature_verified": True,
            "inbound_persistence_replay_verified": True,
            "source_callbacks_preserved_for_rollback": True,
            "provider_ownership_unique": True,
        },
        "set_claims": {"channels": _WEBHOOK_CHANNELS},
    },
    "application_canary": {
        "producer": "chatboc.cutover_application_canary.v1",
        "production_mutations_performed": False,
        "booleans": {
            "authenticated_canary_passed": True,
            "tenant_isolation_verified": True,
            "durable_storage_verified": True,
            "disposable_or_rolled_back": True,
            "provider_messages_sent": False,
        },
    },
    "live_provider_canary": {
        "producer": "chatboc.cutover_live_provider_canary.v1",
        "production_mutations_performed": True,
        "booleans": {
            "provider_sid_exactly_once": True,
            "crm_effect_exactly_once": True,
            "outbound_attempt_exactly_once": True,
            "signed_delivery_callback_passed": True,
            "buffer_drained_exactly_once": True,
            "queue_error_counters_zero": True,
        },
        "set_claims": {
            "flows": frozenset(
                {
                    "whatsapp_text",
                    "whatsapp_location",
                    "whatsapp_audio",
                    "whatsapp_image",
                    "form_or_vote",
                    "ticket_reply",
                }
            )
        },
    },
    "rollback": {
        "producer": "chatboc.compute_rollback_rehearsal.v1",
        "booleans": {
            "compute_transition_rehearsed": True,
            "single_writer_preserved": True,
            "queue_ownership_preserved": True,
            "render_uses_neon": True,
            "durable_uploads_preserved": True,
        },
    },
    "cold_start": {
        "producer": "chatboc.vercel_cold_start_certification.v1",
        "booleans": {
            "revision_match": True,
            "database_ready": True,
            "redis_ready": True,
        },
        "cold_start": True,
    },
}

CRITICAL_REPLACEMENT_GATES = (
    "workers_queues",
    "vercel_crons",
    "whatsapp_twilio_webhooks",
)
PRE_SWITCH_GATES = tuple(
    gate for gate in GATE_SPECS if gate != "live_provider_canary"
)


class CutoverManifestFailure(RuntimeError):
    """Stable, redacted reason code for malformed manifest input."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _issue(gate: str, reason_code: str) -> dict[str, str]:
    return {"gate": gate, "reason_code": reason_code}


def _required_evidence_id(value: Any, *, reason_code: str) -> str:
    if not isinstance(value, str):
        raise CutoverManifestFailure(reason_code)
    normalized = value.strip()
    if not _EVIDENCE_ID_RE.fullmatch(normalized):
        raise CutoverManifestFailure(reason_code)
    return normalized


def _required_revision(value: Any) -> str:
    if not isinstance(value, str):
        raise CutoverManifestFailure("candidate_revision_invalid")
    revision = value.strip().lower()
    if not _FULL_REVISION_RE.fullmatch(revision):
        raise CutoverManifestFailure("candidate_revision_invalid")
    return revision


def _required_deployment_id(value: Any) -> str:
    if not isinstance(value, str):
        raise CutoverManifestFailure("candidate_deployment_id_invalid")
    deployment_id = value.strip()
    if not _DEPLOYMENT_ID_RE.fullmatch(deployment_id):
        raise CutoverManifestFailure("candidate_deployment_id_invalid")
    return deployment_id


def _canonical_vercel_host(value: Any, *, reason_code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CutoverManifestFailure(reason_code)
    rendered = value.strip()
    if "://" not in rendered:
        rendered = f"https://{rendered}"
    try:
        parsed = urlsplit(rendered)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise CutoverManifestFailure(reason_code) from exc
    if (
        parsed.scheme.lower() != "https"
        or not host.endswith(".vercel.app")
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise CutoverManifestFailure(reason_code)
    return host


def _parse_utc_timestamp(value: Any, *, reason_code: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise CutoverManifestFailure(reason_code)
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise CutoverManifestFailure(reason_code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CutoverManifestFailure(reason_code)
    return parsed.astimezone(timezone.utc)


def _required_sha256(value: Any, *, reason_code: str) -> str:
    if not isinstance(value, str):
        raise CutoverManifestFailure(reason_code)
    digest = value.strip().lower()
    if not _SHA256_RE.fullmatch(digest) or len(set(digest)) == 1:
        raise CutoverManifestFailure(reason_code)
    return digest


def _candidate(document: Any) -> dict[str, str]:
    if not isinstance(document, Mapping):
        raise CutoverManifestFailure("candidate_not_object")
    project_name = document.get("project_name")
    if project_name != EXPECTED_PROJECT_NAME:
        raise CutoverManifestFailure("candidate_project_name_mismatch")
    target = document.get("target")
    if not isinstance(target, str) or target.strip().lower() not in {
        "preview",
        "production",
    }:
        raise CutoverManifestFailure("candidate_target_invalid")
    return {
        "project_name": EXPECTED_PROJECT_NAME,
        "deployment_id": _required_deployment_id(document.get("deployment_id")),
        "deployment_host": _canonical_vercel_host(
            document.get("deployment_host"),
            reason_code="candidate_deployment_host_invalid",
        ),
        "revision": _required_revision(document.get("revision")),
        "target": target.strip().lower(),
    }


def _claim_keys(spec: Mapping[str, Any]) -> set[str]:
    keys = set(spec.get("booleans", {}))
    keys.update(spec.get("set_claims", {}))
    if spec.get("cold_start") is True:
        keys.update(
            {
                "sample_count",
                "threshold_seconds",
                "p95_start_seconds",
                "readiness_success_rate",
            }
        )
    return keys


def _validate_cold_start_claims(
    claims: Mapping[str, Any],
) -> list[str]:
    issues: list[str] = []
    sample_count = claims.get("sample_count")
    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or sample_count < 3
    ):
        issues.append("cold_start_sample_count_insufficient")

    threshold = claims.get("threshold_seconds")
    p95 = claims.get("p95_start_seconds")
    numeric_threshold = (
        isinstance(threshold, (int, float)) and not isinstance(threshold, bool)
    )
    numeric_p95 = isinstance(p95, (int, float)) and not isinstance(p95, bool)
    if (
        not numeric_threshold
        or float(threshold) <= 0
        or float(threshold) > MAX_COLD_START_THRESHOLD_SECONDS
    ):
        issues.append("cold_start_threshold_invalid")
    if not numeric_p95 or float(p95) < 0:
        issues.append("cold_start_p95_invalid")
    elif numeric_threshold and float(p95) > float(threshold):
        issues.append("cold_start_p95_exceeds_threshold")

    success_rate = claims.get("readiness_success_rate")
    if (
        isinstance(success_rate, bool)
        or not isinstance(success_rate, (int, float))
        or float(success_rate) != 1.0
    ):
        issues.append("cold_start_readiness_not_100_percent")
    return issues


def _validate_certification(
    gate: str,
    receipt: Any,
    *,
    release_id: str,
    cutover_window_id: str,
    candidate: Mapping[str, str],
    now: datetime,
) -> tuple[list[dict[str, str]], str | None]:
    if not isinstance(receipt, Mapping):
        return [_issue(gate, "certification_not_object")], None

    issues: list[dict[str, str]] = []
    spec = GATE_SPECS[gate]
    if receipt.get("contract_version") != CERTIFICATION_CONTRACT_VERSION:
        issues.append(_issue(gate, "certification_contract_mismatch"))
    if receipt.get("gate") != gate:
        issues.append(_issue(gate, "certification_gate_mismatch"))
    if receipt.get("status") != "certified":
        issues.append(_issue(gate, "certification_not_certified"))
    if receipt.get("producer_contract_version") != spec["producer"]:
        issues.append(_issue(gate, "producer_contract_mismatch"))

    for key, expected in (
        ("release_id", release_id),
        ("cutover_window_id", cutover_window_id),
        ("project_name", candidate["project_name"]),
        ("deployment_id", candidate["deployment_id"]),
        ("deployment_host", candidate["deployment_host"]),
        ("revision", candidate["revision"]),
    ):
        observed = receipt.get(key)
        if key == "deployment_host":
            try:
                observed = _canonical_vercel_host(
                    observed,
                    reason_code="certification_deployment_host_invalid",
                )
            except CutoverManifestFailure:
                issues.append(
                    _issue(gate, "certification_deployment_host_invalid")
                )
                continue
        elif key == "revision" and isinstance(observed, str):
            observed = observed.strip().lower()
        if observed != expected:
            issues.append(_issue(gate, f"certification_{key}_mismatch"))

    for key in (
        "evidence_archived",
        "secret_values_redacted",
    ):
        if receipt.get(key) is not True:
            issues.append(_issue(gate, f"certification_{key}_required"))
    expected_production_mutations = spec.get(
        "production_mutations_performed",
        False,
    )
    if (
        receipt.get("production_mutations_performed")
        is not expected_production_mutations
    ):
        issues.append(
            _issue(gate, "production_mutation_scope_mismatch")
        )

    digest: str | None = None
    try:
        digest = _required_sha256(
            receipt.get("evidence_sha256"),
            reason_code="certification_evidence_sha256_invalid",
        )
    except CutoverManifestFailure as exc:
        issues.append(_issue(gate, exc.reason_code))

    try:
        observed_at = _parse_utc_timestamp(
            receipt.get("observed_at"),
            reason_code="certification_observed_at_invalid",
        )
        expires_at = _parse_utc_timestamp(
            receipt.get("expires_at"),
            reason_code="certification_expires_at_invalid",
        )
    except CutoverManifestFailure as exc:
        issues.append(_issue(gate, exc.reason_code))
    else:
        if observed_at > now + MAX_FUTURE_SKEW:
            issues.append(_issue(gate, "certification_observed_in_future"))
        if expires_at <= observed_at:
            issues.append(_issue(gate, "certification_window_invalid"))
        elif expires_at - observed_at > MAX_CERTIFICATION_LIFETIME:
            issues.append(_issue(gate, "certification_window_too_long"))
        if expires_at < now:
            issues.append(_issue(gate, "certification_expired"))

    claims = receipt.get("claims")
    if not isinstance(claims, Mapping):
        issues.append(_issue(gate, "certification_claims_not_object"))
        return issues, digest
    if set(claims) != _claim_keys(spec):
        issues.append(_issue(gate, "certification_claims_shape_mismatch"))

    for key, expected in spec.get("booleans", {}).items():
        if claims.get(key) is not expected:
            issues.append(_issue(gate, f"claim_{key}_invalid"))
    for key, expected in spec.get("set_claims", {}).items():
        observed = claims.get(key)
        if (
            not isinstance(observed, list)
            or any(not isinstance(item, str) for item in observed)
            or len(observed) != len(set(observed))
            or set(observed) != set(expected)
        ):
            issues.append(_issue(gate, f"claim_{key}_invalid"))
    if spec.get("cold_start") is True:
        issues.extend(
            _issue(gate, reason_code)
            for reason_code in _validate_cold_start_claims(claims)
        )
    return issues, digest


def audit_manifest(
    manifest: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a redacted, fail-closed aggregate cutover decision."""

    if not isinstance(manifest, Mapping):
        raise CutoverManifestFailure("manifest_not_object")
    if manifest.get("contract_version") != MANIFEST_CONTRACT_VERSION:
        raise CutoverManifestFailure("manifest_contract_version_mismatch")

    release_id = _required_evidence_id(
        manifest.get("release_id"),
        reason_code="release_id_invalid",
    )
    cutover_window_id = _required_evidence_id(
        manifest.get("cutover_window_id"),
        reason_code="cutover_window_id_invalid",
    )
    candidate = _candidate(manifest.get("candidate"))
    evaluated_at = now or datetime.now(timezone.utc)
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise CutoverManifestFailure("evaluation_time_not_timezone_aware")
    evaluated_at = evaluated_at.astimezone(timezone.utc)

    certifications = manifest.get("certifications")
    if not isinstance(certifications, Mapping):
        raise CutoverManifestFailure("certifications_not_object")

    issues: list[dict[str, str]] = []
    if candidate["target"] != "production":
        issues.append(_issue("candidate", "candidate_target_not_production"))

    expected_gates = set(GATE_SPECS)
    observed_gates = set(certifications)
    for gate in sorted(expected_gates - observed_gates):
        issues.append(_issue(gate, "certification_missing"))
    for gate in sorted(observed_gates - expected_gates):
        issues.append(_issue(gate, "certification_unexpected"))

    evidence_digest_owners: dict[str, str] = {}
    gate_ready: dict[str, bool] = {}
    for gate in GATE_SPECS:
        if gate not in certifications:
            gate_ready[gate] = False
            continue
        gate_issues, digest = _validate_certification(
            gate,
            certifications[gate],
            release_id=release_id,
            cutover_window_id=cutover_window_id,
            candidate=candidate,
            now=evaluated_at,
        )
        issues.extend(gate_issues)
        if digest is not None:
            owner = evidence_digest_owners.get(digest)
            if owner is not None:
                issues.append(_issue(gate, "evidence_digest_reused"))
                gate_issues.append(_issue(gate, "evidence_digest_reused"))
            else:
                evidence_digest_owners[digest] = gate
        gate_ready[gate] = not gate_issues and digest is not None

    manifest_digest = hashlib.sha256(
        json.dumps(
            manifest,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    candidate_ready = candidate["target"] == "production"
    pre_switch_issue = any(
        issue["gate"] in {*PRE_SWITCH_GATES, "candidate"}
        or issue["reason_code"] == "certification_unexpected"
        for issue in issues
    )
    pre_switch_ready = (
        candidate_ready
        and not pre_switch_issue
        and all(gate_ready.get(gate, False) for gate in PRE_SWITCH_GATES)
    )
    ready = pre_switch_ready and gate_ready.get(
        "live_provider_canary",
        False,
    ) and not issues
    if ready:
        status = "closure_ready_for_operator_review"
    elif pre_switch_ready:
        status = "awaiting_live_provider_canary"
    else:
        status = "blocked"
    return {
        "contract_version": CONTRACT_VERSION,
        "status": status,
        "ready": ready,
        "pre_switch_ready": pre_switch_ready,
        "mode": "audit_only",
        "manifest_sha256": manifest_digest,
        "candidate": candidate,
        "gate_count": len(GATE_SPECS),
        "certified_gate_count": sum(gate_ready.values()),
        "gates": gate_ready,
        "critical_replacements": {
            gate: gate_ready.get(gate, False)
            for gate in CRITICAL_REPLACEMENT_GATES
        },
        "issues": issues,
        "safety": {
            "read_only": True,
            "external_actions_performed": False,
            "production_mutations_performed": False,
            "secret_values_emitted": False,
            "authorizes_cutover": False,
        },
    }


def _reject_duplicate_keys(
    pairs: Sequence[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CutoverManifestFailure("manifest_duplicate_key")
        result[key] = value
    return result


def load_manifest(path: Path) -> Any:
    """Load one bounded regular JSON file without reflecting its contents."""

    try:
        if not path.is_file() or path.is_symlink():
            raise CutoverManifestFailure("manifest_path_not_regular_file")
        size = path.stat().st_size
        if size <= 0 or size > MAX_MANIFEST_BYTES:
            raise CutoverManifestFailure("manifest_size_invalid")
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except CutoverManifestFailure:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CutoverManifestFailure("manifest_read_invalid") from exc


def _blocked_payload(reason_code: str) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "status": "blocked",
        "ready": False,
        "mode": "audit_only",
        "reason_code": reason_code,
        "safety": {
            "read_only": True,
            "external_actions_performed": False,
            "production_mutations_performed": False,
            "secret_values_emitted": False,
            "authorizes_cutover": False,
        },
    }


class _RedactedArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise CutoverManifestFailure("command_arguments_invalid")


def build_parser() -> argparse.ArgumentParser:
    parser = _RedactedArgumentParser(
        description="Audita el manifiesto final de cutover sin I/O externo.",
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="Confirmacion obligatoria; el comando no tiene modo mutante.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if not args.audit_only:
            raise CutoverManifestFailure("audit_only_required")
        report = audit_manifest(load_manifest(args.manifest))
    except CutoverManifestFailure as exc:
        print(json.dumps(_blocked_payload(exc.reason_code), sort_keys=True))
        return 2

    print(json.dumps(report, sort_keys=True))
    return 0 if report["ready"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
