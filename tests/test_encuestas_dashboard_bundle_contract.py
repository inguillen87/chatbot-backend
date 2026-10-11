import pytest

from database import db
from models import EncEncuesta, EncOpcion, EncPregunta, EncRespuesta, EncRespuestaDetalle
from services import encuestas_analytics_service as svc
from tests.test_survey_admin_lifecycle_contract import _tenant


@pytest.fixture(autouse=True)
def persisted_survey_context(client):
    """Local SQLite rows provide lifecycle authority for the mocked analytics."""
    _, tenant = _tenant("bundle-local-fixture")
    db.session.add_all([
        EncEncuesta(id=survey_id, tenant_id=tenant.id, slug=f"bundle-{survey_id}",
                    titulo="Local bundle fixture", estado="publicada", tipo="opinion")
        for survey_id in (84, 100, 101, 77, 99)
    ])
    db.session.commit()


def test_dashboard_bundle_includes_normalized_cards_and_states(monkeypatch):
    monkeypatch.setattr(svc, "get_summary", lambda encuesta_id, filtros=None: {
        "total_respuestas": 12,
        "participantes_unicos": 10,
        "tasa_completitud": "87.5",
        "demografia": {},
        "preguntas": [],
    })
    monkeypatch.setattr(svc, "get_timeseries", lambda encuesta_id, granularity="day", filtros=None: [{"fecha": "2026-01-01", "total": 2}])
    monkeypatch.setattr(
        svc,
        "get_heatmap",
        lambda encuesta_id, filtros=None: {
            "points": [{"lat": -33.1, "lng": -68.8, "weight": 1}],
            "cells": [{"lat": -33.1, "lng": -68.8, "count": 1}],
            "headline": "Mapa con participacion territorial.",
            "legend": {"mode": "category_weight"},
            "empty_state": "Sin puntos geograficos publicados",
            "recommended_action": {"label": "Ver mapa", "route": "/admin/encuestas/84/analytics/heatmap"},
            "metadata": {
                "map": {"hotspots": [{"label": "Centro", "count": 1}]},
                "category_layers": {
                    "provider": "maplibre",
                    "source_options": {"cluster": True},
                    "telemetry": {"event_endpoint": "/api/analytics/event"},
                    "categories": [{"categoria": "seguridad", "total_weight": 2}],
                },
            },
        },
    )
    monkeypatch.setattr(svc, "get_forecast", lambda encuesta_id, filtros=None: {"projected_total": 20})
    monkeypatch.setattr(svc, "get_alerts", lambda encuesta_id, filtros=None: {"alerts": [{"id": "a1"}]})
    monkeypatch.setattr(svc, "get_executive_brief", lambda encuesta_id, filtros=None: {"headline": "ok"})
    monkeypatch.setattr(svc, "get_anomaly_report", lambda encuesta_id, filtros=None: {"risk_score": "0.331"})
    monkeypatch.setattr(svc, "get_segment_compare", lambda encuesta_id, filtros=None, segment_a=None, segment_b=None: {"segment_a": {"stats": {"total_respuestas": 7}}, "segment_b": {"stats": {"total_respuestas": 5}}})
    monkeypatch.setattr(svc, "_build_latest_responses_preview", lambda encuesta_id, filtros=None, limit=10: [{"id": 1}])
    monkeypatch.setattr(
        svc,
        "_build_survey_publication_contract",
        lambda encuesta_id: {
            "contract_version": "surveys.dashboard_publication.v1",
            "public_state": "published",
            "links": {"live_results_endpoint": "/api/v2/public/surveys/demo/live-results"},
            "actions": [{"id": "open_live_results"}],
        },
    )

    bundle = svc.get_dashboard_bundle(84, {"canal": "web"})

    assert bundle["meta"]["schema_version"] == "2026.03"
    assert bundle["meta"]["module_state"]["alerts"] == "attention"
    assert bundle["meta"]["module_state"]["publication"] == "published"
    assert bundle["ui_state"]["latest_responses"] == "ready"
    assert bundle["ui_state"]["map_participation"] == "ready"
    assert isinstance(bundle["cards"], list)
    assert bundle["cards"][0]["id"] == "total_respuestas"
    assert isinstance(bundle["kpis"]["tasa_completitud"], float)
    assert isinstance(bundle["kpis"]["risk_score"], float)
    assert bundle["modules"]["latest_responses"] == [{"id": 1}]
    assert bundle["admin_template"]["layout_version"] == "2026.04"
    assert bundle["admin_template"]["tabs"][0]["id"] == "overview"
    assert bundle["admin_template"]["decision_cards"][0]["id"] == "territory_focus"
    assert bundle["admin_template"]["ux_guardrails"]["chart_container"]["default_min_width"] == 280
    assert bundle["admin_template"]["ux_guardrails"]["chart_container"]["require_non_zero_parent_size"] is True
    assert bundle["admin_template"]["ux_guardrails"]["responsive"]["mobile_breakpoint_px"] == 768
    assert bundle["admin_template"]["ux_guardrails"]["telemetry"]["event_endpoint_preferred"] == "/api/analytics/event"
    assert bundle["admin_template"]["visual_modules"][0]["container"]["min_height"] == 220
    assert "kpis_executive" in bundle
    assert "participacion_total" in bundle["kpis_executive"]
    assert bundle["frontend_render_contract"]["hierarchy"]["chart_engines"][0] == "echarts"
    assert bundle["frontend_render_contract"]["modules"]["heatmap"]["state"] == "ready"
    assert bundle["frontend_render_contract"]["modules"]["publication"]["state"] == "published"
    assert bundle["survey_publication"]["links"]["live_results_endpoint"].endswith("/live-results")
    assert bundle["modules"]["publication"]["actions"][0]["id"] == "open_live_results"
    assert bundle["sections"]["mapas"]["heatmap"]["state"] == "ready"
    assert "categorias" in bundle["sections"]["estadisticas"]
    assert "demografia" in bundle["sections"]["estadisticas"]
    assert "ia" in bundle["sections"]
    heatmap_section = bundle["sections"]["mapas"]["heatmap"]
    assert heatmap_section["points"]
    assert heatmap_section["cells"]
    assert heatmap_section["hotspots"] == [{"label": "Centro", "count": 1}]
    assert heatmap_section["headline"] == "Mapa con participacion territorial."
    assert heatmap_section["legend"]["mode"] == "category_weight"
    assert heatmap_section["empty_state"] == "Sin puntos geograficos publicados"
    assert heatmap_section["recommended_action"]["label"] == "Ver mapa"
    assert heatmap_section["category_layers"]["provider"] == "maplibre"
    assert heatmap_section["category_layers"]["source_options"]["cluster"] is True
    assert heatmap_section["category_layers"]["telemetry"]["event_endpoint"] == "/api/analytics/event"
    assert bundle["sections"]["estadisticas"]["resumen"]["total_respuestas"] == 12
    assert "genero" in bundle["sections"]["estadisticas"]["demografia"]
    assert "rango_etario" in bundle["sections"]["estadisticas"]["demografia"]
    assert "headline" in bundle["sections"]["ia"]
    assert "insights" in bundle["sections"]["ia"]


