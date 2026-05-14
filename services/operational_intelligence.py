from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from models import (
    AnalyticsEventV2,
    ChatSessionContext,
    EncEncuesta,
    EncRespuesta,
    MunicipioTicket,
    PublicSurvey,
    PublicSurveyResponse,
    PymeTicket,
    TenantProfile,
    TenantTicket,
    TicketRealtimeState,
    User,
)


_CLOSED_STATES = {"cerrado", "closed", "resuelto", "resolved", "finalizado", "done"}
_OVERDUE_STATES = {"vencido", "overdue", "breached"}
_ACTIVE_PRESENCE = {"active", "online", "typing", "present"}
_LIVE_SURVEY_STATES = {"publicada", "published", "activa", "active", "en_vivo", "live"}


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _norm(value: Any, default: str = "unknown") -> str:
    text = str(value or "").strip().lower()
    return text or default


def _counter(counter: Counter, *, limit: int = 12) -> list[dict[str, Any]]:
    return [{"key": key, "label": key, "count": int(count)} for key, count in counter.most_common(limit)]


def _between(query, column, start_date: datetime, end_date: datetime):
    return query.filter(column >= start_date, column <= end_date)


def _tenant_ref(tenant: TenantProfile) -> dict[str, Any]:
    return {
        "id": tenant.id,
        "slug": tenant.slug,
        "nombre": tenant.nombre,
        "tipo": tenant.tipo,
        "plan": tenant.plan,
    }


def _period_delta(start_date: datetime, end_date: datetime) -> timedelta:
    delta = end_date - start_date
    if delta.total_seconds() <= 0:
        return timedelta(days=7)
    return delta


def _percent_change(current: float | int, previous: float | int) -> float | None:
    current_value = float(current or 0)
    previous_value = float(previous or 0)
    if previous_value == 0:
        return None if current_value == 0 else 100.0
    return round(((current_value - previous_value) / previous_value) * 100, 2)


def _trend_direction(current: float | int, previous: float | int) -> str:
    if current > previous:
        return "up"
    if current < previous:
        return "down"
    return "flat"


def _trend_item(key: str, current: float | int, previous: float | int) -> dict[str, Any]:
    return {
        "key": key,
        "current": current,
        "previous": previous,
        "direction": _trend_direction(current, previous),
        "percent_change": _percent_change(current, previous),
    }


def _priority_weight(priority: str, status: str) -> float:
    priority_score = {"low": 0.8, "baja": 0.8, "normal": 1.0, "medium": 1.0, "media": 1.0, "high": 1.4, "alta": 1.4, "urgent": 1.8, "critica": 1.8}
    status_score = 1.5 if status in _OVERDUE_STATES else 1.0
    return round(priority_score.get(_norm(priority, "normal"), 1.0) * status_score, 2)


def _tenant_ticket_record(ticket: TenantTicket) -> dict[str, Any]:
    extra = _as_dict(ticket.datos_extra)
    status = _norm(ticket.estado, "nuevo")
    priority = _norm(extra.get("priority") or extra.get("prioridad"), "normal")
    channel = _norm(extra.get("channel") or extra.get("canal") or ticket.origen, "web")
    return {
        "source": "tenant_ticket",
        "id": ticket.id,
        "title": extra.get("title") or ticket.categoria or f"Ticket {ticket.id}",
        "status": status,
        "priority": priority,
        "channel": channel,
        "category": _norm(ticket.categoria, "sin_categoria"),
        "assignee_id": extra.get("assignee_id"),
        "zone": _norm(extra.get("zone") or extra.get("zona") or extra.get("address"), "sin_zona"),
        "lat": ticket.latitud,
        "lng": ticket.longitud,
        "created_at": getattr(ticket, "created_at", None),
        "updated_at": getattr(ticket, "updated_at", None),
        "overdue": status in _OVERDUE_STATES or _norm(extra.get("sla_status") or extra.get("sla_state"), "") in _OVERDUE_STATES,
    }


def _municipio_ticket_record(ticket: MunicipioTicket) -> dict[str, Any]:
    status = _norm(ticket.estado, "nuevo")
    channel = _norm(getattr(ticket, "canal_ingreso", None), "web")
    return {
        "source": "municipio_ticket",
        "id": ticket.id,
        "title": ticket.asunto or ticket.categoria or f"Reclamo {ticket.nro_ticket}",
        "status": status,
        "priority": "normal",
        "channel": channel,
        "category": _norm(ticket.categoria, "sin_categoria"),
        "assignee_id": getattr(ticket, "asignado_a_id", None),
        "zone": _norm(ticket.distrito or ticket.direccion, "sin_zona"),
        "lat": ticket.latitud,
        "lng": ticket.longitud,
        "created_at": ticket.fecha,
        "updated_at": ticket.ultima_actividad or ticket.fecha,
        "overdue": status in _OVERDUE_STATES,
    }


