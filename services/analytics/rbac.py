"""RBAC helpers for the analytics blueprint."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Set

from flask import abort, current_app, g, request
from sqlalchemy import or_

from extensions import db
from models import TenantProfile, User
from services.tenant_ticket_scope import (
    TicketTenantScopeError,
    resolve_unique_tenant_for_owner,
    tenant_owner_ids,
)
from utils.roles import (
    ROLE_EMPLEADO,
    ROLE_SUPERADMIN,
    ROLE_TENANT_ADMIN,
    canonical_role,
    is_authorized_superadmin_user,
)


TENANT_NAMESPACE_OWNER = "owner"
TENANT_NAMESPACE_PROFILE = "profile"
TENANT_NAMESPACE_PLATFORM = "platform"
_TENANT_NAMESPACES = {
    TENANT_NAMESPACE_OWNER,
    TENANT_NAMESPACE_PROFILE,
    TENANT_NAMESPACE_PLATFORM,
}


def _positive_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _tenant_ref(tenant_id: str, tenant_namespace: str) -> str | None:
    namespace = str(tenant_namespace or "").strip().lower()
    if namespace not in _TENANT_NAMESPACES:
        return None
    if namespace == TENANT_NAMESPACE_PLATFORM:
        return "platform:*" if str(tenant_id or "").strip() == "*" else None
    normalized_id = _positive_int(tenant_id)
    return f"{namespace}:{normalized_id}" if normalized_id is not None else None


@dataclass(frozen=True)
class AnalyticsViewer:
    id: Optional[int]
    role: str
    allowed_tenants: Set[str]
    teams: Set[int]
    capabilities: Set[str]
    platform_admin: bool = False

    def can_access(self, tenant_id: str, tenant_namespace: str = TENANT_NAMESPACE_OWNER) -> bool:
        if self.platform_admin and "*" in self.allowed_tenants:
            return True
        tenant_ref = _tenant_ref(tenant_id, tenant_namespace)
        return bool(tenant_ref and tenant_ref in self.allowed_tenants)

    def can_capability(self, capability: str) -> bool:
        normalized = (capability or "").strip().lower()
        if not normalized:
            return True
        if self.role == "admin":
            return True
        if "*" in self.capabilities:
            return True
        if not self.capabilities:
            # Legacy fallback while CT-02 rolls out progressively.
            return True
        return normalized in self.capabilities


_ALLOWED_ROLES = {"admin", "operador", "visor"}


def _analytics_role(raw_role: str | None) -> tuple[str, str]:
    canonical = canonical_role(raw_role)
    if canonical in {ROLE_SUPERADMIN, ROLE_TENANT_ADMIN}:
        return "admin", canonical
    if canonical == ROLE_EMPLEADO:
        return "operador", canonical
    normalized = str(raw_role or "visor").strip().lower() or "visor"
    return normalized, canonical


def _actor_belongs_to_profile(user: User, tenant: TenantProfile) -> bool:
    tenant_id = _positive_int(getattr(tenant, "id", None))
    if tenant_id is None:
        return False
    if _positive_int(getattr(user, "tenant_id", None)) == tenant_id:
        return True

    user_slug = str(getattr(user, "tenant_slug", None) or "").strip().lower()
    tenant_slug = str(getattr(tenant, "slug", None) or "").strip().lower()
    if user_slug and tenant_slug and user_slug == tenant_slug:
        return True

    owners = set(tenant_owner_ids(tenant))
    if not owners:
        return False
    actor_ids = {
        value
        for value in (
            _positive_int(getattr(user, "municipio_id", None)),
            _positive_int(getattr(user, "pyme_id", None)),
            _positive_int(getattr(user, "empresa_id", None)),
        )
        if value is not None
    }
    if canonical_role(getattr(user, "rol", None)) == ROLE_TENANT_ADMIN:
        actor_id = _positive_int(getattr(user, "id", None))
        if actor_id is not None:
            actor_ids.add(actor_id)
    return bool(actor_ids & owners)


def _fallback_namespaced_refs(user) -> Set[str]:
    """Keep non-ORM unit/test principals compatible without global wildcards."""

    refs: Set[str] = set()
    profile_ref = _tenant_ref(getattr(user, "tenant_id", None), TENANT_NAMESPACE_PROFILE)
    if profile_ref:
        refs.add(profile_ref)
    for attr in ("municipio_id", "pyme_id", "empresa_id"):
        owner_ref = _tenant_ref(getattr(user, attr, None), TENANT_NAMESPACE_OWNER)
        if owner_ref:
            refs.add(owner_ref)
    return refs


def _authoritative_namespaced_refs(user) -> Set[str]:
    if not isinstance(user, User):
        return _fallback_namespaced_refs(user)

    try:
        profiles: dict[int, TenantProfile] = {}
        explicit_profile_id = _positive_int(getattr(user, "tenant_id", None))
        if explicit_profile_id is not None:
            explicit_profile = db.session.get(TenantProfile, explicit_profile_id)
            if explicit_profile is not None:
                profiles[int(explicit_profile.id)] = explicit_profile

        tenant_slug = str(getattr(user, "tenant_slug", None) or "").strip()
        if tenant_slug:
            slug_profile = TenantProfile.query.filter_by(slug=tenant_slug).one_or_none()
            if slug_profile is not None:
                profiles[int(slug_profile.id)] = slug_profile

        owner_claims = {
            value
            for value in (
                _positive_int(getattr(user, "municipio_id", None)),
                _positive_int(getattr(user, "pyme_id", None)),
                _positive_int(getattr(user, "empresa_id", None)),
            )
            if value is not None
        }
        if canonical_role(getattr(user, "rol", None)) == ROLE_TENANT_ADMIN:
            actor_id = _positive_int(getattr(user, "id", None))
            if actor_id is not None:
                owner_claims.add(actor_id)
        if owner_claims:
            owned_profiles = TenantProfile.query.filter(
                or_(
                    TenantProfile.municipio_id.in_(owner_claims),
                    TenantProfile.pyme_id.in_(owner_claims),
                )
            ).all()
            profiles.update({int(profile.id): profile for profile in owned_profiles})

        refs: Set[str] = set()
        for profile in profiles.values():
            if getattr(profile, "is_active", True) is False:
                continue
            if not _actor_belongs_to_profile(user, profile):
                continue
            profile_ref = _tenant_ref(profile.id, TENANT_NAMESPACE_PROFILE)
            if profile_ref:
                refs.add(profile_ref)
            for owner_id in tenant_owner_ids(profile):
                try:
                    resolution = resolve_unique_tenant_for_owner(owner_id)
                except TicketTenantScopeError:
                    continue
                if (
                    resolution.status == "unique"
                    and resolution.tenant is not None
                    and int(resolution.tenant.id) == int(profile.id)
                ):
                    owner_ref = _tenant_ref(owner_id, TENANT_NAMESPACE_OWNER)
                    if owner_ref:
                        refs.add(owner_ref)
        return refs
    except Exception as exc:
        current_app.logger.error(
            "[analytics.rbac] failed to resolve tenant membership actor_id=%s error_type=%s",
            getattr(user, "id", None),
            type(exc).__name__,
        )
        return set()


def _viewer_from_user(user) -> AnalyticsViewer:
    teams: Set[int] = set()
    role, canonical = _analytics_role(getattr(user, "rol", "visor"))
    capabilities: Set[str] = set()

    def _normalize_permissions(raw_permissions) -> Set[str]:
        if isinstance(raw_permissions, str):
            values = [value.strip() for value in raw_permissions.split(",") if value.strip()]
        elif isinstance(raw_permissions, (list, tuple, set)):
            values = [str(value).strip() for value in raw_permissions if str(value).strip()]
        else:
            return set()
        return {value.lower() for value in values}

    scope = getattr(user, "scope", None)
    if isinstance(scope, dict):
        capabilities |= _normalize_permissions(scope.get("permisos") or scope.get("permissions") or [])

    accesibilidad = getattr(user, "accesibilidad", None)
    if isinstance(accesibilidad, dict):
        employee_scope = accesibilidad.get("employee_scope")
        if isinstance(employee_scope, dict):
            capabilities |= _normalize_permissions(
                employee_scope.get("permisos") or employee_scope.get("permissions") or []
            )
    if canonical == ROLE_SUPERADMIN:
        if not is_authorized_superadmin_user(user):
            abort(403, description="Platform administrator is not authorised")
        return AnalyticsViewer(
            getattr(user, "id", None),
            role,
            {"*"},
            teams,
            {"*"},
            platform_admin=True,
        )

    tenants = _authoritative_namespaced_refs(user)
    return AnalyticsViewer(getattr(user, "id", None), role, tenants, teams, capabilities)


class _TestingViewer(AnalyticsViewer):
    def can_access(self, tenant_id: str, tenant_namespace: str = TENANT_NAMESPACE_OWNER) -> bool:
        # Test-only headers intentionally keep their historical raw-ID contract.
        if "*" in self.allowed_tenants:
            return True
        return str(tenant_id) in self.allowed_tenants


def resolve_viewer() -> AnalyticsViewer:
    viewer = getattr(g, "viewer", None)
    if viewer is not None:
        if isinstance(viewer, AnalyticsViewer):
            return viewer
        candidate = _viewer_from_user(viewer)
        if candidate.role not in _ALLOWED_ROLES:
            abort(403, description="Role not authorised for analytics")
        return candidate

    app = current_app
    if app.config.get("TESTING"):
        role = request.headers.get("X-Debug-Role", "admin").lower()
        tenant = request.headers.get("X-Debug-Tenant", "*")
        tenants = {tenant} if tenant else {"*"}
        if tenant == "*":
            tenants = {"*"}
        capabilities_header = request.headers.get("X-Debug-Capabilities", "")
        capabilities = {
            value.strip().lower()
            for value in capabilities_header.split(",")
            if value.strip()
        }
        if role == "admin" and not capabilities:
            capabilities = {"*"}
        return _TestingViewer(id=None, role=role, allowed_tenants=tenants, teams=set(), capabilities=capabilities)

    abort(401, description="Authentication required")


def require_access(
    tenant_id: str,
    minimum_role: str = "visor",
    required_capability: str | None = None,
    *,
    tenant_namespace: str = TENANT_NAMESPACE_OWNER,
) -> AnalyticsViewer:
    viewer = resolve_viewer()
    role_rank = {"visor": 0, "operador": 1, "admin": 2}
    viewer_rank = role_rank.get(viewer.role, -1)
    required_rank = role_rank.get(minimum_role, 0)
    if viewer_rank < required_rank:
        abort(403, description="Insufficient role for analytics")
    if tenant_namespace not in _TENANT_NAMESPACES:
        abort(403, description="Tenant namespace is not authorised")
    if not viewer.can_access(tenant_id, tenant_namespace):
        abort(403, description="Viewer not authorised for tenant")
    if required_capability and not viewer.can_capability(required_capability):
        abort(403, description=f"Missing capability: {required_capability}")
    return viewer
