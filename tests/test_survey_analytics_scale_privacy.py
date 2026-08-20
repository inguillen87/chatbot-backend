from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import Numeric, cast, event, func, select
from sqlalchemy.dialects import postgresql

from database import db
from models import (
    EncEncuesta,
    EncLink,
    EncOpcion,
    EncPregunta,
    EncRespuesta,
    EncRespuestaDetalle,
    MunicipioTicket,
    TenantProfile,
    User,
)
from services import encuestas_analytics_service as analytics
from services import encuestas_service
from routes import admin_tenant as admin_tenant_routes
from routes import encuestas_public as public_routes
from routes.v2 import surveys as v2_survey_routes


@contextmanager
def _captured_selects():
    statements: list[str] = []
    engine = db.engine

    def _capture(_conn, _cursor, statement, _parameters, _context, _many):
        normalized = " ".join(str(statement).lower().split())
        if normalized.startswith("select"):
            statements.append(normalized)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", _capture)


def _create_survey(
    *,
    slug: str = "scale-privacy",
    privacy_mode: str = "legacy",
):
    owner = User(
        name=f"Owner {slug}",
        email=f"owner-{slug}@test.com",
        rol="admin",
        tipo_chat="municipio",
    )
    owner.set_password("safe-password")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug=f"tenant-{slug}",
        nombre=f"Tenant {slug}",
        tipo="municipio",
        plan="enterprise",
        municipio_id=owner.id,
    )
    db.session.add(tenant)
    db.session.flush()
    survey = EncEncuesta(
        tenant_id=tenant.id,
        slug=slug,
        titulo="Pulso de escala y privacidad",
        estado="publicada",
        tipo="votacion",
        es_votacion_envivo=True,
        mostrar_resultados_envivo=True,
        privacy_mode=privacy_mode,
        privacy_policy_version=(
            "2026-08-source-anonymous" if privacy_mode == "source_anonymous" else None
        ),
        privacy_policy_url=(
            "https://example.test/privacy" if privacy_mode == "source_anonymous" else None
        ),
        privacy_consent_required=privacy_mode == "source_anonymous",
        response_retention_days=365 if privacy_mode == "source_anonymous" else None,
    )
    question = EncPregunta(
        encuesta=survey,
        orden=1,
        tipo="opcion_unica",
        texto="¿Está de acuerdo?",
        obligatoria=True,
    )
    option = EncOpcion(
        pregunta=question,
        orden=1,
        texto="Sí",
        valor="si",
    )
    db.session.add(survey)
    db.session.flush()
    return tenant, survey, question, option


def _add_response(
    *,
    tenant_id: int,
    survey_id: int,
    question_id: int,
    option_id: int,
    index: int,
    origin: str = "real",
    lat: float | None = None,
    lng: float | None = None,
    channel: str = "web",
):
    response = EncRespuesta(
        tenant_id=tenant_id,
        encuesta_id=survey_id,
        response_origin=origin,
        huella_unica=f"scale-response-{survey_id}-{origin}-{index}",
        submitted_at=datetime.now(timezone.utc),
        canal=channel,
        barrio="Centro",
        ciudad="Ushuaia",
        lat=lat,
        lng=lng,
    )
    db.session.add(response)
    db.session.flush()
    db.session.add(
        EncRespuestaDetalle(
            respuesta_id=response.id,
            pregunta_id=question_id,
            opcion_id=option_id,
        )
    )
    return response


def _add_public_link(survey: EncEncuesta, *, token: str | None = None) -> EncLink:
    link = EncLink(
        encuesta_id=survey.id,
        slug_publico=token or survey.slug,
        canal="web",
    )
    db.session.add(link)
    db.session.flush()
    return link


