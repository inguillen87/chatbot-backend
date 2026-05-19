from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_

from models import CatalogoItem, CategoriaTicket, MunicipioTicket, PymeTicket, TenantProfile, TenantTicket, User
from services.categorias_municipio import CATEGORIAS_RECLAMO
from services.education_contracts import education_case_taxonomy, is_education_tenant


EMPLOYEE_ROUTING_CONTRACT_VERSION = "employee.routing.v1"

_CLOSED_STATES = {"resuelto", "cerrado", "closed", "resolved", "entregado", "completed", "completado"}
_DEFAULT_OPERATIONAL_CHANNELS = ("web", "whatsapp")
_COMMON_SINGLE_WORD_CATEGORIES = {
    "alumbrado",
    "arbolado",
    "bacheo",
    "cloacas",
    "incendio",
    "limpieza",
    "luminaria",
    "luminarias",
    "otros",
    "sugerencia",
    "general",
}
_LOW_CONFIDENCE_NAME_SUFFIXES = ("ito", "ita", "cito", "cita")
_NOISE_CATEGORY_PREFIXES = (
    "hola",
    "quiero",
    "quisiera",
    "necesito",
    "codigo",
    "código",
    "17049",
)


def category_label_key(value: Any) -> str:
    return str(value or "").strip().lower()


def is_valid_employee_category_label(
    value: Any,
    *,
    known_categories: set[str] | None = None,
) -> bool:
    """Return whether a label is safe to expose as an employee routing category."""

    text = category_label_key(value)
    if not text:
        return False
    if known_categories is not None and text in known_categories:
        return True
    if len(text) > 80 or "@" in text or "http://" in text or "https://" in text:
        return False
    if any(text.startswith(prefix) for prefix in _NOISE_CATEGORY_PREFIXES):
        return False
    if any(char.isdigit() for char in text) and len(text.split()) > 3:
        return False
    if (
        len(text.split()) == 1
        and text not in _COMMON_SINGLE_WORD_CATEGORIES
        and text.endswith(_LOW_CONFIDENCE_NAME_SUFFIXES)
    ):
        return False
    return True


def filter_employee_category_labels(
    values: Any,
    *,
    known_categories: set[str] | None = None,
    limit: int = 80,
) -> list[str]:
    normalized = normalize_scope_list(values, limit=limit * 2)
    filtered: list[str] = []
    seen: set[str] = set()
    for value in normalized:
        key = category_label_key(value)
        if key in seen or not is_valid_employee_category_label(key, known_categories=known_categories):
            continue
        filtered.append(key)
        seen.add(key)
        if len(filtered) >= limit:
            break
    return filtered


def normalize_scope_list(values: Any, *, limit: int = 30) -> list[str]:
    if isinstance(values, str):
        values = [item.strip() for item in values.split(",")]
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
        if len(result) >= limit:
            break
    return result


def employee_scope(emp: User) -> dict[str, list[str]]:
    data = emp.accesibilidad if isinstance(emp.accesibilidad, dict) else {}
    scope = data.get("employee_scope") if isinstance(data.get("employee_scope"), dict) else {}
    return {
        "categorias": normalize_scope_list(scope.get("categorias")),
        "zonas": normalize_scope_list(scope.get("zonas")),
        "permisos": normalize_scope_list(scope.get("permisos")),
        "channels": normalize_scope_list(scope.get("channels")),
    }


def employee_ref(emp: User) -> dict[str, Any]:
    return {
        "id": emp.id,
        "employee_id": emp.id,
        "name": emp.name,
        "email": emp.email,
        "role": emp.rol,
        "tenant_slug": emp.tenant_slug,
        "scope": employee_scope(emp),
    }


def tenant_ref(tenant: TenantProfile) -> dict[str, Any]:
    return {
        "id": tenant.id,
        "slug": tenant.slug,
        "nombre": tenant.nombre,
        "tipo": tenant.tipo,
        "vertical": tenant.vertical,
        "subvertical": tenant.subvertical,
    }


def _tenant_config(tenant: TenantProfile) -> dict[str, Any]:
    return tenant.configuracion if isinstance(getattr(tenant, "configuracion", None), dict) else {}


def _append_config_values(target: set[str], cfg: dict[str, Any], *keys: str) -> None:
    for key in keys:
        values = cfg.get(key)
        if isinstance(values, dict):
            values = values.get("items") or values.get("values") or values.get("list")
        if isinstance(values, str):
            values = [item.strip() for item in values.split(",")]
        if not isinstance(values, list):
            continue
        target.update(normalize_scope_list(values, limit=80))


