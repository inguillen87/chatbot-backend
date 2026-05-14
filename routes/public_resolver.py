from datetime import datetime, timezone
import hashlib
import json
import os
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import quote_plus, urlparse
from flask import Blueprint, jsonify, request, g, current_app
from flask_cors import cross_origin
from sqlalchemy import desc

from models import AnalyticsEventV2, ChatSessionContext, Conversacion, TenantProfile, TenantTicket, User, WidgetSettings, Rubro, db
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
from services.demo_pillar_catalog import DEMO_PILLAR_CONTRACT_VERSION, demo_pillars
from services.demo_experience_contract import build_demo_experience_contract
from services.landing_experience_contract import (
    LANDING_EXPERIENCE_CONTRACT_VERSION,
    build_landing_experience_contract,
)
from services.education_contracts import (
    build_education_admin_menu,
    build_education_profile,
    build_education_whatsapp_playbook,
    education_quick_menu,
)
from services.realtime_voice_profiles import (
    REALTIME_VOICE_CONTRACT_VERSION,
    build_multilingual_translation_policy,
    build_realtime_voice_capabilities,
    build_realtime_voice_instructions,
    build_realtime_voice_tools,
    infer_realtime_voice_vertical,
    resolve_realtime_fallback_model,
    resolve_realtime_model,
    resolve_realtime_voice,
)

public_resolver_bp = Blueprint("public_resolver_bp", __name__, url_prefix="/api/public")
public_municipios_bp = Blueprint("public_municipios_bp", __name__)
TENANT_PROFILE_CONTRACT_VERSION = "public.tenant_profile.v1"
WIDGET_CONFIG_CONTRACT_VERSION = "public.widget_config.v1"
LEAD_CAPTURE_CONTRACT_VERSION = "public.lead_capture.v1"


def _normalize_public_chat_session_id(value: str | None) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    if len(candidate) <= 36:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:32]
    return f"sid_{digest}"
WIDGET_ONBOARDING_CONTRACT_VERSION = "public.widget_onboarding.v1"


_REALTIME_SESSION_RATE_LIMIT_WINDOW_SECONDS = 60
_REALTIME_SESSION_RATE_LIMIT_MAX_REQUESTS = 20
_REALTIME_SESSION_RATE_BUCKETS: dict[str, list[float]] = {}
_REALTIME_RATE_BUCKET_MAX_KEYS = 10000
_REALTIME_ACTION_EVENT_ALLOWED = {
    "crear_reclamo",
    "crear_pedido",
    "consultas_generales",
    "derivar_humano",
    "consulta_estado_ticket",
    "finalizar_pedido_pyme",
}

OPENAI_REALTIME_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"


def _realtime_audio_format(value: str | dict | None, *, default: str = "pcm16") -> dict:
    if isinstance(value, dict):
        return value
    normalized = str(value or default).strip().lower().replace("-", "_")
    if normalized in {"g711_ulaw", "ulaw", "pcmu", "mulaw", "audio/pcmu"}:
        return {"type": "audio/pcmu"}
    if normalized in {"g711_alaw", "alaw", "pcma", "audio/pcma"}:
        return {"type": "audio/pcma"}
    return {"type": "audio/pcm", "rate": 24000}


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


def _config_flag(cfg: dict | None, *keys: str, default: bool = False) -> bool:
    if not isinstance(cfg, dict):
        return default
    for key in keys:
        if key not in cfg:
            continue
        value = cfg.get(key)
        if isinstance(value, bool):
            return value
        normalized = str(value or "").strip().lower()
        if normalized in {"1", "true", "yes", "si", "sí", "enabled", "on"}:
            return True
        if normalized in {"0", "false", "no", "disabled", "off"}:
            return False
    return default


def _socket_realtime_contract(cfg: dict) -> dict:
    enabled_raw = cfg.get("socket_enabled")
    if enabled_raw is None:
        enabled_raw = cfg.get("live_chat_socket_enabled")
    if enabled_raw is None:
        enabled_raw = current_app.config.get("PUBLIC_SOCKET_IO_ENABLED")
    if enabled_raw is None:
        enabled_raw = os.environ.get("PUBLIC_SOCKET_IO_ENABLED")

    socket_enabled = bool(enabled_raw) if isinstance(enabled_raw, bool) else str(enabled_raw or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "si",
        "sí",
        "enabled",
    }
    socket_url = cfg.get("socket_url") or cfg.get("socket_io_url")
    if socket_enabled and not socket_url:
        socket_url = current_app.config.get("PUBLIC_SOCKET_IO_URL") or os.environ.get("PUBLIC_SOCKET_IO_URL") or "/api/socket.io"

    return {
        "socket_enabled": socket_enabled,
        "socket_url": socket_url if socket_enabled else None,
        "fallback_mode": "socket_io_enabled" if socket_enabled else "polling_disabled",
        "path": "/api/socket.io" if socket_enabled else None,
    }


def _widget_visibility_rules(*, realtime: dict, voice_enabled: bool = True, video_enabled: bool = False) -> dict:
    allow_websocket = bool((realtime or {}).get("socket_enabled"))
    return {
        "contract_version": "widget.visibility_rules.v1",
        "allow_websocket": allow_websocket,
        "allow_realtime_live_chat": allow_websocket,
        "allow_voice_call": bool(voice_enabled),
        "allow_video_call": bool(video_enabled),
        "socket_path": (realtime or {}).get("path") if allow_websocket else None,
        "fallback_mode": (realtime or {}).get("fallback_mode") or "polling_disabled",
    }


def _platform_hostnames() -> set[str]:
    configured = current_app.config.get("PUBLIC_PLATFORM_DOMAINS") or os.environ.get("PUBLIC_PLATFORM_DOMAINS")
    values = {"chatboc.ar", "www.chatboc.ar", "localhost", "127.0.0.1", "::1"}
    if isinstance(configured, str):
        values.update(item.strip().lower() for item in configured.split(",") if item.strip())
    elif isinstance(configured, (list, tuple, set)):
        values.update(str(item).strip().lower() for item in configured if str(item).strip())
    return values


def _is_platform_widget_host() -> bool:
    candidates = [
        request.host,
        request.headers.get("X-Forwarded-Host"),
        request.headers.get("X-Original-Host"),
        request.headers.get("X-Host"),
        request.headers.get("Origin"),
        request.headers.get("Referer"),
    ]
    platform_hosts = _platform_hostnames()
    for value in candidates:
        host = _normalize_header_host(value)
        if host in platform_hosts:
            return True
    return False


def _normalize_header_host(value: str | None) -> str:
    if not value:
        return ""
    first_value = str(value).split(",", 1)[0].strip()
    if "://" in first_value:
        parsed = urlparse(first_value)
        first_value = parsed.netloc or parsed.path
    return first_value.split(":", 1)[0].strip().lower()


def _default_tenant_slug_for_pillar(key: str) -> str:
    return {
        "gobierno": "municipio",
        "empresas": "bodega",
        "educacion": "colegio-demo",
    }.get((key or "").strip().lower(), "bodega")


