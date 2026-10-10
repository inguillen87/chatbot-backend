from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from flask import Flask
import pytest
from sqlalchemy import text

from database import db
from models import (
    EncEncuesta,
    EncRespuesta,
    SurveyResponseEffect,
    TenantProfile,
    User,
)
from services.survey_response_effect_worker import (
    SURVEY_RESPONSE_EFFECT_SWEEP_TASK_NAME,
    SurveyResponseEffectWorkerConfigurationError,
    assert_survey_response_effect_worker_schema_current,
    dispatch_survey_response_effect_batch,
    dispatch_survey_response_effect_batch_task,
    run_survey_response_effect_worker,
)
from services.survey_response_effects import (
    STATUS_DEAD,
    STATUS_PROCESSING,
    STATUS_RETRY_WAIT,
    STATUS_SUCCEEDED,
    stage_survey_response_effects,
)


def _make_app(database_path: Path | str = ":memory:") -> Flask:
    app = Flask(__name__)
    database_uri = (
        "sqlite:///:memory:"
        if str(database_path) == ":memory:"
        else f"sqlite:///{Path(database_path).as_posix()}"
    )
    app.config.update(
        TESTING=True,
        SECRET_KEY="survey-worker-test",
        SQLALCHEMY_DATABASE_URI=database_uri,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SURVEY_RESPONSE_EFFECT_LEASE_SECONDS=120,
        SURVEY_RESPONSE_EFFECT_WORKER_BATCH_SIZE=50,
        SURVEY_RESPONSE_EFFECT_WORKER_MAX_TENANTS_PER_CYCLE=50,
        SURVEY_RESPONSE_EFFECT_WORKER_POLL_SECONDS=0.05,
    )
    db.init_app(app)
    return app


def _dispatcher_result(*, claimed: int = 1) -> dict:
    return {
        "contract_version": "surveys.response_effect.v1",
        "claimed": claimed,
        "processed": claimed,
        "succeeded": claimed,
        "skipped": 0,
        "retry_wait": 0,
        "dead": 0,
        "fenced": 0,
    }


def _seed_analytics_effect(database_path: Path, *, status: str) -> int:
    app = _make_app(database_path)
    with app.app_context():
        db.create_all()
        owner = User(name="Survey owner", email="owner@test.com", rol="admin")
        owner.set_password(secrets.token_urlsafe(24))
        voter = User(name="Survey voter", email="voter@test.com", rol="usuario")
        voter.set_password(secrets.token_urlsafe(24))
        db.session.add_all([owner, voter])
        db.session.flush()
        tenant = TenantProfile(
            slug="survey-worker",
            nombre="Survey worker",
            tipo="municipio",
            municipio_id=owner.id,
        )
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id = tenant.id
        voter.tenant_id = tenant.id
        survey = EncEncuesta(
            tenant_id=tenant.id,
            slug="worker-restart-survey",
            titulo="Worker restart survey",
            estado="publicada",
            puntos_recompensa=0,
            mostrar_resultados_envivo=False,
            created_by=owner.id,
        )
        db.session.add(survey)
        db.session.flush()
        response = EncRespuesta(
            encuesta_id=survey.id,
            tenant_id=tenant.id,
            user_id=voter.id,
            canal="portal",
            huella_unica="worker-test-fingerprint",
        )
        db.session.add(response)
        db.session.flush()
        effects = stage_survey_response_effects(
            survey,
            response,
            slug_publico="worker-public-token",
            respuestas_payload=[{"pregunta_id": 1, "texto_libre": "private"}],
            authenticated_user=voter,
            grant_reward=False,
            emit_realtime_update=False,
        )
        effect = effects[0]
        now = datetime.now(timezone.utc)
        effect.status = status
        effect.attempt_count = 1
        effect.available_at = now - timedelta(seconds=1)
        if status == STATUS_PROCESSING:
            effect.lease_token = "abandoned-process"
            effect.leased_until = now - timedelta(seconds=1)
        elif status == STATUS_DEAD:
            effect.attempt_count = effect.max_attempts
            effect.processed_at = now
        db.session.commit()
        effect_id = int(effect.id)
        db.session.remove()
        db.engine.dispose()
    return effect_id


def _confirmed_analytics_event(*_args, **kwargs):
    return SimpleNamespace(
        id=kwargs["event_id"],
        tenant_id=kwargs["tenant_id"],
        event_name=kwargs["event_name"],
        entity_ref=kwargs["entity_ref"],
        metadata_payload=kwargs["payload"],
    )