def test_public_geo_hard_k_suppresses_one_through_four_and_allows_five():
    for count in range(1, 5):
        points, cells, metadata = analytics._prepare_live_heatmap_payload(
            [],
            [
                {
                    "cell_id": "private-cell",
                    "count": count,
                    "centroid_lat": -54.801912,
                    "centroid_lon": -68.302951,
                    "barrios": {"Centro": count},
                    "canales": {"whatsapp": count},
                }
            ],
            geo_privacy="public_aggregated",
        )
        assert points == []
        assert cells == []
        assert metadata["minimum_cell_size"] == 5
        assert "raw_points_count" not in metadata

    points, cells, metadata = analytics._prepare_live_heatmap_payload(
        [],
        [
            {
                "cell_id": "publishable-cell",
                "count": 5,
                "centroid_lat": -54.801912,
                "centroid_lon": -68.302951,
                "barrios": {"Centro": 5},
                "canales": {"whatsapp": 5},
            }
        ],
        geo_privacy="public_aggregated",
    )
    assert len(points) == 1
    assert len(cells) == 1
    assert points[0]["count"] == 5
    assert cells[0]["count"] == 5
    assert "barrio" not in points[0]
    assert "canal" not in points[0]
    assert "barrios" not in cells[0]
    assert "canales" not in cells[0]
    assert "raw_points_count" not in metadata


def test_summary_reports_exact_population_with_bounded_projected_sample(client):
    tenant, survey, question, option = _create_survey(slug="bounded-summary")
    for index in range(510):
        _add_response(
            tenant_id=tenant.id,
            survey_id=survey.id,
            question_id=question.id,
            option_id=option.id,
            index=index,
        )
    db.session.commit()

    summary = analytics.get_summary(survey.id)

    assert summary["total_respuestas"] == 510
    assert summary["preguntas"][0]["opciones"][0]["conteo"] == 510
    provenance = summary["data_provenance"]
    assert provenance["population_size"] == 510
    assert provenance["sample_size"] == 500
    assert provenance["sampled"] is True
    assert provenance["partial"] is True
    assert provenance["exact_aggregates"] is True


def test_live_results_use_exact_sql_geo_k_and_stable_etag(client):
    tenant, survey, question, option = _create_survey(slug="live-k-etag")
    for index in range(4):
        _add_response(
            tenant_id=tenant.id,
            survey_id=survey.id,
            question_id=question.id,
            option_id=option.id,
            index=index,
            lat=-54.80191,
            lng=-68.30295,
        )
    db.session.commit()

    first = client.get(f"/api/public/encuestas/{survey.slug}/live-results")
    assert first.status_code == 200
    first_payload = first.get_json()
    assert first_payload["total_respuestas"] == 4
    assert first_payload["data_provenance"]["raw_responses_materialized"] == 0
    assert first_payload["heatmap"]["points"] == []
    assert first_payload["heatmap"]["cells"] == []
    assert "raw_points_count" not in first_payload["heatmap"]["metadata"]

    _add_response(
        tenant_id=tenant.id,
        survey_id=survey.id,
        question_id=question.id,
        option_id=option.id,
        index=5,
        lat=-54.80191,
        lng=-68.30295,
    )
    db.session.commit()

    second = client.get(f"/api/public/encuestas/{survey.slug}/live-results")
    assert second.status_code == 200
    second_payload = second.get_json()
    assert second_payload["total_respuestas"] == 5
    assert second_payload["preguntas"][0]["total_votos"] == 5
    assert len(second_payload["heatmap"]["cells"]) == 1
    assert second_payload["heatmap"]["cells"][0]["count"] == 5
    etag = second.headers["ETag"]
    assert etag

    not_modified = client.get(
        f"/api/public/encuestas/{survey.slug}/live-results",
        headers={"If-None-Match": etag},
    )
    assert not_modified.status_code == 304
    assert not not_modified.data

    # Presentation edits must invalidate both the process cache and the HTTP
    # validator even when no response was added.
    option.texto = "Sí, actualizado"
    db.session.commit()
    presentation_changed = client.get(
        f"/api/public/encuestas/{survey.slug}/live-results",
        headers={"If-None-Match": etag},
    )
    assert presentation_changed.status_code == 200
    assert presentation_changed.headers["ETag"] != etag
    assert (
        presentation_changed.get_json()["preguntas"][0]["opciones"][0]["label"]
        == "Sí, actualizado"
    )


