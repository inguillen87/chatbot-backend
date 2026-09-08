"""Shared authorization and compare-and-set policy for ticket assignment.

Every ticket writer must call this boundary before changing an assignee.  It is
deliberately independent from Flask so API surfaces can keep their established
error envelopes while sharing one fail-closed decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

SUPERVISED_ASSIGNMENT_ROLES = frozenset({"admin", "super_admin", "supervisor"})
ASSIGNMENT_CAPABILITY = "tickets.assign"
_CAPABILITY_ALIASES = {
    "tickets_assign": ASSIGNMENT_CAPABILITY,
}


class TicketAssignmentPolicyError(ValueError):
    def __init__(self, status_code: int, reason_code: str, message: str, action_hint: str):
        super().__init__(message)
        self.status_code = status_code
        self.reason_code = reason_code
        self.message = message
        self.action_hint = action_hint


@dataclass(frozen=True)
class AssignmentTransition:
    current_assignee_id: int | None
    target_assignee_id: int | None
    expected_assignee_id: int | None
    replayed: bool


def _flatten_capability_values(raw: Any) -> list[str]:
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(raw, Mapping):
        return [str(key).strip() for key, enabled in raw.items() if enabled and str(key).strip()]
    if isinstance(raw, (list, tuple, set, frozenset)):
        values: list[str] = []
        for item in raw:
            values.extend(_flatten_capability_values(item))
        return values
    text = str(raw).strip()
    return [text] if text else []


def actor_assignment_capabilities(actor: Any) -> frozenset[str]:
    metadata = getattr(actor, "accesibilidad", None)
    if not isinstance(metadata, Mapping):
        metadata = {}
    employee_scope = metadata.get("employee_scope")
    if not isinstance(employee_scope, Mapping):
        employee_scope = {}

    raw_values = []
    for container in (metadata, employee_scope):
        raw_values.extend(
            container.get(key)
            for key in ("permissions", "permisos", "capabilities", "scopes")
        )

    normalized: set[str] = set()
    for value in raw_values:
        for token in _flatten_capability_values(value):
            token = token.strip().lower()
            if token:
                normalized.add(_CAPABILITY_ALIASES.get(token, token))
    return frozenset(normalized)


def actor_can_assign_tickets(actor: Any) -> bool:
    if actor is None:
        return False
    from utils.roles import canonical_role

    role = canonical_role(getattr(actor, "rol", None))
    if role in SUPERVISED_ASSIGNMENT_ROLES:
        return True
    return ASSIGNMENT_CAPABILITY in actor_assignment_capabilities(actor)


def _optional_positive_id(
    raw: Any,
    *,
    field_name: str,
    invalid_status: int = 400,
    invalid_reason: str | None = None,
) -> int | None:
    if raw is None or raw == "":
        return None
    # Assignment identities are security-sensitive compare-and-set values.
    # Accept only an exact positive integer (or its ASCII decimal spelling):
    # ``int(2.9) == 2`` would otherwise let a lossy JSON number authorize a
    # transition against operator 2.
    if isinstance(raw, bool):
        parsed = None
    elif isinstance(raw, int):
        parsed = raw
    elif isinstance(raw, str):
        value = raw.strip()
        parsed = int(value, 10) if value and value.isascii() and value.isdecimal() else None
    else:
        parsed = None
    if parsed is None or parsed <= 0:
        raise TicketAssignmentPolicyError(
            invalid_status,
            invalid_reason or f"{field_name}_invalid",
            f"{field_name} no es valido",
            f"send_valid_{field_name}",
        )
    return parsed


def assignment_id(raw: Any, *, field_name: str = "assignee_id") -> int | None:
    """Do not truncate floats, reinterpret booleans, or coerce unsafe state."""
    return _optional_positive_id(raw, field_name=field_name)


def assignment_alias_id(payload: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    values = [assignment_id(payload[key], field_name=key) for key in keys if key in payload]
    if len(set(values)) > 1:
        raise TicketAssignmentPolicyError(
            400, "assignment_identity_conflict", "Los identificadores de asignacion no coinciden",
            "send_consistent_assignment_identity",
        )
    return values[0] if values else None


def require_assignment_authority(actor: Any) -> None:
    if not actor_can_assign_tickets(actor):
        raise TicketAssignmentPolicyError(
            403, "ticket_assignment_forbidden", "La asignacion requiere supervision o tickets.assign",
            "request_supervisor_assignment",
        )


def lock_assignment_ticket(ticket: Any):
    """Refresh the persistent row under a lock before reading any owner copy."""
    from extensions import db
    with db.session.no_autoflush:
        return type(ticket).query.filter_by(id=ticket.id).populate_existing().with_for_update().one()


def claim_transition(*, actor: Any, current_assignee_id: Any) -> AssignmentTransition:
    current = _optional_positive_id(
        current_assignee_id, field_name="current_assignee_id", invalid_status=409,
        invalid_reason="assignment_state_invalid",
    )
    target = assignment_id(getattr(actor, "id", None), field_name="actor_id")
    if not target or not bool(getattr(actor, "es_empleado", False)):
        raise TicketAssignmentPolicyError(
            403, "operational_employee_required", "Solo un empleado operativo puede tomar el ticket",
            "request_operational_employee",
        )
    if current not in (None, target):
        raise TicketAssignmentPolicyError(
            409, "already_claimed", "El ticket ya fue tomado por otro operador", "refresh_ticket_assignment",
        )
    return AssignmentTransition(current, target, current, current == target)


def assignment_transition(
    *,
    actor: Any,
    payload: Mapping[str, Any],
    current_assignee_id: Any,
    target_assignee_id: Any,
    enforce_authorization: bool = True,
) -> AssignmentTransition:
    """Authorize and CAS one assignment transition.

    A replay to the already-current target is idempotent even when the original
    expected value is now stale.  Every different target (including unassign)
    must compare against the locked current state. Domain-specific writers may
    disable this role check only after enforcing their own assignment capability;
    the compare-and-set requirement remains mandatory.
    """

    current = _optional_positive_id(
        current_assignee_id,
        field_name="current_assignee_id",
        invalid_status=409,
        invalid_reason="assignment_state_invalid",
    )
    target = _optional_positive_id(target_assignee_id, field_name="assignee_id")
    actor_id = _optional_positive_id(getattr(actor, "id", None), field_name="actor_id")

    if enforce_authorization:
        require_assignment_authority(actor)

    if "expected_assignee_id" not in payload:
        raise TicketAssignmentPolicyError(
            400,
            "expected_assignee_id_required",
            "expected_assignee_id es obligatorio para asignar",
            "refresh_ticket_and_send_expected_assignee_id",
        )
    expected = _optional_positive_id(
        payload.get("expected_assignee_id"),
        field_name="expected_assignee_id",
    )

    if current == target:
        return AssignmentTransition(current, target, expected, True)
    if expected != current:
        raise TicketAssignmentPolicyError(
            409,
            "assignment_state_conflict",
            "La asignacion cambio desde que se abrio el caso",
            "refresh_ticket_assignment",
        )
    return AssignmentTransition(current, target, expected, False)
