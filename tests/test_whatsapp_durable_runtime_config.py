from config import validate_runtime_security


def _production_config(**overrides):
    config = {
        "ENV": "production",
        "DEBUG": False,
        "SECRET_KEY": "s" * 32,
        "WHATSAPP_INBOUND_DURABILITY_MODE": "legacy",
        "WHATSAPP_INBOUND_HASH_SECRET": "",
        "CHANNEL_SESSION_IDENTITY_MODE": "legacy",
        "CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1": "",
        "CHANNEL_SESSION_IDENTITY_VERSION_V1": "v1",
        "WHATSAPP_INBOUND_MAX_PAYLOAD_BYTES": 65536,
        "WHATSAPP_INBOUND_LEASE_SECONDS": 180,
        "WHATSAPP_INBOUND_MAX_ATTEMPTS": 8,
        "WHATSAPP_INBOUND_WORKER_BATCH_SIZE": 8,
        "WHATSAPP_INBOUND_WORKER_POLL_SECONDS": 0.5,
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
