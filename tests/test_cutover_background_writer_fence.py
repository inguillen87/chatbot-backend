from __future__ import annotations

import builtins
import json
import logging
import os
from pathlib import Path
import sys
import threading
from unittest.mock import patch

from flask import Flask
import pytest
import yaml

from cli_commands import register_commands
from cutover_writer_fence import (
    background_writer_fence_report,
    cutover_writer_fence_enabled,
)
from routes.internal_cron import internal_cron_bp
from services import domain_effect_worker
from services import outbox_reconciliation
from services import survey_privacy
from services import survey_response_effect_worker
from services import tasks
from services import whatsapp_inbound_worker
from services import weekly_analytics_reports
from services import analisis_archivo_service
from scripts import run_predeploy_migrations


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CRON_SECRET = "cutover-cron-secret-" + ("x" * 32)


def _fenced_app() -> Flask:
    app = Flask(__name__)
    app.config.update(
        CUTOVER_WRITER_FENCE_ENABLED=True,
        CRON_SECRET=CRON_SECRET,
        VERCEL_OUTBOX_CRON_ENABLED=True,
        VERCEL_MAINTENANCE_CRONS_ENABLED=True,
        VERCEL_WEEKLY_ANALYTICS_CRON_ENABLED=True,
    )
    return app


def test_shared_fence_defaults_off_and_fails_closed_for_present_invalid_value(monkeypatch):
    monkeypatch.delenv("CUTOVER_WRITER_FENCE_ENABLED", raising=False)
    assert cutover_writer_fence_enabled() is False
    assert cutover_writer_fence_enabled({}) is False
    assert cutover_writer_fence_enabled({"CUTOVER_WRITER_FENCE_ENABLED": False}) is False
    assert cutover_writer_fence_enabled({"CUTOVER_WRITER_FENCE_ENABLED": "true"}) is True
    assert cutover_writer_fence_enabled({"CUTOVER_WRITER_FENCE_ENABLED": "false"}) is False
    assert cutover_writer_fence_enabled({"CUTOVER_WRITER_FENCE_ENABLED": "typo"}) is True
    assert cutover_writer_fence_enabled({"CUTOVER_WRITER_FENCE_ENABLED": ""}) is True
    monkeypatch.setenv("CUTOVER_WRITER_FENCE_ENABLED", "1")
    assert cutover_writer_fence_enabled() is True


@pytest.mark.parametrize("flag_value", ["true", "typo", ""])
def test_render_predeploy_fence_skips_migration_and_emits_payload_free_attestation(
    monkeypatch,
    capsys,
    flag_value,
):
    calls = []
    monkeypatch.setenv("CUTOVER_WRITER_FENCE_ENABLED", flag_value)

    assert run_predeploy_migrations.main(run_command=lambda *args, **kwargs: calls.append((args, kwargs))) == 0

    assert calls == []
    report = json.loads(capsys.readouterr().out)
    assert report == background_writer_fence_report("render_predeploy_migration")


def test_render_predeploy_runs_upgrade_only_when_fence_is_explicitly_false(monkeypatch):
    class Completed:
        returncode = 0

    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return Completed()

    monkeypatch.setenv("CUTOVER_WRITER_FENCE_ENABLED", "false")
    command = ["flask", "db", "upgrade"]

    assert run_predeploy_migrations.main(run_command=run, command=command) == 0
    assert calls == [(command, {"check": False})]