def _widget_ui_hints(*, mode: str = "tenant") -> dict:
    return {
        "contract_version": "widget.ui_hints.v1",
        "mode": mode,
        "density": "compact",
        "max_visible_quick_replies": 3,
        "collapse_extra_quick_replies": True,
        "composer": {
            "single_row_actions": True,
            "icon_buttons_only": True,
            "show_labels_on_hover": True,
            "hide_disabled_actions": True,
            "send_button_always_visible": True,
        },
        "toolbar": {
            "position": "composer",
            "avoid_header_action_overload": True,
            "show": ["attach_file", "share_location", "record_audio", "emoji"],
            "collapse": ["whatsapp", "voice_call", "video_call", "catalog"],
        },
        "messages": {
            "max_bubble_width": 0.86,
            "compact_system_cards": True,
            "truncate_long_intro": True,
            "show_full_intro_link": True,
        },
        "layout": {
            "mobile_full_height": True,
            "desktop_width_px": 420,
            "desktop_height_px": 680,
            "avoid_nested_cards": True,
        },
        "accessibility": {
            "enabled": True,
            "default_simplified_text": False,
            "allow_dyslexia_mode": True,
            "allow_high_contrast": True,
            "allow_large_controls": True,
            "captions_enabled": True,
            "respect_prefers_reduced_motion": True,
            "single_visible_header_entry": True,
            "touch_target_min_px": 44,
        },
        "rules": {
            "do_not_render_unknown_backend_actions": True,
            "hide_socket_errors_when_realtime_disabled": True,
            "show_request_id_only_in_debug_or_error_details": True,
        },
    }


def _platform_widget_config_payload() -> dict:
    pillars = demo_pillars()
    sector_groups = []
    quick_menu = []
    for pillar in pillars:
        key = str(pillar.get("key") or "").strip()
        if not key:
            continue
        tenant_slug = _default_tenant_slug_for_pillar(key)
        categories = []
        for category in pillar.get("categories") or []:
            categories.append(
                {
                    "slug": category.get("slug"),
                    "label": category.get("label"),
                    "tipo_chat": category.get("tipo_chat"),
                    "vertical": category.get("vertical"),
                    "subvertical": category.get("subvertical"),
                    "tenant_slug": tenant_slug,
                    "sample_prompts": category.get("sample_prompts") or [],
                    "resources": category.get("resources") or [],
                }
            )
        sector_groups.append(
            {
                "key": key,
                "label": pillar.get("label"),
                "description": pillar.get("description"),
                "tenant_slug": tenant_slug,
                "default_rubro": pillar.get("default_rubro"),
                "default_tipo_chat": pillar.get("default_tipo_chat"),
                "vertical": pillar.get("vertical"),
                "categories": categories,
            }
        )
        quick_menu.append(
            {
                "id": f"select_{key}",
                "label": pillar.get("label"),
                "intent": "select_demo_sector",
                "sector": key,
                "tenant_slug": tenant_slug,
                "rubro": pillar.get("default_rubro"),
                "action_id": f"demo_select_sector:{key}",
            }
        )

    experience = build_demo_experience_contract(
        tenant_type="pyme",
        rubro_label="Chatboc",
        max_messages=10,
        vertical=None,
    )
    media_capabilities = experience.get("media_capabilities") or {}
    realtime = _socket_realtime_contract({})
    visibility_rules = _widget_visibility_rules(realtime=realtime, voice_enabled=True, video_enabled=False)
    voice_capabilities = build_realtime_voice_capabilities(None, {}, current_app.config)
    live_status = build_live_chat_status()
    live_status["available"] = False
    live_status["realtime"] = False
    live_status["socket_enabled"] = False
    live_status["socket_url"] = None
    live_status["fallback_mode"] = "polling_disabled"
    support_channels = {
        "live_chat": live_status,
        "whatsapp": {
            "enabled": False,
            "number": None,
            "channel": "whatsapp",
            "media": {"text": True, "image": True, "audio": True, "file": True},
        },
        "voice_call": {
            "enabled": True,
            "channel": "voice_call",
            "provider": "openai_realtime",
            "contract_version": REALTIME_VOICE_CONTRACT_VERSION,
            "capabilities": voice_capabilities,
            "media": {"audio": True, "text": True},
        },
        "video_call": {
            "enabled": False,
            "channel": "video_call",
            "provider": "openai_realtime",
            "reason_code": "video_call_disabled_until_frontend_surface_ready",
            "media": {"audio": True, "video": True, "text": True},
        },
    }
    onboarding = {
        "contract_version": WIDGET_ONBOARDING_CONTRACT_VERSION,
        "mode": "platform_sector_selector",
        "title": "Que queres probar?",
        "subtitle": "Elegi un rubro y el agente abre una demo lista para conversar.",
        "entry_question": "Que tipo de organizacion queres simular?",
        "required_step": "select_sector",
        "autostart_after_selection": True,
        "selection_endpoint": "/api/v2/demo/session",
        "catalog_endpoint": "/api/v2/demo/catalog",
        "chat_header_policy": "use_chat_bootstrap_from_demo_session",
        "quick_menu": quick_menu,
        "sector_groups": sector_groups,
    }
    builder_config = {
        "quick_menu": quick_menu,
        "onboarding": onboarding,
        "demo_catalog": {
            "contract_version": DEMO_PILLAR_CONTRACT_VERSION,
            "sectors": [group["key"] for group in sector_groups],
            "sector_groups": sector_groups,
        },
        "media_capabilities": media_capabilities,
        "support_channels": support_channels,
        "realtime": realtime,
        "visibility_rules": visibility_rules,
        "ui_hints": _widget_ui_hints(mode="platform_selector"),
        "experience_blueprint": experience,
        "animation_tokens": experience.get("animation_tokens") or {},
        "first_visit": experience.get("first_visit") or {},
    }
    return {
        "contract_version": WIDGET_CONFIG_CONTRACT_VERSION,
        "tenant": {
            "slug": "chatboc-platform",
            "tipo": "platform",
            "nombre": "Chatboc",
            "white_label": False,
        },
        "widget": {
            "mode": "platform_selector",
            "quick_menu": quick_menu,
            "onboarding": onboarding,
            "media_capabilities": media_capabilities,
            "support_channels": support_channels,
            "realtime": realtime,
            "visibility_rules": visibility_rules,
            "ui_hints": builder_config["ui_hints"],
        },
        "builder_config": builder_config,
        "quick_menu": quick_menu,
        "onboarding": onboarding,
        "demo_catalog": builder_config["demo_catalog"],
        "media_capabilities": media_capabilities,
        "support_channels": support_channels,
        "realtime": realtime,
        "visibility_rules": visibility_rules,
        "ui_hints": builder_config["ui_hints"],
        "suppress_global_widget": False,
        "integration_preview": False,
    }




