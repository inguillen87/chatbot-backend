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
