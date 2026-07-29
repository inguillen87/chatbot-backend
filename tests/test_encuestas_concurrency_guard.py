from __future__ import annotations

import threading
import sqlite3
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError as SAOperationalError

from app import create_app
from config import TestingConfig
from database import db
from models import EncEncuesta, EncRespuesta, SurveyResponseEffect, SurveyResponseReceipt
import services.encuestas_service as encuesta_service
from services.encuestas_service import (
    EncuestaError,
    create_encuesta,
    publicar_encuesta,
    save_respuesta,
    serialize_encuesta,
    serialize_public_encuesta,
    update_encuesta,
)


class _User:
    id = None
    municipio_id = 41


def _instrument_payload(title: str = "Encuesta concurrente") -> dict:
    return {
        "titulo": title,
        "politica_unicidad": "libre",
        "preguntas": [
            {
                "orden": 1,
                "tipo": "opcion_unica",
                "texto": "Elegí una opción",
                "obligatoria": True,
                "opciones": [
                    {"orden": 1, "texto": "A"},
                    {"orden": 2, "texto": "B"},
                ],
            }
        ],
    }


def _structural_update_payload(encuesta: EncEncuesta) -> dict:
    question = encuesta.preguntas[0]
    return {
        "titulo": encuesta.titulo,
        "preguntas": [
            {
                "id": question.id,
                "orden": question.orden,
                "tipo": question.tipo,
                "texto": question.texto,
                "obligatoria": question.obligatoria,
                "opciones": [
                    {
                        "id": option.id,
                        "orden": option.orden,
                        "texto": option.texto,
                    }
                    for option in question.opciones
                ],
            },
            {
                "orden": 2,
                "tipo": "abierta",
                "texto": "Nueva pregunta estructural",
                "obligatoria": True,
            },
        ],
    }


@pytest.fixture()
def concurrency_app(tmp_path):
    database_path = (tmp_path / "survey-concurrency.sqlite3").as_posix()
    config = type(
        "SurveyConcurrencyConfig",
        (TestingConfig,),
        {
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database_path}",
            "SQLALCHEMY_ENGINE_OPTIONS": {
                "connect_args": {"check_same_thread": False, "timeout": 5}
            },
        },
    )
    app = create_app(config)
    with app.app_context():
        db.create_all()
    try:
        yield app
    finally:
        with app.app_context():
            db.session.remove()
            db.drop_all()


def _publish_for_threads(app):
    with app.app_context():
        encuesta = create_encuesta(_instrument_payload(), _User())
        encuesta, link = publicar_encuesta(encuesta.id, _User())
        question = encuesta.preguntas[0]
        return {
            "survey_id": encuesta.id,
            "slug": link.slug_publico,
            "question_id": question.id,
            "option_id": question.opciones[0].id,
            "update_payload": deepcopy(_structural_update_payload(encuesta)),
        }


def _response_payload(context: dict) -> dict:
    return {
        "respuestas": [
            {
                "pregunta_id": context["question_id"],
                "opcion_id": context["option_id"],
            }
        ]
    }


def _request_context() -> dict:
    return {
        "ip": "127.0.0.1",
        "user_agent": "pytest-concurrency",
        "anon_id": "survey-concurrency-anon",
        "canal": "web",
    }


