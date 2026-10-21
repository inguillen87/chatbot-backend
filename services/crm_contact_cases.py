"""Exact, tenant-scoped cases published in one CRM contact history.

The resolver deliberately does not join by phone, email, free text or ticket
number.  A ticket is related to a contact only through a persisted legacy user
identity or an interaction event carrying the complete exact ticket identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode
from typing import Any

from sqlalchemy import and_, func, or_

from models import MunicipioTicket, PymeTicket, TenantProfile, TenantTicket, User
from models_memory import Contact, InteractionEvent
from services.crm_output_safety import redact_crm_sensitive_text
from services.employee_ticket_access import apply_employee_ticket_category_scope
from services.tenant_ticket_scope import scoped_municipio_ticket_query


CRM_CONTACT_CASES_CONTRACT_VERSION = "crm.contact_cases.v1"
CRM_CONTACT_CASES_LIMIT = 100
CRM_CONTACT_CASE_EVENT_IDENTITY_LIMIT = 1000


@dataclass(frozen=True)
class CrmContactCasesResult:
    cases: list[dict[str, Any]]
    total: int
    truncated: bool
    total_is_exact: bool

_CASE_MODELS = {
    "TenantTicket": TenantTicket,
    "MunicipioTicket": MunicipioTicket,
    "PymeTicket": PymeTicket,
}


def _iso_or_none(value: Any) -> str | None:
    return value.isoformat() if value else None


def _positive_opaque_id(value: Any) -> int | None:
    """Parse a backing numeric ID without accepting lossy aliases.

    IDs leave this service as opaque strings. The current backing tables still
    use integers, so floats, booleans, whitespace and zero-padded aliases are
    rejected rather than normalized to a potentially different identity.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if not isinstance(value, str) or value != value.strip():
        return None
    if not value or not value.isascii() or not value.isdigit() or value.startswith("0"):
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _contact_legacy_user_id(contact: Contact) -> int | None:
    preferences = contact.preferences if isinstance(contact.preferences, dict) else {}
    return _positive_opaque_id(preferences.get("legacy_user_id"))


def _event_ticket_identity(
    metadata: Any,
    *,
    contact_legacy_user_id: int | None,
) -> tuple[str, int] | None:
    """Return one complete and internally consistent event identity."""

    if not isinstance(metadata, dict):
        return None
    source_present = metadata.get("source_model") not in (None, "")
    ticket_present = metadata.get("ticket_id") not in (None, "")
    if not source_present or not ticket_present:
        return None

    source_model = metadata.get("source_model")
    if not isinstance(source_model, str) or source_model not in _CASE_MODELS:
        return None
    ticket_id = _positive_opaque_id(metadata.get("ticket_id"))
    if ticket_id is None:
        return None

    # Aliases are never alternative evidence. If present, they must agree with
    # the authoritative pair or the event relation is quarantined.
    for alias in ("ticket_source_model", "legacy_model"):
        alias_value = metadata.get(alias)
        if alias_value not in (None, "") and alias_value != source_model:
            return None
    for alias in ("ticketId", "legacy_id"):
        alias_value = metadata.get(alias)
        if alias_value not in (None, "") and _positive_opaque_id(alias_value) != ticket_id:
            return None

    event_legacy_user_id = metadata.get("legacy_user_id")
    if event_legacy_user_id not in (None, ""):
        normalized_event_user_id = _positive_opaque_id(event_legacy_user_id)
        if normalized_event_user_id is None:
            return None
        if (
            contact_legacy_user_id is not None
            and normalized_event_user_id != contact_legacy_user_id
        ):
            return None

    return source_model, ticket_id


def _event_ticket_ids(
    tenant: TenantProfile,
    contact: Contact,
    *,
    contact_legacy_user_id: int | None,
) -> tuple[dict[str, set[int]], bool]:
    refs = {source_model: set() for source_model in _CASE_MODELS}
    last_seen = func.max(InteractionEvent.created_at).label("last_seen")
    rows = (
        InteractionEvent.query.with_entities(InteractionEvent.metadata_payload, last_seen)
        .filter(
            InteractionEvent.tenant_id == tenant.id,
            InteractionEvent.contact_id == contact.id,
            InteractionEvent.metadata_payload["source_model"].as_string().isnot(None),
            InteractionEvent.metadata_payload["ticket_id"].as_string().isnot(None),
        )
        .group_by(InteractionEvent.metadata_payload)
        .order_by(last_seen.desc())
        .limit(CRM_CONTACT_CASE_EVENT_IDENTITY_LIMIT + 1)
        .all()
    )
    truncated = len(rows) > CRM_CONTACT_CASE_EVENT_IDENTITY_LIMIT
    for metadata, _ in rows[:CRM_CONTACT_CASE_EVENT_IDENTITY_LIMIT]:
        identity = _event_ticket_identity(
            metadata,
            contact_legacy_user_id=contact_legacy_user_id,
        )
        if identity is not None:
            source_model, ticket_id = identity
            refs[source_model].add(ticket_id)
    return refs, truncated


