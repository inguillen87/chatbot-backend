from __future__ import annotations

import json

import pytest

from scripts.audit_vercel_cron_ownership import (
    CONTRACT_VERSION,
    CronOwnershipAuditFailure,
    audit_cron_ownership,
    main,
)


EXPECTED_CRONS = [
    {
        "path": "/api/internal/cron/outbox-reconciliation",
        "schedule": "* * * * *",
    },
    {
        "path": "/api/internal/cron/whatsapp-payload-retention",
        "schedule": "43 3 * * *",
    },
    {
        "path": "/api/internal/cron/survey-privacy-retention",
        "schedule": "17 3 * * *",
    },
    {
        "path": "/api/internal/cron/weekly-analytics-report",
        "schedule": "0 0 * * 0",
    },
]
DEPLOYMENT_ID = "dpl_HWoecwQtVvuxNwr3x5nddSatF4zj"
RUNTIME_REVISION = "21e77ca02be9ab0f0875b65622b898b16885f087"


def _config():
    return {"framework": "container", "crons": EXPECTED_CRONS}


def _registry():
    return {"crons": {"definitions": EXPECTED_CRONS}, "enabled": True}


def _environment(**overrides):
    values = {
        "VERCEL_OUTBOX_CRON_ENABLED": "false",
        "VERCEL_MAINTENANCE_CRONS_ENABLED": "false",
        "VERCEL_WHATSAPP_PAYLOAD_RETENTION_CRON_ENABLED": "false",
        "VERCEL_SURVEY_PRIVACY_RETENTION_CRON_ENABLED": "false",
        "VERCEL_WEEKLY_ANALYTICS_CRON_ENABLED": "false",
        "CRON_SECRET": {"configured": False, "utf8_bytes": 0},
    }
    values.update(overrides)
    return {"env": values}


def _runtime_probes(**overrides):
    body = {
        "contract_version": "cutover.background_writer_fence.v1",
        "component": "internal_cron",
        "executed": False,
        "reason_code": "cutover_writer_fence_enabled",
        "status": "fenced",
    }
    document = {
        "deployment_id": DEPLOYMENT_ID,
        "runtime_revision": RUNTIME_REVISION,
        "probes": [
            {
                "path": item["path"],
                "status_code": 503,
                "headers": {
                    "Cache-Control": "no-store",
                    "Retry-After": "60",
                },
                "body": body,
            }
            for item in EXPECTED_CRONS
        ],
    }
    document.update(overrides)
    return document


def _audit(config=None, registry=None, environment=None, probes=None):
    return audit_cron_ownership(
        _config() if config is None else config,
        _registry() if registry is None else registry,
        _environment() if environment is None else environment,
        _runtime_probes() if probes is None else probes,
        expected_deployment_id=DEPLOYMENT_ID,
        expected_runtime_revision=RUNTIME_REVISION,
    )


def _codes(report):
    return [issue["code"] for issue in report["issues"]]


def test_exact_registry_with_explicitly_disabled_flags_is_certified_inert():
    report = _audit()

    assert report["contract_version"] == CONTRACT_VERSION
    assert report["ready"] is True
    assert report["status"] == "ready"
    assert report["registry"] == {
        "status": "exact",
        "expected_count": 4,
        "observed_count": 4,
        "exact": True,
        "scheduler_enabled": True,
    }
    assert report["activation"] == {
        "state": "registered_inert",
        "enabled_flags": [],
        "active_paths": [],
    }
    assert report["rollback"]["requires_redeploy_or_explicit_disable"] is True
    assert report["safety"]["external_actions_performed"] is False


def test_enabled_registered_cron_requires_and_accepts_strong_redacted_secret():
    report = _audit(
        environment=_environment(
            VERCEL_OUTBOX_CRON_ENABLED=True,
            CRON_SECRET={"configured": True, "utf8_bytes": 48},
        ),
    )

    assert report["ready"] is True
    assert report["activation"]["state"] == "active"
    assert report["activation"]["active_paths"] == [
        "/api/internal/cron/outbox-reconciliation"
    ]
    assert report["secret"] == {
        "configured": True,
        "strong": True,
        "minimum_utf8_bytes": 32,
    }


@pytest.mark.parametrize(
    "secret",
    [
        {"configured": False, "utf8_bytes": 0},
        {"configured": True, "utf8_bytes": 12},
        "short-secret",
    ],
)
def test_enabled_cron_without_strong_secret_is_blocked_without_echo(secret):
    report = _audit(
        environment=_environment(
            VERCEL_WEEKLY_ANALYTICS_CRON_ENABLED=True,
            CRON_SECRET=secret,
        ),
    )

    assert report["ready"] is False
    assert "enabled_cron_without_strong_secret" in _codes(report)
    assert "short-secret" not in json.dumps(report)


