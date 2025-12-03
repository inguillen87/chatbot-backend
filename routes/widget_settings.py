from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request
from flask_cors import cross_origin

from models import TenantProfile, WidgetSettings, db
from routes.auth import solo_admin_requerido, token_requerido
from utils.tenant import get_current_tenant


widget_settings_bp = Blueprint(
    "widget_settings",
    __name__,
    url_prefix="/widget-settings",
)


def _tenant_for_user(user) -> TenantProfile | None:
    return (
        getattr(user, "tenant_profile", None)
        or getattr(user, "tenant_profile_municipio", None)
        or getattr(user, "tenant_profile_pyme", None)
        or get_current_tenant()
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