def _case_base_query(source_model: str, tenant: TenantProfile, current_user: User):
    model = _CASE_MODELS[source_model]
    if source_model == "TenantTicket":
        query = TenantTicket.query.filter(TenantTicket.tenant_id == tenant.id)
    elif source_model == "MunicipioTicket":
        query = scoped_municipio_ticket_query(tenant)
    else:
        # CRM contact history requires an explicit tenant binding for PYME
        # cases; rubro/owner fallback would be too broad for identity linkage.
        query = PymeTicket.query.filter(PymeTicket.tenant_id == tenant.id)
    return apply_employee_ticket_category_scope(query, current_user, model)


def _case_datetimes(source_model: str, ticket: Any) -> tuple[datetime | None, datetime | None]:
    if source_model == "TenantTicket":
        return getattr(ticket, "created_at", None), getattr(ticket, "updated_at", None)
    if source_model == "MunicipioTicket":
        created_at = getattr(ticket, "fecha", None)
        return created_at, getattr(ticket, "ultima_actividad", None) or created_at
    created_at = getattr(ticket, "fecha", None)
    return created_at, created_at


def _case_order_columns(source_model: str) -> tuple[Any, ...]:
    if source_model == "TenantTicket":
        return TenantTicket.updated_at, TenantTicket.created_at, TenantTicket.id
    if source_model == "MunicipioTicket":
        return MunicipioTicket.ultima_actividad, MunicipioTicket.fecha, MunicipioTicket.id
    return PymeTicket.fecha, PymeTicket.id


def _case_title(source_model: str, ticket: Any) -> str:
    extra = getattr(ticket, "datos_extra", None)
    extra = extra if isinstance(extra, dict) else {}
    for candidate in (
        extra.get("title"),
        getattr(ticket, "asunto", None),
        getattr(ticket, "pregunta", None),
        getattr(ticket, "descripcion", None),
        getattr(ticket, "categoria", None),
    ):
        value = redact_crm_sensitive_text(candidate)
        if value:
            return str(value).strip()[:240]
    return f"Caso {source_model}"


def _serialize_case(source_model: str, ticket: Any, *, tenant: TenantProfile) -> dict[str, Any]:
    ticket_id = str(ticket.id)
    created_at, updated_at = _case_datetimes(source_model, ticket)
    extra = getattr(ticket, "datos_extra", None)
    extra = extra if isinstance(extra, dict) else {}
    channel = (
        getattr(ticket, "origen", None)
        or getattr(ticket, "canal_ingreso", None)
        or extra.get("channel")
    )
    detail_query = urlencode({
        "tab": "tickets",
        "source_model": source_model,
        "ticket_id": ticket_id,
        "tenant_slug": tenant.slug,
        "tenant": tenant.slug,
    })
    return {
        "case_key": f"{source_model}:{ticket_id}",
        "source_model": source_model,
        "ticket_id": ticket_id,
        "tenant_slug": tenant.slug,
        "title": _case_title(source_model, ticket),
        "category": redact_crm_sensitive_text(getattr(ticket, "categoria", None)),
        "status": redact_crm_sensitive_text(getattr(ticket, "estado", None)),
        "channel": redact_crm_sensitive_text(channel),
        "created_at": _iso_or_none(created_at),
        "updated_at": _iso_or_none(updated_at),
        "detail_href": f"/perfil?{detail_query}",
    }


def _sort_key(item: dict[str, Any]) -> float:
    raw_timestamp = item.get("updated_at") or item.get("created_at")
    if not raw_timestamp:
        return float("-inf")
    try:
        parsed = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def build_crm_contact_cases(
    *,
    tenant: TenantProfile,
    contact: Contact,
    current_user: User,
) -> CrmContactCasesResult:
    """Build the category-authorized, exact case set for one contact."""

    legacy_user_id = _contact_legacy_user_id(contact)
    event_ticket_ids, event_identities_truncated = _event_ticket_ids(
        tenant,
        contact,
        contact_legacy_user_id=legacy_user_id,
    )
    cases: dict[tuple[str, int], dict[str, Any]] = {}
    total = 0

    for source_model, model in _CASE_MODELS.items():
        explicit_ids = event_ticket_ids[source_model]
        predicates = []
        if legacy_user_id is not None:
            predicates.append(model.user_id == legacy_user_id)
        if explicit_ids:
            predicates.append(model.id.in_(tuple(explicit_ids)))
        if not predicates:
            continue

        query = _case_base_query(source_model, tenant, current_user).filter(or_(*predicates))
        if explicit_ids and legacy_user_id is not None:
            query = query.filter(
                ~and_(
                    model.id.in_(tuple(explicit_ids)),
                    model.user_id.isnot(None),
                    model.user_id != legacy_user_id,
                )
            )

        total += int(query.order_by(None).count())
        order_columns = _case_order_columns(source_model)
        tickets = query.order_by(*(column.desc() for column in order_columns)).limit(
            CRM_CONTACT_CASES_LIMIT
        ).all()
        for ticket in tickets:
            ticket_id = _positive_opaque_id(getattr(ticket, "id", None))
            if ticket_id is None:
                continue

            cases[(source_model, ticket_id)] = _serialize_case(
                source_model,
                ticket,
                tenant=tenant,
            )

    sorted_cases = sorted(cases.values(), key=_sort_key, reverse=True)[:CRM_CONTACT_CASES_LIMIT]
    return CrmContactCasesResult(
        cases=sorted_cases,
        total=total,
        truncated=event_identities_truncated or total > CRM_CONTACT_CASES_LIMIT,
        total_is_exact=not event_identities_truncated,
    )
