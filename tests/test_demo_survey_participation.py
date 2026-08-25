from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app
from config import Config
from database import db
from models import DemoSurveyParticipation, EncRespuesta
from services.demo_surveys import (
    build_demo_public_survey_payload,
    build_demo_surveys_votings_contract,
)
from services.encuestas_service import EncuestaError
import services.demo_survey_participation as participation
from socket_service import socketio


SLUG = "demo-gobierno-junin-prioridades-barriales"
SUBMISSION_ID = "018f4c8e-1e56-7f38-a4df-83fd68394870"
FAKE_NEON_URI = (
    "postgresql+psycopg://preview_user:preview_password@"
    "ep-preview-pooler.us-east-2.aws.neon.tech/preview_db?sslmode=require"
)


class DemoParticipationTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


@pytest.fixture()
def demo_app(monkeypatch):
    app = create_app(DemoParticipationTestConfig)
    context = app.app_context()
    context.push()
    db.create_all()
    monkeypatch.setattr(
        participation,
        "demo_survey_participation_gate",
        lambda **_kwargs: {
            "contract_version": "demo.survey.participation_gate.v1",
            "enabled": True,
            "reason": "enabled",
        },
    )
    try:
        yield app
    finally:
        db.session.remove()
        db.drop_all()
        context.pop()


def _question_and_options():
    payload = build_demo_public_survey_payload(SLUG)
    assert payload is not None
    question = payload["preguntas"][0]
    return question, question["opciones"]


def _submission(option_index: int = 0, *, submission_id: str = SUBMISSION_ID):
    question, options = _question_and_options()
    return {
        "submission_id": submission_id,
        "source": "preview_e2e",
        "respuestas": [
            {
                "pregunta_id": question["id"],
                "opcion_id": options[option_index]["id"],
            }
        ],
    }


def test_preview_durable_http_flow_is_exactly_once_across_public_aliases(
    demo_app,
    monkeypatch,
):
    demo_app.config["CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE"] = "true"
    demo_app.config["CLOUDFLARE_TURNSTILE_SECRET_KEY"] = "test-turnstile-secret"
    client = demo_app.test_client()
    payload = _submission()
    verify_calls: list[str | None] = []

    def _verify(token, **_kwargs):
        verify_calls.append(token)
        return True

    monkeypatch.setattr("routes.v2.surveys.verify_turnstile", _verify)

    detail_before = client.get(f"/api/v2/public/surveys/{SLUG}")
    assert detail_before.status_code == 200, detail_before.get_json()
    assert detail_before.get_json()["resultados_envivo"]["total_respuestas"] == 100
    expected_room = f"encuesta:junin:{SLUG}"
    assert detail_before.get_json()["realtime"]["room"] == expected_room
    assert detail_before.get_json()["realtime"]["rooms"] == [expected_room]
    assert "legacy_room" not in detail_before.get_json()["realtime"]
    assert detail_before.get_json()["realtime"]["socket"]["join_payloads"] == [
        {"room": expected_room}
    ]
    assert detail_before.get_json()["persistence"] == {
        "contract_version": "demo.survey_persistence.v1",
        "state": "durable_preview",
        "durable": True,
        "database_write": True,
        "scope": "interactive_demo_only",
        "municipal_truth": False,
    }

    with patch("socket_service.socketio.emit") as first_publish:
        first = client.post(
            f"/api/v2/public/surveys/{SLUG}/respond",
            json=payload,
            headers={
                "Idempotency-Key": SUBMISSION_ID,
                "X-Turnstile-Token": "valid-preview-token",
            },
        )
    assert first.status_code == 201, first.get_json()
    assert [call.args[0] for call in first_publish.call_args_list] == [
        "survey_update",
        "survey_update_v2",
        "survey.vote.created",
    ]
    first_ack = first.get_json()
    assert first_ack["contract_version"] == "surveys.public_response.v2"
    assert first_ack["legacy_contract_version"] == "demo.survey_participation.v1"
    assert first_ack["slug"] == SLUG
    assert first_ack["persisted"] is True
    assert first_ack["durable"] is True
    assert first_ack["municipal_truth"] is False
    assert first_ack["response_origin"] == "interactive_demo"
    assert first_ack["replayed"] is False
    assert first_ack["realtime"]["room"] == expected_room
    assert "legacy_room" not in first_ack["realtime"]
    assert first_ack["realtime"]["delivery"] == "publish_accepted"
    assert first_ack["idempotency"]["state"] == "committed"
    assert first_ack["frontend_contract"]["persistence"] == first_ack["persistence"]
    assert verify_calls == ["valid-preview-token"]

    live = client.get(f"/api/v2/public/surveys/{SLUG}/live-results")
    assert live.status_code == 200, live.get_json()
    live_payload = live.get_json()
    assert live_payload["total_respuestas"] == 101
    assert live_payload["seeded_responses"] == 100
    assert live_payload["interactive_demo_responses"] == 1
    assert live_payload["municipal_truth"] is False
    assert live_payload["demo_data_composition"]["verified_citizen_responses"] == 0

    # A committed retry through the legacy alias bypasses one-shot Turnstile
    # and returns the same durable receipt without a second row.
    with patch("socket_service.socketio.emit") as replay_publish:
        replay = client.post(
            f"/api/public/encuestas/v1/{SLUG}/responder",
            json=payload,
            headers={"Idempotency-Key": SUBMISSION_ID},
        )
    assert replay.status_code == 200, replay.get_json()
    replay_ack = replay.get_json()
    assert replay_ack["response_id"] == first_ack["response_id"]
    assert replay_ack["replayed"] is True
    replay_publish.assert_not_called()
    assert verify_calls == ["valid-preview-token"]
    assert DemoSurveyParticipation.query.count() == 1
    assert EncRespuesta.query.count() == 0

    conflict_payload = _submission(option_index=1)
    conflict = client.post(
        f"/api/v2/public/surveys/{SLUG}/respond",
        json=conflict_payload,
        headers={"Idempotency-Key": SUBMISSION_ID},
    )
    assert conflict.status_code == 409, conflict.get_json()
    assert conflict.get_json()["reason_code"] == "survey_submission_id_conflict"
    assert verify_calls == ["valid-preview-token"]
    assert DemoSurveyParticipation.query.count() == 1


