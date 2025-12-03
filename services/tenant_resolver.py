import logging
import uuid
from typing import Optional, Tuple

from flask import current_app, g, request
from sqlalchemy import func

from database import db
from models import TenantProfile, User

logger = logging.getLogger(__name__)

RESERVED_TENANT_SLUGS = {"iframe", "embed", "widget"}


def _alias_map() -> dict[str, str]:
    """Return alias -> slug mapping including config defaults.

    This lets callers treat generic hints like "municipio", "pwa" or
    "whatsapp" as the canonical tenant slug configured for the deployment,
    preventing 400 responses in public endpoints when the caller only knows an
    alias.
    """

    from flask import current_app

    alias_map = dict(current_app.config.get("TENANT_ALIASES", {}) or {})
    alias_target = current_app.config.get("PUBLIC_CATALOG_DEFAULT_TENANT")
    if alias_target:
        for alias in ["whatsapp", "pwa", "municipio", "municipal", "market", "marketplace"]:
            alias_map.setdefault(alias, alias_target)
    return alias_map


def apply_tenant_alias(slug: Optional[str]) -> Optional[str]:
    """Translate a slug hint using configured aliases if available."""

    cleaned = _clean_slug(slug)
    if not cleaned:
        return None

    alias_target = _alias_map().get(cleaned.lower())
    return alias_target or cleaned


def _clean_slug(slug: Optional[str]) -> Optional[str]:
    """Return a normalized slug or ``None`` when empty/reserved."""

    if not slug:
        return None

    normalized = slug.strip()
    if not normalized:
        return None

    if normalized.lower() in RESERVED_TENANT_SLUGS:
        return None

    return normalized


class TenantResolutionError(Exception):
    """Raised when a tenant cannot be resolved from the request."""


def _tenant_by_slug(slug: Optional[str]) -> Optional[TenantProfile]:
    slug = _clean_slug(slug)
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
            TenantProfile.configuracion["widget_tokens"].astext.contains(token)  # type: ignore[index]
        )
        .limit(1)
        .first()
    )


def _prune_widget_token_from_other_tenants(token: str, keep_slug: str | None) -> None:
    """Remove ``token`` from tenants other than ``keep_slug`` to avoid collisions."""

    if not token:
        return

    query = TenantProfile.query.filter(
        TenantProfile.configuracion["widget_tokens"].astext.contains(token)  # type: ignore[index]
    )

    if keep_slug:
        query = query.filter(func.lower(TenantProfile.slug) != keep_slug.lower())

    for tenant in query.all():
        cfg = tenant.configuracion or {}
        tokens = cfg.get("widget_tokens")
        cleaned: list[str] = []

        if isinstance(tokens, str):
            cleaned = [t for t in [tokens] if t != token]
        elif isinstance(tokens, list):
            cleaned = [t for t in tokens if t != token]

        cfg["widget_tokens"] = cleaned
        tenant.configuracion = cfg
        db.session.add(tenant)

    db.session.commit()