def tenant_operational_dimensions(tenant: TenantProfile, ticket_snapshots: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return real/configured dimensions useful for employee routing screens."""

    cfg = _tenant_config(tenant)
    routing_cfg = cfg.get("employee_routing") if isinstance(cfg.get("employee_routing"), dict) else {}
    categories: set[str] = set()
    ticket_categories: set[str] = set()
    zones: set[str] = set()
    channels: set[str] = set()
    sources: dict[str, list[str]] = {"categorias": [], "zonas": [], "channels": []}

    for item in ticket_snapshots or []:
        category = _norm(item.get("category"), "")
        zone = _norm(item.get("zone"), "")
        channel = _norm(item.get("channel"), "")
        if category and category != "sin_categoria":
            ticket_categories.add(category)
        if zone and zone != "sin_zona":
            zones.add(zone)
        if channel:
            channels.add(channel)
    if ticket_snapshots:
        sources["categorias"].append("open_tickets")
        sources["zonas"].append("open_tickets")
        sources["channels"].append("open_tickets")

    persisted_categories = [
        str(row.nombre or "").strip().lower()
        for row in CategoriaTicket.query.filter_by(tenant_id=tenant.id).all()
        if str(row.nombre or "").strip()
    ]
    if persisted_categories:
        categories.update(persisted_categories)
        sources["categorias"].append("categorias_ticket")

    _append_config_values(
        categories,
        routing_cfg,
        "categorias",
        "categories",
        "ticket_categories",
        "default_ticket_categories",
    )
    _append_config_values(
        categories,
        cfg,
        "employee_categories",
        "ticket_categories",
        "categorias_ticket",
        "default_ticket_categories",
    )
    if routing_cfg or cfg:
        sources["categorias"].append("tenant_config")

    if not categories and is_education_tenant(tenant):
        categories.update(normalize_scope_list([item["key"] for item in education_case_taxonomy()]))
        sources["categorias"].append("education_taxonomy")

    if not categories and str(tenant.tipo or "").lower() in {"municipio", "gobierno"}:
        categories.update(normalize_scope_list(list(CATEGORIAS_RECLAMO), limit=80))
        sources["categorias"].append("municipio_baseline_taxonomy")

    if not categories and str(tenant.tipo or "").lower() in {"pyme", "empresa", "commerce"}:
        owner_id = getattr(tenant, "pyme_id", None) or getattr(tenant, "municipio_id", None)
        query = CatalogoItem.query.filter(CatalogoItem.categoria.isnot(None))
        if tenant.id:
            query = query.filter((CatalogoItem.tenant_id == tenant.id) | (CatalogoItem.user_id == owner_id))
        catalog_categories = [str(row[0] or "").strip().lower() for row in query.with_entities(CatalogoItem.categoria).distinct().limit(80).all()]
        categories.update(normalize_scope_list(catalog_categories, limit=80))
        if catalog_categories:
            sources["categorias"].append("catalog_categories")

    known_categories = {category_label_key(item) for item in categories if category_label_key(item)}
    if ticket_categories:
        categories.update(filter_employee_category_labels(ticket_categories, known_categories=known_categories or None))
        sources["categorias"].append("open_tickets")

    _append_config_values(zones, routing_cfg, "zonas", "zones", "barrios", "districts", "operational_zones")
    _append_config_values(zones, cfg, "zonas", "zones", "barrios", "districts", "operational_zones")
    if zones:
        sources["zonas"].append("tenant_config")

    _append_config_values(channels, routing_cfg, "channels", "canales")
    _append_config_values(channels, cfg, "channels", "canales")
    if not channels:
        channels.update(_DEFAULT_OPERATIONAL_CHANNELS)
        sources["channels"].append("platform_defaults")
    else:
        sources["channels"].append("tenant_config")

    if cfg.get("voice_enabled") or cfg.get("realtime_voice_enabled") or cfg.get("calls_enabled"):
        channels.add("voice")
        sources["channels"].append("tenant_voice_config")

    return {
        "categorias": sorted(categories),
        "zonas": sorted(zones),
        "channels": sorted(channels),
        "sources": {key: sorted(set(value)) for key, value in sources.items() if value},
    }


def _ticket_extra(ticket: TenantTicket) -> dict[str, Any]:
    return ticket.datos_extra if isinstance(ticket.datos_extra, dict) else {}


def _open(value: Any) -> bool:
    return str(value or "").strip().lower() not in _CLOSED_STATES


def _norm(value: Any, fallback: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized or fallback


def _ticket_snapshot(ticket: Any) -> dict[str, Any]:
    if isinstance(ticket, TenantTicket):
        extra = _ticket_extra(ticket)
        return {
            "source_model": "TenantTicket",
            "id": ticket.id,
            "ticket_id": ticket.id,
            "title": extra.get("title") or ticket.categoria or f"Ticket {ticket.id}",
            "status": ticket.estado,
            "category": _norm(ticket.categoria, "sin_categoria"),
            "zone": _norm(extra.get("zone") or extra.get("zona") or extra.get("address"), "sin_zona"),
            "channel": _norm(extra.get("channel") or ticket.origen, "web"),
            "priority": _norm(extra.get("priority"), "medium"),
            "assignee_id": extra.get("assignee_id"),
            "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
            "updated_at": ticket.updated_at.isoformat() if ticket.updated_at else None,
        }
    if isinstance(ticket, MunicipioTicket):
        return {
            "source_model": "MunicipioTicket",
            "id": ticket.id,
            "ticket_id": ticket.id,
            "title": ticket.asunto or ticket.categoria or f"Reclamo {ticket.nro_ticket}",
            "status": ticket.estado,
            "category": _norm(ticket.categoria, "sin_categoria"),
            "zone": _norm(ticket.distrito, "sin_zona"),
            "channel": _norm(ticket.canal_ingreso, "whatsapp"),
            "priority": "medium",
            "assignee_id": ticket.asignado_a_id,
            "created_at": ticket.fecha.isoformat() if ticket.fecha else None,
            "updated_at": (ticket.ultima_actividad or ticket.fecha).isoformat() if (ticket.ultima_actividad or ticket.fecha) else None,
        }
    return {
        "source_model": "PymeTicket",
        "id": ticket.id,
        "ticket_id": ticket.id,
        "title": ticket.asunto or ticket.categoria or f"Ticket {ticket.nro_ticket}",
        "status": ticket.estado,
        "category": _norm(ticket.categoria, "sin_categoria"),
        "zone": "sin_zona",
        "channel": "whatsapp",
        "priority": "medium",
        "assignee_id": ticket.asignado_a_id,
        "created_at": ticket.fecha.isoformat() if ticket.fecha else None,
        "updated_at": ticket.fecha.isoformat() if ticket.fecha else None,
    }


def municipio_ticket_query_for_tenant(tenant: TenantProfile):
    conditions = []
    if getattr(tenant, "id", None):
        conditions.append(MunicipioTicket.tenant_id == tenant.id)
    if getattr(tenant, "municipio_id", None):
        conditions.append(MunicipioTicket.municipio_id == tenant.municipio_id)
    if not conditions:
        return MunicipioTicket.query.filter(False)
    return MunicipioTicket.query.filter(or_(*conditions))


def pyme_ticket_query_for_tenant(tenant: TenantProfile):
    conditions = []
    if getattr(tenant, "id", None):
        conditions.append(PymeTicket.tenant_id == tenant.id)
    owner = User.query.get(getattr(tenant, "pyme_id", None)) if getattr(tenant, "pyme_id", None) else None
    rubro_id = getattr(owner, "rubro_id", None)
    if rubro_id:
        conditions.append(PymeTicket.rubro_id == rubro_id)
    if not conditions:
        return PymeTicket.query.filter(False)
    return PymeTicket.query.filter(or_(*conditions))


def _tenant_tickets(tenant: TenantProfile) -> list[Any]:
    tickets: list[Any] = [
        ticket
        for ticket in TenantTicket.query.filter_by(tenant_id=tenant.id).all()
        if _open(ticket.estado)
    ]
    tickets.extend(
        ticket
        for ticket in municipio_ticket_query_for_tenant(tenant).all()
        if _open(ticket.estado)
    )
    tickets.extend(
        ticket
        for ticket in pyme_ticket_query_for_tenant(tenant).all()
        if _open(ticket.estado)
    )
    return tickets


def tenant_open_ticket_snapshots(tenant: TenantProfile) -> list[dict[str, Any]]:
    return [_ticket_snapshot(ticket) for ticket in _tenant_tickets(tenant)]


def workload_by_employee(tenant: TenantProfile) -> dict[int, int]:
    workload: dict[int, int] = {}
    for ticket in _tenant_tickets(tenant):
        snapshot = _ticket_snapshot(ticket)
        assignee_id = snapshot.get("assignee_id")
        if assignee_id:
            try:
                key = int(assignee_id)
            except (TypeError, ValueError):
                continue
            workload[key] = workload.get(key, 0) + 1
    return workload


def score_employee_for_ticket(emp: User, ticket: dict[str, Any], workload: int = 0) -> tuple[float, list[str]]:
    scope = employee_scope(emp)
    reasons: list[str] = []
    score = 0.0

    if not any(scope[key] for key in ("categorias", "zonas", "channels")):
        score += 10
        reasons.append("generalist")
    if ticket["category"] in scope["categorias"]:
        score += 45
        reasons.append("category_match")
    if ticket["zone"] in scope["zonas"]:
        score += 30
        reasons.append("zone_match")
    if ticket["channel"] in scope["channels"]:
        score += 20
        reasons.append("channel_match")
    if "tickets_assign" in scope["permisos"] or "orders_assign" in scope["permisos"]:
        score += 5
        reasons.append("assignment_permission")

    penalty = min(25, workload * 4)
    score = max(0.0, score - penalty)
    if penalty:
        reasons.append(f"workload_penalty_{penalty}")
    return round(score, 2), reasons


def best_employee_for_ticket(ticket: dict[str, Any], employees: list[User], workloads: dict[int, int]) -> dict[str, Any] | None:
    candidates = []
    for emp in employees:
        workload = workloads.get(emp.id, 0)
        score, reasons = score_employee_for_ticket(emp, ticket, workload)
        candidates.append(
            {
                "employee": employee_ref(emp),
                "score": score,
                "reasons": reasons,
                "workload_open": workload,
            }
        )
    candidates.sort(key=lambda item: (item["score"], -item["workload_open"]), reverse=True)
    return candidates[0] if candidates else None


def build_employee_routing_payload(tenant: TenantProfile) -> dict[str, Any]:
    employees = User.query.filter_by(tenant_id=tenant.id, es_empleado=True).order_by(User.id.asc()).all()
    workloads = workload_by_employee(tenant)
    tickets = tenant_open_ticket_snapshots(tenant)
    unassigned = [ticket for ticket in tickets if not ticket.get("assignee_id")]
    supported_dimensions = tenant_operational_dimensions(tenant, tickets)

    categories = sorted(set(supported_dimensions["categorias"]) | {ticket["category"] for ticket in tickets})
    zones = sorted(set(supported_dimensions["zonas"]) | {ticket["zone"] for ticket in tickets if ticket["zone"] != "sin_zona"})
    channels = sorted(set(supported_dimensions["channels"]) | {ticket["channel"] for ticket in tickets})

    employee_items = []
    for emp in employees:
        ref = employee_ref(emp)
        ref["workload_open"] = workloads.get(emp.id, 0)
        employee_items.append(ref)

    recommendations = []
    for ticket in unassigned[:50]:
        best = best_employee_for_ticket(ticket, employees, workloads)
        recommendations.append(
            {
                "ticket": ticket,
                "suggested_assignee": best["employee"] if best else None,
                "score": best["score"] if best else 0,
                "reasons": best["reasons"] if best else ["no_employee_available"],
            }
        )

    return {
        "contract_version": EMPLOYEE_ROUTING_CONTRACT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant": tenant_ref(tenant),
        "routing_policy": {
            "source": "user.accesibilidad.employee_scope",
            "dimensions": ["categorias", "zonas", "channels", "permisos"],
            "assignment_targets": ["TenantTicket", "MunicipioTicket", "PymeTicket"],
            "scoring": ["category_match", "zone_match", "channel_match", "assignment_permission", "workload_penalty"],
        },
        "dimensions": {
            "categorias": categories,
            "zonas": zones,
            "channels": channels,
            "sources": supported_dimensions.get("sources") or {},
        },
        "employees": employee_items,
        "queues": {
            "open": tickets,
            "unassigned": unassigned,
            "unassigned_count": len(unassigned),
        },
        "recommendations": recommendations,
        "actions": {
            "update_employee_scope": "/api/v2/employees/{employee_id}/routing-scope",
            "auto_assign": "/api/v2/employee-routing/auto-assign",
        },
        "frontend_contract": {
            "render_as": "employee_routing_matrix",
            "primary_refresh_seconds": 30,
            "recommended_views": ["coverage_matrix", "employee_workload", "unassigned_queue", "auto_assign_preview"],
            "empty_state_behavior": "show_scope_setup_checklist",
        },
    }


def find_ticket_for_assignment(tenant: TenantProfile, source_model: str, ticket_id: int) -> Any | None:
    source = str(source_model or "").strip()
    if source == "TenantTicket":
        return TenantTicket.query.filter_by(id=ticket_id, tenant_id=tenant.id).first()
    if source == "MunicipioTicket":
        return municipio_ticket_query_for_tenant(tenant).filter(MunicipioTicket.id == ticket_id).first()
    if source == "PymeTicket":
        return pyme_ticket_query_for_tenant(tenant).filter(PymeTicket.id == ticket_id).first()
    return None
