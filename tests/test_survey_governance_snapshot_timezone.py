from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from models import EncEncuesta, EncOpcion, EncPregunta
from services.survey_governance import (
    RELEASE_SNAPSHOT_SCHEMA_V1,
    RELEASE_SNAPSHOT_SCHEMA_V2,
    SurveyGovernanceError,
    _assert_release_snapshot_integrity,
    _canonical_json,
    _sha256_text,
    build_release_snapshot,
)


def _instrument(starts_at: datetime) -> EncEncuesta:
    survey = EncEncuesta(
        id=632,
        tenant_id=77,
        slug="instrumento-gobernado-632",
        titulo="Instrumento gobernado 632",
        descripcion="Snapshot institucional",
        tipo="votacion",
        estado="borrador",
        inicio_at=starts_at,
        structure_revision=1,
        politica_unicidad="por_cookie",
        anonimo_permitido=True,
    )
    question = EncPregunta(
        id=9001,
        orden=1,
        logical_ref="gestion",
        tipo="opcion_unica",
        texto="Pregunta institucional",
        obligatoria=True,
    )
    question.opciones = [
        EncOpcion(
            id=9101,
            orden=1,
            logical_ref="si",
            texto="Si",
            valor="si",
        ),
        EncOpcion(
            id=9102,
            orden=2,
            logical_ref="no",
            texto="No",
            valor="no",
        ),
    ]
    survey.preguntas = [question]
    return survey


def _policy() -> dict:
    return {
        "eligibility": {"policy_version": "eligibility-1"},
        "consent": {"policy_version": "consent-1"},
        "decision_rules": {"human_review_required": True},
    }


def _release(snapshot: dict, *, release_id: int = 44) -> SimpleNamespace:
    snapshot_json = _canonical_json(snapshot)
    return SimpleNamespace(
        id=release_id,
        snapshot_json=snapshot_json,
        snapshot_sha256=_sha256_text(snapshot_json),
    )


def test_v2_snapshot_is_stable_for_same_instant_across_postgres_timezone_reload(
    client,
):
    client.application.config["TIMEZONE_OFFSET"] = -3
    local_start = datetime(
        2026,
        8,
        14,
        9,
        30,
        0,
        123456,
        tzinfo=timezone(timedelta(hours=-3)),
    )
    survey = _instrument(local_start)

    before = build_release_snapshot(survey, _policy())
    survey.inicio_at = local_start.astimezone(timezone.utc)
    after = build_release_snapshot(survey, _policy())

    assert before["schema_version"] == RELEASE_SNAPSHOT_SCHEMA_V2
    assert before["collection_rules"]["starts_at"] == "2026-08-14T12:30:00.123456+00:00"
    assert _canonical_json(before) == _canonical_json(after)
    assert _sha256_text(_canonical_json(before)) == _sha256_text(_canonical_json(after))


def test_v1_auto_start_accepts_only_the_equivalent_postgres_timezone_round_trip(
    client,
):
    client.application.config["TIMEZONE_OFFSET"] = -3
    local_start = datetime(
        2026,
        8,
        14,
        9,
        30,
        tzinfo=timezone(timedelta(hours=-3)),
    )
    survey = _instrument(local_start)
    legacy_snapshot = build_release_snapshot(
        survey,
        _policy(),
        schema_version=RELEASE_SNAPSHOT_SCHEMA_V1,
    )
    release = _release(legacy_snapshot)

    survey.inicio_at = local_start.astimezone(timezone.utc)
    _assert_release_snapshot_integrity(survey, release)

    survey.inicio_at += timedelta(seconds=1)
    with pytest.raises(SurveyGovernanceError) as exc_info:
        _assert_release_snapshot_integrity(survey, release)
    assert exc_info.value.reason_code == "survey_governance_snapshot_drift"


def test_v1_timezone_compatibility_does_not_hide_non_schedule_drift(client):
    client.application.config["TIMEZONE_OFFSET"] = -3
    local_start = datetime(
        2026,
        8,
        14,
        9,
        30,
        tzinfo=timezone(timedelta(hours=-3)),
    )
    survey = _instrument(local_start)
    legacy_snapshot = build_release_snapshot(
        survey,
        _policy(),
        schema_version=RELEASE_SNAPSHOT_SCHEMA_V1,
    )
    release = _release(legacy_snapshot)

    survey.inicio_at = local_start.astimezone(timezone.utc)
    survey.titulo = "Instrumento modificado"
    with pytest.raises(SurveyGovernanceError) as exc_info:
        _assert_release_snapshot_integrity(survey, release)
    assert exc_info.value.reason_code == "survey_governance_snapshot_drift"


def test_unknown_snapshot_schema_fails_closed(client):
    survey = _instrument(datetime(2026, 8, 14, 12, 30, tzinfo=timezone.utc))
    snapshot = build_release_snapshot(survey, _policy())
    snapshot["schema_version"] = "surveys.release_snapshot.v999"
    release = _release(snapshot)

    with pytest.raises(SurveyGovernanceError) as exc_info:
        _assert_release_snapshot_integrity(survey, release)
    assert (
        exc_info.value.reason_code
        == "survey_governance_snapshot_schema_unsupported"
    )
