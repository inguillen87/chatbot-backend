from datetime import datetime, timezone
from flask import Blueprint, jsonify, request, g, current_app
from flask_cors import cross_origin
from sqlalchemy import desc

from models import ChatSessionContext, Conversacion, TenantProfile, User, WidgetSettings, Rubro, db
from services.live_chat_schedule import build_live_chat_status
from services.tenant_resolver import (
    RESERVED_TENANT_SLUGS,
    TenantResolutionError,
    inject_anon_cookie,
    resolve_tenant_and_user,
    resolve_tenant_only,
)
from routes.pwa_public import _build_public_cart_url

from utils.auth_helpers import _is_jwt_token
from services.demo_registry import load_demo_rubros

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

    cfg = _normalize_widget_config(tenant.configuracion, tenant.widget_settings)
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
        try:
            from routes.market import _whatsapp_share_link

            whatsapp_share_url = _whatsapp_share_link(share_text)
        except ImportError:
            whatsapp_share_url = None

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
    widget_settings = getattr(tenant, "widget_settings", None)
    theme_config = {}
    if widget_settings and getattr(widget_settings, "theme_config", None):
        theme_config = widget_settings.theme_config or {}

    def pick(*keys, default=None):
        for key in keys:
            if isinstance(theme_config, dict) and theme_config.get(key):
                return theme_config[key]
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


def _resolve_widget_api_base(tenant: TenantProfile, cfg: dict) -> str:
    """Return the API base URL that widget embeds should target."""

    candidates = [
        cfg.get("widget_api_base_url"),
        cfg.get("widget_api_base"),
        cfg.get("api_base_url"),
        cfg.get("api_base"),
        current_app.config.get("PUBLIC_API_BASE_URL"),
        current_app.config.get("BACKEND_URL"),
    ]

    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.rstrip("/")

    return "https://api.chatboc.ar"




def _support_channels_payload(tenant: TenantProfile, cfg: dict) -> dict:
    owner = tenant.pyme or tenant.municipio
    whatsapp_number = (
        cfg.get("support_whatsapp")
        or cfg.get("whatsapp_phone")
        or getattr(tenant, "whatsapp_sender_id", None)
        or getattr(owner, "telefono", None)
    )

    return {
        "live_chat": {
            **build_live_chat_status(
                schedule_override=(cfg.get("live_chat_schedule") if isinstance(cfg.get("live_chat_schedule"), dict) else None)
            ),
            "channel": "ticket_chat",
            "realtime": True,
            "media": {"text": True, "image": True, "audio": True, "file": True},
        },
        "whatsapp": {
            "enabled": bool(whatsapp_number),
            "number": whatsapp_number,
            "channel": "whatsapp",
            "realtime_bridge": True,
            "media": {"text": True, "image": True, "audio": True, "file": True},
        },
    }


