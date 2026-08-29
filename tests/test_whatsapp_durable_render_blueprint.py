from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _env_map(service):
    return {entry.get("key"): entry for entry in service.get("envVars", [])}


def _assert_web_reference(entry, key):
    assert entry == {
        "key": key,
        "fromService": {
            "type": "web",
            "name": "chatboc-backend",
            "envVarKey": key,
        },
    }


def test_render_blueprint_keeps_whatsapp_durable_cutover_fail_closed():
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    services = {service["name"]: service for service in blueprint["services"]}
    web = services["chatboc-backend"]
    web_env = _env_map(web)

    assert web["preDeployCommand"] == "python -m scripts.run_predeploy_migrations"
    assert web_env["WHATSAPP_INBOUND_DURABILITY_MODE"]["value"] == "legacy"
    assert web_env["WHATSAPP_INBOUND_QUEUE_TENANT_IDS"]["value"] == ""
    assert (
        web_env["WHATSAPP_INBOUND_CELERY_WAKEUP_ENABLED"]["value"]
        == "false"
    )
    assert web_env["CHANNEL_SESSION_IDENTITY_MODE"]["value"] == "shadow"
    assert web_env["WHATSAPP_INBOUND_PAYLOAD_SCRUB_ENABLED"]["value"] == "false"
    assert web_env["WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD"]["value"] == "true"
    assert web_env["WHATSAPP_INBOUND_PAYLOAD_SCRUB_TENANT_IDS"]["value"] == ""

    worker = services["chatboc-whatsapp-durable"]
    worker_env = _env_map(worker)
    assert worker["type"] == "worker"
    assert worker["startCommand"] == (
        "python -m services.whatsapp_inbound_worker --standby-when-legacy"
    )
    assert worker["maxShutdownDelaySeconds"] == 60
    assert worker_env["CHATBOC_PROCESS_ROLE"]["value"] == "whatsapp-durable-worker"
    assert worker_env["WHATSAPP_DURABLE_WORKER_STANDBY_ENABLED"]["value"] == "true"
    for key in (
        "DATABASE_URL",
        "SECRET_KEY",
        "WHATSAPP_INBOUND_QUEUE_TENANT_IDS",
        "WHATSAPP_INBOUND_HASH_SECRET",
        "CHANNEL_SESSION_IDENTITY_MODE",
        "CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1",
        "CHANNEL_SESSION_IDENTITY_VERSION_V1",
    ):
        _assert_web_reference(worker_env[key], key)
    assert worker_env["WHATSAPP_INBOUND_DURABILITY_MODE"]["value"] == "legacy"
    assert "fromService" not in worker_env["WHATSAPP_INBOUND_DURABILITY_MODE"]

    # Replay executes the same AI/media path as the webhook. The Blueprint must
    # not look queue-ready while silently omitting its provider/storage scope.
    for key in (
        "OPENAI_API_KEY",
        "OPENAI_CHAT_MODEL_DEFAULT",
        "OPENAI_CHAT_MODEL_WHATSAPP",
        "GEMINI_API_KEY",
        "GEMINI_CHAT_MODEL",
        "GOOGLE_MAPS_API_KEY",
        "WHATSAPP_MEDIA_DOWNLOAD_TIMEOUT_SECONDS",
        "WHATSAPP_MEDIA_MAX_BYTES",
        "HUGGINGFACE_API_TOKEN",
        "VISION_HUGGINGFACE_ENABLED",
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_SUBACCOUNT_AUTH_TOKEN",
        "R2_ENDPOINT_URL",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET_NAME",
        "R2_PUBLIC_BASE_URL",
    ):
        assert key in web_env
        _assert_web_reference(worker_env[key], key)

    retention = services["chatboc-whatsapp-payload-retention"]
    retention_env = _env_map(retention)
    assert retention["type"] == "cron"
    assert retention["schedule"] == "43 3 * * *"
    assert retention["startCommand"] == (
        "python -m services.whatsapp_inbound_worker --scrub-expired-payloads"
    )
    assert retention_env["CHATBOC_PROCESS_ROLE"]["value"] == (
        "whatsapp-payload-retention-cron"
    )
    for key in (
        "DATABASE_URL",
        "SECRET_KEY",
        "WHATSAPP_INBOUND_PAYLOAD_SCRUB_TENANT_IDS",
        "WHATSAPP_INBOUND_PAYLOAD_SCRUB_ENABLED",
        "WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD",
        "WHATSAPP_INBOUND_DEAD_PAYLOAD_RETENTION_HOURS",
        "WHATSAPP_INBOUND_PAYLOAD_SCRUB_BATCH_SIZE",
    ):
        _assert_web_reference(retention_env[key], key)
