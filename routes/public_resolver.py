from flask import Blueprint, jsonify, request, g, current_app
from flask_cors import cross_origin

from models import TenantProfile
from services.tenant_resolver import (
    RESERVED_TENANT_SLUGS,
    TenantResolutionError,
    inject_anon_cookie,
    resolve_tenant_and_user,
    resolve_tenant_only,
)
from routes.pwa_public import _build_public_cart_url

from utils.auth_helpers import _is_jwt_token

public_resolver_bp = Blueprint("public_resolver_bp", __name__, url_prefix="/api/public")
public_municipios_bp = Blueprint("public_municipios_bp", __name__)


def _log_widget_public_request(response, tenant=None, *, entity_token=None):
    """Centralized logging for widget-facing public endpoints."""

    status_code = None
    response_obj = response
    if isinstance(response, tuple):
        response_obj = response[0]
        status_code = response[1] if len(response) > 1 else None
    if status_code is None and hasattr(response_obj, "status_code"):
        status_code = response_obj.status_code

    tenant_slug = tenant
    if tenant_slug is None and tenant is not None:
        tenant_slug = getattr(tenant, "slug", None)

    current_app.logger.info(
        "WIDGET_REQ path=%s user_id=%s tenant=%s entity_token=%s status=%s",
        getattr(request, "path", None),
        getattr(getattr(g, "user", None), "id", None),
        tenant_slug,
        entity_token,
        status_code,
    )

    return response


def _extract_widget_token() -> str | None:
    """Read the widget/entity token from query params, headers or cookies."""

    widget_cookie_name = (
        current_app.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token")
        if current_app
        else "widget_token"
    )

    candidates = [
        request.args.get("widget_token"),
        request.args.get("entityToken"),
        request.args.get("owner_token") or request.args.get("ownerToken"),
        request.headers.get("X-Widget-Token"),
        request.headers.get("X-Entity-Token"),
        request.headers.get("X-Owner-Token"),
        request.cookies.get(widget_cookie_name),
        request.cookies.get("owner_token"),
    ]

    auth_header = request.headers.get("Authorization") or ""
    if auth_header.lower().startswith("bearer "):
        candidate = auth_header.split(None, 1)[1]
        if candidate and not _is_jwt_token(candidate):
            candidates.append(candidate)

    for token in candidates:
        if token:
            return token
    return None


def _canonical_widget_token(tenant: TenantProfile, provided: str | None) -> str | None:
    """Resolve the preferred token for embedding the widget.

    The lookup prioritizes a provided token that is already registered for the
    tenant, then falls back to the owner token (municipio/pyme) so rotations are
    transparent for existing embeds. As a last resort it reuses the first stored
    widget token.
    """

    cfg = tenant.configuracion or {}
    tokens_cfg = cfg.get("widget_tokens")
    tokens: list[str] = []

    if isinstance(tokens_cfg, str):
        tokens = [tokens_cfg]
    elif isinstance(tokens_cfg, list):
        tokens = [t for t in tokens_cfg if t]

    if provided and provided in tokens:
        return provided

    owner = tenant.pyme or tenant.municipio
    if owner and getattr(owner, "token", None):
        return owner.token

    if tokens:
        return tokens[0]

    return provided


def _catalog_widget_enabled_for_tenant(tenant: TenantProfile) -> bool:
    cfg = tenant.configuracion or {}
    flags = [
        cfg.get("widget_catalog_enabled"),
        cfg.get("catalogo_widget_visible"),
        cfg.get("catalog_widget_visible"),
    ]

    catalog_cfg = cfg.get("catalogo") or cfg.get("catalogos")
    if isinstance(catalog_cfg, dict):
        flags.extend(
            [
                catalog_cfg.get("widget_enabled"),
                catalog_cfg.get("widget_visible"),
                catalog_cfg.get("catalogo_widget_visible"),
            ]
        )

    for flag in flags:
        if isinstance(flag, bool):
            return flag

    # Default: only enable for PyMEs unless explicitly allowed.
    return (tenant.tipo or "").lower() == "pyme"


