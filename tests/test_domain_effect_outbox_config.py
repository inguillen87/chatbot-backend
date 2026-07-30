from config import validate_runtime_security


def _production_config(**overrides):
    config = {
        "ENV": "production",
        "DEBUG": False,
        "SECRET_KEY": "s" * 32,
        "WHATSAPP_INBOUND_DURABILITY_MODE": "legacy",
        "DOMAIN_EFFECT_OUTBOX_MODE": "legacy",
        "DOMAIN_EFFECT_OUTBOX_SECRET": "",
        "DOMAIN_EFFECT_OUTBOX_TENANT_IDS": "",
        "DOMAIN_EFFECT_OUTBOX_MAX_PAYLOAD_BYTES": 4096,
        "DOMAIN_EFFECT_OUTBOX_LEASE_SECONDS": 180,
        "DOMAIN_EFFECT_OUTBOX_MAX_ATTEMPTS": 8,
        "DOMAIN_EFFECT_OUTBOX_WORKER_BATCH_SIZE": 20,
        "DOMAIN_EFFECT_OUTBOX_WORKER_POLL_SECONDS": 0.5,
    }
    config.update(overrides)
    return config


def test_legacy_mode_does_not_require_outbox_credentials():
    assert validate_runtime_security(_production_config()) == []


def test_placeholder_sigem_transport_cannot_be_marked_live_in_production():
    errors = validate_runtime_security(
        _production_config(SIGEM_LIVE_ENABLED=True)
    )

    assert any("SIGEM_LIVE_ENABLED" in error for error in errors)


def test_queue_mode_requires_dedicated_secret_and_explicit_canary():
    errors = validate_runtime_security(
        _production_config(DOMAIN_EFFECT_OUTBOX_MODE="queue")
    )

    assert any("DOMAIN_EFFECT_OUTBOX_SECRET" in error for error in errors)
    assert any("DOMAIN_EFFECT_OUTBOX_TENANT_IDS" in error for error in errors)


def test_queue_mode_accepts_strong_secret_and_positive_tenant_ids():
    errors = validate_runtime_security(
        _production_config(
            DOMAIN_EFFECT_OUTBOX_MODE="queue",
            DOMAIN_EFFECT_OUTBOX_SECRET="d" * 32,
            DOMAIN_EFFECT_OUTBOX_TENANT_IDS="17, 23",
        )
    )

    assert errors == []


def test_queue_mode_rejects_global_or_malformed_tenant_scope():
    for tenant_scope in ("*", "all", "0", "-1", "17,bad"):
        errors = validate_runtime_security(
            _production_config(
                DOMAIN_EFFECT_OUTBOX_MODE="queue",
                DOMAIN_EFFECT_OUTBOX_SECRET="d" * 32,
                DOMAIN_EFFECT_OUTBOX_TENANT_IDS=tenant_scope,
            )
        )

        assert any("DOMAIN_EFFECT_OUTBOX_TENANT_IDS" in error for error in errors)


def test_queue_mode_rejects_invalid_mode_and_worker_bounds():
    invalid_mode_errors = validate_runtime_security(
        _production_config(DOMAIN_EFFECT_OUTBOX_MODE="sometimes")
    )
    assert any("DOMAIN_EFFECT_OUTBOX_MODE" in error for error in invalid_mode_errors)

    errors = validate_runtime_security(
        _production_config(
            DOMAIN_EFFECT_OUTBOX_MODE="queue",
            DOMAIN_EFFECT_OUTBOX_SECRET="d" * 32,
            DOMAIN_EFFECT_OUTBOX_TENANT_IDS="17",
            DOMAIN_EFFECT_OUTBOX_MAX_PAYLOAD_BYTES=100_000,
            DOMAIN_EFFECT_OUTBOX_LEASE_SECONDS=5,
            DOMAIN_EFFECT_OUTBOX_WORKER_BATCH_SIZE=0,
        )
    )

    assert any("DOMAIN_EFFECT_OUTBOX_MAX_PAYLOAD_BYTES" in error for error in errors)
    assert any("DOMAIN_EFFECT_OUTBOX_LEASE_SECONDS" in error for error in errors)
    assert any("DOMAIN_EFFECT_OUTBOX_WORKER_BATCH_SIZE" in error for error in errors)
