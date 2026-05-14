from flask import Flask

from utils.map_config import get_map_config


def test_map_config_exposes_maplibre_provider_aliases():
    app = Flask(__name__)
    app.config.update(
        GOOGLE_MAPS_API_KEY="",
        MAPTILER_API_KEY="",
        MAP_PROVIDER="maptiler",
        MAPLIBRE_STYLE_URL="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
    )

    with app.app_context():
        cfg = get_map_config()

    assert cfg["provider"] == "maplibre"
    assert cfg["provider_aliases"]["maptiler"] == "maplibre"
    assert "maplibre" in cfg["available_providers"]


def test_map_config_falls_back_from_unverified_chatboc_style_host():
    app = Flask(__name__)
    app.config.update(
        GOOGLE_MAPS_API_KEY="",
        MAPTILER_API_KEY="",
        MAP_PROVIDER="maplibre",
        MAPLIBRE_STYLE_URL="https://maps.chatboc.ar/styles/nexid/style.json",
    )

    with app.app_context():
        cfg = get_map_config()

    assert cfg["provider"] == "maplibre"
    assert cfg["style_url"] == "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"
    assert cfg["style_url_source"] == "fallback_default"
    assert cfg["style_url_warning"] == "configured_style_host_unavailable"


def test_public_map_config_endpoint(client):
    resp = client.get("/api/map/config")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["contract_version"] == "public.map_config.v1"
    assert payload["provider"] in {"maplibre", "google", "none"}
    assert "style_url" in payload
