from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
import copy

from flask import current_app

from extensions import db
from models import TenantProfile, TenantTicket
from services.employee_ticket_access import apply_employee_ticket_category_scope
from services.v2.ticket_event_service import record_ticket_event
from utils.roles import ROLE_EMPLEADO, ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, canonical_role

_DEFAULT_POLICIES = {
    "low": {"first_response_minutes": 240, "resolution_minutes": 2880, "next_update_minutes": 1440},
    "medium": {"first_response_minutes": 120, "resolution_minutes": 1440, "next_update_minutes": 720},
    "high": {"first_response_minutes": 60, "resolution_minutes": 720, "next_update_minutes": 240},
    "urgent": {"first_response_minutes": 15, "resolution_minutes": 240, "next_update_minutes": 60},
}

_POLICY_FIELDS = (
    "first_response_minutes",
    "resolution_minutes",
    "next_update_minutes",
)
_MIN_POLICY_MINUTES = 1
_MAX_POLICY_MINUTES = 525_600
_SLA_CLOCKS = (
    (
        "first_response",
        "first_response_due_at",
        ("first_response_satisfied_at", "first_response_at"),
    ),
    (
        "resolution",
        "resolution_due_at",
        ("resolution_satisfied_at", "resolved_at", "resolution_at"),
    ),
    (
        "next_update",
        "next_update_due_at",
        ("next_update_satisfied_at",),
    ),
)
_SLA_WARNING_WINDOW = timedelta(hours=12)
_SLA_HISTORY_LIMIT = 50
_SLA_NEXT_UPDATE_HISTORY_LIMIT = 50
_SLA_BREACH_RECEIPTS_LIMIT = 100
_SLA_HISTORY_FIELDS = (
    "first_response_due_at",
    "first_response_satisfied_at",
    "first_response_at",
    "resolution_due_at",
    "resolution_satisfied_at",
    "resolved_at",
    "resolution_at",
    "next_update_due_at",
    "next_update_satisfied_at",
    "next_update_last_satisfied_at",
    "next_update_cycle",
    "last_update_at",
    "paused",
)

_PAUSED_STATUSES = {"waiting_customer", "esperando_cliente"}
_CLOSED_STATUSES = {"resuelto", "cerrado", "closed", "resolved"}


class SlaPolicyValidationError(ValueError):
    """Structured validation failure for an attempted SLA policy write."""

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = copy.deepcopy(errors)
        super().__init__("Politicas SLA invalidas")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def is_sla_operator_role(value: Any) -> bool:
    return canonical_role(value) in {
        ROLE_SUPERADMIN,
        ROLE_TENANT_ADMIN,
        ROLE_EMPLEADO,
    }


def _ensure_tenant_cfg(tenant: TenantProfile) -> dict[str, Any]:
    return copy.deepcopy(tenant.configuracion) if isinstance(tenant.configuracion, dict) else {}


