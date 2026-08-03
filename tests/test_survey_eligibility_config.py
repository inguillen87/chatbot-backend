from __future__ import annotations

from pathlib import Path

import yaml

from config import Config, validate_runtime_security


ROOT = Path(__file__).resolve().parents[1]


def _production_config(**overrides):
    config = {
        "ENV": "production",
        "SECRET_KEY": "s" * 32,
        "DEBUG": False,
        "SIGEM_LIVE_ENABLED": False,
        "WHATSAPP_INBOUND_DURABILITY_MODE": "legacy",
        "DOMAIN_EFFECT_OUTBOX_MODE": "legacy",
        "CHATBOC_PROCESS_ROLE": "web",
        "ENABLE_SURVEY_ELIGIBILITY_GRANTS_V1": False,
        "SURVEY_ELIGIBILITY_GRANT_TENANT_IDS": "",
        "SURVEY_ELIGIBILITY_SECRET_V1": "",
    }
    config.update(overrides)
    return config


def test_survey_eligibility_gate_defaults_fail_closed():
    assert Config.ENABLE_SURVEY_ELIGIBILITY_GRANTS_V1 is False
    assert isinstance(Config.SURVEY_ELIGIBILITY_GRANT_TENANT_IDS, str)


def test_production_rejects_enabled_gate_without_secret_or_canary_tenants():
    errors = validate_runtime_security(
        _production_config(ENABLE_SURVEY_ELIGIBILITY_GRANTS_V1=True)
    )

    assert any("SURVEY_ELIGIBILITY_SECRET_V1" in error for error in errors)
    assert any("tenants canarios explicitos" in error for error in errors)


def test_production_accepts_strong_secret_and_explicit_positive_tenants():
    errors = validate_runtime_security(
        _production_config(
            ENABLE_SURVEY_ELIGIBILITY_GRANTS_V1=True,
            SURVEY_ELIGIBILITY_GRANT_TENANT_IDS="7,23",
            SURVEY_ELIGIBILITY_SECRET_V1="e" * 32,
        )
    )

    assert not any("SURVEY_ELIGIBILITY" in error for error in errors)


def test_production_rejects_weak_secret_and_malformed_or_broad_allowlists():
    for tenant_scope in ("*", "7,other", "0", "01", "-2"):
        errors = validate_runtime_security(
            _production_config(
                ENABLE_SURVEY_ELIGIBILITY_GRANTS_V1=True,
                SURVEY_ELIGIBILITY_GRANT_TENANT_IDS=tenant_scope,
                SURVEY_ELIGIBILITY_SECRET_V1="short",
            )
        )

        assert any("SURVEY_ELIGIBILITY_SECRET_V1" in error for error in errors)
        assert any(
            "SURVEY_ELIGIBILITY_GRANT_TENANT_IDS" in error
            or "tenants canarios explicitos" in error
            for error in errors
        )


def test_render_declares_disabled_canary_and_unsynced_secret():
    manifest = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    services = {service["name"]: service for service in manifest["services"]}
    web_env = {item["key"]: item for item in services["chatboc-backend"]["envVars"]}

    assert web_env["ENABLE_SURVEY_ELIGIBILITY_GRANTS_V1"]["value"] == "false"
    assert web_env["SURVEY_ELIGIBILITY_GRANT_TENANT_IDS"]["value"] == ""
    assert web_env["SURVEY_ELIGIBILITY_SECRET_V1"] == {
        "key": "SURVEY_ELIGIBILITY_SECRET_V1",
        "sync": False,
    }
