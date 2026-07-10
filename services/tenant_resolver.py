
import logging
import uuid
from typing import Optional, Tuple
from urllib.parse import urlparse
from flask import current_app, g, request
from sqlalchemy import func
from database import db
from models import TenantProfile, User, Rubro, WidgetSettings
from services.demo_registry import load_demo_rubros

logger = logging.getLogger(__name__)

RESERVED_TENANT_SLUGS = {"iframe", "embed", "widget"}

def _alias_map() -> dict[str, str]:
    """Return alias -> slug mapping including config defaults."""
    alias_map = dict(current_app.config.get("TENANT_ALIASES", {}) or {})
    alias_target = current_app.config.get("PUBLIC_CATALOG_DEFAULT_TENANT")
    if alias_target:
        for alias in ["whatsapp", "pwa", "municipio", "municipal", "market", "marketplace", "estadisticas"]:
            alias_map.setdefault(alias, alias_target)
    return alias_map

def apply_tenant_alias(slug: Optional[str]) -> Optional[str]:
    cleaned = _clean_slug(slug)
    if not cleaned:
        return None
    alias_map = _alias_map()
    for candidate in tenant_slug_lookup_candidates(cleaned):
        alias_target = alias_map.get(candidate.lower())
        if alias_target:
            return alias_target
    candidates = tenant_slug_lookup_candidates(cleaned)
    return candidates[0] if candidates else cleaned

def _clean_slug(slug: Optional[str]) -> Optional[str]:
    if not slug:
        return None
    normalized = slug.strip()
    if not normalized:
        return None
    if normalized.lower() in RESERVED_TENANT_SLUGS:
        return None
    return normalized


def tenant_slug_lookup_candidates(slug: Optional[str]) -> tuple[str, ...]:
    """Return canonical lookup candidates for public tenant slug inputs.

    Public frontend routes may pass a bare slug (``bodega``), a tenant domain
    (``bodega.chatboc.ar``), or occasionally a full URL. We always try the
    explicit value first, then the bare subdomain alias, so real dotted slugs
    still win if they exist.
    """

    cleaned = _clean_slug(slug)
    if not cleaned:
        return tuple()

    raw = cleaned.strip()
    lowered = raw.lower()
    if "://" in lowered:
        parsed = urlparse(raw)
        raw = parsed.netloc or parsed.path or raw
        lowered = raw.lower()

    lowered = lowered.split("?", 1)[0].split("#", 1)[0].strip().strip("/")
    if "/" in lowered:
        lowered = lowered.split("/", 1)[0]
    if lowered.startswith("www."):
        lowered_no_www = lowered[4:]
    else:
        lowered_no_www = lowered

    candidates = [lowered]
    if lowered_no_www != lowered:
        candidates.append(lowered_no_www)

    platform_domains = {"chatboc.ar", "www.chatboc.ar"}
    configured = None
    try:
        configured = current_app.config.get("PUBLIC_PLATFORM_DOMAINS")
    except RuntimeError:
        configured = None
    if isinstance(configured, str):
        platform_domains.update(item.strip().lower() for item in configured.split(",") if item.strip())
    elif isinstance(configured, (list, tuple, set)):
        platform_domains.update(str(item).strip().lower() for item in configured if str(item).strip())

    for domain in list(platform_domains):
        normalized_domain = domain[4:] if domain.startswith("www.") else domain
        for suffix in {domain, normalized_domain}:
            marker = f".{suffix}"
            for value in (lowered, lowered_no_www):
                if value.endswith(marker):
                    candidate = value[: -len(marker)].strip(".")
                    if candidate:
                        candidates.append(candidate)

    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))


def tenant_slug_from_public_referrer(value: Optional[str] = None) -> Optional[str]:
    """Extract tenant slug from public app URLs such as /t/bodega.chatboc.ar."""

    raw = value
    if raw is None:
        try:
            raw = request.headers.get("Referer") or request.headers.get("Origin")
        except RuntimeError:
            raw = None
    if not raw:
        return None

    text = str(raw or "").strip()
    try:
        parsed = urlparse(text)
    except Exception:
        return None
    path = (parsed.path or text).strip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[0].lower() in {"t", "m", "portal"}:
        candidate = parts[1].strip()
        if candidate and candidate.lower() not in RESERVED_TENANT_SLUGS:
            return candidate
    return None

