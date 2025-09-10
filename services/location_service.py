import logging
import os
from typing import Optional

import re
import requests

logger = logging.getLogger(__name__)

# User-Agent required by Nominatim usage policy
NOMINATIM_USER_AGENT = os.environ.get(
    "NOMINATIM_USER_AGENT", "chatbot-backend/1.0"
)


def _nominatim_headers() -> dict:
    return {"User-Agent": NOMINATIM_USER_AGENT}


def _normalize_corner(addr: str) -> str:
    if not addr:
        return ""
    addr = re.sub(r"\besq\.?\b", "esquina", addr, flags=re.I)
    addr = re.sub(r"\besquina\b", "&", addr, flags=re.I)
    addr = re.sub(r"\s+y\s+", " & ", addr, flags=re.I)
    return re.sub(r"\s+", " ", addr).strip()


def geocode_address(address: str, district: str | None = None) -> Optional[dict]:
    """Geocode an address using the free OpenStreetMap Nominatim API.

    Parameters
    ----------
    address: str
        Base street address provided by the user.
    district: str | None
        Optional district or city name to bias the search.
    """

    if not address:
        return None

    # Append district if available and not already present
    query = _normalize_corner(address)
    if district and district.lower() not in query.lower():
        query = f"{query}, {district}"
    if "argentina" not in query.lower():
        query = f"{query}, Argentina"

    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": query, "format": "json", "limit": 1}

    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": query, "format": "json", "limit": 5}
    try:
        resp = requests.get(url, params=params, headers=_nominatim_headers(), timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        item = data[0]
        return {
            "lat": float(item.get("lat")),
            "lng": float(item.get("lon")),
            "display_name": item.get("display_name"),
            "maps_search_url": f"https://www.openstreetmap.org/search?query={requests.utils.quote(query)}",
        }
    except requests.RequestException as e:
        logger.error(f"Error geocoding address via Nominatim: {e}")
        return None


def autocomplete_address(query: str):
    """Return address suggestions using Nominatim. Limited but avoids Google APIs."""

    if not query:
        return []

    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": query, "format": "json", "limit": 5}
    try:
        resp = requests.get(url, params=params, headers=_nominatim_headers(), timeout=5)
        resp.raise_for_status()
        data = resp.json()
        return [item.get("display_name") for item in data]
    except requests.RequestException as e:
        logger.error(f"Error getting autocomplete suggestions: {e}")
        return []


def find_nearby_places(location, keyword, radius=1500):
    """Placeholder for compatibility; Nominatim does not support Places API."""

    logger.warning("find_nearby_places is not implemented for Nominatim.")
    return []