def test_dashboard_bundle_handles_empty_states(monkeypatch):
    monkeypatch.setattr(svc, "get_summary", lambda encuesta_id, filtros=None: {
        "total_respuestas": 0,
        "participantes_unicos": 0,
        "tasa_completitud": None,
        "demografia": {},
        "preguntas": [],
    })
    monkeypatch.setattr(svc, "get_timeseries", lambda encuesta_id, granularity="day", filtros=None: [])
    monkeypatch.setattr(svc, "get_heatmap", lambda encuesta_id, filtros=None: {"points": [], "metadata": {"map": {"hotspots": []}}})
    monkeypatch.setattr(svc, "get_forecast", lambda encuesta_id, filtros=None: {"projected_total": 0})
    monkeypatch.setattr(svc, "get_alerts", lambda encuesta_id, filtros=None: {"alerts": []})
    monkeypatch.setattr(svc, "get_executive_brief", lambda encuesta_id, filtros=None: {"headline": "ok"})
    monkeypatch.setattr(svc, "get_anomaly_report", lambda encuesta_id, filtros=None: {"risk_score": None})
    monkeypatch.setattr(svc, "get_segment_compare", lambda encuesta_id, filtros=None, segment_a=None, segment_b=None: {"segment_a": {"stats": {"total_respuestas": 0}}, "segment_b": {"stats": {"total_respuestas": 0}}})
    monkeypatch.setattr(svc, "_build_latest_responses_preview", lambda encuesta_id, filtros=None, limit=10: [])

    bundle = svc.get_dashboard_bundle(100)

    assert bundle["meta"]["module_state"]["summary"] == "empty"
    assert bundle["meta"]["module_state"]["timeseries"] == "empty"
    assert bundle["meta"]["module_state"]["heatmap"] == "empty"
    assert bundle["ui_state"]["latest_responses"] == "empty"
    assert bundle["ui_state"]["alerts"] == "normal"
    assert bundle["modules"]["latest_responses"] == []
    assert bundle["kpis"]["risk_score"] == 0.0
    assert bundle["frontend_render_contract"]["modules"]["heatmap"]["state"] == "empty"
    assert bundle["admin_template"]["datasets"]["geo_rankings"]["barrio"] == []


