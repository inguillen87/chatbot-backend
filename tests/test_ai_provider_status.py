import json

from services import huggingface_inference_service as hf
from services.ai_provider_status import build_ai_provider_status, build_ai_provider_status_public_view


def test_provider_status_never_exposes_secret_values(monkeypatch):
    hf.clear_last_huggingface_failure()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret")
    monkeypatch.setenv("HUGGINGFACE_API_TOKEN", "hf_test_secret")
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "openai,gemini")
    monkeypatch.setenv("HUGGINGFACE_ENABLED", "true")
    monkeypatch.setenv("HUGGINGFACE_ZERO_SHOT_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-test-secret")
    monkeypatch.setenv("OLLAMA_CHAT_MODEL", "glm-5.2:cloud")

    payload = build_ai_provider_status(include_smoke=True)
    serialized = json.dumps(payload)

    assert payload["secret_values_exposed"] is False
    assert payload["providers"]["gemini"]["configured"] is True
    assert payload["providers"]["ollama"]["configured"] is True
    assert payload["providers"]["ollama"]["chat_model"] == "glm-5.2:cloud"
    assert payload["providers"]["huggingface"]["configured"] is True
    assert payload["providers"]["huggingface"]["reclamo_signal_min_score"] == "0.62"
    assert payload["providers"]["huggingface"]["pyme_intent_min_score"] == "0.62"
    assert "sk-test-secret" not in serialized
    assert "gemini-secret" not in serialized
    assert "hf_test_secret" not in serialized
    assert "ollama-test-secret" not in serialized


def test_provider_status_warns_when_enabled_without_token(monkeypatch):
    hf.clear_last_huggingface_failure()
    monkeypatch.delenv("HUGGINGFACE_API_TOKEN", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setenv("HUGGINGFACE_ENABLED", "true")

    payload = build_ai_provider_status()

    assert "huggingface_enabled_but_missing_token" in payload["readiness"]["warnings"]


def test_provider_status_reports_huggingface_quota_degradation(monkeypatch):
    hf.clear_last_huggingface_failure()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "openai")
    monkeypatch.setenv("HUGGINGFACE_API_TOKEN", "hf_status_secret")
    monkeypatch.setenv("HUGGINGFACE_ENABLED", "true")
    monkeypatch.setenv("HUGGINGFACE_ZERO_SHOT_ENABLED", "true")

    try:
        raise RuntimeError("402 Payment Required: depleted your monthly included credits for hf_status_secret")
    except RuntimeError as exc:
        hf._record_failure("zero_shot", exc)

    payload = build_ai_provider_status()
    serialized = json.dumps(payload)

    assert payload["providers"]["huggingface"]["runtime_status"] == "degraded"
    assert payload["providers"]["huggingface"]["quota_depleted"] is True
    assert payload["providers"]["huggingface"]["fallback_behavior"] == "deterministic_local_fallback"
    assert "huggingface_quota_or_payment_required" in payload["readiness"]["warnings"]
    assert payload["readiness"]["specialized_ai_ready"] is False
    assert "hf_status_secret" not in serialized

    hf.clear_last_huggingface_failure()


def test_provider_status_warns_when_ollama_ordered_but_disabled(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "ollama,openai")
    monkeypatch.delenv("OLLAMA_ENABLED", raising=False)
    monkeypatch.delenv("LLM_OLLAMA_ENABLED", raising=False)

    payload = build_ai_provider_status()

    assert "ollama_in_provider_order_but_disabled" in payload["readiness"]["warnings"]
    assert payload["providers"]["ollama"]["enabled"] is False


def test_public_provider_status_is_safe_for_tenant_crm(monkeypatch):
    hf.clear_last_huggingface_failure()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-public-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-public-secret")
    monkeypatch.setenv("HUGGINGFACE_API_TOKEN", "hf_public_secret")
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "gemini,openai")
    monkeypatch.setenv("HUGGINGFACE_ENABLED", "true")
    monkeypatch.setenv("HUGGINGFACE_ZERO_SHOT_ENABLED", "true")

    try:
        raise RuntimeError("402 Payment Required: hf_public_secret depleted")
    except RuntimeError as exc:
        hf._record_failure("zero_shot", exc)

    payload = build_ai_provider_status_public_view()
    serialized = json.dumps(payload)

    assert payload["contract_version"] == "ai.provider_status_public.v1"
    assert payload["secret_values_exposed"] is False
    assert payload["frontend_contract"]["render_as"] == "operations_ai_provider_status"
    assert payload["providers"]["gemini"]["configured"] is True
    assert payload["providers"]["huggingface"]["last_failure"] == {
        "reason_code": "huggingface_quota_or_payment_required",
        "task": "zero_shot",
        "error_type": "RuntimeError",
    }
    assert "base_url" not in payload["providers"]["ollama"]
    assert "smoke" not in payload
    assert "sk-public-secret" not in serialized
    assert "gemini-public-secret" not in serialized
    assert "hf_public_secret" not in serialized
    assert "Payment Required" not in serialized

    hf.clear_last_huggingface_failure()


def test_admin_provider_status_endpoint_requires_admin(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")

    ok = client.get("/admin/ai/provider-status", headers={"X-Debug-Role": "admin", "X-Debug-Tenant": "*"})
    assert ok.status_code == 200
    assert ok.get_json()["contract_version"] == "ai.provider_status.v1"

    forbidden = client.get("/admin/ai/provider-status", headers={"X-Debug-Role": "visor", "X-Debug-Tenant": "*"})
    assert forbidden.status_code == 403
