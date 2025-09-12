import os
from services import location_extractor
from services.location_extractor import extract_location


def test_extract_location_from_url(monkeypatch):
    monkeypatch.setenv("MUNICIPIO_ID", "default")
    monkeypatch.setattr(
        location_extractor, "_extract_from_maps_url", lambda url: {"lat": -34.1, "lng": -58.4}
    )
    result = extract_location("https://www.google.com/maps/@-34.1,-58.4,17z")
    assert result["coordenadas"] == {"lat": -34.1, "lng": -58.4}
    assert result["maps_search_url"] == "https://maps.google.com/?q=-34.1,-58.4"


def test_extract_location_from_text(monkeypatch):
    monkeypatch.setenv("MUNICIPIO_ID", "default")
    geo_ctx = {"bounds": (0, 0, 1, 1)}
    monkeypatch.setattr(location_extractor, "get_geo_context", lambda tenant: geo_ctx)

    captured = {}

    def fake_geocode(addr, geo_ctx=None):
        captured["geo_ctx"] = geo_ctx
        return {"lat": 1.0, "lng": 2.0, "display_name": "Addr"}

    monkeypatch.setattr(location_extractor, "geocode_address", fake_geocode)
    result = extract_location("Calle Falsa 123")
    assert captured["geo_ctx"] == geo_ctx
    assert result["coordenadas"] == {"lat": 1.0, "lng": 2.0}
    assert result["ubicacion"] == "Addr"
    assert result["maps_search_url"] == "https://maps.google.com/?q=1.0,2.0"
