from types import SimpleNamespace

from services import encuestas_analytics_service as svc


def test_get_segment_suggestions_returns_dynamic_dimensions(client):
    from database import db
    from models import EncEncuesta, EncRespuesta

    encuesta = EncEncuesta(tenant_id=901, slug="suggestions-exact-regression", titulo="QA segmentos")
    db.session.add(encuesta)
    db.session.flush()
    for index, (channel, gender, age, neighborhood) in enumerate([
        ("web", "f", "18-24", "Centro"),
        ("whatsapp", "m", "25-34", "Norte"),
        ("web", "f", "18-24", "Centro"),
    ]):
        db.session.add(EncRespuesta(encuesta_id=encuesta.id, tenant_id=encuesta.tenant_id,
            huella_unica=f"suggestions-{index}", response_origin="real", canal=channel,
            genero=gender, rango_etario=age, barrio=neighborhood, ciudad="Junin", provincia="BA", pais="AR"))
    db.session.commit()

    payload = svc.get_segment_suggestions(encuesta.id)

    assert payload["encuesta_id"] == encuesta.id
    assert payload["total_respuestas"] == 3
    assert payload["exact_aggregates"] is True
    assert payload["dimensions"]["canal"][0] == {
        "label": "web", "filters": {"canal": "web"}, "count": 2, "coverage": 66.67,
    }


