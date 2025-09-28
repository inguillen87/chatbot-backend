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

    def can_access(self, tenant_id: str) -> bool:
        if "*" in self.allowed_tenants:
            return True
        return tenant_id in self.allowed_tenants


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
    if role == "admin":
        tenants.add("*")
    return AnalyticsViewer(getattr(user, "id", None), role.lower(), tenants, teams)


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
        return _TestingViewer(id=None, role=role, allowed_tenants=tenants, teams=set())

    abort(401, description="Authentication required")


def require_access(tenant_id: str, minimum_role: str = "visor") -> AnalyticsViewer:
    viewer = resolve_viewer()
    role_rank = {"visor": 0, "operador": 1, "admin": 2}
    viewer_rank = role_rank.get(viewer.role, -1)
    required_rank = role_rank.get(minimum_role, 0)
    if viewer_rank < required_rank:
        abort(403, description="Insufficient role for analytics")
    if not viewer.can_access(tenant_id):
        abort(403, description="Viewer not authorised for tenant")
    return viewer
