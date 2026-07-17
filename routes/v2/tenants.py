from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote_plus

import jwt
from flask import Blueprint, current_app, g, jsonify, request
from flask_cors import cross_origin
from sqlalchemy import func

from models import TenantProfile
from services.plan_access import integration_access_payload
from services.public_tenant_config import sanitize_public_tenant_config
from utils.roles import normalize_tenant_slug, is_generic_tenant_slug

v2_tenants_bp = Blueprint("v2_tenants", __name__, url_prefix="/api/v2/tenants")
TENANT_PROFILE_V2_CONTRACT_VERSION = "public.tenant_profile.v1"


class V2TenantResolutionError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _find_tenant_by_slug(slug: Optional[str]) -> Optional[TenantProfile]:
    cleaned = normalize_tenant_slug(slug)
    if not cleaned or is_generic_tenant_slug(cleaned):
        return None
    return TenantProfile.query.filter(func.lower(TenantProfile.slug) == cleaned).first()


def _find_tenant_by_widget_token(token: Optional[str]) -> Optional[TenantProfile]:
    token = (token or "").strip()
    if not token:
        return None
    try:
        return TenantProfile.query.filter(
            TenantProfile.configuracion["widget_tokens"].astext.contains(token)
        ).first()
    except Exception:
        return None


def _token_from_request() -> Optional[str]:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header.split(" ", 1)[1].strip()
    return None


