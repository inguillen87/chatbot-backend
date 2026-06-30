from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
import json

from sqlalchemy import or_

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
from services.huggingface_ai_insights import build_collection_ai_insights, build_map_ai_layers


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


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _first_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data.get(key) not in (None, ""):
            return data.get(key)
    return None


def _clean_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _record_has_coordinates(record: dict[str, Any]) -> bool:
    return record.get("lat") is not None and record.get("lng") is not None


def _record_address_from_metadata(metadata: dict[str, Any]) -> str | None:
    return _clean_text(
        _first_value(
            metadata,
            "direccion",
            "address",
            "ubicacion",
            "location",
            "formatted_address",
            "domicilio",
            "calle",
        )
    )


def _normalize_gender(value: Any) -> str:
    raw = _norm(value, "unknown")
    mapping = {
        "f": "femenino",
        "female": "femenino",
        "fem": "femenino",
        "mujer": "femenino",
        "femenino": "femenino",
        "m": "masculino",
        "male": "masculino",
        "masc": "masculino",
        "hombre": "masculino",
        "masculino": "masculino",
        "nb": "no_binario",
        "non_binary": "no_binario",
        "no_binario": "no_binario",
        "no binario": "no_binario",
    }
    return mapping.get(raw, raw if raw else "unknown")


def _age_range(age: Any, explicit_range: Any = None, birth_year: Any = None) -> str:
    explicit = _norm(explicit_range, "")
    if explicit:
        return explicit
    try:
        age_int = int(age) if age not in (None, "") else None
    except (TypeError, ValueError):
        age_int = None
    if age_int is None and birth_year not in (None, ""):
        try:
            age_int = datetime.now(timezone.utc).year - int(birth_year)
        except (TypeError, ValueError):
            age_int = None
    if age_int is None:
        return "unknown"
    if age_int < 18:
        return "menor_18"
    if age_int < 25:
        return "18_24"
    if age_int < 35:
        return "25_34"
    if age_int < 45:
        return "35_44"
    if age_int < 60:
        return "45_59"
    return "60_plus"


def _demographics_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    profile = _json_object(metadata.get("demographics")) or _json_object(metadata.get("perfil_demografico"))
    merged = {**metadata, **profile}
    age = _first_value(merged, "edad", "age")
    gender = _first_value(merged, "genero", "gender", "sexo")
    age_range = _age_range(age, _first_value(merged, "rango_edad", "age_range", "rango_etario"), _first_value(merged, "anio_nacimiento", "birth_year"))
    return {
        "gender": _normalize_gender(gender),
        "age": int(age) if str(age or "").strip().isdigit() else None,
        "age_range": age_range,
        "source": "metadata" if gender is not None or age is not None or age_range != "unknown" else "missing",
    }


def _segment_items(counter: Counter, *, limit: int = 20) -> list[dict[str, Any]]:
    total = sum(int(value or 0) for value in counter.values())
    items = []
    for key, count in counter.most_common(limit):
        value = int(count or 0)
        items.append(
            {
                "key": key,
                "label": key,
                "count": value,
                "share": round((value / total) * 100, 2) if total else 0.0,
            }
        )
    return items


def _normalize_filter_values(values: Any) -> set[str]:
    if isinstance(values, str):
        values = [item.strip() for item in values.split(",")]
    if not isinstance(values, list):
        return set()
    return {_norm(item, "") for item in values if _norm(item, "")}


def _normalize_age_range_filter_values(values: Any) -> set[str]:
    normalized = set()
    for value in _normalize_filter_values(values):
        normalized.add(_age_range(value) if value.isdigit() else value)
    return normalized


def _normalized_heatmap_filter_values(key: str, values: Any) -> set[str]:
    return _normalize_age_range_filter_values(values) if key == "age_range" else _normalize_filter_values(values)


def _point_matches_filters(point: dict[str, Any], filters: dict[str, Any]) -> bool:
    filter_map = {
        "category": point.get("category"),
        "gender": point.get("gender"),
        "age_range": point.get("age_range"),
        "source": point.get("source"),
        "channel": point.get("channel"),
        "status": point.get("status"),
        "zone": point.get("zone"),
        "sla_state": point.get("sla_state"),
        "assignee_id": point.get("assignee_id"),
    }
    for key, raw_values in (filters or {}).items():
        allowed = _normalized_heatmap_filter_values(key, raw_values)
        if not allowed:
            continue
        if key == "source":
            source_allowed = set(allowed)
            aliases = {
                "tickets": "ticket",
                "surveys": "survey",
                "analytics_events": "analytics_event",
                "events": "analytics_event",
            }
            source_allowed.update(aliases.get(value, value) for value in allowed)
            if _norm(filter_map.get(key), "") not in source_allowed:
                return False
            continue
        if _norm(filter_map.get(key), "") not in allowed:
            return False
    return True


def _tenant_ticket_record(ticket: TenantTicket) -> dict[str, Any]:
    extra = _as_dict(ticket.datos_extra)
    status = _norm(ticket.estado, "nuevo")
    sla_state = _norm(extra.get("sla_state") or extra.get("sla_status"), "normal")
    priority = _norm(extra.get("priority") or extra.get("prioridad"), "normal")
    channel = _norm(extra.get("channel") or extra.get("canal") or ticket.origen, "web")
    address = _record_address_from_metadata(extra)
    return {
        "source": "tenant_ticket",
        "id": ticket.id,
        "title": extra.get("title") or ticket.categoria or f"Ticket {ticket.id}",
        "status": status,
        "priority": priority,
        "channel": channel,
        "category": _norm(ticket.categoria, "sin_categoria"),
        "assignee_id": extra.get("assignee_id"),
        "zone": _norm(extra.get("zone") or extra.get("zona") or address, "sin_zona"),
        "address": address,
        "lat": ticket.latitud,
        "lng": ticket.longitud,
        "demographics": _demographics_from_metadata(extra),
        "created_at": getattr(ticket, "created_at", None),
        "updated_at": getattr(ticket, "updated_at", None),
        "sla_state": sla_state,
        "overdue": status in _OVERDUE_STATES or sla_state in _OVERDUE_STATES,
    }


def _municipio_ticket_record(ticket: MunicipioTicket) -> dict[str, Any]:
    status = _norm(ticket.estado, "nuevo")
    channel = _norm(getattr(ticket, "canal_ingreso", None), "web")
    details = _json_object(getattr(ticket, "detalles", None))
    address = _clean_text(getattr(ticket, "direccion", None)) or _record_address_from_metadata(details)
    overdue = status in _OVERDUE_STATES
    return {
        "source": "municipio_ticket",
        "id": ticket.id,
        "title": ticket.asunto or ticket.categoria or f"Reclamo {ticket.nro_ticket}",
        "status": status,
        "priority": "normal",
        "channel": channel,
        "category": _norm(ticket.categoria, "sin_categoria"),
        "assignee_id": getattr(ticket, "asignado_a_id", None),
        "zone": _norm(ticket.distrito or address, "sin_zona"),
        "address": address,
        "lat": ticket.latitud,
        "lng": ticket.longitud,
        "demographics": _demographics_from_metadata(details),
        "created_at": ticket.fecha,
        "updated_at": ticket.ultima_actividad or ticket.fecha,
        "sla_state": "overdue" if overdue else "normal",
        "overdue": overdue,
    }


