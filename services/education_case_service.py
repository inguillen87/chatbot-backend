from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from database import db
from models import MunicipioTicket, PymeTicket, TenantProfile, User
from models_education import Campus, Guardian, School, SchoolCaseAlias, StudentGuardianRelation
from services.education_contracts import education_case_taxonomy, fold_text


EDUCATION_CASE_ALIAS_CONTRACT_VERSION = "education.case_alias.v1"
EDUCATION_OPERATIONS_SUMMARY_CONTRACT_VERSION = "education.operations_summary.v1"
EDUCATION_OPERATIONS_HEATMAP_CONTRACT_VERSION = "education.operations_heatmap.v1"

_CLOSED_STATES = {"cerrado", "resuelto", "completado", "finalizado", "cancelado", "archivado"}
_SENSITIVE_CASE_TYPES = {"convivencia", "cobranza", "transporte", "comedor"}


def _digits(value: Any) -> str:
    return "".join(char for char in str(value or "") if char.isdigit())


def _normalize_case_type(case_type: Any) -> str:
    normalized = fold_text(case_type).replace(" ", "_")
    taxonomy_keys = {item["key"] for item in education_case_taxonomy()}
    return normalized if normalized in taxonomy_keys else "secretaria"


def _main_campus_for_school(school_id: int | None) -> Campus | None:
    if not school_id:
        return None
    return (
        Campus.query.filter_by(school_id=school_id, is_main=True).order_by(Campus.id.asc()).first()
        or Campus.query.filter_by(school_id=school_id).order_by(Campus.id.asc()).first()
    )


def _guardian_from_phone(tenant_id: int, phone: Any) -> Guardian | None:
    phone_digits = _digits(phone)
    if not phone_digits:
        return None
    for guardian in Guardian.query.filter_by(tenant_id=tenant_id).order_by(Guardian.id.asc()).all():
        guardian_digits = _digits(guardian.phone_number)
        if guardian_digits and (
            guardian_digits == phone_digits
            or guardian_digits.endswith(phone_digits)
            or phone_digits.endswith(guardian_digits)
        ):
            return guardian
    return None


def _guardian_from_user_or_phone(
    tenant_id: int,
    *,
    end_user: User | None = None,
    phone: Any = None,
) -> Guardian | None:
    user_id = getattr(end_user, "id", None)
    if user_id:
        guardian = Guardian.query.filter_by(tenant_id=tenant_id, user_id=user_id).order_by(Guardian.id.asc()).first()
        if guardian:
            return guardian

    contact_phone = phone or getattr(end_user, "telefono", None) or getattr(end_user, "phone", None)
    return _guardian_from_phone(tenant_id, contact_phone)


def resolve_default_school_case_context(
    tenant_profile: TenantProfile | int | None,
    *,
    end_user: User | None = None,
    phone: Any = None,
    school_id: int | None = None,
    campus_id: int | None = None,
    section_id: int | None = None,
    student_id: int | None = None,
    guardian_id: int | None = None,
) -> dict[str, Any]:
    tenant_id = tenant_profile if isinstance(tenant_profile, int) else getattr(tenant_profile, "id", None)
    if not tenant_id:
        return {"resolved": False, "reason_code": "tenant_required"}

    guardian = Guardian.query.filter_by(id=guardian_id, tenant_id=tenant_id).first() if guardian_id else None
    guardian = guardian or _guardian_from_user_or_phone(tenant_id, end_user=end_user, phone=phone)

    relation = None
    student = None
    if guardian:
        relation = (
            StudentGuardianRelation.query.filter_by(guardian_id=guardian.id, status="active")
            .order_by(StudentGuardianRelation.is_primary.desc(), StudentGuardianRelation.id.asc())
            .first()
        )
        student = getattr(relation, "student", None) if relation else None

    school = School.query.filter_by(id=school_id, tenant_id=tenant_id).first() if school_id else None
    if not school and student:
        school = student.school
    if not school and guardian and guardian.school_id:
        school = School.query.filter_by(id=guardian.school_id, tenant_id=tenant_id).first()
    if not school:
        school = (
            School.query.filter_by(tenant_id=tenant_id, status="active").order_by(School.id.asc()).first()
            or School.query.filter_by(tenant_id=tenant_id).order_by(School.id.asc()).first()
        )
    if not school:
        return {"resolved": False, "reason_code": "school_not_configured", "tenant_id": tenant_id}

    campus = Campus.query.filter_by(id=campus_id, school_id=school.id).first() if campus_id else None
    campus = campus or getattr(student, "campus", None) or _main_campus_for_school(school.id)

    return {
        "resolved": True,
        "tenant_id": tenant_id,
        "school_id": school.id,
        "campus_id": getattr(campus, "id", None),
        "section_id": section_id or getattr(student, "section_id", None),
        "student_id": student_id or getattr(student, "id", None),
        "guardian_id": getattr(guardian, "id", None),
        "resolution_source": "guardian" if guardian else "default_school",
    }