def test_concurrent_same_submission_id_returns_one_durable_response(concurrency_app):
    context = _publish_for_threads(concurrency_app)
    submission_id = "survey-concurrent-receipt-0001"
    barrier = threading.Barrier(2)
    outcomes: list[tuple[int, bool]] = []
    failures: list[Exception] = []

    def submit_worker():
        with concurrency_app.app_context():
            try:
                barrier.wait(timeout=5)
                response = save_respuesta(
                    context["slug"],
                    {
                        **_response_payload(context),
                        "submission_id": submission_id,
                    },
                    _request_context(),
                    submission_id=submission_id,
                )
                outcomes.append(
                    (response.id, bool(getattr(response, "submission_replayed", False)))
                )
            except Exception as exc:  # pragma: no cover - asserted below
                db.session.rollback()
                failures.append(exc)
            finally:
                db.session.remove()

    threads = [threading.Thread(target=submit_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert len(outcomes) == 2
    assert len({response_id for response_id, _ in outcomes}) == 1
    assert sorted(replayed for _, replayed in outcomes) == [False, True]
    with concurrency_app.app_context():
        assert EncRespuesta.query.filter_by(encuesta_id=context["survey_id"]).count() == 1
        assert SurveyResponseReceipt.query.filter_by(survey_id=context["survey_id"]).count() == 1


def test_response_first_serializes_and_rejects_concurrent_structural_edit(
    concurrency_app,
    monkeypatch,
):
    context = _publish_for_threads(concurrency_app)
    response_holds_guard = threading.Event()
    release_response = threading.Event()
    edit_started = threading.Event()
    edit_finished = threading.Event()
    outcomes: dict[str, object] = {}

    original_validate = encuesta_service._validate_respuesta_payload

    def paused_response_validation(encuesta, respuestas):
        if threading.current_thread().name == "survey-response-worker":
            response_holds_guard.set()
            assert release_response.wait(5), "timed out waiting to release response"
        return original_validate(encuesta, respuestas)

    monkeypatch.setattr(
        encuesta_service,
        "_validate_respuesta_payload",
        paused_response_validation,
    )

    def response_worker():
        with concurrency_app.app_context():
            try:
                outcomes["response"] = save_respuesta(
                    context["slug"],
                    _response_payload(context),
                    _request_context(),
                ).id
            except Exception as exc:  # pragma: no cover - asserted below
                db.session.rollback()
                outcomes["response_error"] = exc
            finally:
                db.session.remove()

    def edit_worker():
        with concurrency_app.app_context():
            edit_started.set()
            try:
                update_encuesta(
                    context["survey_id"],
                    context["update_payload"],
                    _User(),
                )
                outcomes["edit"] = "committed"
            except Exception as exc:
                db.session.rollback()
                outcomes["edit_error"] = exc
            finally:
                edit_finished.set()
                db.session.remove()

    response_thread = threading.Thread(
        target=response_worker,
        name="survey-response-worker",
    )
    edit_thread = threading.Thread(target=edit_worker, name="survey-edit-worker")
    response_thread.start()
    assert response_holds_guard.wait(5)
    edit_thread.start()
    assert edit_started.wait(5)

    # The structural writer must be waiting on the response's real SQLite
    # write lock; it cannot pass a stale count()==0 check.
    assert not edit_finished.wait(0.25)
    release_response.set()
    response_thread.join(5)
    edit_thread.join(5)
    assert not response_thread.is_alive()
    assert not edit_thread.is_alive()

    assert "response_error" not in outcomes
    assert isinstance(outcomes.get("edit_error"), EncuestaError)
    edit_error = outcomes["edit_error"]
    assert edit_error.status_code == 409
    assert edit_error.payload["reason_code"] == "survey_structure_locked"

    with concurrency_app.app_context():
        encuesta = db.session.get(EncEncuesta, context["survey_id"])
        assert EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count() == 1
        assert encuesta.structure_locked_at is not None
        assert len(encuesta.preguntas) == 1
        assert encuesta.structure_revision == 1


def test_edit_first_forces_waiting_response_to_validate_fresh_revision(
    concurrency_app,
    monkeypatch,
):
    context = _publish_for_threads(concurrency_app)
    edit_holds_guard = threading.Event()
    release_edit = threading.Event()
    response_started = threading.Event()
    response_finished = threading.Event()
    outcomes: dict[str, object] = {}
    original_validate = encuesta_service._validate_instrument_payload
    paused_once = False

    def paused_instrument_validation(payload):
        nonlocal paused_once
        if threading.current_thread().name == "survey-edit-worker" and not paused_once:
            paused_once = True
            edit_holds_guard.set()
            assert release_edit.wait(5), "timed out waiting to release edit"
        return original_validate(payload)

    monkeypatch.setattr(
        encuesta_service,
        "_validate_instrument_payload",
        paused_instrument_validation,
    )

    def edit_worker():
        with concurrency_app.app_context():
            try:
                updated = update_encuesta(
                    context["survey_id"],
                    context["update_payload"],
                    _User(),
                )
                outcomes["revision"] = updated.structure_revision
            except Exception as exc:  # pragma: no cover - asserted below
                db.session.rollback()
                outcomes["edit_error"] = exc
            finally:
                db.session.remove()

    def response_worker():
        with concurrency_app.app_context():
            response_started.set()
            try:
                save_respuesta(
                    context["slug"],
                    _response_payload(context),
                    _request_context(),
                )
                outcomes["response"] = "committed"
            except Exception as exc:
                db.session.rollback()
                outcomes["response_error"] = exc
            finally:
                response_finished.set()
                db.session.remove()

    edit_thread = threading.Thread(target=edit_worker, name="survey-edit-worker")
    response_thread = threading.Thread(
        target=response_worker,
        name="survey-response-worker",
    )
    edit_thread.start()
    assert edit_holds_guard.wait(5)
    response_thread.start()
    assert response_started.wait(5)
    assert not response_finished.wait(0.25)

    release_edit.set()
    edit_thread.join(5)
    response_thread.join(5)
    assert not edit_thread.is_alive()
    assert not response_thread.is_alive()

    assert "edit_error" not in outcomes
    assert outcomes["revision"] == 2
    assert isinstance(outcomes.get("response_error"), EncuestaError)
    assert "response" not in outcomes

    with concurrency_app.app_context():
        encuesta = db.session.get(EncEncuesta, context["survey_id"])
        assert encuesta.structure_revision == 2
        assert encuesta.structure_locked_at is None
        assert len(encuesta.preguntas) == 2
        assert EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count() == 0


def test_sqlite_frozen_votes_remain_serialized_by_the_safe_dialect_fallback(
    concurrency_app,
    monkeypatch,
):
    context = _publish_for_threads(concurrency_app)
    with concurrency_app.app_context():
        save_respuesta(
            context["slug"],
            _response_payload(context),
            _request_context(),
        )
        survey = db.session.get(EncEncuesta, context["survey_id"])
        assert survey.structure_locked_at is not None

    first_holds_guard = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_reached_validation = threading.Event()
    second_finished = threading.Event()
    outcomes: dict[str, object] = {}
    original_validate = encuesta_service._validate_respuesta_payload

    def paused_validation(encuesta, respuestas):
        thread_name = threading.current_thread().name
        if thread_name == "sqlite-frozen-vote-one":
            first_holds_guard.set()
            assert release_first.wait(5), "timed out waiting to release first vote"
        elif thread_name == "sqlite-frozen-vote-two":
            second_reached_validation.set()
        return original_validate(encuesta, respuestas)

    monkeypatch.setattr(
        encuesta_service,
        "_validate_respuesta_payload",
        paused_validation,
    )

    def vote_worker(outcome_key: str, *, is_second: bool = False):
        with concurrency_app.app_context():
            if is_second:
                second_started.set()
            try:
                outcomes[outcome_key] = save_respuesta(
                    context["slug"],
                    _response_payload(context),
                    _request_context(),
                ).id
            except Exception as exc:  # pragma: no cover - asserted below
                db.session.rollback()
                outcomes[f"{outcome_key}_error"] = exc
            finally:
                if is_second:
                    second_finished.set()
                db.session.remove()

    first_thread = threading.Thread(
        target=vote_worker,
        args=("first",),
        name="sqlite-frozen-vote-one",
    )
    second_thread = threading.Thread(
        target=vote_worker,
        args=("second",),
        kwargs={"is_second": True},
        name="sqlite-frozen-vote-two",
    )
    first_thread.start()
    assert first_holds_guard.wait(5)
    second_thread.start()
    assert second_started.wait(5)

    # SQLite can only run one writer.  The fallback must wait before response
    # validation instead of reading a snapshot that may become stale.
    assert not second_reached_validation.wait(0.25)
    assert not second_finished.is_set()

    release_first.set()
    first_thread.join(5)
    second_thread.join(5)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert "first_error" not in outcomes
    assert "second_error" not in outcomes
    assert second_reached_validation.is_set()

    with concurrency_app.app_context():
        assert EncRespuesta.query.filter_by(
            encuesta_id=context["survey_id"]
        ).count() == 3


def test_commit_false_flushes_marker_in_same_transaction_and_rollback_reverts_it(client):
    with client.application.app_context():
        encuesta = create_encuesta(_instrument_payload(), _User())
        encuesta, link = publicar_encuesta(encuesta.id, _User())
        question = encuesta.preguntas[0]
        context = {
            "question_id": question.id,
            "option_id": question.opciones[0].id,
        }

        save_respuesta(
            link.slug_publico,
            _response_payload(context),
            _request_context(),
            commit=False,
            emit_realtime_update=False,
            grant_reward=False,
        )

        assert db.session.get(EncEncuesta, encuesta.id).structure_locked_at is not None
        assert EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count() == 1
        assert SurveyResponseEffect.query.filter_by(
            survey_id=encuesta.id,
        ).count() == 1

        db.session.rollback()
        db.session.expire_all()
        assert db.session.get(EncEncuesta, encuesta.id).structure_locked_at is None
        assert EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count() == 0
        assert SurveyResponseEffect.query.filter_by(
            survey_id=encuesta.id,
        ).count() == 0


def test_structure_marker_survives_response_deletion_and_keeps_structure_locked(client):
    with client.application.app_context():
        encuesta = create_encuesta(_instrument_payload(), _User())
        encuesta, link = publicar_encuesta(encuesta.id, _User())
        question = encuesta.preguntas[0]
        context = {
            "question_id": question.id,
            "option_id": question.opciones[0].id,
        }
        save_respuesta(
            link.slug_publico,
            _response_payload(context),
            _request_context(),
        )
        update_payload = _structural_update_payload(encuesta)

        EncRespuesta.query.filter_by(encuesta_id=encuesta.id).delete(
            synchronize_session=False
        )
        db.session.commit()
        db.session.expire_all()

        persisted = db.session.get(EncEncuesta, encuesta.id)
        assert persisted.structure_locked_at is not None
        assert EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count() == 0
        with pytest.raises(EncuestaError) as exc_info:
            update_encuesta(encuesta.id, update_payload, _User())
        db.session.rollback()
        assert exc_info.value.status_code == 409
        assert exc_info.value.payload["reason_code"] == "survey_structure_locked"


def test_stale_admin_structure_revision_fails_without_overwriting_winner(client):
    with client.application.app_context():
        encuesta = create_encuesta(_instrument_payload(), _User())
        original_admin_payload = serialize_encuesta(encuesta)
        assert original_admin_payload["structure_guard"] == {
            "contract_version": "surveys.structure_guard.v1",
            "revision": 1,
            "locked": False,
            "locked_at": None,
        }
        assert "structure_guard" not in serialize_public_encuesta(encuesta)

        winner_payload = _structural_update_payload(encuesta)
        winner_payload["expected_structure_revision"] = 1
        winner_payload["preguntas"][1]["texto"] = "Cambio ganador"
        winner = update_encuesta(encuesta.id, winner_payload, _User())
        assert winner.structure_revision == 2
        assert winner.preguntas[1].texto == "Cambio ganador"

        stale_payload = deepcopy(_structural_update_payload(winner))
        stale_payload["expected_structure_revision"] = 1
        stale_payload["preguntas"][1]["texto"] = "Cambio que debe perder"
        with pytest.raises(EncuestaError) as exc_info:
            update_encuesta(encuesta.id, stale_payload, _User())
        db.session.rollback()

        assert exc_info.value.status_code == 409
        assert exc_info.value.payload == {
            "contract_version": "surveys.structure_guard.v1",
            "reason_code": "survey_structure_revision_conflict",
            "expected_revision": 1,
            "current_revision": 2,
            "retryable": False,
            "action_hint": "reload_survey",
        }
        persisted = db.session.get(EncEncuesta, encuesta.id)
        assert persisted.structure_revision == 2
        assert persisted.preguntas[1].texto == "Cambio ganador"


def test_write_guard_maps_only_known_lock_errors_to_retryable_conflict(client, monkeypatch):
    with client.application.app_context():
        def locked_execute(*_args, **_kwargs):
            raise SAOperationalError(
                "UPDATE enc_encuesta",
                {},
                sqlite3.OperationalError("database is locked"),
            )

        monkeypatch.setattr(db.session, "execute", locked_execute)
        with pytest.raises(EncuestaError) as exc_info:
            encuesta_service._acquire_encuesta_write_guard(1)
        assert exc_info.value.status_code == 409
        assert exc_info.value.payload["reason_code"] == "survey_concurrent_update"
        assert exc_info.value.payload["retryable"] is True


def test_write_guard_does_not_hide_schema_drift_as_retryable_409(client, monkeypatch):
    with client.application.app_context():
        def broken_schema_execute(*_args, **_kwargs):
            raise SAOperationalError(
                "UPDATE enc_encuesta",
                {},
                sqlite3.OperationalError("no such column: structure_revision"),
            )

        monkeypatch.setattr(db.session, "execute", broken_schema_execute)
        with pytest.raises(SAOperationalError):
            encuesta_service._acquire_encuesta_write_guard(1)


class _ScalarGuardResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


def _postgresql_bind():
    return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))


