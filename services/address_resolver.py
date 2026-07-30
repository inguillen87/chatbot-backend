import re
import unicodedata
from typing import Optional, Dict, Any, List

from services.location_service import geocode_address
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
        # Pre-normalize city/state for later token stripping in resolve()
        self._city_norm = self._normalize(self.city)
        self._state_norm = self._normalize(self.state)

    # Normalization
    def _normalize(self, text: str) -> str:
        text = unicodedata.normalize("NFD", text or "").encode("ascii", "ignore").decode("utf-8")
        text = re.sub(r"\s+", " ", text).strip().lower()
        return text

    # Detect intersection
    def _parse_intersection(self, text: str) -> Optional[Dict[str, Any]]:
        # Citizens often append a landmark after the address (for example
        # ``Don Bosco 56 esquina Sarmiento. Plaza Junín``).  Keep it as a
        # reference instead of allowing it to become part of the second street.
        address_part, separator, reference_part = text.partition(".")
        if not separator:
            address_part, separator, reference_part = text.partition(";")
        reference = reference_part.strip(" ,.;") if separator else None
        pattern = r"\b(?:esquina|esq\.?|y|e|&|/)\b"
        if not re.search(pattern, address_part):
            return None
        parts = [p.strip() for p in re.split(pattern, address_part) if p.strip()]
        if len(parts) != 2:
            return None
        street_a = re.sub(r"\d+", "", parts[0]).strip()
        street_b = re.sub(r"\d+", "", parts[1]).strip()
        number_hint_match = re.search(r"\d+", parts[0])
        number_hint = number_hint_match.group(0) if number_hint_match else None
        return {
            "streets": [street_a, street_b],
            "number_hint": number_hint,
            "reference": reference,
        }

    def _geocode(self, street_query: str) -> List[Dict[str, Any]]:
        """
        Geocodes a street query using the application's location_service.
        """
        full_query = f"{street_query}, {self.city}, {self.state}"
        try:
            gmaps_result = geocode_address(full_query)
            if not gmaps_result:
                return []

            lat = gmaps_result.get("geometry", {}).get("location", {}).get("lat")
            # Google Maps API uses 'lng' for longitude.
            lon = gmaps_result.get("geometry", {}).get("location", {}).get("lng")
            display_name = gmaps_result.get("formatted_address")

            if lat is None or lon is None:
                return []

            # AddressResolver expects a list of candidates. We return one from Google.
            return [
                {
                    "lat": lat,
                    "lon": lon,  # The rest of the class expects 'lon'
                    "display_name": display_name,
                    "maps_search_url": f"https://maps.google.com/?q={lat},{lon}",
                    "raw_google_result": gmaps_result,
                }
            ]
        except Exception as exc:
            logger.error(
                "Geocode via location_service failed query_length=%s error_type=%s",
                len(full_query),
                type(exc).__name__,
            )
            return []

    def resolve(self, raw_address: str) -> Optional[Dict[str, Any]]:
        if not raw_address:
            return None
        if raw_address.strip().upper() == "N/A":
            return None
        normalized = self._normalize(raw_address)
        normalized = re.sub(
            r"^(?:(?:la\s+)?(?:direccion|ubicacion|dirección|ubicación|lugar)"
            r"(?:\s+exacta)?\s*(?::|-|es|queda(?:\s+en)?)?\s+)",
            "",
            normalized,
        ).strip()
        # Remove trailing city/province hints from the address itself, but do not
        # strip words from a landmark appended after punctuation (``Plaza
        # Junín`` is a reference name, not merely a municipality suffix).
        suffix_target, suffix_separator, suffix_reference = normalized.partition(".")
        if not suffix_separator:
            suffix_target, suffix_separator, suffix_reference = normalized.partition(";")
        tokens = [t for t in (self._city_norm, self._state_norm) if t]
        changed = True
        while changed:
            changed = False
            for token in tokens:
                pattern = rf"(?:,\s*)?\b{re.escape(token)}\b\s*$"
                new_normalized = re.sub(pattern, "", suffix_target).strip()
                if new_normalized != suffix_target:
                    suffix_target = new_normalized
                    changed = True
        normalized = re.sub(r"\s+", " ", suffix_target).strip().strip(",")
        if suffix_separator and suffix_reference.strip():
            normalized = f"{normalized}{suffix_separator} {suffix_reference.strip()}"

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
                candidates = self._geocode(street_query)
            except Exception as exc:
                logger.error(
                    "Geocode failed for intersection query_length=%s error_type=%s",
                    len(street_query),
                    type(exc).__name__,
                )
                return None
            if not candidates:
                return None
            geo = candidates[0]
            lat = float(geo.get("lat"))
            lon = float(geo.get("lon"))
            validez = self._within_bounds(lat, lon)
            street_a = inter["streets"][0].title()
            if inter.get("number_hint"):
                street_a = f"{street_a} {inter['number_hint']}"
            formatted = (
                f"{street_a} y {inter['streets'][1].title()}, "
                f"{self.city}, {self.state}, {self.country}"
            )
            if inter.get("reference"):
                formatted += f" (referencia: {inter['reference'].title()})"
            return {
                "calle": None,
                "numero": inter.get("number_hint"),
                "entre_calles": [s.title() for s in inter["streets"]],
                "barrio": None,
                "referencia": inter.get("reference").title()
                if inter.get("reference")
                else None,
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
                "candidates": candidates,
            }

        # Single street with optional number anywhere in the text (allow numeric street names)
        matches = list(re.finditer(r"\b(\d+)\b", normalized))
        if matches:
            num_match = matches[-1]
            street = normalized[: num_match.start()].strip()
            number = num_match.group(1)
        else:
            street = normalized.strip()
            number = None
        if not street or street.isdigit():
            return None
        street_query = f"{street} {number}" if number else street
        try:
            candidates = self._geocode(street_query)
        except Exception as exc:
            logger.error(
                "Geocode failed query_length=%s error_type=%s",
                len(street_query),
                type(exc).__name__,
            )
            return None
        if not candidates:
            return None
        geo = candidates[0]
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
            "candidates": candidates,
        }

    def _within_bounds(self, lat: float, lon: float) -> bool:
        if not self.enforce_bounds or not self.bounds:
            return True
        lon_min, lat_min, lon_max, lat_max = self.bounds
        return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max