def test_get_anomaly_report_includes_top_anomalies_metadata(monkeypatch):
    encuesta = SimpleNamespace(id=10)

    class _AggregateQuery:
        def __init__(self):
            self.calls = 0

        def with_entities(self, *_args):
            return self

        def filter(self, *_args):
            return self

        def group_by(self, *_args):
            return self

        def having(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def limit(self, *_args):
            return self

        def all(self):
            self.calls += 1
            if self.calls == 1:
                return [("1.1.1.1", 3)]
            if self.calls == 2:
                return [("fp-1", 3)]
            return [(-34.6, -58.4, 4)]

    aggregate_query = _AggregateQuery()
    monkeypatch.setattr(svc, "get_encuesta", lambda encuesta_id: encuesta)
    monkeypatch.setattr(
        svc,
        "_get_response_snapshot",
        lambda encuesta_obj, filtros=None: {
            "selected_query": aggregate_query,
            "provenance": {"mode": "real", "population_size": 4},
        },
    )
    monkeypatch.setattr(
        svc,
        "_exact_recent_windows",
        lambda snapshot, now, window_minutes: (4, 0, 4),
    )

    payload = svc.get_anomaly_report(10, burst_threshold=1)

    assert "severity" in payload
    assert payload["advisory_policy"]["mutates_operational_state"] is False
    assert payload["state_mutation"]["applied"] is False
    assert payload["thresholds"]["risk_score_high"] == 65
    assert isinstance(payload["top_anomalies"], list)
    assert payload["top_anomalies"]
    first = payload["top_anomalies"][0]
    assert "recommended_action" in first
    assert "confidence" in first
    assert "timestamp" in first


def test_is_demo_respuesta_detects_seed_metadata():
    demo = SimpleNamespace(
        encuesta_id=9,
        metadata_payload={
            "is_demo_seed": True,
            "demo_seed_contract_version": "surveys.demo_seeding.v1",
            "demo_batch_id": "seed-9-1755680400000-abcdef123456",
        },
    )
    regular = SimpleNamespace(
        encuesta_id=9,
        metadata_payload={"demo": True, "synthetic": True},
    )

    assert svc._is_demo_respuesta(demo) is True
    assert svc._is_demo_respuesta(regular) is False


def test_response_data_mode_is_real_by_default_and_explicit_for_admin():
    assert svc._response_data_mode(None) == "real"
    assert svc._response_data_mode({}) == "real"
    assert svc._response_data_mode({"include_demo": "true"}) == "real"
    assert svc._response_data_mode({"data_mode": "synthetic"}) == "synthetic"
    assert svc._response_data_mode({"data_mode": "mixed"}) == "real"
    assert svc._response_data_mode(
        {"data_mode": "synthetic", "exclude_demo": "true"}
    ) == "real"


def test_get_heatmap_exposes_render_contract_hierarchy(monkeypatch):
    encuesta = SimpleNamespace(id=77)
    respuestas = [SimpleNamespace(lat=-33.1, lng=-68.8)]
    points = [{"lat": -33.1, "lng": -68.8, "weight": 2}]
    cells = [{"lat": -33.1, "lng": -68.8, "count": 2}]

    monkeypatch.setattr(svc, "get_encuesta", lambda encuesta_id: encuesta)
    monkeypatch.setattr(
        svc,
        "_get_response_snapshot",
        lambda encuesta_obj, filtros=None: {
            "sample": respuestas,
            "provenance": {"mode": "real", "contains_synthetic": False},
        },
    )
    monkeypatch.setattr(
        svc,
        "_bounded_geo_points",
        lambda snapshot, limit: (points, {"sample_size": 1, "partial": False}),
    )
    monkeypatch.setattr(
        svc,
        "_exact_geo_cells",
        lambda snapshot, max_cells, minimum_count, precision: (
            cells,
            {"cell_count": 1, "partial": False},
        ),
    )
    monkeypatch.setattr(svc, "_build_heatmap_metadata", lambda _encuesta, _points: {"base": True})
    monkeypatch.setattr(svc, "_build_map_filter", lambda _points: {"keys": ["barrio"]})
    monkeypatch.setattr(svc, "build_feature_collection", lambda data: {"type": "FeatureCollection", "features": data})
    monkeypatch.setattr(svc, "get_map_config", lambda: {"provider": "maplibre"})

    payload = svc.get_heatmap(77)

    assert payload["render_contract"]["module"] == "heatmap"
    assert payload["render_contract"]["state"] == "ready"
    assert payload["render_contract"]["chart_hierarchy"][0] == "echarts"
    assert payload["render_contract"]["map_hierarchy"][0] == "maplibre"
    assert payload["ai_policy"]["state_mutation_allowed"] is False


def test_category_heatmap_layers_publish_maplibre_enterprise_contract(monkeypatch):
    monkeypatch.setattr(svc, "get_map_config", lambda: {"style_url": "https://maps.example.com/style.json"})
    points = [
        {
            "lat": -34.61,
            "lng": -58.38,
            "weight": 8,
            "categoria": "seguridad",
            "ts": "2026-05-16T12:00:00Z",
        },
        {
            "lat": -34.62,
            "lng": -58.39,
            "weight": 4,
            "categoria": "transito",
        },
    ]

    layers = svc._build_category_heatmap_layers(points)

    assert layers["provider"] == "maplibre"
    assert layers["engine"] == "maplibre-gl-js"
    assert layers["style_url"] == "https://maps.example.com/style.json"
    assert layers["source"]["type"] == "FeatureCollection"
    assert layers["source_options"]["cluster"] is True
    assert layers["layers"]["heatmap"]["id"] == "encuestas-heat"
    assert layers["layers"]["clusters"]["id"] == "encuestas-clusters"
    assert layers["layers"]["points"]["id"] == "encuestas-points"
    assert layers["interactions"]["hover"] is True
    assert layers["interactions"]["time_slider"]["field"] == "ts"
    assert layers["telemetry"]["event_endpoint"] == "/api/analytics/event"
    assert "cluster_click" in layers["telemetry"]["events"]
    assert layers["legend"]["mode"] == "category_weight"
    assert layers["legend"]["max_weight"] == 8
    assert layers["categories"][0]["categoria"] == "seguridad"
    assert layers["categories"][0]["color"]
    assert layers["categories"][0]["event_count"] == 1
    assert layers["categories"][0]["total_weight"] == 8


def test_get_executive_brief_uses_gemini_advisory_contract(monkeypatch):
    encuesta = SimpleNamespace(id=88, titulo="Movilidad urbana")
    summary = {
        "total_respuestas": 42,
        "participantes_unicos": 38,
        "tasa_completitud": 91.2,
    }
    forecast = {"projected_total": 60, "horizon_minutes": 120, "momentum": "subiendo"}
    alerts = {"alerts": [{"type": "participation_spike"}]}

    monkeypatch.setenv("ENCUESTAS_AI_BRIEF_PROVIDERS", "gemini")
    monkeypatch.setattr(svc, "get_encuesta", lambda encuesta_id: encuesta)
    monkeypatch.setattr(svc, "get_summary", lambda encuesta_id, filtros=None: summary)
    monkeypatch.setattr(svc, "get_forecast", lambda encuesta_id, filtros=None: forecast)
    monkeypatch.setattr(svc, "get_alerts", lambda encuesta_id, filtros=None: alerts)
    monkeypatch.setattr(
        svc,
        "_generate_gemini_executive_brief",
        lambda encuesta_obj, summary_obj, forecast_obj, alerts_obj: {
            "contract_version": svc.SURVEY_AI_BRIEF_CONTRACT_VERSION,
            "provider": "gemini",
            "model": "gemini-test",
            "headline": "La participacion acelera en zonas clave.",
            "insights": ["Revisar barrios con mayor traccion."],
            "risk_level": "medium",
            "advisory_policy": dict(svc.SURVEY_AI_ADVISORY_POLICY),
            "state_mutation": {"requested": False, "applied": False},
        },
    )

    payload = svc.get_executive_brief(88)

    assert payload["contract_version"] == "encuestas.ai_executive_brief.v1"
    assert payload["ai_enhanced"] is True
    assert payload["ai_provider"] == "gemini"
    assert payload["ai_model"] == "gemini-test"
    assert payload["ai_policy"]["mutates_operational_state"] is False
    assert payload["state_mutation"]["applied"] is False
    assert payload["risk_level"] == "medium"
