"""Utilities to normalize and geocode addresses with LLM fallback."""
from __future__ import annotations

from typing import Dict, Any, Optional

from .address_resolver import AddressResolver
from .llm_utils import llamar_llm_para_json_estructurado


def normalize_and_geocode(raw_address: str, municipio_cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Resolve an address string into coordinates using heuristics and an LLM.

    The function first attempts to geocode the provided text using
    :class:`AddressResolver`. If that fails or returns an invalid result, the
    text is sent to an LLM asking for structured components (street, number,
    intersection, barrio). Those components are recombined into a cleaner
    query and geocoded again.
    """
    if not raw_address:
        return None

    try:
        resolver = AddressResolver(municipio_cfg)
    except Exception:
        resolver = None

    if resolver:
        parsed = resolver.resolve(raw_address)
        if parsed and parsed.get("lat") and parsed.get("lon"):
            return parsed

    # LLM fallback: ask the model to split the address into components
    llm_resp = llamar_llm_para_json_estructurado(
        "Extrae componentes de una dirección",
        f"Texto: {raw_address}",
    ) or {}

    parts = []
    calle = llm_resp.get("calle")
    numero = llm_resp.get("numero")
    inter = llm_resp.get("interseccion") or llm_resp.get("entre_calles")
    barrio = llm_resp.get("barrio")
    if calle:
        part = calle
        if numero:
            part += f" {numero}"
        parts.append(part)
    if inter:
        parts.append(f"esquina {inter}")
    if barrio:
        parts.append(barrio)
    if parts:
        cleaned = " ".join(parts)
        if resolver:
            parsed = resolver.resolve(cleaned)
            if parsed and parsed.get("lat") and parsed.get("lon"):
                return parsed
        # Even if geocoding fails, return a structured representation so the
        # caller can continue the flow without repeatedly asking for barrio.
        formatted = f"{cleaned.title()}, {municipio_cfg.get('ciudad')}, {municipio_cfg.get('provincia')}, {municipio_cfg.get('pais', 'AR')}"
        return {
            "calle": calle.title() if calle else None,
            "numero": numero,
            "entre_calles": [],
            "barrio": barrio.title() if isinstance(barrio, str) else None,
            "localidad": municipio_cfg.get("ciudad"),
            "provincia": municipio_cfg.get("provincia"),
            "pais": municipio_cfg.get("pais", "AR"),
            "lat": None,
            "lon": None,
            "precision": "manual",
            "formatted": formatted,
            "maps_search_url": None,
        }
    return None