def test_dashboard_bundle_marks_map_ready_when_only_cells_available(monkeypatch):
    monkeypatch.setattr(svc, "get_summary", lambda encuesta_id, filtros=None: {
        "total_respuestas": 3,
        "participantes_unicos": 3,
        "tasa_completitud": "100",
        "demografia": {},
        "preguntas": [],
        "canales": [],
    })
    monkeypatch.setattr(svc, "get_timeseries", lambda encuesta_id, granularity="day", filtros=None: [])
    monkeypatch.setattr(
        svc,
        "get_heatmap",
        lambda encuesta_id, filtros=None: {"points": [], "cells": [{"cell_id": "x", "count": 2}], "metadata": {"map": {"hotspots": []}}},
    )
    monkeypatch.setattr(svc, "get_forecast", lambda encuesta_id, filtros=None: {"projected_total": 4})
    monkeypatch.setattr(svc, "get_alerts", lambda encuesta_id, filtros=None: {"alerts": []})
    monkeypatch.setattr(svc, "get_executive_brief", lambda encuesta_id, filtros=None: {"headline": "ok", "insights": []})
    monkeypatch.setattr(svc, "get_anomaly_report", lambda encuesta_id, filtros=None: {"risk_score": None})
    monkeypatch.setattr(svc, "get_segment_compare", lambda encuesta_id, filtros=None, segment_a=None, segment_b=None: {"segment_a": {"stats": {"total_respuestas": 0}}, "segment_b": {"stats": {"total_respuestas": 0}}})
    monkeypatch.setattr(svc, "_build_latest_responses_preview", lambda encuesta_id, filtros=None, limit=10: [])

    bundle = svc.get_dashboard_bundle(101)

    assert bundle["ui_state"]["map_participation"] == "ready"
    assert bundle["meta"]["module_state"]["heatmap"] == "ready"
    assert bundle["sections"]["mapas"]["heatmap"]["state"] == "ready"


def test_dashboard_bundle_fast_mode_skips_heavy_modules(monkeypatch):
    monkeypatch.setattr(svc, "get_summary", lambda encuesta_id, filtros=None: {
        "total_respuestas": 4,
        "participantes_unicos": 4,
        "tasa_completitud": "100",
        "demografia": {},
        "preguntas": [],
    })
    monkeypatch.setattr(svc, "get_timeseries", lambda encuesta_id, granularity="day", filtros=None: [{"fecha": "2026-01-01", "total": 4}])
    monkeypatch.setattr(svc, "get_heatmap", lambda encuesta_id, filtros=None: {"points": [], "metadata": {"map": {"hotspots": []}}})
    monkeypatch.setattr(svc, "get_forecast", lambda encuesta_id, filtros=None: {"projected_total": 6})
    monkeypatch.setattr(svc, "get_alerts", lambda encuesta_id, filtros=None: {"alerts": []})
    monkeypatch.setattr(svc, "get_executive_brief", lambda encuesta_id, filtros=None: {"headline": "ok"})

    def _must_not_run(*args, **kwargs):
        raise AssertionError("heavy module should not run in fast mode")

    monkeypatch.setattr(svc, "get_anomaly_report", _must_not_run)
    monkeypatch.setattr(svc, "get_segment_compare", _must_not_run)
    monkeypatch.setattr(svc, "_build_latest_responses_preview", _must_not_run)

    bundle = svc.get_dashboard_bundle(77, {"canal": "web"}, fast_mode=True)

    assert bundle["meta"]["fast_mode"] is True
    assert bundle["ui_state"]["render_strategy"] == "fast"
    assert bundle["modules"]["latest_responses"] == []
    assert bundle["kpis"]["risk_score"] == 0.0
    assert bundle["frontend_render_contract"]["render_strategy"] == "fast"


