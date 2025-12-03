from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request
from flask_cors import cross_origin

from models import TenantProfile, WidgetSettings, db
from services.tenant_resolver import TenantResolutionError, resolve_tenant_only
from routes.auth import solo_admin_requerido, token_requerido
from utils.tenant import get_current_tenant_profile, get_current_tenant_slug


widget_settings_bp = Blueprint(
    "widget_settings",
    __name__,
    url_prefix="/widget-settings",
)
integracion_widget_bp = Blueprint(
    "integracion_widget_settings",
    __name__,
    url_prefix="/integracion",
)


_WIDGET_CORS_ORIGINS = [
    "https://www.chatboc.ar",
    "https://chatboc-demo-widget-oigs.vercel.app",
]


def _tenant_for_user(user) -> TenantProfile | None:
    return (
        getattr(user, "tenant_profile", None)
        or getattr(user, "tenant_profile_municipio", None)
        or getattr(user, "tenant_profile_pyme", None)
        or get_current_tenant_profile()
    )


def _serialize_settings(settings: WidgetSettings, tenant: TenantProfile) -> dict:
    cfg = settings.to_config_dict()
    script_url = current_app.config.get(
        "WIDGET_SCRIPT_URL", "https://www.chatboc.ar/widget.js"
    )
    attrs = {
        "src": script_url,
        "data-tenant": tenant.slug,
        "data-primary-color": cfg["primary_color"],
        "data-secondary-color": cfg["secondary_color"],
        "data-avatar-url": cfg.get("avatar_url") or "",
        "data-welcome-title": cfg.get("welcome_title") or tenant.nombre,
        "data-welcome-subtitle": cfg.get("welcome_subtitle") or "Asistente Virtual",
        "data-font-family": cfg.get("font_family") or "inherit",
        "data-bubble-shape": cfg.get("bubble_shape") or "round",
        "data-default-open": str(cfg.get("default_open", False)).lower(),
        "data-bottom": cfg.get("bottom") or "20px",
        "data-right": cfg.get("side_offset") or "20px",
        "data-singleton": "true",
    }
    snippet_attrs = " ".join(
        f'{key}="{value}"' for key, value in attrs.items() if value is not None
    )
    embed_code = f"<script {snippet_attrs}></script>"

    return {
        **cfg,
        "embed_code": embed_code,
    }


def _resolve_tenant_from_request() -> TenantProfile:
    slug_hint = (
        request.args.get("tenant")
        or request.args.get("tenant_slug")
        or get_current_tenant_slug()
    )
    slug_hint = (slug_hint or "").strip().lower() or None

    alias_map = dict(current_app.config.get("TENANT_ALIAS_MAP", {}) or {})
    alias_target = current_app.config.get("PUBLIC_CATALOG_DEFAULT_TENANT")
    if alias_target:
        alias_map.setdefault("whatsapp", alias_target)
        alias_map.setdefault("pwa", alias_target)

    if slug_hint in alias_map:
        slug_hint = alias_map[slug_hint]

    try:
        tenant = resolve_tenant_only(
            tenant_slug=slug_hint,
            require_explicit_slug=bool(slug_hint),
        )
    except TenantResolutionError as exc:
        raise TenantResolutionError(str(exc))

    if not tenant and not slug_hint:
        tenant = get_current_tenant_profile()

    if not tenant:
        raise TenantResolutionError("Tenant desconocido")

    return tenant


@widget_settings_bp.route("", methods=["GET", "PUT", "OPTIONS"])
@cross_origin()
@token_requerido
@solo_admin_requerido
def manage_settings(current_user):
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    tenant = _tenant_for_user(current_user)
    if not tenant:
        return jsonify({"error": "tenant requerido"}), 400

    settings = tenant.widget_settings or WidgetSettings(tenant=tenant)

    if request.method == "PUT":
        payload = request.get_json(silent=True) or {}
        settings.primary_color = payload.get("primary_color", settings.primary_color)
        settings.secondary_color = payload.get(
            "secondary_color", settings.secondary_color
        )
        settings.avatar_url = payload.get("avatar_url", settings.avatar_url)
        settings.welcome_title = payload.get("welcome_title", settings.welcome_title)
        settings.welcome_subtitle = payload.get(
            "welcome_subtitle", settings.welcome_subtitle
        )
        settings.position = payload.get("position", settings.position)
        settings.bottom = payload.get("bottom", settings.bottom)
        settings.side_offset = payload.get("side_offset", settings.side_offset)
        settings.font_family = payload.get("font_family", settings.font_family)
        settings.bubble_shape = payload.get("bubble_shape", settings.bubble_shape)
        if "default_open" in payload:
            settings.default_open = bool(payload.get("default_open"))

        db.session.add(settings)
        db.session.commit()

    else:
        # Ensure defaults exist for previews even before a first save
        if settings.id is None:
            db.session.add(settings)
            db.session.flush()

    return jsonify(_serialize_settings(settings, tenant))


@integracion_widget_bp.route(
    "/widget-settings", methods=["GET", "OPTIONS"], strict_slashes=False
)
@cross_origin(origins=_WIDGET_CORS_ORIGINS, supports_credentials=True)
def public_widget_settings():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    try:
        tenant = _resolve_tenant_from_request()
    except TenantResolutionError as exc:
        return jsonify({"error": "tenant_desconocido", "detail": str(exc)}), 404

    settings = tenant.widget_settings or WidgetSettings(tenant=tenant)

    # No crear filas nuevas si el tenant aún no guardó su configuración;
    # basta con devolver los defaults del modelo.
    return jsonify(_serialize_settings(settings, tenant))