def test_render_predeploy_standby_requires_direct_migrations_url(monkeypatch, capsys):
    calls = []
    monkeypatch.setenv("CUTOVER_WRITER_FENCE_ENABLED", "false")

    result = run_predeploy_migrations.main(
        run_command=lambda *args, **kwargs: calls.append((args, kwargs)),
        environ={"CHATBOC_RENDER_STANDBY_MODE": "true"},
    )

    assert result == 2
    assert calls == []
    assert json.loads(capsys.readouterr().out)["reason"] == (
        "migrations_database_url_missing"
    )


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("sqlite:////data/database.db", "database_backend_not_postgresql"),
        (
            "postgresql://user:secret@example.invalid/chatboc?sslmode=require",
            "database_provider_not_neon",
        ),
        (
            "postgresql://user:secret@ep-example-pooler.us-east-2.aws.neon.tech/chatboc?sslmode=require",
            "database_connection_not_direct",
        ),
        (
            "postgresql://user:secret@ep-example.us-east-2.aws.neon.tech/chatboc",
            "database_tls_not_required",
        ),
    ],
)
def test_render_predeploy_standby_rejects_unsafe_database_url_without_leaking_it(
    monkeypatch,
    capsys,
    url,
    reason,
):
    monkeypatch.setenv("CUTOVER_WRITER_FENCE_ENABLED", "false")

    result = run_predeploy_migrations.main(
        environ={
            "CHATBOC_RENDER_STANDBY_MODE": "true",
            "MIGRATIONS_DATABASE_URL": url,
        }
    )

    assert result == 2
    output = capsys.readouterr().out
    assert json.loads(output)["reason"] == reason
    assert "secret" not in output


def test_render_predeploy_standby_uses_apply_migrations_for_direct_neon(
    monkeypatch,
    capsys,
):
    class Completed:
        returncode = 0

    calls = []
    url = (
        "postgresql://user:secret@ep-example.us-east-2.aws.neon.tech/"
        "chatboc?sslmode=require"
    )
    monkeypatch.setenv("CUTOVER_WRITER_FENCE_ENABLED", "false")

    result = run_predeploy_migrations.main(
        run_command=lambda command, **kwargs: (
            calls.append((command, kwargs)) or Completed()
        ),
        environ={
            "CHATBOC_RENDER_STANDBY_MODE": "true",
            "MIGRATIONS_DATABASE_URL": url,
        },
    )

    assert result == 0
    assert calls == [
        ([sys.executable, "-m", "scripts.apply_migrations"], {"check": False})
    ]
    assert "secret" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("path", "service_module"),
    [
        (
            "/api/internal/cron/outbox-reconciliation",
            "services.outbox_reconciliation",
        ),
        (
            "/api/internal/cron/whatsapp-payload-retention",
            "services.whatsapp_inbound_worker",
        ),
        (
            "/api/internal/cron/survey-privacy-retention",
            "services.survey_privacy",
        ),
        (
            "/api/internal/cron/weekly-analytics-report",
            "services.weekly_analytics_reports",
        ),
    ],
)
def test_internal_crons_fence_before_auth_and_service_import(path, service_module):
    app = _fenced_app()
    app.register_blueprint(internal_cron_bp)

    with patch.dict(sys.modules, {service_module: None}):
        response = app.test_client().get(path)

    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Retry-After"] == "60"
    assert response.get_json() == background_writer_fence_report("internal_cron")


def test_fenced_once_workers_return_zero_without_db_queue_or_provider_io():
    app = _fenced_app()
    with (
        patch.object(domain_effect_worker, "resolve_domain_effect_outbox_canaries") as domain_scope,
        patch.object(domain_effect_worker, "dispatch_domain_effect_batch") as domain_dispatch,
        patch.object(
            survey_response_effect_worker,
            "assert_survey_response_effect_worker_schema_current",
        ) as survey_schema,
        patch.object(
            survey_response_effect_worker,
            "summarize_survey_response_effect_worker",
        ) as survey_health,
        patch.object(
            survey_response_effect_worker,
            "dispatch_survey_response_effect_batch",
        ) as survey_dispatch,
        patch.object(
            whatsapp_inbound_worker,
            "summarize_whatsapp_turn_health",
        ) as whatsapp_health,
        patch.object(
            whatsapp_inbound_worker,
            "process_whatsapp_inbound_stream",
        ) as whatsapp_inbound,
        patch.object(
            whatsapp_inbound_worker,
            "dispatch_whatsapp_outbound_attempts",
        ) as whatsapp_outbound,
        patch.object(whatsapp_inbound_worker, "Client") as provider,
    ):
        domain_report = domain_effect_worker.run_domain_effect_worker(app, once=True)
        survey_report = survey_response_effect_worker.run_survey_response_effect_worker(
            app,
            once=True,
        )
        whatsapp_report = whatsapp_inbound_worker.run_whatsapp_durable_worker(
            app,
            once=True,
        )

    for report in (domain_report, survey_report, whatsapp_report):
        assert report["contract_version"] == "cutover.background_writer_fence.v1"
        assert report["status"] == "fenced"
        assert report["executed"] is False
        assert report["cycles"] == 0
        assert report["processed"] == 0
    domain_scope.assert_not_called()
    domain_dispatch.assert_not_called()
    survey_schema.assert_not_called()
    survey_health.assert_not_called()
    survey_dispatch.assert_not_called()
    whatsapp_health.assert_not_called()
    whatsapp_inbound.assert_not_called()
    whatsapp_outbound.assert_not_called()
    provider.assert_not_called()


