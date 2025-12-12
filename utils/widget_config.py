from __future__ import annotations

from typing import Any, Dict

from models import WidgetSettings


def _theme_from_tenant(tenant) -> Dict[str, Any]:
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
        "logo": pick("widget_logo", "logo_widget", "logo", "logo_url", default=getattr(tenant, "logo_url", None)),
        "animation": pick("widget_logo_animation", "logo_animation", default=None),
    }


def _default_theme_config(theme: Dict[str, Any]) -> Dict[str, Any]:
    primary = theme.get("primary") or "#2563eb"
    accent = theme.get("accent") or "#22c55e"
    background = theme.get("background") or "#ffffff"
    text = theme.get("text") or "#1f2937"

    return {
        "mode": "light",
        "light": {
            "primary": primary,
            "secondary": accent,
            "background": background,
            "text": text,
        },
        "dark": {
            "primary": primary,
            "secondary": accent,
            "background": "#111827",
            "text": "#f9fafb",
        },
    }


def _normalize_widget_config(config: dict | None, widget_settings: WidgetSettings | None = None) -> Dict[str, Any]:
    """Return a widget-friendly config with consistent shapes."""

    cfg = config.copy() if isinstance(config, dict) else {}

    menu_config = cfg.get("menu") if isinstance(cfg.get("menu"), dict) else None
    if menu_config is None or "children" not in menu_config:
        cfg.setdefault("menu", {"children": []})

    copy_config = cfg.get("copy") if isinstance(cfg.get("copy"), dict) else None
    if copy_config is None:
        cfg.setdefault("copy", {})

    cta_messages = cfg.get("cta_messages")
    if not isinstance(cta_messages, list):
        cfg["cta_messages"] = []

    theme_config = cfg.get("theme_config") if isinstance(cfg.get("theme_config"), dict) else None
    if theme_config is None:
        cfg.setdefault("theme_config", {})

    if isinstance(widget_settings, WidgetSettings):
        cfg.update(widget_settings.to_config_dict())

    return cfg