def _register_widget_token(tenant: Optional[TenantProfile], token: Optional[str]) -> None:
    """Persist the widget token inside the tenant configuration for reuse."""

    if not tenant or not token:
        return

    cfg = tenant.configuracion or {}
    tokens = cfg.get("widget_tokens")
    updated = False

    if not tokens:
        cfg["widget_tokens"] = token
        updated = True
    elif isinstance(tokens, str):
        if tokens != token:
            cfg["widget_tokens"] = [tokens, token]
            updated = True
    elif isinstance(tokens, list):
        if token not in tokens:
            tokens.append(token)
            updated = True

    if updated:
        tenant.configuracion = cfg
        db.session.add(tenant)
        db.session.commit()


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
    tenant_id: Optional[int] = None,
    current_user: Optional[User] = None,
) -> Tuple[TenantProfile, User, bool]:
    """Resolve tenant and user (auth or anonymous) from request context.

    Returns (tenant, user, created_anon_flag).
    Raises TenantResolutionError when tenant cannot be identified.
    """

    explicit_tenant = None
    if tenant_id:
        try:
            explicit_tenant = TenantProfile.query.get(int(tenant_id))
        except (TypeError, ValueError):
            explicit_tenant = None

    tenant_slug = apply_tenant_alias(tenant_slug)

    tenant = (
        explicit_tenant
        or _tenant_by_slug(tenant_slug)
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

    preferred_slug = apply_tenant_alias(tenant_slug)
    if tenant and preferred_slug and tenant.slug.lower() != preferred_slug.lower():
        slug_match = _tenant_by_slug(preferred_slug)
        if slug_match:
            tenant = slug_match

    if not tenant:
        fallback_slug = current_app.config.get("PUBLIC_CATALOG_DEFAULT_TENANT")
        tenant = _tenant_by_slug(fallback_slug)

    if not tenant:
        tenant = TenantProfile.query.order_by(TenantProfile.id.asc()).first()

    if not tenant:
        raise TenantResolutionError("Tenant no encontrado para el contexto dado")

    _register_widget_token(tenant, widget_token)
    if widget_token and preferred_slug and tenant.slug.lower() == preferred_slug.lower():
        _prune_widget_token_from_other_tenants(widget_token, tenant.slug)

    if current_user and getattr(current_user, "is_authenticated", False):
        return tenant, current_user, False

    anon_id = request.cookies.get("anon_id") or str(uuid.uuid4())
    user = _build_anon_user(anon_id)

    response_ctx = getattr(current_app, "_tenant_resolver_response_ctx", None)
    if response_ctx is not None:
        response_ctx.setdefault("anon_id", anon_id)
    return tenant, user, True


def resolve_tenant_only(
    whatsapp_destination_number: Optional[str] = None,
    widget_token: Optional[str] = None,
    tenant_slug: Optional[str] = None,
    host: Optional[str] = None,
    require_explicit_slug: bool = False,
) -> TenantProfile:
    """Resolve solo el tenant sin crear usuarios anónimos.

    Usa las mismas fuentes que ``resolve_tenant_and_user`` pero evita efectos
    secundarios como la creación de visitantes. Se recurre a ``request.host``
    si no se pasa ``host`` de forma explícita. Incluso si se recibe un hint
    inválido (slug o token que no existe) se intenta degradar a dominio o
    tenant por defecto para evitar 404 innecesarios en rutas públicas, a
    menos que ``require_explicit_slug`` solicite fallar cuando el slug no se
    resuelva.
    """

    preferred_slug = _clean_slug(tenant_slug)
    tenant = _tenant_by_slug(preferred_slug)

    if (
        not tenant
        and tenant_slug
        and require_explicit_slug
        and not widget_token
        and not whatsapp_destination_number
    ):
        raise TenantResolutionError(f"Tenant '{tenant_slug}' no encontrado")

    if not tenant:
        tenant = _tenant_by_number(whatsapp_destination_number)

    if not tenant:
        tenant = _tenant_by_widget_token(widget_token)

    if not tenant:
        tenant = _tenant_by_domain(host or request.host)

    if not tenant:
        tenant = getattr(g, "tenant_profile", None)
    if tenant and preferred_slug and tenant.slug.lower() != preferred_slug.lower():
        slug_match = _tenant_by_slug(preferred_slug)
        if slug_match:
            tenant = slug_match

    if not tenant:
        fallback_slug = current_app.config.get("PUBLIC_CATALOG_DEFAULT_TENANT")
        tenant = _tenant_by_slug(fallback_slug)
    if not tenant:
        tenant = TenantProfile.query.order_by(TenantProfile.id.asc()).first()

    if not tenant:
        raise TenantResolutionError("Tenant no encontrado para el contexto dado")

    _register_widget_token(tenant, widget_token)
    if widget_token and preferred_slug and tenant.slug.lower() == preferred_slug.lower():
        _prune_widget_token_from_other_tenants(widget_token, tenant.slug)

    return tenant


def inject_anon_cookie(response, anon_id: Optional[str]) -> None:
    if not anon_id:
        return
    response.set_cookie("anon_id", anon_id, httponly=True, samesite="Lax")

