import os
import re
from typing import Optional

from .location_service import geocode_address
from .geo_context import get_geo_context

# Regex to capture Google Maps URLs
MAPS_URL_RE = re.compile(
    r"https?://(?:maps\.app\.goo\.gl|(?:www\.)?google\.com/maps/[^\s]*)",
    re.IGNORECASE,
)

def _extract_from_maps_url(url: str) -> dict:
    """Extracts latitude and longitude from a Google Maps URL."""
    match = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", url)
    if match:
        return {"lat": float(match.group(1)), "lng": float(match.group(2))}
    return {}

def extract_location(input_str: str) -> Optional[dict]:
    """Return location data from a Google Maps URL or plain text.

    If ``input_str`` contains a Google Maps URL, lat/lon are extracted directly.
    Otherwise the string is treated as a free-form address and geocoded with a
    municipal bias loaded from :func:`get_geo_context`.
    """
    if not input_str:
        return None

    tenant_id = os.environ.get("MUNICIPIO_ID", "default")
    try:
        geo_ctx = get_geo_context(tenant_id)
    except Exception:
        geo_ctx = None

    m = MAPS_URL_RE.search(input_str)
    if m:
        info = _extract_from_maps_url(m.group(0))
        lat = info.get("lat")
        lon = info.get("lng") or info.get("lon")
        if lat and lon:
            return {
                "coordenadas": {"lat": lat, "lng": lon},
                "maps_search_url": f"https://maps.google.com/?q={lat},{lon}",
            }
        if info.get("text"):
            result = geocode_address(info["text"], geo_ctx=geo_ctx)
            if result:
                lat = result.get("lat")
                lon = result.get("lng")
                return {
                    "coordenadas": {"lat": lat, "lng": lon},
                    "ubicacion": result.get("display_name"),
                    "maps_search_url": f"https://maps.google.com/?q={lat},{lon}",
                }
            return None

    result = geocode_address(input_str, geo_ctx=geo_ctx)
    if not result:
        return None
    lat = result.get("lat")
    lon = result.get("lng")
    return {
        "coordenadas": {"lat": lat, "lng": lon},
        "ubicacion": result.get("display_name"),
        "maps_search_url": f"https://maps.google.com/?q={lat},{lon}",
    }
