import logging
import uuid
from typing import Optional, Tuple

from flask import current_app, g, request
from sqlalchemy import func

from database import db
from models import TenantProfile, User

logger = logging.getLogger(__name__)


class TenantResolutionError(Exception):
    """Raised when a tenant cannot be resolved from the request."""


def _tenant_by_slug(slug: Optional[str]) -> Optional[TenantProfile]:
    if not slug:
        return None
    return (
        TenantProfile.query.filter(func.lower(TenantProfile.slug) == slug.lower())
        .limit(1)
        .first()
    )


def _tenant_by_number(number: Optional[str]) -> Optional[TenantProfile]:
    if not number:
        return None
    normalized = number.strip()
    if not normalized:
        return None
    return (
        TenantProfile.query.filter(
            func.lower(TenantProfile.configuracion["whatsapp_numbers"].astext)  # type: ignore[arg-type]
            .contains(normalized.lower())
        )
        .limit(1)
        .first()
    )


def _tenant_by_widget_token(token: Optional[str]) -> Optional[TenantProfile]:
    if not token:
        return None
    return (
        TenantProfile.query.filter(
            TenantProfile.configuracion["widget_tokens"].astext == token  # type: ignore[index]
        )
        .limit(1)
        .first()
    )


def _tenant_by_domain(domain: Optional[str]) -> Optional[TenantProfile]:
    if not domain:
        return None

    normalized = domain.split(":", 1)[0].strip().lower()
    candidates = [normalized]

    if normalized.startswith("www."):
        candidates.append(normalized[4:])
    parts = normalized.split(".")
    if len(parts) > 2:
        candidates.append(".".join(parts[-2:]))

    for candidate in dict.fromkeys(filter(None, candidates)):
        tenant = (
            TenantProfile.query.filter(func.lower(TenantProfile.dominio) == candidate)
            .limit(1)
            .first()
        )
        if tenant:
            return tenant

    return None


def _build_anon_user(anon_id: str) -> User:
    placeholder_email = f"anon-{anon_id}@example.invalid"
    existing = User.query.filter_by(anon_id=anon_id).first()
    if existing:
        return existing
    user = User(
        name="Visitante",
        email=placeholder_email,
        password_hash="",  # contraseñas no usadas para visitantes
        anon_id=anon_id,
    )
    db.session.add(user)
    db.session.commit()
    return user


def resolve_tenant_and_user(
    whatsapp_destination_number: Optional[str] = None,
    widget_token: Optional[str] = None,
    tenant_slug: Optional[str] = None,
    current_user: Optional[User] = None,
) -> Tuple[TenantProfile, User, bool]:
    """Resolve tenant and user (auth or anonymous) from request context.

    Returns (tenant, user, created_anon_flag).
    Raises TenantResolutionError when tenant cannot be identified.
    """

    tenant = (
        _tenant_by_slug(tenant_slug)
        or _tenant_by_number(whatsapp_destination_number)
        or _tenant_by_widget_token(widget_token)
        or _tenant_by_domain(request.host)
    )

    if not tenant:
        tenant = getattr(g, "tenant_profile", None)
    if not tenant:
        if current_user and getattr(current_user, "is_authenticated", False):
            owner_id = current_user.empresa_id or current_user.id
            tenant = TenantProfile.query.filter(
                (TenantProfile.municipio_id == owner_id) | (TenantProfile.pyme_id == owner_id)
            ).first()

    if not tenant:
        fallback_slug = current_app.config.get("PUBLIC_CATALOG_DEFAULT_TENANT")
        tenant = _tenant_by_slug(fallback_slug)

    if not tenant:
        tenant = TenantProfile.query.order_by(TenantProfile.id.asc()).first()

    if not tenant:
        raise TenantResolutionError("Tenant no encontrado para el contexto dado")

    if current_user and getattr(current_user, "is_authenticated", False):
        return tenant, current_user, False

    anon_id = request.cookies.get("anon_id") or str(uuid.uuid4())
    user = _build_anon_user(anon_id)

    response_ctx = getattr(current_app, "_tenant_resolver_response_ctx", None)
    if response_ctx is not None:
        response_ctx.setdefault("anon_id", anon_id)
    return tenant, user, True


def inject_anon_cookie(response, anon_id: Optional[str]) -> None:
    if not anon_id:
        return
    response.set_cookie("anon_id", anon_id, httponly=True, samesite="Lax")

