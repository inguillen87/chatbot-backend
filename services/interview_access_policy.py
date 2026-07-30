"""Capability policy for tenant-scoped assessment and interview operations."""

from __future__ import annotations

from typing import Any

from utils.roles import ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, canonical_role


INTERVIEW_PROGRAMS_MANAGE = "interviews.programs.manage"
INTERVIEW_CASES_CREATE = "interviews.cases.create"
INTERVIEW_CASES_READ = "interviews.cases.read"
INTERVIEW_SESSIONS_CREATE = "interviews.sessions.create"
INTERVIEW_SESSIONS_CONDUCT = "interviews.sessions.conduct"
INTERVIEW_EVIDENCE_WRITE = "interviews.evidence.write"


def _flatten_capability_values(raw: Any) -> set[str]:
    if raw in (None, ""):
        return set()
    if isinstance(raw, str):
        return {item.strip().lower() for item in raw.split(",") if item.strip()}
    if isinstance(raw, dict):
        return {
            str(key).strip().lower()
            for key, enabled in raw.items()
            if enabled and str(key).strip()
        }
    if isinstance(raw, (list, tuple, set, frozenset)):
        values: set[str] = set()
        for item in raw:
            values.update(_flatten_capability_values(item))
        return values
    normalized = str(raw).strip().lower()
    return {normalized} if normalized else set()


def interview_capabilities(user: Any) -> set[str]:
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


def _is_tenant_owner(user: Any, tenant: Any) -> bool:
    user_id = getattr(user, "id", None)
    return bool(
        user_id
        and user_id
        in {
            getattr(tenant, "municipio_id", None),
            getattr(tenant, "pyme_id", None),
        }
    )


def missing_interview_capabilities(user: Any, tenant: Any, *required: str) -> list[str]:
    """Return missing grants after a separate tenant authorization check."""

    role = canonical_role(getattr(user, "rol", None))
    if role in {ROLE_TENANT_ADMIN, ROLE_SUPERADMIN} or _is_tenant_owner(user, tenant):
        return []

    normalized_required = [
        str(item).strip().lower() for item in required if str(item).strip()
    ]
    granted = interview_capabilities(user)
    return [
        capability
        for capability in normalized_required
        if capability not in granted and "*" not in granted
    ]