def test_source_anonymous_zero_through_four_share_one_public_etag(client):
    tenant, survey, question, option = _create_survey(
        slug="anonymous-etag-floor",
        privacy_mode="source_anonymous",
    )
    db.session.commit()

    responses = []
    etags = []
    for target_count in (0, 1, 4):
        while len(responses) < target_count:
            responses.append(
                _add_response(
                    tenant_id=tenant.id,
                    survey_id=survey.id,
                    question_id=question.id,
                    option_id=option.id,
                    index=len(responses),
                    lat=-54.80191,
                    lng=-68.30295,
                )
            )
        db.session.commit()
        response = client.get(
            f"/api/public/encuestas/{survey.slug}/live-results"
        )
        assert response.status_code == 200, response.get_json()
        payload = response.get_json()
        etags.append(response.headers["ETag"])
        assert payload["total_respuestas"] is None
        assert payload["total_respuestas_bucket"] == "withheld_until_close"
        assert payload["data_provenance"]["population_size"] is None
        assert payload["data_provenance"]["real_responses_included"] is None
        assert payload["data_provenance"]["sample_size"] is None
        assert payload["timeline_metadata"]["bucket_count"] is None
        assert payload["live_telemetry"]["has_responses"] is None
        assert payload["empty_state"]["is_empty"] is None
        assert payload["heatmap"]["metadata"]["aggregation"].get(
            "geo_response_count"
        ) is None

    assert len(set(etags)) == 1

    _add_response(
        tenant_id=tenant.id,
        survey_id=survey.id,
        question_id=question.id,
        option_id=option.id,
        index=4,
        lat=-54.80191,
        lng=-68.30295,
    )
    _add_response(
        tenant_id=tenant.id,
        survey_id=survey.id,
        question_id=question.id,
        option_id=option.id,
        index=100,
        origin="synthetic_demo",
        lat=-54.80191,
        lng=-68.30295,
    )
    _add_response(
        tenant_id=tenant.id,
        survey_id=survey.id,
        question_id=question.id,
        option_id=option.id,
        index=101,
        origin="legacy_unverified",
        lat=-54.80191,
        lng=-68.30295,
    )
    db.session.commit()
    still_active = client.get(
        f"/api/public/encuestas/{survey.slug}/live-results",
        headers={"If-None-Match": etags[0]},
    )
    assert still_active.status_code == 304

    survey.estado = "cerrada"
    db.session.commit()
    released = client.get(
        f"/api/public/encuestas/{survey.slug}/live-results",
        headers={"If-None-Match": etags[0]},
    )
    assert released.status_code == 200, released.get_json()
    released_payload = released.get_json()
    assert released.headers["ETag"] != etags[0]
    assert released_payload["total_respuestas"] == 5
    assert released_payload.get("total_respuestas_bucket") is None
    assert released_payload["preguntas"][0]["total_votos"] == 5
    assert released_payload["preguntas"][0]["opciones"][0]["votos"] == 5
    assert released_payload["data_provenance"]["real_responses_included"] == 5
    assert released_payload["data_provenance"]["synthetic_responses_excluded"] == 1
    assert released_payload["data_provenance"]["unverified_responses_excluded"] == 1
    assert released_payload["privacy"]["results_final"] is True

    stable_final = client.get(
        f"/api/public/encuestas/{survey.slug}/live-results",
        headers={"If-None-Match": released.headers["ETag"]},
    )
    assert stable_final.status_code == 304
    assert not stable_final.data

    modern_final = client.get(
        f"/api/v2/public/surveys/{survey.slug}/live-results?include_heatmap=0"
    )
    assert modern_final.status_code == 200, modern_final.get_json()
    assert modern_final.get_json()["total_respuestas"] == 5


