import json
import types

import pytest

from services import openai_maps_service


class _DummyHttpxClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


@pytest.fixture(autouse=True)
def _base_patches(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        openai_maps_service,
        "httpx",
        type("_HTTPX", (), {"Client": _DummyHttpxClient}),
    )
    yield
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def _make_schema():
    return {"name": "demo", "schema": {"type": "object"}}


def _call_service():
    return openai_maps_service._solicitar_json_a_openai(
        api_key="test-key",
        prompt="demo",
        schema=_make_schema(),
        system_message="",
    )


def test_usa_responses_api_prioritariamente(monkeypatch):
    class _DummyResponses:
        def __init__(self):
            self.called_with = None

        def create(self, **kwargs):
            self.called_with = kwargs
            return types.SimpleNamespace(
                output=[
                    types.SimpleNamespace(
                        content=[
                            types.SimpleNamespace(
                                text=json.dumps({"lat": 9.87, "lon": 6.54})
                            )
                        ]
                    )
                ]
            )

    dummy_responses = _DummyResponses()

    class _Client:
        def __init__(self, *_, **__):
            self.responses = dummy_responses
            self.chat = None

    monkeypatch.setattr(openai_maps_service.openai, "OpenAI", _Client)
    monkeypatch.delattr(openai_maps_service.openai, "ChatCompletion", raising=False)

    resultado = _call_service()

    assert resultado == {"lat": 9.87, "lon": 6.54}
    assert dummy_responses.called_with is not None
    assert dummy_responses.called_with["model"] == openai_maps_service.DEFAULT_MODEL


def test_fallback_a_chat_completions_del_cliente(monkeypatch):
    class _DummyCompletions:
        def __init__(self):
            self.called_with = None

        def create(self, **kwargs):
            self.called_with = kwargs
            return types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        message=types.SimpleNamespace(
                            content=json.dumps({"lat": 1.23, "lon": 4.56})
                        )
                    )
                ]
            )

    dummy_completions = _DummyCompletions()

    class _Chat:
        def __init__(self):
            self.completions = dummy_completions

    class _Client:
        def __init__(self, *_, **__):
            self.responses = None
            self.chat = _Chat()

    monkeypatch.setattr(openai_maps_service.openai, "OpenAI", _Client)
    monkeypatch.delattr(openai_maps_service.openai, "ChatCompletion", raising=False)

    resultado = _call_service()

    assert resultado == {"lat": 1.23, "lon": 4.56}
    assert dummy_completions.called_with is not None
    assert (
        dummy_completions.called_with["model"]
        == openai_maps_service.CHAT_FALLBACK_MODEL
    )


def test_fallback_final_a_chatcompletion_legado(monkeypatch):
    class _DummyChat:
        def __init__(self):
            self.called_with = None

        def create(self, **kwargs):
            self.called_with = kwargs
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"lat": 7.89, "lon": 0.12})
                        }
                    }
                ]
            }

    dummy_chat = _DummyChat()

    monkeypatch.delattr(openai_maps_service.openai, "OpenAI", raising=False)
    monkeypatch.setattr(openai_maps_service.openai, "ChatCompletion", dummy_chat)

    resultado = _call_service()

    assert resultado == {"lat": 7.89, "lon": 0.12}
    assert dummy_chat.called_with is not None
