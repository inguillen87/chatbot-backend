from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from flask import Flask
import pytest

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


@contextmanager
def _lease(acquired: bool = True):
    yield acquired


def _whatsapp_report(
    *,
    processed: int = 0,
    completed: int = 0,
    accepted: int = 0,
    retry_wait: int = 0,
    unknown: int = 0,
    dead: int = 0,
    cycle_failures: int = 0,
) -> dict:
    inbound_processed = completed
    outbound_processed = processed - inbound_processed
    return {
        "contract_version": "whatsapp.durable_worker_run.v1",
        "status": "completed",
        "mode": "queue",
        "cycles": 1,
        "processed": processed,
        "inbound_processed": inbound_processed,
        "inbound_completed": completed,
        "outbound_processed": outbound_processed,
        "outbound_accepted": accepted,
        "retry_wait": retry_wait,
        "unknown": unknown,
        "dead": dead,
        "cycle_failures": cycle_failures,
    }


def _whatsapp_standby_report() -> dict:
    report = _whatsapp_report()
    report.update(
        contract_version="whatsapp.durable_worker_standby.v1",
        status="standby",
        mode="legacy",
        cycles=0,
    )
    return report


def _domain_report(
    *,
    processed: int = 0,
    skipped: int = 0,
    retry_wait: int = 0,
    unknown: int = 0,
    dead: int = 0,
    recovered_unknown: int = 0,
    recovered_retry_wait: int = 0,
    recovered_dead: int = 0,
    cycle_failures: int = 0,
) -> dict:
    return {
        "contract_version": "domain.effect_worker_run.v1",
        "cycles": 1,
        "processed": processed,
        "succeeded": max(0, processed - skipped - retry_wait - unknown - dead),
        "skipped": skipped,
        "retry_wait": retry_wait,
        "unknown": unknown,
        "dead": dead,
        "recovered_unknown": recovered_unknown,
        "recovered_retry_wait": recovered_retry_wait,
        "recovered_dead": recovered_dead,
        "cycle_failures": cycle_failures,
    }


def _survey_report(
    *,
    processed: int = 0,
    skipped: int = 0,
    retry_wait: int = 0,
    dead: int = 0,
    fenced: int = 0,
    cycle_failures: int = 0,
) -> dict:
    return {
        "contract_version": "surveys.response_effect_worker_run.v1",
        "cycles": 1,
        "claimed": processed + fenced,
        "processed": processed,
        "succeeded": max(0, processed - skipped - retry_wait - dead),
        "skipped": skipped,
        "retry_wait": retry_wait,
        "dead": dead,
        "fenced": fenced,
        "cycle_failures": cycle_failures,
    }


def _successful_reconciliation() -> dict:
    return {
        "contract_version": "outbox.reconciliation.v2",
        "ok": True,
        "status": "completed",
        "idempotency": "database_outbox_leases_and_fencing",
        "exclusive_lease": "postgresql_transaction_advisory_lock",
        "elapsed_ms": 1,
        "limits": {},
        "component_count": 3,
        "failed_component_count": 0,
        "attention_component_count": 0,
        "cycles_run": 1,
        "has_more": False,
        "components": {
            "whatsapp": {
                "status": "idle",
                "attempts": 1,
                "cycle_failures": 0,
            },
            "domain_effects": {
                "status": "idle",
                "attempts": 1,
                "cycle_failures": 0,
            },
            "survey_effects": {
                "status": "idle",
                "attempts": 1,
                "cycle_failures": 0,
            },
        },
    }


def _run_with_workers(
    app: Flask,
    *,
    whatsapp: object,
    domain: object,
    survey: object,
    lease_acquired: bool = True,
    **kwargs,
) -> tuple[dict, Mock, Mock, Mock]:
    def mock_kwargs(value: object) -> dict:
        if isinstance(value, BaseException):
            return {"side_effect": value}
        if callable(value):
            return {"side_effect": value}
        return {"return_value": value}

    with (
        patch.object(
            reconciliation,
            "_exclusive_reconciliation_lease",
            side_effect=lambda _app: _lease(lease_acquired),
        ),
        patch.object(
            reconciliation,
            "run_whatsapp_durable_worker",
            **mock_kwargs(whatsapp),
        ) as whatsapp_run,
        patch.object(
            reconciliation,
            "run_domain_effect_worker",
            **mock_kwargs(domain),
        ) as domain_run,
        patch.object(
            reconciliation,
            "run_survey_response_effect_worker",
            **mock_kwargs(survey),
        ) as survey_run,
    ):
        report = reconciliation.run_outbox_reconciliation(
            app,
            wall_clock=lambda: 0,
            **kwargs,
        )
    return report, whatsapp_run, domain_run, survey_run


