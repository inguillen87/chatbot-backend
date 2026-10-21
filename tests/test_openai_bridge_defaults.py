import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from services import openai_bridge


@pytest.fixture(autouse=True)
def _reset_lazy_client_state():
    openai_bridge._reset_openai_client_for_tests()
    yield
    openai_bridge._reset_openai_client_for_tests()


class _FakeUsage:
    input_tokens = 10
    output_tokens = 5
    total_tokens = 15

    def to_dict(self):
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


def _chatboc_payload(*, target=None):
    return {
        "message_body": "ok",
        "accion_backend": "responder_directamente",
        "datos_estructura": {
            "_contract_kind": "generico",
            "target": target,
            "pregunta": None,
            "id_reclamo": None,
            "id_ticket": None,
            "pedido_id": None,
            "fecha_turno": None,
            "especialidad": None,
            "vertical": None,
            "school_intent": None,
            "finance_flow": None,
            "datos_extra_json": None,
        },
        "pedir_info": None,
        "botones": [],
    }


class _FakeResponses:
    def __init__(self, *, payload=None, error=None):
        self.last_kwargs = None
        self.calls = 0
        self.payload = payload or _chatboc_payload()
        self.error = error

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self.error:
            raise self.error
        return SimpleNamespace(
            output_text=json.dumps(self.payload),
            output=[],
            status="completed",
            incomplete_details=None,
            usage=_FakeUsage(),
            model=kwargs["model"],
        )


class _FakeClient:
    def __init__(self, *, payload=None, error=None):
        self.responses = _FakeResponses(payload=payload, error=error)


def _inject_client(monkeypatch, *, payload=None, error=None):
    fake_client = _FakeClient(payload=payload, error=error)
    monkeypatch.setattr(openai_bridge, "client", fake_client)
    monkeypatch.setattr(openai_bridge, "_CLIENT_IS_MANAGED", False)
    return fake_client


def test_chatboc_schema_is_strict_at_every_object_boundary():
    def _assert_strict(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node.get("required", [])) == set(node.get("properties", {}))
            for value in node.values():
                _assert_strict(value)
        elif isinstance(node, list):
            for value in node:
                _assert_strict(value)

    _assert_strict(openai_bridge.CHATBOC_RESPONSE_SCHEMA)


def test_chatboc_schema_exposes_agent_metadata_requested_by_the_prompt():
    variants = openai_bridge.CHATBOC_RESPONSE_SCHEMA["properties"][
        "datos_estructura"
    ]["anyOf"]
    expected = {
        "channel",
        "intent",
        "confidence",
        "missing_fields",
        "template_intent",
        "handoff_reason",
        "priority",
        "summary",
    }

    for variant in variants:
        assert expected <= set(variant["properties"])


def test_llamar_openai_uses_responses_strict_contract_and_safe_defaults(monkeypatch):
    fake_client = _inject_client(monkeypatch)

    parsed, meta = openai_bridge.llamar_openai(
        app=None,
        mensaje_usuario="hola",
        usuario={"tipo_entidad": "pyme"},
        historial=[],
        chat_session_id="session-1",
    )

    assert parsed["message_body"] == "ok"
    assert parsed["respuesta_usuario"] == "ok"
    assert parsed["accion_backend"] == "responder_directamente"
    assert parsed["pedir_info"] is None
    assert parsed["datos_estructura"]["target"] == "pyme"
    assert parsed["botones"] == []
    assert meta["usage"]["total_tokens"] == 15
    assert meta["model_used"] == "gpt-5.6-sol"

    request = fake_client.responses.last_kwargs
    assert request["model"] == "gpt-5.6-sol"
    assert request["store"] is False
    assert request["reasoning"] == {"effort": "none"}
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
    assert request["text"]["format"]["schema"]["additionalProperties"] is False
    assert "temperature" not in request


def test_llamar_openai_uses_channel_specific_model(monkeypatch):
    fake_client = _inject_client(monkeypatch)
    monkeypatch.setenv("OPENAI_CHAT_MODEL_DEFAULT", "gpt-5.6-sol")
    monkeypatch.setenv("OPENAI_CHAT_MODEL_WHATSAPP", "gpt-5.6-terra")

    _, meta = openai_bridge.llamar_openai(
        app=None,
        mensaje_usuario="hola",
        usuario={"tipo_entidad": "pyme", "channel": "whatsapp"},
        historial=[],
        chat_session_id="session-2",
    )

    assert fake_client.responses.last_kwargs["model"] == "gpt-5.6-terra"
    assert meta["model_used"] == "gpt-5.6-terra"


