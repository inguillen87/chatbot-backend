from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

import pytest
from flask import Flask

from config import Config
from routes.internal_cron import internal_cron_bp


CRON_SECRET = "maintenance-cron-secret-" + ("x" * 32)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
JOBS = (
    (
        "/api/internal/cron/whatsapp-payload-retention",
        "services.whatsapp_inbound_worker",
    ),
    (
        "/api/internal/cron/survey-privacy-retention",
        "services.survey_privacy",
    ),
)


def _app(*, secret: str = CRON_SECRET, enabled: bool = True) -> Flask:
    app = Flask(__name__)
    app.config.update(
        CRON_SECRET=secret,
        VERCEL_MAINTENANCE_CRONS_ENABLED=enabled,
    )
    app.register_blueprint(internal_cron_bp)
    return app


def _authorized_get(app: Flask, path: str):
    return app.test_client().get(
        path,
        headers={"Authorization": f"Bearer {CRON_SECRET}"},
    )


def _fake_module(module_name: str, function_name: str, result: dict):
    module = ModuleType(module_name)
    run = Mock(return_value=result)
    setattr(module, function_name, run)
    return module, run


def test_maintenance_cutover_flag_defaults_fail_closed():
    assert Config.VERCEL_MAINTENANCE_CRONS_ENABLED is False
    env_example = (REPOSITORY_ROOT / ".env.example").read_text("utf-8").splitlines()
    assert "VERCEL_MAINTENANCE_CRONS_ENABLED=false" in env_example


@pytest.mark.parametrize(("path", "service_module"), JOBS)
def test_missing_authorization_rejects_before_service_import(path, service_module):
    app = _app()
    with patch.dict(sys.modules, {service_module: None}):
        response = app.test_client().get(path)

    assert response.status_code == 401
    assert response.get_json() == {
        "contract_version": "internal.cron.authorization.v1",
        "status": "unauthorized",
    }
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(("path", "service_module"), JOBS)
def test_wrong_authorization_rejects_before_service_import(path, service_module):
    app = _app()
    with patch.dict(sys.modules, {service_module: None}):
        response = app.test_client().get(
            path,
            headers={"Authorization": "Bearer wrong"},
        )

    assert response.status_code == 401


@pytest.mark.parametrize(("path", "service_module"), JOBS)
def test_valid_bearer_remains_inert_before_maintenance_cutover(
    path,
    service_module,
):
    app = _app(enabled=False)
    with patch.dict(sys.modules, {service_module: None}):
        response = _authorized_get(app, path)

    assert response.status_code == 503
    assert response.get_json() == {
        "contract_version": "internal.cron.activation.v1",
        "executed": False,
        "reason_code": "vercel_maintenance_crons_disabled",
        "status": "disabled",
    }
    assert response.headers["Cache-Control"] == "no-store"


def test_whatsapp_retention_invokes_one_bounded_existing_cycle_and_redacts_report():
    private_detail = "postgresql://private:secret@database/chatboc"
    module, run = _fake_module(
        "services.whatsapp_inbound_worker",
        "run_whatsapp_inbound_payload_scrub",
        {
            "contract_version": "whatsapp.inbound_payload_scrub_batch.v1",
            "status": "completed",
            "tenant_count": 2,
            "selected": 7,
            "scrubbed": 7,
            "completed_scrubbed": 5,
            "dead_scrubbed": 2,
            "batch_limit": 200,
            "batch_remaining": 193,
            "tenant_ids": [22, 31],
            "private_detail": private_detail,
        },
    )
    app = _app()

    with patch.dict(sys.modules, {module.__name__: module}):
        response = _authorized_get(app, "/api/internal/cron/whatsapp-payload-retention")

    assert response.status_code == 200
    assert response.get_json() == {
        "contract_version": "internal.maintenance_cron.v1",
        "executed": True,
        "job": "whatsapp_payload_retention",
        "status": "completed",
        "tenant_count": 2,
        "selected": 7,
        "scrubbed": 7,
        "completed_scrubbed": 5,
        "dead_scrubbed": 2,
        "batch_limit": 200,
        "batch_remaining": 193,
    }
    assert private_detail not in response.get_data(as_text=True)
    run.assert_called_once_with()


