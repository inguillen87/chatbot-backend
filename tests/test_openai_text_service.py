from types import SimpleNamespace

import pytest

from services import openai_text_service


class _FakeResponses:
    def __init__(self, *, output_text="{}", error=None):
        self.calls = []
        self.output_text = output_text
        self.error = error

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(output_text=self.output_text, output=[])


class _FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **_kwargs: pytest.fail("must not retry through Chat Completions")
            )
        )


def test_robust_chat_uses_responses_without_storing_and_hashes_user(monkeypatch):
    responses = _FakeResponses(output_text='{"categoria":"Luminaria"}')
    monkeypatch.setattr(openai_text_service, "_get_openai_client", lambda: _FakeClient(responses))
    monkeypatch.setenv("OPENAI_SAFETY_IDENTIFIER_SECRET", "test-secret")
    monkeypatch.setenv("OPENAI_ROBUST_CHAT_MODEL", "gpt-5.6-sol")

    result = openai_text_service.robust_chat(
        "Extrae el reclamo en JSON",
        user_id="citizen-32877851",
    )

    assert result == '{"categoria":"Luminaria"}'
    request = responses.calls[0]
    assert request["model"] == "gpt-5.6-sol"
    assert request["store"] is False
    assert request["input"] == "Extrae el reclamo en JSON"
    assert request["safety_identifier"] != "citizen-32877851"
    assert len(request["safety_identifier"]) == 64


def test_robust_chat_does_not_retry_an_api_failure(monkeypatch):
    responses = _FakeResponses(error=RuntimeError("provider unavailable"))
    monkeypatch.setattr(openai_text_service, "_get_openai_client", lambda: _FakeClient(responses))

    with pytest.raises(RuntimeError, match="provider unavailable"):
        openai_text_service.robust_chat("Extrae el reclamo")

    assert len(responses.calls) == 1


def test_get_client_requires_real_key_instead_of_dummy(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(openai_text_service, "_OPENAI_CLIENT", None)
    monkeypatch.setattr(openai_text_service, "_OPENAI_CLIENT_KEY_DIGEST", None)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        openai_text_service._get_openai_client()


def test_llm_utils_uses_real_openai_compatibility_layer():
    from services import llm_utils

    assert llm_utils.robust_chat is openai_text_service.robust_chat
