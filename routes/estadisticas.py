from datetime import datetime, timedelta

from flask import Blueprint, current_app, jsonify, request, g

from utils.auth_helpers import token_requerido, admin_o_empleado_requerido
from utils.time_utils import get_local_now
from services.ticket_service import servicio_tickets
from services.municipal_stats import build_stats_for_municipio, StatsFilters
from services.metricas_service import MetricasService
from models import User, db
from utils.heatmap import aggregate_heatmap_points, build_feature_collection, enrich_heatmap_points
from utils.map_config import get_map_config
from utils.tenant import get_current_tenant, get_current_tenant_profile, get_current_tenant_slug
from services.tenant_resolver import TenantResolutionError, resolve_tenant_only
from services.operational_heatmap_access import (
    build_employee_legacy_heatmap_points,
    is_employee_heatmap_viewer,
)
from utils.roles import is_authorized_superadmin_user


estadisticas_bp = Blueprint("estadisticas", __name__, url_prefix="/estadisticas")


def _resolve_municipio_id(user):
    """Return the municipio identifier associated with the current request."""

    municipio_id = getattr(user, "municipio_id", None)
    if municipio_id is not None:
        return municipio_id

    owner_user = getattr(g, "owner_user", None)
    if owner_user is not None:
        owner_municipio_id = getattr(owner_user, "municipio_id", None)
        if owner_municipio_id is not None:
            return owner_municipio_id
        if getattr(owner_user, "tipo_chat", None) == "municipio":
            owner_id = getattr(owner_user, "id", None)
            if owner_id is not None:
                return owner_id

    empresa_id = getattr(user, "empresa_id", None)
    if empresa_id is not None:
        return empresa_id

    if getattr(user, "tipo_chat", None) == "municipio":
        fallback_id = getattr(user, "id", None)
        if fallback_id is not None:
            return fallback_id

    return None


def _parse_multi_value_param(args, key: str) -> list[str]:
    """Devuelve los valores únicos enviados para ``key`` en el query string."""

    valores: list[str] = []

    valores.extend([valor.strip() for valor in args.getlist(key) if valor])
    valores.extend(
        [valor.strip() for valor in args.getlist(f"{key}[]") if valor]
    )

    if not valores:
        raw = args.get(key)
        if raw:
            valores.extend(
                [parte.strip() for parte in raw.split(",") if parte.strip()]
            )

    if not valores:
        return []

    vistos: set[str] = set()
    resultado: list[str] = []
    for valor in valores:
        if valor and valor not in vistos:
            resultado.append(valor)
            vistos.add(valor)

    return resultado


def _parse_estado_params(args) -> list[str] | None:
    """Normaliza los parámetros ``estado`` del query string."""

    estados = _parse_multi_value_param(args, "estado")
    return estados or None


def _parse_int_params(args, key: str) -> tuple[int, ...]:
    """Convierte parámetros repetibles a tuplas de enteros."""

    valores_crudos = _parse_multi_value_param(args, key)
    if not valores_crudos:
        return ()

    enteros: list[int] = []
    vistos: set[int] = set()
    for raw in valores_crudos:
        try:
            numero = int(raw)
        except (TypeError, ValueError):
            continue
        if numero not in vistos:
            vistos.add(numero)
            enteros.append(numero)

    return tuple(enteros)


def _parse_bool_param(args, key: str) -> bool | None:
    """Convierte parámetros booleanos del query string."""

    raw = args.get(key)
    if raw is None:
        return None

    texto = str(raw).strip().lower()
    if texto in {"1", "true", "t", "yes", "si", "sí"}:
        return True
    if texto in {"0", "false", "f", "no"}:
        return False

    return None


def _normalize_tenant_slug(raw_slug: str | None) -> str | None:
    if not raw_slug:
        return None

    slug = raw_slug.strip().lower()
    if not slug:
        return None

    alias_map = dict(current_app.config.get("TENANT_ALIAS_MAP", {}) or {})
    alias_target = current_app.config.get("PUBLIC_CATALOG_DEFAULT_TENANT")
    if alias_target:
        alias_map.setdefault("whatsapp", alias_target)
        alias_map.setdefault("pwa", alias_target)

        # Los dashboards de estadísticas suelen invocarse con
        # ``tenant_slug=estadisticas`` desde el frontend. Si ese alias no
        # existe como tenant real, degradamos al tenant por defecto (ej. el
        # municipal) en lugar de responder 404.
        alias_map.setdefault("estadisticas", alias_target)

    # Fallback razonable cuando no hay alias configurado: tratar
    # ``estadisticas`` como sinónimo de municipio para mantener compatibilidad
    # con widgets viejos.
    alias_map.setdefault("estadisticas", "municipio")

    return alias_map.get(slug, slug)


