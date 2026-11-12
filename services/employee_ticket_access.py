from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import false, func, or_

from services.territorial_evidence import canonicalize_territorial_category
from utils.roles import ROLE_EMPLEADO, ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, canonical_role


@dataclass(frozen=True)
class EmployeeTicketCategoryScope:
    """Normalized category scope used by employee-facing ticket details.

    The inputs intentionally mirror the operational queue: explicit
    ``employee_scope`` names, persisted ``categorias_ticket`` relations, and
    the legacy comma-separated ``ticket_categorias`` field are one union.
    """

    names: frozenset[str]
    ids: frozenset[int]


def _normalized_name(value: Any) -> str:
    return str(value or "").strip().lower()


def _category_name_aliases(value: Any) -> frozenset[str]:
    """Return the persisted label and its exact territorial alias.

    Routing, territorial analytics and operator eligibility must not disagree
    only because one surface says ``luminaria`` and another says
    ``luminarias``.  The territorial canonicalizer is deliberately limited to
    exact, auditable aliases; this does not infer a category from free text.
    """

    raw = _normalized_name(value)
    if not raw:
        return frozenset()
    canonical = _normalized_name(canonicalize_territorial_category(raw)["category"])
    return frozenset(item for item in (raw, canonical) if item)


def _is_category_limited_employee(actor: Any) -> bool:
    role = canonical_role(getattr(actor, "rol", None))
    if role in {ROLE_SUPERADMIN, ROLE_TENANT_ADMIN}:
        return False
    return role == ROLE_EMPLEADO or bool(getattr(actor, "es_empleado", False))


def employee_ticket_category_scope(actor: Any) -> EmployeeTicketCategoryScope:
    names: list[str] = []
    ids: list[int] = []

    accessibility = getattr(actor, "accesibilidad", None)
    accessibility = accessibility if isinstance(accessibility, dict) else {}
    configured_scope = accessibility.get("employee_scope")
    configured_scope = configured_scope if isinstance(configured_scope, dict) else {}
    configured_names = configured_scope.get("categorias")
    if isinstance(configured_names, (list, tuple, set)):
        names.extend(_normalized_name(item) for item in configured_names)

    for category in getattr(actor, "categorias_ticket", None) or []:
        name = _normalized_name(getattr(category, "nombre", None))
        if name:
            names.append(name)
        category_id = getattr(category, "id", None)
        if isinstance(category_id, int) and category_id > 0:
            ids.append(category_id)

    configured_csv = str(getattr(actor, "ticket_categorias", None) or "")
    names.extend(_normalized_name(item) for item in configured_csv.split(","))

    expanded_names: set[str] = set()
    for name in names:
        expanded_names.update(_category_name_aliases(name))

    return EmployeeTicketCategoryScope(
        names=frozenset(expanded_names),
        ids=frozenset(ids),
    )


def employee_ticket_category_access_allows(actor: Any, ticket: Any) -> bool:
    """Fail closed when a category-limited employee opens a ticket by ID.

    Tenant authorization remains the responsibility of the calling route.
    Non-employees keep their existing tenant-wide behavior; employees must
    match either the normalized category label or the persisted category ID.
    An empty employee scope therefore grants access to no ticket details.
    """

    return employee_ticket_category_values_allow(
        actor,
        category=getattr(ticket, "categoria", None),
        category_id=getattr(ticket, "categoria_id", None),
    )


def employee_ticket_category_values_allow(
    actor: Any,
    *,
    category: Any = None,
    category_id: Any = None,
) -> bool:
    """Apply the employee category policy to values not yet persisted.

    This is used before recategorizing or creating an operator-owned ticket so
    an employee cannot move a case into a category outside their own scope.
    """

    if not _is_category_limited_employee(actor):
        return True

    scope = employee_ticket_category_scope(actor)
    ticket_names = _category_name_aliases(category)
    if ticket_names.intersection(scope.names):
        return True

    return bool(
        isinstance(category_id, int)
        and category_id > 0
        and category_id in scope.ids
    )


def ticket_assignee_is_compatible(assignee: Any, ticket: Any) -> bool:
    """Return whether an operator may safely own ``ticket``.

    Assignment must not create a case that the destination operator is unable
    to read. Tenant ownership is intentionally checked by the caller because
    legacy municipal records can be associated through owner IDs instead of a
    direct ``tenant_id``.
    """

    return ticket_assignee_category_values_are_compatible(
        assignee,
        category=getattr(ticket, "categoria", None),
        category_id=getattr(ticket, "categoria_id", None),
    )


def ticket_assignee_is_operational(assignee: Any) -> bool:
    """Return whether ``assignee`` is an actual ticket-working identity.

    Tenant membership or an administrative role alone does not make a person
    an operational destination. ``es_empleado`` is the authoritative marker;
    a textual role must never make an identity assignable by itself.
    """

    if assignee is None:
        return False
    return bool(getattr(assignee, "es_empleado", False))


def ticket_assignee_category_values_are_compatible(
    assignee: Any,
    *,
    category: Any = None,
    category_id: Any = None,
) -> bool:
    """Validate the assignment against the case's final category values."""

    if assignee is None:
        return False
    if not ticket_assignee_is_operational(assignee):
        return False
    return employee_ticket_category_values_allow(
        assignee,
        category=category,
        category_id=category_id,
    )


def apply_employee_ticket_category_scope(query: Any, actor: Any, ticket_model: Any):
    """Push the employee category boundary into a SQLAlchemy query.

    Admins and superadmins retain tenant-wide access. Employees with an empty
    or malformed scope receive an empty query, matching detail authorization.
    """

    if not _is_category_limited_employee(actor):
        return query

    scope = employee_ticket_category_scope(actor)
    clauses = []

    category_column = getattr(ticket_model, "categoria", None)
    if category_column is not None and scope.names:
        # Include every exact alias that canonicalizes to an employee's scope.
        # This keeps SQL list filtering aligned with the detail/assignment
        # checks without introducing fuzzy classification.
        query_aliases = set(scope.names)
        for candidate in ("alumbrado", "alumbrado publico", "alumbrado público", "luminaria", "luminarias"):
            if _category_name_aliases(candidate).intersection(scope.names):
                query_aliases.add(candidate)
        clauses.append(func.lower(func.trim(category_column)).in_(tuple(query_aliases)))

    category_id_column = getattr(ticket_model, "categoria_id", None)
    if category_id_column is not None and scope.ids:
        clauses.append(category_id_column.in_(tuple(scope.ids)))

    if not clauses:
        return query.filter(false())
    return query.filter(or_(*clauses))
