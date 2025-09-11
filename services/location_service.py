import logging
import os
from typing import Optional

import re
import requests
from urllib.parse import urlparse, parse_qs, unquote
from urllib.parse import urlparse, parse_qs, unquote

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
    # If an intersection is present, remove standalone numbers near '&'
    if "&" in addr:
        addr = re.sub(r"\b\d+\s*&", "&", addr)
        addr = re.sub(r"&\s*\d+\b", "&", addr)
    return re.sub(r"\s+", " ", addr).strip()


def _extract_from_maps_url(url: str) -> dict:
    """Extract coordinates or query text from a Google Maps URL."""
    try:
        resp = requests.get(url, allow_redirects=True, timeout=5)
        final = resp.url
    except requests.RequestException:
        final = url
    parsed = urlparse(final)
    qs = parse_qs(parsed.query)
    if "q" in qs:
        q = unquote(qs["q"][0])
        m = re.match(r"\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)", q)
        if m:
            return {"lat": float(m.group(1)), "lng": float(m.group(2))}
        return {"text": q}
    m = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", final)
    if m:
        return {"lat": float(m.group(1)), "lng": float(m.group(2))}
    return {"text": unquote(parsed.path)}


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

    # Support direct Google Maps links
    if address.startswith("http"):
        info = _extract_from_maps_url(address)
        if info.get("lat") and info.get("lng"):
            lat, lng = info["lat"], info["lng"]
            gkey = os.environ.get("GOOGLE_MAPS_API_KEY")
            if gkey:
                try:
                    r_params = {
                        "latlng": f"{lat},{lng}",
                        "key": gkey,
                        "language": (geo_ctx or {}).get("locale") or "es-AR",
                    }
                    resp = requests.get(
                        "https://maps.googleapis.com/maps/api/geocode/json",
                        params=r_params,
                        timeout=5,
                    )
                    resp.raise_for_status()
                    results = resp.json().get("results", [])
                    if results:
                        formatted = results[0].get("formatted_address")
                        return {
                            "lat": lat,
                            "lng": lng,
                            "display_name": formatted,
                            "maps_search_url": f"https://www.google.com/maps/search/?api=1&query={lat},{lng}",
                        }
                except requests.RequestException as e:
                    logger.error(f"Error reverse geocoding via Google Maps: {e}")
            return {
                "lat": lat,
                "lng": lng,
                "maps_search_url": f"https://www.google.com/maps/search/?api=1&query={lat},{lng}",
            }
        elif info.get("text"):
            address = info["text"]

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
        country = "AR"

    if city and not re.search(rf"\b{re.escape(city)}\b", query, flags=re.I):
        query = f"{query}, {city}"
    if state and not re.search(rf"\b{re.escape(state)}\b", query, flags=re.I):
        query = f"{query}, {state}"
    if country and not re.search(rf"\b{re.escape(country)}\b", query, flags=re.I):
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

    # Fallback to Nominatim (exclusive modes: free-form "q" OR structured params)
    url = "https://nominatim.openstreetmap.org/search"
    is_corner = ("&" in query) or (" esquina " in address.lower())

    if is_corner:
        # Free-form search for intersections
        params = {
            "format": "json",
            "limit": 1,
            "accept-language": language,
            "q": query,
        }
        if bounds:
            params["viewbox"] = f"{bounds[0]},{bounds[3]},{bounds[2]},{bounds[1]}"
            params["bounded"] = 1
    else:
        # Structured search for addresses with optional number
        street_only = _normalize_corner(address)
        params = {
            "format": "json",
            "limit": 1,
            "accept-language": language,
            "street": street_only,
        }
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
