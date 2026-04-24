from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from services.government_pipeline import (
    IncidentRecord,
    build_heatmap,
    build_scorecards,
    cluster_incidents,
    demand_forecast,
    plan_routes,
)


def _record(**overrides):
    base = dict(
        id=overrides.get("id", 1),
        created_at=overrides.get("created_at", datetime(2024, 1, 1, 8, 0, 0)),
        estado=overrides.get("estado", "cerrado"),
        categoria=overrides.get("categoria", "Iluminación"),
        distrito=overrides.get("distrito", "Centro"),
        latitud=overrides.get("latitud", -34.6037),
        longitud=overrides.get("longitud", -58.3816),
        ultima_actividad=overrides.get("ultima_actividad", datetime(2024, 1, 1, 12, 0, 0)),
        canal=overrides.get("canal", "WhatsApp"),
        anon_id=overrides.get("anon_id", "abc"),
    )
    return IncidentRecord(**base)


def test_build_scorecards_computes_kpis():
    records = [
        _record(id=1, estado="cerrado", created_at=datetime(2024, 1, 1, 8, 0, 0), ultima_actividad=datetime(2024, 1, 1, 12, 0, 0)),
        _record(id=2, estado="en curso", created_at=datetime(2024, 1, 2, 9, 0, 0), ultima_actividad=None, distrito="Norte"),
        _record(id=3, estado="cerrado", created_at=datetime(2024, 1, 3, 10, 0, 0), ultima_actividad=datetime(2024, 1, 4, 10, 0, 0), categoria="Residuos"),
    ]

    payload = build_scorecards(records)

    assert payload["totals"]["incidentes"] == 3
    assert payload["totals"]["abiertos"] == 1
    assert payload["totals"]["cerrados"] == 2
    assert payload["totals"]["backlog"] == 1
    assert payload["incidentes_por_barrio"][0]["label"] == "Centro"
    assert any(item["label"] == "Residuos" for item in payload["incidentes_por_categoria"])
    assert payload["tiempos_resolucion"]["promedio_horas"] is not None


def test_heatmap_aggregates_records():
    records = [
        _record(id=1),
        _record(id=2, anon_id="def"),
        _record(id=3, anon_id="ghi"),
    ]

    payload = build_heatmap(records, resolution=7, min_count=2)

    assert payload["resolution"] == 7
    assert payload["min_count"] == 2
    assert payload["cells"]
    assert payload["cells"][0]["total"] >= 3


def test_demand_forecast_generates_future_points():
    records = []
    start = datetime(2024, 2, 1, 9, 0, 0)
    for days in range(5):
        records.append(
            _record(
                id=days + 1,
                created_at=start + timedelta(days=days),
                ultima_actividad=start + timedelta(days=days, hours=4),
            )
        )

    forecast = demand_forecast(records, periods=7)

    assert len(forecast["historical"]) == 5
    assert len(forecast["forecast"]) == 7
    assert all(entry["expected"] >= 0 for entry in forecast["forecast"])


def test_cluster_incidents_wraps_heatmap():
    records = [
        _record(id=1),
        _record(id=2, anon_id="def"),
        _record(id=3, anon_id="ghi"),
        _record(id=4, anon_id="jkl"),
        _record(id=5, anon_id="mno"),
    ]

    payload = cluster_incidents(records, resolution=7, min_count=3)

    assert payload["clusters"]
    assert payload["clusters"][0]["total"] >= 5


def test_plan_routes_orders_and_returns_distance():
    records = [
        _record(id=1, latitud=-34.60, longitud=-58.38, anon_id="uno"),
        _record(id=2, latitud=-34.61, longitud=-58.39, anon_id="dos"),
        _record(id=3, latitud=-34.62, longitud=-58.37, anon_id="tres"),
    ]

    plan = plan_routes(records, depot_lat=-34.6037, depot_lng=-58.3816, max_stops=2)

    assert plan["depot"]["lat"] == pytest.approx(-34.6037)
    assert len(plan["stops"]) == 2
    assert plan["total_distance_km"] > 0
    assert plan["stops"][0]["orden"] == 1