def test_fenced_broker_entrypoints_return_zero_without_claims_or_wakeups():
    app = _fenced_app()
    with (
        app.app_context(),
        patch.object(domain_effect_worker, "resolve_domain_effect_outbox_canaries") as domain_scope,
        patch.object(
            survey_response_effect_worker,
            "list_due_survey_response_effect_tenant_ids",
        ) as survey_due,
        patch.object(whatsapp_inbound_worker, "_worker_tenant_ids") as whatsapp_scope,
        patch.object(
            whatsapp_inbound_worker,
            "dispatch_next_whatsapp_outbound_attempt",
        ) as whatsapp_dispatch,
        patch.object(
            whatsapp_inbound_worker,
            "scrub_expired_whatsapp_inbound_payloads",
        ) as whatsapp_scrub,
        patch.object(
            whatsapp_inbound_worker.process_whatsapp_inbound_stream_task,
            "apply_async",
        ) as whatsapp_wakeup,
        patch.object(
            domain_effect_worker.dispatch_domain_effects_task,
            "apply_async",
        ) as domain_wakeup,
    ):
        domain = domain_effect_worker.dispatch_domain_effect_batch()
        survey = survey_response_effect_worker.dispatch_survey_response_effect_batch()
        inbound = whatsapp_inbound_worker.process_whatsapp_inbound_stream()
        outbound = whatsapp_inbound_worker.dispatch_whatsapp_outbound_attempts()
        scrub = whatsapp_inbound_worker.run_whatsapp_inbound_payload_scrub()
        assert (
            whatsapp_inbound_worker.enqueue_whatsapp_inbound_stream(
                tenant_id=1,
                stream_key="opaque",
            )
            is False
        )
        assert domain_effect_worker.enqueue_domain_effect_dispatch(tenant_id=1) is False

    assert domain["status"] == "fenced"
    assert survey["status"] == "fenced"
    assert inbound["status"] == "fenced"
    assert outbound["status"] == "fenced"
    assert scrub["status"] == "fenced"
    assert all(item.get("processed", 0) == 0 for item in (domain, survey, inbound, outbound))
    domain_scope.assert_not_called()
    survey_due.assert_not_called()
    whatsapp_scope.assert_not_called()
    whatsapp_dispatch.assert_not_called()
    whatsapp_scrub.assert_not_called()
    whatsapp_wakeup.assert_not_called()
    domain_wakeup.assert_not_called()


@pytest.mark.parametrize(
    "runner",
    [
        domain_effect_worker.run_domain_effect_worker,
        survey_response_effect_worker.run_survey_response_effect_worker,
        whatsapp_inbound_worker.run_whatsapp_durable_worker,
    ],
)
def test_fenced_long_running_workers_wait_for_shutdown_without_polling(runner, caplog):
    class SignalWait:
        def __init__(self):
            self.wait_calls: list[object] = []

        def is_set(self):
            return False

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            return True

    stop = SignalWait()
    caplog.set_level(logging.WARNING)
    report = runner(_fenced_app(), stop_event=stop)

    assert report["status"] == "fenced"
    assert report["cycles"] == 0
    assert stop.wait_calls == [None]
    assert "cutover_writer_fence_active" in caplog.text
    assert f"component={report['component']}" in caplog.text
    assert "status=fenced" in caplog.text


