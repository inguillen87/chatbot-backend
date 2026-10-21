"""Audit Vercel cron registration and activation without changing Vercel.

The command consumes six local JSON snapshots: the repository ``vercel.json``,
the project's observed cron registry, a redacted/declarative environment
snapshot, responses captured from the exact fenced runtime, an immutable
``vercel inspect --json`` deployment snapshot, and a redacted ``/api/version``
probe.  It performs no network calls, imports no application modules and never
echoes environment values.  This makes it suitable for a cutover gate after
read-only registry and runtime evidence has been captured separately.

Accepted registry shapes include ``{"definitions": [...]}``,
``{"crons": [...]}``, and ``{"crons": {"definitions": [...]}}``.  The
environment snapshot can be a plain mapping, an ``env`` mapping, or an
``envs`` list with ``key``/``value`` entries.  ``CRON_SECRET`` may be supplied
as a raw value (it is measured but never emitted) or, preferably, as redacted
metadata such as ``{"configured": true, "utf8_bytes": 48}``.

The runtime snapshot must bind the four captured responses to the approved
``deployment_id``, ``runtime_revision`` and immutable Vercel host.  A fifth
snapshot from ``vercel inspect --json`` proves that this host is the approved,
READY Production deployment and that its built artifact contains the exact
repository cron inventory.  A sixth snapshot from ``/api/version`` binds the
served runtime to the full approved Git SHA.  Every cron response must be the
stable ``503`` background-fence contract with ``Cache-Control: no-store`` and
``Retry-After: 60``.  The operator captures these responses only after the
writer fence is independently known to be active; this auditor never invokes a
mutating GET route itself.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


CONTRACT_VERSION = "chatboc.vercel_cron_ownership_audit.v3"
MAX_INPUT_BYTES = 256 * 1024
MINIMUM_CRON_SECRET_UTF8_BYTES = 32
FENCED_PROBE_CONTRACT = "cutover.background_writer_fence.v1"
_DEPLOYMENT_ID_RE = re.compile(r"^dpl_[A-Za-z0-9]{8,128}$")
_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

FLAG_BY_PATH = {
    "/api/internal/cron/outbox-reconciliation": "VERCEL_OUTBOX_CRON_ENABLED",
    "/api/internal/cron/whatsapp-payload-retention": (
        "VERCEL_MAINTENANCE_CRONS_ENABLED"
    ),
    "/api/internal/cron/survey-privacy-retention": (
        "VERCEL_MAINTENANCE_CRONS_ENABLED"
    ),
    "/api/internal/cron/weekly-analytics-report": (
        "VERCEL_WEEKLY_ANALYTICS_CRON_ENABLED"
    ),
}
REQUIRED_FLAGS = tuple(sorted(set(FLAG_BY_PATH.values())))


class CronOwnershipAuditFailure(RuntimeError):
    """Stable, non-sensitive reason for an invalid audit input."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _load_json(path: Path) -> Any:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CronOwnershipAuditFailure("audit_input_unreadable") from exc
    if size > MAX_INPUT_BYTES:
        raise CronOwnershipAuditFailure("audit_input_too_large")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CronOwnershipAuditFailure("audit_input_invalid_json") from exc


def _definition_list(document: Any, *, source: str) -> list[Any]:
    if not isinstance(document, Mapping):
        raise CronOwnershipAuditFailure(f"{source}_not_object")

    if source == "vercel_config":
        definitions = document.get("crons")
    elif isinstance(document.get("definitions"), list):
        definitions = document.get("definitions")
    else:
        crons = document.get("crons")
        if isinstance(crons, Mapping):
            definitions = crons.get("definitions")
        else:
            definitions = crons

    if definitions is None and source == "registry":
        # Some read-only project snapshots nest the registry one level deeper.
        project = document.get("project")
        if isinstance(project, Mapping):
            return _definition_list(project, source=source)
    if not isinstance(definitions, list):
        raise CronOwnershipAuditFailure(f"{source}_definitions_not_array")
    return definitions


