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
    monkeypatch.setattr(svc, "_build_latest_responses_preview", lambda encuesta_id, filtros=None, limit=10: [])

    bundle = svc.get_dashboard_bundle(100)

    assert bundle["meta"]["module_state"]["summary"] == "empty"
    assert bundle["meta"]["module_state"]["timeseries"] == "empty"
    assert bundle["meta"]["module_state"]["heatmap"] == "empty"
    assert bundle["ui_state"]["latest_responses"] == "empty"
    assert bundle["ui_state"]["alerts"] == "normal"
    assert bundle["modules"]["latest_responses"] == []
    assert bundle["kpis"]["risk_score"] == 0.0
    assert bundle["admin_template"]["datasets"]["geo_rankings"]["barrio"] == []