def _marketplace_meta(tenant: TenantProfile) -> dict:
    enabled = _catalog_widget_enabled_for_tenant(tenant)
    full_url, _, _ = _build_public_cart_url(tenant)
    whatsapp_share_url = None
    if full_url:
        share_text = f"Entrá al marketplace de {tenant.nombre or tenant.slug}: {full_url}"
        from routes.market import _whatsapp_share_link

        whatsapp_share_url = _whatsapp_share_link(share_text)

    return {
        "enabled": enabled,
        "tenant_slug": tenant.slug,
        "tenant_id": tenant.id,
        "tenant_tipo": tenant.tipo,
        "public_cart_url": full_url,
        "whatsapp_share_url": whatsapp_share_url,
    }


def _theme_from_tenant(tenant: TenantProfile) -> dict:
    """Return widget-ready theme tokens derived from tenant config."""

    tema = tenant.tema or {}
    cfg = tenant.configuracion or {}

    def pick(*keys, default=None):
        for key in keys:
            if isinstance(tema, dict) and tema.get(key):
                return tema[key]
            if isinstance(cfg, dict) and cfg.get(key):
                return cfg[key]
        return default

    tipo = (tenant.tipo or "").lower()

    primary_default = "#006c3f" if tipo == "municipio" else "#2563eb"
    accent_default = "#d4a01a" if tipo == "municipio" else "#22c55e"

    return {
        "primary": pick("primary", "color_primario", "primary_color", default=primary_default),
        "accent": pick("accent", "color_secundario", "accent_color", default=accent_default),
        "background": pick("background", "fondo", "bg", default="#ffffff"),
        "surface": pick("surface", default="#ffffff"),
        "text": pick("text", "texto", default="#1f2937"),
        "launcher": pick("launcher", "color_launcher", default=None),
        "logo": pick("widget_logo", "logo_widget", "logo", "logo_url", default=tenant.logo_url),
        "animation": pick("widget_logo_animation", "logo_animation", default=None),
    }


def _build_widget_embed_payload(tenant: TenantProfile, provided_token: str | None) -> dict:
    """Expose a rich embed configuration so `integracion.tsx` can render a SaaS builder."""

    canonical_token = _canonical_widget_token(tenant, provided_token)
    theme = _theme_from_tenant(tenant)
    marketplace = _marketplace_meta(tenant)

    cfg = tenant.configuracion or {}

    welcome_title = cfg.get("widget_welcome_title") or cfg.get("welcome_title") or tenant.nombre
    welcome_subtitle = cfg.get("widget_welcome_subtitle") or cfg.get("welcome_subtitle") or "Asistente Virtual"

    width = cfg.get("widget_width") or "460px"
    height = cfg.get("widget_height") or "680px"
    closed_size = cfg.get("widget_closed_size") or "108px"

    script_url = current_app.config.get("WIDGET_SCRIPT_URL", "https://www.chatboc.ar/widget.js")

    attrs = {
        "data-owner-token": canonical_token,
        "data-default-open": str(cfg.get("widget_default_open", False)).lower(),
        "data-width": width,
        "data-height": height,
        "data-closed-width": closed_size,
        "data-closed-height": closed_size,
        "data-bottom": cfg.get("widget_bottom", "20px"),
        "data-right": cfg.get("widget_right", "20px"),
        "data-z-index": cfg.get("widget_z_index", "100000"),
        "data-endpoint": cfg.get("widget_endpoint") or tenant.tipo or "municipio",
        "data-theme": cfg.get("widget_theme") or cfg.get("tema") or "light",
        "data-primary-color": theme.get("primary"),
        "data-accent-color": theme.get("accent"),
        "data-logo-url": theme.get("logo"),
        "data-logo-animation": theme.get("animation"),
        "data-welcome-title": welcome_title,
        "data-welcome-subtitle": welcome_subtitle,
        "data-allow-attachments": str(cfg.get("widget_allow_attachments", True)).lower(),
        "data-allow-location": str(cfg.get("widget_allow_location", True)).lower(),
        "data-allow-audio": str(cfg.get("widget_allow_audio", True)).lower(),
        "data-domain": tenant.dominio or None,
    }

    # Remove None values so the frontend only renders concrete attributes
    attrs = {k: v for k, v in attrs.items() if v is not None}

    # Prebuild a copy-paste snippet for convenience
    attr_snippet = " ".join(f"{k}='{v}'" for k, v in attrs.items())
    embed_snippet = f"<script src='{script_url}' async {attr_snippet}></script>"

    return {
        "script_url": script_url,
        "attributes": attrs,
        "embed_snippet": embed_snippet,
        "theme": theme,
        "marketplace": marketplace,
        "widget_token": canonical_token,
        "widget_token_cookie_name": current_app.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token"),
    }