def test_dashboard_bundle_marks_latest_responses_degraded_on_error(monkeypatch):
    monkeypatch.setattr(svc, "get_summary", lambda encuesta_id, filtros=None: {
        "total_respuestas": 9,
        "participantes_unicos": 8,
        "tasa_completitud": "89",
        "demografia": {},
        "preguntas": [],
    })
    monkeypatch.setattr(svc, "get_timeseries", lambda encuesta_id, granularity="day", filtros=None: [{"fecha": "2026-01-01", "total": 2}])
    monkeypatch.setattr(svc, "get_heatmap", lambda encuesta_id, filtros=None: {"points": [], "metadata": {"map": {"hotspots": []}}})
    monkeypatch.setattr(svc, "get_forecast", lambda encuesta_id, filtros=None: {"projected_total": 10})
    monkeypatch.setattr(svc, "get_alerts", lambda encuesta_id, filtros=None: {"alerts": []})
    monkeypatch.setattr(svc, "get_executive_brief", lambda encuesta_id, filtros=None: {"headline": "ok"})
    monkeypatch.setattr(svc, "get_anomaly_report", lambda encuesta_id, filtros=None: {"risk_score": 0.2})
    monkeypatch.setattr(svc, "get_segment_compare", lambda encuesta_id, filtros=None, segment_a=None, segment_b=None: {"segment_a": {"stats": {"total_respuestas": 3}}, "segment_b": {"stats": {"total_respuestas": 2}}})

    def _boom(*_args, **_kwargs):
        raise RuntimeError("latest broken")

    monkeypatch.setattr(svc, "_build_latest_responses_preview", _boom)

    bundle = svc.get_dashboard_bundle(99, {"canal": "web"})

    assert bundle["ui_state"]["latest_responses"] == "degraded"
    assert bundle["modules"]["latest_responses"] == []
    assert bundle["modules"]["latest_responses_meta"]["state"] == "degraded"
    assert "latest broken" in (bundle["modules"]["latest_responses_meta"].get("error") or "")


@pytest.fixture
def dashboard_channel_rows(monkeypatch):
    """Persist transport channel names and run the original SQL comparison."""
    survey = db.session.get(EncEncuesta, 84)
    question = EncPregunta(encuesta_id=survey.id, texto="Prioridad", tipo="opcion_unica", orden=0)
    db.session.add(question)
    db.session.flush()
    option = EncOpcion(pregunta_id=question.id, texto="Atención", orden=0)
    db.session.add(option)
    db.session.flush()
    rows = [
        *(('web', 'real') for _ in range(3)),
        ('whatsapp', 'real'),
        ('whatsapp_chat', 'real'),
        ('whatsapp_chat', 'real'),
        ('whatsapp_flow', 'real'),
        ('whatsapp_other', 'real'),
        ('whatsapp_chat', 'synthetic_demo'),
        ('whatsapp_flow', 'legacy_unverified'),
    ]
    for index, (channel, origin) in enumerate(rows):
        response = EncRespuesta(
            encuesta_id=survey.id, tenant_id=survey.tenant_id,
            canal=channel, response_origin=origin,
            huella_unica=f"bundle-channel-{index}",
        )
        db.session.add(response)
        db.session.flush()
        db.session.add(EncRespuestaDetalle(
            respuesta_id=response.id, pregunta_id=question.id, opcion_id=option.id,
        ))
    _, foreign_tenant = _tenant("bundle-channel-foreign")
    foreign_survey = EncEncuesta(
        tenant_id=foreign_tenant.id, slug="bundle-channel-foreign",
        titulo="Otra organización", estado="publicada", tipo="opinion",
    )
    db.session.add(foreign_survey)
    db.session.flush()
    db.session.add(EncRespuesta(
        encuesta_id=foreign_survey.id, tenant_id=foreign_tenant.id,
        canal="whatsapp_chat", response_origin="real",
    ))
    db.session.commit()

    # Only unrelated dashboard modules are replaced. Channel totals and
    # option bases still use get_segment_compare and its persisted SQL rows.
    monkeypatch.setattr(svc, "get_timeseries", lambda *args, **kwargs: [])
    monkeypatch.setattr(svc, "get_heatmap", lambda *args, **kwargs: {"points": []})
    monkeypatch.setattr(svc, "get_forecast", lambda *args, **kwargs: {"projected_total": 0})
    monkeypatch.setattr(svc, "get_alerts", lambda *args, **kwargs: {"alerts": []})
    monkeypatch.setattr(svc, "get_executive_brief", lambda *args, **kwargs: {"headline": "Local"})
    monkeypatch.setattr(svc, "get_anomaly_report", lambda *args, **kwargs: {"risk_score": 0})
    monkeypatch.setattr(svc, "_build_latest_responses_preview", lambda *args, **kwargs: [])
    monkeypatch.setattr(svc, "_build_survey_publication_contract", lambda *args, **kwargs: {
        "public_state": "published", "links": {}, "actions": [],
    })
    return survey