class TenantResolutionError(Exception):
    """Raised when a tenant cannot be resolved from the request."""

def _tenant_by_slug(slug: Optional[str]) -> Optional[TenantProfile]:
    for candidate in tenant_slug_lookup_candidates(slug):
        tenant = TenantProfile.query.filter(func.lower(TenantProfile.slug) == candidate.lower()).limit(1).first()
        if tenant:
            return tenant
    return None

def _tenant_by_number(number: Optional[str]) -> Optional[TenantProfile]:
    if not number:
        return None
    normalized = number.strip()
    if not normalized:
        return None
    return TenantProfile.query.filter(
        func.lower(TenantProfile.configuracion["whatsapp_numbers"].astext).contains(normalized.lower())
    ).limit(1).first()

def _tenant_by_widget_token(token: Optional[str]) -> Optional[TenantProfile]:
    if not token:
        return None
    return TenantProfile.query.filter(
        TenantProfile.configuracion["widget_tokens"].astext.contains(token)
    ).limit(1).first()


def _should_register_widget_token(tenant: Optional[TenantProfile], token: Optional[str], preferred_slug: Optional[str]) -> bool:
    if not tenant or not token:
        return False
    try:
        from services.plan_access import plan_allows_full_integrations

        if not plan_allows_full_integrations(tenant):
            logger.warning(
                "[tenant_resolver] Blocking widget_token registration for tenant '%s' without full integration access",
                getattr(tenant, "slug", None),
            )
            return False
    except Exception as exc:
        logger.warning(
            "[tenant_resolver] Blocking widget_token registration because plan access could not be verified: %s",
            exc,
        )
        return False
    if not preferred_slug:
        return True
    token_tenant = _tenant_by_widget_token(token)
    if token_tenant and token_tenant.id != tenant.id:
        logger.warning(
            "[tenant_resolver] Ignoring widget_token from tenant '%s' while explicit tenant is '%s'",
            token_tenant.slug,
            tenant.slug,
        )
        return False
    return True

def _prune_widget_token_from_other_tenants(token: str, keep_slug: str | None) -> None:
    if not token:
        return
    query = TenantProfile.query.filter(
        TenantProfile.configuracion["widget_tokens"].astext.contains(token)
    )
    if keep_slug:
        query = query.filter(func.lower(TenantProfile.slug) != keep_slug.lower())
    for tenant in query.all():
        cfg = tenant.configuracion or {}
        tokens = cfg.get("widget_tokens")
        cleaned = []
        if isinstance(tokens, str):
            cleaned = [t for t in [tokens] if t != token]
        elif isinstance(tokens, list):
            cleaned = [t for t in tokens if t != token]
        cfg["widget_tokens"] = cleaned
        tenant.configuracion = cfg
        db.session.add(tenant)
    db.session.commit()

def _register_widget_token(tenant: Optional[TenantProfile], token: Optional[str]) -> None:
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
        tenant = TenantProfile.query.filter(func.lower(TenantProfile.dominio) == candidate).limit(1).first()
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
        password_hash="",
        anon_id=anon_id,
    )
    db.session.add(user)
    db.session.commit()
    return user

