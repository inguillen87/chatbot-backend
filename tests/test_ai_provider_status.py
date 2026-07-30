import json
from datetime import datetime, timedelta, timezone

from services import huggingface_inference_service as hf
from services import ai_provider_status as status_service
from services.ai_provider_status import build_ai_provider_status, build_ai_provider_status_public_view


def _recent_utc_iso(*, minutes_ago: int = 5) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).isoformat().replace("+00:00", "Z")


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
    assert payload["providers"]["openai"]["runtime_status"] == "configured_unverified"
    assert payload["providers"]["openai"]["live_verified"] is False
    assert payload["readiness"]["chat_runtime_configured"] is True
    assert payload["readiness"]["chat_ready"] is False
    assert "openai_configured_but_not_live_verified" in payload["readiness"]["warnings"]
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


def test_openai_status_requires_explicit_live_verification_gate(monkeypatch):
    verified_at = _recent_utc_iso()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "openai")
    monkeypatch.setenv("OPENAI_PROVIDER_LIVE_VERIFIED", "true")
    monkeypatch.setenv("OPENAI_PROVIDER_LIVE_VERIFIED_AT", verified_at)

    payload = build_ai_provider_status()

    assert payload["providers"]["openai"]["runtime_status"] == "live_verified"
    assert payload["providers"]["openai"]["credential_status"] == "live_verified"
    assert payload["providers"]["openai"]["live_verified"] is True
    assert payload["providers"]["openai"]["live_verified_at"] == verified_at
    assert payload["readiness"]["selected_chat_provider"] == "openai"
    assert payload["readiness"]["chat_runtime_configured"] is True
    assert payload["readiness"]["chat_ready"] is False
    assert payload["readiness"]["status"] == "warning"
    assert "chat_capability_live_verification_missing" in payload["readiness"]["warnings"]
    assert "openai_configured_but_not_live_verified" not in payload["readiness"]["warnings"]
    assert payload["openai_suite"]["provider_verification"] == {
        "status": "live_verified",
        "live_verified": True,
        "live_verified_at": verified_at,
        "scope": "provider_connectivity_only",
    }
    assert payload["openai_suite"]["status"] == "partially_verified"
    assert payload["openai_suite"]["capabilities"]["chat_responses"]["status"] == "unverified"
    assert payload["openai_suite"]["capabilities"]["chat_responses"]["live_verified"] is False


def test_openai_live_flag_without_valid_dated_evidence_fails_closed(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "openai")
    monkeypatch.setenv("OPENAI_PROVIDER_LIVE_VERIFIED", "true")
    monkeypatch.setenv("OPENAI_PROVIDER_LIVE_VERIFIED_AT", "not-a-timestamp sk-secret")

    payload = build_ai_provider_status()

    assert payload["providers"]["openai"]["runtime_status"] == "configured_unverified"
    assert payload["providers"]["openai"]["credential_status"] == "present_unverified"
    assert payload["providers"]["openai"]["live_verified"] is False
    assert payload["providers"]["openai"]["live_verified_at"] is None
    assert "openai_live_verification_evidence_invalid" in payload["readiness"]["warnings"]
    assert payload["openai_suite"]["provider_verification"]["status"] == "present_unverified"


def test_openai_live_evidence_expires_after_seven_days(monkeypatch):
    stale_at = (
        datetime.now(timezone.utc) - timedelta(hours=169)
    ).isoformat().replace("+00:00", "Z")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "openai")
    monkeypatch.setenv("OPENAI_PROVIDER_LIVE_VERIFIED", "true")
    monkeypatch.setenv("OPENAI_PROVIDER_LIVE_VERIFIED_AT", stale_at)

    payload = build_ai_provider_status()

    assert payload["providers"]["openai"]["live_verified"] is False
    assert payload["providers"]["openai"]["live_verified_at"] is None
    assert payload["readiness"]["chat_runtime_configured"] is True
    assert payload["readiness"]["chat_ready"] is False
    assert "openai_live_verification_evidence_invalid" in payload["readiness"]["warnings"]