def test_worker_schema_gate_accepts_exact_repository_head():
    app = _make_app()
    app.config.update(
        TESTING=False,
        CHATBOC_PROCESS_ROLE="survey-effect-worker",
    )
    with app.app_context(), patch(
        "services.survey_response_effect_worker._repository_schema_heads",
        return_value=frozenset({"release-head"}),
    ), patch(
        "services.survey_response_effect_worker._database_schema_heads",
        return_value=frozenset({"release-head"}),
    ):
        assert_survey_response_effect_worker_schema_current()


def test_worker_schema_gate_rejects_database_before_release_head():
    app = _make_app()
    app.config.update(
        TESTING=False,
        CHATBOC_PROCESS_ROLE="survey-effect-worker",
    )
    with app.app_context(), patch(
        "services.survey_response_effect_worker._repository_schema_heads",
        return_value=frozenset({"release-head"}),
    ), patch(
        "services.survey_response_effect_worker._database_schema_heads",
        return_value=frozenset({"previous-head"}),
    ), pytest.raises(
        SurveyResponseEffectWorkerConfigurationError,
        match="survey_response_effect_worker_schema_not_current",
    ):
        assert_survey_response_effect_worker_schema_current()


def test_worker_schema_gate_does_not_query_database_for_other_process_roles():
    app = _make_app()
    app.config.update(
        TESTING=False,
        CHATBOC_PROCESS_ROLE="web",
    )
    with app.app_context(), patch(
        "services.survey_response_effect_worker._repository_schema_heads"
    ) as repository_heads, patch(
        "services.survey_response_effect_worker._database_schema_heads"
    ) as database_heads:
        assert_survey_response_effect_worker_schema_current()

    repository_heads.assert_not_called()
    database_heads.assert_not_called()


def test_explicit_schema_gate_checks_web_role_for_serverless_cron():
    app = _make_app()
    app.config.update(
        TESTING=False,
        CHATBOC_PROCESS_ROLE="web",
    )
    with app.app_context(), patch(
        "services.survey_response_effect_worker._repository_schema_heads",
        return_value=frozenset({"release-head"}),
    ), patch(
        "services.survey_response_effect_worker._database_schema_heads",
        return_value=frozenset({"previous-head"}),
    ), pytest.raises(
        SurveyResponseEffectWorkerConfigurationError,
        match="survey_response_effect_worker_schema_not_current",
    ):
        assert_survey_response_effect_worker_schema_current(required=True)


def test_worker_rotates_tenants_and_divides_each_bounded_batch_fairly():
    app = _make_app()
    app.config.update(
        SURVEY_RESPONSE_EFFECT_WORKER_BATCH_SIZE=2,
        SURVEY_RESPONSE_EFFECT_WORKER_MAX_TENANTS_PER_CYCLE=3,
    )
    with app.app_context(), patch(
        "services.survey_response_effect_worker."
        "list_due_survey_response_effect_tenant_ids",
        return_value=(1, 2, 3),
    ), patch(
        "services.survey_response_effect_worker.dispatch_survey_response_effects",
        side_effect=[
            _dispatcher_result(),
            _dispatcher_result(),
            _dispatcher_result(),
            _dispatcher_result(),
        ],
    ) as dispatch:
        first = dispatch_survey_response_effect_batch()
        second = dispatch_survey_response_effect_batch()

    assert [item["tenant_id"] for item in first["tenants"]] == [1, 2]
    assert [item["tenant_id"] for item in second["tenants"]] == [3, 1]
    assert dispatch.call_args_list == [
        call(tenant_id=1, limit=1, lease_seconds=120),
        call(tenant_id=2, limit=1, lease_seconds=120),
        call(tenant_id=3, limit=1, lease_seconds=120),
        call(tenant_id=1, limit=1, lease_seconds=120),
    ]


def test_deadline_stops_claiming_new_survey_tenants_and_passes_effect_guard():
    app = _make_app()
    clock = Mock(side_effect=[0.0, 2.0])
    with app.app_context(), patch(
        "services.survey_response_effect_worker."
        "list_due_survey_response_effect_tenant_ids",
        return_value=(1, 2),
    ), patch(
        "services.survey_response_effect_worker.dispatch_survey_response_effects",
        return_value=_dispatcher_result(),
    ) as dispatch:
        report = dispatch_survey_response_effect_batch(
            limit=2,
            deadline_monotonic=1.0,
            clock=clock,
            require_shared_realtime=True,
        )

    dispatch.assert_called_once()
    assert callable(dispatch.call_args.kwargs["should_continue"])
    assert dispatch.call_args.kwargs["require_shared_realtime"] is True
    assert report["processed"] == 1
    assert [item["tenant_id"] for item in report["tenants"]] == [1]