def test_enabled_cron_with_only_presence_metadata_is_not_falsely_certified():
    report = _audit(
        environment=_environment(
            VERCEL_WHATSAPP_PAYLOAD_RETENTION_CRON_ENABLED=True,
            CRON_SECRET={"configured": True},
        ),
    )

    assert report["ready"] is False
    assert "enabled_cron_secret_strength_unverified" in _codes(report)
    assert report["secret"]["configured"] is True
    assert report["secret"]["strong"] is None


@pytest.mark.parametrize(
    ("flag", "path"),
    [
        (
            "VERCEL_WHATSAPP_PAYLOAD_RETENTION_CRON_ENABLED",
            "/api/internal/cron/whatsapp-payload-retention",
        ),
        (
            "VERCEL_SURVEY_PRIVACY_RETENTION_CRON_ENABLED",
            "/api/internal/cron/survey-privacy-retention",
        ),
    ],
)
def test_each_retention_flag_activates_only_its_own_registered_path(flag, path):
    report = _audit(
        environment=_environment(
            **{
                flag: True,
                "CRON_SECRET": {"configured": True, "utf8_bytes": 48},
            }
        )
    )

    assert report["ready"] is True
    assert report["activation"]["enabled_flags"] == [flag]
    assert report["activation"]["active_paths"] == [path]


def test_legacy_maintenance_umbrella_must_remain_disabled():
    report = _audit(
        environment=_environment(VERCEL_MAINTENANCE_CRONS_ENABLED=True)
    )

    assert report["ready"] is False
    assert _codes(report) == [
        "legacy_maintenance_cron_flag_must_be_disabled"
    ]
    assert report["activation"]["active_paths"] == []


def test_empty_registry_is_a_blocking_no_owner_state():
    report = _audit(registry={"definitions": [], "enabled": True})

    assert report["ready"] is False
    assert report["registry"]["status"] == "empty"
    assert report["activation"]["state"] == "unregistered"
    assert _codes(report) == ["registry_empty"]


def test_repository_inventory_cannot_shrink_or_expand_the_required_gate():
    missing = _audit(
        config={"crons": EXPECTED_CRONS[:-1]},
        registry={"crons": {"definitions": EXPECTED_CRONS[:-1]}, "enabled": True},
        probes=_runtime_probes(probes=_runtime_probes()["probes"][:-1]),
    )
    unexpected_definition = {
        "path": "/api/internal/cron/unapproved-job",
        "schedule": "*/15 * * * *",
    }
    expanded_config = {"crons": [*EXPECTED_CRONS, unexpected_definition]}
    expanded_registry = {
        "crons": {"definitions": [*EXPECTED_CRONS, unexpected_definition]},
        "enabled": True,
    }
    expanded_probes = _runtime_probes()
    expanded_probes["probes"].append(
        {
            "path": unexpected_definition["path"],
            "status_code": 503,
            "headers": {"Cache-Control": "no-store", "Retry-After": "60"},
            "body": {
                "contract_version": "cutover.background_writer_fence.v1",
                "component": "internal_cron",
                "executed": False,
                "reason_code": "cutover_writer_fence_enabled",
                "status": "fenced",
            },
        }
    )
    expanded = _audit(
        config=expanded_config,
        registry=expanded_registry,
        probes=expanded_probes,
    )

    assert missing["ready"] is False
    assert "repository_cron_definition_missing" in _codes(missing)
    assert expanded["ready"] is False
    assert "repository_cron_definition_unexpected" in _codes(expanded)


def test_divergent_registry_reports_missing_unexpected_and_schedule_mismatch():
    registry = {
        "definitions": [
            {
                "path": "/api/internal/cron/outbox-reconciliation",
                "schedule": "*/5 * * * *",
            },
            {
                "path": "/api/internal/cron/unexpected",
                "schedule": "0 1 * * *",
            },
        ]
    }
    registry["enabled"] = True
    report = _audit(registry=registry)

    assert report["ready"] is False
    assert report["registry"]["status"] == "divergent"
    assert set(_codes(report)) == {
        "registry_definition_missing",
        "registry_definition_unexpected",
        "registry_schedule_mismatch",
    }


def test_enabled_flag_without_registered_definition_is_explicitly_blocked():
    registry = {
        "definitions": [
            item
            for item in EXPECTED_CRONS
            if item["path"] != "/api/internal/cron/weekly-analytics-report"
        ]
    }
    registry["enabled"] = True
    report = _audit(
        registry=registry,
        environment=_environment(
            VERCEL_WEEKLY_ANALYTICS_CRON_ENABLED=True,
            CRON_SECRET={"configured": True, "utf8_bytes": 64},
        ),
    )

    assert report["ready"] is False
    assert "enabled_cron_without_registered_definition" in _codes(report)