def create_school_case_alias_for_ticket(
    *,
    tenant_profile: TenantProfile | int | None,
    ticket_type: str,
    ticket_id: int | str | None,
    case_type: str | None,
    channel: str = "api",
    end_user: User | None = None,
    phone: Any = None,
    sensitivity_level: str | None = None,
    school_id: int | None = None,
    campus_id: int | None = None,
    section_id: int | None = None,
    student_id: int | None = None,
    guardian_id: int | None = None,
) -> SchoolCaseAlias | None:
    tenant_id = tenant_profile if isinstance(tenant_profile, int) else getattr(tenant_profile, "id", None)
    try:
        parsed_ticket_id = int(ticket_id) if ticket_id is not None else None
    except (TypeError, ValueError):
        parsed_ticket_id = None
    if not tenant_id or not parsed_ticket_id or ticket_type not in {"pyme", "municipio"}:
        return None

    existing = SchoolCaseAlias.query.filter_by(ticket_type=ticket_type, ticket_id=parsed_ticket_id).first()
    if existing:
        return existing if existing.tenant_id == tenant_id else None

    normalized_case_type = _normalize_case_type(case_type)
    resolved = resolve_default_school_case_context(
        tenant_id,
        end_user=end_user,
        phone=phone,
        school_id=school_id,
        campus_id=campus_id,
        section_id=section_id,
        student_id=student_id,
        guardian_id=guardian_id,
    )
    if not resolved.get("resolved"):
        return None

    normalized_sensitivity = fold_text(sensitivity_level) or None
    if not normalized_sensitivity:
        normalized_sensitivity = "sensitive" if normalized_case_type in _SENSITIVE_CASE_TYPES else "internal"

    alias = SchoolCaseAlias(
        tenant_id=tenant_id,
        school_id=resolved["school_id"],
        campus_id=resolved.get("campus_id"),
        section_id=resolved.get("section_id"),
        student_id=resolved.get("student_id"),
        guardian_id=resolved.get("guardian_id"),
        case_type=normalized_case_type,
        sensitivity_level=normalized_sensitivity,
        channel=fold_text(channel) or "api",
        ticket_type=ticket_type,
        ticket_id=parsed_ticket_id,
    )
    db.session.add(alias)
    db.session.commit()
    return alias


def school_case_alias_payload(alias: SchoolCaseAlias | None) -> dict[str, Any] | None:
    if not alias:
        return None
    return {
        "contract_version": EDUCATION_CASE_ALIAS_CONTRACT_VERSION,
        "school_case_id": alias.id,
        "tenant_id": alias.tenant_id,
        "school_id": alias.school_id,
        "campus_id": alias.campus_id,
        "section_id": alias.section_id,
        "student_id": alias.student_id,
        "guardian_id": alias.guardian_id,
        "case_type": alias.case_type,
        "sensitivity_level": alias.sensitivity_level,
        "channel": alias.channel,
        "ticket_type": alias.ticket_type,
        "ticket_id": alias.ticket_id,
    }


def _ticket_for_alias(alias: SchoolCaseAlias):
    if alias.ticket_type == "pyme":
        return PymeTicket.query.get(alias.ticket_id)
    return MunicipioTicket.query.get(alias.ticket_id)


def _ticket_status(ticket: Any) -> str:
    return fold_text(getattr(ticket, "estado", None)) or "desconocido"


def _has_attachments(ticket: Any) -> bool:
    archivos = getattr(ticket, "archivos", None)
    if archivos is None:
        return False
    try:
        return archivos.count() > 0
    except Exception:
        try:
            return len(archivos) > 0
        except Exception:
            return False