def test_reconciliation_runs_every_worker_once_and_preserves_legacy_standby():
    app = Flask(__name__)
    report, whatsapp, domain, survey = _run_with_workers(
        app,
        whatsapp=_whatsapp_standby_report(),
        domain=_domain_report(),
        survey=_survey_report(),
    )

    whatsapp.assert_called_once_with(
        app,
        once=True,
        standby_when_legacy=True,
        inbound_limit=1,
        outbound_limit=2,
        deadline_monotonic=ANY,
        clock=ANY,
    )
    domain.assert_called_once_with(
        app,
        once=True,
        batch_limit=10,
        deadline_monotonic=ANY,
        clock=ANY,
    )
    survey.assert_called_once_with(
        app,
        once=True,
        batch_limit=25,
        deadline_monotonic=ANY,
        clock=ANY,
        require_current_schema=True,
        require_shared_realtime=True,
    )
    assert report["ok"] is True
    assert report["status"] == "completed"
    assert report["components"]["whatsapp"]["status"] == "standby"
    assert report["components"]["whatsapp"]["lease_seconds"] == 180
    assert report["components"]["domain_effects"]["per_cycle_batch_limit"] == 10
    assert report["components"]["survey_effects"]["max_total_per_invocation"] == 100


def test_reconciliation_drains_progress_then_stops_on_idle_cycle():
    app = Flask(__name__)
    domain_reports = iter([_domain_report(processed=2), _domain_report()])
    report, whatsapp, domain, survey = _run_with_workers(
        app,
        whatsapp=_whatsapp_report(),
        domain=lambda *_args, **_kwargs: next(domain_reports),
        survey=_survey_report(),
    )

    assert report["status"] == "completed"
    assert report["cycles_run"] == 2
    assert report["has_more"] is False
    assert report["components"]["domain_effects"]["processed"] == 2
    assert whatsapp.call_count == domain.call_count == survey.call_count == 2


def test_reconciliation_stops_at_drain_limit_with_safe_progress():
    app = Flask(__name__)
    app.config["VERCEL_OUTBOX_CRON_MAX_CYCLES"] = 2
    report, _whatsapp, _domain, _survey = _run_with_workers(
        app,
        whatsapp=_whatsapp_report(processed=1, completed=1),
        domain=_domain_report(processed=1),
        survey=_survey_report(processed=1),
    )

    assert report["ok"] is True
    assert report["status"] == "drain_limit_reached"
    assert report["cycles_run"] == 2
    assert report["has_more"] is True


def test_reconciliation_deadline_defers_unstarted_pipelines_and_rotates_fairly():
    app = Flask(__name__)
    # started, cycle gate, first-pipeline gate, second-pipeline gate, final
    clock = Mock(side_effect=[0.0, 0.0, 0.0, 46.0, 46.0])
    report, whatsapp, domain, survey = _run_with_workers(
        app,
        whatsapp=_whatsapp_report(),
        domain=_domain_report(),
        survey=_survey_report(),
        clock=clock,
    )

    assert report["status"] == "time_budget_reached"
    assert report["has_more"] is True
    whatsapp.assert_called_once()
    domain.assert_not_called()
    survey.assert_not_called()
    assert report["components"]["domain_effects"]["status"] == (
        "deferred_time_budget"
    )
    assert report["components"]["survey_effects"]["status"] == (
        "deferred_time_budget"
    )


def test_reconciliation_contended_invocation_is_a_safe_noop():
    app = Flask(__name__)
    report, whatsapp, domain, survey = _run_with_workers(
        app,
        whatsapp=_whatsapp_report(),
        domain=_domain_report(),
        survey=_survey_report(),
        lease_acquired=False,
    )

    assert report["ok"] is True
    assert report["status"] == "contended"
    assert report["cycles_run"] == 0
    assert report["has_more"] is True
    whatsapp.assert_not_called()
    domain.assert_not_called()
    survey.assert_not_called()


