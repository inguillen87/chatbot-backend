from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flask import current_app
from sqlalchemy import func, or_

from models import CatalogoItem, TenantProfile, User, db
from services.plan_access import tenant_is_demo_context
from utils.roles import normalize_tenant_type


class DemoTenantScopeRepairError(RuntimeError):
    def __init__(self, reason_code: str, *, status_code: int = 409):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.status_code = status_code


def _normalized_slug(value: Any) -> str:
    return str(value or "").strip().lower()


def _configured_demo_entries_for_tenant(tenant: TenantProfile | None) -> list[dict[str, Any]]:
    if tenant is None:
        return []
    tenant_slug = _normalized_slug(getattr(tenant, "slug", None))
    matches: list[dict[str, Any]] = []
    for raw_entry in current_app.config.get("DEMO_RUBROS") or []:
        if not isinstance(raw_entry, Mapping):
            continue
        entry = dict(raw_entry)
        configured_slugs = {
            _normalized_slug(entry.get(key))
            for key in ("tenant_slug", "slug", "key", "rubro_clave")
        }
        if tenant_slug and tenant_slug in configured_slugs:
            matches.append(entry)
    default_slug = _normalized_slug(current_app.config.get("DEFAULT_DEMO_TENANT_SLUG"))
    if tenant_slug and tenant_slug == default_slug and not matches:
        matches.append({"tenant_slug": default_slug})
    return matches


def server_demo_tenant_evidence(tenant: TenantProfile | None) -> dict[str, Any]:
    """Return server-owned demo evidence without trusting request flags."""

    if tenant is None:
        return {"eligible": False, "sources": [], "configured_owner_user_ids": []}
    entries = _configured_demo_entries_for_tenant(tenant)
    configured_owner_ids: set[int] = set()
    for entry in entries:
        try:
            owner_id = int(entry.get("user_id"))
        except (TypeError, ValueError):
            continue
        if owner_id > 0:
            configured_owner_ids.add(owner_id)
    sources = []
    if tenant_is_demo_context(tenant):
        sources.append("tenant_config")
    if entries:
        sources.append("demo_registry")
    return {
        "eligible": bool(sources),
        "sources": sources,
        "configured_owner_user_ids": sorted(configured_owner_ids),
    }


def is_server_demo_tenant(tenant: TenantProfile | None) -> bool:
    return bool(server_demo_tenant_evidence(tenant).get("eligible"))


def _owner_reference_count(owner_id: int | None) -> int:
    if not owner_id:
        return 0
    return int(
        TenantProfile.query.filter(
            or_(
                TenantProfile.municipio_id == owner_id,
                TenantProfile.pyme_id == owner_id,
            )
        ).count()
    )


def _catalog_counts(tenant_id: int) -> list[dict[str, int]]:
    rows = (
        db.session.query(CatalogoItem.user_id, func.count(CatalogoItem.id))
        .filter(CatalogoItem.tenant_id == tenant_id)
        .group_by(CatalogoItem.user_id)
        .order_by(CatalogoItem.user_id.asc())
        .all()
    )
    return [
        {"user_id": int(user_id), "count": int(count)}
        for user_id, count in rows
    ]


def build_demo_tenant_scope_diagnostic(tenant: TenantProfile) -> dict[str, Any]:
    owner_id = getattr(tenant, "municipio_id", None) or getattr(tenant, "pyme_id", None)
    owner = db.session.get(User, owner_id) if owner_id else None
    evidence = server_demo_tenant_evidence(tenant)
    catalog_counts = _catalog_counts(int(tenant.id))
    catalog_owner_ids = [row["user_id"] for row in catalog_counts]
    return {
        "contract_version": "superadmin.demo_tenant_scope_diagnostic.v1",
        "tenant": {
            "id": int(tenant.id),
            "slug": tenant.slug,
            "municipio_id": tenant.municipio_id,
            "pyme_id": tenant.pyme_id,
        },
        "owner": {
            "user_id": int(owner_id) if owner_id else None,
            "tenant_id": getattr(owner, "tenant_id", None),
            "tenant_reference_count": _owner_reference_count(owner_id),
        },
        "catalog_items_by_user_id": catalog_counts,
        "catalog_item_count": sum(row["count"] for row in catalog_counts),
        "catalog_owner_scope_matches": bool(
            owner_id and catalog_owner_ids and all(user_id == int(owner_id) for user_id in catalog_owner_ids)
        ),
        "demo_guard": evidence,
    }