def _resolve_tenant_profile_or_error(args) -> object:
    slug_hint = (
        args.get("tenant_slug")
        or args.get("tenant")
        or get_current_tenant_slug()
    )
    slug_hint = _normalize_tenant_slug(slug_hint)

    if not slug_hint and current_app.config.get("TESTING"):
        return None

    try:
        tenant = resolve_tenant_only(
            tenant_slug=slug_hint,
            require_explicit_slug=bool(slug_hint),
        )
    except TenantResolutionError as exc:
        raise TenantResolutionError(str(exc))
    except Exception:
        if current_app.config.get("TESTING"):
            return None
        raise

    if not tenant and not slug_hint:
        tenant = get_current_tenant_profile()

    if not tenant and slug_hint:
        raise TenantResolutionError("Tenant desconocido")

    if tenant:
        g.tenant_profile = tenant
        g.current_tenant = tenant
        g.current_tenant_slug = getattr(tenant, "slug", None)
    return tenant


def _stats_tenant_access_allowed(current_user, tenant) -> bool:
    if tenant is None:
        return bool(current_app.config.get("TESTING"))
    if current_user is None:
        return False
    if is_authorized_superadmin_user(current_user):
        return True

    owner = current_user
    empresa_id = getattr(current_user, "empresa_id", None)
    if empresa_id:
        owner = db.session.get(User, empresa_id) or current_user

    tenant_ids = {
        getattr(current_user, "tenant_id", None),
        getattr(owner, "tenant_id", None),
    } - {None}
    if getattr(tenant, "id", None) in tenant_ids:
        return True

    owner_id = getattr(owner, "id", None)
    municipality_ids = {
        getattr(current_user, "municipio_id", None),
        getattr(owner, "municipio_id", None),
        owner_id if getattr(owner, "tipo_chat", None) == "municipio" else None,
    } - {None}
    business_ids = {
        getattr(current_user, "pyme_id", None),
        getattr(owner, "pyme_id", None),
        owner_id if getattr(owner, "tipo_chat", None) == "pyme" else None,
    } - {None}
    return bool(
        getattr(tenant, "municipio_id", None) in municipality_ids
        or getattr(tenant, "pyme_id", None) in business_ids
    )


def _resolve_stats_rubro_id(current_user, tenant) -> int | None:
    tenant_owner = getattr(tenant, "pyme", None) if tenant is not None else None
    return getattr(tenant_owner, "rubro_id", None) or getattr(current_user, "rubro_id", None)


def _mark_empty_heatmap_payload(payload: dict[str, object], *, key: str = "heatmap") -> None:
    map_config = get_map_config()
    if map_config:
        payload.setdefault("map_config", map_config)
    payload[key] = []
    payload[f"{key}_cells"] = []
    payload[f"{key}_geojson"] = {"type": "FeatureCollection", "features": []}
    payload[f"{key}_cells_geojson"] = {"type": "FeatureCollection", "features": []}
    metadata = payload.setdefault("metadata", {})
    if isinstance(metadata, dict):
        map_metadata = metadata.setdefault("map", {})
        if isinstance(map_metadata, dict):
            map_metadata[key] = {
                "point_count": 0,
                "cell_count": 0,
                "can_render_heatmap": False,
                "empty_reason": "no_real_geo_points",
                "rendering": {
                    "recommended_engine": "maplibre-gl",
                    "supports_animations": False,
                    "supports_clusters": False,
                    "supports_heatmap": False,
                },
            }
    payload["render_contract"] = {
        "module": key,
        "state": "empty",
        "can_render_heatmap": False,
        "empty_reason": "no_real_geo_points",
        "source_keys": [key, f"{key}_cells", f"metadata.map.{key}"],
    }




