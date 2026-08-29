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


def _config():
    return {"framework": "container", "crons": EXPECTED_CRONS}


def _registry():
    return {"crons": {"definitions": EXPECTED_CRONS}}


def _environment(**overrides):
    values = {
        "VERCEL_OUTBOX_CRON_ENABLED": "false",
        "VERCEL_MAINTENANCE_CRONS_ENABLED": "false",
        "VERCEL_WEEKLY_ANALYTICS_CRON_ENABLED": "false",
        "CRON_SECRET": {"configured": False, "utf8_bytes": 0},
    }
    values.update(overrides)
    return {"env": values}


def _codes(report):
    return [issue["code"] for issue in report["issues"]]


def test_exact_registry_with_explicitly_disabled_flags_is_certified_inert():
    report = audit_cron_ownership(_config(), _registry(), _environment())

    assert report["contract_version"] == CONTRACT_VERSION
    assert report["ready"] is True
    assert report["status"] == "ready"
    assert report["registry"] == {
        "status": "exact",
        "expected_count": 4,
        "observed_count": 4,
        "exact": True,
    }
    assert report["activation"] == {
        "state": "registered_inert",
        "enabled_flags": [],
        "active_paths": [],
    }
    assert report["rollback"]["requires_redeploy_or_explicit_disable"] is True
    assert report["safety"]["external_actions_performed"] is False


def test_enabled_registered_cron_requires_and_accepts_strong_redacted_secret():
    report = audit_cron_ownership(
        _config(),
        _registry(),
        _environment(
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
    report = audit_cron_ownership(
        _config(),
        _registry(),
        _environment(
            VERCEL_WEEKLY_ANALYTICS_CRON_ENABLED=True,
            CRON_SECRET=secret,
        ),
    )

    assert report["ready"] is False
    assert "enabled_cron_without_strong_secret" in _codes(report)
    assert "short-secret" not in json.dumps(report)


def test_enabled_cron_with_only_presence_metadata_is_not_falsely_certified():
    report = audit_cron_ownership(
        _config(),
        _registry(),
        _environment(
            VERCEL_MAINTENANCE_CRONS_ENABLED=True,
            CRON_SECRET={"configured": True},
        ),
    )

    assert report["ready"] is False
    assert "enabled_cron_secret_strength_unverified" in _codes(report)
    assert report["secret"]["configured"] is True
    assert report["secret"]["strong"] is None


def test_empty_registry_is_a_blocking_no_owner_state():
    report = audit_cron_ownership(
        _config(),
        {"definitions": []},
        _environment(),
    )

    assert report["ready"] is False
    assert report["registry"]["status"] == "empty"
    assert report["activation"]["state"] == "unregistered"
    assert _codes(report) == ["registry_empty"]


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
    report = audit_cron_ownership(_config(), registry, _environment())

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
    report = audit_cron_ownership(
        _config(),
        registry,
        _environment(
            VERCEL_WEEKLY_ANALYTICS_CRON_ENABLED=True,
            CRON_SECRET={"configured": True, "utf8_bytes": 64},
        ),
    )

    assert report["ready"] is False
    assert "enabled_cron_without_registered_definition" in _codes(report)


def test_missing_or_invalid_flags_prevent_ownership_certification():
    env = _environment()["env"]
    env.pop("VERCEL_OUTBOX_CRON_ENABLED")
    env["VERCEL_MAINTENANCE_CRONS_ENABLED"] = "maybe"

    report = audit_cron_ownership(_config(), _registry(), env)

    assert report["ready"] is False
    assert "cron_flag_not_declared" in _codes(report)
    assert "cron_flag_invalid" in _codes(report)


def test_duplicate_registry_definition_is_rejected_as_invalid_input():
    with pytest.raises(CronOwnershipAuditFailure) as captured:
        audit_cron_ownership(
            _config(),
            {"definitions": [EXPECTED_CRONS[0], EXPECTED_CRONS[0]]},
            _environment(),
        )

    assert captured.value.reason_code == "registry_definition_duplicate"


def test_cli_requires_explicit_audit_only_and_never_echoes_paths(tmp_path, capsys):
    secret_named_path = tmp_path / "private-provider-token.json"
    secret_named_path.write_text(json.dumps(_config()), encoding="utf-8")
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(_registry()), encoding="utf-8")
    env_path = tmp_path / "env.json"
    env_path.write_text(json.dumps(_environment()), encoding="utf-8")

    exit_code = main(
        [
            "--vercel-config",
            str(secret_named_path),
            "--registry-json",
            str(registry_path),
            "--env-json",
            str(env_path),
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
        ("registry.json", {"definitions": []}),
        (
            "env.json",
            _environment(CRON_SECRET="do-not-print-this-secret"),
        ),
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
            "--audit-only",
        ]
    )
    output = capsys.readouterr().out
    report = json.loads(output)

    assert exit_code == 1
    assert report["status"] == "blocked"
    assert "do-not-print-this-secret" not in output
    assert report["safety"]["secret_values_emitted"] is False