def _normalize_widget_config(config: dict | None) -> dict:
    """Return a widget-friendly config with consistent shapes.

    The frontend expects certain objects (menu, copy) to always be dictionaries
    with the keys it deserializes. When tenants store strings or lists by
    mistake, React components can end up invoking methods on non-callable
    values (e.g., `_t is not a function`). This helper coerces the structure to
    predictable defaults so the widget renders safely.
    """

    cfg = config if isinstance(config, dict) else {}

    menu_config = cfg.get("menu") if isinstance(cfg.get("menu"), dict) else None
    if menu_config is None or "children" not in menu_config:
        cfg.setdefault("menu", {"children": []})

    copy_config = cfg.get("copy") if isinstance(cfg.get("copy"), dict) else None
    if copy_config is None:
        cfg.setdefault("copy", {})

    return cfg


@public_resolver_bp.route("/resolve-tenant", methods=["POST"])
def resolve_tenant_endpoint():
    payload = request.get_json(force=True, silent=True) or {}
    whatsapp_destination_number = payload.get("whatsapp_destination_number")
    widget_token = payload.get("widget_token")
    tenant_slug = payload.get("tenant_slug") or request.args.get("tenant")

    try:
        tenant, user, created_anon = resolve_tenant_and_user(
            whatsapp_destination_number=whatsapp_destination_number,
            widget_token=widget_token,
            tenant_slug=tenant_slug,
            current_user=getattr(g, "user", None),
        )
    except TenantResolutionError as exc:
        return jsonify({"error": str(exc)}), 404

    tenant_info = tenant.to_public_dict()
    tenant_info.setdefault("config", _normalize_widget_config(tenant.configuracion))
    tenant_info["marketplace"] = _marketplace_meta(tenant)

    # Garantizar que el frontend reciba una estructura de menú consistente
    # aunque el tenant no tenga configuración explícita. El widget espera un
    # objeto con la clave ``children`` para renderizar las secciones sin
    # explotar en una desestructuración.
    config = _normalize_widget_config(tenant_info.get("config"))
    tenant_info["config"] = config

    response = jsonify(
        {
            "tenant": tenant_info,
            "anonId": user.anon_id,
            "userId": user.id,
            "created": created_anon,
        }
    )
    inject_anon_cookie(response, user.anon_id)
    return _log_widget_public_request(response, tenant, entity_token=widget_token)


