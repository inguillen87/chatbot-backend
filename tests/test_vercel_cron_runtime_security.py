import pytest

from config import (
    VERCEL_CRON_SECRET_SECURITY_ERROR,
    validate_runtime_security,
)


CRON_FLAGS = (
    "VERCEL_OUTBOX_CRON_ENABLED",
    "VERCEL_WHATSAPP_PAYLOAD_RETENTION_CRON_ENABLED",
    "VERCEL_SURVEY_PRIVACY_RETENTION_CRON_ENABLED",
    "VERCEL_WEEKLY_ANALYTICS_CRON_ENABLED",
)


def _production_config(**overrides):
    config = {
        "ENV": "production",
        "DEBUG": False,
        "SECRET_KEY": "s" * 32,
        "WHATSAPP_INBOUND_DURABILITY_MODE": "legacy",
        "DOMAIN_EFFECT_OUTBOX_MODE": "legacy",
    }
    config.update(overrides)
    return config


@pytest.mark.parametrize("flag", CRON_FLAGS)
@pytest.mark.parametrize("secret", [None, b"x" * 32, "x" * 31])
def test_enabled_production_vercel_cron_requires_32_byte_text_secret(flag, secret):
    errors = validate_runtime_security(
        _production_config(**{flag: True, "CRON_SECRET": secret})
    )

    assert VERCEL_CRON_SECRET_SECURITY_ERROR in errors


@pytest.mark.parametrize("flag", CRON_FLAGS)
@pytest.mark.parametrize("secret", ["x" * 32, "ñ" * 16])
def test_enabled_production_vercel_cron_accepts_32_utf8_bytes(flag, secret):
    errors = validate_runtime_security(
        _production_config(**{flag: True, "CRON_SECRET": secret})
    )

    assert VERCEL_CRON_SECRET_SECURITY_ERROR not in errors


def test_multiple_enabled_vercel_crons_emit_one_stable_secret_error():
    errors = validate_runtime_security(
        _production_config(
            CRON_SECRET="short",
            **{flag: True for flag in CRON_FLAGS},
        )
    )

    assert errors.count(VERCEL_CRON_SECRET_SECURITY_ERROR) == 1


def test_disabled_or_nonproduction_vercel_crons_do_not_require_cron_secret():
    disabled_errors = validate_runtime_security(
        _production_config(
            CRON_SECRET="",
            **{flag: False for flag in CRON_FLAGS},
        )
    )
    development_errors = validate_runtime_security(
        {
            "ENV": "development",
            "CRON_SECRET": "",
            **{flag: True for flag in CRON_FLAGS},
        }
    )

    assert VERCEL_CRON_SECRET_SECURITY_ERROR not in disabled_errors
    assert development_errors == []
