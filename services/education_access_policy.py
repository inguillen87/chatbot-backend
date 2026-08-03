"""Server-authoritative capability policy for the education vertical.

Capabilities are grants stored on the persisted user record.  Request payloads
and JWT role claims never participate in this policy.  Tenant owners and
tenant administrators keep their administrative grant, while employees must
receive the narrow capability required by each surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from utils.roles import (
    ROLE_EMPLEADO,
    ROLE_SUPERADMIN,
    ROLE_TENANT_ADMIN,
    canonical_role,
    is_authorized_superadmin_user,
)


EDUCATION_SETTINGS_READ = "education.settings.read"
EDUCATION_SETTINGS_WRITE = "education.settings.write"
EDUCATION_DIRECTORY_READ = "education.directory.read"
EDUCATION_DIRECTORY_WRITE = "education.directory.write"
EDUCATION_ANALYTICS_READ = "education.analytics.read"
EDUCATION_CASES_READ = "education.cases.read"
EDUCATION_CASES_WRITE = "education.cases.write"
EDUCATION_CASES_MANAGE = "education.cases.manage"
EDUCATION_GUARDIANS_READ = "education.guardians.read"
EDUCATION_GUARDIANS_VERIFY = "education.guardians.verify"
EDUCATION_GUARDIANS_LINK = "education.guardians.link"


@dataclass(frozen=True)
class EducationAccessDecision:
    allowed: bool
    reason_code: str | None = None
    missing_capabilities: tuple[str, ...] = ()


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


def education_capabilities(user: Any) -> set[str]:
    """Read server-side capabilities assigned to an education employee."""

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


def missing_education_capabilities(user: Any, *required: str) -> list[str]:
    """Return missing grants; tenant admins still require a tenant boundary."""

    role = canonical_role(getattr(user, "rol", None))
    if role in {ROLE_TENANT_ADMIN, ROLE_SUPERADMIN}:
        return []

    normalized_required = [
        str(item).strip().lower()
        for item in required
        if str(item).strip()
    ]
    granted = education_capabilities(user)
    return [
        capability
        for capability in normalized_required
        if capability not in granted and "*" not in granted
    ]


def is_education_tenant_owner(user: Any, tenant: Any) -> bool:
    """Return whether ``user`` is the server-side owner of ``tenant``."""

    user_id = getattr(user, "id", None)
    return bool(
        user_id
        and user_id
        in {
            getattr(tenant, "municipio_id", None),
            getattr(tenant, "pyme_id", None),
        }
    )


def is_education_tenant_member(user: Any, tenant: Any) -> bool:
    """Resolve one authoritative membership and require an active tenant."""

    if user is None or tenant is None or getattr(tenant, "is_active", True) is False:
        return False
    try:
        # Local import avoids coupling the policy module to auth initialization.
        from utils.auth_helpers import auth_tenant_for_user

        resolved = auth_tenant_for_user(user)
    except Exception:
        return False
    return bool(resolved is not None and int(resolved.id) == int(tenant.id))


def decide_education_admin_access(
    user: Any,
    tenant: Any,
    *required: str,
    allow_platform_admin: bool = True,
) -> EducationAccessDecision:
    """Authorize an education administrative surface.

    Ordinary users and leads remain on the separate family/citizen surfaces,
    even if attacker-controlled metadata happens to contain capability-looking
    strings.  A platform administrator is accepted only after the central
    allowlist check; route code must still select one explicit active tenant.
    """

    if user is None or tenant is None or getattr(tenant, "is_active", True) is False:
        return EducationAccessDecision(False, "education_tenant_context_required")

    role = canonical_role(getattr(user, "rol", None))
    if role == ROLE_SUPERADMIN:
        if allow_platform_admin and is_authorized_superadmin_user(user):
            return EducationAccessDecision(True)
        return EducationAccessDecision(False, "education_admin_role_required")

    if not is_education_tenant_member(user, tenant):
        return EducationAccessDecision(False, "education_cross_tenant_denied")

    # Ownership is a durable server-side relationship.  It preserves older
    # tenants whose owner row predates role normalization.
    if is_education_tenant_owner(user, tenant):
        return EducationAccessDecision(True)

    if role == ROLE_TENANT_ADMIN:
        return EducationAccessDecision(True)
    if role != ROLE_EMPLEADO:
        return EducationAccessDecision(False, "education_admin_role_required")

    missing = tuple(missing_education_capabilities(user, *required))
    if missing:
        return EducationAccessDecision(
            False,
            "education_capability_required",
            missing,
        )
    return EducationAccessDecision(True)


def education_staff_can_be_assigned(
    user: Any,
    tenant: Any,
    *required: str,
) -> bool:
    """Require same-tenant privileged staff for rosters and case assignment."""

    return decide_education_admin_access(
        user,
        tenant,
        *required,
        allow_platform_admin=False,
    ).allowed
