from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _env_map(service):
    return {entry.get("key"): entry for entry in service.get("envVars", [])}


def test_render_blueprint_declares_permanent_survey_effect_worker():
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    services = {service["name"]: service for service in blueprint["services"]}
    web = services["chatboc-backend"]
    worker = services["chatboc-survey-effects"]

    assert worker["type"] == "worker"
    assert worker["runtime"] == "python"
    assert worker["startCommand"] == (
        "python -m services.survey_response_effect_worker"
    )
    assert worker["maxShutdownDelaySeconds"] >= 120

    web_env = _env_map(web)
    worker_env = _env_map(worker)
    assert worker_env["CHATBOC_PROCESS_ROLE"]["value"] == "survey-effect-worker"
    for key in (
        "SURVEY_RESPONSE_EFFECT_LEASE_SECONDS",
        "SURVEY_RESPONSE_EFFECT_WORKER_BATCH_SIZE",
        "SURVEY_RESPONSE_EFFECT_WORKER_MAX_TENANTS_PER_CYCLE",
        "SURVEY_RESPONSE_EFFECT_WORKER_POLL_SECONDS",
        "SOCKETIO_MESSAGE_QUEUE_URL",
        "SOCKETIO_MESSAGE_QUEUE_CHANNEL",
        "SOCKETIO_MESSAGE_QUEUE_HEALTHCHECK_TIMEOUT_SECONDS",
    ):
        assert key in web_env
        assert worker_env[key]["fromService"] == {
            "type": "web",
            "name": "chatboc-backend",
            "envVarKey": key,
        }

    assert worker_env["DATABASE_URL"]["fromService"]["name"] == "chatboc-backend"
    assert worker_env["SECRET_KEY"]["fromService"]["name"] == "chatboc-backend"
    assert web_env["SOCKETIO_MESSAGE_QUEUE_URL"] == {
        "key": "SOCKETIO_MESSAGE_QUEUE_URL",
        "sync": False,
    }
    assert web_env["SOCKETIO_MESSAGE_QUEUE_CHANNEL"]["value"] == (
        "chatboc-realtime-v1"
    )