def test_gate_is_default_off_even_on_vercel_preview_neon():
    gate = participation.demo_survey_participation_gate(
        config={"SQLALCHEMY_DATABASE_URI": FAKE_NEON_URI},
        environ={"VERCEL": "1", "VERCEL_ENV": "preview"},
    )

    assert gate == {
        "contract_version": "demo.survey_participation_gate.v1",
        "enabled": False,
        "reason": "flag_disabled",
        "flag_enabled": False,
        "runtime": "vercel_preview",
        "database": "neon_postgres",
        "branch_guard_configured": False,
        "max_interactions_per_survey": 50,
        "production_allowed": False,
        "render_allowed": False,
    }


def test_gate_requires_exact_vercel_preview_neon_and_never_render():
    enabled = participation.demo_survey_participation_gate(
        config={
            "SQLALCHEMY_DATABASE_URI": FAKE_NEON_URI,
            participation.EXPECTED_BRANCH_ID_CONFIG: "br-preview-qa",
        },
        environ={
            participation.FEATURE_FLAG: "true",
            "VERCEL": "1",
            "VERCEL_ENV": "preview",
        },
    )
    production = participation.demo_survey_participation_gate(
        config={
            participation.FEATURE_FLAG: True,
            participation.EXPECTED_BRANCH_ID_CONFIG: "br-preview-qa",
            "SQLALCHEMY_DATABASE_URI": FAKE_NEON_URI,
        },
        environ={"VERCEL": "1", "VERCEL_ENV": "production"},
    )
    render = participation.demo_survey_participation_gate(
        config={
            participation.FEATURE_FLAG: True,
            "SQLALCHEMY_DATABASE_URI": FAKE_NEON_URI,
        },
        environ={
            "VERCEL": "1",
            "VERCEL_ENV": "preview",
            "RENDER": "true",
        },
    )
    non_neon = participation.demo_survey_participation_gate(
        config={
            participation.FEATURE_FLAG: True,
            "SQLALCHEMY_DATABASE_URI": "postgresql://user:password@db.example.test/db",
        },
        environ={"VERCEL": "1", "VERCEL_ENV": "preview"},
    )

    assert enabled["enabled"] is True
    assert enabled["reason"] == "enabled"
    assert production["enabled"] is False
    assert production["reason"] == "vercel_preview_required"
    assert render["enabled"] is False
    assert render["reason"] == "render_runtime_forbidden"
    assert non_neon["enabled"] is False
    assert non_neon["reason"] == "neon_postgres_required"
    assert all("uri" not in key and "url" not in key for key in enabled)