def test_closed_source_anonymous_below_k_is_readable_but_fully_suppressed(client):
    tenant, survey, question, option = _create_survey(
        slug="anonymous-final-below-k",
        privacy_mode="source_anonymous",
    )
    for index in range(4):
        _add_response(
            tenant_id=tenant.id,
            survey_id=survey.id,
            question_id=question.id,
            option_id=option.id,
            index=index,
            lat=-54.80191,
            lng=-68.30295,
        )
    _add_public_link(survey)
    survey.estado = "cerrada"
    db.session.commit()

    live_paths = (
        f"/api/public/encuestas/v1/{survey.slug}/live-results?include_heatmap=0",
        f"/api/v2/public/surveys/{survey.slug}/live-results?include_heatmap=0",
    )
    for path in live_paths:
        response = client.get(path)
        assert response.status_code == 200, response.get_json()
        payload = response.get_json()
        assert payload["total_respuestas"] is None
        assert payload["total_respuestas_bucket"] == "<5"
        assert payload["preguntas"][0]["total_votos"] is None
        assert payload["preguntas"][0]["opciones"][0]["votos"] is None
        assert payload["timeline_minute"] == []
        assert payload["heatmap"]["cells"] == []
        assert payload["data_provenance"]["population_size"] is None
        assert payload["data_provenance"]["real_responses_included"] is None
        assert payload["privacy"]["results_final"] is True
        assert payload["privacy"]["reason_code"] == "minimum_cell_size_not_met"

    detail_paths = (
        f"/api/public/encuestas/v1/{survey.slug}",
        f"/api/v2/public/surveys/{survey.slug}",
    )
    for path in detail_paths:
        response = client.get(path)
        assert response.status_code == 200, response.get_json()
        results = response.get_json()["resultados_envivo"]
        assert results["total_respuestas"] is None
        assert results["total_respuestas_bucket"] == "<5"
        assert results["privacy"]["results_final"] is True

    wrong_tenant, _other_survey, _other_question, _other_option = _create_survey(
        slug="anonymous-final-other-tenant"
    )
    db.session.commit()
    denied_read_paths = (
        f"/api/public/encuestas/v1/{survey.slug}?tenant_slug={wrong_tenant.slug}",
        f"/api/public/encuestas/v1/{survey.slug}/live-results?tenant_slug={wrong_tenant.slug}",
        f"/api/v2/public/surveys/{survey.slug}?tenant_slug={wrong_tenant.slug}",
        f"/api/v2/public/surveys/{survey.slug}/live-results?tenant_slug={wrong_tenant.slug}",
        f"/api/v2/public/surveys/{survey.slug}-abcdef",
        f"/api/public/encuestas/v1/{survey.id}/live-results",
    )
    for path in denied_read_paths:
        denied = client.get(path)
        assert denied.status_code == 404, (path, denied.get_json())
        assert denied.get_json()["reason_code"] == "survey_not_found"

    for method, path in (
        ("get", f"/api/public/encuestas/v1/{survey.slug}/comentarios"),
        ("post", f"/api/public/encuestas/v1/{survey.slug}/comentarios"),
    ):
        denied_comment = getattr(client, method)(
            path,
            json={"texto": "No debe persistirse"} if method == "post" else None,
        )
        assert denied_comment.status_code == 403, denied_comment.get_json()
        assert denied_comment.get_json()["reason_code"] == "survey_not_published"

    client.application.config["CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE"] = "false"
    response_count_before = EncRespuesta.query.filter_by(encuesta_id=survey.id).count()
    for index, path in enumerate(
        (
            f"/api/public/encuestas/v1/{survey.slug}/responder",
            f"/api/v2/public/surveys/{survey.slug}/respond",
        ),
        start=1,
    ):
        submission_id = f"closed-survey-new-response-{index}"
        denied = client.post(
            path,
            json={
                "submission_id": submission_id,
                "anon_id": f"closed-citizen-{index}",
                "privacy_consent": True,
                "privacy_policy_version": survey.privacy_policy_version,
                "respuestas": [
                    {"pregunta_id": question.id, "opcion_ids": [option.id]}
                ],
            },
            headers={"Idempotency-Key": submission_id},
        )
        assert denied.status_code == 403, denied.get_json()
        assert denied.get_json()["reason_code"] == "survey_not_published"
    assert EncRespuesta.query.filter_by(encuesta_id=survey.id).count() == response_count_before


def test_public_geo_pipeline_hides_visible_plus_suppressed_remainder(client):
    tenant, survey, question, option = _create_survey(
        slug="geo-seven-plus-four",
        privacy_mode="source_anonymous",
    )
    for index in range(11):
        _add_response(
            tenant_id=tenant.id,
            survey_id=survey.id,
            question_id=question.id,
            option_id=option.id,
            index=index,
            lat=-54.80191 if index < 7 else -53.78610,
            lng=-68.30295 if index < 7 else -67.70020,
        )
    db.session.commit()

    response = client.get(f"/api/public/encuestas/{survey.slug}/live-results")
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["total_respuestas"] is None
    assert payload["total_respuestas_bucket"] == "withheld_until_close"
    assert "total_respuestas_lower_bound" not in payload
    assert payload["preguntas"][0]["total_votos"] is None
    assert payload["timeline_minute"] == []
    assert payload["heatmap"]["cells"] == []
    assert payload["data_provenance"]["population_size"] is None
    aggregation = payload["heatmap"]["metadata"]["aggregation"]
    assert aggregation["geo_response_count"] is None
    assert aggregation["suppressed_response_count"] is None
    assert payload["privacy"]["geo_remainder_protected"] is None


