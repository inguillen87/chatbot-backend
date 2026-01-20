import logging
import os
from typing import Any, Dict, Optional

import googlemaps

logger = logging.getLogger(__name__)

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")

_DEFAULT_GEO_CONTEXT_SENTINEL = object()
_default_geo_context: Any = _DEFAULT_GEO_CONTEXT_SENTINEL


def _get_default_geo_context() -> Optional[Dict[str, Any]]:
    """Load the geo context for the active municipality when available."""

    global _default_geo_context
    if _default_geo_context is not _DEFAULT_GEO_CONTEXT_SENTINEL:
        return _default_geo_context

    tenant_id = os.environ.get("MUNICIPIO_ID") or "default"
    try:
        from services.geo_context import get_geo_context

        _default_geo_context = get_geo_context(str(tenant_id))
    except Exception:
        logger.debug(
            "No se pudo cargar el contexto geográfico para %s", tenant_id, exc_info=True
        )
        _default_geo_context = None
    return _default_geo_context


def _resolve_geo_ctx(explicit_ctx: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if explicit_ctx:
        return explicit_ctx
    return _get_default_geo_context()


def _build_components(
    geo_ctx: Optional[Dict[str, Any]], *, include_locality: bool
) -> Dict[str, str]:
    components: Dict[str, str] = {}
    country = "AR"
    if geo_ctx:
        country = geo_ctx.get("country") or geo_ctx.get("pais") or country
        state = geo_ctx.get("state") or geo_ctx.get("provincia")
        city = geo_ctx.get("city") or geo_ctx.get("ciudad")
        if include_locality and city:
            components["locality"] = city
        if state:
            components["administrative_area"] = state
    components["country"] = (country or "AR").upper()
    return components


def _select_region(geo_ctx: Optional[Dict[str, Any]]) -> str:
    if geo_ctx:
        region = geo_ctx.get("region_hint") or geo_ctx.get("pais")
        if region:
            return str(region).lower()
    return "ar"


def _build_bounds(geo_ctx: Optional[Dict[str, Any]]):
    if not geo_ctx:
        return None
    bounds = geo_ctx.get("bounds")
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        return None
    try:
        west, south, east, north = [float(value) for value in bounds]
    except (TypeError, ValueError):
        return None
    return {
        "southwest": {"lat": south, "lng": west},
        "northeast": {"lat": north, "lng": east},
    }


def _compute_location_bias(geo_ctx: Optional[Dict[str, Any]]):
    if not geo_ctx:
        return None
    bounds = geo_ctx.get("bounds")
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        return None
    try:
        west, south, east, north = [float(value) for value in bounds]
    except (TypeError, ValueError):
        return None

    center_lat = (south + north) / 2.0
    center_lng = (west + east) / 2.0
    lat_span = abs(north - south)
    lng_span = abs(east - west)
    approx_span = max(lat_span, lng_span)
    if approx_span <= 0:
        radius = 10000
    else:
        radius = int(max(5000, min(50000, (approx_span * 111_000) / 2)))
    return {"location": {"lat": center_lat, "lng": center_lng}, "radius": radius}


def _select_language(geo_ctx: Optional[Dict[str, Any]]) -> str:
    if geo_ctx:
        locale = geo_ctx.get("locale") or geo_ctx.get("idioma")
        if locale:
            normalized = str(locale).replace("_", "-")
            return normalized.split("-")[0]
    return "es"


def get_gmaps_client():
    if not GOOGLE_MAPS_API_KEY:
        logger.error("GOOGLE_MAPS_API_KEY not set.")
        return None
    return googlemaps.Client(key=GOOGLE_MAPS_API_KEY)


def geocode_address(address: str, geo_ctx: Optional[Dict[str, Any]] = None):
    """
    Geocodes an address using the Google Geocoding API.

    Args:
        address (str): The address to geocode.

    Returns:
        dict: A dictionary containing the geocoding results, or None if an error occurs.
    """
    gmaps = get_gmaps_client()
    if not gmaps:
        return None

    context = _resolve_geo_ctx(geo_ctx)
    query_address = address
    if context and isinstance(address, str):
        city = context.get("city") or context.get("ciudad")
        state = context.get("state") or context.get("provincia")
        lower_addr = address.lower()
        if city and str(city).lower() not in lower_addr:
            query_address = f"{query_address}, {city}"
        if state and str(state).lower() not in lower_addr:
            query_address = f"{query_address}, {state}"
    components = _build_components(context, include_locality=True)
    region = _select_region(context)
    bounds = _build_bounds(context)
    request_kwargs: Dict[str, Any] = {
        "region": region,
        "components": components,
    }
    if bounds:
        request_kwargs["bounds"] = bounds

    try:
        geocode_result = gmaps.geocode(
            query_address,
            **request_kwargs,
        )
        if geocode_result:
            return geocode_result[0]
        return None
    except Exception as e:
        logger.error(f"Error geocoding address: {e}", exc_info=True)
        return None


def autocomplete_address(query: str, geo_ctx: Optional[Dict[str, Any]] = None):
    """Returns address suggestions with a municipality-aware bias."""
    gmaps = get_gmaps_client()
    if not gmaps:
        return None

    context = _resolve_geo_ctx(geo_ctx)
    components = _build_components(context, include_locality=False)
    language = _select_language(context)
    bias = _compute_location_bias(context)
    request_kwargs: Dict[str, Any] = {
        "input_text": query,
        "language": language,
        "components": components,
    }
    if bias:
        request_kwargs.update(bias)

    try:
        return gmaps.places_autocomplete(**request_kwargs)
    except Exception as e:
        logger.error(f"Error getting autocomplete suggestions: {e}", exc_info=True)
        return None


def find_nearby_places(location, keyword, radius=1500):
    """
    Finds nearby places using the Google Places API.

    Args:
        location (dict): A dictionary containing the latitude and longitude.
        keyword (str): The keyword to search for.
        radius (int): The search radius in meters.

    Returns:
        list: A list of nearby places, or None if an error occurs.
    """
    gmaps = get_gmaps_client()
    if not gmaps:
        return None

    try:
        places_result = gmaps.places_nearby(
            location=location,
            keyword=keyword,
            radius=radius
        )
        return places_result.get("results", [])
    except Exception as e:
        logger.error(f"Error finding nearby places: {e}", exc_info=True)
        return None