def test_explicit_durable_opt_in_fails_closed_instead_of_degrading(monkeypatch):
    monkeypatch.setattr(
        participation,
        "demo_survey_participation_gate",
        lambda **_kwargs: {
            "enabled": False,
            "flag_enabled": True,
            "reason": "neon_postgres_required",
        },
    )

    with pytest.raises(EncuestaError) as exc_info:
        participation.durable_demo_survey_participation_enabled()

    assert exc_info.value.status_code == 503
    assert exc_info.value.payload["reason_code"] == (
        "demo_survey_participation_unavailable"
    )
    assert exc_info.value.payload["gate_reason"] == "neon_postgres_required"


def test_enabled_runtime_verifies_the_actual_neon_branch(demo_app, monkeypatch):
    expected_branch_id = "br-dawn-frost-acazh62o"
    demo_app.config[participation.EXPECTED_BRANCH_ID_CONFIG] = expected_branch_id
    monkeypatch.setattr(
        participation,
        "demo_survey_participation_gate",
        lambda **_kwargs: {
            "enabled": True,
            "reason": "enabled",
            "max_interactions_per_survey": 50,
        },
    )

    class _ScalarResult:
        def scalar(self):
            return "br-unexpected-main"

    monkeypatch.setattr(db.session, "execute", lambda *_args, **_kwargs: _ScalarResult())

    with pytest.raises(EncuestaError) as exc_info:
        participation._require_enabled()

    assert exc_info.value.status_code == 503
    assert exc_info.value.payload["gate_reason"] == "neon_branch_mismatch"


def test_atomic_capacity_keeps_replays_available_and_bounds_new_rows(
    demo_app,
    monkeypatch,
):
    monkeypatch.setattr(
        participation,
        "demo_survey_participation_gate",
        lambda **_kwargs: {
            "enabled": True,
            "reason": "enabled",
            "max_interactions_per_survey": 1,
        },
    )
    first_payload = _submission()
    first = participation.persist_demo_survey_participation(
        SLUG,
        first_payload,
        submission_id=SUBMISSION_ID,
    )

    replay = participation.persist_demo_survey_participation(
        SLUG,
        first_payload,
        submission_id=SUBMISSION_ID,
    )
    assert replay.replayed is True
    assert replay.response_id == first.response_id

    second_submission_id = "018f4c8e-1e56-7f38-a4df-83fd68394899"
    with pytest.raises(EncuestaError) as exc_info:
        participation.persist_demo_survey_participation(
            SLUG,
            _submission(option_index=1, submission_id=second_submission_id),
            submission_id=second_submission_id,
        )

    assert exc_info.value.status_code == 429
    assert exc_info.value.payload["reason_code"] == (
        "demo_survey_participation_capacity_reached"
    )
    assert exc_info.value.payload["capacity"] == {
        "scope": "survey_slug",
        "limit": 1,
        "bounded": True,
    }
    assert DemoSurveyParticipation.query.count() == 1