def test_source_anonymous_public_filters_fail_closed_before_cache(client):
    tenant, survey, question, option = _create_survey(
        slug="anonymous-filter-boundary",
        privacy_mode="source_anonymous",
    )
    for index in range(6):
        _add_response(
            tenant_id=tenant.id,
            survey_id=survey.id,
            question_id=question.id,
            option_id=option.id,
            index=index,
        )
    db.session.commit()

    with patch.object(analytics, "_public_live_cache_get") as cache_get:
        legacy = client.get(
            f"/api/public/encuestas/{survey.slug}/live-results"
            "?desde=2026-08-01T00:00:00Z&hasta=2026-08-02T00:00:00Z"
        )
        modern = client.get(
            f"/api/v2/public/surveys/{survey.slug}/live-results?canal=web"
        )
    assert legacy.status_code == 400, legacy.get_json()
    assert modern.status_code == 400, modern.get_json()
    assert legacy.get_json()["reason_code"] == "privacy_filters_not_available"
    assert modern.get_json()["reason_code"] == "privacy_filters_not_available"
    cache_get.assert_not_called()

    # Unknown segmentation aliases are ignored and therefore cannot select a
    # different public cohort.
    canonical = client.get(
        f"/api/public/encuestas/{survey.slug}/live-results?bbox=1,2,3,4&channel=api"
    )
    assert canonical.status_code == 200, canonical.get_json()
    assert canonical.get_json()["total_respuestas"] is None
    assert (
        canonical.get_json()["total_respuestas_bucket"]
        == "withheld_until_close"
    )


def test_live_rolling_preset_uses_one_canonical_cache_bucket(client):
    tenant, survey, question, option = _create_survey(slug="preset-cache-bucket")
    for index in range(5):
        _add_response(
            tenant_id=tenant.id,
            survey_id=survey.id,
            question_id=question.id,
            option_id=option.id,
            index=index,
        )
    db.session.commit()
    fixed = datetime(2026, 8, 20, 15, 0, 1, 125000, tzinfo=timezone.utc)

    with patch.object(analytics, "_utc_now", return_value=fixed), patch.object(
        analytics,
        "_exact_option_statistics",
        wraps=analytics._exact_option_statistics,
    ) as aggregate:
        first = client.get(
            f"/api/public/encuestas/{survey.slug}/live-results?range_preset=last_60m"
        )
        second = client.get(
            f"/api/public/encuestas/{survey.slug}/live-results?range_preset=last_60m"
        )
    assert first.status_code == second.status_code == 200
    assert first.headers["ETag"] == second.headers["ETag"]
    assert aggregate.call_count == 1

    with patch.object(analytics, "_utc_now", return_value=fixed + timedelta(seconds=5)):
        next_bucket = client.get(
            f"/api/public/encuestas/{survey.slug}/live-results",
            query_string={"range_preset": "last_60m"},
            headers={"If-None-Match": first.headers["ETag"]},
        )
    assert next_bucket.status_code == 200
    assert next_bucket.headers["ETag"] != first.headers["ETag"]


def test_admin_panel_stats_are_exact_and_exclude_non_real_origins(client):
    tenant, survey, question, option = _create_survey(slug="admin-stats-origin")
    for index, origin in enumerate(
        ["real", "real", "synthetic_demo", "legacy_unverified"]
    ):
        _add_response(
            tenant_id=tenant.id,
            survey_id=survey.id,
            question_id=question.id,
            option_id=option.id,
            index=index,
            origin=origin,
            lat=-54.8,
            lng=-68.3,
        )
    db.session.commit()

    stats = encuestas_service._collect_admin_panel_stats([survey])[survey.id]

    assert stats["total_respuestas"] == 2
    assert stats["participantes_unicos"] == 2
    assert stats["respuestas_con_coordenadas"] == 2
    assert stats["synthetic_responses_excluded"] == 1
    assert stats["unverified_responses_excluded"] == 1


