"""Tenant-bound category authority for legacy ticket read models."""

from __future__ import annotations

from typing import Any, Iterable

from models import CategoriaTicket
from services.territorial_evidence import canonicalize_territorial_category
from utils.ticket_utils import normalize_category


CONTRACT_VERSION = "ticket.category_authority.v1"


def _text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def build_municipio_category_authorities(
    tickets: Iterable[Any],
    *,
    tenant_id: int | None,
) -> dict[int, dict[str, Any]]:
    """Resolve category IDs in one tenant-filtered query (safe for list views)."""

    ticket_list = list(tickets)
    category_ids = {
        int(ticket.categoria_id)
        for ticket in ticket_list
        if isinstance(getattr(ticket, "categoria_id", None), int)
        and ticket.categoria_id > 0
        and tenant_id is not None
        and getattr(ticket, "tenant_id", None) == tenant_id
    }
    categories = {
        int(category.id): category
        for category in (
            CategoriaTicket.query.filter(
                CategoriaTicket.tenant_id == tenant_id,
                CategoriaTicket.id.in_(category_ids),
            ).all()
            if tenant_id is not None and category_ids
            else []
        )
    }

    resolved: dict[int, dict[str, Any]] = {}
    for ticket in ticket_list:
        persisted = _text(getattr(ticket, "categoria", None))
        category_id = getattr(ticket, "categoria_id", None)
        same_tenant = tenant_id is not None and getattr(ticket, "tenant_id", None) == tenant_id
        category = categories.get(category_id) if same_tenant else None
        authoritative = normalize_category(_text(getattr(category, "nombre", None))) if category else None
        alias_evidence = canonicalize_territorial_category(persisted)
        alias_category = alias_evidence["category"]
        alias_verified = alias_category == "luminarias" and bool(persisted)
        if not authoritative and alias_verified:
            authoritative = normalize_category(alias_category) or alias_category
        verified = bool(authoritative)
        persisted_normalized = normalize_category(persisted)
        conflict = bool(
            verified
            and persisted_normalized
            and persisted_normalized.casefold() != authoritative.casefold()
        )
        if category and verified:
            reason_code = "verified_tenant_category"
            source = "tenant_category_catalog"
        elif alias_verified:
            reason_code = "verified_persisted_exact_alias"
            source = "persisted_category_exact_alias"
        elif not isinstance(category_id, int) or category_id <= 0:
            reason_code = "category_id_missing_and_alias_unverified"
            source = "persisted_category_unverified"
        elif not same_tenant:
            reason_code = "ticket_tenant_mismatch"
            source = "persisted_category_unverified"
        else:
            reason_code = "category_not_found_in_tenant_catalog"
            source = "persisted_category_unverified"

        resolved[int(ticket.id)] = {
            "contract_version": CONTRACT_VERSION,
            "verified": verified,
            "source": source,
            "reason_code": reason_code,
            "category_id": category_id,
            "authoritative_category": authoritative,
            "persisted_category": persisted,
            "conflict": conflict,
        }
    return resolved


def resolve_municipio_category_authority(ticket: Any) -> dict[str, Any]:
    return build_municipio_category_authorities(
        [ticket], tenant_id=getattr(ticket, "tenant_id", None)
    )[int(ticket.id)]