def test_persist_replay_conflict_and_truth_isolation(demo_app):
    payload = _submission()

    first = participation.persist_demo_survey_participation(
        SLUG,
        payload,
        submission_id=SUBMISSION_ID,
    )
    aggregate = participation.get_demo_survey_participation_aggregate(SLUG)
    ack = participation.build_demo_survey_participation_ack(
        first,
        aggregate=aggregate,
    )

    assert first.replayed is False
    assert DemoSurveyParticipation.query.count() == 1
    assert EncRespuesta.query.count() == 0
    row = DemoSurveyParticipation.query.one()
    assert row.submission_id_hash != SUBMISSION_ID
    assert len(row.submission_id_hash) == 64
    assert row.response_origin == "interactive_demo"
    assert not hasattr(row, "ip")
    assert not hasattr(row, "user_agent")
    assert not hasattr(row, "turnstile_token")
    assert ack["contract_version"] == "surveys.public_response.v2"
    assert ack["persisted"] is True
    assert ack["durable"] is True
    assert ack["municipal_truth"] is False
    assert ack["slug"] == SLUG
    assert ack["tenant_slug"] == "junin"
    assert ack["sector"] == "gobierno"
    assert ack["response_id"] == first.response_id
    assert ack["respuesta_id"] == first.response_id
    assert ack["total_responses_after"] == 101
    assert ack["idempotency"] == {
        "contract_version": "surveys.response_receipt.v1",
        "canonical_version": "survey-response.v1",
        "receipt_id": first.response_id,
        "submission_id": SUBMISSION_ID,
        "response_id": first.response_id,
        "instrument_revision": 1,
        "state": "committed",
        "disposition": "accepted",
        "persisted": True,
        "replayed": False,
    }

    replay = participation.find_demo_survey_participation_replay(
        SLUG,
        payload,
        submission_id=SUBMISSION_ID,
    )
    assert replay is not None
    assert replay.replayed is True
    assert replay.response_id == first.response_id
    replay_from_persist = participation.persist_demo_survey_participation(
        SLUG,
        payload,
        submission_id=SUBMISSION_ID,
    )
    assert replay_from_persist.replayed is True
    assert replay_from_persist.response_id == first.response_id
    assert DemoSurveyParticipation.query.count() == 1

    with pytest.raises(EncuestaError) as conflict:
        participation.persist_demo_survey_participation(
            SLUG,
            _submission(1),
            submission_id=SUBMISSION_ID,
        )
    assert conflict.value.status_code == 409
    assert conflict.value.payload["reason_code"] == "survey_submission_id_conflict"
    assert DemoSurveyParticipation.query.count() == 1


def test_missing_key_invalid_option_and_unknown_slug_fail_without_writes(demo_app):
    payload = _submission()
    payload.pop("submission_id")
    with pytest.raises(EncuestaError) as missing:
        participation.persist_demo_survey_participation(SLUG, payload)
    assert missing.value.status_code == 400
    assert missing.value.payload["reason_code"] == "survey_submission_id_required"

    question, _options = _question_and_options()
    invalid = {
        "submission_id": SUBMISSION_ID,
        "respuestas": [
            {
                "pregunta_id": question["id"],
                "opcion_id": "option-not-in-instrument",
            }
        ],
    }
    with pytest.raises(EncuestaError) as invalid_option:
        participation.persist_demo_survey_participation(SLUG, invalid)
    assert invalid_option.value.status_code == 400
    assert invalid_option.value.payload["reason_code"] == "demo_survey_participation_invalid"

    with pytest.raises(EncuestaError) as unknown:
        participation.persist_demo_survey_participation(
            "demo-gobierno-junin-no-existe",
            _submission(),
            submission_id=SUBMISSION_ID,
        )
    assert unknown.value.status_code == 404
    assert DemoSurveyParticipation.query.count() == 0


def test_live_results_overlay_increments_option_and_keeps_provenance_truthful(demo_app):
    question, options = _question_and_options()
    selected_option = options[0]
    base = participation.build_demo_live_results_payload(SLUG)
    assert base is not None
    base_question = next(iter(base["preguntas"].values()))
    base_selected = next(
        option for option in base_question["opciones"] if option["id"] == selected_option["id"]
    )

    participation.persist_demo_survey_participation(
        SLUG,
        _submission(),
        submission_id=SUBMISSION_ID,
    )
    live = participation.build_durable_demo_live_results_payload(SLUG)

    assert live is not None
    live_question = next(iter(live["preguntas"].values()))
    live_selected = next(
        option for option in live_question["opciones"] if option["id"] == selected_option["id"]
    )
    assert live_selected["votos"] == base_selected["votos"] + 1
    assert live_question["total_votos"] == 101
    assert live["total_respuestas"] == 101
    assert live["seeded_responses"] == 100
    assert live["interactive_demo_responses"] == 1
    assert live["result_version"] == 101
    assert "interactive:1" in live["snapshot_version"]
    assert sum(option["porcentaje"] for option in live_question["opciones"]) == 100
    assert live["municipal_truth"] is False
    provenance = live["data_provenance"]
    assert provenance["mode"] == "synthetic"
    assert provenance["real_responses_included"] == 0
    assert provenance["synthetic_responses_included"] == 101
    assert provenance["composition"] == {
        "contract_version": "demo.survey_data_composition.v1",
        "mode": "synthetic_demo_with_interactive_qa",
        "seeded_synthetic_responses": 100,
        "interactive_demo_responses": 1,
        "verified_citizen_responses": 0,
        "institutional_truth": False,
        "suitable_for_product_demonstration": True,
        "suitable_for_government_decisions": False,
    }
    assert live["heatmap"]["metadata"]["mapped_seeded_responses"] == 100
    assert live["heatmap"]["metadata"]["unmapped_interactive_demo_responses"] == 1
    assert question["id"] in live["preguntas"]