def test_reconciliation_isolates_failures_and_redacts_exception_detail(caplog):
    private_detail = "postgresql://admin:secret@private-db/chatboc"
    app = Flask(__name__)
    report, whatsapp, domain, survey = _run_with_workers(
        app,
        whatsapp=_whatsapp_report(),
        domain=RuntimeError(private_detail),
        survey=_survey_report(),
    )

    whatsapp.assert_called_once()
    domain.assert_called_once()
    survey.assert_called_once()
    assert report["ok"] is False
    assert report["status"] == "degraded"
    assert report["failed_component_count"] == 1
    assert report["components"]["domain_effects"]["status"] == "failed"
    assert report["components"]["domain_effects"]["error_type"] == "RuntimeError"
    assert private_detail not in repr(report)
    assert private_detail not in caplog.text


def test_worker_cycle_failure_is_counted_once_and_marks_degraded():
    app = Flask(__name__)
    report, _whatsapp, _domain, _survey = _run_with_workers(
        app,
        whatsapp=_whatsapp_report(),
        domain=_domain_report(cycle_failures=1),
        survey=_survey_report(),
    )

    component = report["components"]["domain_effects"]
    assert report["status"] == "degraded"
    assert component["attempts"] == 1
    assert component["cycle_failures"] == 1


def test_terminal_or_uncertain_effect_requires_operator_attention():
    app = Flask(__name__)
    report, _whatsapp, _domain, _survey = _run_with_workers(
        app,
        whatsapp=_whatsapp_report(),
        domain=_domain_report(processed=1, unknown=1),
        survey=_survey_report(),
    )

    assert report["ok"] is False
    assert report["status"] == "degraded"
    assert report["attention_component_count"] == 1
    assert report["components"]["domain_effects"]["status"] == (
        "attention_required"
    )


def test_attention_has_precedence_over_retry_wait_in_same_pipeline():
    app = Flask(__name__)
    report, _whatsapp, _domain, _survey = _run_with_workers(
        app,
        whatsapp=_whatsapp_report(),
        domain=_domain_report(processed=2, retry_wait=1, unknown=1),
        survey=_survey_report(),
    )

    assert report["status"] == "degraded"
    assert report["failed_component_count"] == 0
    assert report["attention_component_count"] == 1
    assert report["components"]["domain_effects"]["status"] == (
        "attention_required"
    )


def test_recovered_ambiguous_domain_effect_requires_attention():
    app = Flask(__name__)
    report, _whatsapp, _domain, _survey = _run_with_workers(
        app,
        whatsapp=_whatsapp_report(),
        domain=_domain_report(recovered_unknown=1),
        survey=_survey_report(),
    )

    assert report["status"] == "degraded"
    assert report["attention_component_count"] == 1
    assert report["components"]["domain_effects"]["recovered_unknown"] == 1


def test_survey_fence_marks_reconciliation_degraded():
    app = Flask(__name__)
    report, _whatsapp, _domain, _survey = _run_with_workers(
        app,
        whatsapp=_whatsapp_report(),
        domain=_domain_report(),
        survey=_survey_report(fenced=1),
    )

    assert report["status"] == "degraded"
    assert report["failed_component_count"] == 1
    assert report["components"]["survey_effects"]["status"] == "fenced"


def test_retry_wait_marks_reconciliation_degraded_for_operator_visibility():
    app = Flask(__name__)
    report, _whatsapp, _domain, _survey = _run_with_workers(
        app,
        whatsapp=_whatsapp_report(processed=1, retry_wait=1),
        domain=_domain_report(),
        survey=_survey_report(),
    )

    assert report["ok"] is False
    assert report["status"] == "degraded"
    assert report["failed_component_count"] == 1
    assert report["components"]["whatsapp"]["status"] == "retry_wait"


def test_worker_counters_require_exact_non_boolean_integers():
    for invalid in ("1", 1.5, True):
        app = Flask(__name__)
        whatsapp = _whatsapp_report()
        whatsapp["processed"] = invalid
        report, _whatsapp, _domain, _survey = _run_with_workers(
            app,
            whatsapp=whatsapp,
            domain=_domain_report(),
            survey=_survey_report(),
        )

        assert report["status"] == "degraded"
        assert report["components"]["whatsapp"]["status"] == "failed"