def _normalize_definitions(document: Any, *, source: str) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw in _definition_list(document, source=source):
        if not isinstance(raw, Mapping):
            raise CronOwnershipAuditFailure(f"{source}_definition_invalid")
        path = raw.get("path")
        schedule = raw.get("schedule")
        if not isinstance(path, str) or not path.startswith("/"):
            raise CronOwnershipAuditFailure(f"{source}_definition_path_invalid")
        if not isinstance(schedule, str) or not schedule.strip():
            raise CronOwnershipAuditFailure(
                f"{source}_definition_schedule_invalid"
            )
        path = path.strip()
        schedule = " ".join(schedule.split())
        if path in normalized:
            raise CronOwnershipAuditFailure(f"{source}_definition_duplicate")
        normalized[path] = schedule
    return normalized


def _canonical_vercel_host(value: Any, *, reason_code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CronOwnershipAuditFailure(reason_code)
    rendered = value.strip()
    if "://" not in rendered:
        rendered = f"https://{rendered}"
    try:
        parsed = urlsplit(rendered)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise CronOwnershipAuditFailure(reason_code) from exc
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
        raise CronOwnershipAuditFailure(reason_code)
    return host


def _full_revision(value: Any, *, reason_code: str) -> str:
    if not isinstance(value, str):
        raise CronOwnershipAuditFailure(reason_code)
    revision = value.strip().lower()
    if not _FULL_GIT_SHA_RE.fullmatch(revision):
        raise CronOwnershipAuditFailure(reason_code)
    return revision


def _registry_owner_hosts(document: Any) -> dict[str, str]:
    hosts: dict[str, str] = {}
    for raw in _definition_list(document, source="registry"):
        if not isinstance(raw, Mapping):
            raise CronOwnershipAuditFailure("registry_definition_invalid")
        path = raw.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            raise CronOwnershipAuditFailure("registry_definition_path_invalid")
        hosts[path.strip()] = _canonical_vercel_host(
            raw.get("host"),
            reason_code="registry_definition_host_invalid",
        )
    return hosts


def _registry_drift(document: Any) -> tuple[list[Any], list[Any]]:
    if not isinstance(document, Mapping):
        raise CronOwnershipAuditFailure("registry_not_object")
    undeployed = document.get("undeployed")
    modified = document.get("modified")
    if not isinstance(undeployed, list) or not isinstance(modified, list):
        raise CronOwnershipAuditFailure("registry_drift_state_unverified")
    return undeployed, modified


def _deployment_evidence(
    document: Any,
    *,
    expected_deployment_id: str,
    expected_project_name: str,
    expected_definitions: Mapping[str, str],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if not isinstance(document, Mapping):
        raise CronOwnershipAuditFailure("deployment_snapshot_not_object")
    deployment_id = document.get("id")
    if not isinstance(deployment_id, str) or not _DEPLOYMENT_ID_RE.fullmatch(
        deployment_id.strip()
    ):
        raise CronOwnershipAuditFailure("deployment_snapshot_id_invalid")
    deployment_id = deployment_id.strip()
    deployment_host = _canonical_vercel_host(
        document.get("url"),
        reason_code="deployment_snapshot_url_invalid",
    )
    project_name = document.get("name")
    target = document.get("target")
    ready_state = document.get("readyState")
    issues: list[dict[str, str]] = []
    if deployment_id != expected_deployment_id:
        issues.append(_issue("deployment_id_mismatch"))
    if project_name != expected_project_name:
        issues.append(_issue("deployment_project_mismatch"))
    if target != "production":
        issues.append(_issue("deployment_target_not_production"))
    if ready_state != "READY":
        issues.append(_issue("deployment_not_ready"))

    builds = document.get("builds")
    if not isinstance(builds, list) or not builds:
        raise CronOwnershipAuditFailure("deployment_builds_missing")
    artifact_definitions: dict[str, str] | None = None
    build_exact = True
    for build in builds:
        if not isinstance(build, Mapping):
            raise CronOwnershipAuditFailure("deployment_build_invalid")
        if build.get("deploymentId") != deployment_id:
            issues.append(_issue("deployment_build_owner_mismatch"))
            build_exact = False
        if build.get("readyState") != "READY":
            issues.append(_issue("deployment_build_not_ready"))
            build_exact = False
        config = build.get("config")
        vercel_config = config.get("vercelConfig") if isinstance(config, Mapping) else None
        if isinstance(vercel_config, Mapping) and isinstance(
            vercel_config.get("crons"), list
        ):
            candidate = _normalize_definitions(
                {"crons": vercel_config.get("crons")},
                source="vercel_config",
            )
            if artifact_definitions is not None and candidate != artifact_definitions:
                issues.append(_issue("deployment_build_cron_inventory_divergent"))
                build_exact = False
            artifact_definitions = candidate
    if artifact_definitions is None:
        issues.append(_issue("deployment_build_cron_inventory_missing"))
        build_exact = False
    elif dict(expected_definitions) != artifact_definitions:
        issues.append(_issue("deployment_build_cron_inventory_mismatch"))
        build_exact = False

    return issues, {
        "deployment_id_match": deployment_id == expected_deployment_id,
        "project_match": project_name == expected_project_name,
        "target": target,
        "ready_state": ready_state,
        "host": deployment_host,
        "artifact_crons_exact": build_exact,
    }


def _version_probe_evidence(
    document: Any,
    *,
    expected_host: str,
    expected_runtime_revision: str,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if not isinstance(document, Mapping):
        raise CronOwnershipAuditFailure("version_probe_not_object")
    probe_host = _canonical_vercel_host(
        document.get("host"),
        reason_code="version_probe_host_invalid",
    )
    status_code = document.get("status_code")
    body = document.get("body")
    if not isinstance(body, Mapping):
        raise CronOwnershipAuditFailure("version_probe_body_invalid")
    served_revision = body.get("backend")
    issues: list[dict[str, str]] = []
    if probe_host != expected_host:
        issues.append(_issue("version_probe_host_mismatch"))
    if isinstance(status_code, bool) or status_code != 200:
        issues.append(_issue("version_probe_status_mismatch"))
    try:
        normalized_revision = _full_revision(
            served_revision,
            reason_code="version_probe_revision_invalid",
        )
    except CronOwnershipAuditFailure:
        normalized_revision = None
        issues.append(_issue("version_probe_revision_invalid"))
    if (
        normalized_revision is not None
        and normalized_revision != expected_runtime_revision
    ):
        issues.append(_issue("version_probe_revision_mismatch"))
    return issues, {
        "host_match": probe_host == expected_host,
        "status_code": status_code,
        "revision_match": normalized_revision == expected_runtime_revision,
    }


def _environment_mapping(document: Any) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise CronOwnershipAuditFailure("environment_not_object")

    for key in ("env", "environment"):
        candidate = document.get(key)
        if isinstance(candidate, Mapping):
            return dict(candidate)

    envs = document.get("envs")
    if isinstance(envs, list):
        result: dict[str, Any] = {}
        for raw in envs:
            if not isinstance(raw, Mapping):
                raise CronOwnershipAuditFailure("environment_entry_invalid")
            name = raw.get("key") or raw.get("name")
            if not isinstance(name, str) or not name:
                raise CronOwnershipAuditFailure("environment_entry_key_invalid")
            # Preserve redacted metadata when a Vercel snapshot has no value.
            result[name] = raw.get("value", raw)
        return result

    return dict(document)


def _scheduler_enabled(document: Any) -> bool | None:
    if not isinstance(document, Mapping):
        raise CronOwnershipAuditFailure("registry_not_object")
    value = document.get("enabled")
    return value if isinstance(value, bool) else None


def _runtime_probe_document(
    document: Any,
) -> tuple[str, str, str, dict[str, Any]]:
    if not isinstance(document, Mapping):
        raise CronOwnershipAuditFailure("runtime_probe_not_object")
    deployment_id = document.get("deployment_id")
    runtime_revision = document.get("runtime_revision")
    runtime_host = _canonical_vercel_host(
        document.get("host"),
        reason_code="runtime_probe_host_invalid",
    )
    probes = document.get("probes")
    if not isinstance(deployment_id, str) or not deployment_id.strip():
        raise CronOwnershipAuditFailure("runtime_probe_deployment_id_invalid")
    runtime_revision = _full_revision(
        runtime_revision,
        reason_code="runtime_probe_revision_invalid",
    )
    if not isinstance(probes, list):
        raise CronOwnershipAuditFailure("runtime_probes_not_array")

    normalized: dict[str, Any] = {}
    for raw in probes:
        if not isinstance(raw, Mapping):
            raise CronOwnershipAuditFailure("runtime_probe_entry_invalid")
        path = raw.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            raise CronOwnershipAuditFailure("runtime_probe_path_invalid")
        if path in normalized:
            raise CronOwnershipAuditFailure("runtime_probe_duplicate")
        normalized[path] = raw
    return deployment_id.strip(), runtime_revision, runtime_host, normalized


def _header_value(headers: Any, name: str) -> str | None:
    if not isinstance(headers, Mapping):
        return None
    expected = name.lower()
    for key, value in headers.items():
        if (
            isinstance(key, str)
            and key.lower() == expected
            and isinstance(value, str)
        ):
            return value.strip()
    return None


def _runtime_probe_issues(
    document: Any,
    *,
    expected_paths: set[str],
    expected_deployment_id: str,
    expected_runtime_revision: str,
    expected_host: str,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    deployment_id, runtime_revision, runtime_host, probes = (
        _runtime_probe_document(document)
    )
    issues: list[dict[str, str]] = []
    if deployment_id != expected_deployment_id:
        issues.append(_issue("runtime_probe_deployment_mismatch"))
    if runtime_revision != expected_runtime_revision:
        issues.append(_issue("runtime_probe_revision_mismatch"))
    if runtime_host != expected_host:
        issues.append(_issue("runtime_probe_host_mismatch"))

    probe_paths = set(probes)
    for path in sorted(expected_paths - probe_paths):
        issues.append(_issue("runtime_probe_missing", path=path))
    for path in sorted(probe_paths - expected_paths):
        issues.append(_issue("runtime_probe_unexpected", path=path))

    exact_paths: list[str] = []
    expected_body = {
        "contract_version": FENCED_PROBE_CONTRACT,
        "component": "internal_cron",
        "executed": False,
        "reason_code": "cutover_writer_fence_enabled",
        "status": "fenced",
    }
    for path in sorted(expected_paths & probe_paths):
        probe = probes[path]
        path_ready = True
        status_code = probe.get("status_code")
        if isinstance(status_code, bool) or status_code != 503:
            issues.append(_issue("runtime_probe_status_mismatch", path=path))
            path_ready = False
        cache_control = _header_value(probe.get("headers"), "cache-control")
        directives = {
            item.strip().lower()
            for item in (cache_control or "").split(",")
            if item.strip()
        }
        if "no-store" not in directives:
            issues.append(_issue("runtime_probe_cache_control_invalid", path=path))
            path_ready = False
        if _header_value(probe.get("headers"), "retry-after") != "60":
            issues.append(_issue("runtime_probe_retry_after_invalid", path=path))
            path_ready = False
        if probe.get("body") != expected_body:
            issues.append(_issue("runtime_probe_body_mismatch", path=path))
            path_ready = False
        if path_ready:
            exact_paths.append(path)

    return issues, {
        "deployment_match": deployment_id == expected_deployment_id,
        "runtime_revision_match": runtime_revision == expected_runtime_revision,
        "host_match": runtime_host == expected_host,
        "expected_count": len(expected_paths),
        "observed_count": len(probes),
        "exact_paths": exact_paths,
        "all_fail_closed": len(exact_paths) == len(expected_paths),
    }


def _parse_flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, Mapping):
        if "value" in value:
            return _parse_flag(value.get("value"))
        if "enabled" in value:
            return _parse_flag(value.get("enabled"))
    return None


def _secret_metadata(value: Any) -> tuple[bool | None, int | None]:
    if value is None:
        return False, 0
    if isinstance(value, str):
        try:
            return bool(value), len(value.encode("utf-8"))
        except UnicodeEncodeError:
            return bool(value), None
    if not isinstance(value, Mapping):
        return None, None

    if isinstance(value.get("value"), str):
        return _secret_metadata(value.get("value"))

    configured_raw = value.get("configured", value.get("present"))
    configured = configured_raw if isinstance(configured_raw, bool) else None
    byte_count = value.get("utf8_bytes")
    if isinstance(byte_count, bool) or not isinstance(byte_count, int):
        byte_count = None
    elif byte_count < 0:
        byte_count = None
    if configured is False:
        return False, 0
    return configured, byte_count


def _issue(code: str, **context: str) -> dict[str, str]:
    return {"severity": "error", "code": code, **context}


def audit_cron_ownership(
    vercel_config: Any,
    registry_snapshot: Any,
    environment_snapshot: Any,
    runtime_probe_snapshot: Any,
    deployment_snapshot: Any,
    version_probe_snapshot: Any,
    *,
    expected_deployment_id: str,
    expected_runtime_revision: str,
    expected_project_name: str,
) -> dict[str, Any]:
    """Return redacted cron ownership evidence from local snapshots only."""

    if not isinstance(expected_deployment_id, str) or not _DEPLOYMENT_ID_RE.fullmatch(
        expected_deployment_id.strip()
    ):
        raise CronOwnershipAuditFailure("expected_deployment_id_invalid")
    expected_deployment_id = expected_deployment_id.strip()
    expected_runtime_revision = _full_revision(
        expected_runtime_revision,
        reason_code="expected_runtime_revision_invalid",
    )
    if (
        not isinstance(expected_project_name, str)
        or not expected_project_name.strip()
        or len(expected_project_name.strip()) > 100
    ):
        raise CronOwnershipAuditFailure("expected_project_name_invalid")
    expected_project_name = expected_project_name.strip()

    expected = _normalize_definitions(vercel_config, source="vercel_config")
    observed = _normalize_definitions(registry_snapshot, source="registry")
    registry_hosts = _registry_owner_hosts(registry_snapshot)
    undeployed, modified = _registry_drift(registry_snapshot)
    scheduler_enabled = _scheduler_enabled(registry_snapshot)
    environment = _environment_mapping(environment_snapshot)
    issues: list[dict[str, str]] = []

    deployment_issues, candidate = _deployment_evidence(
        deployment_snapshot,
        expected_deployment_id=expected_deployment_id,
        expected_project_name=expected_project_name,
        expected_definitions=expected,
    )
    issues.extend(deployment_issues)
    candidate_host = candidate["host"]
    version_issues, version_probe = _version_probe_evidence(
        version_probe_snapshot,
        expected_host=candidate_host,
        expected_runtime_revision=expected_runtime_revision,
    )
    issues.extend(version_issues)

    expected_paths = set(expected)
    observed_paths = set(observed)
    required_paths = set(FLAG_BY_PATH)
    for path in sorted(required_paths - expected_paths):
        issues.append(_issue("repository_cron_definition_missing", path=path))
    for path in sorted(expected_paths - required_paths):
        issues.append(_issue("repository_cron_definition_unexpected", path=path))
    if expected and not observed:
        issues.append(_issue("registry_empty"))
    else:
        for path in sorted(expected_paths - observed_paths):
            issues.append(_issue("registry_definition_missing", path=path))
        for path in sorted(observed_paths - expected_paths):
            issues.append(_issue("registry_definition_unexpected", path=path))
        for path in sorted(expected_paths & observed_paths):
            if expected[path] != observed[path]:
                issues.append(_issue("registry_schedule_mismatch", path=path))

    registry_owner_set = set(registry_hosts.values())
    if len(registry_owner_set) > 1:
        issues.append(_issue("registry_multiple_owner_hosts"))
    for path in sorted(expected_paths & observed_paths):
        if registry_hosts.get(path) != candidate_host:
            issues.append(_issue("registry_owner_host_mismatch", path=path))
    if undeployed:
        issues.append(_issue("registry_has_undeployed_definitions"))
    if modified:
        issues.append(_issue("registry_has_modified_definitions"))

    flag_states: dict[str, bool | None] = {}
    for flag in REQUIRED_FLAGS:
        if flag not in environment:
            flag_states[flag] = None
            issues.append(_issue("cron_flag_not_declared", flag=flag))
            continue
        flag_states[flag] = _parse_flag(environment[flag])
        if flag_states[flag] is None:
            issues.append(_issue("cron_flag_invalid", flag=flag))

    enabled_flags = sorted(
        flag for flag, enabled in flag_states.items() if enabled is True
    )
    secret_configured, secret_bytes = _secret_metadata(
        environment.get("CRON_SECRET")
    )
    secret_strong = (
        secret_bytes >= MINIMUM_CRON_SECRET_UTF8_BYTES
        if isinstance(secret_bytes, int)
        else None
    )
    if enabled_flags:
        if secret_configured is not True or secret_strong is False:
            issues.append(_issue("enabled_cron_without_strong_secret"))
        elif secret_strong is None:
            issues.append(_issue("enabled_cron_secret_strength_unverified"))

    active_paths: list[str] = []
    for path in sorted(expected_paths):
        flag = FLAG_BY_PATH.get(path)
        if flag is None:
            issues.append(
                _issue("cron_definition_without_activation_flag", path=path)
            )
            continue
        if flag_states.get(flag) is True:
            if path not in observed_paths:
                issues.append(
                    _issue(
                        "enabled_cron_without_registered_definition",
                        path=path,
                        flag=flag,
                    )
                )
            elif observed.get(path) == expected.get(path):
                active_paths.append(path)

    registry_exact = expected == observed
    if scheduler_enabled is None:
        issues.append(_issue("registry_scheduler_state_unverified"))
    elif scheduler_enabled is not True:
        issues.append(_issue("registry_scheduler_disabled"))

    probe_issues, runtime_fail_closed = _runtime_probe_issues(
        runtime_probe_snapshot,
        expected_paths=expected_paths,
        expected_deployment_id=expected_deployment_id,
        expected_runtime_revision=expected_runtime_revision,
        expected_host=candidate_host,
    )
    issues.extend(probe_issues)
    if not observed:
        registry_state = "empty"
    elif registry_exact:
        registry_state = "exact"
    else:
        registry_state = "divergent"

    if not observed:
        activation_state = "unregistered"
    elif active_paths:
        activation_state = "active"
    else:
        activation_state = "registered_inert"

    return {
        "contract_version": CONTRACT_VERSION,
        "status": "ready" if not issues else "blocked",
        "ready": not issues,
        "candidate": {
            **candidate,
            "runtime_revision_match": version_probe["revision_match"],
        },
        "registry": {
            "status": registry_state,
            "expected_count": len(expected),
            "observed_count": len(observed),
            "exact": registry_exact,
            "scheduler_enabled": scheduler_enabled,
            "owner_host_match": bool(registry_hosts)
            and registry_owner_set == {candidate_host},
            "undeployed_count": len(undeployed),
            "modified_count": len(modified),
        },
        "activation": {
            "state": activation_state,
            "enabled_flags": enabled_flags,
            "active_paths": active_paths,
        },
        "secret": {
            "configured": secret_configured,
            "strong": secret_strong,
            "minimum_utf8_bytes": MINIMUM_CRON_SECRET_UTF8_BYTES,
        },
        "runtime_fail_closed": runtime_fail_closed,
        "version_probe": version_probe,
        "overlap": {
            "scope": "vercel_cron_registry",
            "unique_candidate_owner": bool(registry_hosts)
            and registry_owner_set == {candidate_host},
            "global_source_destination_certified": False,
            "global_reason": (
                "requires_shared_writer_authority_and_source_scheduler_evidence"
            ),
        },
        "issues": issues,
        "rollback": {
            "registry_retarget_is_automatic": False,
            "requires_redeploy_or_explicit_disable": True,
            "guidance": (
                "El rollback debe redesplegar la revision propietaria de los "
                "crons o deshabilitar explicitamente sus flags; cambiar DNS o "
                "compute no transfiere el registro de Vercel."
            ),
        },
        "safety": {
            "read_only": True,
            "external_actions_performed": False,
            "secret_values_emitted": False,
        },
    }


def _blocked_payload(reason_code: str) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "status": "blocked",
        "ready": False,
        "reason_code": reason_code,
        "safety": {
            "read_only": True,
            "external_actions_performed": False,
            "secret_values_emitted": False,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audita ownership de crons de Vercel sin mutaciones.",
    )
    parser.add_argument("--vercel-config", required=True, type=Path)
    parser.add_argument("--registry-json", required=True, type=Path)
    parser.add_argument("--env-json", required=True, type=Path)
    parser.add_argument("--runtime-probes-json", required=True, type=Path)
    parser.add_argument("--deployment-json", required=True, type=Path)
    parser.add_argument("--version-probe-json", required=True, type=Path)
    parser.add_argument("--expected-deployment-id", required=True)
    parser.add_argument("--expected-runtime-revision", required=True)
    parser.add_argument("--expected-project-name", required=True)
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="Confirmacion obligatoria de que solo se auditan snapshots locales.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.audit_only:
        print(json.dumps(_blocked_payload("audit_only_required"), sort_keys=True))
        return 2
    try:
        report = audit_cron_ownership(
            _load_json(args.vercel_config),
            _load_json(args.registry_json),
            _load_json(args.env_json),
            _load_json(args.runtime_probes_json),
            _load_json(args.deployment_json),
            _load_json(args.version_probe_json),
            expected_deployment_id=args.expected_deployment_id,
            expected_runtime_revision=args.expected_runtime_revision,
            expected_project_name=args.expected_project_name,
        )
    except CronOwnershipAuditFailure as exc:
        print(json.dumps(_blocked_payload(exc.reason_code), sort_keys=True))
        return 2

    print(json.dumps(report, sort_keys=True))
    return 0 if report["ready"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