@pytest.mark.parametrize(
    ("module", "argv"),
    [
        (domain_effect_worker, ["domain_effect_worker", "--once"]),
        (
            survey_response_effect_worker,
            ["survey_response_effect_worker", "--once"],
        ),
        (
            whatsapp_inbound_worker,
            ["whatsapp_inbound_worker", "--once"],
        ),
        (survey_privacy, ["survey_privacy"]),
    ],
)
def test_fenced_cli_exits_zero_before_importing_application(
    module,
    argv,
    capsys,
):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "app":
            raise AssertionError("fenced background process must not import app")
        return original_import(name, *args, **kwargs)

    with (
        patch.object(sys, "argv", argv),
        patch.dict(os.environ, {"CUTOVER_WRITER_FENCE_ENABLED": "true"}),
        patch("builtins.__import__", side_effect=guarded_import),
    ):
        assert module.main() == 0

    report = json.loads(capsys.readouterr().out)
    assert report["contract_version"] == "cutover.background_writer_fence.v1"
    assert report["status"] == "fenced"
    assert report["executed"] is False


def test_weekly_analytics_cli_fences_before_service_import():
    app = _fenced_app()
    register_commands(app)
    runner = app.test_cli_runner()

    with patch.dict(sys.modules, {"services.weekly_analytics_reports": None}):
        result = runner.invoke(args=["generate-weekly-reports"])

    assert result.exit_code == 0
    report = json.loads(result.output)
    assert report == {
        **background_writer_fence_report("weekly_analytics_report"),
        "batches": 0,
        "selected": 0,
        "provider_attempts": 0,
        "reports_generated": 0,
    }


def test_manual_survey_effect_cli_fences_before_scope_query_or_service_import():
    app = _fenced_app()
    register_commands(app)
    runner = app.test_cli_runner()

    with patch.dict(sys.modules, {"services.survey_response_effects": None}):
        result = runner.invoke(
            args=["dispatch-survey-response-effects", "--all-tenants"]
        )

    assert result.exit_code == 0
    report = json.loads(result.output)
    assert report["contract_version"] == "cutover.background_writer_fence.v1"
    assert report["status"] == "fenced"
    assert report["batches"] == 0
    assert all(value == 0 for value in report["totals"].values())


def test_retention_and_weekly_services_fence_before_query_or_provider_setup():
    app = _fenced_app()
    with (
        app.app_context(),
        patch.object(survey_privacy, "purge_expired_source_anonymous_responses") as purge,
        patch.object(weekly_analytics_reports, "_bounded_integer") as bounds,
        patch.object(weekly_analytics_reports, "_build_redis_client") as redis,
    ):
        retention = survey_privacy.run_retention_purge_batches()
        weekly_batch = weekly_analytics_reports.run_weekly_analytics_batch(app)
        weekly_drain = weekly_analytics_reports.run_weekly_analytics_drain(app)

    assert retention["status"] == "fenced"
    assert retention["batches"] == 0
    assert retention["deleted"] == 0
    assert weekly_batch["status"] == "fenced"
    assert weekly_batch["provider_attempts"] == 0
    assert weekly_drain["status"] == "fenced"
    assert weekly_drain["batches_run"] == 0
    purge.assert_not_called()
    bounds.assert_not_called()
    redis.assert_not_called()


def test_outbox_coordinator_fences_before_configuration_or_database_lease():
    app = _fenced_app()
    with (
        patch.object(outbox_reconciliation, "_configured_limits") as limits,
        patch.object(
            outbox_reconciliation,
            "_exclusive_reconciliation_lease",
        ) as lease,
    ):
        report = outbox_reconciliation.run_outbox_reconciliation(app)

    assert report["status"] == "fenced"
    assert report["ok"] is False
    assert report["cycles_run"] == 0
    assert report["component_count"] == 0
    assert report["components"] == {}
    limits.assert_not_called()
    lease.assert_not_called()


