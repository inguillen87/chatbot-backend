from types import SimpleNamespace

from services import encuestas_analytics_service as svc


def test_get_segment_suggestions_returns_dynamic_dimensions(monkeypatch):
    encuesta = SimpleNamespace(id=9)
    respuestas = [
        SimpleNamespace(canal="web", genero="f", rango_etario="18-24", barrio="Centro", ciudad="Junin", provincia="BA", pais="AR"),
        SimpleNamespace(canal="whatsapp", genero="m", rango_etario="25-34", barrio="Norte", ciudad="Junin", provincia="BA", pais="AR"),
        SimpleNamespace(canal="web", genero="f", rango_etario="18-24", barrio="Centro", ciudad="Junin", provincia="BA", pais="AR"),
    ]
    monkeypatch.setattr(svc, "get_encuesta", lambda encuesta_id: encuesta)
    monkeypatch.setattr(svc, "_collect_respuestas", lambda encuesta_obj, filtros=None: respuestas)

    payload = svc.get_segment_suggestions(9)

    assert payload["encuesta_id"] == 9
    assert "canal" in payload["dimensions"]
    assert payload["dimensions"]["canal"][0]["label"] == "web"
    assert payload["dimensions"]["canal"][0]["coverage"] > 0


def test_get_anomaly_report_includes_top_anomalies_metadata(monkeypatch):
    encuesta = SimpleNamespace(id=10)
    respuestas = [
        SimpleNamespace(ip="1.1.1.1", huella_unica="fp-1", lat=-34.6, lng=-58.4, submitted_at=svc.datetime.now(svc.timezone.utc)),
        SimpleNamespace(ip="1.1.1.1", huella_unica="fp-1", lat=-34.6, lng=-58.4, submitted_at=svc.datetime.now(svc.timezone.utc)),
        SimpleNamespace(ip="1.1.1.1", huella_unica="fp-1", lat=-34.6, lng=-58.4, submitted_at=svc.datetime.now(svc.timezone.utc)),
        SimpleNamespace(ip="2.2.2.2", huella_unica="fp-2", lat=-34.6, lng=-58.4, submitted_at=svc.datetime.now(svc.timezone.utc)),
    ]
    monkeypatch.setattr(svc, "get_encuesta", lambda encuesta_id: encuesta)
    monkeypatch.setattr(svc, "_collect_respuestas", lambda encuesta_obj, filtros=None: respuestas)

    payload = svc.get_anomaly_report(10, burst_threshold=1)

    assert "severity" in payload
    assert isinstance(payload["top_anomalies"], list)
    assert payload["top_anomalies"]
    first = payload["top_anomalies"][0]
    assert "recommended_action" in first
    assert "confidence" in first
    assert "timestamp" in first


def test_is_demo_respuesta_detects_seed_metadata():
    demo = SimpleNamespace(metadata_payload={"is_demo_seed": True})
    regular = SimpleNamespace(metadata_payload={"is_demo_seed": False})

    assert svc._is_demo_respuesta(demo) is True
    assert svc._is_demo_respuesta(regular) is False


def test_get_heatmap_exposes_render_contract_hierarchy(monkeypatch):
    encuesta = SimpleNamespace(id=77)
    respuestas = [SimpleNamespace(lat=-33.1, lng=-68.8)]
    points = [{"lat": -33.1, "lng": -68.8, "weight": 2}]
    cells = [{"lat": -33.1, "lng": -68.8, "count": 2}]

    monkeypatch.setattr(svc, "get_encuesta", lambda encuesta_id: encuesta)
    monkeypatch.setattr(svc, "_collect_respuestas", lambda encuesta_obj, filtros=None: respuestas)
    monkeypatch.setattr(svc, "_aggregate_heatmap_cells", lambda _respuestas, resolution=None: (points, cells))
    monkeypatch.setattr(svc, "_build_heatmap_metadata", lambda _encuesta, _points: {"base": True})
    monkeypatch.setattr(svc, "_build_map_filter", lambda _points: {"keys": ["barrio"]})
    monkeypatch.setattr(svc, "build_feature_collection", lambda data: {"type": "FeatureCollection", "features": data})
    monkeypatch.setattr(svc, "get_map_config", lambda: {"provider": "maplibre"})

    payload = svc.get_heatmap(77)

    assert payload["render_contract"]["module"] == "heatmap"
    assert payload["render_contract"]["state"] == "ready"
    assert payload["render_contract"]["chart_hierarchy"][0] == "echarts"
    assert payload["render_contract"]["map_hierarchy"][0] == "maplibre"