def _get_or_create_demo_tenant(slug: str) -> Optional[TenantProfile]:
    """Lazy-load a demo tenant into the database if it doesn't exist.

    This ensures that accessing /demo/bodega creates a persistent TenantProfile
    so that carts and orders can be linked to a real ID.
    """
    if not slug:
        return None

    candidates = tenant_slug_lookup_candidates(slug)
    slug_norm = (candidates[-1] if candidates else str(slug or "")).strip().lower()

    # 1. Check if exists
    tenant = _tenant_by_slug(slug_norm)
    if tenant:
        return tenant

    # 2. Check if it's a known demo slug
    demos = load_demo_rubros(require_owner=False)
    # The 'key' in demo rubros is usually the slug, or rubro_clave
    matched_demo = next((d for d in demos if d.key == slug_norm or d.rubro_clave == slug_norm), None)

    # Also support specific fallback map for "bodega", "ferreteria" if they aren't in load_demo_rubros
    fallback_map = {
        "bodega": {"label": "Bodega Demo", "segment": "Empresas"},
        "ferreteria": {"label": "Ferretería Demo", "segment": "Empresas"},
        "almacen": {"label": "Almacén Demo", "segment": "Empresas"},
        "medico_general": {"label": "Clínica Demo", "segment": "Empresas"},
        "municipio": {"label": "Municipio Demo", "segment": "Gobiernos"},
        "farmacia": {"label": "Farmacia Demo", "segment": "Empresas"},
        "logistica": {"label": "Logística Demo", "segment": "Empresas"},
        "seguros": {"label": "Seguros Demo", "segment": "Empresas"},
        "inmobiliaria": {"label": "Inmobiliaria Demo", "segment": "Empresas"},
        "fintech": {"label": "Fintech Demo", "segment": "Empresas"},
        "energia": {"label": "Energía Demo", "segment": "Empresas"},
        "local_comercial_general": {"label": "Comercio Demo", "segment": "Empresas"},
        "empresa": {"label": "Empresa Genérica", "segment": "Empresas"},
        "soluciones": {"label": "Soluciones Corporativas", "segment": "Empresas"},
    }

    fallback_data = fallback_map.get(slug_norm)

    if not matched_demo and not fallback_data:
        return None

    logger.info(f"Lazy-creating demo tenant for slug: {slug_norm}")

    def _resolve_demo_owner_rubro_id() -> Optional[int]:
        if matched_demo and getattr(matched_demo, "rubro_id", None):
            return matched_demo.rubro_id
        if matched_demo and getattr(matched_demo, "rubro_clave", None):
            rubro = Rubro.query.filter(func.lower(Rubro.clave) == matched_demo.rubro_clave.lower()).first()
            if rubro:
                return rubro.id
        rubro_slug = Rubro.query.filter(func.lower(Rubro.clave) == slug_norm.lower()).first()
        if rubro_slug:
            return rubro_slug.id
        if (matched_demo and matched_demo.segment == "Gobiernos") or (fallback_data and fallback_data.get("segment") == "Gobiernos"):
            rubro_publico = Rubro.query.filter(func.lower(Rubro.clave) == "municipio").first() or Rubro.query.filter(Rubro.es_publico.is_(True)).first()
            return rubro_publico.id if rubro_publico else None
        rubro_privado = Rubro.query.filter(func.lower(Rubro.clave) == "pyme").first() or Rubro.query.filter(Rubro.es_publico.is_(False)).first()
        return rubro_privado.id if rubro_privado else None

    owner_rubro_id = _resolve_demo_owner_rubro_id()

    # Create Owner User
    owner_email = f"admin@{slug_norm}.demo"
    owner = User.query.filter_by(email=owner_email).first()
    if not owner:
        import secrets
        owner = User(
            email=owner_email,
            name=f"Admin {slug_norm.capitalize()}",
            password_hash="demo", # Not usable for login without hash, but safe placeholder
            rol="admin",
            tipo_chat="pyme" if (matched_demo and matched_demo.segment == "Empresas") or (fallback_data and fallback_data.get("segment") == "Empresas") else "municipio",
            rubro_id=owner_rubro_id,
            token=secrets.token_urlsafe(32)
        )
        db.session.add(owner)
        db.session.commit()
    elif owner_rubro_id and not getattr(owner, "rubro_id", None):
        owner.rubro_id = owner_rubro_id
        db.session.add(owner)
        db.session.commit()

    # Create Tenant
    label = matched_demo.label if matched_demo else fallback_data["label"]
    tipo = "pyme" if (matched_demo and matched_demo.segment == "Empresas") or (fallback_data and fallback_data.get("segment") == "Empresas") else "municipio"

    tenant = TenantProfile(
        slug=slug_norm,
        nombre=label,
        dominio=f"{slug_norm}.chatboc.ar",
        tipo=tipo,
        pyme_id=owner.id if tipo == 'pyme' else None,
        municipio_id=owner.id if tipo == 'municipio' else None,
        configuracion={
            "widget_welcome_title": f"Bienvenido a {label}",
            "wallet_balance": 50000 if slug_norm == "bodega" else 0
        },
        tema={
             "primaryColor": "#722F37" if slug_norm == "bodega" else "#006c3f",
             "secondaryColor": "#E6D7C3" if slug_norm == "bodega" else "#d4a01a"
        }
    )
    db.session.add(tenant)
    db.session.commit()

    # Create Widget Settings
    ws = WidgetSettings(
        tenant_id=tenant.id,
        welcome_title=f"Bienvenido a {label}",
        primary_color=tenant.tema.get("primaryColor"),
        secondary_color=tenant.tema.get("secondaryColor")
    )
    db.session.add(ws)
    db.session.commit()

    # Populate Initial Content
    try:
        from services.catalog_seed import ensure_seed_catalog
        ensure_seed_catalog(owner, tenant)
    except Exception as e:
        logger.error(f"Failed to seed content for lazy tenant {slug_norm}: {e}")

    return tenant