def _tenant_slug_from_auth_token(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        payload = jwt.decode(token, current_app.config["SECRET_KEY"], algorithms=["HS256"])
    except Exception:
        return None

    slug = payload.get("tenant_slug") or payload.get("tenant")
    if not slug:
        return None
    return str(slug).strip().lower() or None


def _demo_serializer_secret() -> str:
    return str(current_app.config.get("DEMO_SESSION_SECRET") or current_app.config.get("SECRET_KEY"))


def _sign_demo_session(payload: dict[str, Any]) -> str:
    return jwt.encode(payload, _demo_serializer_secret(), algorithm="HS256")


def create_demo_session_token(*, tenant_slug: str, sector: str, rubro: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    demo_payload = {
        "kind": "demo_session",
        "tenant_slug": (tenant_slug or "").strip().lower(),
        "sector": (sector or "").strip().lower(),
        "rubro": (rubro or "").strip().lower() or None,
        "iat": now,
        "exp": now + timedelta(hours=8),
    }
    return _sign_demo_session(demo_payload)


def decode_demo_session_token(raw: Optional[str]) -> Optional[dict[str, Any]]:
    raw = (raw or "").strip()
    if not raw:
        return None

    try:
        payload = jwt.decode(raw, _demo_serializer_secret(), algorithms=["HS256"])
    except Exception:
        return None

    if payload.get("kind") != "demo_session":
        return None

    return payload if isinstance(payload, dict) else None


def _tenant_slug_from_demo_session_token(raw: Optional[str]) -> Optional[str]:
    payload = decode_demo_session_token(raw)
    if not payload:
        return None

    slug = payload.get("tenant_slug")
    if not slug:
        return None
    return str(slug).strip().lower() or None


def resolve_tenant_v2(*, required: bool = True, explicit_slug: Optional[str] = None) -> Optional[TenantProfile]:
    """Resolve tenant for v2 endpoints with strict, explicit-only sources.

    Order:
    1) explicit path param
    2) X-Tenant-Slug header
    3) authenticated JWT tenant_slug
    4) valid widget token or demo session token
    """

    slug_candidates = [
        explicit_slug,
        request.view_args.get("tenant_slug") if isinstance(getattr(request, "view_args", None), dict) else None,
        request.headers.get("X-Tenant-Slug"),
    ]

    auth_tenant = _tenant_slug_from_auth_token(_token_from_request())
    if auth_tenant:
        slug_candidates.append(auth_tenant)

    demo_session_token = (
        request.headers.get("X-Demo-Session")
        or request.headers.get("X-Demo-Session-Id")
        or request.args.get("demo_session_id")
        or request.args.get("session")
    )
    if not demo_session_token and request.method in {"POST", "PUT", "PATCH"}:
        demo_session_token = (request.get_json(silent=True) or {}).get("demo_session_id")
    demo_tenant_slug = _tenant_slug_from_demo_session_token(demo_session_token)
    if demo_tenant_slug:
        slug_candidates.append(demo_tenant_slug)

    for candidate in slug_candidates:
        tenant = _find_tenant_by_slug(candidate)
        if tenant:
            g.v2_tenant = tenant
            return tenant

    widget_token = request.headers.get("X-Widget-Token") or request.args.get("widget_token")
    tenant = _find_tenant_by_widget_token(widget_token)
    if tenant:
        g.v2_tenant = tenant
        return tenant

    if required:
        if any((explicit_slug, request.headers.get("X-Tenant-Slug"), auth_tenant, demo_session_token, widget_token)):
            raise V2TenantResolutionError("Tenant no encontrado", status_code=404)
        raise V2TenantResolutionError("tenant_slug es obligatorio para este endpoint", status_code=400)

    return None


def _frontend_base_url() -> str:
    base = (
        current_app.config.get("PUBLIC_FRONTEND_URL")
        or current_app.config.get("FRONTEND_URL")
        or current_app.config.get("APP_BASE_URL")
        or current_app.config.get("PUBLIC_BASE_URL")
        or "https://www.chatboc.ar"
    )
    return str(base).strip().rstrip("/") or "https://www.chatboc.ar"


def _widget_config_for_profile(tenant: TenantProfile) -> dict[str, Any]:
    cfg = tenant.configuracion.copy() if isinstance(tenant.configuracion, dict) else {}
    widget_settings = getattr(tenant, "widget_settings", None)
    if widget_settings and hasattr(widget_settings, "to_config_dict"):
        try:
            cfg.update(widget_settings.to_config_dict())
        except Exception:
            current_app.logger.debug(
                "[v2_tenants] widget settings serialization failed",
                exc_info=True,
            )

    if not isinstance(cfg.get("menu"), dict):
        cfg["menu"] = {"children": []}
    else:
        cfg["menu"].setdefault("children", [])

    cfg.setdefault("copy", {})
    cfg.setdefault("cta_messages", [])
    cfg.setdefault("theme_config", {})

    channels = cfg.get("channels") if isinstance(cfg.get("channels"), dict) else {}
    channels.setdefault(
        "web_widget",
        {
            "enabled": True,
            "cta": cfg.get("widget_launcher_text") or "Necesitas ayuda?",
            "welcome_message": cfg.get("widget_welcome_message")
            or cfg.get("widget_welcome_subtitle")
            or "Asistente Virtual",
            "brand_name": cfg.get("widget_brand") or tenant.nombre or tenant.slug,
        },
    )
    channels.setdefault(
        "whatsapp",
        {
            "enabled": bool(
                cfg.get("whatsapp_phone") or getattr(tenant, "whatsapp_sender_id", None)
            ),
            "phone": cfg.get("whatsapp_phone") or getattr(tenant, "whatsapp_sender_id", "") or "",
            "cta": "Escribinos por WhatsApp",
            "brand_name": cfg.get("whatsapp_brand") or tenant.nombre or tenant.slug,
        },
    )
    cfg["channels"] = channels
    return sanitize_public_tenant_config(cfg)


def _tenant_profile_v2_payload(tenant: TenantProfile) -> dict[str, Any]:
    base_url = _frontend_base_url()
    market_path = f"/t/{tenant.slug}/market"
    public_catalog_url = f"{base_url}{market_path}"
    whatsapp_share_text = quote_plus(f"Catalogo {tenant.slug} {public_catalog_url}")
    config = _widget_config_for_profile(tenant)
    integration_access = integration_access_payload(tenant)

    payload = tenant.to_public_dict()
    payload.update(
        {
            "contract_version": TENANT_PROFILE_V2_CONTRACT_VERSION,
            "tenant_id": tenant.id,
            "tipo_chat": tenant.tipo,
            "config": config,
            "catalog": {
                "enabled": bool(
                    (tenant.tipo or "").lower() == "pyme"
                    or config.get("catalog_widget_visible")
                ),
                "is_public": True,
                "share_on_intent": True,
                "prefer_pdf_on_whatsapp": bool(config.get("prefer_pdf_on_whatsapp", False)),
                "banner_url": config.get("catalog_banner_url") or config.get("banner_url"),
            },
            "marketplace": {
                "enabled": True,
                "tenant_slug": tenant.slug,
                "tenant_id": tenant.id,
                "tenant_tipo": tenant.tipo,
                "public_cart_url": public_catalog_url,
            },
            "public_base_url": f"{base_url}/t/{tenant.slug}",
            "public_catalog_url": public_catalog_url,
            "public_cart_url": public_catalog_url,
            "whatsapp_share_url": f"https://wa.me/?text={whatsapp_share_text}",
            "integration_access": integration_access,
            "embed_locked": not bool(integration_access.get("enabled")),
            "widget": {
                "support_channels": config.get("channels", {}),
                "realtime_voice": config.get("realtime_voice"),
            },
            "builder_config": (
                config.get("builder_config")
                if isinstance(config.get("builder_config"), dict)
                else {}
            ),
        }
    )
    if (tenant.tipo or "").lower() == "municipio":
        payload["rubro_publico"] = "municipios"
    return payload


@v2_tenants_bp.route(
    "/<string:tenant_slug>/profile",
    methods=["GET", "OPTIONS"],
    provide_automatic_options=False,
)
@cross_origin(origins="*", automatic_options=False)
def get_tenant_profile_v2(tenant_slug: str):
    if request.method == "OPTIONS":
        return jsonify({"ok": True, "contract_version": TENANT_PROFILE_V2_CONTRACT_VERSION})

    try:
        tenant = resolve_tenant_v2(required=True, explicit_slug=tenant_slug)
    except V2TenantResolutionError as exc:
        return (
            jsonify(
                {
                    "contract_version": TENANT_PROFILE_V2_CONTRACT_VERSION,
                    "error": {"code": exc.status_code, "message": exc.message},
                }
            ),
            exc.status_code,
        )

    return jsonify(_tenant_profile_v2_payload(tenant))


@v2_tenants_bp.route("/current", methods=["GET"])
def get_current_tenant_v2():
    try:
        tenant = resolve_tenant_v2(required=True)
    except V2TenantResolutionError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    return jsonify(
        {
            "tenant": {
                "id": tenant.id,
                "slug": tenant.slug,
                "nombre": tenant.nombre,
                "tipo": tenant.tipo,
            }
        }
    )
