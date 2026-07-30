from __future__ import annotations

from pathlib import Path

import yaml

from config import validate_runtime_security


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
        "SURVEY_IDENTITY_HMAC_SECRET_V1": "",
    }
    config.update(overrides)
    return config


def test_production_rejects_configured_short_survey_identity_secret():
    errors = validate_runtime_security(
        _production_config(SURVEY_IDENTITY_HMAC_SECRET_V1="short")
    )

    assert any("SURVEY_IDENTITY_HMAC_SECRET_V1" in error for error in errors)


def test_production_accepts_strong_survey_identity_secret():
    errors = validate_runtime_security(
        _production_config(SURVEY_IDENTITY_HMAC_SECRET_V1="p" * 32)
    )

    assert not any("SURVEY_IDENTITY_HMAC_SECRET_V1" in error for error in errors)


def test_render_declares_secret_and_daily_retention_cron():
    manifest = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    services = {service["name"]: service for service in manifest["services"]}
    web = services["chatboc-backend"]
    retention = services["chatboc-survey-retention"]

    web_env = {item["key"]: item for item in web["envVars"]}
    assert web_env["SURVEY_IDENTITY_HMAC_SECRET_V1"] == {
        "key": "SURVEY_IDENTITY_HMAC_SECRET_V1",
        "sync": False,
    }
    assert retention["type"] == "cron"
    assert retention["schedule"] == "17 3 * * *"
    assert retention["startCommand"].startswith("python -m services.survey_privacy")
    retention_env = {item["key"]: item for item in retention["envVars"]}
    assert retention_env["CHATBOC_PROCESS_ROLE"]["value"] == "survey-retention-cron"
    assert retention_env["DATABASE_URL"]["fromService"]["name"] == "chatboc-backend"
