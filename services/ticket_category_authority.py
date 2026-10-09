"""Tenant-bound category authority for legacy ticket read models."""

from __future__ import annotations

from typing import Any, Iterable

from models import CategoriaTicket
from services.territorial_evidence import canonicalize_territorial_category


CONTRACT_VERSION = "ticket.category_authority.v1"


def _text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _guidance(*, verified: bool, conflict: bool, reason_code: str) -> dict[str, str | None]:
    if reason_code == "ticket_tenant_mismatch":
        return {
            "message": "No se pudo verificar la categoría dentro de esta organización.",
            "recovery_text": "Solicitá a supervisión revisar el ámbito del caso antes de clasificarlo o derivarlo.",
            "action_hint": "review_ticket_category",
        }
    if conflict:
        message = "La categoría del catálogo y la categoría registrada no coinciden."
    elif verified:
        return {
            "message": "La categoría del caso está verificada.",
            "recovery_text": None,
            "action_hint": None,
        }
    elif reason_code == "category_not_found_in_tenant_catalog":
        message = "La referencia de categoría no se encontró en el catálogo de esta organización."
    else:
        message = "La categoría registrada no tiene una referencia verificada en el catálogo de esta organización."
    return {
        "message": message,
        "recovery_text": "Solicitá a supervisión revisar la clasificación del caso y el catálogo de la organización antes de derivarlo.",
        "action_hint": "review_ticket_category",
    }


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
        # A tenant catalog entry is the authority itself. Global keyword or
        # substring normalization must never rename a verified catalog label.
        authoritative = _text(getattr(category, "nombre", None)) if category else None
        alias_evidence = canonicalize_territorial_category(persisted)
        alias_category = alias_evidence["category"]
        alias_verified = same_tenant and alias_category == "luminarias" and bool(persisted)
        if not authoritative and alias_verified:
            authoritative = "Luminarias"
        verified = bool(authoritative)
        persisted_normalized = canonicalize_territorial_category(persisted)["category"] if persisted else None
        authoritative_normalized = canonicalize_territorial_category(authoritative)["category"] if authoritative else None
        conflict = bool(
            verified
            and persisted_normalized
            and persisted_normalized != authoritative_normalized
        )
        if not same_tenant:
            reason_code = "ticket_tenant_mismatch"
            source = "persisted_category_unverified"
        elif category and verified:
            reason_code = "verified_tenant_category"
            source = "tenant_category_catalog"
        elif alias_verified:
            reason_code = "verified_persisted_exact_alias"
            source = "persisted_category_exact_alias"
        elif not isinstance(category_id, int) or category_id <= 0:
            reason_code = "category_id_missing_and_alias_unverified"
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
            **_guidance(verified=verified, conflict=conflict, reason_code=reason_code),
        }
    return resolved


def resolve_municipio_category_authority(ticket: Any) -> dict[str, Any]:
    return build_municipio_category_authorities(
        [ticket], tenant_id=getattr(ticket, "tenant_id", None)
    )[int(ticket.id)]
