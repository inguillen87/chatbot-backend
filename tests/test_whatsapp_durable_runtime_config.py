from config import validate_runtime_security


def _production_config(**overrides):
    config = {
        "ENV": "production",
        "DEBUG": False,
        "SECRET_KEY": "s" * 32,
        "WHATSAPP_INBOUND_DURABILITY_MODE": "legacy",
        "WHATSAPP_INBOUND_QUEUE_TENANT_IDS": "7",
        "WHATSAPP_INBOUND_HASH_SECRET": "",
        "CHANNEL_SESSION_IDENTITY_MODE": "legacy",
        "CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1": "",
        "CHANNEL_SESSION_IDENTITY_VERSION_V1": "v1",
        "WHATSAPP_INBOUND_MAX_PAYLOAD_BYTES": 65536,
        "WHATSAPP_INBOUND_LEASE_SECONDS": 180,
        "WHATSAPP_INBOUND_MAX_ATTEMPTS": 8,
        "WHATSAPP_INBOUND_WORKER_BATCH_SIZE": 8,
        "WHATSAPP_INBOUND_WORKER_POLL_SECONDS": 0.5,
        "WHATSAPP_DURABLE_WORKER_STANDBY_ENABLED": False,
        "WHATSAPP_INBOUND_PAYLOAD_SCRUB_TENANT_IDS": "",
        "WHATSAPP_INBOUND_PAYLOAD_SCRUB_ENABLED": False,
        "WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD": True,
        "WHATSAPP_INBOUND_DEAD_PAYLOAD_RETENTION_HOURS": 72,
        "WHATSAPP_INBOUND_PAYLOAD_SCRUB_BATCH_SIZE": 200,
    }
    config.update(overrides)
    return config


def test_queue_mode_requires_dedicated_32_byte_stream_secret():
    errors = validate_runtime_security(
        _production_config(
            WHATSAPP_INBOUND_DURABILITY_MODE="queue",
            WHATSAPP_INBOUND_HASH_SECRET="too-short",
            CHANNEL_SESSION_IDENTITY_MODE="enforce",
            CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1="i" * 32,
        )
    )

    assert any("WHATSAPP_INBOUND_HASH_SECRET" in error for error in errors)


def test_queue_mode_accepts_strong_stream_secret():
    errors = validate_runtime_security(
        _production_config(
            WHATSAPP_INBOUND_DURABILITY_MODE="queue",
            WHATSAPP_INBOUND_HASH_SECRET="q" * 32,
            CHANNEL_SESSION_IDENTITY_MODE="enforce",
            CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1="i" * 32,
        )
    )

    assert errors == []


def test_unknown_durability_mode_fails_closed_in_production():
    errors = validate_runtime_security(
        _production_config(WHATSAPP_INBOUND_DURABILITY_MODE="sometimes")
    )

    assert any("WHATSAPP_INBOUND_DURABILITY_MODE" in error for error in errors)


def test_queue_mode_requires_explicit_tenant_canaries():
    errors = validate_runtime_security(
        _production_config(
            WHATSAPP_INBOUND_DURABILITY_MODE="queue",
            WHATSAPP_INBOUND_QUEUE_TENANT_IDS="",
            WHATSAPP_INBOUND_HASH_SECRET="q" * 32,
            CHANNEL_SESSION_IDENTITY_MODE="enforce",
            CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1="i" * 32,
        )
    )

    assert any("WHATSAPP_INBOUND_QUEUE_TENANT_IDS" in error for error in errors)


def test_queue_mode_rejects_invalid_tenant_canary():
    errors = validate_runtime_security(
        _production_config(
            WHATSAPP_INBOUND_DURABILITY_MODE="queue",
            WHATSAPP_INBOUND_QUEUE_TENANT_IDS="7,other",
            WHATSAPP_INBOUND_HASH_SECRET="q" * 32,
            CHANNEL_SESSION_IDENTITY_MODE="enforce",
            CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1="i" * 32,
        )
    )

    assert any("WHATSAPP_INBOUND_QUEUE_TENANT_IDS" in error for error in errors)


def test_queue_mode_rejects_unsafe_worker_bounds():
    errors = validate_runtime_security(
        _production_config(
            WHATSAPP_INBOUND_DURABILITY_MODE="queue",
            WHATSAPP_INBOUND_HASH_SECRET="q" * 32,
            CHANNEL_SESSION_IDENTITY_MODE="enforce",
            CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1="i" * 32,
            WHATSAPP_INBOUND_LEASE_SECONDS=5,
            WHATSAPP_INBOUND_WORKER_BATCH_SIZE=0,
        )
    )

    assert any("WHATSAPP_INBOUND_LEASE_SECONDS" in error for error in errors)
    assert any("WHATSAPP_INBOUND_WORKER_BATCH_SIZE" in error for error in errors)