def _pyme_ticket_record(ticket: PymeTicket) -> dict[str, Any]:
    status = _norm(ticket.estado, "nuevo")
    return {
        "source": "pyme_ticket",
        "id": ticket.id,
        "title": ticket.asunto or ticket.categoria or f"Ticket {ticket.nro_ticket}",
        "status": status,
        "priority": "normal",
        "channel": "web",
        "category": _norm(ticket.categoria, "sin_categoria"),
        "assignee_id": getattr(ticket, "asignado_a_id", None),
        "zone": "sin_zona",
        "lat": None,
        "lng": None,
        "created_at": ticket.fecha,
        "updated_at": ticket.fecha,
        "overdue": status in _OVERDUE_STATES,
    }


def _collect_ticket_records(tenant: TenantProfile, start_date: datetime, end_date: datetime) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    tenant_tickets = _between(TenantTicket.query.filter_by(tenant_id=tenant.id), TenantTicket.created_at, start_date, end_date).all()
    records.extend(_tenant_ticket_record(ticket) for ticket in tenant_tickets)

    municipio_tickets = _between(MunicipioTicket.query.filter_by(tenant_id=tenant.id), MunicipioTicket.fecha, start_date, end_date).all()
    records.extend(_municipio_ticket_record(ticket) for ticket in municipio_tickets)

    pyme_tickets = _between(PymeTicket.query.filter_by(tenant_id=tenant.id), PymeTicket.fecha, start_date, end_date).all()
    records.extend(_pyme_ticket_record(ticket) for ticket in pyme_tickets)

    return records


def _ticket_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = Counter(record["status"] for record in records)
    channel_counts = Counter(record["channel"] for record in records)
    category_counts = Counter(record["category"] for record in records)
    priority_counts = Counter(record["priority"] for record in records)
    source_counts = Counter(record["source"] for record in records)
    open_records = [record for record in records if record["status"] not in _CLOSED_STATES]
    overdue_records = [record for record in records if record.get("overdue")]

    return {
        "summary": {
            "total": len(records),
            "open": len(open_records),
            "closed": len(records) - len(open_records),
            "overdue": len(overdue_records),
            "unassigned": len([record for record in open_records if not record.get("assignee_id")]),
            "whatsapp": len([record for record in records if record["channel"] == "whatsapp"]),
            "with_location": len([record for record in records if record.get("lat") is not None and record.get("lng") is not None]),
        },
        "by_status": _counter(status_counts),
        "by_channel": _counter(channel_counts),
        "by_category": _counter(category_counts),
        "by_priority": _counter(priority_counts),
        "by_source": _counter(source_counts),
    }


def _survey_metrics(tenant: TenantProfile, start_date: datetime, end_date: datetime) -> dict[str, Any]:
    encuestas = EncEncuesta.query.filter_by(tenant_id=tenant.id).all()
    enc_respuestas = _between(EncRespuesta.query.filter_by(tenant_id=tenant.id), EncRespuesta.submitted_at, start_date, end_date).all()
    public_surveys = PublicSurvey.query.filter_by(tenant_id=tenant.id).all()
    public_survey_ids = [survey.id for survey in public_surveys]
    public_response_count = 0
    if public_survey_ids:
        public_response_count = (
            _between(PublicSurveyResponse.query.filter(PublicSurveyResponse.survey_id.in_(public_survey_ids)), PublicSurveyResponse.created_at, start_date, end_date)
            .count()
        )

    live_votaciones = [
        encuesta
        for encuesta in encuestas
        if bool(encuesta.es_votacion_envivo)
        or "vot" in _norm(encuesta.tipo, "")
        or "vot" in _norm(encuesta.titulo, "")
    ]
    active_encuestas = [encuesta for encuesta in encuestas if _norm(encuesta.estado, "") in _LIVE_SURVEY_STATES]
    responses_by_channel = Counter(_norm(respuesta.canal, "unknown") for respuesta in enc_respuestas)
    responses_with_geo = [respuesta for respuesta in enc_respuestas if respuesta.lat is not None and respuesta.lng is not None]

    return {
        "summary": {
            "encuestas": len(encuestas),
            "public_surveys": len(public_surveys),
            "active": len(active_encuestas),
            "votaciones_live": len(live_votaciones),
            "responses": len(enc_respuestas) + public_response_count,
            "responses_with_geo": len(responses_with_geo),
            "public_responses": public_response_count,
        },
        "by_channel": _counter(responses_by_channel),
        "live_items": [
            {
                "id": encuesta.id,
                "slug": encuesta.slug,
                "title": encuesta.titulo,
                "status": encuesta.estado,
                "show_live_results": bool(encuesta.mostrar_resultados_envivo),
            }
            for encuesta in live_votaciones[:10]
        ],
    }


