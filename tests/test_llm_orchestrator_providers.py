from unittest.mock import Mock, patch

from services import llm_orchestrator


def test_provider_order_skips_unconfigured_gemini(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "gemini,openai")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_GENAI_API_KEY", raising=False)

    assert llm_orchestrator._provider_order_from_env() == ["openai"]


def test_provider_order_includes_configured_gemini(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "gemini,openai")
    monkeypatch.setenv("GEMINI_API_KEY", "test")

    assert llm_orchestrator._provider_order_from_env() == ["gemini", "openai"]


def test_llamar_llm_con_fallback_uses_gemini(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test")

    mock_gemini = Mock(return_value=({"message_body": "hola gemini"}, {"provider": "gemini"}))
    with patch("services.gemini_bridge.llamar_gemini", mock_gemini):
        response, context = llm_orchestrator.llamar_llm_con_fallback(
            None,
            "hola",
            {},
            [],
            "session-1",
            model="gemini-test",
        )

    assert response["message_body"] == "hola gemini"
    assert context["provider"] == "gemini"
    mock_gemini.assert_called_once()