def _support_channels_payload(tenant: TenantProfile, cfg: dict) -> dict:
    owner = tenant.pyme or tenant.municipio
    whatsapp_number = (
        cfg.get("support_whatsapp")
        or cfg.get("whatsapp_phone")
        or getattr(tenant, "whatsapp_sender_id", None)
        or getattr(owner, "telefono", None)
    )
    voice_enabled = _config_flag(cfg, "realtime_voice_enabled", default=True)
    realtime_voice = build_realtime_voice_capabilities(tenant, cfg, current_app.config)
    realtime_voice["enabled"] = bool(voice_enabled)
    if not voice_enabled:
        realtime_voice["reason_code"] = "voice_not_enabled"
        realtime_voice.setdefault("features", {})["tool_calling"] = False
    realtime_model = realtime_voice.get("recommended_model")
    realtime_voice_name = realtime_voice.get("voice")
    socket_realtime = _socket_realtime_contract(cfg)
    live_status = build_live_chat_status(
        schedule_override=(cfg.get("live_chat_schedule") if isinstance(cfg.get("live_chat_schedule"), dict) else None)
    )
    live_chat_available = bool(live_status.get("available")) and bool(socket_realtime.get("socket_enabled"))

    return {
        "live_chat": {
            **live_status,
            "available": live_chat_available,
            "channel": "ticket_chat",
            "realtime": bool(socket_realtime.get("socket_enabled")),
            "socket_enabled": bool(socket_realtime.get("socket_enabled")),
            "socket_url": socket_realtime.get("socket_url"),
            "fallback_mode": socket_realtime.get("fallback_mode"),
            "media": {"text": True, "image": True, "audio": True, "file": True},
        },
        "whatsapp": {
            "enabled": bool(whatsapp_number),
            "number": whatsapp_number,
            "channel": "whatsapp",
            "realtime_bridge": True,
            "media": {"text": True, "image": True, "audio": True, "file": True},
        },
        "voice_call": {
            "enabled": bool(voice_enabled),
            "channel": "voice_call",
            "realtime_bridge": True,
            "provider": "openai_realtime",
            "model": realtime_model,
            "fallback_model": realtime_voice.get("fallback_model"),
            "voice": realtime_voice_name,
            "contract_version": REALTIME_VOICE_CONTRACT_VERSION,
            "capabilities": realtime_voice,
            "media": {"audio": True, "text": True},
            "features": {
                "barge_in": True,
                "dtmf_fallback": True,
                "transfer_humano": True,
                "native_speech_to_speech": True,
                "whatsapp_followup": True,
            },
        },
        "video_call": {
            "enabled": bool(cfg.get("realtime_video_enabled", False)),
            "channel": "video_call",
            "provider": "openai_realtime",
            "model": realtime_model,
            "fallback_model": realtime_voice.get("fallback_model"),
            "voice": realtime_voice_name,
            "avatar": {
                "enabled": bool(cfg.get("widget_avatar_enabled", True)),
                "type": cfg.get("widget_avatar_type") or "robot",
                "persona": cfg.get("widget_avatar_persona") or "chatboc_assistant",
            },
            "media": {"audio": True, "video": True, "text": True},
            "features": {
                "captions": True,
                "accessibility": ["voice_only", "subtitles", "keyboard_navigation"],
            },
        },
    }


def _tenant_rubro_profile(tenant: TenantProfile) -> dict:
    owner = tenant.pyme or tenant.municipio
    rubro = getattr(owner, "rubro", None) if owner else None
    rubro_nombre = getattr(rubro, "nombre", None) or ((tenant.tipo or "").title())
    rubro_slug = getattr(rubro, "nombre", None)
    if isinstance(rubro_slug, str):
        rubro_slug = rubro_slug.strip().lower().replace(" ", "-")
    text = str(rubro_nombre or "").strip().lower()
    education_keywords = ("colegio", "escuela", "educacion", "educación", "instituto", "jardin", "jardín")
    is_education = any(keyword in text for keyword in education_keywords)
    institution_type = "general"
    if is_education:
        if "privad" in text:
            institution_type = "private"
        elif "public" in text or "públic" in text or "estatal" in text:
            institution_type = "public"

    profile = {
        "tenant_type": tenant.tipo,
        "rubro_label": rubro_nombre,
        "rubro_slug": rubro_slug or (tenant.tipo or "").lower(),
    }
    if is_education:
        profile["education_profile"] = {
            "is_education": True,
            "institution_type": institution_type,
            "modules": [
                "asistencia",
                "comunicados",
                "agenda_academica",
                "tramites_secretaria",
            ],
        }
    education_profile = build_education_profile(tenant, rubro_label=rubro_nombre)
    if education_profile.get("is_education"):
        profile["education_profile"] = education_profile
    return profile


def _demo_trial_payload_for_widget(tenant: TenantProfile, cfg: dict) -> dict:
    phrase = str(cfg.get("demo_whatsapp_join_phrase") or "join brief-yesterday").strip()
    number = str(cfg.get("demo_whatsapp_number_e164") or "+14155238886").strip() or "+14155238886"
    max_messages = cfg.get("demo_max_messages", 10)
    try:
        max_messages = max(1, min(int(max_messages), 100))
    except (TypeError, ValueError):
        max_messages = 10

    return {
        "enabled": bool(cfg.get("demo_trial_enabled", True)),
        "join_phrase": phrase,
        "display_number": cfg.get("demo_whatsapp_display_number") or "+1 (415) 523-8886",
        "number_e164": number,
        "wa_deeplink": f"https://wa.me/{number.lstrip('+')}?text={quote_plus(phrase)}",
        "limits": {
            "max_messages": max_messages,
            "upgrade_required_for": cfg.get("demo_upgrade_required_for") or ["qdrant_catalogo_completo", "automatizaciones_enterprise"],
            "upgrade_message": cfg.get("demo_upgrade_message") or "Límite demo alcanzado. Activá plan Full para continuar.",
        },
    }


def _quick_menu_for_widget(tenant: TenantProfile) -> list[dict]:
    tipo = (tenant.tipo or "").strip().lower()
    rubro_profile = _tenant_rubro_profile(tenant)
    education = rubro_profile.get("education_profile") if isinstance(rubro_profile, dict) else None
    if isinstance(education, dict) and education.get("is_education"):
        return education_quick_menu(
            tenant,
            institution_type=education.get("institution_type") or "general",
            surface="widget",
        )
        institution_type = education.get("institution_type") or "general"
        return [
            {"id": "menu_asistencia", "label": "Asistencia", "intent": "asistencia_alumno"},
            {"id": "menu_comunicados", "label": "Comunicados", "intent": "comunicados_familias"},
            {"id": "menu_agenda", "label": "Agenda académica", "intent": "agenda_academica"},
            {
                "id": "menu_tramites",
                "label": "Trámites secretaría",
                "intent": "tramites_secretaria",
                "institution_type": institution_type,
            },
        ]
    if tipo == "municipio":
        return [
            {"id": "menu_reclamo", "label": "Crear reclamo", "intent": "iniciar_reclamo"},
            {"id": "menu_sugerencia", "label": "Crear sugerencia", "intent": "enviar_sugerencia"},
            {"id": "menu_estado_ticket", "label": "Estado ticket", "intent": "consultar_ticket"},
            {"id": "menu_heatmap", "label": "Mapa de calor", "intent": "analytics_heatmap"},
        ]
    return [
        {"id": "menu_catalogo", "label": "Ver catálogo", "intent": "ver_catalogo"},
        {"id": "menu_pedido", "label": "Crear pedido", "intent": "crear_pedido"},
        {"id": "menu_estado_pedido", "label": "Estado pedido", "intent": "estado_pedido"},
        {"id": "menu_subir_catalogo", "label": "Subir PDF/Excel", "intent": "subir_catalogo"},
    ]




def _widget_token_allowed_for_tenant(tenant: TenantProfile, token: str | None) -> bool:
    if not token:
        return False

    cfg = _normalize_widget_config(tenant.configuracion, tenant.widget_settings)
    tokens_cfg = cfg.get("widget_tokens")
    tokens: list[str] = []

    if isinstance(tokens_cfg, str):
        tokens = [tokens_cfg]
    elif isinstance(tokens_cfg, list):
        tokens = [str(item) for item in tokens_cfg if item]

    owner = tenant.pyme or tenant.municipio
    owner_token = getattr(owner, "token", None) if owner else None
    if owner_token:
        tokens.append(owner_token)

    return token in set(tokens)


