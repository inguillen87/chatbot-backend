"""Nightly analytics aggregation jobs."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from flask import current_app

from extensions import db

from .filters import AnalyticsFilters
from .models import (
    AnalyticsCohortMetric,
    AnalyticsDailyMetric,
    AnalyticsGeoCell,
    AnalyticsModuleStatus,
    AnalyticsTopMetric,
    AnalyticsWhatsappTemplate,
)
from .service import (
    get_cohorts,
    get_geo_heatmap,
    get_summary,
    get_top,
    get_whatsapp_templates,
)


def _date_range(start: datetime, end: datetime):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def rebuild_analytics_snapshot(
    tenant_id: str,
    scope: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> None:
    """Recompute aggregated analytics tables for the given tenant and scope."""

    if start_date is None:
        start_date = datetime.utcnow() - timedelta(days=7)
    if end_date is None:
        end_date = datetime.utcnow()

    filters = AnalyticsFilters(
        tenant_id=tenant_id,
        scope=scope,
        date_from=None,
        date_to=None,
        canales=(),
        categorias=(),
        estados=(),
        agentes=(),
        zonas=(),
        etiquetas=(),
        rubros=(),
        bbox=None,
        pyme_ids=(),
        resolution=8,
    )

    for day in _date_range(start_date, end_date):
        day_start = datetime(day.year, day.month, day.day)
        day_end = day_start + timedelta(hours=23, minutes=59, seconds=59)
        day_filters = AnalyticsFilters(
            tenant_id=tenant_id,
            scope=scope,
            date_from=day_start,
            date_to=day_end,
            canales=filters.canales,
            categorias=filters.categorias,
            estados=filters.estados,
            agentes=filters.agentes,
            zonas=filters.zonas,
            etiquetas=filters.etiquetas,
            rubros=filters.rubros,
            bbox=None,
            pyme_ids=filters.pyme_ids,
            resolution=filters.resolution,
        )

        summary = get_summary(day_filters)
        db.session.query(AnalyticsDailyMetric).filter(
            AnalyticsDailyMetric.tenant_id == tenant_id,
            AnalyticsDailyMetric.scope == scope,
            AnalyticsDailyMetric.metric_date == day_start.date(),
        ).delete(synchronize_session=False)

        kpi_map = {
            "tickets_total": summary["totals"].get("tickets", 0),
            "tickets_abiertos": summary["totals"].get("tickets_abiertos", 0),
            "backlog": summary["totals"].get("backlog", 0),
            "automatizado_pct": summary["totals"].get("automatizado_pct", 0.0),
            "primer_contacto_pct": summary["totals"].get("primer_contacto_pct", 0.0),
            "reaperturas": summary["totals"].get("reaperturas", 0),
            "nps": summary["totals"].get("nps"),
            "csat": summary["totals"].get("csat"),
        }
        for metric_key, value in kpi_map.items():
            db.session.add(
                AnalyticsDailyMetric(
                    tenant_id=tenant_id,
                    scope=scope,
                    metric_date=day_start.date(),
                    metric=metric_key,
                    dimension=None,
                    value=float(value or 0),
                )
            )

        for sla_key, values in summary.get("sla", {}).items():
            for percentile, value in values.items():
                metric_key = f"{sla_key}_{percentile}"
                db.session.add(
                    AnalyticsDailyMetric(
                        tenant_id=tenant_id,
                        scope=scope,
                        metric_date=day_start.date(),
                        metric=metric_key,
                        dimension=None,
                        value=float(value or 0),
                    )
                )

        heatmap = get_geo_heatmap(day_filters)
        db.session.query(AnalyticsGeoCell).filter(
            AnalyticsGeoCell.tenant_id == tenant_id,
            AnalyticsGeoCell.scope == scope,
            AnalyticsGeoCell.metric_date == day_start.date(),
        ).delete(synchronize_session=False)
        for cell in heatmap.get("cells", []):
            db.session.add(
                AnalyticsGeoCell(
                    tenant_id=tenant_id,
                    scope=scope,
                    metric_date=day_start.date(),
                    cell_id=cell.get("cell_id"),
                    count=cell.get("count", 0),
                    categories=cell.get("categories"),
                    centroid={
                        "centroid_lat": cell.get("centroid_lat"),
                        "centroid_lon": cell.get("centroid_lon"),
                    },
                )
            )

        for category in ("barrios", "calles", "productos"):
            top = get_top(day_filters, category=category)
            db.session.query(AnalyticsTopMetric).filter(
                AnalyticsTopMetric.tenant_id == tenant_id,
                AnalyticsTopMetric.scope == scope,
                AnalyticsTopMetric.metric_date == day_start.date(),
                AnalyticsTopMetric.category == category,
            ).delete(synchronize_session=False)
            for item in top.get("items", [])[:20]:
                db.session.add(
                AnalyticsTopMetric(
                    tenant_id=tenant_id,
                    scope=scope,
                    metric_date=day_start.date(),
                    category=category,
                    label=item.get("label"),
                    value=float(item.get("value", 0)),
                    delta=item.get("delta"),
                    details=item.get("metadata"),
                )
            )

        if scope == "pyme":
            cohorts = get_cohorts(day_filters)
            db.session.query(AnalyticsCohortMetric).filter(
                AnalyticsCohortMetric.tenant_id == tenant_id
            ).delete(synchronize_session=False)
            for cohort in cohorts.get("cohorts", []):
                db.session.add(
                    AnalyticsCohortMetric(
                        tenant_id=tenant_id,
                        cohort_key=cohort.get("cohort"),
                        size=int(cohort.get("size", 0)),
                        retention_30=cohort.get("retention30"),
                        retention_60=cohort.get("retention60"),
                        retention_90=cohort.get("retention90"),
                    )
                )

        templates = get_whatsapp_templates(day_filters)
        db.session.query(AnalyticsWhatsappTemplate).filter(
            AnalyticsWhatsappTemplate.tenant_id == tenant_id,
            AnalyticsWhatsappTemplate.metric_date == day_start.date(),
        ).delete(synchronize_session=False)
        for tpl in templates.get("templates", []):
            db.session.add(
                AnalyticsWhatsappTemplate(
                    tenant_id=tenant_id,
                    template_name=tpl.get("template"),
                    metric_date=day_start.date(),
                    sent=int(tpl.get("sent", 0)),
                    delivered=int(tpl.get("delivered", 0)),
                    read=int(tpl.get("read", 0)),
                    responded=int(tpl.get("responded", 0)),
                    blocked=int(tpl.get("blocked", 0)),
                    extra=tpl.get("metadata"),
                )
            )

    db.session.commit()

    db.session.add(
        AnalyticsModuleStatus(
            cache_hits=0,
            cache_misses=0,
            cache_evictions=0,
            jobs_pending=0,
            jobs_running=0,
            jobs_failed=0,
            extra={"tenant_id": tenant_id, "scope": scope},
        )
    )
    db.session.commit()
