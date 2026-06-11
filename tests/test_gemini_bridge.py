from types import SimpleNamespace

import pytest

from services import gemini_bridge


class FakeGenerateContentConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeContent:
    def __init__(self, role, parts):
        self.role = role
        self.parts = parts


class FakePart:
    def __init__(self, text):
        self.text = text


class FakeTypes:
    GenerateContentConfig = FakeGenerateContentConfig
    Content = FakeContent
    Part = FakePart


class FakeModels:
    def __init__(self):
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text='{"respuesta_usuario":"Hola","datos_estructura":{}}')


class FakeClient:
    last_instance = None

    def __init__(self, api_key):
        self.api_key = api_key
        self.models = FakeModels()
        FakeClient.last_instance = self


class FakeGenai:
    Client = FakeClient


def test_gemini_chat_model_default(monkeypatch):
    monkeypatch.delenv("GEMINI_CHAT_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)

    assert gemini_bridge._gemini_chat_model() == "gemini-2.5-flash"


def test_gemini_chat_model_can_be_overridden(monkeypatch):
    monkeypatch.setenv("GEMINI_CHAT_MODEL", "gemini-test")

    assert gemini_bridge._gemini_chat_model() == "gemini-test"


def test_llamar_gemini_normalizes_chatboc_contract(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_CHAT_MODEL", "gemini-test")
    monkeypatch.setattr(gemini_bridge, "_get_genai_modules", lambda: (FakeGenai, FakeTypes))

    response, context = gemini_bridge.llamar_gemini(
        None,
        '{"texto":"hola","instruccion_canal":"modo voz"}',
        {"tipo_entidad": "municipio"},
        [{"role": "model", "parts": [{"text": "respuesta previa"}]}],
        "session-1",
    )

    assert response["message_body"] == "Hola"
    assert response["accion_backend"] == "responder_directamente"
    assert response["datos_estructura"]["target"] == "municipio"
    assert response["botones"] == []
    assert context == {"model_used": "gemini-test", "provider": "gemini"}

    call = FakeClient.last_instance.models.calls[0]
    assert call["model"] == "gemini-test"
    assert call["config"].kwargs["response_mime_type"] == "application/json"
    assert "modo voz" in call["config"].kwargs["system_instruction"]


def test_llamar_gemini_requires_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_GENAI_API_KEY", raising=False)

    with pytest.raises(ConnectionError):
        gemini_bridge.llamar_gemini(None, "hola", {}, [], "session-1")