def _select_repair_owner(
    diagnostic: dict[str, Any],
    requested_owner_user_id: Any,
) -> int:
    allowed_ids = {
        int(row["user_id"])
        for row in diagnostic.get("catalog_items_by_user_id") or []
    }
    current_owner_id = (diagnostic.get("owner") or {}).get("user_id")
    if current_owner_id:
        allowed_ids.add(int(current_owner_id))
    configured_ids = {
        int(value)
        for value in (diagnostic.get("demo_guard") or {}).get("configured_owner_user_ids") or []
    }
    allowed_ids.update(configured_ids)

    if requested_owner_user_id not in (None, ""):
        try:
            selected = int(requested_owner_user_id)
        except (TypeError, ValueError) as exc:
            raise DemoTenantScopeRepairError("demo_owner_user_id_invalid", status_code=400) from exc
        if selected <= 0 or selected not in allowed_ids:
            raise DemoTenantScopeRepairError("demo_owner_outside_repair_scope", status_code=403)
        return selected

    if len(configured_ids) == 1:
        return next(iter(configured_ids))
    catalog_ids = {
        int(row["user_id"])
        for row in diagnostic.get("catalog_items_by_user_id") or []
    }
    if len(catalog_ids) == 1:
        return next(iter(catalog_ids))
    if not catalog_ids and current_owner_id:
        return int(current_owner_id)
    raise DemoTenantScopeRepairError("demo_owner_ambiguous")


def _assert_owner_not_bound_to_real_tenant(owner_id: int, target_tenant_id: int) -> None:
    tenants = TenantProfile.query.filter(
        or_(
            TenantProfile.municipio_id == owner_id,
            TenantProfile.pyme_id == owner_id,
        )
    ).all()
    owner = db.session.get(User, owner_id)
    if owner and owner.tenant_id and int(owner.tenant_id) != int(target_tenant_id):
        linked = db.session.get(TenantProfile, owner.tenant_id)
        if linked is not None and not is_server_demo_tenant(linked):
            raise DemoTenantScopeRepairError("demo_owner_bound_to_real_tenant", status_code=403)
    for tenant in tenants:
        if int(tenant.id) != int(target_tenant_id) and not is_server_demo_tenant(tenant):
            raise DemoTenantScopeRepairError("demo_owner_bound_to_real_tenant", status_code=403)


def repair_demo_tenant_scope(
    tenant: TenantProfile,
    *,
    owner_user_id: Any = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    diagnostic_before = build_demo_tenant_scope_diagnostic(tenant)
    if not (diagnostic_before.get("demo_guard") or {}).get("eligible"):
        raise DemoTenantScopeRepairError("tenant_not_registered_as_demo", status_code=403)

    selected_owner_id = _select_repair_owner(diagnostic_before, owner_user_id)
    selected_owner = db.session.get(User, selected_owner_id)
    if selected_owner is None:
        raise DemoTenantScopeRepairError("demo_owner_not_found", status_code=404)
    _assert_owner_not_bound_to_real_tenant(selected_owner_id, int(tenant.id))

    tenant_type = normalize_tenant_type(getattr(tenant, "tipo", None))
    owner_field = "municipio_id" if tenant_type == "municipio" else "pyme_id"
    other_owner_field = "pyme_id" if owner_field == "municipio_id" else "municipio_id"
    changes: list[dict[str, Any]] = []

    old_owner_id = getattr(tenant, owner_field)
    if old_owner_id != selected_owner_id:
        changes.append({
            "field": f"tenant_profile.{owner_field}",
            "old": old_owner_id,
            "new": selected_owner_id,
        })
    old_other_owner_id = getattr(tenant, other_owner_field)
    if old_other_owner_id is not None:
        changes.append({
            "field": f"tenant_profile.{other_owner_field}",
            "old": old_other_owner_id,
            "new": None,
        })
    if selected_owner.tenant_id != tenant.id:
        changes.append({
            "field": "user.tenant_id",
            "user_id": selected_owner_id,
            "old": selected_owner.tenant_id,
            "new": tenant.id,
        })

    catalog_items = CatalogoItem.query.filter(CatalogoItem.tenant_id == tenant.id).all()
    catalog_reassign_count = sum(1 for item in catalog_items if item.user_id != selected_owner_id)
    if catalog_reassign_count:
        changes.append({
            "field": "catalogo_item.user_id",
            "old_owner_count": catalog_reassign_count,
            "new": selected_owner_id,
        })

    if not dry_run:
        setattr(tenant, owner_field, selected_owner_id)
        setattr(tenant, other_owner_field, None)
        selected_owner.tenant_id = tenant.id
        for item in catalog_items:
            item.user_id = selected_owner_id
        db.session.flush()

    return {
        "contract_version": "superadmin.demo_tenant_scope_repair.v1",
        "tenant_id": int(tenant.id),
        "tenant_slug": tenant.slug,
        "dry_run": bool(dry_run),
        "selected_owner_user_id": selected_owner_id,
        "change_count": len(changes),
        "changes": changes,
        "catalog_items_reassigned": catalog_reassign_count,
        "idempotent": not changes,
    }
