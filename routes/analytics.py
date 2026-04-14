"""Analytics blueprint exposing CRM dashboards and data endpoints."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any
import time
import uuid

from flask import Blueprint, abort, current_app, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from extensions import db

from services.analytics.cache import analytics_cache
from services.analytics.config import get_config
from services.analytics.filters import AnalyticsFilters, parse_filters
from services.analytics import (
    get_breakdown,
    get_cohorts,
    get_geo_heatmap,
    get_geo_points,
    get_operations_overview,
    get_summary,
    get_timeseries,
    get_top,
    get_whatsapp_templates,
)
from services.analytics.ingestor import analytics_ingestor
from services.analytics.models import AnalyticsModuleStatus
from models import AnalyticsEventV2, TenantProfile
from services.analytics.rbac import require_access

analytics_bp = Blueprint("analytics", __name__, url_prefix="/analytics")

ANALYTICS_IDENTITY_COVERAGE_CONTRACT_VERSION = "analytics.identity_coverage.v1"
ANALYTICS_EVENT_INGEST_CONTRACT_VERSION = "analytics.event_ingest.v1"
ANALYTICS_EVENT_SCHEMA_CONTRACT_VERSION = "analytics.event_schema.v1"

ANALYTICS_CANONICAL_EVENT_NAMES = [
    "message_received",
    "ticket_created",
    "ticket_assigned",
    "ticket_resolved",
    "survey_answer_submitted",
    "vote_submitted",
    "product_viewed",
    "cart_started",
    "order_created",
    "location_shared",
    "widget_session_opened",
    "portal_session_opened",
]


@analytics_bp.before_request
def _ensure_feature_enabled() -> None:
    config = get_config()
    if not config.feature_enabled:
        abort(404)






def _tenant_id_from_filters(filters: AnalyticsFilters) -> int:
    try:
        return int(filters.tenant_id)
    except (TypeError, ValueError):
        return 0

def _resolve_tenant_id_from_event_payload(payload: dict) -> int | None:
    """Resolve tenant id from JSON/body/query hints used by frontend trackers."""

    tenant_candidates = [
        payload.get("tenant_id"),
        payload.get("tenant"),
        request.args.get("tenant_id"),
        request.args.get("tenant"),
        request.args.get("tenant_slug"),
    ]
    for candidate in tenant_candidates:
        if candidate is None:
            continue
        raw = str(candidate).strip()
        if not raw:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            tenant = (
                db.session.query(TenantProfile)
                .filter(TenantProfile.slug == raw)
                .first()
            )
            if tenant is not None:
                owner_tenant_id = tenant.municipio_id or tenant.pyme_id
                if owner_tenant_id is not None:
                    return int(owner_tenant_id)
                return int(tenant.id)
    return None




def _current_contact_identity() -> dict:
    identity = getattr(g, "contact_identity", None)
    if isinstance(identity, dict):
        return identity
    return {}


def _build_event_payload_with_identity(payload: dict) -> dict:
    event_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    enriched_payload = dict(event_payload)

    identity = _current_contact_identity()
    if identity:
        enriched_payload.setdefault("contact_key", identity.get("contact_key"))
        enriched_payload.setdefault("conversation_id", identity.get("conversation_id"))
        enriched_payload.setdefault("phone_e164", identity.get("phone_e164"))
        enriched_payload.setdefault("identity_source", identity.get("source"))

    # Also honor direct API payload hints when provided by trusted callers.
    if payload.get("contact_key"):
        enriched_payload["contact_key"] = payload.get("contact_key")
    if payload.get("conversation_id"):
        enriched_payload["conversation_id"] = payload.get("conversation_id")

    return {k: v for k, v in enriched_payload.items() if v is not None}

def _resolve_event_name(payload: dict) -> str:
    for key in ("event_name", "event", "name", "type"):
        value = payload.get(key)
        if value is None:
            value = request.args.get(key)
        text = str(value or "").strip()
        if text:
            return text
    # Keep ingest resilient for frontend telemetry beacons that omit the event
    # name while still sending tenant context.
    return "frontend_analytics_event"

def _json_response(payload, status: int = 200):
    response = jsonify(payload)
    response.status_code = status
    return response


def _event_has_contact_identity(metadata: dict[str, Any] | None, session_id: str | None, anon_id: str | None) -> bool:
    if isinstance(metadata, dict):
        if metadata.get("contact_key") or metadata.get("conversation_id"):
            return True

    return bool(session_id or anon_id)






def _parse_channel_targets(raw_value: Any, default_target: float) -> dict[str, float]:
    if raw_value is None:
        return {}

    parsed: dict[str, Any]
    if isinstance(raw_value, dict):
        parsed = raw_value
    else:
        text = str(raw_value).strip()
        if not text:
            return {}
        try:
            loaded = json.loads(text)
            parsed = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            parsed = {}
            for chunk in text.split(","):
                if ":" not in chunk:
                    continue
                channel, value = chunk.split(":", 1)
                parsed[channel.strip()] = value.strip()

    targets: dict[str, float] = {}
    for channel, value in parsed.items():
        channel_key = str(channel or "").strip().lower()
        if not channel_key:
            continue
        try:
            targets[channel_key] = float(value)
        except (TypeError, ValueError):
            targets[channel_key] = default_target
    return targets

def _coverage_slo_status(coverage_pct: float, target_pct: float) -> str:
    try:
        target = float(target_pct)
    except (TypeError, ValueError):
        target = 90.0
    return "ok" if float(coverage_pct) >= target else "below_target"



def _build_identity_alerts(
    channels: dict[str, Any],
    target_pct: float,
    *,
    channel_targets: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    channel_targets = channel_targets or {}

    for channel, stats in (channels or {}).items():
        normalized_channel = str(channel or "").strip().lower() or "unknown"
        channel_target = float(channel_targets.get(normalized_channel, target_pct))
        try:
            coverage = float(stats.get("coverage_pct", 0.0))
        except (TypeError, ValueError):
            coverage = 0.0
        if coverage >= channel_target:
            continue

        alerts.append(
            {
                "type": "identity_coverage_below_target",
                "channel": normalized_channel,
                "coverage_pct": coverage,
                "target_pct": channel_target,
                "gap_pct": round(channel_target - coverage, 2),
                "recommended_action": "increase_contact_key_propagation",
            }
        )
    return alerts



def _build_identity_alert_event_payloads(
    *,
    tenant_id: int,
    alerts: list[dict[str, Any]],
    target_pct: float,
    overall_coverage_pct: float,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for alert in alerts:
        payloads.append(
            {
                "tenant_id": tenant_id,
                "event_name": "identity_coverage_alert",
                "channel": alert.get("channel"),
                "payload": {
                    "type": alert.get("type"),
                    "coverage_pct": alert.get("coverage_pct"),
                    "target_pct": alert.get("target_pct", target_pct),
                    "gap_pct": alert.get("gap_pct"),
                    "recommended_action": alert.get("recommended_action"),
                    "overall_coverage_pct": overall_coverage_pct,
                },
            }
        )
    return payloads

def _compute_identity_coverage(events: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(events)
    if total == 0:
        return {
            "total_events": 0,
            "events_with_identity": 0,
            "coverage_pct": 0.0,
            "channels": {},
        }

    events_with_identity = 0
    per_channel: dict[str, dict[str, int]] = {}

    for event in events:
        channel = str(event.get("channel") or "unknown").strip().lower() or "unknown"
        channel_stats = per_channel.setdefault(channel, {"total": 0, "with_identity": 0})
        channel_stats["total"] += 1

        has_identity = _event_has_contact_identity(
            event.get("metadata") if isinstance(event.get("metadata"), dict) else None,
            event.get("session_id"),
            event.get("anon_id"),
        )
        if has_identity:
            events_with_identity += 1
            channel_stats["with_identity"] += 1

    channels_payload = {}
    for channel, stats in per_channel.items():
        channel_total = stats.get("total", 0)
        channel_with_identity = stats.get("with_identity", 0)
        channels_payload[channel] = {
            "total": channel_total,
            "with_identity": channel_with_identity,
            "coverage_pct": round((channel_with_identity / channel_total) * 100.0, 2) if channel_total else 0.0,
        }

    return {
        "total_events": total,
        "events_with_identity": events_with_identity,
        "coverage_pct": round((events_with_identity / total) * 100.0, 2),
        "channels": channels_payload,
    }


@analytics_bp.route("/summary", methods=["GET"])
def analytics_summary():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "visor")
    current_app.logger.info("[analytics] summary %s", filters)
    data = get_summary(filters)
    return _json_response(data)


@analytics_bp.route("/timeseries", methods=["GET"])
def analytics_timeseries():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "visor")
    metric = request.args.get("metric", "tickets")
    group = request.args.get("group")
    data = get_timeseries(filters, metric=metric, group=group)
    return _json_response(data)


@analytics_bp.route("/breakdown", methods=["GET"])
def analytics_breakdown():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "visor")
    dimension = request.args.get("dimension", "categoria")
    data = get_breakdown(filters, dimension=dimension)
    return _json_response(data)


@analytics_bp.route("/geo/heatmap", methods=["GET"])
def analytics_heatmap():
    request_started = time.perf_counter()
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "visor")
    data = get_geo_heatmap(filters)
    response = _json_response(data)
    elapsed_ms = round((time.perf_counter() - request_started) * 1000.0, 2)
    response.headers.setdefault("X-Request-Id", request_id)
    response.headers.setdefault("Server-Timing", f"analytics_geo_heatmap;dur={elapsed_ms}")
    current_app.logger.info(
        "[analytics.geo.heatmap] request_id=%s tenant_id=%s scope=%s cells=%s state=%s total_ms=%s",
        request_id,
        filters.tenant_id,
        filters.scope,
        len(data.get("cells") or []),
        (data.get("render_contract") or {}).get("state"),
        elapsed_ms,
    )
    return response


@analytics_bp.route("/geo/points", methods=["GET"])
def analytics_points():
    request_started = time.perf_counter()
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "visor")
    limit = int(request.args.get("limit", 500))
    data = get_geo_points(filters, limit=limit)
    response = _json_response(data)
    elapsed_ms = round((time.perf_counter() - request_started) * 1000.0, 2)
    response.headers.setdefault("X-Request-Id", request_id)
    response.headers.setdefault("Server-Timing", f"analytics_geo_points;dur={elapsed_ms}")
    current_app.logger.info(
        "[analytics.geo.points] request_id=%s tenant_id=%s scope=%s points=%s state=%s total_ms=%s",
        request_id,
        filters.tenant_id,
        filters.scope,
        len(data.get("points") or []),
        (data.get("render_contract") or {}).get("state"),
        elapsed_ms,
    )
    return response


@analytics_bp.route("/top", methods=["GET"])
def analytics_top():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "visor")
    category = request.args.get("category", "barrios")
    limit = int(request.args.get("limit", 10))
    data = get_top(filters, category=category, limit=limit)
    return _json_response(data)


@analytics_bp.route("/operations", methods=["GET"])
def analytics_operations():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador")
    data = get_operations_overview(filters)
    return _json_response(data)


@analytics_bp.route("/cohorts", methods=["GET"])
def analytics_cohorts():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "visor")
    data = get_cohorts(filters)
    return _json_response(data)


@analytics_bp.route("/whatsapp/templates", methods=["GET"])
def analytics_templates():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador")
    data = get_whatsapp_templates(filters)
    return _json_response(data)




@analytics_bp.route("/identity/coverage", methods=["GET"])
def analytics_identity_coverage():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador")

    limit = int(request.args.get("limit", 5000))
    if limit < 1:
        limit = 1
    if limit > 20000:
        limit = 20000

    query = AnalyticsEventV2.query.filter(AnalyticsEventV2.tenant_id == filters.tenant_id)
    if filters.date_from:
        query = query.filter(AnalyticsEventV2.ts >= filters.date_from)
    if filters.date_to:
        query = query.filter(AnalyticsEventV2.ts <= filters.date_to)

    rows = (
        query.with_entities(
            AnalyticsEventV2.channel.label("channel"),
            AnalyticsEventV2.metadata_payload.label("metadata"),
            AnalyticsEventV2.session_id.label("session_id"),
            AnalyticsEventV2.anon_id.label("anon_id"),
        )
        .order_by(AnalyticsEventV2.ts.desc())
        .limit(limit)
        .all()
    )

    events = [
        {
            "channel": row.channel,
            "metadata": row.metadata if isinstance(row.metadata, dict) else {},
            "session_id": row.session_id,
            "anon_id": row.anon_id,
        }
        for row in rows
    ]

    coverage = _compute_identity_coverage(events)

    target_pct_raw = request.args.get("target_pct", 90)
    try:
        target_pct = float(target_pct_raw)
    except (TypeError, ValueError):
        target_pct = 90.0

    channel_targets = _parse_channel_targets(request.args.get("target_by_channel"), target_pct)
    slo_status = _coverage_slo_status(coverage.get("coverage_pct", 0.0), target_pct)
    alerts = _build_identity_alerts(
        coverage.get("channels") if isinstance(coverage, dict) else {},
        target_pct,
        channel_targets=channel_targets,
    )

    emit_alert_events = str(request.args.get("emit_alert_events", "0")).strip().lower() in {"1", "true", "yes"}
    alert_event_count = 0
    if emit_alert_events and alerts:
        event_payloads = _build_identity_alert_event_payloads(
            tenant_id=_tenant_id_from_filters(filters),
            alerts=alerts,
            target_pct=target_pct,
            overall_coverage_pct=float(coverage.get("coverage_pct", 0.0)),
        )
        for event in event_payloads:
            analytics_ingestor.track(
                tenant_id=event["tenant_id"],
                event_name=event["event_name"],
                payload=event["payload"],
                channel=event.get("channel") or "system",
                session_id="identity_coverage_monitor",
                tenant_type=filters.scope,
            )
        alert_event_count = len(event_payloads)

    coverage.update(
        {
            "tenant_id": filters.tenant_id,
            "sample_size": len(events),
            "limit": limit,
            "date_from": filters.date_from.isoformat() if filters.date_from else None,
            "date_to": filters.date_to.isoformat() if filters.date_to else None,
            "target_pct": target_pct,
            "target_by_channel": channel_targets,
            "slo_status": slo_status,
            "alerts": alerts,
            "alert_count": len(alerts),
            "emit_alert_events": emit_alert_events,
            "alert_events_emitted": alert_event_count,
            "contract_version": ANALYTICS_IDENTITY_COVERAGE_CONTRACT_VERSION,
        }
    )
    return _json_response(coverage)

@analytics_bp.route("/health", methods=["GET"])
def analytics_health():
    latest = (
        db.session.query(AnalyticsModuleStatus)
        .order_by(AnalyticsModuleStatus.snapshot_at.desc())
        .first()
    )
    payload = {
        "cache": {
            "hits": analytics_cache.stats.hits,
            "misses": analytics_cache.stats.misses,
            "evictions": analytics_cache.stats.evictions,
        },
        "jobs": {
            "pending": latest.jobs_pending if latest else 0,
            "running": latest.jobs_running if latest else 0,
            "failed": latest.jobs_failed if latest else 0,
        },
        "last_snapshot": latest.snapshot_at.isoformat() if latest else None,
        "metadata": latest.metadata if latest else {},
    }
    return _json_response(payload)


@analytics_bp.route("/event", methods=["POST"])
def analytics_event_ingest():
    payload = request.get_json(silent=True) or {}
    tenant_id = _resolve_tenant_id_from_event_payload(payload)
    if tenant_id is None:
        current_app.logger.info("[analytics] ignored event without tenant context")
        return _json_response({"ok": True, "ignored": True, "reason": "tenant_unresolved"}, status=202)

    event_name = _resolve_event_name(payload)

    try:
        require_access(str(tenant_id), "operador")
    except HTTPException as exc:
        if exc.code not in {401, 403}:
            raise
        current_app.logger.info(
            "[analytics] ignored event due to access guard tenant_id=%s status=%s",
            tenant_id,
            exc.code,
        )
        return _json_response({"ok": True, "ignored": True, "reason": "access_denied"}, status=202)

    identity = _current_contact_identity()
    payload_with_identity = _build_event_payload_with_identity(payload)

    analytics_ingestor.track(
        tenant_id=tenant_id,
        event_name=event_name,
        payload=payload_with_identity,
        user_id=payload.get("user_id"),
        anon_id=payload.get("anon_id") or identity.get("anon_id"),
        channel=payload.get("channel") or request.args.get("channel"),
        session_id=(
            payload.get("session_id")
            or request.args.get("session_id")
            or identity.get("conversation_id")
            or identity.get("contact_key")
        ),
        lat=payload.get("lat") if payload.get("lat") is not None else request.args.get("lat"),
        lng=payload.get("lng") if payload.get("lng") is not None else request.args.get("lng"),
        entity_ref=payload.get("entity_ref") or request.args.get("entity_ref"),
        tenant_type=payload.get("tenant_type") or request.args.get("tenant_type"),
    )
    return _json_response(
        {
            "ok": True,
            "contract_version": ANALYTICS_EVENT_INGEST_CONTRACT_VERSION,
            "tenant_id": tenant_id,
            "event_name": event_name,
            "contact_key": payload_with_identity.get("contact_key"),
            "conversation_id": payload_with_identity.get("conversation_id"),
            "identity_source": payload_with_identity.get("identity_source"),
        },
        status=202,
    )


@analytics_bp.route("/event/schema", methods=["GET"])
def analytics_event_schema():
    tenant_id_raw = request.args.get("tenant_id")
    try:
        tenant_id = int(str(tenant_id_raw or "").strip())
    except (TypeError, ValueError):
        return _json_response({"error": "tenant_id requerido y numérico"}, status=400)

    require_access(str(tenant_id), "operador")
    return _json_response(
        {
            "contract_version": ANALYTICS_EVENT_SCHEMA_CONTRACT_VERSION,
            "tenant_id": tenant_id,
            "required_dimensions": [
                "event_name",
                "channel",
                "tenant_id",
            ],
            "recommended_dimensions": [
                "contact_key",
                "conversation_id",
                "screen_name",
                "category",
                "lat",
                "lng",
            ],
            "canonical_events": ANALYTICS_CANONICAL_EVENT_NAMES,
        }
    )


@analytics_bp.route("/ui", methods=["GET"])
def analytics_ui():
    tenant_id = request.args.get("tenant_id")
    scope = request.args.get("scope", "municipio")
    return render_template("analytics/dashboard.html", tenant_id=tenant_id, scope=scope)


@analytics_bp.route("", methods=["GET"])
def analytics_root():
    return analytics_ui()