def _build_showcase_metrics(points: list[dict[str, object]]) -> dict[str, object]:
    """Build front-end friendly KPI and animation hints for premium dashboards."""

    if not points:
        return {
            "events_total": 0,
            "active_zones": 0,
            "top_hotspots": [],
            "sparkline": [],
            "animation": {"enabled": False},
        }

    total_events = 0.0
    hotspots: dict[str, float] = {}
    sparkline: list[float] = []

    for idx, point in enumerate(points):
        weight = float(point.get("weight", 1) or 1)
        total_events += weight
        location = point.get("location") if isinstance(point.get("location"), dict) else {}
        label = (
            point.get("barrio")
            or point.get("distrito")
            or location.get("barrio")
            or location.get("ciudad")
            or f"Zona {idx + 1}"
        )
        hotspots[label] = hotspots.get(label, 0.0) + weight
        sparkline.append(round(weight, 2))

    top_hotspots = sorted(
        ({"label": label, "weight": round(value, 2)} for label, value in hotspots.items()),
        key=lambda item: item["weight"],
        reverse=True,
    )[:6]

    # Keep sparkline compact and deterministic for UI cards
    if len(sparkline) > 24:
        step = max(1, len(sparkline) // 24)
        sparkline = sparkline[::step][:24]

    return {
        "events_total": int(round(total_events)),
        "active_zones": len(hotspots),
        "top_hotspots": top_hotspots,
        "sparkline": sparkline,
        "animation": {
            "enabled": True,
            "pulse_interval_ms": 3200,
            "toast_lifetime_ms": 4200,
            "recommended_layers": ["heatmap", "clusters", "pulses", "arcs"],
        },
    }

def _augment_heatmap_payload(payload: dict[str, object], *, key: str = "heatmap") -> None:
    """Attach shared heatmap representations, metadata and provider hints."""

    map_config = get_map_config()
    if map_config:
        payload.setdefault("map_config", map_config)

    points = payload.get(key)
    if not isinstance(points, list) or not points:
        return

    enrich_heatmap_points(
        points,
        property_keys=("categoria", "estado", "barrio", "fuente", "canal", "total"),
    )

    def _style_hint(items: list[dict[str, object]]) -> dict[str, object]:
        intensities = [float(p.get("intensity", 0.0) or 0.0) for p in items]
        max_intensity = max(intensities) if intensities else 0.0
        if max_intensity < 0:
            max_intensity = 0.0
        if max_intensity >= 0.75:
            recommended_radius = 28
        elif max_intensity >= 0.4:
            recommended_radius = 22
        else:
            recommended_radius = 16

        gradient = [
            {"stop": 0.0, "color": "rgba(0, 126, 255, 0)"},
            {"stop": 0.3, "color": "rgba(0, 126, 255, 0.6)"},
            {"stop": 0.6, "color": "rgba(255, 200, 0, 0.85)"},
            {"stop": 1.0, "color": "rgba(255, 60, 0, 1)"},
        ]

        return {
            "max_intensity": round(max_intensity, 4),
            "recommended_radius": recommended_radius,
            "gradient": gradient,
        }

    feature_collection = build_feature_collection(points)
    supported_formats: list[str] = ["points"]
    preferred_format = "points"

    if feature_collection:
        payload[f"{key}_geojson"] = feature_collection
        supported_formats.append("geojson")
        preferred_format = "geojson"

    provider_hint = map_config.get("provider") if isinstance(map_config, dict) else None
    if not provider_hint or provider_hint in {"none", "google"}:
        provider_hint = "maplibre"

    layers = payload.setdefault("map_layers", {})
    if isinstance(layers, dict):
        layers[key] = {
            "kind": "heatmap",
            "supported_formats": supported_formats,
            "preferred_format": preferred_format,
            "provider_hint": provider_hint,
            "source_keys": {"points": key, "geojson": f"{key}_geojson"},
        }
        layers[f"{key}_pulses"] = {
            "kind": "pulses",
            "provider_hint": provider_hint,
            "source_keys": {"points": key},
            "style": {"radius": 8, "glow": 0.85},
        }

    # Aggregate the points into grid cells so that MapLibre/MapTiler can render
    # either a heatmap or clustered overlays without relying on the deprecated
    # Google APIs.
    cells, cells_metadata = aggregate_heatmap_points(
        points,
        categorical_keys=("categoria", "estado", "barrio", "fuente", "canal"),
    )

    if cells:
        pulses = []
        for idx, cell in enumerate(cells[:25]):
            if not isinstance(cell, dict):
                continue
            location = cell.get("location") or {}
            pulses.append(
                {
                    "id": f"{key}_pulse_{idx+1}",
                    "lat": location.get("lat"),
                    "lng": location.get("lng"),
                    "intensity": cell.get("intensity"),
                    "weight": cell.get("count"),
                }
            )
        payload[f"{key}_cells"] = cells
        if pulses:
            payload[f"{key}_pulses"] = pulses
        cells_geojson = build_feature_collection(cells)
        if cells_geojson:
            payload[f"{key}_cells_geojson"] = cells_geojson

        if isinstance(layers, dict):
            layers[f"{key}_cells"] = {
                "kind": "grid",
                "supported_formats": ["cells", "geojson"],
                "preferred_format": "geojson",
                "provider_hint": provider_hint,
                "source_keys": {
                    "cells": f"{key}_cells",
                    "geojson": f"{key}_cells_geojson",
                },
            }
            if payload.get(f"{key}_pulses"):
                layers[f"{key}_pulses"] = {
                    "kind": "pulse",
                    "supported_formats": ["points"],
                    "preferred_format": "points",
                    "provider_hint": provider_hint,
                    "source_keys": {"points": f"{key}_pulses"},
                }

    metadata = payload.setdefault("metadata", {})
    if isinstance(metadata, dict):
        map_metadata = metadata.setdefault("map", {})
        if isinstance(map_metadata, dict):
            top_cells = sorted(
                cells,
                key=lambda cell: float(cell.get("count", 0.0) or 0.0),
                reverse=True,
            )[:5]
            cinematic_events = []
            for idx, cell in enumerate(top_cells, start=1):
                if not isinstance(cell, dict):
                    continue
                loc = cell.get("location") or {}
                cinematic_events.append(
                    {
                        "rank": idx,
                        "lat": loc.get("lat"),
                        "lng": loc.get("lng"),
                        "label": cell.get("barrio") or cell.get("distrito") or f"Zona {idx}",
                        "weight": cell.get("count"),
                        "intensity": cell.get("intensity"),
                        "pulse_ms": 1200 + idx * 180,
                    }
                )

            showcase = _build_showcase_metrics(points)
            if isinstance(showcase, dict):
                anim = showcase.setdefault("animation", {})
                if isinstance(anim, dict):
                    anim.setdefault("enabled", bool(points))
                    anim.setdefault("default", "pulse")

            map_metadata[key] = {
                "point_count": cells_metadata.get("point_count"),
                "cell_count": cells_metadata.get("cell_count"),
                "max_point_weight": cells_metadata.get("max_point_weight"),
                "max_cell_count": cells_metadata.get("max_cell_count"),
                "total_weight": cells_metadata.get("total_weight"),
                "resolution": cells_metadata.get("resolution"),
                "bounds": cells_metadata.get("bounds"),
                "centroid": cells_metadata.get("centroid"),
                "provider_hint": provider_hint,
                "style": _style_hint(points),
                "showcase": showcase,
                "hotspots": cinematic_events,
                "rendering": {
                    "recommended_engine": "maplibre-gl",
                    "supports_animations": True,
                    "supports_clusters": True,
                    "supports_heatmap": True,
                },
            }

        category_palette = [
            "#EF4444",
            "#F97316",
            "#EAB308",
            "#22C55E",
            "#06B6D4",
            "#3B82F6",
            "#8B5CF6",
            "#EC4899",
        ]
        grouped_categories: dict[str, dict[str, object]] = {}
        for point in points:
            if not isinstance(point, dict):
                continue
            categoria = str(point.get("categoria") or "sin_categoria").strip().lower() or "sin_categoria"
            lat = point.get("lat")
            lng = point.get("lng")
            if lat is None or lng is None:
                continue
            weight = float(point.get("weight") or point.get("w") or point.get("count") or 1.0)
            bucket = grouped_categories.setdefault(categoria, {"count": 0, "weight": 0.0, "points": []})
            bucket["count"] = int(bucket.get("count") or 0) + 1
            bucket["weight"] = float(bucket.get("weight") or 0.0) + max(weight, 0.0)
            point_list = bucket.setdefault("points", [])
            if isinstance(point_list, list):
                point_list.append({"lat": float(lat), "lng": float(lng), "weight": round(max(weight, 0.0), 4)})

        ranked_categories = sorted(grouped_categories.items(), key=lambda item: float(item[1].get("weight") or 0.0), reverse=True)
        max_weight = max((float(data.get("weight") or 0.0) for _, data in ranked_categories), default=0.0)
        category_items = []
        for idx, (name, data) in enumerate(ranked_categories):
            total_weight = float(data.get("weight") or 0.0)
            category_items.append(
                {
                    "categoria": name,
                    "color": category_palette[idx % len(category_palette)],
                    "event_count": int(data.get("count") or 0),
                    "total_weight": round(total_weight, 4),
                    "intensity": round((total_weight / max_weight) if max_weight > 0 else 0.0, 4),
                    "points": data.get("points") or [],
                }
            )

        metadata["category_layers"] = {
            "provider": provider_hint or "maplibre",
            "tiles": {
                "url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
                "attribution": "© OpenStreetMap contributors",
            },
            "categories": category_items,
            "legend": {"mode": "category_weight", "min_weight": 0, "max_weight": round(max_weight, 4)},
        }

    if isinstance(metadata, dict):
        filters_meta = metadata.setdefault("filters", {})
        if isinstance(filters_meta, dict):
            categorias = sorted({p.get("categoria") for p in points if p.get("categoria")})
            estados = sorted({p.get("estado") for p in points if p.get("estado")})
            distritos = sorted({
                p.get("barrio")
                or p.get("distrito")
                or (p.get("location") or {}).get("barrio")
                for p in points
                if p.get("barrio")
                or p.get("distrito")
                or (isinstance(p.get("location"), dict) and (p.get("location") or {}).get("barrio"))
            })
            if categorias:
                filters_meta.setdefault("categorias", categorias)
            if estados:
                filters_meta.setdefault("estados", estados)
            if distritos:
                filters_meta.setdefault("distritos", distritos)
            filters_meta.setdefault(
                "rangos_tiempo",
                [
                    {"label": "Últimos 7 días", "days": 7},
                    {"label": "Últimos 30 días", "days": 30},
                    {"label": "Últimos 90 días", "days": 90},
                ],
            )

    if isinstance(metadata, dict):
        analytics_meta = metadata.setdefault("analytics", {})
        if isinstance(analytics_meta, dict):
            sorted_points = sorted(
                [p for p in points if isinstance(p, dict)],
                key=lambda item: float(item.get("weight", 0.0) or 0.0),
                reverse=True,
            )
            analytics_meta[key] = {
                "top_points": [
                    {
                        "rank": idx + 1,
                        "lat": (pt.get("location") or {}).get("lat", pt.get("lat")),
                        "lng": (pt.get("location") or {}).get("lng", pt.get("lng")),
                        "weight": pt.get("weight"),
                        "categoria": pt.get("categoria"),
                        "estado": pt.get("estado"),
                    }
                    for idx, pt in enumerate(sorted_points[:10])
                ],
                "kpi": {
                    "coverage_score": round(min(100.0, float(cells_metadata.get("cell_count", 0) or 0) * 2.75), 2),
                    "activity_score": round(min(100.0, float(cells_metadata.get("total_weight", 0.0) or 0.0) * 1.5), 2),
                },
            }


def _prepare_legacy_heatmap_payload(
    payload: dict[str, object],
    points: list[dict[str, object]] | None,
    viewer,
    *,
    key: str = "heatmap",
) -> list[dict[str, object]]:
    """Apply viewer privacy before deriving any legacy heatmap representation."""

    prepared_points = points or []
    employee_privacy: dict[str, object] | None = None
    if is_employee_heatmap_viewer(viewer):
        prepared_points, employee_privacy = build_employee_legacy_heatmap_points(
            prepared_points,
            viewer,
        )

    payload[key] = prepared_points
    if employee_privacy is not None:
        payload["privacy"] = employee_privacy

    if prepared_points:
        _augment_heatmap_payload(payload, key=key)
    else:
        _mark_empty_heatmap_payload(payload, key=key)

    if employee_privacy is not None:
        render_contract = payload.setdefault("render_contract", {})
        if isinstance(render_contract, dict):
            render_contract["privacy_mode"] = "employee_aggregated"
            render_contract["k_min"] = employee_privacy.get("k_min")
            render_contract["coordinate_precision_decimals"] = employee_privacy.get(
                "coordinate_precision_decimals"
            )
            if not prepared_points:
                render_contract["empty_reason"] = employee_privacy.get("empty_reason")

    return prepared_points


def _parse_date_param(value: str | None, *, name: str, is_end: bool = False) -> datetime | None:
    """Parses date parameters supporting YYYY-MM-DD and ISO 8601 (with Z)."""

    if not value:
        return None

    raw = value.strip()
    if not raw:
        return None

    normalized = raw
    if raw.endswith("Z"):
        normalized = f"{raw[:-1]}+00:00"

    parsed = None
    for parser in (
        lambda text: datetime.fromisoformat(text),
        lambda text: datetime.strptime(text, "%Y-%m-%d"),
    ):
        try:
            parsed = parser(normalized)
            break
        except ValueError:
            parsed = None

    if parsed is None:
        raise ValueError(
            f"El parámetro '{name}' tiene un formato inválido. Usa YYYY-MM-DD o ISO 8601."
        )

    tzinfo = get_local_now().tzinfo
    if parsed.tzinfo is None and tzinfo is not None:
        parsed = parsed.replace(tzinfo=tzinfo)

    if is_end and "T" not in raw:
        parsed = parsed + timedelta(days=1)

    return parsed


def _build_stats_filters(args, estados: list[str] | None) -> StatsFilters | None:
    """Construye ``StatsFilters`` reutilizando los parámetros del request."""

    categorias = _parse_multi_value_param(args, "categoria")
    distritos = _parse_multi_value_param(args, "distrito")
    canales = _parse_multi_value_param(args, "canal")
    agentes = _parse_int_params(args, "agente_id")
    if not agentes:
        agentes = _parse_int_params(args, "agente")

    filtros = StatsFilters(
        fecha_inicio=_parse_date_param(args.get("fecha_inicio"), name="fecha_inicio"),
        fecha_fin=_parse_date_param(args.get("fecha_fin"), name="fecha_fin", is_end=True),
        estados=tuple(estados) if estados else None,
        categorias=tuple(categorias) if categorias else None,
        distritos=tuple(distritos) if distritos else None,
        canales=tuple(canales) if canales else None,
        agentes=agentes or None,
    )

    if filtros.is_empty():
        return None

    return filtros


def _serialize_filters(filters: StatsFilters | None) -> dict:
    """Convierte ``StatsFilters`` a un diccionario serializable."""

    if not filters:
        return {}

    def _maybe_iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    return {
        "fecha_inicio": _maybe_iso(filters.fecha_inicio),
        "fecha_fin": _maybe_iso(filters.fecha_fin),
        "estados": list(filters.estados) if filters.estados else [],
        "categorias": list(filters.categorias) if filters.categorias else [],
        "distritos": list(filters.distritos) if filters.distritos else [],
        "canales": list(filters.canales) if filters.canales else [],
        "agentes": list(filters.agentes) if filters.agentes else [],
    }


def _build_summary_cards(summary: dict) -> list[dict]:
    """Devuelve un conjunto fijo de tarjetas KPI para el tablero municipal."""

    abiertos = int(summary.get("abiertos", 0) or 0)
    en_proceso = int(summary.get("en_proceso", 0) or 0)
    resueltos = int(summary.get("resueltos", 0) or 0)

    return [
        {"label": "Reclamos Abiertos", "value": abiertos},
        {"label": "Reclamos en Proceso", "value": en_proceso},
        {"label": "Reclamos Resueltos", "value": resueltos},
    ]


def _normalize_dashboard_tipo(raw_tipo: str | None) -> str:
    """Normaliza el parámetro ``tipo`` para el dashboard unificado."""

    if not raw_tipo:
        return "municipio"

    tipo = raw_tipo.strip().lower()
    if tipo not in {"municipio", "pyme"}:
        return "municipio"

    return tipo


def _resolve_heatmap_ticket_type(args) -> str:
    """Resolve the canonical ticket type without silently changing domains.

    ``tipo`` is the query parameter used by the current frontend, while
    ``tipo_ticket`` is kept for legacy clients.  Conflicting or unsupported
    values must fail closed so a PyME request can never fall back to municipal
    data.
    """

    raw_tipo_ticket = _clean_text_param(args.get("tipo_ticket"))
    raw_tipo = _clean_text_param(args.get("tipo"))

    normalized_tipo_ticket = (
        raw_tipo_ticket.lower() if raw_tipo_ticket is not None else None
    )
    normalized_tipo = raw_tipo.lower() if raw_tipo is not None else None

    if (
        normalized_tipo_ticket is not None
        and normalized_tipo is not None
        and normalized_tipo_ticket != normalized_tipo
    ):
        raise ValueError("tipo y tipo_ticket no pueden identificar dominios distintos")

    ticket_type = normalized_tipo_ticket or normalized_tipo or "municipio"
    if ticket_type not in {"municipio", "pyme"}:
        raise ValueError("tipo debe ser municipio o pyme")

    return ticket_type


def _clean_text_param(value: str | None) -> str | None:
    if value is None:
        return None

    cleaned = value.strip()
    return cleaned or None


def _build_applied_filters(
    *,
    estados: list[str] | None,
    categorias: list[str],
    distrito: str | None,
    fecha_inicio: str | None,
    fecha_fin: str | None,
    satisfactorio: bool | None,
    municipio_id: int | None,
    rubro_id: int | None,
) -> dict:
    """Construye un diccionario serializable con los filtros aplicados."""

    return {
        "estados": estados or [],
        "categorias": categorias,
        "distrito": distrito,
        "fecha_inicio": fecha_inicio,
        "fecha_fin": fecha_fin,
        "satisfactorio": satisfactorio,
        "municipio_id": municipio_id,
        "rubro_id": rubro_id,
    }


@estadisticas_bp.route("/dashboard", methods=["GET"])
@token_requerido
@admin_o_empleado_requerido
def estadisticas_dashboard(current_user):
    """Fusiona estadísticas municipales/pyme en un único payload para dashboards modernos."""

    args = request.args
    tipo = _normalize_dashboard_tipo(args.get("tipo", "municipio"))

    try:
        tenant = _resolve_tenant_profile_or_error(args)
    except TenantResolutionError as exc:
        return jsonify({"error": "tenant_desconocido", "detail": str(exc)}), 404
    if not _stats_tenant_access_allowed(current_user, tenant):
        return jsonify({"error": "tenant_forbidden"}), 403
    tenant_id = getattr(tenant, "id", None)
    if tipo == "pyme" and tenant_id is None and not current_app.config.get("TESTING"):
        return jsonify({"error": "tenant_required"}), 403

    estados = _parse_estado_params(args)
    estado_param = None
    if estados:
        estado_param = estados if len(estados) > 1 else estados[0]

    categorias = _parse_multi_value_param(args, "categoria")

    distrito = _clean_text_param(args.get("distrito"))
    fecha_inicio = args.get("fecha_inicio")
    fecha_fin = args.get("fecha_fin")
    satisfactorio = _parse_bool_param(args, "satisfactorio")

    municipio_id = args.get("municipio_id", type=int)
    rubro_id = args.get("rubro_id", type=int)

    if tipo == "municipio":
        municipio_id = getattr(tenant, "municipio_id", None) or _resolve_municipio_id(current_user)
    if tipo == "pyme":
        rubro_id = _resolve_stats_rubro_id(current_user, tenant)

    try:
        stats_filters = _build_stats_filters(args, estados)
    except ValueError as exc:
        return jsonify({"error": "bad_request", "detail": str(exc)}), 400

    heatmap = servicio_tickets.obtener_tickets_con_ubicacion_para_mapa(
        tipo_ticket=tipo,
        actor=current_user,
        municipio_id=municipio_id,
        rubro_id=rubro_id,
        tenant_id=tenant_id,
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin,
        categoria=categorias or None,
        distrito=distrito,
        estado=estado_param,
        satisfactorio=satisfactorio,
    )
    metadata = {
        "municipio_id": municipio_id,
        "rubro_id": rubro_id,
        "tenant_id": tenant_id,
        "last_updated": get_local_now().isoformat(),
    }

    payload: dict[str, object] = {
        "tipo": tipo,
        "heatmap": heatmap,
        "metadata": metadata,
        "applied_filters": _build_applied_filters(
            estados=estados,
            categorias=categorias,
            distrito=distrito,
            fecha_inicio=fecha_inicio,
            fecha_fin=fecha_fin,
            satisfactorio=satisfactorio,
            municipio_id=municipio_id,
            rubro_id=rubro_id,
        ),
    }

    heatmap = _prepare_legacy_heatmap_payload(payload, heatmap, current_user)

    if tipo == "municipio":
        if stats_filters:
            stats = build_stats_for_municipio(municipio_id, filters=stats_filters, actor=current_user)
        else:
            stats = build_stats_for_municipio(municipio_id, actor=current_user)

        resumen = dict(stats.get("resumen", {})) if isinstance(stats, dict) else {}
        payload["stats"] = stats
        payload["summary"] = resumen
        payload["cards"] = _build_summary_cards(resumen)
        payload["filters"] = _serialize_filters(stats_filters)
    else:
        pyme_id = getattr(tenant, "pyme_id", None)
        if pyme_id is None:
            pyme_id = getattr(current_user, "pyme_id", None) or getattr(current_user, "id", None)

        metadata["pyme_id"] = pyme_id

        resumen_pyme: dict[str, object] = {}
        cards_pyme: list[dict[str, object]] = []
        stats_pyme: dict[str, object] = {}

        if pyme_id:
            metricas = MetricasService(pyme_id)
            total_ventas = metricas.get_total_ingresos()
            total_pedidos = metricas.get_total_pedidos()
            clientes_unicos = metricas.get_new_customers()
            tasa_conversion = metricas.get_conversion_rate()

            resumen_pyme = {
                "total_ventas": total_ventas,
                "total_pedidos": total_pedidos,
                "clientes_unicos": clientes_unicos,
                "tasa_conversion": tasa_conversion,
            }

            cards_pyme = [
                {"label": "Ingresos Totales", "value": total_ventas},
                {"label": "Pedidos", "value": total_pedidos},
                {"label": "Clientes Únicos", "value": clientes_unicos},
                {"label": "Tasa de Conversión", "value": tasa_conversion},
            ]

            stats_pyme = {
                "kpis": metricas.get_kpis(),
                "ventas_over_time": metricas.get_sales_over_time(),
                "top_productos": metricas.get_top_products(),
                "ventas_por_region": metricas.get_sales_by_region(),
            }

        payload["stats"] = stats_pyme
        payload["summary"] = resumen_pyme
        payload["cards"] = cards_pyme
        payload["filters"] = {}

    return jsonify(payload)


@estadisticas_bp.route("/mapa_calor/datos", methods=["GET"])
@token_requerido
@admin_o_empleado_requerido
def mapa_calor_datos(current_user):
    """Devuelve los puntos para el mapa de calor en formato JSON."""
    args = request.args
    try:
        tipo_ticket = _resolve_heatmap_ticket_type(args)
    except ValueError as exc:
        return jsonify({"error": "bad_request", "detail": str(exc)}), 400

    try:
        tenant = _resolve_tenant_profile_or_error(args)
    except TenantResolutionError as exc:
        return jsonify({"error": "tenant_desconocido", "detail": str(exc)}), 404
    if not _stats_tenant_access_allowed(current_user, tenant):
        return jsonify({"error": "tenant_forbidden"}), 403
    tenant_id = getattr(tenant, "id", None)
    if tipo_ticket == "pyme" and tenant_id is None and not current_app.config.get("TESTING"):
        return jsonify({"error": "tenant_required"}), 403

    estados = _parse_estado_params(args)
    estado_param = None
    if estados:
        estado_param = estados if len(estados) > 1 else estados[0]

    distritos = _parse_multi_value_param(args, "distrito")
    distrito = distritos[0] if distritos else None

    municipio_id = args.get("municipio_id", type=int)
    if tipo_ticket == "municipio":
        municipio_id = getattr(tenant, "municipio_id", None) or _resolve_municipio_id(current_user)

    rubro_id = args.get("rubro_id", type=int)
    if tipo_ticket == "pyme":
        rubro_id = _resolve_stats_rubro_id(current_user, tenant)

    categorias = _parse_multi_value_param(args, "categoria")

    try:
        stats_filters = _build_stats_filters(args, estados)
        puntos = servicio_tickets.obtener_tickets_con_ubicacion_para_mapa(
            tipo_ticket=tipo_ticket,
            actor=current_user,
            municipio_id=municipio_id,
            rubro_id=rubro_id,
            tenant_id=tenant_id,
            fecha_inicio=args.get("fecha_inicio"),
            fecha_fin=args.get("fecha_fin"),
            categoria=categorias or None,
            distrito=distrito,
            estado=estado_param,
            satisfactorio=args.get(
                "satisfactorio", type=lambda v: str(v).lower() == "true"
            ),
        )
    except ValueError as exc:
        return jsonify({"error": "bad_request", "detail": str(exc)}), 400
    except Exception:
        current_app.logger.error(
            "[estadisticas] error interno",
            exc_info=True,
            extra={
                "path": request.path,
                "args": dict(request.args),
                "tenant": request.args.get("tenant"),
                "tenant_slug": request.args.get("tenant_slug"),
            },
        )
        return jsonify({"error": "server_error", "detail": "Error interno"}), 500

    payload: dict[str, object] = {"heatmap": puntos}
    puntos = _prepare_legacy_heatmap_payload(payload, puntos, current_user)

    if tipo_ticket == "municipio":
        if stats_filters:
            stats = build_stats_for_municipio(municipio_id, filters=stats_filters, actor=current_user)
        else:
            stats = build_stats_for_municipio(municipio_id, actor=current_user)

        resumen = dict(stats.get("resumen", {})) if isinstance(stats, dict) else {}
        payload["stats"] = stats
        payload["summary"] = resumen
        payload["cards"] = _build_summary_cards(resumen)
        payload["filters"] = _serialize_filters(stats_filters)

    return jsonify(payload)


@estadisticas_bp.route("/usuarios/ubicaciones", methods=["GET"])
@token_requerido
@admin_o_empleado_requerido
def get_user_locations(current_user):
    """Devuelve las ubicaciones (lat, lng) de usuarios del mismo municipio."""
    municipio_id = _resolve_municipio_id(current_user)
    if municipio_id is None:
        return jsonify([])

    users = (
        User.query.filter(
            User.municipio_id == municipio_id,
            User.latitud.isnot(None),
            User.longitud.isnot(None),
        ).all()
    )
    locations = [{"lat": u.latitud, "lng": u.longitud} for u in users]
    return jsonify(locations)


@estadisticas_bp.route("/tickets", methods=["OPTIONS"])
def tickets_options():
    """Preflight CORS para /estadisticas/tickets."""
    return "", 204


@estadisticas_bp.route("/tickets", methods=["GET"])
@token_requerido
@admin_o_empleado_requerido
def estadisticas_tickets(current_user):
    """Devuelve datos de tickets para gráficas y mapas de calor.

    Responde con un objeto JSON que contiene la clave `heatmap` con los
    puntos agregados para el mapa de calor. Si no se especifica el
    `municipio_id` o `rubro_id`, se utilizan los del `current_user`.
    """
    args = request.args
    tipo = args.get("tipo", "municipio")

    try:
        tenant = _resolve_tenant_profile_or_error(args)
    except TenantResolutionError as exc:
        return jsonify({"error": "tenant_desconocido", "detail": str(exc)}), 404
    if not _stats_tenant_access_allowed(current_user, tenant):
        return jsonify({"error": "tenant_forbidden"}), 403
    tenant_id = getattr(tenant, "id", None)
    if tipo == "pyme" and tenant_id is None and not current_app.config.get("TESTING"):
        return jsonify({"error": "tenant_required"}), 403

    municipio_id = args.get("municipio_id", type=int)
    rubro_id = args.get("rubro_id", type=int)

    estados = _parse_estado_params(args)
    estado_param = None
    if estados:
        estado_param = estados if len(estados) > 1 else estados[0]

    distrito = args.get("distrito", type=str)
    if distrito:
        distrito = distrito.strip() or None

    if tipo == "municipio":
        municipio_id = getattr(tenant, "municipio_id", None) or _resolve_municipio_id(current_user)
    if tipo == "pyme":
        rubro_id = _resolve_stats_rubro_id(current_user, tenant)

    categoria_values = _parse_multi_value_param(args, "categoria")
    categoria = categoria_values or None

    try:
        stats_filters = _build_stats_filters(args, estados)
        puntos = servicio_tickets.obtener_tickets_con_ubicacion_para_mapa(
            tipo_ticket=tipo,
            actor=current_user,
            municipio_id=municipio_id,
            rubro_id=rubro_id,
            tenant_id=tenant_id,
            fecha_inicio=args.get("fecha_inicio"),
            fecha_fin=args.get("fecha_fin"),
            categoria=categoria,
            distrito=distrito,
            estado=estado_param,
            satisfactorio=args.get(
                "satisfactorio", type=lambda v: str(v).lower() == "true"
            ),
        )
    except ValueError as exc:
        return jsonify({"error": "bad_request", "detail": str(exc)}), 400
    except Exception:
        current_app.logger.error(
            "[estadisticas] error interno",
            exc_info=True,
            extra={
                "path": request.path,
                "args": dict(request.args),
                "tenant": request.args.get("tenant"),
                "tenant_slug": request.args.get("tenant_slug"),
            },
        )
        return jsonify({"error": "server_error", "detail": "Error interno"}), 500

    heatmap = puntos or []

    respuesta: dict[str, object] = {"heatmap": heatmap}

    heatmap = _prepare_legacy_heatmap_payload(respuesta, heatmap, current_user)

    if tipo == "municipio":
        if stats_filters:
            stats = build_stats_for_municipio(municipio_id, filters=stats_filters, actor=current_user)
        else:
            stats = build_stats_for_municipio(municipio_id, actor=current_user)

        resumen = dict(stats.get("resumen", {})) if isinstance(stats, dict) else {}
        respuesta["stats"] = stats
        respuesta["summary"] = resumen
        respuesta["cards"] = _build_summary_cards(resumen)
        respuesta["filters"] = _serialize_filters(stats_filters)

    return jsonify(respuesta)
