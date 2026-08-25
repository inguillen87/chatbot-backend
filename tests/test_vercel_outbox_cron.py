from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from routes.internal_cron import internal_cron_bp
from services import outbox_reconciliation as reconciliation


CRON_SECRET = "cron-test-secret-" + ("x" * 32)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _route_app(*, secret: str = CRON_SECRET, enabled: bool = True) -> Flask:
    app = Flask(__name__)
    app.config["CRON_SECRET"] = secret
    app.config["VERCEL_OUTBOX_CRON_ENABLED"] = enabled
    app.register_blueprint(internal_cron_bp)
    return app


def _successful_reconciliation() -> dict:
    return {
        "contract_version": "outbox.reconciliation.v1",
        "ok": True,
        "status": "completed",
        "component_count": 3,
        "failed_component_count": 0,
        "components": {},
    }


def test_reconciliation_runs_every_worker_once_and_preserves_legacy_standby():
    app = Flask(__name__)
    with (
        patch.object(
            reconciliation,
            "run_whatsapp_durable_worker",
            return_value={
                "contract_version": "whatsapp.durable_worker_standby.v1",
                "status": "standby",
                "mode": "legacy",
                "cycles": 0,
            },
        ) as whatsapp,
        patch.object(
            reconciliation,
            "run_domain_effect_worker",
            return_value={
                "contract_version": "domain.effect_worker_run.v1",
                "cycles": 1,
                "processed": 0,
            },
        ) as domain,
        patch.object(
            reconciliation,
            "run_survey_response_effect_worker",
            return_value={
                "contract_version": "surveys.response_effect_worker_run.v1",
                "cycles": 1,
                "processed": 0,
                "cycle_failures": 0,
            },
        ) as survey,
    ):
        report = reconciliation.run_outbox_reconciliation(app)

    whatsapp.assert_called_once_with(
        app,
        once=True,
        standby_when_legacy=True,
    )
    domain.assert_called_once_with(app, once=True)
    survey.assert_called_once_with(app, once=True)
    assert report["ok"] is True
    assert report["status"] == "completed"
    assert report["components"]["whatsapp"]["report"]["status"] == "standby"


def test_reconciliation_isolates_failures_and_redacts_exception_detail(caplog):
    private_detail = "postgresql://admin:secret@private-db/chatboc"
    app = Flask(__name__)
    with (
        patch.object(
            reconciliation,
            "run_whatsapp_durable_worker",
            return_value={"contract_version": "whatsapp.test.v1", "cycles": 1},
        ) as whatsapp,
        patch.object(
            reconciliation,
            "run_domain_effect_worker",
            side_effect=RuntimeError(private_detail),
        ) as domain,
        patch.object(
            reconciliation,
            "run_survey_response_effect_worker",
            return_value={"contract_version": "survey.test.v1", "cycles": 1},
        ) as survey,
    ):
        report = reconciliation.run_outbox_reconciliation(app)

    whatsapp.assert_called_once()
    domain.assert_called_once()
    survey.assert_called_once()
    assert report["ok"] is False
    assert report["status"] == "degraded"
    assert report["failed_component_count"] == 1
    assert report["components"]["domain_effects"] == {
        "status": "failed",
        "error_type": "RuntimeError",
    }
    assert private_detail not in repr(report)
    assert private_detail not in caplog.text


def test_missing_cron_secret_fails_closed_without_running_workers():
    app = _route_app(secret="")
    with patch.object(reconciliation, "run_outbox_reconciliation") as run:
        response = app.test_client().get(
            "/api/internal/cron/outbox-reconciliation",
            headers={"Authorization": "Bearer "},
        )

    assert response.status_code == 401
    assert response.get_json()["status"] == "unauthorized"
    assert response.headers["Cache-Control"] == "no-store"
    run.assert_not_called()


def test_short_cron_secret_fails_closed_without_running_workers():
    app = _route_app(secret="too-short")
    with patch.object(reconciliation, "run_outbox_reconciliation") as run:
        response = app.test_client().get(
            "/api/internal/cron/outbox-reconciliation",
            headers={"Authorization": "Bearer too-short"},
        )

    assert response.status_code == 401
    run.assert_not_called()


