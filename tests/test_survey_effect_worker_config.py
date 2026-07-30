from config import validate_runtime_security


def _production_config(**overrides):
    config = {
        "ENV": "production",
        "DEBUG": False,
        "SECRET_KEY": "s" * 32,
        "WHATSAPP_INBOUND_DURABILITY_MODE": "legacy",
        "DOMAIN_EFFECT_OUTBOX_MODE": "legacy",
        "SURVEY_RESPONSE_EFFECT_LEASE_SECONDS": 120,
        "SURVEY_RESPONSE_EFFECT_WORKER_BATCH_SIZE": 50,
        "SURVEY_RESPONSE_EFFECT_WORKER_MAX_TENANTS_PER_CYCLE": 50,
        "SURVEY_RESPONSE_EFFECT_WORKER_POLL_SECONDS": 0.5,
    }
    config.update(overrides)
    return config


def test_survey_effect_worker_defaults_are_valid_in_production():
    assert validate_runtime_security(_production_config()) == []


def test_survey_effect_worker_rejects_invalid_operational_bounds():
    errors = validate_runtime_security(
        _production_config(
            SURVEY_RESPONSE_EFFECT_LEASE_SECONDS=5,
            SURVEY_RESPONSE_EFFECT_WORKER_BATCH_SIZE=0,
            SURVEY_RESPONSE_EFFECT_WORKER_MAX_TENANTS_PER_CYCLE=501,
            SURVEY_RESPONSE_EFFECT_WORKER_POLL_SECONDS="invalid",
        )
    )

    assert any("SURVEY_RESPONSE_EFFECT_LEASE_SECONDS" in error for error in errors)
    assert any("SURVEY_RESPONSE_EFFECT_WORKER_BATCH_SIZE" in error for error in errors)
    assert any(
        "SURVEY_RESPONSE_EFFECT_WORKER_MAX_TENANTS_PER_CYCLE" in error
        for error in errors
    )
    assert any("SURVEY_RESPONSE_EFFECT_WORKER_POLL_SECONDS" in error for error in errors)


def test_production_survey_worker_requires_explicit_redis_socket_queue():
    missing = validate_runtime_security(
        _production_config(CHATBOC_PROCESS_ROLE="survey-effect-worker")
    )
    assert any("SOCKETIO_MESSAGE_QUEUE_URL" in error for error in missing)

    valid = validate_runtime_security(
        _production_config(
            CHATBOC_PROCESS_ROLE="survey-effect-worker",
            SOCKETIO_MESSAGE_QUEUE_URL="rediss://redis.example.test:6380/7",
            SOCKETIO_MESSAGE_QUEUE_CHANNEL="chatboc-realtime-v1",
            SOCKETIO_MESSAGE_QUEUE_HEALTHCHECK_TIMEOUT_SECONDS=2,
        )
    )
    assert valid == []


def test_socket_queue_contract_rejects_unsafe_url_channel_and_timeout():
    errors = validate_runtime_security(
        _production_config(
            CHATBOC_PROCESS_ROLE="survey-effect-worker",
            SOCKETIO_MESSAGE_QUEUE_URL="memory://not-shared",
            SOCKETIO_MESSAGE_QUEUE_CHANNEL="unsafe channel",
            SOCKETIO_MESSAGE_QUEUE_HEALTHCHECK_TIMEOUT_SECONDS=30,
        )
    )

    assert any("redis://" in error for error in errors)
    assert any("SOCKETIO_MESSAGE_QUEUE_CHANNEL" in error for error in errors)
    assert any(
        "SOCKETIO_MESSAGE_QUEUE_HEALTHCHECK_TIMEOUT_SECONDS" in error
        for error in errors
    )