def test_openai_suite_reports_each_modality_without_inference(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "openai")
    monkeypatch.setenv("OPENAI_PROVIDER_LIVE_VERIFIED", "false")
    monkeypatch.delenv("OPENAI_PROVIDER_LIVE_VERIFIED_AT", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("VOICE_STREAM_SIGNING_SECRET", raising=False)
    monkeypatch.delenv("VOICE_STREAM_REPLAY_REDIS_URL", raising=False)
    monkeypatch.delenv("SOCKETIO_MESSAGE_QUEUE_URL", raising=False)
    monkeypatch.delenv("SOCKETIO_REDIS_URL", raising=False)
    monkeypatch.setenv("CELERY_BROKER_URL", "amqp://not-a-valid-replay-store")

    suite = build_ai_provider_status()["openai_suite"]

    assert suite["status"] == "unverified"
    assert set(suite["capabilities"]) == {
        "chat_responses",
        "vision",
        "stt",
        "tts",
        "realtime_voice",
    }
    for key in ("chat_responses", "vision", "stt", "tts"):
        assert suite["capabilities"][key]["status"] == "unverified"
        assert suite["capabilities"][key]["runtime_configured"] is True
        assert suite["capabilities"][key]["live_verified"] is False
    realtime = suite["capabilities"]["realtime_voice"]
    assert realtime["status"] == "blocked"
    assert "twilio_request_auth_missing" in realtime["reason_codes"]
    assert "voice_stream_replay_store_missing" in realtime["reason_codes"]


def test_openai_suite_is_blocked_when_key_is_missing(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "openai")
    monkeypatch.delenv("OPENAI_PROVIDER_LIVE_VERIFIED", raising=False)
    monkeypatch.delenv("OPENAI_PROVIDER_LIVE_VERIFIED_AT", raising=False)

    suite = build_ai_provider_status()["openai_suite"]

    assert suite["status"] == "blocked"
    assert suite["key_configured"] is False
    assert suite["provider_verification"]["status"] == "missing"
    assert all(capability["status"] == "blocked" for capability in suite["capabilities"].values())


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


def test_specialized_provider_configuration_is_not_live_readiness(monkeypatch):
    hf.clear_last_huggingface_failure()
    monkeypatch.setenv("HUGGINGFACE_API_TOKEN", "hf-runtime-configured")
    monkeypatch.setenv("HUGGINGFACE_ENABLED", "true")
    monkeypatch.setenv("HUGGINGFACE_ZERO_SHOT_ENABLED", "true")
    monkeypatch.setattr(status_service, "_module_available", lambda _name: True)

    readiness = build_ai_provider_status()["readiness"]

    assert readiness["specialized_ai_runtime_configured"] is True
    assert readiness["specialized_ai_ready"] is False
    assert "specialized_ai_live_verification_missing" in readiness["warnings"]


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
    assert payload["openai_suite"]["capability_evidence_available"] is False
    assert payload["openai_suite"]["capabilities"]["vision"]["live_verified"] is False

    hf.clear_last_huggingface_failure()


def test_public_provider_status_drops_raw_urls_errors_and_untrusted_evidence():
    payload = build_ai_provider_status_public_view(
        {
            "generated_at": "2026-07-30T10:00:00Z",
            "llm_provider_order": ["openai"],
            "readiness": {
                "chat_ready": True,
                "specialized_ai_ready": True,
                "status": "ready",
                "warnings": [
                    "provider_live_verification_missing",
                    "raw error https://secret.invalid sk-public-secret",
                ],
            },
            "providers": {
                "openai": {
                    "configured": True,
                    "base_url": "https://secret.invalid/sk-public-secret",
                    "last_failure": {
                        "reason_code": "provider_call_failed",
                        "message": "401 sk-public-secret",
                    },
                },
                "sk-public-secret": {"configured": True},
            },
            "openai_suite": {
                "status": "live_verified",
                "key_configured": True,
                "runtime_configured": True,
                "provider_verification": {
                    "status": "live_verified",
                    "live_verified": True,
                    "live_verified_at": "not-valid sk-public-secret",
                },
                "reason_codes": ["provider_live_verification_missing", "sk-public-secret"],
                "capabilities": {
                    "chat_responses": {
                        "status": "unverified",
                        "runtime_configured": False,
                        "provider_live_verified": True,
                    },
                    "vision": {
                        "status": "live_verified",
                        "runtime_configured": True,
                        "provider_live_verified": True,
                        "live_verified": True,
                        "live_verified_at": "2026-07-30T09:00:00Z",
                        "reason_codes": ["capability_live_verification_missing", "sk-public-secret"],
                        "configuration_env": ["OPENAI_VISION_MODEL", "SK_PUBLIC_SECRET"],
                    }
                },
            },
        }
    )
    serialized = json.dumps(payload)

    assert payload["readiness"]["warnings"] == ["provider_live_verification_missing"]
    assert payload["readiness"]["chat_runtime_configured"] is False
    assert payload["readiness"]["chat_ready"] is False
    assert payload["readiness"]["specialized_ai_runtime_configured"] is False
    assert payload["readiness"]["specialized_ai_ready"] is False
    assert payload["readiness"]["status"] == "blocked"
    assert payload["openai_suite"]["status"] == "unverified"
    assert payload["openai_suite"]["provider_verification"]["status"] == "present_unverified"
    assert payload["openai_suite"]["capabilities"]["vision"]["live_verified"] is False
    assert payload["openai_suite"]["capabilities"]["chat_responses"]["status"] == "blocked"
    assert payload["openai_suite"]["capabilities"]["vision"]["configuration_env"] == ["OPENAI_VISION_MODEL"]
    assert "base_url" not in serialized
    assert "sk-public-secret" not in payload["providers"]
    assert "secret.invalid" not in serialized
    assert "sk-public-secret" not in serialized
    assert "401" not in serialized
    assert payload["frontend_contract"]["access_tenant_scoped"] is True
    assert payload["frontend_contract"]["configuration_scope"] == "platform_runtime"
    assert "tenant_scoped" not in payload["frontend_contract"]


def test_admin_provider_status_endpoint_requires_admin(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")

    ok = client.get("/admin/ai/provider-status", headers={"X-Debug-Role": "admin", "X-Debug-Tenant": "*"})
    assert ok.status_code == 200
    assert ok.get_json()["contract_version"] == "ai.provider_status.v1"

    forbidden = client.get("/admin/ai/provider-status", headers={"X-Debug-Role": "visor", "X-Debug-Tenant": "*"})
    assert forbidden.status_code == 403
