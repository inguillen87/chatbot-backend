import pytest

from services.domain_effect_gate import (
    DomainEffectOutboxConfigurationError,
    resolve_domain_effect_outbox_canaries,
    resolve_domain_effect_outbox_policy,
)


def _config(**overrides):
    values = {
        "DOMAIN_EFFECT_OUTBOX_MODE": "legacy",
        "DOMAIN_EFFECT_OUTBOX_SECRET": "",
        "DOMAIN_EFFECT_OUTBOX_TENANT_IDS": "",
        "DOMAIN_EFFECT_OUTBOX_MAX_PAYLOAD_BYTES": 4096,
        "DOMAIN_EFFECT_OUTBOX_MAX_ATTEMPTS": 8,
    }
    values.update(overrides)
    return values


def test_legacy_policy_preserves_existing_path_without_a_secret():
    policy = resolve_domain_effect_outbox_policy(_config(), tenant_id=17)

    assert policy.mode == "legacy"
    assert policy.enabled is False
    assert policy.secret is None


def test_queue_policy_enables_only_explicit_canary_tenants():
    config = _config(
        DOMAIN_EFFECT_OUTBOX_MODE="queue",
        DOMAIN_EFFECT_OUTBOX_SECRET="d" * 32,
        DOMAIN_EFFECT_OUTBOX_TENANT_IDS="17,23",
    )

    enabled = resolve_domain_effect_outbox_policy(config, tenant_id=17)
    excluded = resolve_domain_effect_outbox_policy(config, tenant_id=99)

    assert enabled.enabled is True
    assert enabled.secret == "d" * 32
    assert excluded.enabled is False
    assert excluded.secret is None
    assert resolve_domain_effect_outbox_canaries(config) == frozenset({17, 23})


@pytest.mark.parametrize(
    "overrides",
    [
        {"DOMAIN_EFFECT_OUTBOX_MODE": "unknown"},
        {
            "DOMAIN_EFFECT_OUTBOX_MODE": "queue",
            "DOMAIN_EFFECT_OUTBOX_SECRET": "short",
            "DOMAIN_EFFECT_OUTBOX_TENANT_IDS": "17",
        },
        {
            "DOMAIN_EFFECT_OUTBOX_MODE": "queue",
            "DOMAIN_EFFECT_OUTBOX_SECRET": "d" * 32,
            "DOMAIN_EFFECT_OUTBOX_TENANT_IDS": "*",
        },
        {"DOMAIN_EFFECT_OUTBOX_MAX_ATTEMPTS": 0},
        {"DOMAIN_EFFECT_OUTBOX_MAX_PAYLOAD_BYTES": 100_000},
    ],
)
def test_invalid_policy_fails_closed(overrides):
    with pytest.raises(DomainEffectOutboxConfigurationError):
        resolve_domain_effect_outbox_policy(_config(**overrides), tenant_id=17)


@pytest.mark.parametrize("tenant_id", [None, 0, -1, "bad"])
def test_invalid_tenant_identity_fails_closed(tenant_id):
    with pytest.raises(DomainEffectOutboxConfigurationError):
        resolve_domain_effect_outbox_policy(_config(), tenant_id=tenant_id)