def test_channel_frequencies_are_top_n_with_exact_other_bucket(client):
    tenant, survey, question, option = _create_survey(slug="channel-cardinality")
    for index in range(250):
        _add_response(
            tenant_id=tenant.id,
            survey_id=survey.id,
            question_id=question.id,
            option_id=option.id,
            index=index,
            channel=f"Custom-Channel-{index:03d}",
        )
    db.session.commit()

    with _captured_selects() as summary_selects:
        summary = analytics.get_summary(survey.id)
    metadata = summary["canales_metadata"]
    assert metadata == {
        "top_limit": 20,
        "distinct_total": 250,
        "returned_distinct": 20,
        "truncated": True,
        "other_count": 230,
    }
    assert summary["canales_map"]["otros"] == 230
    assert len(summary["canales_map"]) == 21
    assert sum(summary["canales_map"].values()) == 250
    channel_group_queries = [
        statement
        for statement in summary_selects
        if "enc_respuesta.canal" in statement and "group by" in statement
    ]
    assert channel_group_queries
    assert any(" limit " in statement for statement in channel_group_queries)

    with _captured_selects() as admin_selects:
        admin_stats = encuestas_service._collect_admin_panel_stats([survey])[survey.id]
    assert admin_stats["canales"]["otros"] == 230
    assert len(admin_stats["canales"]) == 21
    assert admin_stats["canales_metadata"]["distinct_total"] == 250
    assert admin_stats["canales_metadata"]["truncated"] is True
    ranked_queries = [
        statement
        for statement in admin_selects
        if "enc_respuesta.canal" in statement and "row_number() over" in statement
    ]
    assert ranked_queries
    assert any("rank <=" in statement for statement in ranked_queries)