def test_postgresql_locked_response_guard_uses_shared_row_lock_without_parent_update(
    client,
    monkeypatch,
):
    with client.application.app_context():
        executed: list[tuple[str, dict]] = []
        guarded = SimpleNamespace(id=77, structure_locked_at="already-frozen")

        monkeypatch.setattr(db.session, "get_bind", _postgresql_bind)

        def capture_execute(statement, params):
            executed.append((" ".join(str(statement).split()), params))
            return _ScalarGuardResult(77)

        monkeypatch.setattr(db.session, "execute", capture_execute)
        monkeypatch.setattr(
            encuesta_service,
            "_load_encuesta_after_guard",
            lambda encuesta_id: guarded if encuesta_id == 77 else None,
        )

        def unexpected_exclusive_guard(_encuesta_id):
            pytest.fail("a frozen PostgreSQL survey must not take the parent UPDATE guard")

        monkeypatch.setattr(
            encuesta_service,
            "_acquire_encuesta_write_guard",
            unexpected_exclusive_guard,
        )

        result = encuesta_service._acquire_encuesta_response_guard(77)

        assert result is guarded
        assert executed == [
            (
                "SELECT id FROM enc_encuesta WHERE id = :encuesta_id "
                "AND structure_locked_at IS NOT NULL FOR SHARE",
                {"encuesta_id": 77},
            )
        ]
        assert "FOR KEY SHARE" not in executed[0][0]
        assert "UPDATE enc_encuesta" not in executed[0][0]