def build_education_operations_summary(tenant_profile: TenantProfile | int | None) -> dict[str, Any]:
    tenant_id = tenant_profile if isinstance(tenant_profile, int) else getattr(tenant_profile, "id", None)
    tenant = tenant_profile if isinstance(tenant_profile, TenantProfile) else TenantProfile.query.get(tenant_id)
    if not tenant_id:
        return {
            "contract_version": EDUCATION_OPERATIONS_SUMMARY_CONTRACT_VERSION,
            "status": "error",
            "reason_code": "tenant_required",
            "summary": {},
            "next_best_actions": [],
        }

    aliases = SchoolCaseAlias.query.filter_by(tenant_id=tenant_id).order_by(SchoolCaseAlias.created_at.desc()).all()
    schools_count = School.query.filter_by(tenant_id=tenant_id).count()
    today = datetime.now(timezone.utc).date()

    by_type: Counter[str] = Counter()
    by_channel: Counter[str] = Counter()
    by_status: Counter[str] = Counter()
    by_school: Counter[str] = Counter()

    open_cases = 0
    waiting_assignment = 0
    sensitive_open_cases = 0
    cases_today = 0
    cases_with_location = 0
    cases_with_attachments = 0

    for alias in aliases:
        ticket = _ticket_for_alias(alias)
        status = _ticket_status(ticket)
        is_open = status not in _CLOSED_STATES
        by_type[alias.case_type or "sin_tipo"] += 1
        by_channel[alias.channel or "api"] += 1
        by_status[status] += 1
        by_school[getattr(alias.school, "name", None) or str(alias.school_id)] += 1

        created_at = alias.created_at
        if created_at and created_at.date() == today:
            cases_today += 1
        if is_open:
            open_cases += 1
            if not getattr(ticket, "asignado_a_id", None):
                waiting_assignment += 1
            if alias.sensitivity_level in {"sensitive", "critical"} or alias.case_type == "convivencia":
                sensitive_open_cases += 1
        if ticket and getattr(ticket, "latitud", None) is not None and getattr(ticket, "longitud", None) is not None:
            cases_with_location += 1
        if ticket and _has_attachments(ticket):
            cases_with_attachments += 1

    next_best_actions: list[dict[str, Any]] = []
    if schools_count == 0:
        next_best_actions.append(
            {
                "id": "setup_school_structure",
                "label": "Configurar colegio, sedes y cursos",
                "priority": "high",
                "endpoint": "/api/v1/education/schools",
            }
        )
    if sensitive_open_cases:
        next_best_actions.append(
            {
                "id": "review_sensitive_cases",
                "label": "Revisar casos sensibles abiertos",
                "priority": "high",
                "endpoint": "/api/v1/education/cases?sensitivity_level=sensitive&envelope=1",
            }
        )
    if waiting_assignment:
        next_best_actions.append(
            {
                "id": "assign_open_cases",
                "label": "Asignar casos escolares sin responsable",
                "priority": "medium",
                "endpoint": "/api/v1/education/cases?unassigned=1&envelope=1",
            }
        )
    if cases_with_location:
        next_best_actions.append(
            {
                "id": "inspect_school_case_heatmap",
                "label": "Revisar mapa de casos escolares",
                "priority": "low",
                "endpoint": "/api/v1/education/operations/heatmap",
            }
        )

    return {
        "contract_version": EDUCATION_OPERATIONS_SUMMARY_CONTRACT_VERSION,
        "status": "ok",
        "tenant_id": tenant_id,
        "tenant_slug": getattr(tenant, "slug", None),
        "summary": {
            "schools": schools_count,
            "total_cases": len(aliases),
            "open_cases": open_cases,
            "waiting_assignment": waiting_assignment,
            "sensitive_open_cases": sensitive_open_cases,
            "cases_today": cases_today,
            "cases_with_location": cases_with_location,
            "cases_with_attachments": cases_with_attachments,
        },
        "breakdown": {
            "by_type": dict(by_type),
            "by_channel": dict(by_channel),
            "by_status": dict(by_status),
            "by_school": dict(by_school),
        },
        "next_best_actions": next_best_actions,
        "endpoints": {
            "cases": "/api/v1/education/cases",
            "heatmap": "/api/v1/education/operations/heatmap",
            "admin_menu": "/api/v1/education/admin/menu",
            "whatsapp_playbook": "/api/v1/education/whatsapp/playbook",
        },
    }


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_education_operations_heatmap(
    tenant_profile: TenantProfile | int | None,
    *,
    case_type: str | None = None,
    channel: str | None = None,
    sensitivity_level: str | None = None,
    school_id: int | None = None,
    max_points: int = 500,
) -> dict[str, Any]:
    tenant_id = tenant_profile if isinstance(tenant_profile, int) else getattr(tenant_profile, "id", None)
    tenant = tenant_profile if isinstance(tenant_profile, TenantProfile) else TenantProfile.query.get(tenant_id)
    if not tenant_id:
        return {
            "contract_version": EDUCATION_OPERATIONS_HEATMAP_CONTRACT_VERSION,
            "status": "error",
            "reason_code": "tenant_required",
            "summary": {"points": 0, "cells": 0},
            "points": [],
            "cells": [],
            "hotspots": [],
        }

    query = SchoolCaseAlias.query.filter_by(tenant_id=tenant_id)
    if school_id:
        query = query.filter(SchoolCaseAlias.school_id == school_id)
    normalized_type = _normalize_case_type(case_type) if case_type else None
    if normalized_type:
        query = query.filter(SchoolCaseAlias.case_type == normalized_type)
    normalized_channel = fold_text(channel)
    if normalized_channel:
        query = query.filter(SchoolCaseAlias.channel == normalized_channel)
    normalized_sensitivity = fold_text(sensitivity_level)
    if normalized_sensitivity:
        query = query.filter(SchoolCaseAlias.sensitivity_level == normalized_sensitivity)

    points: list[dict[str, Any]] = []
    aliases = query.order_by(SchoolCaseAlias.created_at.desc()).limit(max_points * 3).all()
    for alias in aliases:
        ticket = _ticket_for_alias(alias)
        lat = _float_or_none(getattr(ticket, "latitud", None))
        lng = _float_or_none(getattr(ticket, "longitud", None))
        if lat is None or lng is None:
            continue
        status = _ticket_status(ticket)
        weight = 2.0 if alias.sensitivity_level in {"sensitive", "critical"} else 1.0
        if status not in _CLOSED_STATES:
            weight += 0.5
        points.append(
            {
                "id": f"school_case:{alias.id}",
                "school_case_id": alias.id,
                "ticket": {"type": alias.ticket_type, "id": alias.ticket_id, "status": status},
                "lat": lat,
                "lng": lng,
                "weight": weight,
                "source": "education_case",
                "case_type": alias.case_type,
                "sensitivity_level": alias.sensitivity_level,
                "channel": alias.channel,
                "school_id": alias.school_id,
                "campus_id": alias.campus_id,
                "student_id": alias.student_id,
                "guardian_id": alias.guardian_id,
                "timestamp": alias.created_at.isoformat() if alias.created_at else None,
                "label": getattr(alias.school, "name", None) or "Caso escolar",
            }
        )
        if len(points) >= max_points:
            break

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
                "case_types": Counter(),
                "channels": Counter(),
                "sensitivity": Counter(),
            },
        )
        cell["weight"] += point["weight"]
        cell["count"] += 1
        cell["case_types"][point["case_type"]] += 1
        cell["channels"][point["channel"]] += 1
        cell["sensitivity"][point["sensitivity_level"]] += 1

    cell_items = [
        {
            "id": cell["id"],
            "lat": cell["lat"],
            "lng": cell["lng"],
            "weight": round(cell["weight"], 2),
            "count": cell["count"],
            "top_case_types": dict(cell["case_types"].most_common(5)),
            "channels": dict(cell["channels"].most_common(5)),
            "sensitivity": dict(cell["sensitivity"].most_common(5)),
        }
        for cell in cells.values()
    ]
    cell_items.sort(key=lambda item: (item["weight"], item["count"]), reverse=True)

    bounds = None
    if points:
        lats = [point["lat"] for point in points]
        lngs = [point["lng"] for point in points]
        bounds = {
            "north": max(lats),
            "south": min(lats),
            "east": max(lngs),
            "west": min(lngs),
        }

    return {
        "contract_version": EDUCATION_OPERATIONS_HEATMAP_CONTRACT_VERSION,
        "status": "ok",
        "tenant_id": tenant_id,
        "tenant_slug": getattr(tenant, "slug", None),
        "render_contract": {
            "state": "ready" if points else "empty",
            "map_engine": "maplibre",
            "layers": ["education_cases"],
            "point_format": {"lat": "number", "lng": "number", "weight": "number"},
            "empty_state_behavior": "show_school_case_geo_empty",
        },
        "summary": {
            "points": len(points),
            "cells": len(cell_items),
            "open_points": len([point for point in points if point["ticket"]["status"] not in _CLOSED_STATES]),
            "sensitive_points": len([point for point in points if point["sensitivity_level"] in {"sensitive", "critical"}]),
        },
        "filters": {
            "school_id": school_id,
            "case_type": normalized_type,
            "channel": normalized_channel or None,
            "sensitivity_level": normalized_sensitivity or None,
        },
        "bounds": bounds,
        "points": points,
        "cells": cell_items,
        "hotspots": cell_items[:10],
    }