@pytest.mark.parametrize("non_finite_percentage", [float("nan"), float("inf"), float("-inf")])
def test_admin_projection_rejects_non_finite_percentages(
    demo_app,
    non_finite_percentage,
):
    contract = build_demo_surveys_votings_contract(
        sector="gobierno",
        tenant_slug="junin",
    )
    item = contract["items"][0]
    live = participation.build_durable_demo_live_results_payload(item["slug"])
    assert live is not None
    question = next(iter(live["preguntas"].values()))
    question["opciones"][0]["porcentaje"] = non_finite_percentage

    with pytest.raises(ValueError, match="percentage is invalid"):
        participation._merge_durable_live_results_into_demo_item(item, live)


def test_committed_demo_vote_emits_v2_events_only_to_tenant_scoped_room(demo_app):
    receipt = participation.persist_demo_survey_participation(
        SLUG,
        _submission(),
        submission_id=SUBMISSION_ID,
    )
    aggregate = participation.get_demo_survey_participation_aggregate(SLUG)
    expected_room = f"encuesta:junin:{SLUG}"

    with patch("socket_service.socketio.emit") as socket_emit:
        published = participation.publish_durable_demo_survey_participation_update(
            receipt,
            aggregate,
        )

    assert published is True
    assert [call.args[0] for call in socket_emit.call_args_list] == [
        "survey_update",
        "survey_update_v2",
        "survey.vote.created",
    ]
    assert {call.kwargs.get("room") for call in socket_emit.call_args_list} == {
        expected_room
    }
    modern_payload = socket_emit.call_args_list[1].args[1]
    assert modern_payload["contract_version"] == "surveys.live_results.v2"
    assert modern_payload["tenant_slug"] == "junin"
    assert modern_payload["slug"] == SLUG
    assert modern_payload["total_respuestas"] == 101
    assert modern_payload["event"]["event_name"] == "survey.response.committed"
    assert len(modern_payload["event"]["event_id"]) == 64
    assert "legacy_results" not in modern_payload

    replay = participation.persist_demo_survey_participation(
        SLUG,
        _submission(),
        submission_id=SUBMISSION_ID,
    )
    assert replay.replayed is True
    with patch("socket_service.socketio.emit") as replay_emit:
        republished = participation.publish_durable_demo_survey_participation_update(
            replay,
            aggregate,
        )
    assert republished is False
    replay_emit.assert_not_called()


def test_durable_demo_socket_membership_ack_receives_committed_vote(demo_app):
    expected_room = f"encuesta:junin:{SLUG}"
    socket_client = socketio.test_client(demo_app)
    try:
        assert socket_client.is_connected()
        socket_client.emit("join", {"room": expected_room})
        join_events = socket_client.get_received()
        join_ack = next(event for event in join_events if event["name"] == "join_ack")
        assert join_ack["args"][0] == {
            "room": expected_room,
            "access_mode": "public_survey_room",
        }

        response = demo_app.test_client().post(
            f"/api/v2/public/surveys/{SLUG}/respond",
            json=_submission(),
            headers={"Idempotency-Key": SUBMISSION_ID},
        )
        assert response.status_code == 201, response.get_json()
        events = socket_client.get_received()
        event_names = {event["name"] for event in events}
        assert event_names == {
            "survey_update",
            "survey_update_v2",
            "survey.vote.created",
        }
        update = next(event for event in events if event["name"] == "survey_update_v2")
        assert update["args"][0]["tenant_slug"] == "junin"
        assert update["args"][0]["slug"] == SLUG
        assert update["args"][0]["total_respuestas"] == 101
    finally:
        if socket_client.is_connected():
            socket_client.disconnect()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("survey_slug", "demo-gobierno-ushuaia-prioridades-barriales"),
        ("tenant_slug", "ushuaia"),
    ],
)
def test_demo_realtime_scope_mismatch_fails_closed_before_emit(
    demo_app,
    field,
    value,
):
    receipt = participation.persist_demo_survey_participation(
        SLUG,
        _submission(),
        submission_id=SUBMISSION_ID,
    )
    aggregate = participation.get_demo_survey_participation_aggregate(SLUG)
    aggregate[field] = value

    with patch("socket_service.socketio.emit") as socket_emit:
        published = participation.publish_durable_demo_survey_participation_update(
            receipt,
            aggregate,
        )

    assert published is False
    socket_emit.assert_not_called()


