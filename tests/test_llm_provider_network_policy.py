from unittest.mock import Mock, patch

import pytest
from flask import Flask

from services import (
    cohere_bridge,
    gemini_bridge,
    google_maps_service,
    herramientas_municipio,
    llm_orchestrator,
    ollama_bridge,
    openai_bridge,
    openai_maps_service,
)
from services.llm_provider_network_policy import (
    LLMProviderNetworkDisabledError,
    llm_provider_network_allowed,
)


_OPT_IN_NAMES = {
    "openai": "OPENAI_ALLOW_NETWORK_IN_TESTS",
    "gemini": "GEMINI_ALLOW_NETWORK_IN_TESTS",
    "cohere": "COHERE_ALLOW_NETWORK_IN_TESTS",
    "ollama": "OLLAMA_ALLOW_NETWORK_IN_TESTS",
    "geocoding": "GEOCODING_ALLOW_NETWORK_IN_TESTS",
    "twilio": "TWILIO_ALLOW_NETWORK_IN_TESTS",
}


@pytest.fixture(autouse=True)
def _offline_llm_providers(monkeypatch):
    monkeypatch.setenv("TESTING", "1")
    for name in _OPT_IN_NAMES.values():
        monkeypatch.delenv(name, raising=False)
    openai_bridge._reset_openai_client_for_tests()
    monkeypatch.setattr(cohere_bridge, "co", None)
    monkeypatch.setattr(cohere_bridge, "co_v2", None)
    monkeypatch.setattr(cohere_bridge, "_COHERE_CLIENTS_INITIALIZED", False)
    yield
    openai_bridge._reset_openai_client_for_tests()


def test_provider_opt_in_is_scoped_to_one_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_ALLOW_NETWORK_IN_TESTS", "1")

    assert llm_provider_network_allowed("openai") is True
    assert llm_provider_network_allowed("gemini") is False
    assert llm_provider_network_allowed("cohere") is False
    assert llm_provider_network_allowed("ollama") is False
    assert llm_provider_network_allowed("geocoding") is False
    assert llm_provider_network_allowed("twilio") is False


def test_production_behavior_does_not_require_test_opt_ins(monkeypatch):
    monkeypatch.setenv("TESTING", "0")

    for provider in _OPT_IN_NAMES:
        assert llm_provider_network_allowed(provider) is True


def test_flask_testing_config_is_offline_without_environment_flag(monkeypatch):
    monkeypatch.setenv("TESTING", "0")
    app = Flask(__name__)
    app.config["TESTING"] = True

    with app.app_context():
        for provider in _OPT_IN_NAMES:
            assert llm_provider_network_allowed(provider) is False

        app.config["GEMINI_ALLOW_NETWORK_IN_TESTS"] = True
        assert llm_provider_network_allowed("gemini") is True
        assert llm_provider_network_allowed("openai") is False


def test_openai_bridge_blocks_before_sdk_constructor(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-never-send")

    with (
        patch.object(openai_bridge, "OpenAI") as constructor,
        pytest.raises(
            LLMProviderNetworkDisabledError,
            match="openai_test_network_disabled",
        ),
    ):
        openai_bridge._get_openai_client(None)

    constructor.assert_not_called()


def test_gemini_bridge_blocks_before_sdk_modules_or_send(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-never-send")
    module_loader = Mock()

    with (
        patch.object(gemini_bridge, "_get_genai_modules", module_loader),
        pytest.raises(
            LLMProviderNetworkDisabledError,
            match="gemini_test_network_disabled",
        ),
    ):
        gemini_bridge.llamar_gemini(None, "private-message", {}, [], "private-session")

    module_loader.assert_not_called()


def test_cohere_bridge_blocks_before_client_constructors():
    with (
        patch.object(cohere_bridge.cohere, "Client") as v1_constructor,
        patch.object(cohere_bridge.cohere, "ClientV2") as v2_constructor,
        pytest.raises(
            LLMProviderNetworkDisabledError,
            match="cohere_test_network_disabled",
        ),
    ):
        cohere_bridge.llamar_cohere(None, "private-message", {}, [], "private-session")

    v1_constructor.assert_not_called()
    v2_constructor.assert_not_called()


def test_ollama_bridge_blocks_before_http_and_sdk_clients(monkeypatch):
    monkeypatch.setenv("OLLAMA_ENABLED", "1")

    with (
        patch.object(ollama_bridge.httpx, "Client") as http_constructor,
        patch.object(ollama_bridge, "OpenAI") as sdk_constructor,
        pytest.raises(
            LLMProviderNetworkDisabledError,
            match="ollama_test_network_disabled",
        ),
    ):
        ollama_bridge.llamar_ollama(None, "private-message", {}, [], "private-session")

    http_constructor.assert_not_called()
    sdk_constructor.assert_not_called()


def test_openai_maps_blocks_before_http_and_sdk_clients():
    with (
        patch.object(openai_maps_service.httpx, "Client") as http_constructor,
        patch.object(openai_maps_service.openai, "OpenAI") as sdk_constructor,
    ):
        result = openai_maps_service._solicitar_json_a_openai(
            api_key="test-key-never-send",
            prompt="private-address",
            schema={"name": "test", "schema": {"type": "object"}},
            system_message="private-system-message",
        )

    assert result is None
    http_constructor.assert_not_called()
    sdk_constructor.assert_not_called()


def test_geopy_path_blocks_before_provider_loader():
    with patch.object(
        herramientas_municipio.google_maps_service,
        "_get_geolocators",
    ) as loader:
        result = herramientas_municipio._reverse_geocode_with_geopy(-32.89, -68.83)

    assert result is None
    loader.assert_not_called()


def test_geocoding_factory_blocks_before_provider_constructors(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key-never-send")
    monkeypatch.setenv("MAPTILER_API_KEY", "test-key-never-send")

    with (
        patch.object(google_maps_service, "GoogleV3") as google_constructor,
        patch.object(google_maps_service, "MapTiler") as maptiler_constructor,
        patch.object(google_maps_service, "Nominatim") as nominatim_constructor,
    ):
        assert google_maps_service._get_geolocators() == []

    google_constructor.assert_not_called()
    maptiler_constructor.assert_not_called()
    nominatim_constructor.assert_not_called()


def test_orchestrator_skips_every_blocked_fallback_without_invoking_it(
    monkeypatch,
    caplog,
):
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "openai,gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-never-send")
    openai_call = Mock()
    gemini_call = Mock()

    with (
        patch.object(llm_orchestrator, "llamar_openai", openai_call),
        patch.object(gemini_bridge, "llamar_gemini", gemini_call),
    ):
        response, context = llm_orchestrator.llamar_llm_con_fallback(
            None,
            "private-message-32877851",
            {},
            [],
            "private-session-32877851",
        )

    assert response["accion_backend"] == "error_fatal_llm"
    assert context == {}
    openai_call.assert_not_called()
    gemini_call.assert_not_called()
    assert "private-message-32877851" not in caplog.text
    assert "private-session-32877851" not in caplog.text
