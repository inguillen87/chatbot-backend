from services import encuestas_analytics_service as svc


def test_dashboard_bundle_includes_normalized_cards_and_states(monkeypatch):
    monkeypatch.setattr(svc, "get_summary", lambda encuesta_id, filtros=None: {
        "total_respuestas": 12,
        "participantes_unicos": 10,
        "tasa_completitud": "87.5",
        "demografia": {},
        "preguntas": [],
    })
    monkeypatch.setattr(svc, "get_timeseries", lambda encuesta_id, granularity="day", filtros=None: [{"fecha": "2026-01-01", "total": 2}])
    monkeypatch.setattr(svc, "get_heatmap", lambda encuesta_id, filtros=None: {"points": [{"lat": -33.1, "lng": -68.8, "weight": 1}], "metadata": {"map": {"hotspots": []}}})
    monkeypatch.setattr(svc, "get_forecast", lambda encuesta_id, filtros=None: {"projected_total": 20})
    monkeypatch.setattr(svc, "get_alerts", lambda encuesta_id, filtros=None: {"alerts": [{"id": "a1"}]})
    monkeypatch.setattr(svc, "get_executive_brief", lambda encuesta_id, filtros=None: {"headline": "ok"})
    monkeypatch.setattr(svc, "get_anomaly_report", lambda encuesta_id, filtros=None: {"risk_score": "0.331"})
    monkeypatch.setattr(svc, "get_segment_compare", lambda encuesta_id, filtros=None, segment_a=None, segment_b=None: {"segment_a": {"stats": {"total_respuestas": 7}}, "segment_b": {"stats": {"total_respuestas": 5}}})
    monkeypatch.setattr(svc, "_build_latest_responses_preview", lambda encuesta_id, filtros=None, limit=10: [{"id": 1}])

    bundle = svc.get_dashboard_bundle(84, {"canal": "web"})

    assert bundle["meta"]["schema_version"] == "2026.03"
    assert bundle["meta"]["module_state"]["alerts"] == "attention"
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
    assert bundle["sections"]["mapas"]["heatmap"]["state"] == "ready"
    assert "categorias" in bundle["sections"]["estadisticas"]
    assert "demografia" in bundle["sections"]["estadisticas"]
    assert "ia" in bundle["sections"]


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
