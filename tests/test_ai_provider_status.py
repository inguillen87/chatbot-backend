import json

from services.ai_provider_status import build_ai_provider_status


def test_provider_status_never_exposes_secret_values(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret")
    monkeypatch.setenv("HUGGINGFACE_API_TOKEN", "hf_test_secret")
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "openai,gemini")
    monkeypatch.setenv("HUGGINGFACE_ENABLED", "true")
    monkeypatch.setenv("HUGGINGFACE_ZERO_SHOT_ENABLED", "true")

    payload = build_ai_provider_status(include_smoke=True)
    serialized = json.dumps(payload)

    assert payload["secret_values_exposed"] is False
    assert payload["providers"]["gemini"]["configured"] is True
    assert payload["providers"]["huggingface"]["configured"] is True
    assert "sk-test-secret" not in serialized
    assert "gemini-secret" not in serialized
    assert "hf_test_secret" not in serialized


def test_provider_status_warns_when_enabled_without_token(monkeypatch):
    monkeypatch.delenv("HUGGINGFACE_API_TOKEN", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setenv("HUGGINGFACE_ENABLED", "true")

    payload = build_ai_provider_status()

    assert "huggingface_enabled_but_missing_token" in payload["readiness"]["warnings"]


def test_admin_provider_status_endpoint_requires_admin(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")

    ok = client.get("/admin/ai/provider-status", headers={"X-Debug-Role": "admin", "X-Debug-Tenant": "*"})
    assert ok.status_code == 200
    assert ok.get_json()["contract_version"] == "ai.provider_status.v1"

    forbidden = client.get("/admin/ai/provider-status", headers={"X-Debug-Role": "visor", "X-Debug-Tenant": "*"})
    assert forbidden.status_code == 403