def test_generic_celery_and_file_analysis_tasks_fence_before_db_or_provider_imports():
    forbidden_modules = {
        "app",
        "models",
        "services.email_service",
        "services.interpretacion_imagen_service",
        "services.interpretacion_service",
        "services.notification_orchestrator",
        "services.scraper_avanzado",
        "services.survey_response_effects",
        "services.v2.sla_service",
        "services.webinfo",
        "twilio.rest",
    }
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in forbidden_modules:
            raise AssertionError(f"fenced task imported writer dependency: {name}")
        return original_import(name, *args, **kwargs)

    with (
        patch.dict(os.environ, {"CUTOVER_WRITER_FENCE_ENABLED": "true"}),
        patch("builtins.__import__", side_effect=guarded_import),
    ):
        reports = [
            tasks.actualizar_info_pyme_desde_web(1),
            tasks.tarea_enviar_campana_email.run(
                empresa_id_solicitante=1,
                lista_ids_clientes_destinatarios=[2],
                asunto="opaque",
                cuerpo_html="opaque",
            ),
            tasks.process_image_for_chat_task.run("opaque", 1, {}, "opaque"),
            tasks.dispatch_notifications_task.run(tenant_id=1),
            tasks.v2_detect_sla_breaches_task.run(tenant_id=1),
            tasks.dispatch_survey_response_effects_task.run(tenant_id=1),
            analisis_archivo_service.tarea_analizar_contenido_archivo.run(1),
        ]

    assert all(report["status"] == "fenced" for report in reports)
    assert all(report["executed"] is False for report in reports)
    assert all(report.get("provider_attempts", 0) == 0 for report in reports)


def test_generic_enqueue_helpers_do_not_touch_broker_while_fenced():
    with (
        patch.dict(os.environ, {"CUTOVER_WRITER_FENCE_ENABLED": "true"}),
        patch.object(tasks.tarea_enviar_campana_email, "delay") as campaign,
        patch.object(tasks.process_image_for_chat_task, "delay") as image,
        patch.object(tasks.dispatch_notifications_task, "delay") as notifications,
        patch.object(tasks.v2_detect_sla_breaches_task, "delay") as sla,
        patch.object(tasks.dispatch_survey_response_effects_task, "delay") as survey,
        patch.object(
            analisis_archivo_service.tarea_analizar_contenido_archivo,
            "delay",
        ) as analysis,
    ):
        assert tasks.enqueue_campaign_email(opaque=True) is False
        assert tasks.enqueue_image_for_chat("opaque") is False
        assert tasks.enqueue_notification_dispatch(1) is False
        assert tasks.enqueue_v2_sla_detection(1) is False
        assert tasks.enqueue_survey_response_effect_dispatch(1) is False
        assert analisis_archivo_service.enqueue_file_content_analysis(1) is False

    for broker_call in (campaign, image, notifications, sla, survey, analysis):
        broker_call.assert_not_called()


def test_file_upload_producer_fences_before_importing_task_or_touching_broker():
    from routes import archivos

    with (
        patch.dict(os.environ, {"CUTOVER_WRITER_FENCE_ENABLED": "true"}),
        patch.dict(sys.modules, {"services.analisis_archivo_service": None}),
    ):
        assert archivos._enqueue_file_content_analysis(1) is False


def test_render_propagates_shared_fence_to_every_background_writer():
    services = {
        item["name"]: item
        for item in yaml.safe_load(
            (REPOSITORY_ROOT / "render.yaml").read_text(encoding="utf-8")
        )["services"]
    }
    web_env = {
        item["key"]: item for item in services["chatboc-backend"]["envVars"]
    }
    assert web_env["CUTOVER_WRITER_FENCE_ENABLED"]["value"] == "false"

    for service_name in (
        "chatboc-whatsapp-durable",
        "chatboc-whatsapp-payload-retention",
        "chatboc-domain-effects",
        "chatboc-survey-effects",
        "chatboc-survey-retention",
        "weekly-analytics-report",
    ):
        env = {item["key"]: item for item in services[service_name]["envVars"]}
        assert env["CUTOVER_WRITER_FENCE_ENABLED"]["fromService"] == {
            "type": "web",
            "name": "chatboc-backend",
            "envVarKey": "CUTOVER_WRITER_FENCE_ENABLED",
        }