def _build_widget_embed_payload(tenant: TenantProfile, provided_token: str | None) -> dict:
    """Expose a rich embed configuration so `integracion.tsx` can render a SaaS builder."""

    canonical_token = _canonical_widget_token(tenant, provided_token)
    if canonical_token:
        cfg = tenant.configuracion or {}
        tokens_cfg = cfg.get("widget_tokens")
        tokens: list[str] = []
        if isinstance(tokens_cfg, str):
            tokens = [tokens_cfg]
        elif isinstance(tokens_cfg, list):
            tokens = [t for t in tokens_cfg if t]
        if canonical_token not in tokens:
            tokens.append(canonical_token)
            cfg["widget_tokens"] = tokens
            tenant.configuracion = cfg
            try:
                db.session.add(tenant)
                db.session.commit()
            except Exception:
                db.session.rollback()
                current_app.logger.exception(
                    "[widget] Failed to persist canonical widget token for tenant %s",
                    tenant.id,
                )
    theme = _theme_from_tenant(tenant)
    marketplace = _marketplace_meta(tenant)

    settings = getattr(tenant, "widget_settings", None) or WidgetSettings.query.filter_by(
        tenant_id=tenant.id
    ).first()

    cfg = _normalize_widget_config(tenant.configuracion, settings)
    api_base_url = _resolve_widget_api_base(tenant, cfg)

    welcome_title = cfg.get("widget_welcome_title") or cfg.get("welcome_title") or tenant.nombre
    welcome_subtitle = cfg.get("widget_welcome_subtitle") or cfg.get("welcome_subtitle") or "Asistente Virtual"

    position = cfg.get("widget_position") or cfg.get("position")
    border_radius = cfg.get("widget_border_radius") or cfg.get("border_radius")
    launcher_text = cfg.get("widget_launcher_text") or cfg.get("launcher_text")
    header_title = cfg.get("widget_header_title") or cfg.get("header_title")
    header_subtitle = cfg.get("widget_header_subtitle") or cfg.get("header_subtitle")

    width = cfg.get("widget_width") or "460px"
    height = cfg.get("widget_height") or "680px"
    closed_size = cfg.get("widget_closed_size") or "108px"

    script_url = current_app.config.get("WIDGET_SCRIPT_URL", "https://www.chatboc.ar/widget.js")
    iframe_url = (
        current_app.config.get("WIDGET_IFRAME_URL")
        or cfg.get("widget_iframe_url")
        or "https://www.chatboc.ar/iframe"
    )

    right_offset = cfg.get("widget_right", "20px")
    left_offset = cfg.get("widget_left", right_offset)

    ux = cfg.get("ux") if isinstance(cfg.get("ux"), dict) else {}
    motion_level = ux.get("motion_level") or cfg.get("widget_motion_level") or "balanced"
    widget_preset = ux.get("preset") or cfg.get("widget_preset") or "premium"
    gradient_start = ux.get("gradient_start") or cfg.get("widget_gradient_start")
    gradient_end = ux.get("gradient_end") or cfg.get("widget_gradient_end")
    glassmorphism = bool(ux.get("glassmorphism", cfg.get("widget_glassmorphism", True)))
    logo_ring = bool(ux.get("logo_ring", cfg.get("widget_logo_ring", True)))
    typing_animation = ux.get("typing_animation") or cfg.get("widget_typing_animation") or "wave-dots"
    bubble_animation = ux.get("bubble_animation") or cfg.get("widget_bubble_animation") or "soft-rise"
    launcher_animation = ux.get("launcher_animation") or cfg.get("widget_launcher_animation") or "pulse-glow"
    message_enter_animation = ux.get("message_enter_animation") or cfg.get("widget_message_enter_animation") or "fade-up"
    logo_badge_style = ux.get("logo_badge_style") or cfg.get("widget_logo_badge_style") or "ring"
    cursor_trail = bool(ux.get("cursor_trail", cfg.get("widget_cursor_trail", False)))
    ambient_particles = bool(ux.get("ambient_particles", cfg.get("widget_ambient_particles", False)))

    attrs = {
        "data-owner-token": canonical_token,
        "data-widget-token": canonical_token,
        "data-entity-token": canonical_token,
        "data-tenant": tenant.slug,
        "data-tenant-slug": tenant.slug,
        "data-default-open": str(cfg.get("widget_default_open", False)).lower(),
        "data-width": width,
        "data-height": height,
        "data-closed-width": closed_size,
        "data-closed-height": closed_size,
        "data-bottom": cfg.get("widget_bottom", "20px"),
        "data-z-index": cfg.get("widget_z_index", "100000"),
        "data-endpoint": cfg.get("widget_endpoint") or tenant.tipo or "municipio",
        "data-theme": cfg.get("widget_theme") or cfg.get("tema") or "light",
        "data-primary-color": cfg.get("primary_color") or theme.get("primary"),
        "data-accent-color": cfg.get("secondary_color") or theme.get("accent"),
        "data-text-color": theme.get("text"),
        "data-background-color": theme.get("background"),
        "data-surface-color": theme.get("surface"),
        "data-launcher-color": theme.get("launcher"),
        "data-logo-url": cfg.get("avatar_url") or theme.get("logo"),
        "data-logo-animation": cfg.get("widget_logo_animation") or theme.get("animation"),
        "data-widget-preset": widget_preset,
        "data-motion-level": motion_level,
        "data-glassmorphism": str(glassmorphism).lower(),
        "data-logo-ring": str(logo_ring).lower(),
        "data-gradient-start": gradient_start,
        "data-gradient-end": gradient_end,
        "data-typing-animation": typing_animation,
        "data-bubble-animation": bubble_animation,
        "data-launcher-animation": launcher_animation,
        "data-message-enter-animation": message_enter_animation,
        "data-logo-badge-style": logo_badge_style,
        "data-cursor-trail": str(cursor_trail).lower(),
        "data-ambient-particles": str(ambient_particles).lower(),
        "data-font-family": cfg.get("font_family") or "inherit",
        "data-bubble-shape": cfg.get("bubble_shape") or "round",
        "data-singleton": "true",
        "data-welcome-title": welcome_title,
        "data-welcome-subtitle": welcome_subtitle,
        "data-allow-attachments": str(cfg.get("widget_allow_attachments", True)).lower(),
        "data-allow-location": str(cfg.get("widget_allow_location", True)).lower(),
        "data-allow-audio": str(cfg.get("widget_allow_audio", True)).lower(),
        "data-domain": api_base_url,
        "data-api-base": api_base_url,
        "data-shadow-dom": "true",  # Ensure styles don't leak/conflict with host page
        # Keep iframe source absolute so embeds work on external origins.
        "data-iframe-url": iframe_url,
        "data-iframe-src": iframe_url,
    }
    if str(position).lower() == "left":
        attrs["data-left"] = left_offset
    else:
        attrs["data-right"] = right_offset
    if position:
        attrs["data-position"] = position
    if border_radius:
        attrs["data-border-radius"] = border_radius
    if launcher_text:
        attrs["data-launcher-text"] = launcher_text
    if header_title:
        attrs["data-header-title"] = header_title
    if header_subtitle:
        attrs["data-header-subtitle"] = header_subtitle

    # Remove None values so the frontend only renders concrete attributes
    attrs = {k: v for k, v in attrs.items() if v is not None}

    # Prebuild a copy-paste snippet for convenience
    attr_snippet = " ".join(f"{k}='{v}'" for k, v in attrs.items())
    embed_snippet = f"<script src='{script_url}' async {attr_snippet}></script>"

    support_channels = _support_channels_payload(tenant, cfg)

    builder_config = {
        "welcome_title": welcome_title,
        "welcome_subtitle": welcome_subtitle,
        "cta_messages": cfg.get("cta_messages") or [],
        "theme_config": cfg.get("theme_config") or {},
        "channels": cfg.get("channels") or {},
        "preview": cfg.get("preview") or {},
        "ux": {
            "preset": widget_preset,
            "motion_level": motion_level,
            "glassmorphism": glassmorphism,
            "logo_ring": logo_ring,
            "gradient_start": gradient_start,
            "gradient_end": gradient_end,
            "typing_animation": typing_animation,
            "bubble_animation": bubble_animation,
            "launcher_animation": launcher_animation,
            "message_enter_animation": message_enter_animation,
            "logo_badge_style": logo_badge_style,
            "cursor_trail": cursor_trail,
            "ambient_particles": ambient_particles,
        },
        "embed_snippet": embed_snippet,
        "api_base_url": api_base_url,
        "iframe_url": iframe_url,
        "attributes": attrs,
        "support_channels": support_channels,
        "layout": {
            "position": position or "right",
            "width": width,
            "height": height,
            "closed_size": closed_size,
            "border_radius": border_radius,
        },
    }

    return {
        "script_url": script_url,
        "attributes": attrs,
        "embed_snippet": embed_snippet,
        "api_base_url": api_base_url,
        "iframe_url": iframe_url,
        "theme": theme,
        "builder_config": builder_config,
        "marketplace": marketplace,
        "support_channels": support_channels,
        "widget_token": canonical_token,
        "widget_token_cookie_name": current_app.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token"),
    }


