from unittest.mock import patch

import pytest
from flask import Flask

from services import location_service, openai_text_service, qdrant_service, qdrant_utils


@pytest.mark.parametrize("testing_source", ["environment", "app"])
def test_openai_client_factory_blocks_network_during_testing(
    monkeypatch,
    testing_source,
):
    monkeypatch.setenv("TESTING", "1" if testing_source == "environment" else "0")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-never-send")
    monkeypatch.delenv("OPENAI_ALLOW_NETWORK_IN_TESTS", raising=False)
    monkeypatch.setattr(openai_text_service, "_OPENAI_CLIENT", None)
    monkeypatch.setattr(openai_text_service, "_OPENAI_CLIENT_KEY_DIGEST", None)
    app = Flask(__name__)
    app.config["TESTING"] = testing_source == "app"

    with (
        app.app_context(),
        patch.object(openai_text_service, "OpenAI") as constructor,
        pytest.raises(RuntimeError, match="disabled while TESTING"),
    ):
        openai_text_service._get_openai_client()

    constructor.assert_not_called()


@pytest.mark.parametrize("testing_source", ["environment", "app"])
def test_google_maps_client_factory_blocks_network_during_testing(
    monkeypatch,
    testing_source,
):
    monkeypatch.setenv("TESTING", "1" if testing_source == "environment" else "0")
    monkeypatch.delenv("GOOGLE_MAPS_ALLOW_NETWORK_IN_TESTS", raising=False)
    monkeypatch.setattr(location_service, "GOOGLE_MAPS_API_KEY", "test-key")
    app = Flask(__name__)
    app.config["TESTING"] = testing_source == "app"

    with app.app_context(), patch.object(location_service.googlemaps, "Client") as constructor:
        assert location_service.get_gmaps_client() is None

    constructor.assert_not_called()


@pytest.mark.parametrize("testing_source", ["environment", "app"])
@pytest.mark.parametrize("module", [qdrant_service, qdrant_utils])
def test_qdrant_client_factories_block_network_during_testing(
    monkeypatch,
    testing_source,
    module,
):
    monkeypatch.setenv("TESTING", "1" if testing_source == "environment" else "0")
    monkeypatch.setenv("QDRANT_URL", "https://qdrant.invalid")
    monkeypatch.setenv("QDRANT_API_KEY", "test-key-never-send")
    monkeypatch.delenv("QDRANT_ALLOW_NETWORK_IN_TESTS", raising=False)
    if module is qdrant_utils:
        monkeypatch.setattr(module, "qdrant_client_instance", None)
    app = Flask(__name__)
    app.config["TESTING"] = testing_source == "app"

    with app.app_context(), patch.object(module, "QdrantClient") as constructor:
        assert module.get_qdrant_client() is None

    constructor.assert_not_called()


@pytest.mark.parametrize("module", [qdrant_service, qdrant_utils])
def test_qdrant_test_opt_in_allows_only_the_mocked_factory(monkeypatch, module):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("QDRANT_ALLOW_NETWORK_IN_TESTS", "1")
    monkeypatch.setenv("QDRANT_URL", "https://qdrant.invalid")
    monkeypatch.setenv("QDRANT_API_KEY", "test-key-never-send")
    if module is qdrant_utils:
        monkeypatch.setattr(module, "qdrant_client_instance", None)

    fake_client = object()
    with patch.object(module, "QdrantClient", return_value=fake_client) as constructor:
        assert module.get_qdrant_client() is fake_client

    constructor.assert_called_once()
