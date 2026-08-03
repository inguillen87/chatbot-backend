from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services import ollama_bridge


def test_ollama_config_requires_explicit_enable(monkeypatch):
    monkeypatch.delenv("OLLAMA_ENABLED", raising=False)
    monkeypatch.delenv("LLM_OLLAMA_ENABLED", raising=False)

    assert ollama_bridge.is_ollama_llm_configured() is False

    monkeypatch.setenv("OLLAMA_ENABLED", "true")

    assert ollama_bridge.is_ollama_llm_configured() is True


def test_llamar_ollama_parses_json_and_sets_target(monkeypatch):
    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_ALLOW_NETWORK_IN_TESTS", "1")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("OLLAMA_CHAT_MODEL", "glm-test")

    completion = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content='{"respuesta_usuario":"OK","accion_backend":"responder_directamente","datos_estructura":{}}'
                )
            )
        ]
    )
    create_mock = Mock(return_value=completion)

    class FakeOpenAI:
        def __init__(self, *args, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create_mock))

    monkeypatch.setattr(ollama_bridge, "OpenAI", FakeOpenAI)

    response, context = ollama_bridge.llamar_ollama(
        None,
        '{"texto":"hola","instruccion_canal":"WhatsApp"}',
        {"tipo_entidad": "municipio"},
        [{"role": "model", "parts": [{"text": "Hola"}]}],
        "session-1",
    )

    assert response["message_body"] == "OK"
    assert response["datos_estructura"]["target"] == "municipio"
    assert context["provider"] == "ollama"
    assert context["model_used"] == "glm-test"
    create_mock.assert_called_once()
    kwargs = create_mock.call_args.kwargs
    assert kwargs["model"] == "glm-test"
    assert kwargs["response_format"] == {"type": "json_object"}


def test_llamar_ollama_fails_when_disabled(monkeypatch):
    monkeypatch.delenv("OLLAMA_ENABLED", raising=False)
    monkeypatch.delenv("LLM_OLLAMA_ENABLED", raising=False)

    with pytest.raises(ConnectionError):
        ollama_bridge.llamar_ollama(None, "hola", {}, [], "session-1")
