from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _env_map(service):
    return {entry.get("key"): entry for entry in service.get("envVars", [])}


def test_render_blueprint_has_fail_closed_domain_effect_worker():
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    services = {service["name"]: service for service in blueprint["services"]}
    web = services["chatboc-backend"]
    worker = services["chatboc-domain-effects"]

    assert worker["type"] == "worker"
    assert worker["runtime"] == "python"
    assert "env" not in worker
    assert worker["startCommand"] == "python -m services.domain_effect_worker"
    assert worker["maxShutdownDelaySeconds"] >= 60

    web_env = _env_map(web)
    worker_env = _env_map(worker)
    assert web_env["DOMAIN_EFFECT_OUTBOX_MODE"]["value"] == "legacy"
    assert web_env["DOMAIN_EFFECT_OUTBOX_TENANT_IDS"]["value"] == ""
    assert web_env["EMAIL_NOTIFICATIONS_ENABLED"]["value"] == "false"
    assert worker_env["CHATBOC_PROCESS_ROLE"]["value"] == "domain-effect-worker"
    assert worker_env["SIGEM_LIVE_ENABLED"]["value"] == "false"

    for key in (
        "DATABASE_URL",
        "SECRET_KEY",
        "DOMAIN_EFFECT_OUTBOX_MODE",
        "DOMAIN_EFFECT_OUTBOX_SECRET",
        "DOMAIN_EFFECT_OUTBOX_TENANT_IDS",
        "EMAIL_NOTIFICATIONS_ENABLED",
        "ZOHO_SMTP_USER",
        "ZOHO_SMTP_PASSWORD",
    ):
        reference = worker_env[key]["fromService"]
        assert reference == {
            "type": "web",
            "name": "chatboc-backend",
            "envVarKey": key,
        }
