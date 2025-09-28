"""SQLAlchemy models for analytics aggregates."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.sqlite import JSON as SQLITE_JSON

from extensions import db
from utils.time_utils import get_local_now

JSONType = JSONB().with_variant(SQLITE_JSON, "sqlite")


class AnalyticsDailyMetric(db.Model):
    __tablename__ = "analytics_daily_metric"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "scope", "metric_date", "metric", "dimension",
            name="uq_analytics_daily_metric",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    scope = db.Column(db.String(32), nullable=False, index=True)
    metric_date = db.Column(db.Date, nullable=False, index=True)
    metric = db.Column(db.String(64), nullable=False, index=True)
    dimension = db.Column(db.String(128), nullable=True, index=True)
    value = db.Column(db.Float, nullable=False)
    extra = db.Column(JSONType, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=get_local_now, nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True), default=get_local_now, onupdate=get_local_now, nullable=False
    )


class AnalyticsGeoCell(db.Model):
    __tablename__ = "analytics_geo_cell"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "scope", "metric_date", "cell_id", name="uq_analytics_geo_cell"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    scope = db.Column(db.String(32), nullable=False, index=True)
    metric_date = db.Column(db.Date, nullable=False, index=True)
    cell_id = db.Column(db.String(32), nullable=False)
    count = db.Column(db.Integer, nullable=False, default=0)
    severity_avg = db.Column(db.Float, nullable=True)
    categories = db.Column(JSONType, nullable=True)
    centroid = db.Column(JSONType, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=get_local_now, nullable=False)


class AnalyticsTopMetric(db.Model):
    __tablename__ = "analytics_top_metric"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "scope", "metric_date", "category", "label",
            name="uq_analytics_top_metric",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    scope = db.Column(db.String(32), nullable=False, index=True)
    metric_date = db.Column(db.Date, nullable=False, index=True)
    category = db.Column(db.String(64), nullable=False)
    label = db.Column(db.String(128), nullable=False)
    value = db.Column(db.Float, nullable=False)
    delta = db.Column(db.Float, nullable=True)
    details = db.Column(JSONType, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=get_local_now, nullable=False)


class AnalyticsCohortMetric(db.Model):
    __tablename__ = "analytics_cohort_metric"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "cohort_key", name="uq_analytics_cohort_metric"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    cohort_key = db.Column(db.String(64), nullable=False)
    size = db.Column(db.Integer, nullable=False)
    retention_30 = db.Column(db.Float, nullable=True)
    retention_60 = db.Column(db.Float, nullable=True)
    retention_90 = db.Column(db.Float, nullable=True)
    extra = db.Column(JSONType, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=get_local_now, nullable=False)


class AnalyticsWhatsappTemplate(db.Model):
    __tablename__ = "analytics_whatsapp_template"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "template_name", "metric_date",
            name="uq_analytics_whatsapp_template",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.String(64), nullable=False, index=True)
    template_name = db.Column(db.String(128), nullable=False)
    metric_date = db.Column(db.Date, nullable=False, index=True)
    sent = db.Column(db.Integer, nullable=False, default=0)
    delivered = db.Column(db.Integer, nullable=False, default=0)
    read = db.Column(db.Integer, nullable=False, default=0)
    responded = db.Column(db.Integer, nullable=False, default=0)
    blocked = db.Column(db.Integer, nullable=False, default=0)
    extra = db.Column(JSONType, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=get_local_now, nullable=False)


class AnalyticsModuleStatus(db.Model):
    __tablename__ = "analytics_module_status"
    id = db.Column(db.Integer, primary_key=True)
    snapshot_at = db.Column(db.DateTime(timezone=True), default=get_local_now, nullable=False)
    cache_hits = db.Column(db.Integer, nullable=False, default=0)
    cache_misses = db.Column(db.Integer, nullable=False, default=0)
    cache_evictions = db.Column(db.Integer, nullable=False, default=0)
    jobs_pending = db.Column(db.Integer, nullable=False, default=0)
    jobs_running = db.Column(db.Integer, nullable=False, default=0)
    jobs_failed = db.Column(db.Integer, nullable=False, default=0)
    extra = db.Column(JSONType, nullable=True)


def touch_module_status(**kwargs) -> None:
    """Update or insert a module status snapshot with the provided counters."""

    status = AnalyticsModuleStatus(
        cache_hits=kwargs.get("cache_hits", 0),
        cache_misses=kwargs.get("cache_misses", 0),
        cache_evictions=kwargs.get("cache_evictions", 0),
        jobs_pending=kwargs.get("jobs_pending", 0),
        jobs_running=kwargs.get("jobs_running", 0),
        jobs_failed=kwargs.get("jobs_failed", 0),
        extra=kwargs.get("metadata") or kwargs.get("extra"),
    )
    db.session.add(status)
    db.session.commit()