def _normalize_widget_config(config: dict | None, widget_settings=None) -> dict:
    """Return a widget-friendly config with consistent shapes.

    The frontend expects certain objects (menu, copy) to always be dictionaries
    with the keys it deserializes. When tenants store strings or lists by
    mistake, React components can end up invoking methods on non-callable
    values (e.g., `_t is not a function`). This helper coerces the structure to
    predictable defaults so the widget renders safely. When explicit
    ``WidgetSettings`` exist, we merge their values to drive the SaaS embed
    preview.
    """

    cfg = config.copy() if isinstance(config, dict) else {}

    menu_config = cfg.get("menu") if isinstance(cfg.get("menu"), dict) else None
    if menu_config is None or "children" not in menu_config:
        cfg.setdefault("menu", {"children": []})

    copy_config = cfg.get("copy") if isinstance(cfg.get("copy"), dict) else None
    if copy_config is None:
        cfg.setdefault("copy", {})

    if isinstance(widget_settings, WidgetSettings):
        cfg.update(widget_settings.to_config_dict())

    cfg.setdefault("cta_messages", [])
    cfg.setdefault("theme_config", {})

    channels_config = cfg.get("channels") if isinstance(cfg.get("channels"), dict) else {}
    channels_config.setdefault(
        "whatsapp",
        {
            "enabled": False,
            "phone": cfg.get("whatsapp_phone") or "",
            "cta": "Escribinos por WhatsApp",
            "preview_message": "Hola, ¿en qué podemos ayudarte?",
            "brand_name": cfg.get("whatsapp_brand") or cfg.get("tenant_name") or "",
        },
    )
    channels_config.setdefault(
        "telegram",
        {
            "enabled": False,
            "username": cfg.get("telegram_username") or "",
            "cta": "Chatear por Telegram",
            "preview_message": "¡Estamos en Telegram!",
            "brand_name": cfg.get("telegram_brand") or cfg.get("tenant_name") or "",
        },
    )
    channels_config.setdefault(
        "web_widget",
        {
            "enabled": True,
            "cta": cfg.get("widget_launcher_text") or "¿Necesitás ayuda?",
            "welcome_message": cfg.get("widget_welcome_message")
            or cfg.get("widget_welcome_subtitle")
            or "Asistente Virtual",
            "brand_name": cfg.get("widget_brand") or cfg.get("tenant_name") or "",
        },
    )
    cfg["channels"] = channels_config

    preview_config = cfg.get("preview") if isinstance(cfg.get("preview"), dict) else {}
    preview_config.setdefault("device", "desktop")
    preview_config.setdefault("show_branding", True)
    preview_config.setdefault("alignment", "right")
    preview_config.setdefault("card_density", "comfortable")
    cfg["preview"] = preview_config

    ux_config = cfg.get("ux") if isinstance(cfg.get("ux"), dict) else {}
    ux_config.setdefault("preset", cfg.get("widget_preset") or "premium")
    ux_config.setdefault("motion_level", cfg.get("widget_motion_level") or "balanced")
    ux_config.setdefault("glassmorphism", bool(cfg.get("widget_glassmorphism", True)))
    ux_config.setdefault("logo_ring", bool(cfg.get("widget_logo_ring", True)))
    ux_config.setdefault("gradient_start", cfg.get("widget_gradient_start") or cfg.get("primary_color"))
    ux_config.setdefault("gradient_end", cfg.get("widget_gradient_end") or cfg.get("secondary_color"))
    ux_config.setdefault("typing_animation", cfg.get("widget_typing_animation") or "wave-dots")
    ux_config.setdefault("bubble_animation", cfg.get("widget_bubble_animation") or "soft-rise")
    ux_config.setdefault("launcher_animation", cfg.get("widget_launcher_animation") or "pulse-glow")
    ux_config.setdefault("message_enter_animation", cfg.get("widget_message_enter_animation") or "fade-up")
    ux_config.setdefault("logo_badge_style", cfg.get("widget_logo_badge_style") or "ring")
    ux_config.setdefault("cursor_trail", bool(cfg.get("widget_cursor_trail", False)))
    ux_config.setdefault("ambient_particles", bool(cfg.get("widget_ambient_particles", False)))
    cfg["ux"] = ux_config

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
    tenant_info["tipo_chat"] = tenant.tipo
    if (tenant.tipo or "").lower() == "municipio":
        tenant_info["rubro_publico"] = "municipios"
    tenant_info.setdefault(
        "config", _normalize_widget_config(tenant.configuracion, tenant.widget_settings)
    )
    tenant_info["marketplace"] = _marketplace_meta(tenant)

    # Garantizar que el frontend reciba una estructura de menú consistente
    # aunque el tenant no tenga configuración explícita. El widget espera un
    # objeto con la clave ``children`` para renderizar las secciones sin
    # explotar en una desestructuración.
    config = _normalize_widget_config(tenant_info.get("config"), tenant.widget_settings)
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


