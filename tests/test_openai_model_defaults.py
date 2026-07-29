from flask import Flask
from types import SimpleNamespace

from schemas.ai_contracts import GatewayRequest
from services import ai_backoffice_summaries
from services.openai_model_defaults import (
    DEFAULT_OPENAI_SOL_MODEL,
    DEFAULT_OPENAI_TERRA_MODEL,
    chat_completion_compatibility_options,
    resolve_openai_model,
)


def test_model_resolution_is_runtime_override_aware(monkeypatch):
    monkeypatch.delenv("OPENAI_TEST_MODEL", raising=False)
    assert resolve_openai_model("OPENAI_TEST_MODEL", DEFAULT_OPENAI_SOL_MODEL) == "gpt-5.6-sol"

    monkeypatch.setenv("OPENAI_TEST_MODEL", " custom-model ")
    assert resolve_openai_model("OPENAI_TEST_MODEL", DEFAULT_OPENAI_SOL_MODEL) == "custom-model"


def test_chat_options_preserve_legacy_sampling_without_sending_temperature_to_gpt56():
    assert chat_completion_compatibility_options("gpt-5.6-sol") == {
        "reasoning_effort": "none"
    }
    assert chat_completion_compatibility_options(
        "gpt-4o-mini", legacy_temperature=0.2
    ) == {"temperature": 0.2}


def test_gateway_generic_default_is_balanced_terra():
    request = GatewayRequest(tenant_id=1, channel="web")
    assert request.model == DEFAULT_OPENAI_TERRA_MODEL


def test_backoffice_default_is_quality_first_and_overridable(monkeypatch):
    captured = []

    def fake_llm(*args, **kwargs):
        captured.append(kwargs["model"])
        return ({"summary": "ok"}, {"model_used": kwargs["model"]})

    monkeypatch.setattr(ai_backoffice_summaries, "llamar_llm_con_fallback", fake_llm)
    app = Flask(__name__)
    with app.app_context():
        ai_backoffice_summaries._call_backoffice_llm(
            prompt="demo",
            tenant_type="municipio",
            task_type="analytics",
        )
        monkeypatch.setenv("OPENAI_BACKOFFICE_MODEL", "custom-backoffice")
        ai_backoffice_summaries._call_backoffice_llm(
            prompt="demo",
            tenant_type="municipio",
            task_type="analytics",
        )

    assert captured == [DEFAULT_OPENAI_SOL_MODEL, "custom-backoffice"]


def test_survey_brief_uses_sol_without_legacy_temperature(monkeypatch):
    from services import encuestas_analytics_service as analytics

    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"headline":"Resumen","insights":[],"risk_level":"low"}'
                    )
                )
            ]
        )

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(analytics, "openai_client", fake_client)
    monkeypatch.delenv("OPENAI_SURVEY_ANALYTICS_MODEL", raising=False)

    result = analytics._generate_openai_executive_brief(
        SimpleNamespace(id=7, titulo="Encuesta"),
        {"total_respuestas": 10, "participantes_unicos": 8},
        {"projected_total": 12, "horizon_minutes": 60, "momentum": "stable"},
        {"alerts": []},
    )

    assert result["headline"] == "Resumen"
    assert captured["model"] == DEFAULT_OPENAI_SOL_MODEL
    assert captured["reasoning_effort"] == "none"
    assert "temperature" not in captured