def test_dashboard_default_whatsapp_segment_includes_native_chat_and_flow(
    monkeypatch, dashboard_channel_rows,
):
    original_compare = svc.get_segment_compare
    comparisons = []

    def capture_comparison(*args, **kwargs):
        result = original_compare(*args, **kwargs)
        comparisons.append(result)
        return result

    monkeypatch.setattr(svc, "get_segment_compare", capture_comparison)
    bundle = svc.get_dashboard_bundle(dashboard_channel_rows.id)

    assert bundle["modules"]["summary"]["total_respuestas"] == 8
    assert len(comparisons) == 1
    comparison = comparisons[0]
    assert comparison["segment_a"]["stats"]["total_respuestas"] == 3
    assert comparison["segment_b"]["stats"]["total_respuestas"] == 4
    assert comparison["segment_b"]["stats"]["canales"] == [
        {"label": "whatsapp_chat", "value": 2},
        {"label": "whatsapp", "value": 1},
        {"label": "whatsapp_flow", "value": 1},
    ]
    assert bundle["kpis_executive"]["brecha_segmento_max"]["value"] == 1


def test_explicit_whatsapp_segment_and_global_channel_filter_stay_exact(
    monkeypatch, dashboard_channel_rows,
):
    exact = svc.get_segment_compare(
        dashboard_channel_rows.id,
        segment_a={"canal": "web"}, segment_b={"canal": "whatsapp"},
    )
    assert exact["segment_b"]["stats"]["total_respuestas"] == 1

    original_compare = svc.get_segment_compare
    comparisons = []

    def capture_comparison(*args, **kwargs):
        result = original_compare(*args, **kwargs)
        comparisons.append(result)
        return result

    monkeypatch.setattr(svc, "get_segment_compare", capture_comparison)
    bundle = svc.get_dashboard_bundle(dashboard_channel_rows.id, {"canal": "whatsapp_chat"})

    assert bundle["meta"]["filters"] == {"canal": "whatsapp_chat"}
    assert bundle["modules"]["summary"]["total_respuestas"] == 2
    assert len(comparisons) == 1
    assert comparisons[0]["segment_a"]["stats"]["total_respuestas"] == 0
    assert comparisons[0]["segment_b"]["stats"]["total_respuestas"] == 2


@pytest.mark.parametrize("channel,origin,foreign_scope", [
    ("whatsapp_unrecognized", "real", False),
    ("whatsapp_chat", "synthetic_demo", False),
    ("whatsapp_flow", "legacy_unverified", False),
    ("whatsapp_chat", "real", True),
])
def test_dashboard_default_whatsapp_segment_does_not_expand_channel_origin_or_scope(
    monkeypatch, dashboard_channel_rows, channel, origin, foreign_scope,
):
    from models import TenantProfile

    survey = dashboard_channel_rows
    tenant_id = (
        TenantProfile.query.filter_by(slug="bundle-channel-foreign").one().id
        if foreign_scope else survey.tenant_id
    )
    # The foreign-scope case deliberately injects an inconsistent response row
    # for the same survey. The original comparison's tenant predicate must still
    # exclude it; changing the default channel group never grants another scope.
    db.session.add(EncRespuesta(
        encuesta_id=survey.id, tenant_id=tenant_id,
        canal=channel, response_origin=origin,
    ))
    db.session.commit()
    original_compare = svc.get_segment_compare
    comparisons = []

    def capture_comparison(*args, **kwargs):
        result = original_compare(*args, **kwargs)
        comparisons.append(result)
        return result

    monkeypatch.setattr(svc, "get_segment_compare", capture_comparison)
    bundle = svc.get_dashboard_bundle(survey.id)

    assert len(comparisons) == 1
    assert comparisons[0]["segment_a"]["stats"]["total_respuestas"] == 3
    assert comparisons[0]["segment_b"]["stats"]["total_respuestas"] == 4
    assert bundle["kpis_executive"]["brecha_segmento_max"]["value"] == 1