def _chat_metrics(tenant: TenantProfile, start_date: datetime, end_date: datetime) -> dict[str, Any]:
    events = _between(AnalyticsEventV2.query.filter_by(tenant_id=tenant.id), AnalyticsEventV2.ts, start_date, end_date).all()
    message_events = [event for event in events if _norm(event.event_name, "") in {"message_in", "message_out", "chat_message", "whatsapp_message"}]
    handoff_events = [event for event in events if "handoff" in _norm(event.event_name, "")]
    sessions = _between(ChatSessionContext.query.filter_by(tenant_id=tenant.id), ChatSessionContext.last_updated, start_date, end_date).all()

    by_channel = Counter(_norm(event.channel, "unknown") for event in message_events)
    by_event = Counter(_norm(event.event_name, "unknown") for event in events)
    whatsapp_messages = sum(count for channel, count in by_channel.items() if "whatsapp" in channel)
    widget_messages = sum(count for channel, count in by_channel.items() if "widget" in channel or channel == "web")

    return {
        "summary": {
            "events": len(events),
            "messages": len(message_events),
            "sessions": len(sessions),
            "whatsapp_messages": whatsapp_messages,
            "widget_messages": widget_messages,
            "handoffs": len(handoff_events),
            "handoff_rate": round((len(handoff_events) / len(message_events)) * 100, 2) if message_events else 0.0,
        },
        "by_channel": _counter(by_channel),
        "by_event": _counter(by_event),
    }


def _employee_metrics(tenant: TenantProfile, records: list[dict[str, Any]]) -> dict[str, Any]:
    employees = User.query.filter_by(tenant_id=tenant.id, es_empleado=True).order_by(User.id.asc()).all()
    open_records = [record for record in records if record["status"] not in _CLOSED_STATES]
    workload = Counter(str(record.get("assignee_id")) for record in open_records if record.get("assignee_id"))
    categories = sorted({record["category"] for record in open_records})
    channels = sorted({record["channel"] for record in open_records})

    items = []
    covered_categories = set()
    covered_channels = set()
    for employee in employees:
        scope = _as_dict((_as_dict(employee.accesibilidad)).get("employee_scope"))
        emp_categories = {str(item).strip().lower() for item in scope.get("categorias", []) if str(item).strip()}
        emp_channels = {str(item).strip().lower() for item in scope.get("channels", []) if str(item).strip()}
        covered_categories.update(emp_categories)
        covered_channels.update(emp_channels)
        items.append(
            {
                "id": employee.id,
                "name": employee.name,
                "email": employee.email,
                "role": employee.rol,
                "scope": {
                    "categorias": sorted(emp_categories),
                    "channels": sorted(emp_channels),
                    "zonas": [str(item).strip().lower() for item in scope.get("zonas", []) if str(item).strip()],
                },
                "open_workload": workload.get(str(employee.id), 0),
            }
        )

    total_dimensions = len(categories) + len(channels)
    covered_dimensions = len(set(categories) & covered_categories) + len(set(channels) & covered_channels)
    coverage_rate = round((covered_dimensions / total_dimensions) * 100, 2) if total_dimensions else 100.0

    return {
        "summary": {
            "employees": len(employees),
            "assigned_open_tickets": sum(workload.values()),
            "coverage_rate": coverage_rate,
            "unassigned_open_tickets": len([record for record in open_records if not record.get("assignee_id")]),
        },
        "items": items,
        "coverage": {
            "categories": categories,
            "channels": channels,
            "uncovered_categories": sorted(set(categories) - covered_categories),
            "uncovered_channels": sorted(set(channels) - covered_channels),
        },
    }


def _active_presence(records: list[dict[str, Any]]) -> dict[str, Any]:
    ids_by_type: dict[str, set[int]] = defaultdict(set)
    for record in records:
        ids_by_type[record["source"]].add(int(record["id"]))

    states = TicketRealtimeState.query.filter(TicketRealtimeState.presence_status.in_(_ACTIVE_PRESENCE)).all()
    active = []
    for state in states:
        ticket_type = _norm(state.ticket_type, "")
        ticket_id = int(state.ticket_id or 0)
        matches = (
            (ticket_type in {"tenant", "tenant_ticket"} and ticket_id in ids_by_type["tenant_ticket"])
            or (ticket_type in {"municipio", "municipio_ticket"} and ticket_id in ids_by_type["municipio_ticket"])
            or (ticket_type in {"pyme", "pyme_ticket"} and ticket_id in ids_by_type["pyme_ticket"])
        )
        if matches:
            active.append(state)

    by_role = Counter(_norm(state.viewer_role, "unknown") for state in active)
    return {
        "active_viewers": len(active),
        "by_role": _counter(by_role),
        "items": [
            {
                "ticket_type": state.ticket_type,
                "ticket_id": state.ticket_id,
                "viewer_role": state.viewer_role,
                "presence_status": state.presence_status,
                "last_presence_at": _iso(state.last_presence_at),
            }
            for state in active[:30]
        ],
    }