def test_postgresql_first_response_falls_back_to_exclusive_parent_guard(
    client,
    monkeypatch,
):
    with client.application.app_context():
        executed: list[str] = []
        exclusive_calls: list[int] = []
        guarded = SimpleNamespace(id=88, structure_locked_at=None)

        monkeypatch.setattr(db.session, "get_bind", _postgresql_bind)

        def capture_execute(statement, _params):
            executed.append(" ".join(str(statement).split()))
            return _ScalarGuardResult(None)

        def exclusive_guard(encuesta_id):
            exclusive_calls.append(encuesta_id)
            return guarded

        monkeypatch.setattr(db.session, "execute", capture_execute)
        monkeypatch.setattr(
            encuesta_service,
            "_acquire_encuesta_write_guard",
            exclusive_guard,
        )

        result = encuesta_service._acquire_encuesta_response_guard(88)

        assert result is guarded
        assert exclusive_calls == [88]
        assert len(executed) == 1
        assert "structure_locked_at IS NOT NULL" in executed[0]
        assert executed[0].endswith("FOR SHARE")


def test_sqlite_response_guard_deliberately_keeps_exclusive_write_path(
    client,
    monkeypatch,
):
    with client.application.app_context():
        guarded = SimpleNamespace(id=99, structure_locked_at="already-frozen")
        exclusive_calls: list[int] = []

        def exclusive_guard(encuesta_id):
            exclusive_calls.append(encuesta_id)
            return guarded

        monkeypatch.setattr(
            encuesta_service,
            "_acquire_encuesta_write_guard",
            exclusive_guard,
        )
        monkeypatch.setattr(
            db.session,
            "execute",
            lambda *_args, **_kwargs: pytest.fail(
                "SQLite must not receive PostgreSQL FOR SHARE syntax"
            ),
        )

        result = encuesta_service._acquire_encuesta_response_guard(99)

        assert result is guarded
        assert exclusive_calls == [99]