def _pyme_ticket_record(ticket: PymeTicket) -> dict[str, Any]:
    status = _norm(ticket.estado, "nuevo")
    address = _clean_text(getattr(ticket, "direccion", None))
    overdue = status in _OVERDUE_STATES
    return {
        "source": "pyme_ticket",
        "id": ticket.id,
        "title": ticket.asunto or ticket.categoria or f"Ticket {ticket.nro_ticket}",
        "status": status,
        "priority": "normal",
        "channel": "web",
        "category": _norm(ticket.categoria, "sin_categoria"),
        "assignee_id": getattr(ticket, "asignado_a_id", None),
        "zone": _norm(address, "sin_zona"),
        "address": address,
        "lat": getattr(ticket, "latitud", None),
        "lng": getattr(ticket, "longitud", None),
        "demographics": {"gender": "unknown", "age": None, "age_range": "unknown", "source": "missing"},
        "created_at": ticket.fecha,
        "updated_at": ticket.fecha,
        "sla_state": "overdue" if overdue else "normal",
        "overdue": overdue,
    }


def _collect_ticket_records(tenant: TenantProfile, start_date: datetime, end_date: datetime) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    tenant_tickets = _between(TenantTicket.query.filter_by(tenant_id=tenant.id), TenantTicket.created_at, start_date, end_date).all()
    records.extend(_tenant_ticket_record(ticket) for ticket in tenant_tickets)

    municipio_tickets = _between(_municipio_ticket_query(tenant), MunicipioTicket.fecha, start_date, end_date).all()
    records.extend(_municipio_ticket_record(ticket) for ticket in municipio_tickets)

    pyme_tickets = _between(PymeTicket.query.filter_by(tenant_id=tenant.id), PymeTicket.fecha, start_date, end_date).all()
    records.extend(_pyme_ticket_record(ticket) for ticket in pyme_tickets)

    return records


def _municipio_ticket_query(tenant: TenantProfile):
    conditions = [MunicipioTicket.tenant_id == tenant.id]
    municipio_id = getattr(tenant, "municipio_id", None)
    if municipio_id:
        conditions.append(MunicipioTicket.municipio_id == municipio_id)
    return MunicipioTicket.query.filter(or_(*conditions))


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
    live_control_room = _survey_live_control_room(
        tenant=tenant,
        live_votaciones=live_votaciones,
        responses=enc_respuestas,
        responses_by_channel=responses_by_channel,
        responses_with_geo=responses_with_geo,
    )

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
                "public_token": _public_survey_token(encuesta),
                "live_results_endpoint": f"/api/v2/public/surveys/{_public_survey_token(encuesta)}/live-results",
                "public_url": f"/e/{_public_survey_token(encuesta)}",
            }
            for encuesta in live_votaciones[:10]
        ],
        "live_control_room": live_control_room,
    }


def _public_survey_token(encuesta: EncEncuesta) -> str:
    for link in getattr(encuesta, "links", []) or []:
        slug_publico = str(getattr(link, "slug_publico", "") or "").strip()
        if slug_publico:
            return slug_publico
    return str(getattr(encuesta, "slug", "") or "").strip()


