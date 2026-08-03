"""Authoritative tenant scope for legacy municipal tickets.

``MunicipioTicket.tenant_id`` was introduced after the original owner-based
schema.  A null tenant can therefore be read through its legacy
``municipio_id`` only when that owner maps to exactly one tenant profile.
Ambiguous and orphaned rows stay quarantined until an operator assigns them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from sqlalchemy import and_, false, or_

from models import MunicipioTicket, TenantProfile, db


TenantOwnerResolutionStatus = Literal["unique", "ambiguous", "orphan"]


class TicketTenantScopeError(ValueError):
    """Raised when a ticket scope cannot be proven without guessing."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TenantOwnerResolution:
    status: TenantOwnerResolutionStatus
    owner_id: int
    tenant: TenantProfile | None
    candidate_ids: tuple[int, ...]


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise TicketTenantScopeError(
            f"ticket_{field}_invalid",
            f"Municipal ticket {field} must be a positive integer.",
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TicketTenantScopeError(
            f"ticket_{field}_invalid",
            f"Municipal ticket {field} must be a positive integer.",
        ) from exc
    if parsed <= 0:
        raise TicketTenantScopeError(
            f"ticket_{field}_invalid",
            f"Municipal ticket {field} must be a positive integer.",
        )
    return parsed


def tenant_owner_ids(tenant: TenantProfile | None) -> tuple[int, ...]:
    """Return the distinct, valid owner IDs declared by one tenant."""

    values: list[int] = []
    if tenant is None:
        return tuple()
    for raw_value in (
        getattr(tenant, "municipio_id", None),
        getattr(tenant, "pyme_id", None),
    ):
        if raw_value is None:
            continue
        try:
            value = _positive_int(raw_value, field="owner_id")
        except TicketTenantScopeError:
            return tuple()
        if value not in values:
            values.append(value)
    return tuple(values)


def tenant_unique_legacy_owner_id(tenant: TenantProfile | None) -> int | None:
    """Return the sole legacy owner only when it resolves back to ``tenant``.

    Legacy rows in several CRM tables predate ``tenant_id`` and are linked via
    an owner user.  Merely seeing one owner value on the tenant is not enough:
    older data can contain the same owner on multiple tenant profiles.  Those
    rows must remain quarantined instead of becoming a cross-tenant wildcard.
    """

    owners = tenant_owner_ids(tenant)
    if tenant is None or getattr(tenant, "id", None) is None or len(owners) != 1:
        return None
    try:
        resolution = resolve_unique_tenant_for_owner(owners[0])
    except TicketTenantScopeError:
        return None
    if (
        resolution.status != "unique"
        or resolution.tenant is None
        or int(resolution.tenant.id) != int(tenant.id)
    ):
        return None
    return owners[0]


def resolve_unique_tenant_for_owner(owner_id: Any) -> TenantOwnerResolution:
    """Resolve an owner without ever choosing an arbitrary first tenant."""

    normalized_owner_id = _positive_int(owner_id, field="owner_id")
    candidates = (
        TenantProfile.query.filter(
            or_(
                TenantProfile.municipio_id == normalized_owner_id,
                TenantProfile.pyme_id == normalized_owner_id,
            )
        )
        .order_by(TenantProfile.id.asc())
        .limit(2)
        .all()
    )
    candidate_ids = tuple(int(candidate.id) for candidate in candidates)
    if len(candidates) == 1:
        return TenantOwnerResolution(
            status="unique",
            owner_id=normalized_owner_id,
            tenant=candidates[0],
            candidate_ids=candidate_ids,
        )
    if candidates:
        return TenantOwnerResolution(
            status="ambiguous",
            owner_id=normalized_owner_id,
            tenant=None,
            candidate_ids=candidate_ids,
        )
    return TenantOwnerResolution(
        status="orphan",
        owner_id=normalized_owner_id,
        tenant=None,
        candidate_ids=tuple(),
    )


def municipio_ticket_scope_filter(tenant: TenantProfile | None):
    """Build the only supported tenant predicate for ``MunicipioTicket``."""

    if tenant is None or getattr(tenant, "id", None) is None:
        return false()
    try:
        tenant_id = _positive_int(tenant.id, field="tenant_id")
    except TicketTenantScopeError:
        return false()

    explicit_scope = MunicipioTicket.tenant_id == tenant_id
    owners = tenant_owner_ids(tenant)
    if len(owners) != 1:
        return explicit_scope

    resolution = resolve_unique_tenant_for_owner(owners[0])
    if (
        resolution.status != "unique"
        or resolution.tenant is None
        or int(resolution.tenant.id) != tenant_id
    ):
        return explicit_scope

    return or_(
        explicit_scope,
        and_(
            MunicipioTicket.tenant_id.is_(None),
            MunicipioTicket.municipio_id == owners[0],
        ),
    )


def scoped_municipio_ticket_query(tenant: TenantProfile | None, query=None):
    """Apply the authoritative scope to a municipal ticket query."""

    base_query = query if query is not None else MunicipioTicket.query
    return base_query.filter(municipio_ticket_scope_filter(tenant))


def municipio_ticket_belongs_to_tenant(
    ticket: MunicipioTicket | None,
    tenant: TenantProfile | None,
) -> bool:
    """Check access without allowing owner fallback over an explicit tenant."""

    if ticket is None or tenant is None:
        return False
    try:
        tenant_id = _positive_int(getattr(tenant, "id", None), field="tenant_id")
    except TicketTenantScopeError:
        return False

    ticket_tenant_id = getattr(ticket, "tenant_id", None)
    if ticket_tenant_id is not None:
        try:
            return _positive_int(ticket_tenant_id, field="tenant_id") == tenant_id
        except TicketTenantScopeError:
            return False

    owner_id = getattr(ticket, "municipio_id", None)
    if owner_id is None:
        return False
    try:
        resolution = resolve_unique_tenant_for_owner(owner_id)
    except TicketTenantScopeError:
        return False
    return bool(
        resolution.status == "unique"
        and resolution.tenant is not None
        and int(resolution.tenant.id) == tenant_id
    )


def resolve_municipio_ticket_access_tenant(
    ticket: MunicipioTicket | None,
) -> TenantProfile:
    """Resolve the tenant that may authorize access to one ticket."""

    if ticket is None:
        raise TicketTenantScopeError(
            "ticket_tenant_not_found",
            "Municipal ticket tenant scope is unavailable.",
        )

    explicit_tenant_id = getattr(ticket, "tenant_id", None)
    if explicit_tenant_id is not None:
        tenant_id = _positive_int(explicit_tenant_id, field="tenant_id")
        tenant = db.session.get(TenantProfile, tenant_id)
        if tenant is None:
            raise TicketTenantScopeError(
                "ticket_tenant_not_found",
                "Municipal ticket tenant scope is unavailable.",
            )
        return tenant

    owner_id = getattr(ticket, "municipio_id", None)
    if owner_id is None:
        raise TicketTenantScopeError(
            "ticket_tenant_missing",
            "Municipal ticket tenant scope is unavailable.",
        )
    resolution = resolve_unique_tenant_for_owner(owner_id)
    if resolution.status == "ambiguous":
        raise TicketTenantScopeError(
            "ticket_tenant_ambiguous",
            "Municipal ticket tenant scope is unavailable.",
        )
    if resolution.status == "orphan" or resolution.tenant is None:
        raise TicketTenantScopeError(
            "ticket_tenant_not_found",
            "Municipal ticket tenant scope is unavailable.",
        )
    return resolution.tenant


def normalize_municipio_ticket_write_scope(
    ticket_data: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate and canonicalize the tenant/owner pair before persistence."""

    normalized = dict(ticket_data or {})
    raw_tenant_id = normalized.get("tenant_id")
    raw_owner_id = normalized.get("municipio_id")

    tenant: TenantProfile | None = None
    if raw_tenant_id is not None:
        tenant_id = _positive_int(raw_tenant_id, field="tenant_id")
        tenant = db.session.get(TenantProfile, tenant_id)
        if tenant is None:
            raise TicketTenantScopeError(
                "ticket_tenant_not_found",
                "Municipal ticket tenant scope is unavailable.",
            )
    else:
        if raw_owner_id is None:
            raise TicketTenantScopeError(
                "ticket_tenant_missing",
                "Municipal ticket tenant scope is required.",
            )
        resolution = resolve_unique_tenant_for_owner(raw_owner_id)
        if resolution.status == "ambiguous":
            raise TicketTenantScopeError(
                "ticket_tenant_ambiguous",
                "Municipal ticket tenant scope is unavailable.",
            )
        if resolution.status == "orphan" or resolution.tenant is None:
            raise TicketTenantScopeError(
                "ticket_tenant_not_found",
                "Municipal ticket tenant scope is unavailable.",
            )
        tenant = resolution.tenant

    owners = tenant_owner_ids(tenant)
    if len(owners) != 1:
        raise TicketTenantScopeError(
            "ticket_tenant_owner_invalid",
            "Municipal ticket tenant owner is unavailable.",
        )
    canonical_owner_id = owners[0]
    if raw_owner_id is not None:
        supplied_owner_id = _positive_int(raw_owner_id, field="owner_id")
        if supplied_owner_id != canonical_owner_id:
            raise TicketTenantScopeError(
                "ticket_tenant_owner_mismatch",
                "Municipal ticket tenant and owner do not match.",
            )

    normalized["tenant_id"] = int(tenant.id)
    normalized["municipio_id"] = canonical_owner_id
    return normalized


__all__ = [
    "TicketTenantScopeError",
    "TenantOwnerResolution",
    "municipio_ticket_belongs_to_tenant",
    "municipio_ticket_scope_filter",
    "normalize_municipio_ticket_write_scope",
    "resolve_municipio_ticket_access_tenant",
    "resolve_unique_tenant_for_owner",
    "scoped_municipio_ticket_query",
    "tenant_owner_ids",
    "tenant_unique_legacy_owner_id",
]
