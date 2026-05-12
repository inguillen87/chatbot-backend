from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from models import MunicipioTicket, PymeTicket, TenantProfile, TenantTicket, User


EMPLOYEE_ROUTING_CONTRACT_VERSION = "employee.routing.v1"

_CLOSED_STATES = {"resuelto", "cerrado", "closed", "resolved", "entregado", "completed", "completado"}


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
            "zone": _norm(ticket.distrito or ticket.direccion, "sin_zona"),
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
        "zone": _norm(ticket.direccion, "sin_zona"),
        "channel": "whatsapp",
        "priority": "medium",
        "assignee_id": ticket.asignado_a_id,
        "created_at": ticket.fecha.isoformat() if ticket.fecha else None,
        "updated_at": ticket.fecha.isoformat() if ticket.fecha else None,
    }


def _tenant_tickets(tenant: TenantProfile) -> list[Any]:
    tickets: list[Any] = [
        ticket
        for ticket in TenantTicket.query.filter_by(tenant_id=tenant.id).all()
        if _open(ticket.estado)
    ]
    tickets.extend(
        ticket
        for ticket in MunicipioTicket.query.filter_by(tenant_id=tenant.id).all()
        if _open(ticket.estado)
    )
    tickets.extend(
        ticket
        for ticket in PymeTicket.query.filter_by(tenant_id=tenant.id).all()
        if _open(ticket.estado)
    )
    return tickets


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
    tickets = [_ticket_snapshot(ticket) for ticket in _tenant_tickets(tenant)]
    unassigned = [ticket for ticket in tickets if not ticket.get("assignee_id")]

    categories = sorted({ticket["category"] for ticket in tickets})
    zones = sorted({ticket["zone"] for ticket in tickets})
    channels = sorted({ticket["channel"] for ticket in tickets})

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
        return MunicipioTicket.query.filter_by(id=ticket_id, tenant_id=tenant.id).first()
    if source == "PymeTicket":
        return PymeTicket.query.filter_by(id=ticket_id, tenant_id=tenant.id).first()
    return None
