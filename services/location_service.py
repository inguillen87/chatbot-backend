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
    addr = re.sub(r"\s+(?:y|e)\s+", " & ", addr, flags=re.I)
    return re.sub(r"\s+", " ", addr).strip()


def geocode_address(
    address: str,
    district: str | None = None,
    geo_ctx: dict | None = None,
) -> Optional[dict]:
    """Geocode an address using Google Maps if available, falling back to Nominatim.

    Parameters
    ----------
    address: str
        Base street address provided by the user.
    district: str | None
        Optional district or city name to bias the search. Retained for
        backwards compatibility but superseded by ``geo_ctx``.
    geo_ctx: dict | None
        Geographical context with keys like ``city``, ``state``, ``country``,
        ``region_hint`` and ``bounds`` to bias the lookup for multi-tenant
        deployments.
    """

    if not address:
        return None

    query = _normalize_corner(address)

    # Build Google / Nominatim bias parameters from geo_ctx or district
    city = state = country = bounds = region = None
    language = (geo_ctx or {}).get("locale") or "es-AR"
    components = []
    def _clean(v: str | None) -> str | None:
        if not v:
            return v
        return re.sub(r"\s+", " ", v.split(",")[0].strip())

    if geo_ctx:
        city = _clean(geo_ctx.get("city") or geo_ctx.get("ciudad"))
        state = _clean(geo_ctx.get("state") or geo_ctx.get("provincia"))
        country = _clean(geo_ctx.get("country") or geo_ctx.get("pais"))
        region = geo_ctx.get("region_hint") or geo_ctx.get("region")
        bounds = geo_ctx.get("bounds")
    elif district:
        city = _clean(district)
        country = "Argentina"

    if city and city.lower() not in query.lower():
        query = f"{query}, {city}"
    if state and state.lower() not in query.lower():
        query = f"{query}, {state}"
    if country and country.lower() not in query.lower():
        query = f"{query}, {country}"

    if country:
        components.append(f"country:{country}")
    if state:
        components.append(f"administrative_area:{state}")
    if city:
        components.append(f"locality:{city}")

    # Try Google Maps Geocoding API first if the key is configured
    gkey = os.environ.get("GOOGLE_MAPS_API_KEY")
    if gkey:
        try:
            g_params = {"address": query, "key": gkey, "language": language}
            if region:
                g_params["region"] = region
            if components:
                g_params["components"] = "|".join(components)
            if bounds:
                # bounds stored as (lon_min, lat_min, lon_max, lat_max)
                g_params["bounds"] = (
                    f"{bounds[1]},{bounds[0]}|{bounds[3]},{bounds[2]}"
                )
            resp = requests.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params=g_params,
                timeout=5,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if results:
                location = results[0]["geometry"]["location"]
                return {
                    "lat": float(location.get("lat")),
                    "lng": float(location.get("lng")),
                    "display_name": results[0].get("formatted_address"),
                    "maps_search_url": f"https://www.google.com/maps/search/?api=1&query={requests.utils.quote(query)}",
                }
        except requests.RequestException as e:
            logger.error(f"Error geocoding address via Google Maps: {e}")

    # Fallback to Nominatim
    url = "https://nominatim.openstreetmap.org/search"
    params = {"format": "json", "limit": 1, "accept-language": language}
    if "&" in query or " esquina " in address.lower():
        params["q"] = query
    else:
        params["q"] = query
        if city:
            params["city"] = city
        if state:
            params["state"] = state
        if country:
            params["countrycodes"] = country.lower()
    if bounds:
        params["viewbox"] = f"{bounds[0]},{bounds[3]},{bounds[2]},{bounds[1]}"
        params["bounded"] = 1
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