def _check_realtime_rate_limit(*, tenant_slug: str, widget_token: str | None, ip: str | None) -> bool:
    now = datetime.now(timezone.utc).timestamp()
    token_fragment = (widget_token or "missing")[:24]
    bucket_key = f"{tenant_slug}|{ip or 'unknown'}|{token_fragment}"

    # Opportunistic cleanup to prevent unbounded growth in long-running workers.
    if len(_REALTIME_SESSION_RATE_BUCKETS) > _REALTIME_RATE_BUCKET_MAX_KEYS:
        stale_keys = [
            key
            for key, values in _REALTIME_SESSION_RATE_BUCKETS.items()
            if not values or now - max(values) > (_REALTIME_SESSION_RATE_LIMIT_WINDOW_SECONDS * 2)
        ]
        for key in stale_keys[:2000]:
            _REALTIME_SESSION_RATE_BUCKETS.pop(key, None)

    bucket = _REALTIME_SESSION_RATE_BUCKETS.get(bucket_key, [])
    bucket = [ts for ts in bucket if now - ts <= _REALTIME_SESSION_RATE_LIMIT_WINDOW_SECONDS]

    if len(bucket) >= _REALTIME_SESSION_RATE_LIMIT_MAX_REQUESTS:
        _REALTIME_SESSION_RATE_BUCKETS[bucket_key] = bucket
        return False

    bucket.append(now)
    _REALTIME_SESSION_RATE_BUCKETS[bucket_key] = bucket
    return True


def _realtime_rate_limit_headers() -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(_REALTIME_SESSION_RATE_LIMIT_MAX_REQUESTS),
        "X-RateLimit-Window": str(_REALTIME_SESSION_RATE_LIMIT_WINDOW_SECONDS),
    }


def _realtime_error_response(error: str, status: int, **extra):
    payload = {"error": error}
    payload.update(extra)
    response = jsonify(payload)
    response.status_code = status
    for key, value in _realtime_rate_limit_headers().items():
        response.headers[key] = value
    return response