@public_resolver_bp.route(
    "/tenant-profile", methods=["GET", "OPTIONS"], provide_automatic_options=False
)
@cross_origin(origins="*", automatic_options=False)
def tenant_profile():
    """Devuelve datos públicos del tenant sin requerir autenticación.

    Se puede resolver por slug (``tenant``/``slug``), token de widget o número
    de WhatsApp. Siempre responde JSON para evitar páginas HTML de error que
    rompan el widget.
    """

    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    resolved_from_fallback = False
    resolution_error = None
    explicit_slug_failure = False

    tenant_slug_original = request.args.get("tenant") or request.args.get("slug")
    tenant_slug = tenant_slug_original.strip() if tenant_slug_original else None

    if tenant_slug and tenant_slug.lower() in RESERVED_TENANT_SLUGS:
        resolved_from_fallback = True
        resolution_error = (
            f"Tenant slug '{tenant_slug_original}' is reserved; using default tenant"
        )
        tenant_slug = None
    widget_token = _extract_widget_token()
    whatsapp_destination_number = request.args.get("whatsapp_destination_number")

    try:
        tenant = resolve_tenant_only(
            whatsapp_destination_number=whatsapp_destination_number,
            widget_token=widget_token,
            tenant_slug=tenant_slug,
            require_explicit_slug=bool(tenant_slug),
        )
    except TenantResolutionError as exc:
        resolution_error = resolution_error or str(exc)
        explicit_slug_failure = bool(tenant_slug_original) and not widget_token and not whatsapp_destination_number

        normalized_slug = tenant_slug_original.strip().lower() if tenant_slug_original else None

        fallback_tenant = None
        if normalized_slug in {"municipio", "pyme"}:
            fallback_tenant = (
                TenantProfile.query.filter_by(tipo=normalized_slug)
                .order_by(TenantProfile.id.asc())
                .first()
            )

        if not fallback_tenant:
            fallback_tenant = TenantProfile.query.order_by(TenantProfile.id.asc()).first()

        tenant = fallback_tenant
        if not tenant:
            placeholder = {
                "id": None,
                "slug": "default",
                "nombre": None,
                "tipo": None,
                "logo_url": None,
                "dominio": request.host,
                "tema": {},
                "config": {},
            }
            payload = {
                "tenant": placeholder,
                "warning": {
                    "message": resolution_error,
                    "fallback": "placeholder",
                },
            }
            return _log_widget_public_request(jsonify(payload), tenant)

        resolved_from_fallback = True

        if fallback_tenant and normalized_slug in {"municipio", "pyme"}:
            resolution_error = resolution_error or (
                f"Tenant slug '{tenant_slug_original}' not found; using first {normalized_slug} tenant"
            )

        # Si encontramos un tenant de respaldo, no devolvemos 404 aun cuando el
        # slug explícito sea inválido. Esto evita errores en widgets que envían
        # slugs genéricos (ej. "municipio") y permite servir el tenant
        # disponible con una advertencia en vez de romper el flujo.
        if tenant:
            explicit_slug_failure = False

    if (
        tenant_slug_original
        and tenant_slug
        and tenant_slug.lower() != tenant.slug.lower()
        and not resolved_from_fallback
    ):
        resolved_from_fallback = True
        resolution_error = (
            f"Tenant '{tenant_slug_original}' not found; using '{tenant.slug}' instead"
        )

    tenant_info = tenant.to_public_dict()
    tenant_info.setdefault("config", _normalize_widget_config(tenant.configuracion))
    tenant_info["marketplace"] = _marketplace_meta(tenant)

    # Garantizar que el frontend reciba una estructura de menú consistente
    # aunque el tenant no tenga configuración explícita. El widget espera un
    # objeto con la clave ``children`` para renderizar las secciones sin
    # explotar en una desestructuración.
    config = _normalize_widget_config(tenant_info.get("config"))
    tenant_info["config"] = config

    canonical_widget_token = _canonical_widget_token(tenant, widget_token)

    if explicit_slug_failure:
        return jsonify({"error": resolution_error}), 404

    payload = {"tenant": tenant_info}
    if canonical_widget_token:
        payload["widget_token"] = canonical_widget_token
        payload["widget_token_cookie_name"] = current_app.config.get(
            "WIDGET_TOKEN_COOKIE_NAME", "widget_token"
        )
    if resolved_from_fallback and resolution_error:
        payload["warning"] = {
            "message": resolution_error,
            "fallback": "default_tenant",
        }

    response = jsonify(payload)

    if canonical_widget_token:
        cookie_args = {
            "key": current_app.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token"),
            "value": canonical_widget_token,
            "secure": current_app.config.get("SESSION_COOKIE_SECURE", True),
            "httponly": False,
            "samesite": current_app.config.get("SESSION_COOKIE_SAMESITE", "None"),
        }

        cookie_domain = current_app.config.get("SESSION_COOKIE_DOMAIN")
        if cookie_domain:
            cookie_args["domain"] = cookie_domain

        response.set_cookie(**cookie_args)

    return _log_widget_public_request(response, tenant, entity_token=widget_token)