def test_llamar_openai_uses_high_complexity_model(monkeypatch):
    fake_client = _inject_client(monkeypatch)
    monkeypatch.setenv("OPENAI_CHAT_MODEL_DEFAULT", "gpt-5.6-sol")
    monkeypatch.setenv("OPENAI_CHAT_MODEL_WIDGET", "gpt-5.6-terra")
    monkeypatch.setenv("OPENAI_CHAT_MODEL_HIGH_COMPLEXITY", "gpt-5.6-sol")
    monkeypatch.setenv("OPENAI_CHAT_COMPLEXITY_MIN_CHARS", "20")
    monkeypatch.setenv("OPENAI_CHAT_COMPLEXITY_MIN_TURNS", "10")

    _, meta = openai_bridge.llamar_openai(
        app=None,
        mensaje_usuario=json.dumps(
            {
                "texto": "Necesito una respuesta bastante extensa para un caso complejo",
                "channel": "widget",
            }
        ),
        usuario={"tipo_entidad": "pyme"},
        historial=[],
        chat_session_id="session-3",
    )

    assert fake_client.responses.last_kwargs["model"] == "gpt-5.6-sol"
    assert meta["model_used"] == "gpt-5.6-sol"


def test_llamar_openai_explicit_model_has_priority(monkeypatch):
    fake_client = _inject_client(monkeypatch)
    monkeypatch.setenv("OPENAI_CHAT_MODEL_DEFAULT", "gpt-5.6-sol")
    monkeypatch.setenv("OPENAI_CHAT_MODEL_WHATSAPP", "gpt-5.6-terra")

    _, meta = openai_bridge.llamar_openai(
        app=None,
        mensaje_usuario="hola",
        usuario={"tipo_entidad": "pyme", "channel": "whatsapp"},
        historial=[],
        chat_session_id="session-4",
        model="gpt-4.1-mini",
    )

    assert fake_client.responses.last_kwargs["model"] == "gpt-4.1-mini"
    assert meta["model_used"] == "gpt-4.1-mini"
    assert "reasoning" not in fake_client.responses.last_kwargs


def test_lazy_client_prefers_app_config_and_disables_sdk_retries(monkeypatch):
    openai_bridge._reset_openai_client_for_tests()
    imported_client_proxy = openai_bridge.client
    captured = {}
    sdk_client = _FakeClient()
    monkeypatch.setenv("OPENAI_ALLOW_NETWORK_IN_TESTS", "1")

    def _fake_openai(**kwargs):
        captured.update(kwargs)
        return sdk_client

    monkeypatch.setattr(openai_bridge, "OpenAI", _fake_openai)
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key-not-used")
    app = SimpleNamespace(
        config={
            "OPENAI_API_KEY": "app-config-key",
            "OPENAI_TIMEOUT_SECONDS": 17,
        }
    )

    assert openai_bridge._get_openai_client(app) is sdk_client
    assert captured["api_key"] == "app-config-key"
    assert captured["max_retries"] == 0
    assert captured["timeout"] == 17.0
    assert "base_url" not in captured
    assert "default_headers" not in captured
    # Modules that imported ``client`` before configuration retain a live proxy.
    assert imported_client_proxy.responses is sdk_client.responses


def _configure_cloudflare_gateway(monkeypatch, *, gateway_id="default"):
    monkeypatch.setenv("CLOUDFLARE_AI_GATEWAY_ENABLED", "true")
    monkeypatch.setenv(
        "CLOUDFLARE_AI_GATEWAY_ACCOUNT_ID",
        "a" * 32,
    )
    monkeypatch.setenv(
        "CLOUDFLARE_AI_GATEWAY_API_TOKEN",
        "cloudflare-test-token-never-sent",
    )
    monkeypatch.setenv("CLOUDFLARE_AI_GATEWAY_ID", gateway_id)