def test_missing_or_invalid_flags_prevent_ownership_certification():
    env = _environment()["env"]
    env.pop("VERCEL_OUTBOX_CRON_ENABLED")
    env["VERCEL_WHATSAPP_PAYLOAD_RETENTION_CRON_ENABLED"] = "maybe"

    report = _audit(environment=env)

    assert report["ready"] is False
    assert "cron_flag_not_declared" in _codes(report)
    assert "cron_flag_invalid" in _codes(report)


@pytest.mark.parametrize(
    ("registry", "reason_code"),
    [
        ({"crons": EXPECTED_CRONS}, "registry_scheduler_state_unverified"),
        (
            {"crons": EXPECTED_CRONS, "enabled": False},
            "registry_scheduler_disabled",
        ),
    ],
)
def test_scheduler_state_must_be_explicitly_enabled(registry, reason_code):
    report = _audit(registry=registry)

    assert report["ready"] is False
    assert reason_code in _codes(report)


def test_runtime_probes_are_bound_to_exact_deployment_and_revision():
    report = _audit(
        probes=_runtime_probes(
            deployment_id="dpl_anotherDeployment123456789",
            runtime_revision="f" * 40,
        )
    )

    assert report["ready"] is False
    assert set(_codes(report)) == {
        "runtime_probe_deployment_mismatch",
        "runtime_probe_revision_mismatch",
    }
    assert report["runtime_fail_closed"]["deployment_match"] is False
    assert report["runtime_fail_closed"]["runtime_revision_match"] is False


def test_incomplete_or_non_fenced_runtime_probe_blocks_certification():
    probes = _runtime_probes()["probes"][:-1]
    probes[0] = {
        **probes[0],
        "status_code": 200,
        "headers": {"Cache-Control": "public", "Retry-After": "5"},
        "body": {"status": "completed", "executed": True},
    }

    report = _audit(probes=_runtime_probes(probes=probes))

    assert report["ready"] is False
    assert set(_codes(report)) == {
        "runtime_probe_missing",
        "runtime_probe_status_mismatch",
        "runtime_probe_cache_control_invalid",
        "runtime_probe_retry_after_invalid",
        "runtime_probe_body_mismatch",
    }
    assert report["runtime_fail_closed"]["all_fail_closed"] is False


def test_duplicate_registry_definition_is_rejected_as_invalid_input():
    with pytest.raises(CronOwnershipAuditFailure) as captured:
        audit_cron_ownership(
            _config(),
            {
                "definitions": [EXPECTED_CRONS[0], EXPECTED_CRONS[0]],
                "enabled": True,
            },
            _environment(),
            _runtime_probes(),
            expected_deployment_id=DEPLOYMENT_ID,
            expected_runtime_revision=RUNTIME_REVISION,
        )

    assert captured.value.reason_code == "registry_definition_duplicate"


def test_cli_requires_explicit_audit_only_and_never_echoes_paths(tmp_path, capsys):
    secret_named_path = tmp_path / "private-provider-token.json"
    secret_named_path.write_text(json.dumps(_config()), encoding="utf-8")
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(_registry()), encoding="utf-8")
    env_path = tmp_path / "env.json"
    env_path.write_text(json.dumps(_environment()), encoding="utf-8")
    probe_path = tmp_path / "probes.json"
    probe_path.write_text(json.dumps(_runtime_probes()), encoding="utf-8")

    exit_code = main(
        [
            "--vercel-config",
            str(secret_named_path),
            "--registry-json",
            str(registry_path),
            "--env-json",
            str(env_path),
            "--runtime-probes-json",
            str(probe_path),
            "--expected-deployment-id",
            DEPLOYMENT_ID,
            "--expected-runtime-revision",
            RUNTIME_REVISION,
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 2
    assert json.loads(output)["reason_code"] == "audit_only_required"
    assert "private-provider-token" not in output


def test_cli_emits_redacted_blocking_report_and_nonzero_exit(tmp_path, capsys):
    paths = []
    for name, document in (
        ("vercel.json", _config()),
        ("registry.json", {"definitions": [], "enabled": True}),
        (
            "env.json",
            _environment(CRON_SECRET="do-not-print-this-secret"),
        ),
        ("probes.json", _runtime_probes()),
    ):
        path = tmp_path / name
        path.write_text(json.dumps(document), encoding="utf-8")
        paths.append(path)

    exit_code = main(
        [
            "--vercel-config",
            str(paths[0]),
            "--registry-json",
            str(paths[1]),
            "--env-json",
            str(paths[2]),
            "--runtime-probes-json",
            str(paths[3]),
            "--expected-deployment-id",
            DEPLOYMENT_ID,
            "--expected-runtime-revision",
            RUNTIME_REVISION,
            "--audit-only",
        ]
    )
    output = capsys.readouterr().out
    report = json.loads(output)

    assert exit_code == 1
    assert report["status"] == "blocked"
    assert "do-not-print-this-secret" not in output
    assert report["safety"]["secret_values_emitted"] is False
