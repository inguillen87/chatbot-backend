"""Parsing utilities for analytics query parameters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional, Sequence

from flask import abort, current_app, g, request
from sqlalchemy.exc import SQLAlchemyError

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
    # Exact TenantProfile identity when the request/session already proved it.
    # ``tenant_id`` remains the legacy owner identifier used by analytics
    # snapshots and RBAC, so existing callers keep their contract.
    tenant_profile_id: Optional[int] = None


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




def _tenant_owner_id_for_scope(tenant: TenantProfile, scope: str) -> Optional[str]:
    owner_id = tenant.pyme_id if scope == "pyme" else tenant.municipio_id
    return str(owner_id) if owner_id is not None else None


def _resolve_tenant_from_context(scope: str) -> tuple[Optional[str], Optional[int]]:
    """Best-effort tenant inference for authenticated dashboards.

    Keeps /analytics and /admin/analytics usable when frontend omits tenant_id
    while user/session context is already available in request globals.
    """

    debug_tenant = (request.headers.get("X-Debug-Tenant") or "").strip()
    if current_app.config.get("TESTING") and debug_tenant:
        return debug_tenant, None

    tenant_profile = getattr(g, "tenant_profile", None)
    if tenant_profile is not None and getattr(tenant_profile, "id", None) is not None:
        owner_id = _tenant_owner_id_for_scope(tenant_profile, scope)
        if owner_id:
            return owner_id, int(tenant_profile.id)

    viewer = getattr(g, "viewer", None)
    if viewer is not None:
        preferred_attrs = (
            ("pyme_id", "empresa_id", "tenant_id", "id")
            if scope == "pyme"
            else ("municipio_id", "empresa_id", "tenant_id", "id")
        )
        for attr in preferred_attrs:
            value = getattr(viewer, attr, None)
            if value is not None:
                return str(value), None

    # Debug fallback for local/manual calls without full auth stack.
    if debug_tenant:
        return debug_tenant, None

    return None, None

def parse_filters(args) -> AnalyticsFilters:
    """Parse request args into a structured filter object."""

    scope = args.get("scope") or args.get("entity") or "municipio"
    normalized_scope = scope.lower()
    if normalized_scope not in {"municipio", "pyme", "operaciones", "operations"}:
        abort(400, description=f"Unknown scope '{scope}'")
    if normalized_scope == "operations":
        normalized_scope = "operaciones"

    tenant_id = args.get("tenant_id")
    raw_tenant_profile_id = args.get("tenant_profile_id")
    try:
        tenant_profile_id = int(raw_tenant_profile_id) if raw_tenant_profile_id not in (None, "") else None
    except (TypeError, ValueError):
        abort(400, description="tenant_profile_id must be an integer")
    if tenant_profile_id is not None:
        tenant_obj = TenantProfile.query.filter_by(id=tenant_profile_id).one_or_none()
        if tenant_obj is None:
            abort(400, description="tenant context is invalid")
        owner_scope = "pyme" if normalized_scope == "pyme" else "municipio"
        owner_id = _tenant_owner_id_for_scope(tenant_obj, owner_scope)
        if not owner_id:
            abort(400, description="tenant context is invalid for scope")
        if tenant_id and str(tenant_id) != owner_id:
            abort(400, description="tenant context is inconsistent")
        tenant_id = owner_id

    if not tenant_id:
        tenant_slug = (args.get("tenant_slug") or args.get("tenant") or "").strip().lower()
        if tenant_slug:
            try:
                tenant_obj = TenantProfile.query.filter(TenantProfile.slug.ilike(tenant_slug)).one_or_none()
            except SQLAlchemyError:
                current_app.logger.exception("[analytics] tenant_slug resolution failed slug=%s", tenant_slug)
                tenant_obj = None
            if tenant_obj:
                owner_scope = "pyme" if normalized_scope == "pyme" else "municipio"
                tenant_id = _tenant_owner_id_for_scope(tenant_obj, owner_scope)
                tenant_profile_id = int(tenant_obj.id)

    if not tenant_id:
        tenant_id, context_profile_id = _resolve_tenant_from_context(normalized_scope)
        tenant_profile_id = tenant_profile_id or context_profile_id

    if not tenant_id:
        abort(400, description="tenant_id is required")

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
        tenant_profile_id=tenant_profile_id,
    )
