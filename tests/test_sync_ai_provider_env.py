import json

from scripts import sync_ai_provider_env as sync_ai


def test_collect_ai_env_values_reads_secrets_and_defaults(monkeypatch):
    monkeypatch.setenv("HUGGINGFACE_API_TOKEN", "hf_test_secret")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("HUGGINGFACE_ZERO_SHOT_ENABLED", raising=False)

    values = sync_ai.collect_ai_env_values(include_recommended_defaults=True)

    assert values["HUGGINGFACE_API_TOKEN"] == "hf_test_secret"
    assert values["HUGGINGFACE_ZERO_SHOT_ENABLED"] == "true"
    assert values["GEMINI_CHAT_MODEL"] == "gemini-2.5-flash"


def test_dry_run_does_not_print_secret_values(monkeypatch):
    monkeypatch.setenv("HUGGINGFACE_API_TOKEN", "hf_test_secret")

    result = sync_ai.sync_ai_provider_env(include_recommended_defaults=True, dry_run=True)
    payload = json.dumps(result)

    assert result["ok"] is True
    assert result["secret_values_printed"] is False
    assert "HUGGINGFACE_API_TOKEN" in result["keys"]
    assert "hf_test_secret" not in payload


def test_sync_redacts_secret_values_from_render_results(monkeypatch):
    calls = []

    def fake_sync_render_env_var(key, value, config):
        calls.append((key, value))
        return {
            "ok": True,
            "env_var_key": key,
            "debug": f"value={value}",
            "secret_value_stored": True,
        }

    monkeypatch.setenv("HUGGINGFACE_API_TOKEN", "hf_test_secret")
    monkeypatch.setattr(sync_ai.render_env_sync, "sync_render_env_var", fake_sync_render_env_var)

    result = sync_ai.sync_ai_provider_env(dry_run=False)
    payload = json.dumps(result)

    assert result["ok"] is True
    assert calls == [("HUGGINGFACE_API_TOKEN", "hf_test_secret")]
    assert "hf_test_secret" not in payload
    assert "[redacted]" in payload