def test_cloudflare_gateway_builds_private_zero_retry_responses_client(monkeypatch):
    captured = {}
    sdk_client = _FakeClient()
    monkeypatch.setenv("OPENAI_ALLOW_NETWORK_IN_TESTS", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _configure_cloudflare_gateway(monkeypatch)

    def _fake_openai(**kwargs):
        captured.update(kwargs)
        return sdk_client

    monkeypatch.setattr(openai_bridge, "OpenAI", _fake_openai)

    assert openai_bridge._get_openai_responses_client(None) is sdk_client
    assert captured["api_key"] == "cloudflare-test-token-never-sent"
    assert captured["base_url"] == (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{'a' * 32}/ai/v1"
    )
    assert captured["max_retries"] == 0
    assert captured["default_headers"] == {
        "cf-aig-gateway-id": "default",
        "cf-aig-skip-cache": "true",
        "cf-aig-collect-log-payload": "false",
    }
    assert all(
        "cloudflare-test-token-never-sent" not in value
        for value in captured["default_headers"].values()
    )


def test_cloudflare_gateway_does_not_change_shared_direct_openai_client(monkeypatch):
    captured = {}
    sdk_client = _FakeClient()
    monkeypatch.setenv("OPENAI_ALLOW_NETWORK_IN_TESTS", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "direct-openai-test-key")
    _configure_cloudflare_gateway(monkeypatch)

    def _fake_openai(**kwargs):
        captured.update(kwargs)
        return sdk_client

    monkeypatch.setattr(openai_bridge, "OpenAI", _fake_openai)

    assert openai_bridge._get_openai_client(None) is sdk_client
    assert captured["api_key"] == "direct-openai-test-key"
    assert "base_url" not in captured
    assert "default_headers" not in captured


def test_cloudflare_gateway_prefixes_model_and_preserves_reasoning(monkeypatch):
    fake_client = _inject_client(monkeypatch)
    _configure_cloudflare_gateway(monkeypatch)
    monkeypatch.setenv("OPENAI_CHAT_MODEL_WHATSAPP", "gpt-5.6-terra")

    _, meta = openai_bridge.llamar_openai(
        app=None,
        mensaje_usuario="hola",
        usuario={"tipo_entidad": "municipio", "channel": "whatsapp"},
        historial=[],
        chat_session_id="gateway-session",
    )

    request = fake_client.responses.last_kwargs
    assert request["model"] == "openai/gpt-5.6-sol"
    assert request["reasoning"] == {"effort": "none"}
    assert request["store"] is False
    assert meta["model_used"] == "openai/gpt-5.6-sol"
    assert (
        openai_bridge._model_for_openai_transport("openai/gpt-5.6-sol")
        == "openai/gpt-5.6-sol"
    )


def test_cloudflare_gateway_accepts_header_safe_existing_id(monkeypatch):
    _configure_cloudflare_gateway(monkeypatch, gateway_id="Team_Gateway.v2")

    config = openai_bridge._cloudflare_ai_gateway_config()

    assert config is not None
    assert config.gateway_id == "Team_Gateway.v2"


def test_cloudflare_gateway_configuration_changes_client_fingerprint(monkeypatch):
    constructed_clients = []
    monkeypatch.setenv("OPENAI_ALLOW_NETWORK_IN_TESTS", "1")
    _configure_cloudflare_gateway(monkeypatch, gateway_id="default")

    def _fake_openai(**_kwargs):
        sdk_client = _FakeClient()
        constructed_clients.append(sdk_client)
        return sdk_client

    monkeypatch.setattr(openai_bridge, "OpenAI", _fake_openai)

    first = openai_bridge._get_openai_responses_client(None)
    same = openai_bridge._get_openai_responses_client(None)
    first_fingerprint = openai_bridge._RESPONSES_CLIENT_KEY_DIGEST
    monkeypatch.setenv("CLOUDFLARE_AI_GATEWAY_ID", "secondary-gateway")
    changed = openai_bridge._get_openai_responses_client(None)

    assert first is same
    assert changed is not first
    assert len(constructed_clients) == 2
    assert openai_bridge._RESPONSES_CLIENT_KEY_DIGEST != first_fingerprint


@pytest.mark.parametrize(
    ("env_name", "env_value", "error_code"),
    [
        (
            "CLOUDFLARE_AI_GATEWAY_ACCOUNT_ID",
            "not-an-account",
            "cloudflare_ai_gateway_account_id_invalid",
        ),
        (
            "CLOUDFLARE_AI_GATEWAY_ID",
            "invalid\r\ngateway",
            "cloudflare_ai_gateway_id_invalid",
        ),
        (
            "CLOUDFLARE_AI_GATEWAY_API_TOKEN",
            "short",
            "cloudflare_ai_gateway_api_token_invalid",
        ),
    ],
)
def test_cloudflare_gateway_rejects_invalid_configuration_before_client(
    monkeypatch,
    env_name,
    env_value,
    error_code,
):
    _configure_cloudflare_gateway(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "direct-key-must-not-be-used")
    monkeypatch.setenv(env_name, env_value)

    def _unexpected_openai(**_kwargs):
        pytest.fail("provider client must not be constructed")

    monkeypatch.setattr(openai_bridge, "OpenAI", _unexpected_openai)

    with pytest.raises(openai_bridge.LLMProviderPreRequestError, match=error_code):
        openai_bridge._get_openai_responses_client(None)


def test_cloudflare_gateway_missing_configuration_does_not_fallback_to_openai(
    monkeypatch,
):
    _configure_cloudflare_gateway(monkeypatch)
    monkeypatch.delenv("CLOUDFLARE_AI_GATEWAY_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "direct-key-must-not-be-used")

    with pytest.raises(
        openai_bridge.LLMProviderPreRequestError,
        match="cloudflare_ai_gateway_account_id_missing",
    ):
        openai_bridge._get_openai_responses_client(None)


def test_cloudflare_gateway_invalid_flag_fails_closed(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_AI_GATEWAY_ENABLED", "tru")
    monkeypatch.setenv("OPENAI_API_KEY", "direct-key-must-not-be-used")

    with pytest.raises(
        openai_bridge.LLMProviderPreRequestError,
        match="cloudflare_ai_gateway_enabled_invalid",
    ):
        openai_bridge._get_openai_responses_client(None)


def test_lazy_client_mock_introspection_does_not_initialize_provider(monkeypatch):
    imported_client_proxy = openai_bridge.client

    def _unexpected_client_initialization(_app=None):
        pytest.fail("dunder introspection initialized the OpenAI provider")

    monkeypatch.setattr(
        openai_bridge,
        "_get_openai_client",
        _unexpected_client_initialization,
    )

    assert not hasattr(imported_client_proxy, "__func__")
    with patch.object(openai_bridge, "client") as mocked_client:
        assert mocked_client is not imported_client_proxy


def test_lazy_client_still_delegates_public_provider_attributes(monkeypatch):
    imported_client_proxy = openai_bridge.client
    sdk_client = _FakeClient()
    calls = []

    def _fake_get_client(app=None):
        calls.append(app)
        return sdk_client

    monkeypatch.setattr(openai_bridge, "_get_openai_client", _fake_get_client)

    assert imported_client_proxy.responses is sdk_client.responses
    assert calls == [None]


def test_safety_identifier_is_hmac_and_never_raw_session(monkeypatch):
    fake_client = _inject_client(monkeypatch)
    monkeypatch.setenv("OPENAI_SAFETY_IDENTIFIER_SECRET", "test-secret")

    openai_bridge.llamar_openai(
        app=None,
        mensaje_usuario="hola",
        usuario={"tipo_entidad": "municipio"},
        historial=[],
        chat_session_id="citizen-session-32877851",
    )

    safety_identifier = fake_client.responses.last_kwargs["safety_identifier"]
    assert safety_identifier != "citizen-session-32877851"
    assert len(safety_identifier) == 64


def test_response_failure_is_uncertain_called_once_and_logs_no_payload(monkeypatch, caplog):
    fake_client = _inject_client(
        monkeypatch,
        error=TimeoutError("secret-exception-detail-32877851"),
    )

    with pytest.raises(
        openai_bridge.LLMProviderRequestUncertainError,
        match="openai_request_failed",
    ):
        openai_bridge.llamar_openai(
            app=None,
            mensaje_usuario="dato privado",
            usuario={"tipo_entidad": "municipio"},
            historial=[],
            chat_session_id="session-5",
        )

    assert fake_client.responses.calls == 1
    assert "dato privado" not in caplog.text
    assert "secret-exception-detail-32877851" not in caplog.text


def test_json_extensions_preserve_evolving_action_fields(monkeypatch):
    payload = _chatboc_payload(target="municipio")
    payload["datos_estructura"]["datos_extra_json"] = json.dumps(
        {"campo_nuevo": "valor"}
    )
    fake_client = _inject_client(monkeypatch, payload=payload)

    parsed, _ = openai_bridge.llamar_openai(
        app=None,
        mensaje_usuario="consulta",
        usuario={"tipo_entidad": "municipio"},
        historial=[],
        chat_session_id="session-6",
    )

    assert fake_client.responses.calls == 1
    assert parsed["datos_estructura"]["campo_nuevo"] == "valor"
    assert "datos_extra_json" not in parsed["datos_estructura"]
    assert "_contract_kind" not in parsed["datos_estructura"]
