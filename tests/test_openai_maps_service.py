import json
import os

import pytest

from services import openai_maps_service


class _DummyHttpxClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _DummyOpenAIClient:
    """Simula un cliente OpenAI sin soporte para responses.create."""

    def __init__(self, *_, **__):
        pass


class _DummyChatCompletion:
    """Captura la llamada al fallback de ChatCompletion."""

    def __init__(self):
        self.called_with = None

    def create(self, **kwargs):
        self.called_with = kwargs
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({"lat": 1.23, "lon": 4.56})
                    }
                }
            ]
        }


@pytest.fixture(autouse=True)
def _patch_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(openai_maps_service, "httpx", type("_HTTPX", (), {"Client": _DummyHttpxClient}))

    dummy_chat = _DummyChatCompletion()
    monkeypatch.setattr(openai_maps_service.openai, "OpenAI", _DummyOpenAIClient)
    monkeypatch.setattr(openai_maps_service.openai, "ChatCompletion", dummy_chat)
    yield dummy_chat
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_solicitar_json_usa_fallback_chat_completion(_patch_openai):
    resultado = openai_maps_service._solicitar_json_a_openai(
        api_key=os.environ["OPENAI_API_KEY"],
        prompt="demo",
        schema={"name": "demo", "schema": {"type": "object"}},
        system_message="",
    )

    assert resultado == {"lat": 1.23, "lon": 4.56}
    assert _patch_openai.called_with is not None
    assert (
        _patch_openai.called_with["model"]
        == openai_maps_service.CHAT_FALLBACK_MODEL
    )