def test_postgresql_shared_response_guard_maps_lock_timeout_to_retryable_conflict(
    client,
    monkeypatch,
):
    class _PgLockTimeout(Exception):
        pgcode = "55P03"

    with client.application.app_context():
        monkeypatch.setattr(db.session, "get_bind", _postgresql_bind)

        def locked_execute(*_args, **_kwargs):
            raise SAOperationalError(
                "SELECT id FROM enc_encuesta FOR SHARE",
                {},
                _PgLockTimeout("lock not available"),
            )

        monkeypatch.setattr(db.session, "execute", locked_execute)
        with pytest.raises(EncuestaError) as exc_info:
            encuesta_service._acquire_encuesta_response_guard(1)

        assert exc_info.value.status_code == 409
        assert exc_info.value.payload["reason_code"] == "survey_concurrent_update"
        assert exc_info.value.payload["retryable"] is True


def test_postgresql_shared_response_guard_does_not_mask_schema_drift(
    client,
    monkeypatch,
):
    with client.application.app_context():
        monkeypatch.setattr(db.session, "get_bind", _postgresql_bind)

        def broken_schema_execute(*_args, **_kwargs):
            raise SAOperationalError(
                "SELECT id FROM enc_encuesta FOR SHARE",
                {},
                RuntimeError("column structure_locked_at does not exist"),
            )

        monkeypatch.setattr(db.session, "execute", broken_schema_execute)
        with pytest.raises(SAOperationalError):
            encuesta_service._acquire_encuesta_response_guard(1)


