"""Analytics module integrating CRM dashboards and KPI services."""

from .service import (
    get_summary,
    get_timeseries,
    get_breakdown,
    get_geo_heatmap,
    get_geo_points,
    get_top,
    get_operations_overview,
    get_cohorts,
    get_whatsapp_templates,
)
from .jobs import rebuild_analytics_snapshot

__all__ = [
    "get_summary",
    "get_timeseries",
    "get_breakdown",
    "get_geo_heatmap",
    "get_geo_points",
    "get_top",
    "get_operations_overview",
    "get_cohorts",
    "get_whatsapp_templates",
    "rebuild_analytics_snapshot",
]