def _audit_realtime_event(tenant: TenantProfile, *, event_name: str, channel: str, metadata: dict | None = None) -> None:
    try:
        db.session.add(
            AnalyticsEventV2(
                tenant_id=tenant.id,
                tenant_type=tenant.tipo,
                event_name=event_name,
                channel=f"realtime_{channel}",
                metadata_payload=metadata or {},
            )
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("[realtime] failed auditing event=%s tenant=%s", event_name, tenant.slug)


def _build_realtime_session_payload(tenant: TenantProfile, cfg: dict, *, channel: str, request_payload: dict | None = None) -> dict:
    """Build OpenAI Realtime 2 client-secret payload for widget voice/video channels."""

    request_payload = request_payload if isinstance(request_payload, dict) else {}
    transports = request_payload.get("transports") if isinstance(request_payload.get("transports"), dict) else {}
    tenant_name = tenant.nombre or "Chatboc"
    requested_model = request_payload.get("model") or request_payload.get("recommended_model")
    model = str(resolve_realtime_model(cfg, current_app.config))
    fallback_model = str(resolve_realtime_fallback_model(cfg, current_app.config))
    voice = str(request_payload.get("voice") or resolve_realtime_voice(cfg, current_app.config))
    transport = str(request_payload.get("transport") or transports.get("browser") or "webrtc")
    requested_profile = str(request_payload.get("profile") or request_payload.get("realtime_profile") or "realtime_voice_native")
    avatar_enabled = bool(cfg.get("widget_avatar_enabled", True))
    voice_vertical = str(request_payload.get("active_vertical") or infer_realtime_voice_vertical(tenant))
    translation_policy = build_multilingual_translation_policy(cfg, current_app.config)
    session_metadata = {
        "tenant_slug": tenant.slug,
        "tenant_type": tenant.tipo,
        "channel": channel,
        "transport": transport,
        "avatar_enabled": avatar_enabled,
        "avatar_type": cfg.get("widget_avatar_type") or "robot",
        "avatar_persona": cfg.get("widget_avatar_persona") or "chatboc_assistant",
        "business_flows": ["crear_reclamo", "crear_pedido", "crear_caso_escolar", "consultas_generales", "derivar_humano"],
        "realtime_profile": requested_profile,
        "active_vertical": voice_vertical,
        "recommended_model": model,
        "fallback_model": fallback_model,
        "requested_model_ignored": str(requested_model) if requested_model and str(requested_model) != model else None,
        "capabilities_contract": REALTIME_VOICE_CONTRACT_VERSION,
        "openai_realtime_contract": "client_secrets.v2",
        "openai_endpoint": "/v1/realtime/client_secrets",
        "translation": translation_policy,
    }

    return {
        "expires_after": {
            "anchor": "created_at",
            "seconds": int(cfg.get("openai_realtime_client_secret_ttl_seconds") or 600),
        },
        "session": {
            "type": "realtime",
            "model": model,
            "output_modalities": ["audio"],
            "instructions": build_realtime_voice_instructions(
                tenant_name=tenant_name,
                vertical=voice_vertical,
                user_name=None,
                user_address=None,
                translation_policy=translation_policy,
            ),
            "audio": {
                "input": {
                    "format": _realtime_audio_format(cfg.get("openai_realtime_input_audio_format") or "pcm16"),
                    "noise_reduction": {"type": cfg.get("openai_realtime_noise_reduction") or "near_field"},
                    "turn_detection": {
                        "type": cfg.get("openai_realtime_turn_detection_type") or "semantic_vad",
                        "eagerness": cfg.get("openai_realtime_semantic_vad_eagerness") or "auto",
                        "create_response": True,
                        "interrupt_response": True,
                    },
                    "transcription": {
                        "model": cfg.get("openai_realtime_transcription_model") or "gpt-4o-mini-transcribe",
                    },
                },
                "output": {
                    "format": _realtime_audio_format(cfg.get("openai_realtime_output_audio_format") or "pcm16"),
                    "voice": voice,
                    "speed": float(cfg.get("openai_realtime_voice_speed") or 1.0),
                },
            },
            "tools": build_realtime_voice_tools(voice_vertical),
            "tool_choice": "auto",
            "max_output_tokens": "inf",
            "tracing": {
                "workflow_name": "chatboc_realtime_voice",
                "group_id": tenant.slug,
                "metadata": {k: v for k, v in session_metadata.items() if k != "translation" and v is not None},
            },
        },
        "metadata": {k: v for k, v in session_metadata.items() if v is not None},
    }


def _openai_realtime_session_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


@public_resolver_bp.route("/realtime/session", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@cross_origin(origins="*", automatic_options=False)
def create_realtime_session():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    payload = request.get_json(silent=True) or {}
    tenant_slug = payload.get("tenant_slug") or request.args.get("tenant")
    widget_token = payload.get("widget_token") or _extract_widget_token()

    if not tenant_slug:
        return _realtime_error_response("tenant_slug_required", 400)

    try:
        tenant = resolve_tenant_only(
            tenant_slug=tenant_slug,
            require_explicit_slug=True,
        )
    except TenantResolutionError as exc:
        return jsonify({"error": str(exc)}), 404

    if not _widget_token_allowed_for_tenant(tenant, widget_token):
        _audit_realtime_event(tenant, event_name="realtime_session_denied", channel="unknown", metadata={"reason": "invalid_widget_token"})
        return _realtime_error_response("widget_token_invalid", 403)

    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if isinstance(client_ip, str) and "," in client_ip:
        client_ip = client_ip.split(",", 1)[0].strip()

    if not _check_realtime_rate_limit(tenant_slug=tenant.slug, widget_token=widget_token, ip=client_ip):
        _audit_realtime_event(tenant, event_name="realtime_session_rate_limited", channel="unknown", metadata={"ip": client_ip})
        return _realtime_error_response("rate_limit_exceeded", 429)

    cfg = _normalize_widget_config(tenant.configuracion, tenant.widget_settings)
    requested_channel = str(payload.get("channel") or "voice").strip().lower()
    channel = "video" if requested_channel == "video" else "voice"

    if channel == "video" and not bool(cfg.get("realtime_video_enabled", False)):
        _audit_realtime_event(tenant, event_name="realtime_session_denied", channel=channel, metadata={"reason": "video_disabled"})
        return _realtime_error_response("video_realtime_disabled", 400)
    if channel == "voice" and not bool(cfg.get("realtime_voice_enabled", True)):
        _audit_realtime_event(tenant, event_name="realtime_session_denied", channel=channel, metadata={"reason": "voice_disabled"})
        return _realtime_error_response("voice_realtime_disabled", 400)

    api_key = current_app.config.get("OPENAI_API_KEY")
    if api_key is None:
        api_key = os.environ.get("OPENAI_API_KEY")
    api_key = str(api_key or "").strip()
    if not api_key:
        _audit_realtime_event(tenant, event_name="realtime_session_failed", channel=channel, metadata={"reason": "openai_api_key_missing"})
        return _realtime_error_response("openai_api_key_missing", 503)

    session_payload = _build_realtime_session_payload(tenant, cfg, channel=channel, request_payload=payload)
    openai_payload = {
        "expires_after": session_payload["expires_after"],
        "session": session_payload["session"],
    }

    req = urllib_request.Request(
        url=OPENAI_REALTIME_CLIENT_SECRETS_URL,
        method="POST",
        data=json.dumps(openai_payload).encode("utf-8"),
        headers=_openai_realtime_session_headers(api_key),
    )

    try:
        with urllib_request.urlopen(req, timeout=12) as response:
            raw = response.read().decode("utf-8")
            session_data = json.loads(raw) if raw else {}
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore") if getattr(exc, "fp", None) else ""
        current_app.logger.warning("[realtime] OpenAI session error %s: %s", exc.code, detail[:500])
        _audit_realtime_event(tenant, event_name="realtime_session_failed", channel=channel, metadata={"reason": "openai_http_error", "status": exc.code})
        return _realtime_error_response("openai_realtime_session_error", 502, upstream_status=exc.code)
    except Exception as exc:
        current_app.logger.exception("[realtime] Failed creating realtime session: %s", exc)
        _audit_realtime_event(tenant, event_name="realtime_session_failed", channel=channel, metadata={"reason": "openai_unavailable"})
        return _realtime_error_response("openai_realtime_unavailable", 502)

    if session_data.get("value") and not session_data.get("client_secret"):
        session_data["client_secret"] = {
            "value": session_data.get("value"),
            "expires_at": session_data.get("expires_at"),
        }

    _audit_realtime_event(tenant, event_name="realtime_session_created", channel=channel, metadata={"model": session_payload["session"].get("model")})

    public_payload = {
        "ok": True,
        "tenant": tenant.slug,
        "channel": channel,
        "model": session_payload["session"].get("model"),
        "avatar": session_payload.get("metadata", {}),
        "session": session_data,
    }
    response = jsonify(public_payload)
    for key, value in _realtime_rate_limit_headers().items():
        response.headers[key] = value
    return _log_widget_public_request(response, tenant, entity_token=widget_token)


@public_resolver_bp.route("/realtime/voice-capabilities", methods=["GET", "OPTIONS"], provide_automatic_options=False)
@public_municipios_bp.route("/public/realtime/voice-capabilities", methods=["GET", "OPTIONS"], provide_automatic_options=False)
@public_municipios_bp.route("/realtime/voice-capabilities", methods=["GET", "OPTIONS"], provide_automatic_options=False)
@cross_origin(origins="*", automatic_options=False)
def realtime_voice_capabilities():
    if request.method == "OPTIONS":
        response = jsonify({"ok": True})
        response.headers["X-Request-Id"] = str(request.headers.get("X-Request-Id") or getattr(g, "request_id", None) or os.urandom(8).hex())
        return response

    widget_token = _extract_widget_token()
    tenant_slug = request.args.get("tenant") or request.args.get("tenant_slug") or request.args.get("slug")
    tenant = None
    cfg = {}
    request_id = str(request.headers.get("X-Request-Id") or getattr(g, "request_id", None) or os.urandom(8).hex())
    g.request_id = request_id

    if tenant_slug or widget_token:
        try:
            tenant = resolve_tenant_only(
                tenant_slug=tenant_slug,
                widget_token=widget_token,
                require_explicit_slug=bool(tenant_slug),
            )
            cfg = _normalize_widget_config(tenant.configuracion, tenant.widget_settings)
        except TenantResolutionError as exc:
            payload = build_realtime_voice_capabilities(None, {}, current_app.config)
            payload["enabled"] = False
            payload.setdefault("features", {})["tool_calling"] = False
            payload["support_channels"] = {
                "voice_call": {
                    "enabled": False,
                    "channel": "voice_call",
                    "provider": "openai_realtime",
                    "session_endpoint": "/api/public/realtime/session",
                }
            }
            payload.update(
                {
                    "request_id": request_id,
                    "status_code": 200,
                    "reason_code": "tenant_resolution_failed",
                    "retryable": False,
                    "action_hint": "send tenant_slug or X-Tenant-Slug",
                    "error": {"code": 404, "message": str(exc)},
                }
            )
            response = jsonify(payload)
            response.headers["X-Request-Id"] = request_id
            return response

    payload = build_realtime_voice_capabilities(tenant, cfg, current_app.config)
    voice_enabled = _config_flag(cfg, "realtime_voice_enabled", default=True)
    payload["enabled"] = voice_enabled
    if not voice_enabled:
        payload["reason_code"] = "voice_not_enabled"
        payload.setdefault("features", {})["tool_calling"] = False
        payload["action_hint"] = "enable realtime_voice_enabled for this tenant"
    payload["request_id"] = request_id
    payload["support_channels"] = {
        "voice_call": {
            "enabled": bool(voice_enabled),
            "channel": "voice_call",
            "provider": "openai_realtime",
            "session_endpoint": "/api/public/realtime/session",
        }
    }
    if tenant:
        payload["tenant"] = {
            "id": tenant.id,
            "slug": tenant.slug,
            "tipo": tenant.tipo,
            "nombre": tenant.nombre,
        }
    response = jsonify(payload)
    response.headers["X-Request-Id"] = request_id
    return response


@public_resolver_bp.route("/realtime/action-event", methods=["POST", "OPTIONS"], provide_automatic_options=False)
@cross_origin(origins="*", automatic_options=False)
def realtime_action_event():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    payload = request.get_json(silent=True) or {}
    tenant_slug = payload.get("tenant_slug") or request.args.get("tenant")
    widget_token = payload.get("widget_token") or _extract_widget_token()
    action_name = str(payload.get("action") or "").strip().lower()
    channel = str(payload.get("channel") or "voice").strip().lower()

    if not tenant_slug:
        return _realtime_error_response("tenant_slug_required", 400)
    if not action_name:
        return jsonify({"error": "action_required"}), 400
    if action_name not in _REALTIME_ACTION_EVENT_ALLOWED:
        return jsonify({"error": "action_not_allowed"}), 400

    try:
        tenant = resolve_tenant_only(tenant_slug=tenant_slug, require_explicit_slug=True)
    except TenantResolutionError as exc:
        return jsonify({"error": str(exc)}), 404

    if not _widget_token_allowed_for_tenant(tenant, widget_token):
        return _realtime_error_response("widget_token_invalid", 403)

    _audit_realtime_event(
        tenant,
        event_name="realtime_business_action_executed",
        channel=channel,
        metadata={
            "action": action_name,
            "session_id": payload.get("session_id"),
            "status": payload.get("status") or "ok",
            "details": {k: v for k, v in (payload.get("details") or {}).items() if isinstance(k, str)} if isinstance(payload.get("details"), dict) else None,
        },
    )
    return jsonify({"ok": True, "action": action_name})


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
        "data-realtime-model": resolve_realtime_model(cfg, current_app.config),
        "data-realtime-fallback-model": build_realtime_voice_capabilities(tenant, cfg, current_app.config).get("fallback_model"),
        "data-realtime-voice": resolve_realtime_voice(cfg, current_app.config),
        "data-realtime-transport": "webrtc",
        "data-realtime-profile": "realtime_voice_native",
        "data-realtime-voice-enabled": str(_config_flag(cfg, "realtime_voice_enabled", default=True)).lower(),
        "data-realtime-video-enabled": str(_config_flag(cfg, "realtime_video_enabled", default=False)).lower(),
        "data-avatar-enabled": str(bool(cfg.get("widget_avatar_enabled", True))).lower(),
        "data-avatar-type": cfg.get("widget_avatar_type") or "robot",
        "data-avatar-persona": cfg.get("widget_avatar_persona") or "chatboc_assistant",
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
    realtime_contract = _socket_realtime_contract(cfg)
    visibility_rules = _widget_visibility_rules(
        realtime=realtime_contract,
        voice_enabled=_config_flag(cfg, "realtime_voice_enabled", default=True),
        video_enabled=_config_flag(cfg, "realtime_video_enabled", default=False),
    )
    rubro_profile = _tenant_rubro_profile(tenant)
    demo_trial = _demo_trial_payload_for_widget(tenant, cfg)
    quick_menu = _quick_menu_for_widget(tenant)
    education_profile = rubro_profile.get("education_profile") if isinstance(rubro_profile, dict) else None
    experience_blueprint = build_demo_experience_contract(
        tenant_type=tenant.tipo,
        rubro_label=rubro_profile.get("rubro_label"),
        max_messages=(demo_trial.get("limits") or {}).get("max_messages", 10),
        vertical=(education_profile or {}).get("vertical"),
        subvertical=(education_profile or {}).get("subvertical"),
        education_profile=education_profile,
    )
    first_visit = experience_blueprint.get("first_visit") or {}
    sample_conversations = experience_blueprint.get("sample_conversations") or []
    trust_signals = experience_blueprint.get("trust_signals") or []
    lead_capture = experience_blueprint.get("lead_capture") or {}
    media_capabilities = experience_blueprint.get("media_capabilities") or {}
    conversion_ctas = experience_blueprint.get("conversion_ctas") or {}
    animation_tokens = experience_blueprint.get("animation_tokens") or {}
    education_payload = None
    if isinstance(education_profile, dict) and education_profile.get("is_education"):
        education_payload = {
            "profile": education_profile,
            "quick_menu": quick_menu,
            "whatsapp_playbook": build_education_whatsapp_playbook(tenant),
            "admin_menu": build_education_admin_menu(tenant),
        }

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
        "realtime": realtime_contract,
        "visibility_rules": visibility_rules,
        "ui_hints": _widget_ui_hints(mode="tenant"),
        "enterprise_iteration": {
            "realtime": {
                **realtime_contract,
                "session_endpoint": "/api/public/realtime/session",
                "capabilities_endpoint": "/api/public/realtime/voice-capabilities",
                "action_event_endpoint": "/api/public/realtime/action-event",
                "required_widget_token": True,
                "model": attrs.get("data-realtime-model"),
                "fallback_model": attrs.get("data-realtime-fallback-model"),
                "voice": attrs.get("data-realtime-voice"),
                "contract_version": REALTIME_VOICE_CONTRACT_VERSION,
                "active_vertical": (support_channels.get("voice_call", {}).get("capabilities") or {}).get("active_vertical"),
                "voice_handoff": {
                    "enabled": True,
                    "supports_whatsapp_followup": True,
                    "supports_confirmation_cards": True,
                    "preferred_channels": ["voice", "whatsapp", "widget"],
                },
                "rate_limit": {
                    "window_seconds": _REALTIME_SESSION_RATE_LIMIT_WINDOW_SECONDS,
                    "max_requests": _REALTIME_SESSION_RATE_LIMIT_MAX_REQUESTS,
                },
            },
            "ux": {
                "must_support": ["captions", "voice_only", "video_to_voice_fallback", "keyboard_navigation"],
                "qa": ["a11y_contrast", "session_reconnect", "action_confirmation_cards"],
            },
        },
        "layout": {
            "position": position or "right",
            "width": width,
            "height": height,
            "closed_size": closed_size,
            "border_radius": border_radius,
        },
        "rubro_profile": rubro_profile,
        "demo_trial": demo_trial,
        "quick_menu": quick_menu,
        "experience_blueprint": experience_blueprint,
        "first_visit": first_visit,
        "sample_conversations": sample_conversations,
        "trust_signals": trust_signals,
        "lead_capture": lead_capture,
        "media_capabilities": media_capabilities,
        "conversion_ctas": conversion_ctas,
        "animation_tokens": animation_tokens,
        "onboarding": {
            "contract_version": WIDGET_ONBOARDING_CONTRACT_VERSION,
            "mode": "tenant_quick_menu",
            "title": welcome_title,
            "subtitle": welcome_subtitle,
            "entry_question": "Que necesitas resolver?",
            "quick_menu": quick_menu,
            "autostart_after_selection": False,
            "chat_header_policy": "use_tenant_widget_config",
        },
    }
    if education_payload:
        builder_config["education"] = education_payload

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
        "realtime": realtime_contract,
        "visibility_rules": visibility_rules,
        "ui_hints": builder_config["ui_hints"],
        "rubro_profile": rubro_profile,
        "demo_trial": demo_trial,
        "quick_menu": quick_menu,
        "experience_blueprint": experience_blueprint,
        "first_visit": first_visit,
        "sample_conversations": sample_conversations,
        "trust_signals": trust_signals,
        "lead_capture": lead_capture,
        "media_capabilities": media_capabilities,
        "conversion_ctas": conversion_ctas,
        "animation_tokens": animation_tokens,
        "onboarding": builder_config["onboarding"],
        "widget_token": canonical_token,
        "widget_token_cookie_name": current_app.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token"),
        "education": education_payload,
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

    cfg.setdefault("realtime_voice_enabled", True)
    cfg.setdefault("realtime_video_enabled", False)
    cfg.setdefault("openai_realtime_model", resolve_realtime_model(app_config=current_app.config))
    cfg.setdefault("widget_avatar_enabled", True)
    cfg.setdefault("widget_avatar_type", "robot")
    cfg.setdefault("widget_avatar_persona", "chatboc_assistant")

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
    if not bool(current_app.config.get("ENABLE_DEMO_MODE", False)):
        return None
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

        # Try Mock Demos first if explicit slug failed and demo mode is enabled
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

        if not tenant and bool(current_app.config.get("ENABLE_DEMO_MODE", False)):
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
                "contract_version": TENANT_PROFILE_CONTRACT_VERSION,
                "tenant": placeholder,
                "warning": {
                    "message": resolution_error,
                    "fallback": "placeholder",
                },
            }
            return _log_widget_public_request(jsonify(payload), tenant)

        if not tenant and not bool(current_app.config.get("ENABLE_DEMO_MODE", False)):
            payload = {
                "contract_version": TENANT_PROFILE_CONTRACT_VERSION,
                "error": {
                    "code": 404,
                    "message": resolution_error or "Tenant no encontrado",
                }
            }
            response = jsonify(payload)
            response.status_code = 404
            return _log_widget_public_request(response, tenant)

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
        return jsonify(
            {
                "contract_version": TENANT_PROFILE_CONTRACT_VERSION,
                "error": {"code": 404, "message": resolution_error or "Tenant no encontrado"},
            }
        ), 404

    payload = {
        "contract_version": TENANT_PROFILE_CONTRACT_VERSION,
        "tenant": tenant_info,
    }
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
    "/landing-experience", methods=["GET", "OPTIONS"], provide_automatic_options=False
)
@cross_origin(origins="*", automatic_options=False)
def landing_experience():
    """Public contract for landing and adjacent marketing/product pages."""

    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    widget_token = _extract_widget_token()
    tenant_slug = request.args.get("tenant") or request.args.get("slug")
    whatsapp_destination_number = request.args.get("whatsapp_destination_number")
    tenant = None

    if tenant_slug or widget_token or whatsapp_destination_number:
        try:
            tenant = resolve_tenant_only(
                whatsapp_destination_number=whatsapp_destination_number,
                widget_token=widget_token,
                tenant_slug=tenant_slug,
                require_explicit_slug=bool(tenant_slug),
            )
        except TenantResolutionError as exc:
            return (
                jsonify(
                    {
                        "contract_version": LANDING_EXPERIENCE_CONTRACT_VERSION,
                        "error": {"code": 404, "message": str(exc)},
                    }
                ),
                404,
            )

    return jsonify(
        build_landing_experience_contract(
            tenant,
            page=request.args.get("page"),
        )
    )


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

    if (
        not widget_token
        and not tenant_slug
        and not whatsapp_destination_number
        and _is_platform_widget_host()
    ):
        response = jsonify(_platform_widget_config_payload())
        return _log_widget_public_request(response, tenant="chatboc-platform")

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
            return (
                jsonify(
                    {
                        "contract_version": WIDGET_CONFIG_CONTRACT_VERSION,
                        "error": {"code": 404, "message": str(exc)},
                    }
                ),
                404,
            )

    is_integration_preview = "/integracion" in (request.headers.get("Referer", "") or "")

    widget_payload = _build_widget_embed_payload(tenant, widget_token)
    payload = {
        "contract_version": WIDGET_CONFIG_CONTRACT_VERSION,
        "tenant": tenant.to_public_dict(),
        "widget": widget_payload,
        "builder_config": widget_payload.get("builder_config", {}),
        "quick_menu": widget_payload.get("quick_menu", []),
        "onboarding": widget_payload.get("onboarding", {}),
        "media_capabilities": widget_payload.get("media_capabilities", {}),
        "conversion_ctas": widget_payload.get("conversion_ctas", {}),
        "animation_tokens": widget_payload.get("animation_tokens", {}),
        "ui_hints": widget_payload.get("ui_hints", {}),
        "visibility_rules": widget_payload.get("visibility_rules", {}),
        "support_channels": widget_payload.get("support_channels", {}),
        "realtime": widget_payload.get("realtime", {}),
        "realtime_voice": (widget_payload.get("support_channels", {}).get("voice_call", {}).get("capabilities")),
        "rubro_profile": widget_payload.get("rubro_profile", {}),
        "experience_blueprint": widget_payload.get("experience_blueprint", {}),
        "education": widget_payload.get("education"),
        # The integration builder renders its own preview iframe; the global
        # site-wide widget bubble must stay hidden to avoid duplicated widgets
        # on /t/[tenant]/integracion.
        "suppress_global_widget": is_integration_preview,
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
        "message_body": f"Gracias! Ya registramos tu interes. En breve estaremos en contacto desde {tenant_name}.",
        "respuesta": f"Gracias! Ya registramos tu interes. En breve estaremos en contacto desde {tenant_name}.",
        "message_type": "text",
        "fuente": "lead_capture",
        "contract_version": LEAD_CAPTURE_CONTRACT_VERSION,
        "frontend_contract": {
            "render_as": "lead_capture_success",
            "title": "Listo, ya tenemos tu consulta",
            "body": "El equipo puede continuar por WhatsApp, email o llamada con el contexto de la demo.",
            "show_retry": False,
        },
        "follow_up": {
            "status": "queued_for_sales",
            "channels": ["whatsapp", "email", "phone"],
            "owner_panel": "superadmin_leads",
        },
    }


