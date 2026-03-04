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
