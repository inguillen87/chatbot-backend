from types import SimpleNamespace

import pytest

from services.employee_ticket_access import ticket_assignee_is_operational
from services.ticket_assignment_policy import (
    TicketAssignmentPolicyError,
    actor_can_assign_tickets,
    assignment_transition,
)


def _actor(*, role: str, actor_id: int = 10, employee: bool = True, metadata=None):
    return SimpleNamespace(
        id=actor_id,
        rol=role,
        es_empleado=employee,
        accesibilidad=metadata or {},
    )


@pytest.mark.parametrize("role", ["admin", "super_admin", "super-admin", "supervisor"])
def test_only_explicit_supervised_roles_can_assign_third_parties(role):
    assert actor_can_assign_tickets(_actor(role=role)) is True


@pytest.mark.parametrize(
    "role",
    ["tenant_admin", "tenant-admin", "admin_pyme", "admin_municipio", "manager", "administrador"],
)
def test_tenant_role_aliases_do_not_escalate_to_supervised_assignment(role):
    actor = _actor(role=role)

    assert actor_can_assign_tickets(actor) is False
    with pytest.raises(TicketAssignmentPolicyError) as exc_info:
        assignment_transition(
            actor=actor,
            payload={"expected_assignee_id": None},
            current_assignee_id=None,
            target_assignee_id=20,
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.reason_code == "ticket_assignment_forbidden"


def test_dotted_capability_is_the_only_role_independent_assignment_elevation():
    actor = _actor(
        role="manager",
        metadata={"employee_scope": {"capabilities": ["tickets.assign"]}},
    )

    assert actor_can_assign_tickets(actor) is True
    transition = assignment_transition(
        actor=actor,
        payload={"expected_assignee_id": None},
        current_assignee_id=None,
        target_assignee_id=20,
    )
    assert transition.target_assignee_id == 20


@pytest.mark.parametrize("lossy_value", [2.9, 2.0, True, "2.9", " 2.0 "])
def test_assignment_cas_rejects_lossy_or_ambiguous_numeric_identities(lossy_value):
    with pytest.raises(TicketAssignmentPolicyError) as exc_info:
        assignment_transition(
            actor=_actor(role="admin", actor_id=10),
            payload={"expected_assignee_id": lossy_value},
            current_assignee_id=2,
            target_assignee_id=20,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.reason_code == "expected_assignee_id_invalid"


def test_operational_destination_requires_authoritative_employee_flag():
    assert ticket_assignee_is_operational(_actor(role="empleado", employee=False)) is False
    assert ticket_assignee_is_operational(_actor(role="admin", employee=False)) is False
    assert ticket_assignee_is_operational(_actor(role="usuario", employee=True)) is True