def test_whatsapp_job_level_safety_gate_is_a_successful_inert_cycle():
    module, run = _fake_module(
        "services.whatsapp_inbound_worker",
        "run_whatsapp_inbound_payload_scrub",
        {
            "contract_version": "whatsapp.inbound_payload_scrub_batch.v1",
            "status": "legal_hold",
            "tenant_count": 0,
            "selected": 0,
            "scrubbed": 0,
            "completed_scrubbed": 0,
            "dead_scrubbed": 0,
        },
    )
    app = _app()

    with patch.dict(sys.modules, {module.__name__: module}):
        response = _authorized_get(app, "/api/internal/cron/whatsapp-payload-retention")

    assert response.status_code == 200
    assert response.get_json()["status"] == "legal_hold"
    assert response.get_json()["executed"] is False
    run.assert_called_once_with()


def test_survey_privacy_invokes_exact_render_bounds_and_redacts_report():
    private_detail = "survey-person@example.test"
    module, run = _fake_module(
        "services.survey_privacy",
        "run_retention_purge_batches",
        {
            "contract_version": "surveys.privacy_retention_purge.v1",
            "dry_run": False,
            "batches": 3,
            "eligible": 410,
            "deleted": 410,
            "exhausted_batch_budget": False,
            "cutoff_at": "2026-08-25T00:00:00+00:00",
            "private_detail": private_detail,
        },
    )
    app = _app()

    with patch.dict(sys.modules, {module.__name__: module}):
        response = _authorized_get(app, "/api/internal/cron/survey-privacy-retention")

    assert response.status_code == 200
    assert response.get_json() == {
        "contract_version": "internal.maintenance_cron.v1",
        "executed": True,
        "job": "survey_privacy_retention",
        "status": "completed",
        "batches": 3,
        "eligible": 410,
        "deleted": 410,
        "exhausted_batch_budget": False,
    }
    assert private_detail not in response.get_data(as_text=True)
    run.assert_called_once_with(batch_size=200, max_batches=20, dry_run=False)


def test_survey_privacy_reports_exhausted_bound_as_service_unavailable():
    module, run = _fake_module(
        "services.survey_privacy",
        "run_retention_purge_batches",
        {
            "contract_version": "surveys.privacy_retention_purge.v1",
            "dry_run": False,
            "batches": 20,
            "eligible": 4000,
            "deleted": 4000,
            "exhausted_batch_budget": True,
        },
    )
    app = _app()

    with patch.dict(sys.modules, {module.__name__: module}):
        response = _authorized_get(app, "/api/internal/cron/survey-privacy-retention")

    assert response.status_code == 503
    assert response.get_json()["status"] == "batch_budget_exhausted"
    run.assert_called_once_with(batch_size=200, max_batches=20, dry_run=False)


@pytest.mark.parametrize(
    ("path", "module_name", "function_name"),
    (
        (
            "/api/internal/cron/whatsapp-payload-retention",
            "services.whatsapp_inbound_worker",
            "run_whatsapp_inbound_payload_scrub",
        ),
        (
            "/api/internal/cron/survey-privacy-retention",
            "services.survey_privacy",
            "run_retention_purge_batches",
        ),
    ),
)
def test_maintenance_failure_response_does_not_expose_exception_detail(
    path,
    module_name,
    function_name,
    caplog,
):
    private_detail = "provider-token-must-not-leak"
    module = ModuleType(module_name)
    run = Mock(side_effect=RuntimeError(private_detail))
    setattr(module, function_name, run)
    app = _app()

    with patch.dict(sys.modules, {module_name: module}):
        response = _authorized_get(app, path)

    assert response.status_code == 503
    assert response.get_json()["status"] == "failed"
    assert response.get_json()["error_type"] == "RuntimeError"
    assert private_detail not in response.get_data(as_text=True)
    assert private_detail not in caplog.text
    run.assert_called_once()
