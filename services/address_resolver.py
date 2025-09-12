import re
import unicodedata
from typing import Optional, Dict, Any, List

import requests

from services.location_service import NOMINATIM_USER_AGENT, geocode_address
import logging

logger = logging.getLogger(__name__)


class AddressResolver:
    """Resolve and geocode user-provided addresses constrained to a municipality.

    Parameters are supplied via ``municipio_config`` which should include:
    ``ciudad`` (city/locality name), ``provincia`` (state/province), ``pais``
    (country code) and ``bounds`` (lon_min, lat_min, lon_max, lat_max).  An
    optional ``conflicting_jurisdicciones`` list can be provided to mark text
    fragments that should be treated as external jurisdictions (e.g.,
    ``"san martin"`` for Junín).
    """

    INTERSECTION_TOKENS = ["esquina", "esq", "y", "e", "&", "/"]
    DISTRICT_KEYWORDS = ["distrito", "departamento", "dpto", "partido"]

    def __init__(self, municipio_config: Dict[str, Any], enforce_bounds: bool = True):
        self.city = municipio_config.get("ciudad")
        self.state = municipio_config.get("provincia")
        self.country = municipio_config.get("pais", "AR")
        self.bounds = municipio_config.get("bounds")
        self.region_hint = municipio_config.get("region_hint")
        self.locale = municipio_config.get("locale")
        self.enforce_bounds = enforce_bounds
        self.conflicting = [
            self._normalize(n)
            for n in municipio_config.get("conflicting_jurisdicciones", [])
        ]
        if not all([self.city, self.state, self.country, self.bounds]):
            raise ValueError("Municipio config must include ciudad, provincia, pais and bounds")

    # Normalization
    def _normalize(self, text: str) -> str:
        text = unicodedata.normalize("NFD", text or "").encode("ascii", "ignore").decode("utf-8")
        text = re.sub(r"\s+", " ", text).strip().lower()
        return text

    # Detect intersection
    def _parse_intersection(self, text: str) -> Optional[Dict[str, Any]]:
        pattern = r"\b(?:esquina|esq\.?|y|e|&|/)\b"
        if not re.search(pattern, text):
            return None
        parts = [p.strip() for p in re.split(pattern, text) if p.strip()]
        if len(parts) != 2:
            return None
        street_a = re.sub(r"\d+", "", parts[0]).strip()
        street_b = re.sub(r"\d+", "", parts[1]).strip()
        number_hint_match = re.search(r"\d+", parts[0])
        number_hint = number_hint_match.group(0) if number_hint_match else None
        return {"streets": [street_a, street_b], "number_hint": number_hint}

    # Geocoding using Nominatim with bounding box and Google fallback
    def _geocode(self, street_query: str) -> Optional[Dict[str, Any]]:
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            "street": street_query,
            "city": self.city,
            "state": self.state,
            "countrycodes": self.country,
            "format": "json",
            "limit": 1,
        }
        if self.enforce_bounds and self.bounds:
            params["viewbox"] = f"{self.bounds[0]},{self.bounds[3]},{self.bounds[2]},{self.bounds[1]}"
            params["bounded"] = 1
        headers = {"User-Agent": NOMINATIM_USER_AGENT}
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            if data:
                item = data[0]
                lat = float(item.get("lat"))
                lon = float(item.get("lon"))
                return {
                    "lat": lat,
                    "lon": lon,
                    "display_name": item.get("display_name"),
                    "maps_search_url": f"https://www.google.com/maps/search/?api=1&query={lat},{lon}",
                }
        except Exception as e:
            logger.warning("Geocode via Nominatim failed for '%s': %s", street_query, e)

        # Fallback multi-tenant (Google si hay key; si no, Nominatim sesgado)
        try:
            alt = geocode_address(
                street_query,
                geo_ctx={
                    "city": self.city,
                    "state": self.state,
                    "country": self.country,
                    "bounds": self.bounds if self.enforce_bounds else None,
                    "region_hint": self.region_hint,
                    "locale": self.locale,
                },
            )
            if alt:
                lat = alt.get("lat")
                lon = alt.get("lng")
                return {
                    "lat": lat,
                    "lon": lon,
                    "display_name": alt.get("display_name"),
                    "maps_search_url": alt.get("maps_search_url")
                    or f"https://www.google.com/maps/search/?api=1&query={lat},{lon}",
                }
        except Exception as e:
            logger.error("Fallback geocode failed for '%s': %s", street_query, e)
        return None

    def resolve(self, raw_address: str) -> Optional[Dict[str, Any]]:
        if not raw_address:
            return None
        if raw_address.strip().upper() == "N/A":
            return None
        normalized = self._normalize(raw_address)

        # Detect external jurisdictions mentioned explicitly
        for j in self.conflicting:
            if j in normalized:
                for kw in self.DISTRICT_KEYWORDS:
                    if re.search(rf"{kw}[^a-zA-Z]+{j}", normalized):
                        logger.info("Detected conflicting jurisdiction '%s'", j)
                        return {"validez": False}

        inter = self._parse_intersection(normalized)
        if inter:
            street_query = f"{inter['streets'][0]} & {inter['streets'][1]}"
            try:
                geo = self._geocode(street_query)
            except Exception as e:
                logger.error(f"Geocode failed for intersection '{street_query}': {e}")
                return None
            if not geo:
                return None
            lat = float(geo.get("lat"))
            lon = float(geo.get("lon"))
            validez = self._within_bounds(lat, lon)
            formatted = f"{inter['streets'][0].title()} y {inter['streets'][1].title()}, {self.city}, {self.state}, {self.country}"
            return {
                "calle": None,
                "numero": None,
                "entre_calles": [s.title() for s in inter["streets"]],
                "barrio": None,
                "localidad": self.city,
                "provincia": self.state,
                "pais": self.country,
                "lat": lat,
                "lon": lon,
                "precision": "intersection",
                "formatted": formatted,
                "validez": validez,
                "display_name": geo.get("display_name"),
                "maps_search_url": geo.get("maps_search_url"),
            }

        # Single street with optional number (allow numeric street names)
        match = re.match(r"(.*?)(?:\s+(\d+))?$", normalized)
        street = match.group(1).strip() if match else ""
        number = match.group(2) if match else None
        if not street or street.isdigit():
            return None
        street_query = f"{street} {number}" if number else street
        try:
            geo = self._geocode(street_query)
        except Exception as e:
            logger.error(f"Geocode failed for '{street_query}': {e}")
            return None
        if not geo:
            return None
        lat = float(geo.get("lat"))
        lon = float(geo.get("lon"))
        validez = self._within_bounds(lat, lon)
        formatted = f"{street.title()}" + (f" {number}" if number else "") + f", {self.city}, {self.state}, {self.country}"
        precision = "point" if number else "approx"
        return {
            "calle": street.title(),
            "numero": number,
            "entre_calles": [],
            "barrio": None,
            "localidad": self.city,
            "provincia": self.state,
            "pais": self.country,
            "lat": lat,
            "lon": lon,
            "precision": precision,
            "formatted": formatted,
            "validez": validez,
            "display_name": geo.get("display_name"),
            "maps_search_url": geo.get("maps_search_url"),
        }

    def _within_bounds(self, lat: float, lon: float) -> bool:
        if not self.enforce_bounds or not self.bounds:
            return True
        lon_min, lat_min, lon_max, lat_max = self.bounds
        return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max