def test_invalid_or_nonfinite_bounds_fail_before_lease_or_workers():
    for invalid in (True, "invalid", "nan", float("inf")):
        app = Flask(__name__)
        app.config["VERCEL_OUTBOX_CRON_TIME_BUDGET_SECONDS"] = invalid
        with (
            patch.object(
                reconciliation,
                "_exclusive_reconciliation_lease",
            ) as lease,
            patch.object(
                reconciliation,
                "run_whatsapp_durable_worker",
            ) as whatsapp,
        ):
            report = reconciliation.run_outbox_reconciliation(app)

        assert report["ok"] is False
        assert report["status"] == "configuration_unavailable"
        assert str(invalid) not in repr(report)
        lease.assert_not_called()
        whatsapp.assert_not_called()

    fractional = Flask(__name__)
    fractional.config["VERCEL_OUTBOX_CRON_MAX_CYCLES"] = 1.5
    report = reconciliation.run_outbox_reconciliation(fractional)
    assert report["status"] == "configuration_unavailable"


def test_incomplete_worker_contract_fails_closed():
    app = Flask(__name__)
    report, _whatsapp, _domain, _survey = _run_with_workers(
        app,
        whatsapp={
            "contract_version": "whatsapp.durable_worker_run.v1",
            "status": "completed",
        },
        domain=_domain_report(),
        survey=_survey_report(),
    )

    assert report["status"] == "degraded"
    assert report["components"]["whatsapp"]["error_type"] == (
        "OutboxReconciliationReportError"
    )


def test_postgres_transaction_advisory_lease_is_nonblocking_and_released():
    transaction = Mock(is_active=True)
    scalar = Mock()
    scalar.scalar_one.return_value = True
    connection = Mock()
    connection.begin.return_value = transaction
    connection.execute.return_value = scalar
    engine = Mock()
    engine.dialect.name = "postgresql"
    engine.connect.return_value = connection
    fake_db = SimpleNamespace(engine=engine)
    app = Flask(__name__)

    with patch.object(reconciliation, "db", fake_db):
        with reconciliation._exclusive_reconciliation_lease(app) as acquired:
            assert acquired is True

    statement = str(connection.execute.call_args.args[0])
    assert "pg_try_advisory_xact_lock" in statement
    transaction.rollback.assert_called_once_with()
    connection.close.assert_called_once_with()


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


def test_time_budget_can_expire_after_lease_before_first_cycle():
    app = _route_app()
    report = _successful_reconciliation()
    report.update(
        status="time_budget_reached",
        cycles_run=0,
        has_more=True,
    )
    for component in report["components"].values():
        component.update(status="not_run", attempts=0)
    with patch.object(
        reconciliation,
        "run_outbox_reconciliation",
        return_value=report,
    ):
        response = app.test_client().get(
            "/api/internal/cron/outbox-reconciliation",
            headers={"Authorization": f"Bearer {CRON_SECRET}"},
        )

    assert response.status_code == 200
    assert response.get_json()["status"] == "time_budget_reached"


def test_degraded_reconciliation_returns_service_unavailable():
    app = _route_app()
    degraded = _successful_reconciliation()
    degraded.update(
        ok=False,
        status="degraded",
        failed_component_count=1,
        has_more=True,
    )
    degraded["components"]["domain_effects"].update(
        status="failed",
        cycle_failures=1,
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


def test_malformed_reconciliation_report_returns_redacted_failure():
    app = _route_app()
    with patch.object(
        reconciliation,
        "run_outbox_reconciliation",
        return_value={"ok": True, "status": "completed"},
    ):
        response = app.test_client().get(
            "/api/internal/cron/outbox-reconciliation",
            headers={"Authorization": f"Bearer {CRON_SECRET}"},
        )

    assert response.status_code == 503
    assert response.get_json() == {
        "contract_version": "internal.outbox_reconciliation.v1",
        "executed": True,
        "ok": False,
        "status": "failed",
    }


@pytest.mark.parametrize(
    "mutation",
    (
        lambda report: report.update(has_more=True),
        lambda report: report.update(failed_component_count=1),
        lambda report: report["components"].update(unexpected={}),
        lambda report: report["components"]["whatsapp"].update(attempts="1"),
    ),
)
def test_success_status_with_incoherent_shape_fails_closed(mutation):
    app = _route_app()
    malformed = _successful_reconciliation()
    mutation(malformed)
    with patch.object(
        reconciliation,
        "run_outbox_reconciliation",
        return_value=malformed,
    ):
        response = app.test_client().get(
            "/api/internal/cron/outbox-reconciliation",
            headers={"Authorization": f"Bearer {CRON_SECRET}"},
        )

    assert response.status_code == 503
    assert response.get_json()["status"] == "failed"


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
