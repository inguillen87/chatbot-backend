import re
import unicodedata
from typing import Optional, Dict, Any, List

import requests

from services.location_service import NOMINATIM_USER_AGENT
import logging

logger = logging.getLogger(__name__)

class AddressResolver:
    """Resolve and geocode user provided addresses within Junín (Mendoza, AR)."""

    CITY = "Junín"
    STATE = "Mendoza"
    COUNTRY = "AR"
    # Approx bounding box for Junín, Mendoza (lon_min, lat_min, lon_max, lat_max)
    BOUNDS = (-68.6, -33.1, -68.4, -32.9)

    INTERSECTION_TOKENS = ["esquina", "esq", "y", "&", "/"]
    DISTRICT_KEYWORDS = ["distrito", "departamento", "dpto", "partido"]

    def __init__(self):
        pass

    # Normalization
    def _normalize(self, text: str) -> str:
        text = unicodedata.normalize("NFD", text or "").encode("ascii", "ignore").decode("utf-8")
        text = re.sub(r"\s+", " ", text).strip().lower()
        return text

    # Detect intersection
    def _parse_intersection(self, text: str) -> Optional[Dict[str, Any]]:
        pattern = r"\b(?:esquina|esq\.?|y|&|/)\b"
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

    # Geocoding using Nominatim with bounding box
    def _geocode(self, street_query: str) -> Optional[Dict[str, Any]]:
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            "street": street_query,
            "city": self.CITY,
            "state": self.STATE,
            "countrycodes": self.COUNTRY,
            "format": "json",
            "limit": 1,
            "viewbox": f"{self.BOUNDS[0]},{self.BOUNDS[3]},{self.BOUNDS[2]},{self.BOUNDS[1]}",
            "bounded": 1,
        }
        headers = {"User-Agent": NOMINATIM_USER_AGENT}
        resp = requests.get(url, params=params, headers=headers, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        return data[0]

    def resolve(self, raw_address: str) -> Optional[Dict[str, Any]]:
        if not raw_address:
            return None
        normalized = self._normalize(raw_address)

        # Determine if 'San Martin' is used as jurisdiction
        if "san martin" in normalized:
            for kw in self.DISTRICT_KEYWORDS:
                if re.search(rf"{kw}[^a-zA-Z]+san martin", normalized):
                    # If user specifies San Martín as district, it's outside Junín
                    logger.info("Detected jurisdiction San Martín outside Junín")
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
            formatted = f"{inter['streets'][0].title()} y {inter['streets'][1].title()}, {self.CITY}, {self.STATE}, {self.COUNTRY}"
            return {
                "calle": None,
                "numero": None,
                "entre_calles": [s.title() for s in inter["streets"]],
                "barrio": None,
                "localidad": self.CITY,
                "provincia": self.STATE,
                "pais": self.COUNTRY,
                "lat": lat,
                "lon": lon,
                "precision": "intersection",
                "formatted": formatted,
                "validez": validez,
            }

        # Single street with optional number
        match = re.match(r"([^0-9]+)(\d+)?", normalized)
        if not match:
            return None
        street = match.group(1).strip()
        number = match.group(2)
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
        formatted = f"{street.title()}" + (f" {number}" if number else "") + f", {self.CITY}, {self.STATE}, {self.COUNTRY}"
        precision = "point" if number else "approx"
        return {
            "calle": street.title(),
            "numero": number,
            "entre_calles": [],
            "barrio": None,
            "localidad": self.CITY,
            "provincia": self.STATE,
            "pais": self.COUNTRY,
            "lat": lat,
            "lon": lon,
            "precision": precision,
            "formatted": formatted,
            "validez": validez,
        }

    def _within_bounds(self, lat: float, lon: float) -> bool:
        lon_min, lat_min, lon_max, lat_max = self.BOUNDS
        return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max