def _survey_live_control_room(
    *,
    tenant: TenantProfile,
    live_votaciones: list[EncEncuesta],
    responses: list[EncRespuesta],
    responses_by_channel: Counter,
    responses_with_geo: list[EncRespuesta],
) -> dict[str, Any]:
    response_by_survey = Counter(int(response.encuesta_id or 0) for response in responses)
    geo_by_survey = Counter(int(response.encuesta_id or 0) for response in responses_with_geo)
    channel_by_survey: dict[int, Counter] = {}
    for response in responses:
        survey_id = int(response.encuesta_id or 0)
        channel_by_survey.setdefault(survey_id, Counter())[_norm(response.canal, "unknown")] += 1

    monitors: list[dict[str, Any]] = []
    for encuesta in live_votaciones[:10]:
        survey_id = int(encuesta.id)
        public_token = _public_survey_token(encuesta)
        total = int(response_by_survey.get(survey_id, 0))
        geo = int(geo_by_survey.get(survey_id, 0))
        show_live_results = bool(encuesta.mostrar_resultados_envivo)
        published = _norm(encuesta.estado, "") in _LIVE_SURVEY_STATES
        monitors.append(
            {
                "id": survey_id,
                "slug": encuesta.slug,
                "public_token": public_token,
                "title": encuesta.titulo,
                "status": encuesta.estado,
                "type": encuesta.tipo,
                "live": bool(encuesta.es_votacion_envivo),
                "published": published,
                "show_live_results": show_live_results,
                "responses": total,
                "responses_with_geo": geo,
                "geo_coverage_rate": round((geo / total) * 100, 2) if total else 0.0,
                "channels": _counter(channel_by_survey.get(survey_id, Counter())),
                "public_url": f"/e/{public_token}",
                "admin_url": f"/admin/encuestas/{survey_id}/analytics",
                "live_results_endpoint": f"/api/v2/public/surveys/{public_token}/live-results",
                "heatmap_endpoint": f"/api/v2/public/surveys/{public_token}/live-results?include_heatmap=1",
                "whatsapp_template_id": "gov_survey_invite" if getattr(tenant, "tipo", "") == "municipio" else "survey_invite",
                "state": (
                    "live_collecting"
                    if published and show_live_results
                    else "published_hidden_results"
                    if published
                    else "setup_required"
                ),
            }
        )

    total_responses = sum(int(item["responses"]) for item in monitors)
    total_geo = sum(int(item["responses_with_geo"]) for item in monitors)
    return {
        "contract_version": "operations.survey_live_control_room.v1",
        "enabled": bool(monitors),
        "state": "live" if any(item["state"] == "live_collecting" for item in monitors) else "setup_required" if monitors else "empty",
        "summary": {
            "live_surveys": len(monitors),
            "responses": total_responses,
            "responses_with_geo": total_geo,
            "geo_coverage_rate": round((total_geo / total_responses) * 100, 2) if total_responses else 0.0,
            "channels": _counter(responses_by_channel),
        },
        "monitors": monitors,
        "realtime": {
            "enabled": True,
            "refresh_seconds": 10 if monitors else 30,
            "socket_events": ["survey.vote.created", "survey.response.created", "analytics.event.created"],
            "fallback_polling": True,
        },
        "actions": [
            {
                "id": "open_surveys_admin",
                "label": "Abrir encuestas",
                "endpoint": "/api/v2/surveys",
                "route": "/admin/encuestas",
            },
            {
                "id": "open_operations_heatmap",
                "label": "Ver mapa operativo",
                "endpoint": "/api/v2/analytics/operations/heatmap",
                "route": "/analytics?tab=operations",
            },
            {
                "id": "sync_whatsapp_survey_template",
                "label": "Preparar plantilla WhatsApp",
                "endpoint": "/api/admin/templates/twilio-content/sync",
                "template_id": "gov_survey_invite" if getattr(tenant, "tipo", "") == "municipio" else "survey_invite",
            },
        ],
        "frontend_contract": {
            "render_as": "survey_live_control_room",
            "recommended_widgets": ["live_vote_cards", "channel_mix", "heatmap_coverage", "whatsapp_template_action"],
            "empty_state": "show_create_or_publish_survey_cta",
        },
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
        _latest_from_query(_municipio_ticket_query(tenant), MunicipioTicket.fecha),
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


def _record_to_filter_probe(record: dict[str, Any]) -> dict[str, Any]:
    demographics = _as_dict(record.get("demographics"))
    return {
        "source": "ticket",
        "record_source": record.get("source"),
        "category": record.get("category"),
        "channel": record.get("channel"),
        "status": record.get("status"),
        "zone": record.get("zone"),
        "sla_state": record.get("sla_state"),
        "assignee_id": str(record.get("assignee_id")) if record.get("assignee_id") is not None else None,
        "gender": demographics.get("gender") or "unknown",
        "age_range": demographics.get("age_range") or "unknown",
    }


def _ticket_action_contract(record: dict[str, Any]) -> list[dict[str, Any]]:
    record_source = str(record.get("source") or "")
    record_id = record.get("id")
    if record_id is None:
        return []

    source_config = {
        "tenant_ticket": {
            "open_endpoint": f"/api/v2/tickets/{record_id}",
            "location_endpoint": f"/api/v2/tickets/{record_id}",
            "location_method": "PATCH",
            "location_body_template": {"location": {"lat": "number", "lng": "number", "address": "string"}},
            "requires": ["location.lat", "location.lng"],
        },
        "municipio_ticket": {
            "open_endpoint": f"/tickets/municipio/{record_id}",
            "location_endpoint": f"/tickets/municipio/{record_id}/ubicacion",
            "location_method": "PUT",
            "location_body_template": {"latitud": "number", "longitud": "number", "direccion": "string"},
            "requires": ["latitud", "longitud"],
        },
        "pyme_ticket": {
            "open_endpoint": f"/tickets/pyme/{record_id}",
            "location_endpoint": f"/tickets/pyme/{record_id}/ubicacion",
            "location_method": "PUT",
            "location_body_template": {"latitud": "number", "longitud": "number", "direccion": "string"},
            "requires": ["latitud", "longitud"],
        },
    }.get(record_source)

    if not source_config:
        return []

    return [
        {
            "id": "open_record",
            "label": "Abrir caso",
            "type": "api",
            "method": "GET",
            "endpoint": source_config["open_endpoint"],
            "record_source": record_source,
            "record_id": record_id,
        },
        {
            "id": "update_location",
            "label": "Actualizar ubicacion",
            "type": "api",
            "method": source_config["location_method"],
            "endpoint": source_config["location_endpoint"],
            "record_source": record_source,
            "record_id": record_id,
            "requires": source_config["requires"],
            "body_template": source_config["location_body_template"],
        },
    ]


def _geocoding_candidate(record: dict[str, Any]) -> dict[str, Any]:
    demographics = _as_dict(record.get("demographics"))
    return {
        "id": f"{record.get('source')}:{record.get('id')}",
        "record_source": record.get("source"),
        "record_id": record.get("id"),
        "category": record.get("category"),
        "channel": record.get("channel"),
        "status": record.get("status"),
        "zone": record.get("zone"),
        "address": record.get("address"),
        "label": record.get("title"),
        "timestamp": _iso(record.get("created_at")),
        "gender": demographics.get("gender") or "unknown",
        "age_range": demographics.get("age_range") or "unknown",
        "reason_code": "address_without_coordinates",
        "sla_state": record.get("sla_state") or "normal",
        "overdue": bool(record.get("overdue")),
        "assignee_id": record.get("assignee_id"),
        "actions": _ticket_action_contract(record),
    }


def _location_quality(records: list[dict[str, Any]], geocoding_candidates: list[dict[str, Any]]) -> dict[str, Any]:
    ticket_records = [record for record in records if record.get("source") in {"tenant_ticket", "municipio_ticket", "pyme_ticket"}]
    with_coordinates = [record for record in ticket_records if _record_has_coordinates(record)]
    with_address = [record for record in ticket_records if record.get("address")]
    missing_location = [
        record
        for record in ticket_records
        if not _record_has_coordinates(record) and not record.get("address")
    ]
    total = len(ticket_records)
    coverage_pct = round((len(with_coordinates) / total) * 100, 2) if total else 0.0
    return {
        "contract_version": "operations.location_quality.v1",
        "total_ticket_records": total,
        "ticket_records_with_coordinates": len(with_coordinates),
        "ticket_records_with_address": len(with_address),
        "ticket_records_pending_geocode": len(geocoding_candidates),
        "ticket_records_without_location": len(missing_location),
        "coordinate_coverage_pct": coverage_pct,
        "status": "ready" if with_coordinates else ("pending_geocode" if geocoding_candidates else "empty"),
        "reason_code": (
            "coordinates_available"
            if with_coordinates
            else ("addresses_need_geocoding" if geocoding_candidates else "no_ticket_locations")
        ),
    }


def _heatmap_quality_contract(
    *,
    points: list[dict[str, Any]],
    records: list[dict[str, Any]],
    geocoding_candidates: list[dict[str, Any]],
    location_quality: dict[str, Any],
    max_points: int,
) -> dict[str, Any]:
    total_ticket_records = int(location_quality.get("total_ticket_records") or 0)
    ticket_records_with_coordinates = int(location_quality.get("ticket_records_with_coordinates") or 0)
    pending_geocode = len(geocoding_candidates or [])
    visible_points = len(points or [])
    ticket_coverage_rate = round(ticket_records_with_coordinates / total_ticket_records, 4) if total_ticket_records else 0.0
    has_survey_or_event_points = any(point.get("source") in {"survey", "analytics_event"} for point in points or [])

    if visible_points:
        if total_ticket_records and ticket_coverage_rate < 0.35 and not has_survey_or_event_points:
            state = "partial"
            reason_code = "low_ticket_coordinate_coverage"
            label = "Cobertura territorial parcial"
        else:
            state = "ready"
            reason_code = "ready"
            label = "Mapa operativo confiable"
    elif pending_geocode:
        state = "pending_geocode"
        reason_code = "addresses_need_geocoding"
        label = "Direcciones pendientes de geocodificar"
    elif records:
        state = "blocked"
        reason_code = "missing_coordinates"
        label = "Sin coordenadas reales"
    else:
        state = "empty"
        reason_code = "no_operational_events"
        label = "Sin eventos para el periodo"

    return {
        "contract_version": "operations.heatmap_quality.v1",
        "state": state,
        "label": label,
        "reason_code": reason_code,
        "coverage_rate": ticket_coverage_rate,
        "coverage_percent": round(ticket_coverage_rate * 100, 1),
        "visible_points": visible_points,
        "total_ticket_records": total_ticket_records,
        "ticket_records_with_coordinates": ticket_records_with_coordinates,
        "ticket_records_without_coordinates": max(0, total_ticket_records - ticket_records_with_coordinates),
        "pending_geocode": pending_geocode,
        "max_points": max_points,
        "can_render_heatmap": bool(visible_points),
        "empty_state_action": {
            "label": "Capturar ubicacion en WhatsApp",
            "description": "Pedir ubicacion o geocodificar direcciones mejora el mapa, los hotspots y la asignacion por zona.",
            "endpoint": "/api/v2/analytics/operations/heatmap",
            "ui_hint": "open_geocoding_queue",
        },
    }


def _heatmap_realtime_contract(points: list[dict[str, Any]]) -> dict[str, Any]:
    timestamps: list[datetime] = []
    for point in points or []:
        raw_ts = point.get("timestamp")
        if not raw_ts:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        except ValueError:
            continue
        timestamps.append(parsed)

    latest = max(timestamps) if timestamps else None
    return {
        "contract_version": "operations.heatmap_realtime.v1",
        "poll_seconds": 20,
        "socket_namespace": "analytics",
        "socket_events": ["ticket.updated", "survey.vote.created", "whatsapp.message.created", "analytics.event.created"],
        "latest_event_at": _iso(latest) if latest else None,
        "sources": ["tickets", "surveys", "analytics_events", "whatsapp"],
    }


def _bounds_center(bounds: dict[str, Any] | None) -> dict[str, float] | None:
    if not bounds:
        return None
    try:
        return {
            "lat": round((float(bounds["north"]) + float(bounds["south"])) / 2, 6),
            "lng": round((float(bounds["east"]) + float(bounds["west"])) / 2, 6),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _heatmap_narrative_contract(
    tenant: TenantProfile,
    *,
    summary: dict[str, Any],
    quality: dict[str, Any],
    category_layers: list[dict[str, Any]],
    geocoding_candidates: list[dict[str, Any]],
    ai_summary: dict[str, Any],
) -> dict[str, Any]:
    state = str(quality.get("state") or "empty")
    points = int(summary.get("points") or 0)
    cells = int(summary.get("cells") or 0)
    pending_geocode = len(geocoding_candidates or [])
    top_category = (category_layers[0] if category_layers else {}).get("key")
    tenant_type = _norm(getattr(tenant, "tipo", None), "operacion")

    if points:
        headline = f"{points} puntos territoriales listos para decision"
        body = (
            f"El mapa consolida {cells} zonas activas con cobertura "
            f"{quality.get('coverage_percent', 0)}% y senales AI en modo {ai_summary.get('risk_level') or 'normal'}."
        )
    elif pending_geocode:
        headline = f"{pending_geocode} direcciones listas para geocodificar"
        body = "La UI puede mostrar la cola territorial y pedir coordenadas antes de pintar calor real."
    elif state == "blocked":
        headline = "Sin coordenadas reales para pintar el territorio"
        body = "Hay actividad operativa, pero falta latitud/longitud o direcciones utiles para construir hotspots."
    else:
        headline = "Sin eventos territoriales para el periodo"
        body = "El mapa queda en estado vacio y puede mostrar configuracion, captura de ubicacion y filtros."

    return {
        "contract_version": "operations.heatmap_narrative.v1",
        "state": state,
        "tenant_type": tenant_type,
        "headline": headline,
        "body": body,
        "primary_metric": {"key": "visible_points", "label": "Puntos visibles", "value": points},
        "secondary_metrics": [
            {"key": "hotspot_cells", "label": "Zonas activas", "value": cells},
            {"key": "coverage_percent", "label": "Cobertura GPS", "value": quality.get("coverage_percent", 0)},
            {"key": "pending_geocode", "label": "Direcciones pendientes", "value": pending_geocode},
        ],
        "focus": {
            "top_category": top_category,
            "risk_level": ai_summary.get("risk_level") or "normal",
            "dominant_intent": ai_summary.get("dominant_intent") or "general_query",
            "requires_human_attention": bool(ai_summary.get("requires_human_attention")),
        },
        "empty_state": {
            "reason_code": quality.get("reason_code"),
            "recommended_view": "geocoding_queue" if pending_geocode else "capture_location_setup",
        },
    }


def _heatmap_viewport_presets(
    *,
    bounds: dict[str, Any] | None,
    hotspots: list[dict[str, Any]],
    quality: dict[str, Any],
    geocoding_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    center = _bounds_center(bounds)
    presets: list[dict[str, Any]] = []
    if bounds and center:
        presets.append(
            {
                "id": "fit_operational_bounds",
                "label": "Territorio activo",
                "mode": "fit_bounds",
                "default": True,
                "bounds": bounds,
                "center": center,
                "padding_px": 72,
                "reason_code": "points_available",
            }
        )
    if hotspots:
        hotspot = hotspots[0]
        presets.append(
            {
                "id": "top_hotspot",
                "label": "Hotspot principal",
                "mode": "fly_to",
                "default": not bool(presets),
                "center": {"lat": hotspot.get("lat"), "lng": hotspot.get("lng")},
                "zoom": 14,
                "bearing": 0,
                "pitch": 45,
                "target_cell_id": hotspot.get("id"),
                "reason_code": "highest_weight_cell",
            }
        )
    if geocoding_candidates:
        presets.append(
            {
                "id": "geocoding_queue",
                "label": "Direcciones pendientes",
                "mode": "queue_panel",
                "default": not bool(presets),
                "reason_code": "addresses_need_geocoding",
                "candidate_count": len(geocoding_candidates),
            }
        )
    if not presets:
        presets.append(
            {
                "id": "tenant_region_empty",
                "label": "Region sin coordenadas",
                "mode": "frontend_default_region",
                "default": True,
                "reason_code": quality.get("reason_code") or "no_points",
                "requires_frontend_default_center": True,
            }
        )

    return {
        "contract_version": "operations.heatmap_viewport_presets.v1",
        "default_preset_id": next((preset["id"] for preset in presets if preset.get("default")), presets[0]["id"]),
        "camera_constraints": {
            "min_zoom": 4,
            "max_zoom": 18,
            "fit_bounds_padding_px": 72,
            "prefer_reduced_motion": False,
        },
        "presets": presets,
    }


def _heatmap_layer_style_contract(
    *,
    ai_layers: dict[str, Any],
    category_layers: list[dict[str, Any]],
    quality: dict[str, Any],
) -> dict[str, Any]:
    style_tokens = _as_dict(ai_layers.get("style_tokens")) or {
        "risk": "#EF4444",
        "whatsapp": "#10B981",
        "survey": "#7C3AED",
        "attention": "#F59E0B",
        "neutral": "#2563EB",
    }
    return {
        "contract_version": "operations.heatmap_layer_styles.v1",
        "default_engine": "maplibre",
        "compatible_engines": ["maplibre", "deckgl", "google"],
        "quality_state": quality.get("state"),
        "style_tokens": style_tokens,
        "layers": [
            {
                "id": "base_heatmap",
                "source": "points",
                "geometry": "weighted_points",
                "default_visible": True,
                "style": {"type": "heatmap", "weight_field": "weight", "radius_px": 34, "intensity": 0.75},
                "interaction": {"hover": True, "click": "inspect_point"},
            },
            {
                "id": "hotspot_cells",
                "source": "cells",
                "geometry": "cell_centroids",
                "default_visible": True,
                "style": {"type": "circle", "color": style_tokens.get("attention"), "radius_field": "weight"},
                "interaction": {"hover": True, "click": "open_hotspot_actions"},
            },
            {
                "id": "category_layers",
                "source": "category_layers",
                "geometry": "grouped_points",
                "default_visible": bool(category_layers),
                "style": {"type": "category_heatmap", "max_categories": 12},
                "interaction": {"toggle": True, "filter_key": "category"},
            },
            {
                "id": "ai_risk_layers",
                "source": "ai_layers.layers.risk_pulses",
                "geometry": "animated_scatter",
                "default_visible": True,
                "style": {"type": "pulse", "color": style_tokens.get("risk"), "reduced_motion": "static_circle"},
                "interaction": {"click": "inspect_ai_signals"},
            },
            {
                "id": "whatsapp_activity",
                "source": "ai_layers.layers.whatsapp_activity",
                "geometry": "pulse_points",
                "default_visible": True,
                "style": {"type": "pulse", "color": style_tokens.get("whatsapp"), "reduced_motion": "static_circle"},
                "interaction": {"click": "filter_channel_whatsapp"},
            },
            {
                "id": "survey_participation",
                "source": "ai_layers.layers.survey_participation",
                "geometry": "territory_heat",
                "default_visible": True,
                "style": {"type": "heatmap", "color": style_tokens.get("survey")},
                "interaction": {"click": "open_survey_context"},
            },
            {
                "id": "geocoding_queue",
                "source": "geocoding.candidates",
                "geometry": "address_rows",
                "default_visible": quality.get("state") == "pending_geocode",
                "style": {"type": "queue_badge", "color": style_tokens.get("attention")},
                "interaction": {"click": "open_geocoding_queue"},
            },
        ],
        "legend_contract": {
            "color_mode": "category_source_quality",
            "show_quality_badge": True,
            "show_ai_badge": True,
            "show_geocoding_badge": True,
        },
    }


def _heatmap_hotspot_actions_contract(
    *,
    hotspots: list[dict[str, Any]],
    quality: dict[str, Any],
    geocoding_candidates: list[dict[str, Any]],
    ai_summary: dict[str, Any],
) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    for index, hotspot in enumerate(hotspots[:5]):
        actions.append(
            {
                "id": f"inspect_hotspot_{index + 1}",
                "label": "Inspeccionar hotspot",
                "priority": "high" if float(hotspot.get("weight") or 0) >= 1.4 else "medium",
                "action_type": "client_filter",
                "target": {"type": "cell", "cell_id": hotspot.get("id"), "lat": hotspot.get("lat"), "lng": hotspot.get("lng")},
                "ui_hint": "open_heatmap_cell",
                "writes_enabled": False,
            }
        )

    if geocoding_candidates:
        actions.append(
            {
                "id": "open_geocoding_queue",
                "label": "Completar coordenadas pendientes",
                "priority": "high",
                "action_type": "open_queue",
                "target": {"type": "geocoding_queue", "candidate_count": len(geocoding_candidates)},
                "ui_hint": "open_geocoding_queue",
                "writes_enabled": False,
            }
        )

    if ai_summary.get("requires_human_attention"):
        actions.append(
            {
                "id": "open_ai_risk_queue",
                "label": "Revisar senales AI de riesgo",
                "priority": "high",
                "action_type": "open_panel",
                "target": {"type": "ai_risk", "risk_level": ai_summary.get("risk_level")},
                "ui_hint": "open_ai_risk_layers",
                "writes_enabled": False,
            }
        )

    return {
        "contract_version": "operations.heatmap_hotspot_actions.v1",
        "safe_by_default": True,
        "writes_enabled": False,
        "quality_state": quality.get("state"),
        "actions": actions,
        "playbook": [
            {
                "id": "triage_high_density_zone",
                "label": "Priorizar zona de mayor concentracion",
                "trigger": "hotspots_present",
                "enabled": bool(hotspots),
                "steps": ["filter_top_cell", "inspect_records", "assign_owner_or_followup", "monitor_realtime_updates"],
                "confirmation_required_for_writes": True,
            },
            {
                "id": "recover_missing_geolocation",
                "label": "Recuperar ubicaciones faltantes",
                "trigger": "pending_geocode",
                "enabled": bool(geocoding_candidates),
                "steps": ["open_geocoding_queue", "validate_address", "use_update_location_action", "refresh_heatmap"],
                "confirmation_required_for_writes": True,
            },
            {
                "id": "watch_ai_risk_layer",
                "label": "Vigilar capa AI de riesgo",
                "trigger": "ai_requires_attention",
                "enabled": bool(ai_summary.get("requires_human_attention")),
                "steps": ["show_ai_risk_layers", "open_related_records", "escalate_if_confirmed"],
                "confirmation_required_for_writes": True,
            },
        ],
    }


def _heatmap_geocoding_guidance(
    *,
    geocoding_candidates: list[dict[str, Any]],
    location_quality: dict[str, Any],
    quality: dict[str, Any],
) -> dict[str, Any]:
    candidate_count = len(geocoding_candidates or [])
    return {
        "contract_version": "operations.heatmap_geocoding_guidance.v1",
        "state": "pending" if candidate_count else "clear",
        "reason_code": "addresses_need_coordinates" if candidate_count else "no_pending_addresses",
        "candidate_count": candidate_count,
        "coverage_percent": location_quality.get("coordinate_coverage_pct"),
        "quality_state": quality.get("state"),
        "backend_external_calls": "none",
        "queue_behavior": "show_candidates_and_require_user_or_batch_confirmation" if candidate_count else "hide_queue_or_show_success",
        "field_contract": {
            "record_id": "string",
            "record_source": "tenant_ticket|municipio_ticket|pyme_ticket",
            "address": "string",
            "actions": "array<open_record|update_location>",
            "location_endpoint": "use candidate.actions[id=update_location].endpoint",
            "latitud": "number",
            "longitud": "number",
        },
        "validation_rules": [
            "latitud must be between -90 and 90",
            "longitud must be between -180 and 180",
            "do not mutate records without user confirmation",
            "refresh /api/v2/analytics/operations/heatmap after update_location succeeds",
        ],
        "recommended_actions": [
            {
                "id": "open_geocoding_queue",
                "label": "Abrir cola de geocodificacion",
                "enabled": bool(candidate_count),
                "ui_hint": "open_geocoding_queue",
            },
            {
                "id": "request_whatsapp_location",
                "label": "Pedir ubicacion por WhatsApp",
                "enabled": True,
                "ui_hint": "open_template_or_live_chat",
            },
        ],
    }


def _heatmap_ai_status_contract(ai_insights: dict[str, Any], ai_layers: dict[str, Any]) -> dict[str, Any]:
    hf_status = _as_dict(ai_insights.get("hf_status"))
    frontend_contract = _as_dict(ai_insights.get("frontend_contract"))
    used_hf = bool(hf_status.get("used"))
    return {
        "contract_version": "operations.heatmap_ai_status.v1",
        "provider_family": ai_insights.get("provider_family") or "huggingface",
        "mode": ai_insights.get("mode") or ("huggingface_zero_shot" if used_hf else "deterministic_local_fallback"),
        "status": "hf_active" if used_hf else "local_fallback",
        "configured": bool(hf_status.get("configured")),
        "zero_shot_enabled": bool(hf_status.get("zero_shot_enabled")),
        "used_hf": used_hf,
        "fallback_reason": hf_status.get("fallback_reason"),
        "safe_to_render_without_hf_token": bool(frontend_contract.get("safe_to_render_without_hf_token", True)),
        "ai_layers_ready": bool((ai_layers.get("layers") or {}) if isinstance(ai_layers, dict) else {}),
        "map_layer_hints": (ai_insights.get("summary") or {}).get("map_layer_hints") or [],
        "requires_human_attention": bool((ai_insights.get("summary") or {}).get("requires_human_attention")),
    }


def _ai_items_from_heatmap(
    records: list[dict[str, Any]],
    points: list[dict[str, Any]],
    geocoding_candidates: list[dict[str, Any]],
    filters: dict[str, Any],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for record in records:
        if not _point_matches_filters(_record_to_filter_probe(record), filters):
            continue
        items.append(
            {
                "source": "ticket",
                "record_source": record.get("source"),
                "id": record.get("id"),
                "text": " ".join(
                    str(value)
                    for value in (
                        record.get("title"),
                        record.get("category"),
                        record.get("status"),
                        record.get("priority"),
                        record.get("channel"),
                        record.get("address"),
                    )
                    if value
                ),
                "category": record.get("category"),
                "channel": record.get("channel"),
                "status": record.get("status"),
                "address": record.get("address"),
            }
        )
    for point in points:
        items.append(
            {
                "source": point.get("source"),
                "id": point.get("id"),
                "text": " ".join(
                    str(value)
                    for value in (
                        point.get("label"),
                        point.get("category"),
                        point.get("status"),
                        point.get("channel"),
                    )
                    if value
                ),
                "category": point.get("category"),
                "channel": point.get("channel"),
                "status": point.get("status"),
                "lat": point.get("lat"),
                "lng": point.get("lng"),
            }
        )
    for candidate in geocoding_candidates:
        items.append(
            {
                "source": "pending_geocode",
                "id": candidate.get("id"),
                "text": " ".join(
                    str(value)
                    for value in (
                        candidate.get("label"),
                        candidate.get("category"),
                        candidate.get("status"),
                        candidate.get("channel"),
                        candidate.get("address"),
                    )
                    if value
                ),
                "category": candidate.get("category"),
                "channel": candidate.get("channel"),
                "status": candidate.get("status"),
                "address": candidate.get("address"),
            }
        )
    return items


def build_operational_heatmap(
    tenant: TenantProfile,
    start_date: datetime,
    end_date: datetime,
    *,
    ticket_records: list[dict[str, Any]] | None = None,
    max_points: int = 1000,
    segment_filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    records = ticket_records if ticket_records is not None else _collect_ticket_records(tenant, start_date, end_date)
    points: list[dict[str, Any]] = []
    filters = segment_filters or {}
    geocoding_candidates = [
        _geocoding_candidate(record)
        for record in records
        if not _record_has_coordinates(record)
        and record.get("address")
        and _point_matches_filters(_record_to_filter_probe(record), filters)
    ]

    for record in records:
        if record.get("lat") is None or record.get("lng") is None:
            continue
        demographics = _as_dict(record.get("demographics"))
        point = {
            "source": "ticket",
            "record_source": record["source"],
            "id": f"{record['source']}:{record['id']}",
            "lat": float(record["lat"]),
            "lng": float(record["lng"]),
            "weight": _priority_weight(record["priority"], record["status"]),
            "category": record["category"],
            "channel": record["channel"],
            "status": record["status"],
            "sla_state": record.get("sla_state") or "normal",
            "overdue": bool(record.get("overdue")),
            "assignee_id": record.get("assignee_id"),
            "zone": record.get("zone"),
            "address": record.get("address"),
            "label": record["title"],
            "timestamp": _iso(record.get("created_at")),
            "gender": demographics.get("gender") or "unknown",
            "age_range": demographics.get("age_range") or "unknown",
            "demographics_source": demographics.get("source") or "missing",
            "actions": _ticket_action_contract(record),
        }
        if _point_matches_filters(point, filters):
            points.append(point)

    survey_responses = _between(EncRespuesta.query.filter_by(tenant_id=tenant.id), EncRespuesta.submitted_at, start_date, end_date).filter(
        EncRespuesta.lat.isnot(None),
        EncRespuesta.lng.isnot(None),
    ).limit(max_points).all()
    for response in survey_responses:
        metadata = _as_dict(response.metadata_payload)
        demographics = _demographics_from_metadata(
            {
                **metadata,
                "genero": response.genero,
                "edad": response.edad,
                "rango_etario": response.rango_etario,
                "anio_nacimiento": response.anio_nacimiento,
            }
        )
        point = {
            "source": "survey",
            "record_source": "survey_response",
            "id": f"survey_response:{response.id}",
            "lat": float(response.lat),
            "lng": float(response.lng),
            "weight": 0.8,
            "category": _norm(metadata.get("categoria") or metadata.get("category"), "survey_response"),
            "channel": _norm(response.canal, "survey"),
            "status": "submitted",
            "label": response.barrio or response.ciudad or "Respuesta de encuesta",
            "timestamp": _iso(response.submitted_at),
            "gender": demographics.get("gender") or "unknown",
            "age_range": demographics.get("age_range") or "unknown",
            "demographics_source": demographics.get("source") or "missing",
        }
        if _point_matches_filters(point, filters):
            points.append(point)

    events = _between(AnalyticsEventV2.query.filter_by(tenant_id=tenant.id), AnalyticsEventV2.ts, start_date, end_date).filter(
        AnalyticsEventV2.lat.isnot(None),
        AnalyticsEventV2.lng.isnot(None),
    ).limit(max_points).all()
    for event in events:
        metadata = _as_dict(event.metadata_payload)
        demographics = _demographics_from_metadata(metadata)
        point = {
            "source": "analytics_event",
            "record_source": "analytics_event",
            "id": f"analytics_event:{event.id}",
            "lat": float(event.lat),
            "lng": float(event.lng),
            "weight": 0.6,
            "category": _norm(metadata.get("categoria") or metadata.get("category") or event.event_name, "event"),
            "channel": _norm(event.channel, "unknown"),
            "status": "event",
            "label": event.event_name,
            "timestamp": _iso(event.ts),
            "gender": demographics.get("gender") or "unknown",
            "age_range": demographics.get("age_range") or "unknown",
            "demographics_source": demographics.get("source") or "missing",
        }
        if _point_matches_filters(point, filters):
            points.append(point)

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
                "channels": Counter(),
                "genders": Counter(),
                "age_ranges": Counter(),
            },
        )
        cell["weight"] += float(point.get("weight") or 1.0)
        cell["count"] += 1
        cell["sources"][point["source"]] += 1
        cell["categories"][point["category"]] += 1
        cell["channels"][point.get("channel") or "unknown"] += 1
        cell["genders"][point.get("gender") or "unknown"] += 1
        cell["age_ranges"][point.get("age_range") or "unknown"] += 1

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
                "top_channels": _counter(cell["channels"], limit=5),
                "demographics": {
                    "gender": _segment_items(cell["genders"], limit=5),
                    "age_ranges": _segment_items(cell["age_ranges"], limit=5),
                },
            }
        )
    cell_items.sort(key=lambda item: (item["weight"], item["count"]), reverse=True)

    category_counter = Counter(point.get("category") or "unknown" for point in points)
    gender_counter = Counter(point.get("gender") or "unknown" for point in points)
    age_range_counter = Counter(point.get("age_range") or "unknown" for point in points)
    channel_counter = Counter(point.get("channel") or "unknown" for point in points)
    source_counter = Counter(point.get("source") or "unknown" for point in points)
    status_counter = Counter(point.get("status") or "unknown" for point in points)
    zone_counter = Counter(point.get("zone") or "unknown" for point in points)
    sla_counter = Counter(point.get("sla_state") or "normal" for point in points)

    category_layers = []
    for category, count in category_counter.most_common(20):
        category_points = [point for point in points if point.get("category") == category]
        category_layers.append(
            {
                "key": category,
                "label": category,
                "count": int(count),
                "weight": round(sum(float(point.get("weight") or 1.0) for point in category_points), 2),
                "points": category_points[:100],
            }
        )

    bounds = None
    if points:
        lats = [point["lat"] for point in points]
        lngs = [point["lng"] for point in points]
        bounds = {"north": max(lats), "south": min(lats), "east": max(lngs), "west": min(lngs)}

    normalized_filters = {
        key: sorted(_normalized_heatmap_filter_values(key, value))
        for key, value in filters.items()
        if _normalized_heatmap_filter_values(key, value)
    }
    points_with_gender = len([point for point in points if point.get("gender") not in (None, "unknown")])
    points_with_age = len([point for point in points if point.get("age_range") not in (None, "unknown")])
    location_quality = _location_quality(records, geocoding_candidates)
    quality = _heatmap_quality_contract(
        points=points,
        records=records,
        geocoding_candidates=geocoding_candidates,
        location_quality=location_quality,
        max_points=max_points,
    )
    realtime = _heatmap_realtime_contract(points)
    ai_insights = build_collection_ai_insights(
        _ai_items_from_heatmap(records, points, geocoding_candidates, filters),
        domain="operations",
    )
    ai_layers = build_map_ai_layers(points, category_layers=category_layers, insights=ai_insights)
    ai_summary = ai_insights.get("summary") or {}
    heatmap_summary = {
        "points": len(points),
        "cells": len(cell_items),
        "can_render_heatmap": bool(points),
        "ticket_points": len([point for point in points if point["source"] == "ticket"]),
        "survey_points": len([point for point in points if point["source"] == "survey"]),
        "event_points": len([point for point in points if point["source"] == "analytics_event"]),
        "points_with_gender": points_with_gender,
        "points_with_age": points_with_age,
        "unknown_gender_points": len(points) - points_with_gender,
        "unknown_age_points": len(points) - points_with_age,
        "filtered": bool(normalized_filters),
        "pending_geocode": len(geocoding_candidates),
        "coordinate_coverage_pct": location_quality.get("coordinate_coverage_pct"),
        "coverage_rate": quality.get("coverage_rate"),
        "coverage_percent": quality.get("coverage_percent"),
        "quality_state": quality.get("state"),
        "quality_reason_code": quality.get("reason_code"),
        "ai_risk_level": ai_summary.get("risk_level") or "normal",
        "dominant_intent": ai_summary.get("dominant_intent") or "general_query",
        "requires_human_attention": bool(ai_summary.get("requires_human_attention")),
    }
    hotspots = cell_items[:10]
    geocoding_guidance = _heatmap_geocoding_guidance(
        geocoding_candidates=geocoding_candidates,
        location_quality=location_quality,
        quality=quality,
    )
    hotspot_actions = _heatmap_hotspot_actions_contract(
        hotspots=hotspots,
        quality=quality,
        geocoding_candidates=geocoding_candidates,
        ai_summary=ai_summary,
    )
    viewport_presets = _heatmap_viewport_presets(
        bounds=bounds,
        hotspots=hotspots,
        quality=quality,
        geocoding_candidates=geocoding_candidates,
    )
    layer_style_contract = _heatmap_layer_style_contract(
        ai_layers=ai_layers,
        category_layers=category_layers,
        quality=quality,
    )
    map_narrative = _heatmap_narrative_contract(
        tenant,
        summary=heatmap_summary,
        quality=quality,
        category_layers=category_layers,
        geocoding_candidates=geocoding_candidates,
        ai_summary=ai_summary,
    )
    ai_status = _heatmap_ai_status_contract(ai_insights, ai_layers)

    return {
        "contract_version": "operations.heatmap.v1",
        "tenant": _tenant_ref(tenant),
        "period": {"from": _iso(start_date), "to": _iso(end_date)},
        "render_contract": {
            "state": "ready" if points else "empty",
            "can_render_heatmap": bool(points),
            "empty_reason": None if points else "no_real_geo_points",
            "map_engine": "maplibre",
            "layers": ["tickets", "surveys", "analytics_events", "ai_risk", "whatsapp_activity"],
            "point_format": {"lat": "number", "lng": "number", "weight": "number"},
            "segment_filters": ["categoria", "estado", "genero", "rango_edad", "source", "channel", "zona", "sla_state", "assignee_id"],
            "category_layers": True,
            "demographics_source": "metadata_fields_only",
            "address_geocoding": True,
            "geocoding_state": (location_quality.get("reason_code") or "unknown"),
            "quality_state": quality.get("state"),
            "quality_reason_code": quality.get("reason_code"),
            "recommended_views": [
                "heatmap",
                "category_layers",
                "demographic_segments",
                "geocoding_queue",
                "interactive_globe",
                "ai_risk_layers",
                "whatsapp_activity_layer",
                "survey_participation_layer",
            ],
            "premium_metadata": [
                "map_narrative",
                "viewport_presets",
                "layer_style_contract",
                "hotspot_actions",
                "geocoding.guidance",
                "ai_status",
            ],
        },
        "summary": heatmap_summary,
        "map_narrative": map_narrative,
        "viewport_presets": viewport_presets,
        "layer_style_contract": layer_style_contract,
        "hotspot_actions": hotspot_actions,
        "ai_status": ai_status,
        "ai_insights": ai_insights,
        "ai_layers": ai_layers,
        "map_experience": {
            "contract_version": "operations.map_experience.v1",
            "preferred_visualization": "interactive_globe_heatmap",
            "map_engines": ["maplibre", "deckgl", "google"],
            "layer_groups": ["base_heatmap", "category_layers", "ai_risk_layers", "whatsapp_activity", "survey_participation"],
            "empty_state_behavior": "show_geocoding_queue_and_ai_summary",
            "supports_reduced_motion": True,
            "narrative_contract": map_narrative.get("contract_version"),
            "viewport_contract": viewport_presets.get("contract_version"),
            "layer_style_contract": layer_style_contract.get("contract_version"),
            "hotspot_actions_contract": hotspot_actions.get("contract_version"),
            "ai_status_contract": ai_status.get("contract_version"),
        },
        "quality": quality,
        "realtime": realtime,
        "legend": {
            "mode": "category_source_quality",
            "categories": [
                {"key": item["key"], "label": item["label"], "count": item["count"]}
                for item in category_layers[:12]
            ],
            "sources": _segment_items(source_counter),
            "quality": {
                "state": quality.get("state"),
                "coverage_percent": quality.get("coverage_percent"),
            },
        },
        "applied_filters": normalized_filters,
        "segments": {
            "category": _segment_items(category_counter),
            "gender": _segment_items(gender_counter),
            "age_range": _segment_items(age_range_counter),
            "channel": _segment_items(channel_counter),
            "source": _segment_items(source_counter),
            "status": _segment_items(status_counter),
            "zone": _segment_items(zone_counter),
            "sla_state": _segment_items(sla_counter),
        },
        "demographics": {
            "source": "real_metadata_only",
            "gender": _segment_items(gender_counter),
            "age_ranges": _segment_items(age_range_counter),
            "known_gender_points": points_with_gender,
            "known_age_points": points_with_age,
            "unknown_gender_points": len(points) - points_with_gender,
            "unknown_age_points": len(points) - points_with_age,
        },
        "location_quality": location_quality,
        "geocoding": {
            "contract_version": "operations.heatmap.geocoding_queue.v1",
            "status": "pending" if geocoding_candidates else "empty",
            "reason_code": "address_without_coordinates" if geocoding_candidates else "no_pending_addresses",
            "candidate_count": len(geocoding_candidates),
            "candidates": geocoding_candidates[:50],
            "guidance": geocoding_guidance,
            "recommended_action": {
                "action_id": "geocode_ticket_addresses",
                "label": "Geocodificar direcciones pendientes",
                "method": "dynamic",
                "endpoint_template": "use candidates[].actions[id=update_location].endpoint",
                "requires": ["latitud|location.lat", "longitud|location.lng"],
                "body_template": "use candidates[].actions[id=update_location].body_template",
            },
        },
        "category_layers": category_layers,
        "bounds": bounds,
        "points": points,
        "cells": cell_items,
        "hotspots": hotspots,
        "ui": {
            "labels": {
                "map_quality": "Calidad del mapa",
                "coverage": "Cobertura GPS",
                "visible_points": "Puntos visibles",
                "pending_geocode": "Pendientes de geocodificar",
                "realtime": "Actualizacion en vivo",
                "quality_ready": "Mapa operativo confiable",
                "quality_partial": "Cobertura territorial parcial",
                "quality_pending_geocode": "Direcciones pendientes de geocodificar",
                "quality_blocked": "Sin coordenadas reales",
                "quality_empty": "Sin eventos para el periodo",
            }
        },
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


def _build_ai_operational_brief(
    *,
    summary: dict[str, Any],
    alerts: list[dict[str, Any]],
    next_best_actions: list[dict[str, Any]],
    heatmap: dict[str, Any],
) -> dict[str, Any]:
    ai_summary = ((heatmap.get("ai_insights") or {}).get("summary") or {}) if isinstance(heatmap, dict) else {}
    high_alerts = [item for item in alerts if item.get("severity") == "high"]
    medium_alerts = [item for item in alerts if item.get("severity") == "medium"]
    top_action = (next_best_actions or [{}])[0] or {}
    risk_level = ai_summary.get("risk_level") or ("high" if high_alerts else "medium" if medium_alerts else "normal")
    if high_alerts or risk_level in {"critical", "high"}:
        severity = "high"
        headline = "Atencion operativa prioritaria"
    elif medium_alerts or risk_level == "medium":
        severity = "medium"
        headline = "Actividad para monitoreo cercano"
    else:
        severity = "low"
        headline = "Operacion estable"

    focus_items: list[dict[str, Any]] = []
    if summary.get("overdue_tickets", 0):
        focus_items.append(
            {
                "id": "overdue_tickets",
                "label": "Reclamos vencidos",
                "value": int(summary.get("overdue_tickets") or 0),
                "priority": "high",
                "ui_hint": "open_overdue_queue",
            }
        )
    if summary.get("heatmap_points", 0):
        focus_items.append(
            {
                "id": "heatmap_points",
                "label": "Puntos en mapa operativo",
                "value": int(summary.get("heatmap_points") or 0),
                "priority": "medium",
                "ui_hint": "open_heatmap",
            }
        )
    if summary.get("survey_responses", 0):
        focus_items.append(
            {
                "id": "survey_responses",
                "label": "Participacion en encuestas",
                "value": int(summary.get("survey_responses") or 0),
                "priority": "medium",
                "ui_hint": "open_survey_monitor",
            }
        )
    if summary.get("whatsapp_messages", 0):
        focus_items.append(
            {
                "id": "whatsapp_messages",
                "label": "Actividad WhatsApp",
                "value": int(summary.get("whatsapp_messages") or 0),
                "priority": "medium",
                "ui_hint": "open_whatsapp_inbox",
            }
        )

    if not focus_items:
        focus_items.append(
            {
                "id": "monitoring",
                "label": "Monitoreo operativo",
                "value": 0,
                "priority": "low",
                "ui_hint": "open_dashboard",
            }
        )

    dominant_intent_label = ai_summary.get("dominant_intent_label") or "consulta general"
    sentiment = ai_summary.get("sentiment") or "neutral"
    recommendation = top_action.get("title") or "Mantener monitoreo operativo"
    narrative = (
        f"{headline}: {recommendation}. "
        f"Senal dominante: {dominant_intent_label}; sentimiento: {sentiment}."
    )

    return {
        "contract_version": "operations.ai_brief.v1",
        "severity": severity,
        "headline": headline,
        "narrative": narrative,
        "risk_level": risk_level,
        "dominant_intent": ai_summary.get("dominant_intent") or "general_query",
        "dominant_intent_label": dominant_intent_label,
        "sentiment": sentiment,
        "requires_human_attention": bool(ai_summary.get("requires_human_attention") or high_alerts),
        "requires_location_focus": bool(ai_summary.get("requires_location_focus")),
        "top_action": top_action,
        "focus_items": focus_items[:4],
        "signals": {
            "alerts": len(alerts),
            "high_alerts": len(high_alerts),
            "medium_alerts": len(medium_alerts),
            "actions": len(next_best_actions or []),
            "hf_mode": (heatmap.get("ai_insights") or {}).get("mode"),
            "hf_configured": (((heatmap.get("ai_insights") or {}).get("hf_status") or {}).get("configured")),
        },
        "frontend_contract": {
            "render_as": "operations_ai_brief",
            "recommended_widgets": ["priority_banner", "focus_cards", "ai_signal_badges", "next_best_action"],
            "refresh_seconds": 30,
            "safe_empty_state": "show_monitoring_ok",
        },
    }


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
    ai_brief = _build_ai_operational_brief(
        summary=summary,
        alerts=alerts,
        next_best_actions=next_best_actions,
        heatmap=heatmap,
    )

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
                "ai_insights": heatmap.get("ai_insights"),
                "ai_layers": heatmap.get("ai_layers"),
                "map_experience": heatmap.get("map_experience"),
                "location_quality": heatmap.get("location_quality"),
                "geocoding": {
                    "candidate_count": ((heatmap.get("geocoding") or {}).get("candidate_count") or 0),
                    "reason_code": ((heatmap.get("geocoding") or {}).get("reason_code") or "no_pending_addresses"),
                },
            }
        },
        "alerts": alerts,
        "next_best_actions": next_best_actions,
        "ai_brief": ai_brief,
        "frontend_contract": {
            "recommended_views": [
                "executive_summary",
                "ticket_board",
                "live_chat_inbox",
                "survey_vote_monitor",
                "heatmap",
                "interactive_globe",
                "ai_risk_layers",
                "employee_coverage",
                "action_center",
            ],
            "primary_refresh_seconds": 30,
            "empty_state_behavior": "show_contract_empty_state",
            "map_layers": ["tickets", "surveys", "analytics_events", "ai_risk", "whatsapp_activity", "survey_participation"],
            "exports": {
                "pdf": "/api/v2/analytics/operations/export.pdf",
                "ai_summary": "/api/v2/analytics/operations/executive-summary",
                "heatmap": "/api/v2/analytics/operations/heatmap",
                "freshness": "/api/v2/analytics/operations/freshness",
                "ai_brief": "/api/v2/analytics/operations/ai-brief",
            },
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
        "ai_brief": dashboard.get("ai_brief") or {},
        "trends": dashboard.get("trends") or {},
        "frontend_contract": {
            "render_as": "action_center",
            "primary_refresh_seconds": 30,
            "empty_state_behavior": "show_monitoring_ok",
        },
    }