def test_wrong_cron_bearer_fails_closed_without_running_workers():
    app = _route_app()
    with patch.object(reconciliation, "run_outbox_reconciliation") as run:
        response = app.test_client().get(
            "/api/internal/cron/outbox-reconciliation",
            headers={"Authorization": "Bearer wrong"},
        )

    assert response.status_code == 401
    run.assert_not_called()


def test_valid_cron_bearer_stays_inert_until_cutover_is_enabled():
    app = _route_app(enabled=False)
    with patch.object(reconciliation, "run_outbox_reconciliation") as run:
        response = app.test_client().get(
            "/api/internal/cron/outbox-reconciliation",
            headers={"Authorization": f"Bearer {CRON_SECRET}"},
        )

    assert response.status_code == 503
    assert response.get_json() == {
        "contract_version": "internal.cron.activation.v1",
        "executed": False,
        "reason_code": "vercel_outbox_cron_disabled",
        "status": "disabled",
    }
    assert response.headers["Cache-Control"] == "no-store"
    run.assert_not_called()


def test_valid_cron_bearer_runs_reconciliation():
    app = _route_app()
    with patch.object(
        reconciliation,
        "run_outbox_reconciliation",
        return_value=_successful_reconciliation(),
    ) as run:
        response = app.test_client().get(
            "/api/internal/cron/outbox-reconciliation",
            headers={"Authorization": f"Bearer {CRON_SECRET}"},
        )

    assert response.status_code == 200
    assert response.get_json()["status"] == "completed"
    assert response.headers["Cache-Control"] == "no-store"
    run.assert_called_once_with(app)


def test_degraded_reconciliation_returns_service_unavailable():
    app = _route_app()
    degraded = _successful_reconciliation()
    degraded.update(
        ok=False,
        status="degraded",
        failed_component_count=1,
    )
    with patch.object(
        reconciliation,
        "run_outbox_reconciliation",
        return_value=degraded,
    ):
        response = app.test_client().get(
            "/api/internal/cron/outbox-reconciliation",
            headers={"Authorization": f"Bearer {CRON_SECRET}"},
        )

    assert response.status_code == 503
    assert response.get_json()["status"] == "degraded"


def test_vercel_config_declares_bounded_internal_crons():
    config = json.loads((REPOSITORY_ROOT / "vercel.json").read_text("utf-8"))

    assert config["framework"] == "container"
    assert config["crons"] == [
        {
            "path": "/api/internal/cron/outbox-reconciliation",
            "schedule": "* * * * *",
        },
        {
            "path": "/api/internal/cron/whatsapp-payload-retention",
            "schedule": "43 3 * * *",
        },
        {
            "path": "/api/internal/cron/survey-privacy-retention",
            "schedule": "17 3 * * *",
        },
        {
            "path": "/api/internal/cron/weekly-analytics-report",
            "schedule": "0 * * * 0",
        },
    ]


def test_application_registers_the_internal_cron_blueprint(client):
    client.application.config["CRON_SECRET"] = CRON_SECRET
    client.application.config["VERCEL_OUTBOX_CRON_ENABLED"] = True
    with patch.object(
        reconciliation,
        "run_outbox_reconciliation",
        return_value=_successful_reconciliation(),
    ) as run:
        response = client.get(
            "/api/internal/cron/outbox-reconciliation",
            headers={"Authorization": f"Bearer {CRON_SECRET}"},
        )

    assert response.status_code == 200
    run.assert_called_once_with(client.application)


def test_r2_bucket_bootstrap_route_is_not_registered(client):
    response = client.post(
        "/api/internal/cron/r2-bucket-bootstrap",
        json={
            "mode": "apply",
            "policy_version": "chatboc.r2_direct_upload.v1",
        },
        headers={"Authorization": f"Bearer {CRON_SECRET}"},
    )

    assert response.status_code == 404