def _try_get_demo_tenant(slug):
    """Attempt to return mock tenant info for specific demo slugs if they don't exist."""
    if not slug: return None
    slug = slug.strip().lower()

    demo_map = {
        "bodega": {
            "nombre": "Bodega Demo",
            "tipo": "pyme",
            "logo_url": "https://img.icons8.com/color/96/wine-bottle.png",
            "primary": "#722F37",
            "secondary": "#E6D7C3",
            "welcome": "Bienvenido a Bodega Demo"
        },
        "ferreteria": {
            "nombre": "Ferretería Demo",
            "tipo": "pyme",
            "logo_url": "https://img.icons8.com/color/96/hammer.png",
            "primary": "#FF9900",
            "secondary": "#333333",
            "welcome": "Herramientas y Construcción"
        },
        "almacen": {
            "nombre": "Almacén Demo",
            "tipo": "pyme",
            "logo_url": "https://img.icons8.com/color/96/shop.png",
            "primary": "#4CAF50",
            "secondary": "#FFFFFF",
            "welcome": "Tu almacén de confianza"
        },
        "medico_general": {
            "nombre": "Clínica Demo",
            "tipo": "pyme",
            "logo_url": "https://img.icons8.com/color/96/stethoscope.png",
            "primary": "#0099CC",
            "secondary": "#FFFFFF",
            "welcome": "Salud y Bienestar"
        }
    }

    if slug in demo_map:
        data = demo_map[slug]
        # Create a transient TenantProfile object
        tenant = TenantProfile(
            slug=slug,
            nombre=data["nombre"],
            tipo=data["tipo"],
            logo_url=data["logo_url"],
            dominio=f"{slug}.chatboc.ar",
            configuracion={"widget_welcome_title": data["welcome"]},
            tema={"primaryColor": data["primary"], "secondaryColor": data["secondary"]}
        )
        # Mock widget settings for theme consistency
        tenant.widget_settings = WidgetSettings(
            tenant=tenant,
            primary_color=data["primary"],
            secondary_color=data["secondary"],
            welcome_title=data["welcome"]
        )
        # Fake ID so to_public_dict works if it checks ID
        tenant.id = 999999
        return tenant
    return None

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

        # Try Mock Demos first if explicit slug failed
        tenant = _try_get_demo_tenant(normalized_slug)

        fallback_tenant = None
        if not tenant:
            if normalized_slug in {"municipio", "pyme"}:
                fallback_tenant = (
                    TenantProfile.query.filter_by(tipo=normalized_slug)
                    .order_by(TenantProfile.id.asc())
                    .first()
                )

            if not fallback_tenant and normalized_slug in {"municipio", "pyme"}:
                fallback_tenant = TenantProfile.query.order_by(TenantProfile.id.asc()).first()

        if not tenant:
            # Fetch public rubros for the demo selector
            public_rubros = Rubro.query.filter_by(es_publico=True).order_by(Rubro.nombre.asc()).all()
            rubros_list = [
                {
                    "id": r.id,
                    "nombre": r.nombre,
                    "clave": r.clave,
                    "descripcion": r.descripcion,
                    "padre_id": r.padre_id
                } for r in public_rubros
            ]

            placeholder = {
                "id": None,
                "slug": "default",
                "nombre": None,
                "tipo": None,
                "logo_url": None,
                "dominio": request.host,
                "tema": {},
                "config": {},
                "rubros": rubros_list, # Injected for generic demo
                "is_demo_placeholder": True
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

        if not _try_get_demo_tenant(normalized_slug) and fallback_tenant and normalized_slug in {"municipio", "pyme"}:
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
    tenant_info["tipo_chat"] = tenant.tipo
    if (tenant.tipo or "").lower() == "municipio":
        tenant_info["rubro_publico"] = "municipios"
    tenant_info.setdefault(
        "config", _normalize_widget_config(tenant.configuracion, tenant.widget_settings)
    )
    tenant_info["marketplace"] = _marketplace_meta(tenant)

    # Garantizar que el frontend reciba una estructura de menú consistente
    # aunque el tenant no tenga configuración explícita. El widget espera un
    # objeto con la clave ``children`` para renderizar las secciones sin
    # explotar en una desestructuración.
    config = _normalize_widget_config(tenant_info.get("config"), tenant.widget_settings)

    # Backport updated flat fields to support new frontend requirements in legacy endpoint
    if tenant.widget_settings:
        if tenant.widget_settings.theme_config:
            config["theme_config"] = tenant.widget_settings.theme_config
        if tenant.widget_settings.cta_messages:
            config["cta_messages"] = tenant.widget_settings.cta_messages
        if tenant.widget_settings.default_open is not None:
            config["default_open"] = tenant.widget_settings.default_open

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
        # Try Mock Demos first
        tenant = _try_get_demo_tenant(tenant_slug)
        if not tenant:
            return jsonify({"error": str(exc)}), 404

    is_integration_preview = "/integracion" in (request.headers.get("Referer", "") or "")

    widget_payload = _build_widget_embed_payload(tenant, widget_token)
    payload = {
        "tenant": tenant.to_public_dict(),
        "widget": widget_payload,
        "builder_config": widget_payload.get("builder_config", {}),
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




def _resolve_tenant_for_lead_capture(payload: dict) -> TenantProfile | None:
    tenant_slug = (
        payload.get("tenant_slug")
        or payload.get("tenant")
        or request.args.get("tenant_slug")
        or request.args.get("tenant")
    )
    tenant_slug = str(tenant_slug or "").strip().lower()

    if tenant_slug in {"pyme", "municipio"}:
        return (
            TenantProfile.query.filter_by(tipo=tenant_slug)
            .filter(TenantProfile.is_active.is_(True))
            .order_by(TenantProfile.created_at.asc(), TenantProfile.id.asc())
            .first()
        )

    if tenant_slug:
        try:
            return resolve_tenant_only(tenant_slug=tenant_slug, require_explicit_slug=True)
        except TenantResolutionError:
            return None

    return None


def _build_lead_capture_ack(tenant: TenantProfile | None) -> dict:
    tenant_name = getattr(tenant, "nombre", None) or "nuestro equipo"
    return {
        "ok": True,
        "message_body": f"¡Gracias! Ya registramos tu interés. En breve estaremos en contacto desde {tenant_name}.",
        "respuesta": f"¡Gracias! Ya registramos tu interés. En breve estaremos en contacto desde {tenant_name}.",
        "message_type": "text",
        "fuente": "lead_capture",
    }


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


@public_resolver_bp.route("/lead-capture", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@cross_origin(origins="*", automatic_options=False)
def capture_public_lead():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    payload = request.get_json(silent=True) or {}
    tenant = _resolve_tenant_for_lead_capture(payload)

    nombre = str(payload.get("nombre") or payload.get("name") or "").strip()
    email = str(payload.get("email") or "").strip().lower()
    telefono = str(payload.get("telefono") or payload.get("phone") or "").strip()
    mensaje = str(payload.get("mensaje") or payload.get("message") or "").strip()
    interes = str(payload.get("interes") or payload.get("interest") or "").strip()

    if not (nombre or email or telefono):
        return jsonify({"error": "nombre/email/telefono requerido"}), 400

    anon_id = (
        str(payload.get("anon_id") or "").strip()
        or str(request.headers.get("X-Anon-Id") or "").strip()
        or str(request.cookies.get("chatboc_anon_id") or "").strip()
    )

    user = User.create_or_get_by_anon(anon_id or None, display_name=nombre or "Interesado")
    if nombre:
        user.name = nombre
    if email:
        user.email = email
    if telefono:
        user.telefono = telefono
    if tenant:
        user.tenant_id = tenant.id
        user.tenant_slug = tenant.slug
        if not user.tipo_chat:
            user.tipo_chat = (tenant.tipo or "pyme").lower()

    db.session.add(user)
    db.session.flush()

    session_id = (
        str(payload.get("chat_session_id") or "").strip()
        or str(request.headers.get("X-Chat-Session-Id") or "").strip()
    )

    context_obj = None
    if session_id:
        context_obj = ChatSessionContext.query.get(session_id)
        if not context_obj:
            context_obj = ChatSessionContext(chat_session_id=session_id, anon_id=anon_id or user.anon_id, user_id=user.id)
    elif anon_id:
        context_obj = (
            ChatSessionContext.query
            .filter(ChatSessionContext.anon_id == anon_id)
            .order_by(desc(ChatSessionContext.last_updated))
            .first()
        )

    if context_obj:
        if not context_obj.user_id:
            context_obj.user_id = user.id
        if anon_id and not context_obj.anon_id:
            context_obj.anon_id = anon_id
        if tenant and not context_obj.tenant_id:
            context_obj.tenant_id = tenant.id

        data = context_obj.context_data if isinstance(context_obj.context_data, dict) else {}
        lead_profile = data.get("lead_profile") if isinstance(data.get("lead_profile"), dict) else {}
        if nombre:
            lead_profile["nombre"] = nombre
        if email:
            lead_profile["email"] = email
        if telefono:
            lead_profile["telefono"] = telefono
        if interes:
            lead_profile["interes"] = interes
        if mensaje:
            lead_profile["mensaje"] = mensaje
        if tenant:
            lead_profile["tenant_slug"] = tenant.slug
            lead_profile["tenant_id"] = tenant.id
            lead_profile["tenant_tipo"] = tenant.tipo
        lead_profile["updated_at"] = datetime.now(timezone.utc).isoformat()
        data["lead_profile"] = lead_profile

        events = data.get("lead_events") if isinstance(data.get("lead_events"), list) else []
        events.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "mensaje": mensaje,
            "interes": interes,
            "tenant_slug": tenant.slug if tenant else None,
        })
        data["lead_events"] = events[-20:]
        context_obj.context_data = data
        db.session.add(context_obj)

    lead_question = mensaje or f"Lead capturado ({interes or 'sin_interes'})"
    conv = Conversacion(
        user_id=user.id,
        pyme_id=(tenant.pyme_id if tenant else None) or (tenant.municipio_id if tenant else None),
        pregunta=lead_question,
        respuesta="Lead registrado",
        fuente="lead_capture",
        rubro=(tenant.tipo if tenant else None),
        session_id=anon_id or user.anon_id or str(user.id),
    )
    db.session.add(conv)
    db.session.commit()

    return jsonify(_build_lead_capture_ack(tenant))
