import re
from typing import Any, Dict, Optional


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _strip_prefix(text: str, prefixes: tuple[str, ...]) -> str:
    lowered = text.lower()
    for prefix in prefixes:
        if lowered.startswith(prefix):
            return text[len(prefix) :].strip(" ,.-")
    return text.strip(" ,.-")


def parse_address(text: str, tenant_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Parse an address string into structured components for municipio usage.

    Returns a dict with keys:
    raw, tipo, calle, numero, entre_calles, barrio, distrito, referencia, query_geocode
    """
    tenant_cfg = tenant_cfg or {}
    raw = _normalize_text(text)
    result: Dict[str, Any] = {
        "raw": raw,
        "tipo": None,
        "calle": None,
        "numero": None,
        "entre_calles": None,
        "barrio": None,
        "distrito": None,
        "referencia": None,
        "query_geocode": raw,
    }

    if not raw:
        return result

    lower = raw.lower()

    plaza_match = re.search(r"\b(plaza|parque|monumento)\s+(.+)", lower, re.IGNORECASE)
    if plaza_match:
        result["tipo"] = "POI"
        result["referencia"] = raw

    intersection = None
    if "esquina" in lower:
        intersection = raw
    else:
        inter_match = re.search(r"(.+?)\s+\b(y|e|con)\b\s+(.+)", raw, re.IGNORECASE)
        if inter_match:
            intersection = raw

    if intersection:
        result["tipo"] = result["tipo"] or "ESQUINA"
        parts = re.split(r"\b(?:esquina|y|e|con)\b", raw, maxsplit=1, flags=re.IGNORECASE)
        streets = [p.strip(" ,.-") for p in parts if p.strip(" ,.-")]
        if len(streets) >= 2:
            result["entre_calles"] = streets[:2]
            result["calle"] = streets[0]
            result["referencia"] = f"{streets[0]} y {streets[1]}"

    number_match = re.search(r"\b(.+?)\s+(\d+[a-zA-Z/-]*)\b", raw)
    if number_match:
        result["tipo"] = result["tipo"] or "CALLE_NUMERO"
        result["calle"] = number_match.group(1).strip(" ,.-")
        result["numero"] = number_match.group(2).strip()

    barrio_match = re.search(r"\b(barrio|b°|bº)\s+([A-Za-zÀ-ÿ'\s]+)", raw, re.IGNORECASE)
    if barrio_match:
        result["barrio"] = _strip_prefix(raw[barrio_match.start() :], ("barrio ", "b° ", "bº "))

    distrito_match = re.search(r"\b(distrito|zona|localidad|ciudad)\s+([A-Za-zÀ-ÿ'\s]+)", raw, re.IGNORECASE)
    if distrito_match:
        result["distrito"] = _strip_prefix(raw[distrito_match.start() :], ("distrito ", "zona ", "localidad ", "ciudad "))

    if "manzana" in lower or re.search(r"\bmz\b", lower):
        result["referencia"] = result["referencia"] or raw

    default_city = tenant_cfg.get("ciudad") or tenant_cfg.get("ciudad_default")
    default_province = tenant_cfg.get("provincia") or tenant_cfg.get("provincia_default")
    geo_parts = [raw]
    if default_city and default_city.lower() not in lower:
        geo_parts.append(str(default_city))
    if default_province and default_province.lower() not in lower:
        geo_parts.append(str(default_province))
    geo_parts.append("Argentina")
    result["query_geocode"] = ", ".join([part for part in geo_parts if part])

    return result