def resolve_tenant_and_user(
    whatsapp_destination_number: Optional[str] = None,
    widget_token: Optional[str] = None,
    tenant_slug: Optional[str] = None,
    tenant_id: Optional[int] = None,
    current_user: Optional[User] = None,
    allow_fallback: bool = True,
) -> Tuple[TenantProfile, User, bool]:
    explicit_tenant = None
    if tenant_id:
        try:
            explicit_tenant = TenantProfile.query.get(int(tenant_id))
        except (TypeError, ValueError):
            explicit_tenant = None

    tenant_slug = apply_tenant_alias(tenant_slug)

    # Try standard resolution
    tenant = (
        explicit_tenant
        or _tenant_by_slug(tenant_slug)
        or _tenant_by_number(whatsapp_destination_number)
        or _tenant_by_widget_token(widget_token)
        or _tenant_by_domain(request.host)
    )

    # Try Lazy Demo Creation if slug is explicit and tenant missing
    if not tenant and tenant_slug:
         tenant = _get_or_create_demo_tenant(tenant_slug)

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
        elif not allow_fallback:
            raise TenantResolutionError(f"Tenant '{preferred_slug}' no encontrado")

    if not tenant and allow_fallback:
        fallback_slug = current_app.config.get("PUBLIC_CATALOG_DEFAULT_TENANT")
        tenant = _tenant_by_slug(fallback_slug)

    if not tenant and allow_fallback:
        tenant = TenantProfile.query.order_by(TenantProfile.id.asc()).first()

    if not tenant:
        raise TenantResolutionError("Tenant no encontrado para el contexto dado")

    token_matches_resolution = _should_register_widget_token(tenant, widget_token, preferred_slug)
    if token_matches_resolution:
        _register_widget_token(tenant, widget_token)
    if token_matches_resolution and widget_token and preferred_slug and tenant.slug.lower() == preferred_slug.lower():
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
    allow_fallback: bool = True,
) -> TenantProfile:
    preferred_slug = apply_tenant_alias(tenant_slug)
    tenant = _tenant_by_slug(preferred_slug)

    if not tenant and preferred_slug:
        # Attempt lazy creation for explicit demo slugs
        tenant = _get_or_create_demo_tenant(preferred_slug)

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

    if not tenant and allow_fallback:
        fallback_slug = current_app.config.get("PUBLIC_CATALOG_DEFAULT_TENANT")
        tenant = _tenant_by_slug(fallback_slug)
    if not tenant and allow_fallback:
        tenant = TenantProfile.query.order_by(TenantProfile.id.asc()).first()

    if not tenant:
        raise TenantResolutionError("Tenant no encontrado para el contexto dado")

    token_matches_resolution = _should_register_widget_token(tenant, widget_token, preferred_slug)
    if token_matches_resolution:
        _register_widget_token(tenant, widget_token)
    if token_matches_resolution and widget_token and preferred_slug and tenant.slug.lower() == preferred_slug.lower():
        _prune_widget_token_from_other_tenants(widget_token, tenant.slug)

    return tenant

def inject_anon_cookie(response, anon_id: Optional[str]) -> None:
    if not anon_id:
        return
    response.set_cookie("anon_id", anon_id, httponly=True, samesite="Lax")
