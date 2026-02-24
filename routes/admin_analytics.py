"""Admin analytics endpoints with tenant-scoped access and exports."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import time
from datetime import datetime
from typing import Any

from flask import Blueprint, Response, abort, jsonify, request
from sqlalchemy import func

from models import AnalyticsEventV2
from services.analytics import get_geo_heatmap, get_summary
from services.analytics.filters import parse_filters
from services.analytics.rbac import require_access

admin_analytics_bp = Blueprint("admin_analytics", __name__, url_prefix="/admin/analytics")

_DASHBOARD_CACHE: dict[str, dict[str, Any]] = {}
_DASHBOARD_CACHE_TTL_SECONDS = 20.0


def _json(payload: dict, status: int = 200):
    response = jsonify(payload)
    response.status_code = status
    return response


def _tenant_id_as_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        abort(400, description="tenant_id must be numeric")


def _ensure_total_interactions(payload: dict[str, Any]) -> dict[str, Any]:
    totals = payload.setdefault("totals", {})
    if "total_interactions" not in totals:
        tickets = int(totals.get("tickets") or 0)
        pedidos = int(totals.get("pedidos") or 0)
        encuestas = int(totals.get("encuestas") or 0)
        totals["total_interactions"] = tickets + pedidos + encuestas
    return payload


def _build_dashboard_payload(filters) -> dict[str, Any]:
    overview = _ensure_total_interactions(get_summary(filters))
    totals = overview.get("totals") or {}
    geo = get_geo_heatmap(filters)

    return {
        "tenant_id": filters.tenant_id,
        "scope": filters.scope,
        "period": {
            "from": filters.date_from.isoformat() if filters.date_from else None,
            "to": filters.date_to.isoformat() if filters.date_to else None,
        },
        "sections": {
            "general": overview,
            "municipio": overview if filters.scope == "municipio" else {"totals": totals},
            "ventas": overview if filters.scope == "pyme" else {"totals": totals},
            "mapas": {"geo": geo},
        },
        "navigation": {
            "primary": [
                {"key": "analytics", "label": "Analytics & Insights", "path": "/analytics", "active": True},
                {"key": "estadisticas", "label": "Estadísticas", "path": "/estadisticas", "active": False},
                {"key": "encuestas", "label": "Encuestas", "path": "/admin/encuestas", "active": False},
            ],
            "encuestas": {
                "admin_list_endpoint": "/api/admin/encuestas",
                "templates_endpoint": "/api/admin/encuestas/templates",
                "seed_demo_endpoint_template": "/api/admin/encuestas/{encuesta_id}/seed-demo",
                "public_results_endpoint_template": "/api/public/encuestas/{slug}/live-results",
            },
        },
    }


def _dashboard_cache_key(filters) -> str:
    return "|".join(
        [
            str(filters.tenant_id),
            str(filters.scope),
            str(filters.date_from.isoformat() if filters.date_from else ""),
            str(filters.date_to.isoformat() if filters.date_to else ""),
            ",".join(filters.canales),
            ",".join(filters.categorias),
            ",".join(filters.estados),
            str(filters.bbox or ""),
            str(filters.resolution),
        ]
    )


def _etag_for_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _dashboard_response(filters):
    cache_key = _dashboard_cache_key(filters)
    now = time.time()
    entry = _DASHBOARD_CACHE.get(cache_key)
    if entry and float(entry.get("expires_at") or 0.0) > now:
        payload = entry["payload"]
    else:
        payload = _build_dashboard_payload(filters)
        _DASHBOARD_CACHE[cache_key] = {
            "payload": payload,
            "expires_at": now + _DASHBOARD_CACHE_TTL_SECONDS,
        }

    etag = _etag_for_payload(payload)
    if request.if_none_match and request.if_none_match.contains(etag):
        return Response(status=304, headers={"ETag": etag, "Cache-Control": "private, max-age=20"})

    response = _json(payload)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, max-age=20"
    return response


@admin_analytics_bp.get("/overview")
def admin_analytics_overview():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador")
    payload = _ensure_total_interactions(get_summary(filters))
    return _json(payload)


@admin_analytics_bp.get("/heatmap")
def admin_analytics_heatmap():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador")
    tz = request.args.get("tz") or "UTC"
    base = get_geo_heatmap(filters)

    tenant_id = _tenant_id_as_int(filters.tenant_id)
    query = AnalyticsEventV2.query.filter(AnalyticsEventV2.tenant_id == tenant_id)
    if filters.date_from:
        query = query.filter(AnalyticsEventV2.ts >= filters.date_from)
    if filters.date_to:
        query = query.filter(AnalyticsEventV2.ts <= filters.date_to)

    temporal_rows = (
        query.with_entities(
            func.strftime("%w", AnalyticsEventV2.ts).label("weekday"),
            func.strftime("%H", AnalyticsEventV2.ts).label("hour"),
            func.count(AnalyticsEventV2.id).label("total"),
        )
        .group_by("weekday", "hour")
        .all()
    )

    temporal = [
        {"weekday": int(row.weekday), "hour": int(row.hour), "count": int(row.total)}
        for row in temporal_rows
        if row.weekday is not None and row.hour is not None
    ]

    return _json({"geo": base, "temporal": temporal, "tz": tz})


@admin_analytics_bp.get("/export.csv")
def admin_analytics_export_csv():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador")
    overview = get_summary(filters)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["metric", "value"])
    for key, value in overview.get("totals", {}).items():
        writer.writerow([key, value])

    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=analytics_{filters.tenant_id}.csv"
        },
    )


@admin_analytics_bp.get("/export.pdf")
def admin_analytics_export_pdf():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador")
    overview = get_summary(filters)

    lines = [
        "Reporte de analytics",
        f"Tenant: {filters.tenant_id}",
        f"Emitido: {datetime.utcnow().isoformat()}Z",
    ]
    for key, value in overview.get("totals", {}).items():
        lines.append(f"{key}: {value}")

    text = "\\n".join(lines).replace("(", "[").replace(")", "]")
    stream = f"BT /F1 12 Tf 50 780 Td ({text}) Tj ET".encode("latin-1", errors="replace")
    body = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    body += b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    body += b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
    body += b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    body += f"5 0 obj<</Length {len(stream)}>>stream\n".encode("ascii") + stream + b"\nendstream endobj\n"
    body += b"xref\n0 6\n0000000000 65535 f \n0000000010 00000 n \n0000000060 00000 n \n0000000118 00000 n \n0000000244 00000 n \n0000000314 00000 n \n"
    body += b"trailer<</Root 1 0 R/Size 6>>\nstartxref\n420\n%%EOF"

    return Response(
        body,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=analytics_{filters.tenant_id}.pdf"
        },
    )


@admin_analytics_bp.get("/dashboard")
def admin_analytics_dashboard():
    """Unified payload for the /analytics UI tabs (general/municipio/ventas/mapas)."""

    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador")
    return _dashboard_response(filters)


@admin_analytics_bp.get("/hub")
def admin_analytics_hub():
    """Alias endpoint to support frontend convergence on one analytics hub route."""

    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador")
    return _dashboard_response(filters)