def _aware_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _latest_from_query(query, column) -> datetime | None:
    row = query.order_by(column.desc()).first()
    if not row:
        return None
    return _aware_datetime(getattr(row, column.key, None))


def _max_datetime(*values: Any) -> datetime | None:
    normalized = [_aware_datetime(value) for value in values]
    normalized = [value for value in normalized if value is not None]
    if not normalized:
        return None
    return max(normalized)


def _age_seconds(value: datetime | None) -> int | None:
    if not value:
        return None
    return max(0, int((datetime.now(timezone.utc) - value).total_seconds()))


def _freshness_item(
    *,
    key: str,
    label: str,
    latest_at: datetime | None,
    period_count: int,
    stale_after_seconds: int,
    empty_reason: str,
    recommended_action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    age = _age_seconds(latest_at)
    if latest_at is None and period_count == 0:
        status = "empty"
        reason_code = empty_reason
    elif age is not None and age > stale_after_seconds:
        status = "stale"
        reason_code = "source_stale"
    else:
        status = "fresh"
        reason_code = "source_fresh"

    return {
        "key": key,
        "label": label,
        "status": status,
        "reason_code": reason_code,
        "period_count": int(period_count or 0),
        "latest_at": _iso(latest_at),
        "age_seconds": age,
        "stale_after_seconds": stale_after_seconds,
        "recommended_action": recommended_action or {},
    }


def build_operational_freshness(tenant: TenantProfile, start_date: datetime, end_date: datetime) -> dict[str, Any]:
    ticket_records = _collect_ticket_records(tenant, start_date, end_date)
    ticket_latest = _max_datetime(
        _latest_from_query(TenantTicket.query.filter_by(tenant_id=tenant.id), TenantTicket.created_at),
        _latest_from_query(MunicipioTicket.query.filter_by(tenant_id=tenant.id), MunicipioTicket.fecha),
        _latest_from_query(PymeTicket.query.filter_by(tenant_id=tenant.id), PymeTicket.fecha),
    )

    enc_response_latest = _latest_from_query(EncRespuesta.query.filter_by(tenant_id=tenant.id), EncRespuesta.submitted_at)
    public_survey_ids = [survey.id for survey in PublicSurvey.query.filter_by(tenant_id=tenant.id).all()]
    public_response_latest = None
    public_response_count = 0
    if public_survey_ids:
        public_response_query = PublicSurveyResponse.query.filter(PublicSurveyResponse.survey_id.in_(public_survey_ids))
        public_response_latest = _latest_from_query(public_response_query, PublicSurveyResponse.created_at)
        public_response_count = _between(public_response_query, PublicSurveyResponse.created_at, start_date, end_date).count()

    survey_response_count = _between(EncRespuesta.query.filter_by(tenant_id=tenant.id), EncRespuesta.submitted_at, start_date, end_date).count()
    survey_latest = _max_datetime(enc_response_latest, public_response_latest)

    event_query = AnalyticsEventV2.query.filter_by(tenant_id=tenant.id)
    analytics_event_count = _between(event_query, AnalyticsEventV2.ts, start_date, end_date).count()
    analytics_latest = _latest_from_query(event_query, AnalyticsEventV2.ts)

    chat_query = ChatSessionContext.query.filter_by(tenant_id=tenant.id)
    chat_count = _between(chat_query, ChatSessionContext.last_updated, start_date, end_date).count()
    chat_latest = _latest_from_query(chat_query, ChatSessionContext.last_updated)

    employee_count = User.query.filter_by(tenant_id=tenant.id, es_empleado=True).count()
    heatmap = build_operational_heatmap(tenant, start_date, end_date, ticket_records=ticket_records, max_points=250)
    heatmap_latest = None
    for point in heatmap.get("points") or []:
        timestamp = point.get("timestamp")
        if not timestamp:
            continue
        try:
            parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        heatmap_latest = _max_datetime(heatmap_latest, parsed)

    sources = [
        _freshness_item(
            key="tickets",
            label="Tickets y reclamos",
            latest_at=ticket_latest,
            period_count=len(ticket_records),
            stale_after_seconds=6 * 60 * 60,
            empty_reason="no_tickets_in_period",
            recommended_action={"endpoint": "/api/v2/tickets", "ui_hint": "open_ticket_board"},
        ),
        _freshness_item(
            key="surveys",
            label="Encuestas y votaciones",
            latest_at=survey_latest,
            period_count=survey_response_count + public_response_count,
            stale_after_seconds=24 * 60 * 60,
            empty_reason="no_survey_responses_in_period",
            recommended_action={"endpoint": "/api/v2/surveys", "ui_hint": "open_survey_monitor"},
        ),
        _freshness_item(
            key="analytics_events",
            label="Eventos analytics",
            latest_at=analytics_latest,
            period_count=analytics_event_count,
            stale_after_seconds=2 * 60 * 60,
            empty_reason="no_analytics_events_in_period",
            recommended_action={"endpoint": "/api/v2/analytics/operations/dashboard", "ui_hint": "check_tracking"},
        ),
        _freshness_item(
            key="chats",
            label="Sesiones de chat",
            latest_at=chat_latest,
            period_count=chat_count,
            stale_after_seconds=2 * 60 * 60,
            empty_reason="no_chat_sessions_in_period",
            recommended_action={"endpoint": "/api/v2/inbox/omnichannel", "ui_hint": "open_live_chat"},
        ),
        _freshness_item(
            key="heatmap",
            label="Mapa operativo",
            latest_at=heatmap_latest,
            period_count=(heatmap.get("summary") or {}).get("points", 0),
            stale_after_seconds=24 * 60 * 60,
            empty_reason="no_geo_points_in_period",
            recommended_action={"endpoint": "/api/v2/analytics/operations/heatmap", "ui_hint": "open_heatmap"},
        ),
    ]

    stale_sources = [item for item in sources if item["status"] == "stale"]
    empty_sources = [item for item in sources if item["status"] == "empty"]
    latest_at = _max_datetime(*(datetime.fromisoformat(item["latest_at"]) for item in sources if item.get("latest_at")))
    has_operational_data = any(item["period_count"] > 0 for item in sources)

    if not has_operational_data:
        status = "empty"
        reason_code = "no_operational_data_in_period"
    elif stale_sources:
        status = "degraded"
        reason_code = "one_or_more_sources_stale"
    else:
        status = "fresh"
        reason_code = "all_sources_fresh"

    return {
        "contract_version": "operations.freshness.v1",
        "tenant": _tenant_ref(tenant),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": {"from": _iso(start_date), "to": _iso(end_date)},
        "status": status,
        "reason_code": reason_code,
        "summary": {
            "sources": len(sources),
            "fresh_sources": len([item for item in sources if item["status"] == "fresh"]),
            "stale_sources": len(stale_sources),
            "empty_sources": len(empty_sources),
            "latest_at": _iso(latest_at),
            "employee_count": employee_count,
            "has_operational_data": has_operational_data,
            "can_render_dashboard": has_operational_data,
            "can_render_heatmap": bool((heatmap.get("summary") or {}).get("points", 0)),
        },
        "sources": sources,
        "frontend_contract": {
            "render_as": "analytics_freshness",
            "primary_refresh_seconds": 60,
            "empty_state_behavior": "show_reason_code",
            "degraded_state_behavior": "show_stale_sources",
        },
    }


def build_operational_heatmap(
    tenant: TenantProfile,
    start_date: datetime,
    end_date: datetime,
    *,
    ticket_records: list[dict[str, Any]] | None = None,
    max_points: int = 1000,
) -> dict[str, Any]:
    records = ticket_records if ticket_records is not None else _collect_ticket_records(tenant, start_date, end_date)
    points: list[dict[str, Any]] = []

    for record in records:
        if record.get("lat") is None or record.get("lng") is None:
            continue
        points.append(
            {
                "source": "ticket",
                "id": f"{record['source']}:{record['id']}",
                "lat": float(record["lat"]),
                "lng": float(record["lng"]),
                "weight": _priority_weight(record["priority"], record["status"]),
                "category": record["category"],
                "channel": record["channel"],
                "status": record["status"],
                "label": record["title"],
                "timestamp": _iso(record.get("created_at")),
            }
        )

    survey_responses = _between(EncRespuesta.query.filter_by(tenant_id=tenant.id), EncRespuesta.submitted_at, start_date, end_date).filter(
        EncRespuesta.lat.isnot(None),
        EncRespuesta.lng.isnot(None),
    ).limit(max_points).all()
    for response in survey_responses:
        points.append(
            {
                "source": "survey",
                "id": f"survey_response:{response.id}",
                "lat": float(response.lat),
                "lng": float(response.lng),
                "weight": 0.8,
                "category": "survey_response",
                "channel": _norm(response.canal, "survey"),
                "status": "submitted",
                "label": response.barrio or response.ciudad or "Respuesta de encuesta",
                "timestamp": _iso(response.submitted_at),
            }
        )

    events = _between(AnalyticsEventV2.query.filter_by(tenant_id=tenant.id), AnalyticsEventV2.ts, start_date, end_date).filter(
        AnalyticsEventV2.lat.isnot(None),
        AnalyticsEventV2.lng.isnot(None),
    ).limit(max_points).all()
    for event in events:
        points.append(
            {
                "source": "analytics_event",
                "id": f"analytics_event:{event.id}",
                "lat": float(event.lat),
                "lng": float(event.lng),
                "weight": 0.6,
                "category": _norm(event.event_name, "event"),
                "channel": _norm(event.channel, "unknown"),
                "status": "event",
                "label": event.event_name,
                "timestamp": _iso(event.ts),
            }
        )

    points = points[:max_points]
    cells: dict[str, dict[str, Any]] = {}
    for point in points:
        cell_id = f"{round(point['lat'], 3)}:{round(point['lng'], 3)}"
        cell = cells.setdefault(
            cell_id,
            {
                "id": cell_id,
                "lat": round(point["lat"], 3),
                "lng": round(point["lng"], 3),
                "weight": 0.0,
                "count": 0,
                "sources": Counter(),
                "categories": Counter(),
            },
        )
        cell["weight"] += float(point.get("weight") or 1.0)
        cell["count"] += 1
        cell["sources"][point["source"]] += 1
        cell["categories"][point["category"]] += 1

    cell_items = []
    for cell in cells.values():
        cell_items.append(
            {
                "id": cell["id"],
                "lat": cell["lat"],
                "lng": cell["lng"],
                "weight": round(cell["weight"], 2),
                "count": cell["count"],
                "sources": _counter(cell["sources"], limit=5),
                "top_categories": _counter(cell["categories"], limit=5),
            }
        )
    cell_items.sort(key=lambda item: (item["weight"], item["count"]), reverse=True)

    bounds = None
    if points:
        lats = [point["lat"] for point in points]
        lngs = [point["lng"] for point in points]
        bounds = {"north": max(lats), "south": min(lats), "east": max(lngs), "west": min(lngs)}

    return {
        "contract_version": "operations.heatmap.v1",
        "tenant": _tenant_ref(tenant),
        "period": {"from": _iso(start_date), "to": _iso(end_date)},
        "render_contract": {
            "state": "ready" if points else "empty",
            "can_render_heatmap": bool(points),
            "empty_reason": None if points else "no_real_geo_points",
            "map_engine": "maplibre",
            "layers": ["tickets", "surveys", "analytics_events"],
            "point_format": {"lat": "number", "lng": "number", "weight": "number"},
        },
        "summary": {
            "points": len(points),
            "cells": len(cell_items),
            "can_render_heatmap": bool(points),
            "ticket_points": len([point for point in points if point["source"] == "ticket"]),
            "survey_points": len([point for point in points if point["source"] == "survey"]),
            "event_points": len([point for point in points if point["source"] == "analytics_event"]),
        },
        "bounds": bounds,
        "points": points,
        "cells": cell_items,
        "hotspots": cell_items[:10],
    }


def _build_alerts(ticket_metrics: dict[str, Any], survey_metrics: dict[str, Any], chat_metrics: dict[str, Any], employee_metrics: dict[str, Any], heatmap: dict[str, Any]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    ticket_summary = ticket_metrics.get("summary") or {}
    survey_summary = survey_metrics.get("summary") or {}
    chat_summary = chat_metrics.get("summary") or {}
    employee_summary = employee_metrics.get("summary") or {}

    if ticket_summary.get("overdue", 0) > 0:
        alerts.append({"severity": "high", "reason_code": "tickets_overdue", "message": "Hay tickets o reclamos vencidos que requieren accion."})
    if ticket_summary.get("unassigned", 0) > 0:
        alerts.append({"severity": "medium", "reason_code": "tickets_unassigned", "message": "Hay tickets abiertos sin responsable asignado."})
    if ticket_summary.get("whatsapp", 0) > 0 and employee_summary.get("employees", 0) == 0:
        alerts.append({"severity": "high", "reason_code": "whatsapp_without_team", "message": "Entraron reclamos por WhatsApp pero no hay empleados configurados."})
    if survey_summary.get("votaciones_live", 0) > 0 and survey_summary.get("responses", 0) == 0:
        alerts.append({"severity": "medium", "reason_code": "live_vote_without_responses", "message": "Hay votaciones activas sin respuestas recientes."})
    if chat_summary.get("handoff_rate", 0) >= 30:
        alerts.append({"severity": "medium", "reason_code": "high_handoff_rate", "message": "La tasa de derivacion humana esta alta; revisar intents y respuestas."})
    hotspot = (heatmap.get("hotspots") or [None])[0]
    if hotspot and hotspot.get("count", 0) >= 5:
        alerts.append({"severity": "medium", "reason_code": "heatmap_hotspot", "message": "Se detecto una zona con alta concentracion de actividad.", "cell_id": hotspot.get("id")})

    return alerts


def _summary_from_metrics(ticket_metrics: dict[str, Any], survey_metrics: dict[str, Any], chat_metrics: dict[str, Any], employee_metrics: dict[str, Any], heatmap: dict[str, Any], alerts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "open_tickets": ticket_metrics["summary"]["open"],
        "overdue_tickets": ticket_metrics["summary"]["overdue"],
        "survey_responses": survey_metrics["summary"]["responses"],
        "live_votes": survey_metrics["summary"]["votaciones_live"],
        "chat_messages": chat_metrics["summary"]["messages"],
        "whatsapp_messages": chat_metrics["summary"]["whatsapp_messages"],
        "employees": employee_metrics["summary"]["employees"],
        "heatmap_points": heatmap["summary"]["points"],
        "alerts": len(alerts or []),
    }


def _build_trends(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "open_tickets",
        "overdue_tickets",
        "survey_responses",
        "live_votes",
        "chat_messages",
        "whatsapp_messages",
        "heatmap_points",
    ]
    return {
        "contract_version": "operations.trends.v1",
        "items": [_trend_item(key, current.get(key, 0), previous.get(key, 0)) for key in keys],
    }


def _action(
    *,
    action_id: str,
    title: str,
    description: str,
    priority: str,
    reason_code: str,
    endpoint: str,
    method: str = "GET",
    payload_template: dict[str, Any] | None = None,
    ui_hint: str = "open_view",
) -> dict[str, Any]:
    return {
        "id": action_id,
        "title": title,
        "description": description,
        "priority": priority,
        "reason_code": reason_code,
        "endpoint": endpoint,
        "method": method,
        "payload_template": payload_template or {},
        "ui_hint": ui_hint,
    }


def _build_next_best_actions(
    ticket_metrics: dict[str, Any],
    survey_metrics: dict[str, Any],
    chat_metrics: dict[str, Any],
    employee_metrics: dict[str, Any],
    heatmap: dict[str, Any],
    alerts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    ticket_summary = ticket_metrics.get("summary") or {}
    survey_summary = survey_metrics.get("summary") or {}
    chat_summary = chat_metrics.get("summary") or {}
    employee_summary = employee_metrics.get("summary") or {}
    coverage = employee_metrics.get("coverage") or {}

    if ticket_summary.get("overdue", 0) > 0:
        actions.append(
            _action(
                action_id="review_overdue_tickets",
                title="Revisar tickets vencidos",
                description="Priorizar reclamos y tickets con SLA vencido antes de que escalen.",
                priority="high",
                reason_code="tickets_overdue",
                endpoint="/api/v2/tickets?status=overdue",
            )
        )

    if ticket_summary.get("unassigned", 0) > 0:
        actions.append(
            _action(
                action_id="assign_unassigned_tickets",
                title="Asignar tickets sin responsable",
                description="Hay tickets abiertos sin empleado asignado. Conviene resolver ownership primero.",
                priority="high" if ticket_summary.get("unassigned", 0) >= 5 else "medium",
                reason_code="tickets_unassigned",
                endpoint="/api/v2/inbox/omnichannel/actions",
                method="POST",
                payload_template={"action": "assign", "ticket_id": "{ticket_id}", "assignee_id": "{employee_id}"},
                ui_hint="open_assignment_drawer",
            )
        )

    if coverage.get("uncovered_categories") or coverage.get("uncovered_channels"):
        actions.append(
            _action(
                action_id="improve_employee_coverage",
                title="Completar cobertura de empleados",
                description="Hay categorias o canales sin cobertura declarada para la demanda actual.",
                priority="medium",
                reason_code="employee_coverage_gap",
                endpoint="/api/v2/employee-coverage",
                ui_hint="open_employee_coverage",
            )
        )

    if survey_summary.get("votaciones_live", 0) > 0 and survey_summary.get("responses", 0) == 0:
        actions.append(
            _action(
                action_id="promote_live_vote",
                title="Impulsar votacion activa",
                description="Hay una votacion activa sin respuestas recientes. Mostrar QR/link y canales de difusion.",
                priority="medium",
                reason_code="live_vote_without_responses",
                endpoint="/api/v2/analytics/operations/dashboard",
                ui_hint="open_vote_monitor",
            )
        )

    if chat_summary.get("whatsapp_messages", 0) > 0:
        actions.append(
            _action(
                action_id="monitor_whatsapp_claims",
                title="Monitorear reclamos de WhatsApp",
                description="WhatsApp tiene actividad reciente; revisar bandeja omnicanal y handoffs.",
                priority="medium",
                reason_code="whatsapp_activity",
                endpoint="/api/v2/inbox/omnichannel?channel=whatsapp",
            )
        )

    if chat_summary.get("handoff_rate", 0) >= 30:
        actions.append(
            _action(
                action_id="review_high_handoff_rate",
                title="Reducir derivaciones innecesarias",
                description="La tasa de handoff esta alta. Revisar intents, respuestas y formularios de captura.",
                priority="medium",
                reason_code="high_handoff_rate",
                endpoint="/api/v2/analytics/operations/dashboard",
                ui_hint="open_chat_quality_panel",
            )
        )

    hotspot = (heatmap.get("hotspots") or [None])[0]
    if hotspot:
        actions.append(
            _action(
                action_id="inspect_top_hotspot",
                title="Inspeccionar hotspot principal",
                description="El mapa operativo detecto concentracion de actividad en una celda.",
                priority="medium" if hotspot.get("count", 0) < 5 else "high",
                reason_code="heatmap_hotspot",
                endpoint="/api/v2/analytics/operations/heatmap",
                payload_template={"cell_id": hotspot.get("id")},
                ui_hint="open_heatmap_cell",
            )
        )

    if not actions and not alerts:
        actions.append(
            _action(
                action_id="keep_monitoring",
                title="Mantener monitoreo operativo",
                description="No hay alertas criticas. Seguir observando tickets, encuestas, WhatsApp y mapa.",
                priority="low",
                reason_code="all_clear",
                endpoint="/api/v2/analytics/operations/dashboard",
            )
        )

    priority_order = {"high": 0, "medium": 1, "low": 2}
    actions.sort(key=lambda item: priority_order.get(item.get("priority"), 9))
    return actions[:10]


def build_operational_dashboard(tenant: TenantProfile, start_date: datetime, end_date: datetime) -> dict[str, Any]:
    ticket_records = _collect_ticket_records(tenant, start_date, end_date)
    ticket_metrics = _ticket_metrics(ticket_records)
    survey_metrics = _survey_metrics(tenant, start_date, end_date)
    chat_metrics = _chat_metrics(tenant, start_date, end_date)
    employee_metrics = _employee_metrics(tenant, ticket_records)
    live_chat = _active_presence(ticket_records)
    heatmap = build_operational_heatmap(tenant, start_date, end_date, ticket_records=ticket_records)
    alerts = _build_alerts(ticket_metrics, survey_metrics, chat_metrics, employee_metrics, heatmap)
    summary = _summary_from_metrics(ticket_metrics, survey_metrics, chat_metrics, employee_metrics, heatmap, alerts)
    period = _period_delta(start_date, end_date)
    previous_start = start_date - period
    previous_end = start_date
    previous_ticket_records = _collect_ticket_records(tenant, previous_start, previous_end)
    previous_ticket_metrics = _ticket_metrics(previous_ticket_records)
    previous_survey_metrics = _survey_metrics(tenant, previous_start, previous_end)
    previous_chat_metrics = _chat_metrics(tenant, previous_start, previous_end)
    previous_employee_metrics = _employee_metrics(tenant, previous_ticket_records)
    previous_heatmap = build_operational_heatmap(tenant, previous_start, previous_end, ticket_records=previous_ticket_records, max_points=250)
    previous_summary = _summary_from_metrics(
        previous_ticket_metrics,
        previous_survey_metrics,
        previous_chat_metrics,
        previous_employee_metrics,
        previous_heatmap,
        [],
    )
    next_best_actions = _build_next_best_actions(ticket_metrics, survey_metrics, chat_metrics, employee_metrics, heatmap, alerts)

    return {
        "contract_version": "operations.dashboard.v1",
        "tenant": _tenant_ref(tenant),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": {"from": _iso(start_date), "to": _iso(end_date)},
        "summary": summary,
        "previous_summary": previous_summary,
        "trends": _build_trends(summary, previous_summary),
        "tickets": ticket_metrics,
        "surveys": survey_metrics,
        "chats": chat_metrics,
        "live_chat": live_chat,
        "employees": employee_metrics,
        "maps": {
            "heatmap": {
                "summary": heatmap["summary"],
                "bounds": heatmap["bounds"],
                "hotspots": heatmap["hotspots"],
                "render_contract": heatmap["render_contract"],
            }
        },
        "alerts": alerts,
        "next_best_actions": next_best_actions,
        "frontend_contract": {
            "recommended_views": ["executive_summary", "ticket_board", "live_chat_inbox", "survey_vote_monitor", "heatmap", "employee_coverage", "action_center"],
            "primary_refresh_seconds": 30,
            "empty_state_behavior": "show_contract_empty_state",
            "map_layers": ["tickets", "surveys", "analytics_events"],
        },
    }


def build_action_center(tenant: TenantProfile, start_date: datetime, end_date: datetime) -> dict[str, Any]:
    dashboard = build_operational_dashboard(tenant, start_date, end_date)
    actions = dashboard.get("next_best_actions") or []
    high = len([item for item in actions if item.get("priority") == "high"])
    medium = len([item for item in actions if item.get("priority") == "medium"])
    low = len([item for item in actions if item.get("priority") == "low"])

    return {
        "contract_version": "operations.action_center.v1",
        "tenant": dashboard.get("tenant"),
        "generated_at": dashboard.get("generated_at"),
        "period": dashboard.get("period"),
        "summary": {
            "total": len(actions),
            "high": high,
            "medium": medium,
            "low": low,
            "alerts": (dashboard.get("summary") or {}).get("alerts", 0),
        },
        "items": actions,
        "alerts": dashboard.get("alerts") or [],
        "trends": dashboard.get("trends") or {},
        "frontend_contract": {
            "render_as": "action_center",
            "primary_refresh_seconds": 30,
            "empty_state_behavior": "show_monitoring_ok",
        },
    }