def test_postgresql_geo_rounding_casts_float_columns_to_numeric(client):
    compiled = str(
        select(
            func.round(cast(EncRespuesta.lat, Numeric), 3),
            func.round(cast(EncRespuesta.lng, Numeric), 3),
            func.round(cast(MunicipioTicket.latitud, Numeric), 3),
            func.round(cast(MunicipioTicket.longitud, Numeric), 3),
        ).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "round(CAST(enc_respuesta.lat AS NUMERIC), 3)" in compiled
    assert "round(CAST(municipio_ticket.latitud AS NUMERIC), 3)" in compiled
    assert not re.search(
        r"round\((?:enc_respuesta|municipio_ticket)\.(?:lat|lng|latitud|longitud),\s*\d+\)",
        compiled,
        re.IGNORECASE,
    )

    repository = Path(__file__).resolve().parents[1]
    expected_sql_round_calls = {
        repository / "services" / "encuestas_analytics_service.py": 4,
        repository / "routes" / "admin_tenant.py": 2,
    }
    for source_path, minimum_calls in expected_sql_round_calls.items():
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        sql_round_calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            call_target = node.func
            if not (
                isinstance(call_target, ast.Attribute)
                and call_target.attr == "round"
                and (
                    (
                        isinstance(call_target.value, ast.Attribute)
                        and call_target.value.attr == "func"
                    )
                    or (
                        isinstance(call_target.value, ast.Name)
                        and call_target.value.id == "func"
                    )
                )
            ):
                continue
            sql_round_calls.append(node)
            first_argument = node.args[0]
            assert isinstance(first_argument, ast.Call), source_path
            assert isinstance(first_argument.func, ast.Name), source_path
            assert first_argument.func.id == "cast", source_path
            assert len(first_argument.args) >= 2, source_path
            assert isinstance(first_argument.args[1], ast.Name), source_path
            assert first_argument.args[1].id == "Numeric", source_path
        assert len(sql_round_calls) >= minimum_calls, source_path


def test_public_live_rate_limit_key_ignores_forwarding_headers(app, client):
    peer = "198.18.0.77"
    keys = []
    for index in range(3):
        with app.test_request_context(
            "/",
            environ_base={"REMOTE_ADDR": peer},
            headers={
                "X-Forwarded-For": f"203.0.113.{index + 1}",
                "CF-Connecting-IP": f"198.51.100.{index + 1}",
            },
        ):
            keys.append(public_routes._public_live_results_rate_limit_key())
            keys.append(v2_survey_routes._public_live_results_rate_limit_key())
    assert set(keys) == {peer}

    _tenant, survey, _question, _option = _create_survey(
        slug="live-rate-transport-peer"
    )
    db.session.commit()
    statuses = []
    for index in range(61):
        response = client.get(
            f"/api/public/encuestas/{survey.slug}/live-results",
            environ_base={"REMOTE_ADDR": peer},
            headers={
                "X-Forwarded-For": f"203.0.113.{(index % 200) + 1}",
                "CF-Connecting-IP": f"198.51.100.{(index % 200) + 1}",
            },
        )
        statuses.append(response.status_code)
    assert statuses[0] == 200
    assert statuses[-1] == 429
    assert 429 in statuses


def test_source_anonymous_detail_and_ack_redact_counts_until_k(client):
    tenant, survey, question, option = _create_survey(
        slug="anonymous-detail-ack",
        privacy_mode="source_anonymous",
    )
    responses = []
    for index in range(4):
        responses.append(
            _add_response(
                tenant_id=tenant.id,
                survey_id=survey.id,
                question_id=question.id,
                option_id=option.id,
                index=index,
            )
        )
    db.session.commit()

    legacy_detail = client.get(f"/api/public/encuestas/{survey.slug}")
    modern_detail = client.get(f"/api/v2/public/surveys/{survey.slug}")
    assert legacy_detail.status_code == modern_detail.status_code == 200
    for payload in (legacy_detail.get_json(), modern_detail.get_json()):
        results = payload["resultados_envivo"]
        assert results["total_respuestas"] is None
        assert results["total_respuestas_bucket"] == "withheld_until_close"
        assert results["data_provenance"]["population_size"] is None
        assert results["data_provenance"]["real_responses_included"] is None
    modern_payload = modern_detail.get_json()
    assert modern_payload["operations"]["responses_count"] is None
    assert modern_payload["admin_operations"]["responses_count"] is None
    assert (
        modern_payload["response_count_privacy"]["bucket"]
        == "withheld_until_close"
    )

    receipt = responses[-1]
    receipt.submission_replayed = False
    ack = v2_survey_routes._build_public_survey_response_ack(
        survey.slug,
        receipt,
    )
    assert ack["operations"]["responses_count"] is None
    assert ack["admin_operations"]["responses_count"] is None
    assert ack["response_count_privacy"]["bucket"] == "withheld_until_close"
    receipt.submission_replayed = True
    replay_ack = v2_survey_routes._build_public_survey_response_ack(
        survey.slug,
        receipt,
    )
    assert replay_ack["replayed"] is True
    assert replay_ack["operations"]["responses_count"] is None

    survey.mostrar_resultados_envivo = False
    db.session.commit()
    live_off = client.get(f"/api/v2/public/surveys/{survey.slug}")
    assert live_off.status_code == 200
    assert "resultados_envivo" not in live_off.get_json()
    assert live_off.get_json()["operations"]["responses_count"] is None

    _add_response(
        tenant_id=tenant.id,
        survey_id=survey.id,
        question_id=question.id,
        option_id=option.id,
        index=4,
    )
    survey.mostrar_resultados_envivo = True
    db.session.commit()
    still_active = client.get(f"/api/v2/public/surveys/{survey.slug}")
    assert still_active.status_code == 200
    assert still_active.get_json()["resultados_envivo"]["total_respuestas"] is None
    assert still_active.get_json()["operations"]["responses_count"] is None

    survey.estado = "cerrada"
    db.session.commit()
    released = client.get(f"/api/v2/public/surveys/{survey.slug}")
    assert released.status_code == 200, released.get_json()
    released_payload = released.get_json()
    assert released_payload["resultados_envivo"]["total_respuestas"] == 5
    assert released_payload["resultados_envivo"]["total_respuestas_bucket"] is None
    assert released_payload["resultados_envivo"]["privacy"]["results_final"] is True
    assert released_payload["operations"]["responses_count"] == 5
    assert released_payload["response_count_privacy"]["results_final"] is True


def test_source_anonymous_socket_is_silent_below_k_and_safe_at_release(client):
    tenant, survey, question, option = _create_survey(
        slug="anonymous-socket-floor",
        privacy_mode="source_anonymous",
    )
    for index in range(4):
        _add_response(
            tenant_id=tenant.id,
            survey_id=survey.id,
            question_id=question.id,
            option_id=option.id,
            index=index,
        )
    db.session.commit()
    envelope = {
        "contract_version": "survey-response.effect.v1",
        "event_id": "evt-private-4",
        "event_name": "survey.response.created",
        "tenant_id": tenant.id,
        "survey_id": survey.id,
        "response_id": 999,
        "slug": survey.slug,
    }

    with patch("services.encuestas_service.emit_survey_update") as emitter:
        emitted = encuestas_service.emit_survey_response_update(
            survey,
            survey.slug,
            event_envelope=envelope,
        )
    assert emitted is False
    emitter.assert_not_called()

    _add_response(
        tenant_id=tenant.id,
        survey_id=survey.id,
        question_id=question.id,
        option_id=option.id,
        index=4,
    )
    db.session.commit()
    with patch("services.encuestas_service.emit_survey_update") as emitter:
        emitted = encuestas_service.emit_survey_response_update(
            survey,
            survey.slug,
            event_envelope={**envelope, "event_id": "evt-release-5"},
        )
    assert emitted is False
    emitter.assert_not_called()

    with patch(
        "services.encuestas_analytics_service.calculate_live_results",
        side_effect=RuntimeError("analytics unavailable"),
    ), patch("services.encuestas_service.emit_survey_update") as emitter:
        emitted = encuestas_service.emit_survey_response_update(
            survey,
            survey.slug,
            event_envelope=envelope,
        )
    assert emitted is False
    emitter.assert_not_called()


def test_admin_tenant_bundle_query_count_is_constant_and_items_are_bounded(client):
    tenant, _survey, _question, _option = _create_survey(slug="bundle-query-shape")
    owner = db.session.get(User, tenant.municipio_id)
    now = datetime.now(timezone.utc)
    db.session.add(
        MunicipioTicket(
            tenant_id=tenant.id,
            municipio_id=owner.id,
            pregunta="Caso inicial",
            estado="nuevo",
            fecha=now,
            ultima_actividad=now,
        )
    )
    db.session.commit()

    with _captured_selects() as baseline_selects:
        baseline = admin_tenant_routes._build_tenant_dashboard_bundle_payload(
            tenant,
            viewer=owner,
            leads_limit=5,
        )
    assert baseline["summary"]["total_leads"] == 1

    for index in range(20):
        db.session.add(
            MunicipioTicket(
                tenant_id=tenant.id,
                municipio_id=owner.id,
                pregunta=f"Caso {index}",
                estado="nuevo",
                fecha=now,
                ultima_actividad=now,
            )
        )
    db.session.commit()

    with _captured_selects() as scaled_selects:
        scaled = admin_tenant_routes._build_tenant_dashboard_bundle_payload(
            tenant,
            viewer=owner,
            leads_limit=5,
        )

    assert scaled["summary"]["total_leads"] == 21
    assert scaled["leads"]["total"] == 21
    assert len(scaled["leads"]["items"]) == 5
    assert scaled["leads"]["items_partial"] is True
    assert len(scaled_selects) <= len(baseline_selects) + 2
    candidate_selects = [
        statement
        for statement in scaled_selects
        if "from municipio_ticket" in statement
        and "group by" not in statement
        and "count(" not in statement
    ]
    assert any(" limit " in statement for statement in candidate_selects)


def test_admin_tenant_heatmap_uses_exact_total_and_bounded_ticket_projection(client):
    tenant, _survey, _question, _option = _create_survey(slug="heatmap-query-shape")
    owner = db.session.get(User, tenant.municipio_id)
    now = datetime.now(timezone.utc)
    for index in range(12):
        db.session.add(
            MunicipioTicket(
                tenant_id=tenant.id,
                municipio_id=owner.id,
                pregunta=f"Caso territorial {index}",
                categoria="Limpieza",
                distrito=f"Zona {index % 3}",
                estado="nuevo",
                fecha=now,
                ultima_actividad=now,
                latitud=-54.8 + (index * 0.001),
                longitud=-68.3 - (index * 0.001),
            )
        )
    db.session.commit()

    with _captured_selects() as statements:
        payload = admin_tenant_routes._build_tenant_heatmap_summary_payload(
            tenant,
            viewer=owner,
            limit_points=5,
        )

    assert payload["total"] == 12
    assert len(payload["heatmap_points"]) == 5
    assert payload["materialization"] == {
        "point_limit": 5,
        "points_returned": 5,
        "partial": True,
        "ticket_entities_materialized": 0,
    }
    projected_ticket_selects = [
        statement
        for statement in statements
        if "from municipio_ticket" in statement
        and "group by" not in statement
        and "count(" not in statement
        and "municipio_ticket.latitud" in statement
    ]
    assert projected_ticket_selects
    assert all(" limit " in statement for statement in projected_ticket_selects)
    assert all(
        "municipio_ticket.pregunta" not in statement
        for statement in projected_ticket_selects
    )