def test_queue_mode_requires_enforced_canonical_session_identity():
    errors = validate_runtime_security(
        _production_config(
            WHATSAPP_INBOUND_DURABILITY_MODE="queue",
            WHATSAPP_INBOUND_HASH_SECRET="q" * 32,
            CHANNEL_SESSION_IDENTITY_MODE="shadow",
        )
    )

    assert any("CHANNEL_SESSION_IDENTITY_MODE=enforce" in error for error in errors)


def test_enforce_mode_requires_dedicated_identity_secret():
    errors = validate_runtime_security(
        _production_config(CHANNEL_SESSION_IDENTITY_MODE="enforce")
    )

    assert any("CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1" in error for error in errors)


def test_notification_transport_requires_explicit_canary_tenants():
    errors = validate_runtime_security(
        _production_config(
            WHATSAPP_NOTIFICATION_TRANSPORT_ENABLED=True,
            WHATSAPP_NOTIFICATION_TRANSPORT_TENANT_IDS="",
            NOTIFICATION_DISPATCH_LEASE_SECONDS=180,
        )
    )

    assert any(
        "WHATSAPP_NOTIFICATION_TRANSPORT_TENANT_IDS" in error for error in errors
    )


def test_notification_transport_accepts_bounded_canary_configuration():
    errors = validate_runtime_security(
        _production_config(
            WHATSAPP_NOTIFICATION_TRANSPORT_ENABLED=True,
            WHATSAPP_NOTIFICATION_TRANSPORT_TENANT_IDS="7,11",
            NOTIFICATION_DISPATCH_LEASE_SECONDS=180,
        )
    )

    assert errors == []


def test_durable_worker_role_refuses_legacy_mode():
    errors = validate_runtime_security(
        _production_config(
            CHATBOC_PROCESS_ROLE="whatsapp-durable-worker",
            WHATSAPP_INBOUND_DURABILITY_MODE="legacy",
        )
    )

    assert any(
        "whatsapp-durable-worker" in error
        and "WHATSAPP_INBOUND_DURABILITY_MODE=queue" in error
        for error in errors
    )


def test_durable_worker_role_accepts_complete_queue_contract():
    errors = validate_runtime_security(
        _production_config(
            CHATBOC_PROCESS_ROLE="whatsapp-durable-worker",
            WHATSAPP_INBOUND_DURABILITY_MODE="queue",
            WHATSAPP_INBOUND_HASH_SECRET="q" * 32,
            CHANNEL_SESSION_IDENTITY_MODE="enforce",
            CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1="i" * 32,
        )
    )

    assert errors == []


def test_durable_worker_role_accepts_explicit_zero_io_legacy_standby():
    errors = validate_runtime_security(
        _production_config(
            CHATBOC_PROCESS_ROLE="whatsapp-durable-worker",
            WHATSAPP_INBOUND_DURABILITY_MODE="legacy",
            WHATSAPP_DURABLE_WORKER_STANDBY_ENABLED=True,
        )
    )

    assert errors == []


def test_payload_scrub_requires_enabled_hold_release_and_historical_scope():
    missing_scope = validate_runtime_security(
        _production_config(
            WHATSAPP_INBOUND_PAYLOAD_SCRUB_ENABLED=True,
            WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD=False,
        )
    )
    held = validate_runtime_security(
        _production_config(
            WHATSAPP_INBOUND_PAYLOAD_SCRUB_ENABLED=True,
            WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD=True,
        )
    )
    active = validate_runtime_security(
        _production_config(
            CHATBOC_PROCESS_ROLE="whatsapp-payload-retention-cron",
            WHATSAPP_INBOUND_PAYLOAD_SCRUB_ENABLED=True,
            WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD=False,
            WHATSAPP_INBOUND_PAYLOAD_SCRUB_TENANT_IDS="7,11",
        )
    )

    assert any("SCRUB_TENANT_IDS" in error for error in missing_scope)
    assert held == []
    assert active == []


def test_payload_scrub_rejects_invalid_scope_and_bounds():
    errors = validate_runtime_security(
        _production_config(
            WHATSAPP_INBOUND_PAYLOAD_SCRUB_ENABLED=True,
            WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD=False,
            WHATSAPP_INBOUND_PAYLOAD_SCRUB_TENANT_IDS="7,invalid",
            WHATSAPP_INBOUND_DEAD_PAYLOAD_RETENTION_HOURS=721,
            WHATSAPP_INBOUND_PAYLOAD_SCRUB_BATCH_SIZE=0,
        )
    )

    assert any("SCRUB_TENANT_IDS" in error for error in errors)
    assert any("RETENTION_HOURS" in error for error in errors)
    assert any("SCRUB_BATCH_SIZE" in error for error in errors)
