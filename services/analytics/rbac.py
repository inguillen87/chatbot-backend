"""RBAC helpers for the analytics blueprint."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Set

from flask import abort, current_app, g, request


@dataclass(frozen=True)
class AnalyticsViewer:
    id: Optional[int]
    role: str
    allowed_tenants: Set[str]
    teams: Set[int]
    capabilities: Set[str]

    def can_access(self, tenant_id: str) -> bool:
        if "*" in self.allowed_tenants:
            return True
        return tenant_id in self.allowed_tenants

    def can_capability(self, capability: str) -> bool:
        normalized = (capability or "").strip().lower()
        if not normalized:
            return True
        if self.role == "admin":
            return True
        if not self.capabilities:
            # Legacy fallback while CT-02 rolls out progressively.
            return True
        return normalized in self.capabilities


_ALLOWED_ROLES = {"admin", "operador", "visor"}


def _viewer_from_user(user) -> AnalyticsViewer:
    tenants: Set[str] = set()
    teams: Set[int] = set()
    if getattr(user, "municipio_id", None):
        tenants.add(str(user.municipio_id))
    if getattr(user, "pyme_id", None):
        tenants.add(str(user.pyme_id))
    if getattr(user, "empresa_id", None):
        tenants.add(str(user.empresa_id))
    role = getattr(user, "rol", "visor") or "visor"
    capabilities: Set[str] = set()

    def _normalize_permissions(raw_permissions) -> Set[str]:
        if not isinstance(raw_permissions, (list, tuple, set)):
            return set()
        return {
            str(permission).strip().lower()
            for permission in raw_permissions
            if str(permission).strip()
        }

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
    if role == "admin":
        tenants.add("*")
    return AnalyticsViewer(getattr(user, "id", None), role.lower(), tenants, teams, capabilities)


class _TestingViewer(AnalyticsViewer):
    pass


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


def require_access(tenant_id: str, minimum_role: str = "visor", required_capability: str | None = None) -> AnalyticsViewer:
    viewer = resolve_viewer()
    role_rank = {"visor": 0, "operador": 1, "admin": 2}
    viewer_rank = role_rank.get(viewer.role, -1)
    required_rank = role_rank.get(minimum_role, 0)
    if viewer_rank < required_rank:
        abort(403, description="Insufficient role for analytics")
    if not viewer.can_access(tenant_id):
        abort(403, description="Viewer not authorised for tenant")
    if required_capability and not viewer.can_capability(required_capability):
        abort(403, description=f"Missing capability: {required_capability}")
    return viewer