def _public_request_id() -> str:
    incoming = str(request.headers.get("X-Request-Id") or "").strip()
    if incoming:
        return incoming[:120]
    seed = f"{datetime.now(timezone.utc).isoformat()}|{request.remote_addr or ''}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _lead_error_response(
    message: str,
    status_code: int,
    reason_code: str,
    action_hint: str,
    *,
    required_fields: list[str] | None = None,
    field_errors: dict[str, str] | None = None,
):
    request_id = _public_request_id()
    response = jsonify(
        {
            "contract_version": LEAD_CAPTURE_CONTRACT_VERSION,
            "ok": False,
            "status_code": status_code,
            "reason_code": reason_code,
            "retryable": status_code >= 500,
            "action_hint": action_hint,
            "required_fields": required_fields or [],
            "field_errors": field_errors or {},
            "request_id": request_id,
            "frontend_contract": {
                "render_as": "lead_capture_validation",
                "title": "Faltan datos para contactarte",
                "body": "Pedimos nombre y al menos un WhatsApp/telefono o email.",
                "focus_first_error": True,
            },
            "error": {"code": status_code, "message": message},
            "message": message,
        }
    )
    response.status_code = status_code
    response.headers["X-Request-Id"] = request_id
    return response


def _lead_idempotency_key(
    *,
    payload: dict,
    tenant: TenantProfile | None,
    anon_id: str,
    email: str,
    telefono: str,
    session_id: str,
    interes: str,
    mensaje: str,
) -> str | None:
    explicit = payload.get("idempotency_key") or payload.get("idempotencyKey") or request.headers.get("Idempotency-Key")
    if explicit:
        return str(explicit).strip()[:120] or None
    if not tenant:
        return None
    source = "|".join(
        [
            "lead_capture",
            str(tenant.id),
            session_id or "",
            anon_id or "",
            email or "",
            telefono or "",
            interes or "",
            mensaje or "",
        ]
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _resolve_or_create_lead_user(
    *,
    tenant: TenantProfile | None,
    anon_id: str,
    nombre: str,
    email: str,
    telefono: str,
) -> User:
    user = User.query.filter_by(email=email).first() if email else None
    if not user:
        user = User.create_or_get_by_anon(anon_id or None, display_name=nombre or "Interesado")

    if nombre:
        user.name = nombre
    if email and (not user.email or user.email.endswith("@passkey.chatboc")):
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
    return user


def _build_lead_ticket(
    *,
    tenant: TenantProfile,
    user: User,
    payload: dict,
    request_id: str,
    idempotency_key: str,
    nombre: str,
    email: str,
    telefono: str,
    mensaje: str,
    interes: str,
    anon_id: str,
    session_id: str,
) -> tuple[TenantTicket, bool]:
    existing = TenantTicket.query.filter_by(tenant_id=tenant.id, fingerprint=idempotency_key).first()
    if existing:
        return existing, True

    channel = str(payload.get("channel") or payload.get("source") or "web_widget").strip().lower() or "web_widget"
    trigger = str(payload.get("trigger") or payload.get("intent") or "lead_capture").strip().lower() or "lead_capture"
    now_iso = datetime.now(timezone.utc).isoformat()
    details = {
        "title": f"Lead capturado - {interes or tenant.nombre or tenant.slug}",
        "priority": "high" if trigger in {"pricing_interest", "demo_interest", "checkout_intent"} else "normal",
        "channel": channel,
        "lead_stage": "nuevo",
        "lead_source": channel,
        "lead_trigger": trigger,
        "lead_score": 70 if trigger in {"pricing_interest", "demo_interest", "checkout_intent"} else 50,
        "lead_reasons": [trigger, "public_lead_capture"],
        "lead_profile": {
            "nombre": nombre,
            "email": email,
            "telefono": telefono,
            "interes": interes,
            "mensaje": mensaje,
            "anon_id": anon_id,
            "chat_session_id": session_id,
            "tenant_slug": tenant.slug,
            "tenant_tipo": tenant.tipo,
            "source": channel,
            "trigger": trigger,
        },
        "lead_timeline": [
            {
                "at": now_iso,
                "type": "lead_created",
                "source": channel,
                "message": mensaje,
                "interest": interes,
                "request_id": request_id,
            }
        ],
        "request_id": request_id,
        "idempotency_key": idempotency_key,
    }
    ticket = TenantTicket(
        tenant_id=tenant.id,
        user_id=user.id,
        categoria="lead_capture",
        descripcion=mensaje or f"Lead capturado desde {channel}",
        estado="nuevo",
        origen=channel[:20],
        datos_extra=details,
        fingerprint=idempotency_key,
    )
    db.session.add(ticket)
    db.session.flush()
    return ticket, False


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
    request_id = _public_request_id()
    tenant = _resolve_tenant_for_lead_capture(payload)

    nombre = str(payload.get("nombre") or payload.get("name") or "").strip()
    email = str(payload.get("email") or "").strip().lower()
    telefono = str(payload.get("telefono") or payload.get("phone") or "").strip()
    mensaje = str(payload.get("mensaje") or payload.get("message") or "").strip()
    interes = str(payload.get("interes") or payload.get("interest") or "").strip()

    field_errors: dict[str, str] = {}
    if not nombre:
        field_errors["name"] = "required"
    if not (email or telefono):
        field_errors["contact"] = "phone_or_email_required"
        field_errors["phone"] = "required_without_email"
        field_errors["email"] = "required_without_phone"

    if field_errors:
        return _lead_error_response(
            "nombre y telefono o email requeridos",
            400,
            "validation_failed",
            "send_name_and_phone_or_email",
            required_fields=["name", "phone_or_email"],
            field_errors=field_errors,
        )

    anon_id = (
        str(payload.get("anon_id") or "").strip()
        or str(request.headers.get("X-Anon-Id") or "").strip()
        or str(request.cookies.get("chatboc_anon_id") or "").strip()
    )

    user = _resolve_or_create_lead_user(
        tenant=tenant,
        anon_id=anon_id,
        nombre=nombre,
        email=email,
        telefono=telefono,
    )

    raw_session_id = (
        str(payload.get("chat_session_id") or "").strip()
        or str(request.headers.get("X-Chat-Session-Id") or "").strip()
    )
    session_id = _normalize_public_chat_session_id(raw_session_id)
    idempotency_key = _lead_idempotency_key(
        payload=payload,
        tenant=tenant,
        anon_id=anon_id,
        email=email,
        telefono=telefono,
        session_id=session_id,
        interes=interes,
        mensaje=mensaje,
    )

    context_obj = None
    if session_id:
        context_obj = ChatSessionContext.query.get(session_id)
        if not context_obj:
            context_obj = ChatSessionContext(
                chat_session_id=session_id,
                anon_id=anon_id or user.anon_id,
                user_id=user.id,
                context_data={"source_chat_session_id": raw_session_id}
                if raw_session_id and raw_session_id != session_id
                else {},
            )
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
        if raw_session_id and raw_session_id != session_id:
            lead_profile["source_chat_session_id"] = raw_session_id
        if idempotency_key:
            lead_profile["idempotency_key"] = idempotency_key
        lead_profile["request_id"] = request_id
        lead_profile["updated_at"] = datetime.now(timezone.utc).isoformat()
        data["lead_profile"] = lead_profile

        events = data.get("lead_events") if isinstance(data.get("lead_events"), list) else []
        events.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "mensaje": mensaje,
            "interes": interes,
            "tenant_slug": tenant.slug if tenant else None,
            "request_id": request_id,
            "idempotency_key": idempotency_key,
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
    lead_ticket = None
    deduplicated = False
    if tenant and idempotency_key:
        lead_ticket, deduplicated = _build_lead_ticket(
            tenant=tenant,
            user=user,
            payload=payload,
            request_id=request_id,
            idempotency_key=idempotency_key,
            nombre=nombre,
            email=email,
            telefono=telefono,
            mensaje=mensaje,
            interes=interes,
            anon_id=anon_id,
            session_id=session_id,
        )
        db.session.add(
            AnalyticsEventV2(
                tenant_id=tenant.id,
                tenant_type=tenant.tipo,
                user_id=user.id,
                anon_id=anon_id or user.anon_id,
                channel=str(payload.get("channel") or payload.get("source") or "web_widget").strip().lower() or "web_widget",
                event_name="lead_capture_created",
                session_id=session_id or anon_id or user.anon_id,
                metadata_payload={
                    "request_id": request_id,
                    "idempotency_key": idempotency_key,
                    "deduplicated": deduplicated,
                    "interest": interes,
                    "message": mensaje,
                    "lead_ticket_id": lead_ticket.id,
                },
                entity_ref=f"tenant_ticket:{lead_ticket.id}",
            )
        )
    db.session.commit()

    body = _build_lead_capture_ack(tenant)
    body.update(
        {
            "request_id": request_id,
            "tenant": {
                "id": tenant.id,
                "slug": tenant.slug,
                "tipo": tenant.tipo,
                "nombre": tenant.nombre,
            }
            if tenant
            else None,
            "lead_id": f"lead_{lead_ticket.id}" if lead_ticket else None,
            "lead": {
                "id": f"lead_{lead_ticket.id}" if lead_ticket else None,
                "status": "created" if lead_ticket else "captured_without_tenant",
            },
            "ticket_id": lead_ticket.id if lead_ticket else None,
            "ticket_type": "tenant_ticket" if lead_ticket else None,
            "status": "nuevo" if lead_ticket else "captured_without_tenant",
            "deduplicated": deduplicated,
            "idempotency_key": idempotency_key,
            "next_actions": [
                {"id": "open_lead", "label": "Abrir lead", "endpoint": f"/api/v2/tickets/{lead_ticket.id}"},
                {"id": "send_whatsapp", "label": "Enviar WhatsApp", "requires": ["phone"]},
                {"id": "send_email", "label": "Enviar email", "requires": ["email"]},
                {"id": "schedule_call", "label": "Agendar llamada", "requires": ["phone_or_email"]},
            ]
            if lead_ticket
            else [],
        }
    )
    response = jsonify(body)
    response.headers["X-Request-Id"] = request_id
    if idempotency_key:
        response.headers["Idempotency-Key"] = idempotency_key
    return response
