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


def test_provider_order_includes_enabled_ollama(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "ollama,gemini,openai")
    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_GENAI_API_KEY", raising=False)

    assert llm_orchestrator._provider_order_from_env() == ["ollama", "openai"]


def test_provider_order_skips_disabled_ollama(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "ollama,openai")
    monkeypatch.delenv("OLLAMA_ENABLED", raising=False)
    monkeypatch.delenv("LLM_OLLAMA_ENABLED", raising=False)

    assert llm_orchestrator._provider_order_from_env() == ["openai"]


def test_llamar_llm_con_fallback_uses_ollama(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "ollama")
    monkeypatch.setenv("OLLAMA_ENABLED", "true")

    mock_ollama = Mock(return_value=({"message_body": "hola ollama"}, {"provider": "ollama"}))
    with patch("services.ollama_bridge.llamar_ollama", mock_ollama):
        response, context = llm_orchestrator.llamar_llm_con_fallback(
            None,
            "hola",
            {"tipo_entidad": "municipio"},
            [],
            "session-ollama",
            model="glm-5.2:cloud",
        )

    assert response["message_body"] == "hola ollama"
    assert context["provider"] == "ollama"
    mock_ollama.assert_called_once()


def test_task_provider_order_prefers_ollama_for_backoffice_when_enabled(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "openai,ollama")
    monkeypatch.setenv("OLLAMA_ENABLED", "true")

    assert llm_orchestrator._task_provider_order("survey_insights") == ["ollama", "openai"]


def test_task_provider_order_keeps_ollama_out_of_transactional_flows(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "ollama,openai")
    monkeypatch.setenv("OLLAMA_ENABLED", "true")

    assert llm_orchestrator._task_provider_order("whatsapp_realtime") == ["openai"]


def test_build_llm_task_policy_exposes_secret_safe_analytics_policy(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "openai,ollama")
    monkeypatch.setenv("OLLAMA_ENABLED", "true")

    policy = llm_orchestrator.build_llm_task_policy("analytics")

    assert policy["contract_version"] == "llm.task_policy.v1"
    assert policy["task_type"] == "analytics"
    assert policy["primary_provider"] == "ollama"
    assert policy["provider_order"] == ["ollama", "openai"]
    assert policy["open_source_ready"] is True
    assert policy["backoffice_optimized"] is True
    assert "OPENAI_API_KEY" not in str(policy)


def test_build_llm_task_policy_keeps_realtime_conservative(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "ollama,openai")
    monkeypatch.setenv("OLLAMA_ENABLED", "true")

    policy = llm_orchestrator.build_llm_task_policy("whatsapp_realtime")

    assert policy["primary_provider"] == "openai"
    assert policy["provider_order"] == ["openai"]
    assert policy["open_source_ready"] is False
    assert policy["realtime_safe"] is True


def test_llamar_llm_con_fallback_routes_by_task_type(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "openai,ollama")
    monkeypatch.setenv("OLLAMA_ENABLED", "true")

    mock_ollama = Mock(return_value=({"message_body": "resumen glm"}, {"provider": "ollama"}))
    with patch("services.ollama_bridge.llamar_ollama", mock_ollama):
        response, context = llm_orchestrator.llamar_llm_con_fallback(
            None,
            "resumi tickets",
            {"ai_task_type": "crm_summary"},
            [],
            "session-summary",
        )

    assert response["message_body"] == "resumen glm"
    assert context["provider"] == "ollama"
    mock_ollama.assert_called_once()
