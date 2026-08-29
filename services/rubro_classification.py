"""Lightweight tenant-sector classification helpers.

This module intentionally has no Flask, database, or LLM dependencies so it is
safe to import from authentication and application-startup paths.
"""

from __future__ import annotations


RUBROS_PUBLICOS = {
    "municipio",
    "municipios",
    "municipio inteligente",
    "ong",
    "gobierno",
    "hospital_publico",
    "entidad_publica",
    "municipal",
    "publico",
    "municipalidad",
}


def normalizar_rubro(rubro: object) -> str:
    if not rubro:
        return ""
    if isinstance(rubro, str):
        return rubro.strip().lower()
    if hasattr(rubro, "clave") and getattr(rubro, "clave"):
        return str(rubro.clave).strip().lower()
    if hasattr(rubro, "nombre") and getattr(rubro, "nombre"):
        return str(rubro.nombre).strip().lower()
    return str(rubro).strip().lower()


def es_rubro_publico(rubro: object) -> bool:
    return normalizar_rubro(rubro) in RUBROS_PUBLICOS


__all__ = ["RUBROS_PUBLICOS", "es_rubro_publico", "normalizar_rubro"]