def test_public_instrument_revision_rejects_stale_form_without_false_duplicate_ack(client):
    with client.application.app_context():
        encuesta = create_encuesta(_instrument_payload(), _User())
        encuesta, link = publicar_encuesta(encuesta.id, _User())
        stale_public = serialize_public_encuesta(encuesta, link.slug_publico)
        survey_id = encuesta.id
        public_slug = link.slug_publico
        stale_question = stale_public["preguntas"][0]
        assert stale_public["instrument_revision"] == 1
        assert "structure_guard" not in stale_public

        update_payload = _structural_update_payload(encuesta)
        update_payload["expected_structure_revision"] = 1
        updated = update_encuesta(encuesta.id, update_payload, _User())
        assert updated.structure_revision == 2

    stale_submission_id = "stale-public-instrument-0001"
    stale_response = client.post(
        f"/api/public/encuestas/{public_slug}/responder",
        json={
            "submission_id": stale_submission_id,
            "instrument_revision": 1,
            "respuestas": [
                {
                    "pregunta_id": stale_question["id"],
                    "opcion_id": stale_question["opciones"][0]["id"],
                }
            ],
        },
        headers={
            "Idempotency-Key": stale_submission_id,
            "X-Anon-Id": "stale-public-form",
            "X-Forwarded-For": "198.51.100.230",
        },
    )
    assert stale_response.status_code == 409
    stale_error = stale_response.get_json()
    assert stale_error["reason_code"] == "survey_structure_changed"
    assert stale_error["retryable"] is False
    assert stale_error["action_hint"] == "reload_survey"
    assert stale_error.get("duplicate") is not True

    with client.application.app_context():
        current = db.session.get(EncEncuesta, survey_id)
        first, second = current.preguntas
        current_submission_id = "current-public-instrument-0001"
        current_payload = {
            "submission_id": current_submission_id,
            "instrument_revision": 2,
            "respuestas": [
                {
                    "pregunta_id": first.id,
                    "opcion_id": first.opciones[0].id,
                },
                {"pregunta_id": second.id, "texto_libre": "Respuesta actual"},
            ],
        }

    accepted = client.post(
        f"/api/public/encuestas/{public_slug}/responder",
        json=current_payload,
        headers={
            "Idempotency-Key": current_submission_id,
            "X-Anon-Id": "current-public-form",
        },
    )
    assert accepted.status_code == 201
    assert accepted.get_json()["instrument_revision"] == 2


def test_legacy_public_route_maps_only_real_uniqueness_conflict_to_duplicate_200(client):
    with client.application.app_context():
        payload = _instrument_payload("Encuesta con unicidad")
        payload["politica_unicidad"] = "por_cookie"
        encuesta = create_encuesta(payload, _User())
        encuesta, link = publicar_encuesta(encuesta.id, _User())
        public_slug = link.slug_publico
        question = encuesta.preguntas[0]
        response_payload = {
            "instrument_revision": encuesta.structure_revision,
            "respuestas": [
                {
                    "pregunta_id": question.id,
                    "opcion_id": question.opciones[0].id,
                }
            ],
        }

    endpoint = f"/api/public/encuestas/{public_slug}/responder"
    first_submission_id = "legacy-uniqueness-first-0001"
    first_payload = {**response_payload, "submission_id": first_submission_id}
    first_headers = {
        "Idempotency-Key": first_submission_id,
        "X-Anon-Id": "same-public-participant",
    }
    first = client.post(endpoint, json=first_payload, headers=first_headers)
    assert first.status_code == 201
    second_submission_id = "legacy-uniqueness-second-0001"
    second_payload = {**response_payload, "submission_id": second_submission_id}
    second_headers = {
        "Idempotency-Key": second_submission_id,
        "X-Anon-Id": "same-public-participant",
    }
    second = client.post(endpoint, json=second_payload, headers=second_headers)
    assert second.status_code == 200
    duplicate = second.get_json()
    assert duplicate["duplicate"] is True
    assert duplicate["reason_code"] == "survey_response_duplicate"
    assert duplicate["retryable"] is False
