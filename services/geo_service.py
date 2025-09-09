import os
import httpx


def _parse_google(data: dict) -> dict:
    results = data.get("results") or []
    if not results:
        return {}
    components = results[0].get("address_components", [])
    out = {}
    for comp in components:
        types = comp.get("types", [])
        if "route" in types:
            out["calle"] = comp.get("long_name")
        elif "street_number" in types:
            out["numero"] = comp.get("long_name")
        elif "neighborhood" in types or "sublocality" in types:
            out["barrio"] = comp.get("long_name")
        elif "locality" in types:
            out["localidad"] = comp.get("long_name")
        elif "administrative_area_level_1" in types:
            out["provincia"] = comp.get("long_name")
        elif "postal_code" in types:
            out["codigo_postal"] = comp.get("long_name")
    out["display"] = results[0].get("formatted_address")
    return out


def _parse_nominatim(data: dict) -> dict:
    addr = data.get("address", {})
    return {
        "calle": addr.get("road") or addr.get("pedestrian"),
        "numero": addr.get("house_number"),
        "barrio": addr.get("suburb") or addr.get("neighbourhood"),
        "localidad": addr.get("city") or addr.get("town") or addr.get("village"),
        "provincia": addr.get("state"),
        "codigo_postal": addr.get("postcode"),
        "display": data.get("display_name"),
    }


def reverse_geocode(lat: float, lon: float) -> dict:
    """Return address dict for coordinates using Google Maps or Nominatim."""
    gkey = os.getenv("GOOGLE_MAPS_API_KEY")
    params = {"latlng": f"{lat},{lon}", "key": gkey} if gkey else None
    try:
        if gkey:
            r = httpx.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params=params,
                timeout=10,
            )
            r.raise_for_status()
            return _parse_google(r.json())
    except Exception:
        pass
    # Fallback to Nominatim
    r = httpx.get(
        "https://nominatim.openstreetmap.org/reverse",
        params={"format": "jsonv2", "lat": lat, "lon": lon, "addressdetails": 1},
        headers={"User-Agent": "chatboc/municipios"},
        timeout=10,
    )
    r.raise_for_status()
    return _parse_nominatim(r.json())
