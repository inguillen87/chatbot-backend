"""Capability policy shared by authenticated survey administration surfaces."""

from __future__ import annotations

from typing import Any

from utils.roles import ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, canonical_role


SURVEY_EXPORT_CAPABILITY = "survey.export"
SURVEY_PII_READ_CAPABILITY = "survey.pii.read"
SURVEY_GOVERNANCE_MANAGE_CAPABILITY = "survey.governance.manage"


def _flatten_capability_values(raw: Any) -> set[str]:
    if raw in (None, ""):
        return set()
    if isinstance(raw, str):
        return {item.strip().lower() for item in raw.split(",") if item.strip()}
    if isinstance(raw, dict):
        values = {
            str(key).strip().lower()
            for key, enabled in raw.items()
            if enabled and str(key).strip()
        }
        return values
    if isinstance(raw, (list, tuple, set, frozenset)):
        values: set[str] = set()
        for item in raw:
            values.update(_flatten_capability_values(item))
        return values
    normalized = str(raw).strip().lower()
    return {normalized} if normalized else set()


def survey_capabilities(user: Any) -> set[str]:
    """Return explicit survey capabilities stored on an employee profile."""

    metadata = getattr(user, "accesibilidad", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    employee_scope = metadata.get("employee_scope")
    employee_scope = employee_scope if isinstance(employee_scope, dict) else {}
    legacy_scope = getattr(user, "scope", None)
    legacy_scope = legacy_scope if isinstance(legacy_scope, dict) else {}

    capabilities: set[str] = set()
    for container in (metadata, employee_scope, legacy_scope):
        for key in ("permissions", "permisos", "capabilities", "scopes"):
            capabilities.update(_flatten_capability_values(container.get(key)))
    return capabilities


def missing_survey_capabilities(user: Any, *required: str) -> list[str]:
    """Return missing capabilities, preserving tenant-owner compatibility.

    Tenant admins and authorized superadmins are still subject to each route's
    tenant authorization boundary, but do not need employee-scope grants.
    """

    role = canonical_role(getattr(user, "rol", None))
    if role in {ROLE_TENANT_ADMIN, ROLE_SUPERADMIN}:
        return []

    normalized_required = [
        str(item).strip().lower()
        for item in required
        if str(item).strip()
    ]
    granted = survey_capabilities(user)
    return [
        capability
        for capability in normalized_required
        if capability not in granted and "*" not in granted
    ]