def _positive_bounded_minutes(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
    else:
        return None
    if not _MIN_POLICY_MINUTES <= parsed <= _MAX_POLICY_MINUTES:
        return None
    return parsed


def _normalized_policies(value: Any) -> dict[str, dict[str, int]]:
    normalized = copy.deepcopy(_DEFAULT_POLICIES)
    if not isinstance(value, dict):
        return normalized

    for priority, incoming in value.items():
        if priority not in normalized or not isinstance(incoming, dict):
            continue
        for field in _POLICY_FIELDS:
            parsed = _positive_bounded_minutes(incoming.get(field))
            if parsed is not None:
                normalized[priority][field] = parsed
    return normalized


def _validated_policy_patch(value: Any) -> dict[str, dict[str, int]]:
    """Validate a write payload without accepting ambiguous coercions.

    Stored legacy configuration is read through ``_normalized_policies`` so a
    malformed historical value cannot break ticket reads.  New writes use this
    strict path: every supplied priority, field and value must be explicit and
    valid, otherwise the whole update is rejected before mutating the tenant.
    """

    errors: list[dict[str, str]] = []
    validated: dict[str, dict[str, int]] = {}
    if not isinstance(value, dict) or not value:
        raise SlaPolicyValidationError(
            [
                {
                    "field": "policies",
                    "code": "invalid_object",
                    "message": "Debe incluir al menos una politica SLA.",
                }
            ]
        )

    for priority, incoming in value.items():
        priority_path = f"policies.{priority}"
        if priority not in _DEFAULT_POLICIES:
            errors.append(
                {
                    "field": priority_path,
                    "code": "unknown_priority",
                    "message": "Prioridad SLA desconocida.",
                }
            )
            continue
        if not isinstance(incoming, dict) or not incoming:
            errors.append(
                {
                    "field": priority_path,
                    "code": "invalid_object",
                    "message": "La prioridad debe incluir al menos un reloj SLA.",
                }
            )
            continue

        validated_priority: dict[str, int] = {}
        for field, raw_value in incoming.items():
            field_path = f"{priority_path}.{field}"
            if field not in _POLICY_FIELDS:
                errors.append(
                    {
                        "field": field_path,
                        "code": "unknown_field",
                        "message": "Reloj SLA desconocido.",
                    }
                )
                continue
            parsed = (
                raw_value
                if isinstance(raw_value, int)
                and not isinstance(raw_value, bool)
                and _MIN_POLICY_MINUTES <= raw_value <= _MAX_POLICY_MINUTES
                else None
            )
            if parsed is None:
                errors.append(
                    {
                        "field": field_path,
                        "code": "invalid_minutes",
                        "message": (
                            f"Debe ser un entero entre {_MIN_POLICY_MINUTES} "
                            f"y {_MAX_POLICY_MINUTES} minutos."
                        ),
                    }
                )
                continue
            validated_priority[field] = parsed

        if validated_priority:
            validated[priority] = validated_priority

    if errors:
        raise SlaPolicyValidationError(errors)
    return validated


def get_policies_for_tenant(tenant: TenantProfile) -> dict[str, Any]:
    cfg = _ensure_tenant_cfg(tenant)
    return _normalized_policies(cfg.get("v2_sla_policies"))


def save_policies_for_tenant(tenant: TenantProfile, policies: dict[str, Any]) -> dict[str, Any]:
    patch = _validated_policy_patch(policies)
    normalized = get_policies_for_tenant(tenant)
    for priority, fields in patch.items():
        normalized[priority].update(fields)

    cfg = _ensure_tenant_cfg(tenant)
    cfg["v2_sla_policies"] = copy.deepcopy(normalized)
    tenant.configuracion = cfg
    db.session.add(tenant)
    return copy.deepcopy(normalized)


def _aware_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def evaluate_sla_clocks(
    sla: dict[str, Any] | None,
    *,
    ticket_status: Any,
    now: datetime | None = None,
    warning_window: timedelta = _SLA_WARNING_WINDOW,
) -> dict[str, Any]:
    """Evaluate all current SLA deadlines without database or request state.

    Missing or malformed deadlines remain explicit ``unknown`` evidence.  An
    active ticket is only ``ok`` when all three clocks are known and healthy.
    """

    observed_at = _aware_datetime(now) or _utc_now()
    if warning_window.total_seconds() < 0:
        warning_window = timedelta(0)
    values = sla if isinstance(sla, dict) else {}
    normalized_status = str(ticket_status or "").strip().lower()
    closed = normalized_status in _CLOSED_STATUSES
    paused = normalized_status in _PAUSED_STATUSES or values.get("paused") is True
    active = not closed and not paused

    clocks: dict[str, dict[str, Any]] = {}
    breached_clocks: list[str] = []
    warning_clocks: list[str] = []
    unknown_clocks: list[str] = []
    known_deadlines: list[datetime] = []

    for clock_name, due_field, satisfied_fields in _SLA_CLOCKS:
        raw_due = values.get(due_field)
        due_at = _aware_datetime(raw_due)
        satisfied_field = next(
            (field for field in satisfied_fields if values.get(field) not in (None, "")),
            None,
        )
        raw_satisfied = values.get(satisfied_field) if satisfied_field else None
        satisfied_at = _aware_datetime(raw_satisfied)
        invalid_satisfaction = raw_satisfied not in (None, "") and satisfied_at is None

        if raw_due in (None, ""):
            clocks[clock_name] = {
                "field": due_field,
                "due_at": None,
                "satisfied_field": satisfied_field,
                "fulfilled_at": satisfied_at.isoformat() if satisfied_at else None,
                "known": False,
                "status": "unknown",
                "state": "unknown",
                "overdue": None,
                "at_risk": False,
                "remaining_seconds": None,
                "satisfaction_lag_seconds": None,
                "unknown_reason": "missing_due_at",
                "satisfaction_evidence_error": (
                    "invalid_satisfied_at" if invalid_satisfaction else None
                ),
            }
            unknown_clocks.append(clock_name)
            continue
        if due_at is None:
            clocks[clock_name] = {
                "field": due_field,
                "due_at": str(raw_due),
                "satisfied_field": satisfied_field,
                "fulfilled_at": satisfied_at.isoformat() if satisfied_at else None,
                "known": False,
                "status": "unknown",
                "state": "unknown",
                "overdue": None,
                "at_risk": False,
                "remaining_seconds": None,
                "satisfaction_lag_seconds": None,
                "unknown_reason": "invalid_due_at",
                "satisfaction_evidence_error": (
                    "invalid_satisfied_at" if invalid_satisfaction else None
                ),
            }
            unknown_clocks.append(clock_name)
            continue

        if invalid_satisfaction:
            clocks[clock_name] = {
                "field": due_field,
                "due_at": due_at.isoformat(),
                "satisfied_field": satisfied_field,
                "fulfilled_at": None,
                "known": False,
                "status": "unknown",
                "state": "unknown",
                "overdue": None,
                "at_risk": False,
                "remaining_seconds": None,
                "satisfaction_lag_seconds": None,
                "unknown_reason": "invalid_satisfied_at",
                "satisfaction_evidence_error": "invalid_satisfied_at",
            }
            unknown_clocks.append(clock_name)
            continue

        if satisfied_at is not None:
            satisfied_late = satisfied_at > due_at
            if satisfied_late:
                breached_clocks.append(clock_name)
            clocks[clock_name] = {
                "field": due_field,
                "due_at": due_at.isoformat(),
                "satisfied_field": satisfied_field,
                "fulfilled_at": satisfied_at.isoformat(),
                "known": True,
                "status": "overdue" if satisfied_late else "satisfied",
                "state": "satisfied_late" if satisfied_late else "satisfied",
                "overdue": satisfied_late,
                "at_risk": satisfied_late,
                "remaining_seconds": None,
                "satisfaction_lag_seconds": int((satisfied_at - due_at).total_seconds()),
                "unknown_reason": None,
                "satisfaction_evidence_error": None,
            }
            continue

        remaining_seconds = int((due_at - observed_at).total_seconds())
        known_deadlines.append(due_at)
        if not active:
            status = "inactive"
            state = "inactive"
            overdue: bool | None = False
            at_risk = False
        elif due_at <= observed_at:
            status = "overdue"
            state = "breached"
            overdue = True
            at_risk = True
            breached_clocks.append(clock_name)
        elif due_at <= observed_at + warning_window:
            status = "due"
            state = "warning"
            overdue = False
            at_risk = True
            warning_clocks.append(clock_name)
        else:
            status = "due"
            state = "ok"
            overdue = False
            at_risk = False

        clocks[clock_name] = {
            "field": due_field,
            "due_at": due_at.isoformat(),
            "satisfied_field": satisfied_field,
            "fulfilled_at": None,
            "known": True,
            "status": status,
            "state": state,
            "overdue": overdue,
            "at_risk": at_risk,
            "remaining_seconds": remaining_seconds,
            "satisfaction_lag_seconds": None,
            "unknown_reason": None,
            "satisfaction_evidence_error": None,
        }

    known = not unknown_clocks
    if closed:
        state = "closed"
    elif paused:
        state = "paused"
    elif breached_clocks:
        state = "breached"
    elif warning_clocks:
        state = "warning"
    elif unknown_clocks:
        state = "unknown"
    else:
        state = "ok"

    if not active:
        overdue: bool | None = False
    elif breached_clocks:
        overdue = True
    elif unknown_clocks:
        overdue = None
    else:
        overdue = False

    return {
        "contract_version": "ticket.sla.v1",
        "state": state,
        "known": known,
        "unknown": not known,
        "active": active,
        "overdue": overdue,
        "evaluated_at": observed_at.isoformat(),
        "warning_window_seconds": int(warning_window.total_seconds()),
        "next_due_at": min(known_deadlines).isoformat() if known_deadlines else None,
        "breached_clocks": breached_clocks,
        "warning_clocks": warning_clocks,
        "unknown_clocks": unknown_clocks,
        "clocks": clocks,
    }


def evaluate_ticket_sla(
    ticket: TenantTicket,
    *,
    sla_override: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    extra = ticket.datos_extra if isinstance(ticket.datos_extra, dict) else {}
    sla = sla_override if isinstance(sla_override, dict) else (
        extra.get("sla") if isinstance(extra.get("sla"), dict) else {}
    )
    return evaluate_sla_clocks(sla, ticket_status=ticket.estado, now=now)


def _policy_for_ticket(ticket: TenantTicket, policies: dict[str, Any]) -> dict[str, int]:
    extra = ticket.datos_extra if isinstance(ticket.datos_extra, dict) else {}
    priority = str(extra.get("priority") or "medium").lower()
    normalized_policies = _normalized_policies(policies)
    return normalized_policies.get(priority) or normalized_policies["medium"]


def _append_sla_history(
    sla: dict[str, Any],
    *,
    event: str,
    occurred_at: datetime,
) -> None:
    """Keep a bounded factual snapshot before changing an active SLA cycle."""

    raw_history = sla.get("history")
    history = list(raw_history) if isinstance(raw_history, list) else []
    snapshot = {
        field: copy.deepcopy(sla.get(field))
        for field in _SLA_HISTORY_FIELDS
        if field in sla
    }
    history.append(
        {
            "event": event,
            "occurred_at": occurred_at.isoformat(),
            "snapshot": snapshot,
        }
    )
    sla["history"] = history[-_SLA_HISTORY_LIMIT:]


def _valid_cycle_number(value: Any) -> int | None:
    """Return a bounded positive cycle number without accepting booleans."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        return None
    if not 1 <= parsed <= 2_147_483_647:
        return None
    return parsed


def _current_next_update_cycle(sla: dict[str, Any]) -> int:
    """Resolve the active cycle without trusting malformed stored evidence."""

    explicit = _valid_cycle_number(sla.get("next_update_cycle"))
    if explicit is not None:
        return explicit

    raw_history = sla.get("next_update_history")
    if not isinstance(raw_history, list):
        return 1
    archived_cycles = [
        cycle
        for item in raw_history[-_SLA_NEXT_UPDATE_HISTORY_LIMIT:]
        if isinstance(item, dict)
        for cycle in [_valid_cycle_number(item.get("cycle"))]
        if cycle is not None
    ]
    return min(max(archived_cycles, default=0) + 1, 2_147_483_647)


def _append_next_update_cycle_history(
    sla: dict[str, Any],
    *,
    satisfied_at: datetime,
) -> int:
    """Archive one completed update obligation before opening the next one.

    ``datos_extra.sla`` remains the persistence boundary, so this is backward
    compatible and needs no schema migration. Existing entries are deep-copied
    and never edited by the lifecycle helper. Missing or malformed due-date
    evidence is explicitly ``unknown`` rather than being misreported as an
    on-time response.
    """

    raw_due_at = copy.deepcopy(sla.get("next_update_due_at"))
    due_at = _aware_datetime(raw_due_at)
    satisfied_iso = satisfied_at.isoformat()
    cycle = _current_next_update_cycle(sla)

    if raw_due_at in (None, ""):
        result = "unknown"
        known = False
        unknown_reason = "missing_due_at"
        stored_due_at = None
        completion_delta_seconds = None
    elif due_at is None:
        result = "unknown"
        known = False
        unknown_reason = "invalid_due_at"
        stored_due_at = str(raw_due_at)
        completion_delta_seconds = None
    else:
        completion_delta = (satisfied_at - due_at).total_seconds()
        completion_delta_seconds = round(completion_delta, 6)
        result = "late" if completion_delta > 0 else "on_time"
        known = True
        unknown_reason = None
        stored_due_at = due_at.isoformat()

    entry = {
        "contract_version": "ticket.sla.next_update_cycle.v1",
        "cycle": cycle,
        "due_at": stored_due_at,
        "satisfied_at": satisfied_iso,
        "result": result,
        "known": known,
        "unknown_reason": unknown_reason,
        "completion_delta_seconds": completion_delta_seconds,
        "closed_by": "public_operator_response",
    }
    raw_history = sla.get("next_update_history")
    history = (
        copy.deepcopy(raw_history[-(_SLA_NEXT_UPDATE_HISTORY_LIMIT - 1):])
        if isinstance(raw_history, list)
        else []
    )
    history.append(entry)
    sla["next_update_history"] = history[-_SLA_NEXT_UPDATE_HISTORY_LIMIT:]
    return cycle


def apply_priority_change_sla(
    ticket: TenantTicket,
    policies: dict[str, Any],
    *,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    """Apply a new priority without rewriting completed SLA facts.

    Only obligations that are still active receive a new deadline, anchored at
    the priority-change instant.  Completed first-response or resolution facts
    retain both their original deadline and their fulfillment timestamp.
    """

    observed_at = _aware_datetime(occurred_at) or _utc_now()
    policy = _policy_for_ticket(ticket, policies)
    extra = copy.deepcopy(ticket.datos_extra) if isinstance(ticket.datos_extra, dict) else {}
    sla = copy.deepcopy(extra.get("sla")) if isinstance(extra.get("sla"), dict) else {}
    _append_sla_history(sla, event="priority_changed", occurred_at=observed_at)

    normalized_status = str(ticket.estado or "").strip().lower()
    active = normalized_status not in _CLOSED_STATUSES | _PAUSED_STATUSES
    if active:
        if not (
            _aware_datetime(sla.get("first_response_satisfied_at"))
            or _aware_datetime(sla.get("first_response_at"))
        ):
            sla["first_response_due_at"] = (
                observed_at + timedelta(minutes=policy["first_response_minutes"])
            ).isoformat()
        if not (
            _aware_datetime(sla.get("resolution_satisfied_at"))
            or _aware_datetime(sla.get("resolved_at"))
            or _aware_datetime(sla.get("resolution_at"))
        ):
            sla["resolution_due_at"] = (
                observed_at + timedelta(minutes=policy["resolution_minutes"])
            ).isoformat()
        sla.pop("next_update_satisfied_at", None)
        sla["next_update_due_at"] = (
            observed_at + timedelta(minutes=policy["next_update_minutes"])
        ).isoformat()
        sla["paused"] = False

    extra["sla"] = sla
    ticket.datos_extra = extra
    return sla


def apply_reopen_sla(
    ticket: TenantTicket,
    policies: dict[str, Any],
    *,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    """Open a new resolution/update cycle while retaining prior evidence."""

    observed_at = _aware_datetime(occurred_at) or _utc_now()
    policy = _policy_for_ticket(ticket, policies)
    extra = copy.deepcopy(ticket.datos_extra) if isinstance(ticket.datos_extra, dict) else {}
    sla = copy.deepcopy(extra.get("sla")) if isinstance(extra.get("sla"), dict) else {}
    _append_sla_history(sla, event="reopened", occurred_at=observed_at)

    # Resolution fulfillment belongs to the completed cycle preserved above.
    # First response remains a one-shot fact across reopenings.
    for field in (
        "resolution_satisfied_at",
        "resolved_at",
        "resolution_at",
        "next_update_satisfied_at",
    ):
        sla.pop(field, None)
    sla["resolution_due_at"] = (
        observed_at + timedelta(minutes=policy["resolution_minutes"])
    ).isoformat()
    sla["next_update_due_at"] = (
        observed_at + timedelta(minutes=policy["next_update_minutes"])
    ).isoformat()
    sla["paused"] = False
    raw_cycle = sla.get("resolution_cycle")
    try:
        cycle = int(raw_cycle)
    except (TypeError, ValueError):
        cycle = 0
    sla["resolution_cycle"] = max(cycle, 0) + 1

    extra["sla"] = sla
    ticket.datos_extra = extra
    return sla


def apply_operator_response_sla(
    ticket: TenantTicket,
    policies: dict[str, Any],
    *,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    """Persist SLA facts for one durable public operator response.

    First response is a one-shot fact. Every public response archives the
    previous update obligation with its factual result, then starts a new
    ``next_update`` clock. Legacy current-clock fields remain available.
    """

    observed_at = _aware_datetime(occurred_at) or _utc_now()
    policy = _policy_for_ticket(ticket, policies)
    extra = copy.deepcopy(ticket.datos_extra) if isinstance(ticket.datos_extra, dict) else {}
    sla = copy.deepcopy(extra.get("sla")) if isinstance(extra.get("sla"), dict) else {}
    observed_iso = observed_at.isoformat()

    completed_cycle = _append_next_update_cycle_history(
        sla,
        satisfied_at=observed_at,
    )

    if not (
        _aware_datetime(sla.get("first_response_satisfied_at"))
        or _aware_datetime(sla.get("first_response_at"))
    ):
        sla["first_response_satisfied_at"] = observed_iso
        sla["first_response_at"] = observed_iso

    sla["last_update_at"] = observed_iso
    sla["next_update_last_satisfied_at"] = observed_iso
    sla.pop("next_update_satisfied_at", None)
    sla["next_update_due_at"] = (
        observed_at + timedelta(minutes=policy["next_update_minutes"])
    ).isoformat()
    sla["next_update_cycle"] = min(completed_cycle + 1, 2_147_483_647)
    sla["paused"] = str(ticket.estado or "").strip().lower() in _PAUSED_STATUSES

    extra["sla"] = sla
    ticket.datos_extra = extra
    return sla


def apply_resolution_sla(
    ticket: TenantTicket,
    *,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    observed_at = _aware_datetime(occurred_at) or _utc_now()
    extra = copy.deepcopy(ticket.datos_extra) if isinstance(ticket.datos_extra, dict) else {}
    sla = copy.deepcopy(extra.get("sla")) if isinstance(extra.get("sla"), dict) else {}
    observed_iso = observed_at.isoformat()
    sla["resolution_satisfied_at"] = observed_iso
    sla["resolved_at"] = observed_iso
    extra["sla"] = sla
    ticket.datos_extra = extra
    return sla


def _sla_fields_for_ticket(
    ticket: TenantTicket,
    policies: dict[str, Any],
    *,
    force_recalculate: bool = False,
) -> dict[str, Any]:
    extra = copy.deepcopy(ticket.datos_extra) if isinstance(ticket.datos_extra, dict) else {}
    policy = _policy_for_ticket(ticket, policies)

    created_at = ticket.created_at or _utc_now()
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    status = str(ticket.estado or "").lower()
    paused = status in _PAUSED_STATUSES

    existing_sla = extra.get("sla") if isinstance(extra.get("sla"), dict) else {}

    if paused:
        due_fields = dict(existing_sla)
        due_fields.update(
            {
                "first_response_due_at": None,
                "resolution_due_at": None,
                "next_update_due_at": None,
                "paused": True,
            }
        )
    else:
        due_fields = dict(existing_sla)
        if force_recalculate or not due_fields.get("first_response_due_at"):
            due_fields["first_response_due_at"] = (created_at + timedelta(minutes=policy["first_response_minutes"])).isoformat()
        if force_recalculate or not due_fields.get("resolution_due_at"):
            due_fields["resolution_due_at"] = (created_at + timedelta(minutes=policy["resolution_minutes"])).isoformat()
        if force_recalculate or not due_fields.get("next_update_due_at"):
            due_fields["next_update_due_at"] = (created_at + timedelta(minutes=policy["next_update_minutes"])).isoformat()
        due_fields["paused"] = False

    return due_fields


def apply_sla_to_ticket(ticket: TenantTicket, policies: dict[str, Any], *, force_recalculate: bool = False) -> dict[str, Any]:
    due_fields = _sla_fields_for_ticket(
        ticket,
        policies,
        force_recalculate=force_recalculate,
    )
    extra = copy.deepcopy(ticket.datos_extra) if isinstance(ticket.datos_extra, dict) else {}
    extra["sla"] = due_fields
    ticket.datos_extra = extra
    return due_fields


def is_ticket_overdue(
    ticket: TenantTicket,
    *,
    sla_override: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> bool:
    evaluation = evaluate_ticket_sla(ticket, sla_override=sla_override, now=now)
    return evaluation["overdue"] is True


def detect_sla_breaches_for_tenant(
    tenant: TenantProfile,
    actor_user=None,
    *,
    materialize: bool = True,
) -> list[dict[str, Any]]:
    policies = get_policies_for_tenant(tenant)
    breaches: list[dict[str, Any]] = []

    query = TenantTicket.query.filter_by(tenant_id=tenant.id)
    query = apply_employee_ticket_category_scope(query, actor_user, TenantTicket)
    if materialize:
        # The JSON receipt below is transactionally coupled to the audit event.
        # Serialize concurrent workers on each ticket so two detectors cannot
        # both observe a missing receipt and emit the same breach.
        # A periodic detector must not serialize every ticket of a busy tenant
        # behind one in-flight row. Locked rows are safely deferred to the next
        # run; receipt and audit event still commit atomically for owned rows.
        query = query.with_for_update(skip_locked=True)
    tickets = query.all()
    for ticket in tickets:
        sla_fields = _sla_fields_for_ticket(ticket, policies)
        sla_evaluation = evaluate_ticket_sla(ticket, sla_override=sla_fields)
        if materialize:
            extra_with_sla = copy.deepcopy(ticket.datos_extra) if isinstance(ticket.datos_extra, dict) else {}
            extra_with_sla["sla"] = sla_fields
            ticket.datos_extra = extra_with_sla
        if sla_evaluation["overdue"] is not True:
            continue

        extra = ticket.datos_extra if isinstance(ticket.datos_extra, dict) else {}
        breach_key = "sla_breach_event_emitted_at"
        raw_receipts = extra.get("sla_breach_receipts")
        stored_receipts = {
            str(item)
            for item in raw_receipts
            if str(item).strip()
        } if isinstance(raw_receipts, list) else set()
        receipt_by_clock = {
            clock_name: (
                f"ticket.sla.v1:{clock_name}:"
                f"{sla_evaluation['clocks'][clock_name].get('due_at')}"
            )
            for clock_name in sla_evaluation["breached_clocks"]
            if sla_evaluation["clocks"].get(clock_name, {}).get("due_at")
        }
        current_receipts = set(receipt_by_clock.values())
        # Compatibility with the former one-shot marker. The legacy detector
        # only evaluated ``resolution_due_at`` (or ``next_update_due_at`` when
        # resolution was absent), so adopt at most that one receipt. Seeding
        # every currently breached clock would silently discard first-response
        # and next-update breaches that the old marker never represented.
        if extra.get(breach_key) and not stored_receipts:
            legacy_clock = (
                "resolution"
                if sla_fields.get("resolution_due_at")
                else "next_update"
                if sla_fields.get("next_update_due_at")
                else None
            )
            legacy_receipt = receipt_by_clock.get(str(legacy_clock or ""))
            if legacy_receipt:
                stored_receipts.add(legacy_receipt)
        new_receipts = sorted(current_receipts - stored_receipts)
        newly_breached_clocks = sorted(
            clock_name
            for clock_name, receipt in receipt_by_clock.items()
            if receipt in new_receipts
        )

        breach_payload = {
            "ticket_id": ticket.id,
            "estado": ticket.estado,
            "categoria": ticket.categoria,
            "sla_contract_version": sla_evaluation["contract_version"],
            "breached_clocks": sla_evaluation["breached_clocks"],
            "newly_breached_clocks": newly_breached_clocks,
        }
        breaches.append(breach_payload)

        if materialize:
            merged_receipts = sorted(stored_receipts | current_receipts)
            extra["sla_breach_receipts"] = merged_receipts[-_SLA_BREACH_RECEIPTS_LIMIT:]
        if materialize and new_receipts:
            record_ticket_event(
                tenant_id=tenant.id,
                event_type="sla.breach_detected",
                ticket=ticket,
                actor_user=actor_user,
                details=breach_payload,
            )
            extra[breach_key] = _utc_now().isoformat()
        if materialize:
            ticket.datos_extra = extra
            db.session.add(ticket)

    if breaches:
        current_app.logger.info("[v2.sla] Detected %s SLA breaches for tenant=%s", len(breaches), tenant.id)

    return breaches