def test_socket_failure_keeps_committed_demo_write_and_returns_polling_fallback(
    demo_app,
):
    client = demo_app.test_client()
    with patch("socket_service.socketio.emit", side_effect=RuntimeError("broker unavailable")):
        response = client.post(
            f"/api/v2/public/surveys/{SLUG}/respond",
            json=_submission(),
            headers={"Idempotency-Key": SUBMISSION_ID},
        )

    assert response.status_code == 201, response.get_json()
    payload = response.get_json()
    assert payload["persisted"] is True
    assert payload["durable"] is True
    assert payload["realtime"]["delivery"] == "polling_fallback"
    assert DemoSurveyParticipation.query.count() == 1


def test_model_unique_constraint_keeps_one_receipt(demo_app):
    prepared = participation.prepare_demo_survey_participation(
        SLUG,
        _submission(),
        submission_id=SUBMISSION_ID,
    )
    values = {
        "survey_slug": prepared.instrument.slug,
        "tenant_slug": prepared.instrument.tenant_slug,
        "sector": prepared.instrument.sector,
        "question_id": prepared.question_id,
        "option_id": prepared.option_id,
        "submission_id_hash": prepared.submission_id_hash,
        "payload_hash": prepared.payload_hash,
        "instrument_sha256": prepared.instrument.instrument_sha256,
        "instrument_revision": 1,
        "response_origin": "interactive_demo",
    }
    db.session.add(DemoSurveyParticipation(**values))
    db.session.commit()
    db.session.add(DemoSurveyParticipation(**values))
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()
    assert DemoSurveyParticipation.query.count() == 1


def _load_migration_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "20260825_add_demo_survey_participation.py"
    )
    spec = importlib.util.spec_from_file_location("demo_participation_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_has_one_correct_head_and_append_only_sqlite_guards(tmp_path):
    migration = _load_migration_module()
    assert migration.revision == "20260825_demo_survey_participation_v1"
    assert migration.down_revision == "20260820_survey_content_jurisdiction_v1"

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'demo-participation.sqlite'}")
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

    valid_row = {
        "survey_slug": SLUG,
        "tenant_slug": "junin",
        "sector": "gobierno",
        "question_id": "q_1",
        "option_id": "q_1_op_1",
        "submission_id_hash": "a" * 64,
        "payload_hash": "b" * 64,
        "instrument_sha256": "c" * 64,
        "instrument_revision": 1,
        "response_origin": "interactive_demo",
    }
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO demo_survey_participation "
                "(survey_slug, tenant_slug, sector, question_id, option_id, "
                "submission_id_hash, payload_hash, instrument_sha256, "
                "instrument_revision, response_origin) VALUES "
                "(:survey_slug, :tenant_slug, :sector, :question_id, :option_id, "
                ":submission_id_hash, :payload_hash, :instrument_sha256, "
                ":instrument_revision, :response_origin)"
            ),
            valid_row,
        )

    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "UPDATE demo_survey_participation SET option_id = 'q_1_op_2' "
                    "WHERE survey_slug = :slug"
                ),
                {"slug": SLUG},
            )
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "DELETE FROM demo_survey_participation WHERE survey_slug = :slug"
                ),
                {"slug": SLUG},
            )

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()
        assert not sa.inspect(connection).has_table("demo_survey_participation")