@public_resolver_bp.route(
    "/widget-config", methods=["GET", "OPTIONS"], provide_automatic_options=False
)
@cross_origin(origins="*", automatic_options=False)
def widget_config():
    """Expose a SaaS-style embed configuration for builder/preview UIs.

    This endpoint feeds ``integracion.tsx`` with all the tokens, theme colors and
    script attributes needed to render a live preview and a ready-to-copy embed
    snippet. Always responds JSON to avoid HTML errors crashing widget loaders.
    """

    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    widget_token = _extract_widget_token()
    tenant_slug = request.args.get("tenant") or request.args.get("slug")
    whatsapp_destination_number = request.args.get("whatsapp_destination_number")

    try:
        tenant = resolve_tenant_only(
            whatsapp_destination_number=whatsapp_destination_number,
            widget_token=widget_token,
            tenant_slug=tenant_slug,
            require_explicit_slug=bool(tenant_slug),
        )
    except TenantResolutionError as exc:
        return jsonify({"error": str(exc)}), 404

    is_integration_preview = "/integracion" in (request.headers.get("Referer", "") or "")

    payload = {
        "tenant": tenant.to_public_dict(),
        "widget": _build_widget_embed_payload(tenant, widget_token),
        # The integration builder renders its own preview iframe; the global
        # site-wide widget bubble must stay hidden to avoid duplicated widgets
        # on /t/[tenant]/integracion.
        "suppress_global_widget": True,
        "integration_preview": is_integration_preview,
    }

    response = jsonify(payload)

    if is_integration_preview:
        response.headers.setdefault("X-Suppress-Global-Widget", "true")
        response.set_cookie(
            key="suppress_global_widget",
            value="true",
            secure=current_app.config.get("SESSION_COOKIE_SECURE", True),
            httponly=False,
            samesite=current_app.config.get("SESSION_COOKIE_SAMESITE", "None"),
            path="/",
        )

    return _log_widget_public_request(response, tenant, entity_token=widget_token)


def _municipios_response():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    tenants = (
        TenantProfile.query.filter_by(tipo="municipio")
        .order_by(TenantProfile.id.asc())
        .all()
    )

    payload = [
        {
            "id": tenant.id,
            "slug": tenant.slug,
            "nombre": tenant.nombre,
            "logo_url": tenant.logo_url,
            "dominio": tenant.dominio,
        }
        for tenant in tenants
    ]

    return jsonify({"municipios": payload})


@public_resolver_bp.route(
    "/municipios", methods=["GET", "OPTIONS"], provide_automatic_options=False
)
@cross_origin(origins="*", automatic_options=False)
def list_municipios():
    """Lista pública de tenants tipo municipio con un payload JSON estable.

    El widget la consulta para poblar catálogos; respondemos siempre JSON para
    evitar que un 404 u otra página HTML dispare un ApiError en el frontend.
    """

    return _municipios_response()


@public_municipios_bp.route(
    "/municipios", methods=["GET", "OPTIONS"], provide_automatic_options=False
)
@cross_origin(origins="*", automatic_options=False)
def list_municipios_root():
    """Alias sin prefijo para clientes legacy que llaman ``/municipios``."""

    return _municipios_response()

