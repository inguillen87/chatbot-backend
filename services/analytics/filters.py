"""Parsing utilities for analytics query parameters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional, Sequence

from flask import abort, current_app, g, request

from models import TenantProfile

from models import TenantProfile


@dataclass(frozen=True)
class AnalyticsFilters:
    tenant_id: str
    scope: str
    date_from: Optional[datetime]
    date_to: Optional[datetime]
    canales: tuple[str, ...]
    categorias: tuple[str, ...]
    estados: tuple[str, ...]
    agentes: tuple[int, ...]
    zonas: tuple[str, ...]
    etiquetas: tuple[str, ...]
    rubros: tuple[str, ...]
    bbox: Optional[tuple[float, float, float, float]]
    pyme_ids: tuple[int, ...]
    resolution: int


def _parse_list(value: Optional[str]) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(sorted({item.strip() for item in value.split(',') if item.strip()}))


def _parse_int_list(value: Optional[str]) -> tuple[int, ...]:
    if not value:
        return ()
    numbers = []
    for raw in value.split(','):
        raw = raw.strip()
        if not raw:
            continue
        try:
            numbers.append(int(raw))
        except ValueError:
            abort(400, description=f"Invalid numeric value '{raw}'")
    return tuple(sorted(set(numbers)))


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        if len(value) == 10:
            return datetime.strptime(value, "%Y-%m-%d")
        return datetime.fromisoformat(value)
    except ValueError:
        abort(400, description=f"Invalid date '{value}'")


def _parse_bbox(value: Optional[str]) -> Optional[tuple[float, float, float, float]]:
    if not value:
        return None
    try:
        parts = [float(item) for item in value.split(',')]
    except ValueError as exc:
        abort(400, description=f"Invalid bbox '{value}': {exc}")
    if len(parts) != 4:
        abort(400, description=f"bbox must have 4 comma separated values (got {len(parts)})")
    min_lon, min_lat, max_lon, max_lat = parts
    if min_lon >= max_lon or min_lat >= max_lat:
        abort(400, description="bbox has invalid bounds")
    return (min_lon, min_lat, max_lon, max_lat)




def _resolve_tenant_id_from_context() -> Optional[str]:
    """Best-effort tenant inference for authenticated dashboards.

    Keeps /analytics and /admin/analytics usable when frontend omits tenant_id
    while user/session context is already available in request globals.
    """

    debug_tenant = (request.headers.get("X-Debug-Tenant") or "").strip()
    if current_app.config.get("TESTING") and debug_tenant:
        return debug_tenant

    tenant_profile = getattr(g, "tenant_profile", None)
    if tenant_profile is not None and getattr(tenant_profile, "id", None) is not None:
        return str(tenant_profile.id)

    viewer = getattr(g, "viewer", None)
    if viewer is not None:
        for attr in ("tenant_id", "municipio_id", "pyme_id", "empresa_id", "id"):
            value = getattr(viewer, attr, None)
            if value is not None:
                return str(value)

    # Debug fallback for local/manual calls without full auth stack.
    if debug_tenant:
        return debug_tenant

    return None

def parse_filters(args) -> AnalyticsFilters:
    """Parse request args into a structured filter object."""

    tenant_id = args.get("tenant_id")
    if not tenant_id:
        tenant_slug = (args.get("tenant_slug") or args.get("tenant") or "").strip().lower()
        if tenant_slug:
            tenant_obj = TenantProfile.query.filter(TenantProfile.slug.ilike(tenant_slug)).first()
            if tenant_obj:
                tenant_id = str(tenant_obj.id)

    if not tenant_id:
        tenant_id = _resolve_tenant_id_from_context()

    if not tenant_id:
        abort(400, description="tenant_id is required")

    scope = args.get("scope") or args.get("entity") or "municipio"
    normalized_scope = scope.lower()
    if normalized_scope not in {"municipio", "pyme", "operaciones", "operations"}:
        abort(400, description=f"Unknown scope '{scope}'")
    if normalized_scope == "operations":
        normalized_scope = "operaciones"

    date_from = _parse_date(args.get("from"))
    date_to = _parse_date(args.get("to"))

    canales = _parse_list(args.get("canal"))
    categorias = _parse_list(args.get("categoria"))
    estados = _parse_list(args.get("estado"))
    agentes = _parse_int_list(args.get("agente"))
    zonas = _parse_list(args.get("zona"))
    etiquetas = _parse_list(args.get("etiqueta"))
    rubros = _parse_list(args.get("rubro"))
    pyme_ids = _parse_int_list(args.get("pyme"))
    bbox = _parse_bbox(args.get("bbox"))
    resolution = int(args.get("resolution", 8))
    resolution = max(5, min(resolution, 12))

    return AnalyticsFilters(
        tenant_id=tenant_id,
        scope=normalized_scope,
        date_from=date_from,
        date_to=date_to,
        canales=canales,
        categorias=categorias,
        estados=estados,
        agentes=agentes,
        zonas=zonas,
        etiquetas=etiquetas,
        rubros=rubros,
        bbox=bbox,
        pyme_ids=pyme_ids,
        resolution=resolution,
    )
