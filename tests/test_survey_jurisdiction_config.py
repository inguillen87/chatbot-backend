from config import validate_runtime_security


def _production_config(**overrides):
    config = {
        "ENV": "production",
        "DEBUG": False,
        "SECRET_KEY": "s" * 48,
        "SURVEY_JURISDICTION_GATE_MODE": "observe",
        "SURVEY_JURISDICTION_GATE_TENANT_IDS": "",
    }
    config.update(overrides)
    return config


def _jurisdiction_errors(config):
    return [
        error
        for error in validate_runtime_security(config)
        if "JURISDICTION" in error.upper() or "jurisdiccion" in error.lower()
    ]


def test_observe_is_the_reversible_default_without_canaries():
    assert _jurisdiction_errors(_production_config()) == []


def test_enforcement_requires_strict_positive_canary_ids():
    missing = _jurisdiction_errors(
        _production_config(SURVEY_JURISDICTION_GATE_MODE="enforce_publish")
    )
    malformed = _jurisdiction_errors(
        _production_config(
            SURVEY_JURISDICTION_GATE_MODE="enforce_visibility",
            SURVEY_JURISDICTION_GATE_TENANT_IDS="7,01",
        )
    )
    valid = _jurisdiction_errors(
        _production_config(
            SURVEY_JURISDICTION_GATE_MODE="enforce_visibility",
            SURVEY_JURISDICTION_GATE_TENANT_IDS="7,23",
        )
    )

    assert any("canarios" in error for error in missing)
    assert any("TENANT_IDS" in error for error in malformed)
    assert valid == []


def test_unknown_enforcement_mode_is_rejected():
    errors = _jurisdiction_errors(
        _production_config(SURVEY_JURISDICTION_GATE_MODE="best_effort")
    )
    assert any("GATE_MODE" in error for error in errors)