def test_retry_wait_survives_process_restart_and_is_applied_once(tmp_path):
    database_path = tmp_path / "survey-worker-restart.sqlite3"
    effect_id = _seed_analytics_effect(database_path, status=STATUS_RETRY_WAIT)

    first_process = _make_app(database_path)
    with patch(
        "services.analytics.ingestor.analytics_ingestor.track",
        side_effect=_confirmed_analytics_event,
    ) as track:
        first_report = run_survey_response_effect_worker(first_process, once=True)
    with first_process.app_context():
        effect = db.session.get(SurveyResponseEffect, effect_id)
        assert effect.status == STATUS_SUCCEEDED
        assert effect.attempt_count == 2
        db.session.remove()
        db.engine.dispose()

    second_process = _make_app(database_path)
    with patch(
        "services.analytics.ingestor.analytics_ingestor.track",
        side_effect=_confirmed_analytics_event,
    ) as restarted_track:
        second_report = run_survey_response_effect_worker(second_process, once=True)

    assert first_report["claimed"] == 1
    assert first_report["processed"] == 1
    assert second_report["claimed"] == 0
    track.assert_called_once()
    restarted_track.assert_not_called()


def test_expired_processing_lease_is_recovered_by_permanent_worker(tmp_path):
    database_path = tmp_path / "survey-worker-lease.sqlite3"
    effect_id = _seed_analytics_effect(database_path, status=STATUS_PROCESSING)
    restarted_process = _make_app(database_path)

    with patch(
        "services.analytics.ingestor.analytics_ingestor.track",
        side_effect=_confirmed_analytics_event,
    ) as track:
        report = run_survey_response_effect_worker(restarted_process, once=True)

    with restarted_process.app_context():
        effect = db.session.get(SurveyResponseEffect, effect_id)
        assert effect.status == STATUS_SUCCEEDED
        # Lease recovery resumes the abandoned attempt rather than spending a
        # new retry budget slot.
        assert effect.attempt_count == 1
    assert report["claimed"] == 1
    track.assert_called_once()


def test_dead_effect_is_terminal_and_never_auto_retried(tmp_path):
    database_path = tmp_path / "survey-worker-dead.sqlite3"
    effect_id = _seed_analytics_effect(database_path, status=STATUS_DEAD)
    restarted_process = _make_app(database_path)

    with patch(
        "services.survey_response_effects._execute_effect"
    ) as execute_effect:
        report = run_survey_response_effect_worker(restarted_process, once=True)

    with restarted_process.app_context():
        effect = db.session.get(SurveyResponseEffect, effect_id)
        assert effect.status == STATUS_DEAD
        assert effect.attempt_count == effect.max_attempts
    assert report["claimed"] == 0
    execute_effect.assert_not_called()


def test_future_unknown_state_is_not_selected_for_blind_retry(tmp_path):
    """The due allowlist stays safe if an unknown state is added later.

    Today's schema rejects ``unknown``. SQLite's test-only pragma lets this
    exercise the dispatcher selection contract without changing that schema.
    """

    database_path = tmp_path / "survey-worker-unknown.sqlite3"
    effect_id = _seed_analytics_effect(database_path, status=STATUS_RETRY_WAIT)
    setup_process = _make_app(database_path)
    with setup_process.app_context():
        db.session.execute(text("PRAGMA ignore_check_constraints = ON"))
        db.session.execute(
            text(
                "UPDATE survey_response_effect_outbox "
                "SET status = 'unknown' WHERE id = :effect_id"
            ),
            {"effect_id": effect_id},
        )
        db.session.commit()

    restarted_process = _make_app(database_path)
    with patch(
        "services.survey_response_effects._execute_effect"
    ) as execute_effect:
        report = run_survey_response_effect_worker(restarted_process, once=True)

    with restarted_process.app_context():
        effect = db.session.get(SurveyResponseEffect, effect_id)
        assert effect.status == "unknown"
    assert report["claimed"] == 0
    execute_effect.assert_not_called()


def test_celery_sweep_task_is_registered_with_safe_acknowledgement_policy():
    task = dispatch_survey_response_effect_batch_task

    assert task.name == SURVEY_RESPONSE_EFFECT_SWEEP_TASK_NAME
    assert task.acks_late is True
    assert task.ignore_result is True
