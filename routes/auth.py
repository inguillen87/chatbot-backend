# Contenido COMPLETO para: routes/auth.py

from flask import Blueprint, current_app, g, jsonify, make_response, request, url_for
from flask_cors import cross_origin
from services.logic import es_rubro_publico, normalizar_rubro
import os
import re
import unicodedata
from sqlalchemy import func, or_
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm.attributes import flag_modified
from models import (
    ChatSessionContext,
    MunicipioTicket,
    PymeTicket,
    Rubro,
    TenantProfile,
    TicketComentario,
    User,
    generate_token,
)
from extensions import db, limiter
from functools import wraps
import threading
import uuid
import json
from datetime import datetime, timedelta, timezone
import time
import jwt
from jwt import algorithms as jwt_algorithms
import base64
from services.google_auth import login_o_crear_usuario
from services.pymes import get_or_create_pyme_user_by_token
from services.tenant_resolver import resolve_tenant_only
from services.demo_registry import load_demo_rubros
from services.demo_experience_contract import build_demo_experience_contract
from services.auth_notification_service import send_verification_email
from services.clerk_auth_service import (
    ClerkAuthError,
    ClerkNotConfigured,
    build_chatboc_session_payload,
    build_clerk_frontend_contract,
    complete_clerk_onboarding,
    sync_clerk_webhook_event,
    upsert_user_from_clerk,
    verify_clerk_session_token,
    verify_clerk_webhook_signature,
)
from typing import Any, Callable, Dict, Optional
import secrets
from urllib.parse import quote_plus

_DEMO_CATALOG_CACHE: dict[str, Any] = {"payload": None, "expires_at": 0.0, "fingerprint": ""}
_DEMO_RUBROS_CACHE: dict[str, Any] = {
    "items": None,
    "expires_at": 0.0,
    "fingerprint": "",
}
AUTH_DEMO_CONTRACT_VERSION = "auth.demo.v1"


def _demo_catalog_fingerprint() -> str:
    """Return a stable cache fingerprint for config-sensitive demo catalog data."""

    demo_rubros = current_app.config.get("DEMO_RUBROS")
    default_slug = current_app.config.get("DEFAULT_DEMO_TENANT_SLUG")
    return f"{repr(demo_rubros)}|{default_slug or ''}"


def _load_demo_rubros_cached(*, ttl_seconds: float = 30.0) -> list[Any]:
    """Return demo rubros with short-lived caching to reduce login latency."""

    now = time.time()
    fingerprint = _demo_catalog_fingerprint()
    cached_items = _DEMO_RUBROS_CACHE.get("items")
    if (
        cached_items is not None
        and _DEMO_RUBROS_CACHE.get("expires_at", 0.0) > now
        and _DEMO_RUBROS_CACHE.get("fingerprint") == fingerprint
    ):
        return list(cached_items)

    items = list(load_demo_rubros(require_owner=False))
    _DEMO_RUBROS_CACHE["items"] = items
    _DEMO_RUBROS_CACHE["expires_at"] = now + max(1.0, float(ttl_seconds))
    _DEMO_RUBROS_CACHE["fingerprint"] = fingerprint
    return list(items)


def _looks_like_jwt(token: Optional[str]) -> bool:
    """Return True if the given token matches the typical JWT shape."""

    return bool(token and isinstance(token, str) and token.count(".") == 2)


def _looks_like_uuid(value: Optional[str]) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError):
        return False

auth_bp = Blueprint('auth', __name__, url_prefix='/auth')

from utils.auth_helpers import (
    token_requerido,
    obtener_token,
    get_or_create_anon_id,
    generar_token,
    user_from_token,
    get_or_create_entity_token,
    _safe_user_query,
)
from flask_login import current_user
from utils.roles import canonical_role
from utils.plan_limits import limite_para_usuario
from services.plan_config import (
    get_plan_metadata,
    serialize_plan_catalog,
    serialize_plan_for_response,
)
from services.plan_access import (
    integration_access_payload,
    integration_plan_required_payload as build_integration_plan_required_payload,
    plan_allows_full_integrations,
)
from services.rewards import recompensas_service
from services.user_service import (
    build_profile_avatar_policy_contract,
    change_user_email,
    change_user_password,
    create_password_reset_request,
    get_user_profile_identity,
    reset_password_with_token,
    set_user_profile_avatar,
    split_password_reset_token,
    update_user_profile,
)
from services.gcs_service import upload_to_gcs
from utils.map_config import get_map_config
from utils.user_query import user_table_has_tenant_id_column


_OWNER_TOKEN_RESOLVER: Optional[Callable[[User], Optional[str]]] = None


def _user_query():
    """Return a ``User`` query that tolerates optional columns."""

    return _safe_user_query()


def _resolve_owner_token(user: User) -> Optional[str]:
    """Return the persistent entity token for the given user without crashing."""

    global _OWNER_TOKEN_RESOLVER

    resolver = _OWNER_TOKEN_RESOLVER
    if resolver is None:
        candidate = globals().get("get_or_create_owner_entity_token")
        if not callable(candidate):
            try:
                from utils.auth_helpers import get_or_create_owner_entity_token as helper
            except Exception:
                helper = None
            else:
                candidate = helper
        if not callable(candidate):
            try:
                from utils.response_utils import (
                    get_or_create_owner_entity_token as response_helper,
                )
            except Exception:
                response_helper = None
            else:
                candidate = response_helper

        if callable(candidate):
            _OWNER_TOKEN_RESOLVER = resolver = candidate  # type: ignore[assignment]

    owner_user = getattr(g, "owner_user", None) or user

    empresa_id = getattr(owner_user, "empresa_id", None)
    if empresa_id:
        try:
            looked_up = _user_query().get(empresa_id)
        except Exception:
            looked_up = None
        else:
            if looked_up:
                owner_user = looked_up

    token_value = getattr(owner_user, "entity_token", None)
    if token_value and _looks_like_jwt(token_value):
        token_value = None
    if token_value:
        if not getattr(owner_user, "token", None) or _looks_like_jwt(getattr(owner_user, "token", None)):
            try:
                owner_user.token = token_value
                db.session.add(owner_user)
                db.session.commit()
            except Exception:
                current_app.logger.exception(
                    "[auth] Failed to mirror owner entity token for user %s",
                    getattr(owner_user, "id", None),
                )
                db.session.rollback()
        return getattr(owner_user, "entity_token", None) or token_value

    if callable(resolver):
        try:
            resolved = resolver(owner_user)
        except Exception:
            current_app.logger.exception(
                "[auth] Owner token helper failed for user %s", getattr(user, "id", None)
            )
        else:
            if resolved and not _looks_like_jwt(resolved):
                changed = False
                if not getattr(owner_user, "entity_token", None) or _looks_like_jwt(getattr(owner_user, "entity_token", None)):
                    owner_user.entity_token = resolved
                    changed = True
                if not getattr(owner_user, "token", None) or _looks_like_jwt(getattr(owner_user, "token", None)):
                    owner_user.token = resolved
                    changed = True
                if changed:
                    try:
                        db.session.add(owner_user)
                        db.session.commit()
                    except Exception:
                        current_app.logger.exception(
                            "[auth] Failed to persist resolved owner token for user %s",
                            getattr(owner_user, "id", None),
                        )
                        db.session.rollback()
                return resolved

    if not owner_user:
        return None


def _tenant_for_owner(owner: Optional[User]) -> Optional[TenantProfile]:
    """Return the TenantProfile owned by ``owner`` if present."""

    if owner is None:
        return None

    return (
        TenantProfile.query.filter(
            or_(
                TenantProfile.municipio_id == owner.id,
                TenantProfile.pyme_id == owner.id,
            )
        )
        .order_by(TenantProfile.id.desc())
        .first()
    )


def _tenant_owner(tenant: Optional[TenantProfile]) -> Optional[User]:
    """Resolve the User owner of a tenant profile."""
    if not tenant:
        return None

    if tenant.municipio_id:
        return _user_query().get(tenant.municipio_id)
    if tenant.pyme_id:
        return _user_query().get(tenant.pyme_id)

    return None

def _attach_user_to_tenant(user: User, tenant: Optional[TenantProfile]) -> bool:
    """Persist tenant binding only when it changed and return whether it changed."""

    if not tenant or not user:
        return False

    # Validar si la columna existe antes de intentar asignarla para evitar errores 500
    # si la migración no se ha aplicado.
    changed = False
    if hasattr(user, "tenant_id") and user_table_has_tenant_id_column():
        if user.tenant_id != tenant.id:
            user.tenant_id = tenant.id
            changed = True

    current_slug = getattr(user, "tenant_slug", None)
    if current_slug != tenant.slug:
        user.tenant_slug = tenant.slug
        changed = True

    if changed:
        db.session.add(user)
    return changed


def _tenant_market_payload(tenant: Optional[TenantProfile]) -> Dict[str, object]:
    """Expose marketplace hints so the widget can redirect after login."""

    if not tenant:
        return {"enabled": False}

    # Import inside function to avoid circular dependency
    # However, since pwa_public imports auth at top level, we must be careful.
    # To break the cycle safely, we can duplicate the simple logic or ensure
    # pwa_public is fully loaded before auth.
    # Given the crash, we'll implement a safe local helper or verify import order.

    try:
        from routes.pwa_public import _build_public_cart_url
        full_url, _, _ = _build_public_cart_url(tenant)
    except ImportError:
        # Fallback if pwa_public cannot be imported due to circularity
        base = (request.url_root or "").rstrip("/")
        slug = tenant.slug
        full_url = f"{base}/m/{slug}"

    enabled = (tenant.tipo or "").lower() == "pyme"

    payload: Dict[str, object] = {
        "enabled": enabled,
        "tenant_id": tenant.id,
        "tenant_slug": tenant.slug,
        "tenant_tipo": tenant.tipo,
        "public_cart_url": full_url,
    }

    catalog_enabled = tenant.configuracion.get("widget_catalog_enabled") if isinstance(tenant.configuracion, dict) else None
    if isinstance(catalog_enabled, bool):
        payload["catalog_enabled"] = catalog_enabled
    else:
        payload["catalog_enabled"] = enabled

    return payload

    legacy_value = getattr(owner_user, "token", None)
    if legacy_value and not _looks_like_jwt(legacy_value):
        try:
            owner_user.entity_token = legacy_value
            db.session.add(owner_user)
            db.session.commit()
            return legacy_value
        except Exception:
            current_app.logger.exception(
                "[auth] Failed to persist legacy owner token for user %s",
                getattr(owner_user, "id", None),
            )
            db.session.rollback()

    return get_or_create_entity_token(owner_user)


# --- Entity token propagation helpers -------------------------------------------------


def _include_entity_token_fields(
    payload: Dict[str, Any], owner_token: Optional[str]
) -> Optional[str]:
    token_value = owner_token or payload.get("entity_token") or payload.get(
        "entityToken"
    ) or payload.get("owner_token")
    if _looks_like_jwt(token_value):
        token_value = None

    if token_value:
        payload.setdefault("entity_token", token_value)
        payload.setdefault("entityToken", token_value)
        payload.setdefault("owner_token", token_value)

    return token_value


def _integration_plan_required_payload(
    tenant: Optional[TenantProfile],
    *,
    contract_version: str | None = None,
) -> Dict[str, Any]:
    return build_integration_plan_required_payload(
        tenant,
        "widget_embed",
        contract_version=contract_version,
        render_as="integration_locked",
        hide_embed_copy=True,
        hide_widget_session=True,
    )


def _generate_email_verification_token() -> str:
    return secrets.token_urlsafe(48)


def _send_verification_email(user: User):
    """Send verification mail through the configured transactional provider."""

    if not user.email_verification_token:
        return
    send_verification_email(user, reason="legacy_signup")


def _extract_bearer_token() -> Optional[str]:
    header = request.headers.get("Authorization") or ""
    if header.lower().startswith("bearer "):
        return header.split(" ", 1)[1].strip()
    return None


def _clerk_profile_from_payload(data: dict) -> dict:
    profile = data.get("user") or data.get("clerk_user") or data.get("profile") or {}
    return profile if isinstance(profile, dict) else {}


@auth_bp.route("/clerk/config", methods=["GET"])
@cross_origin()
def clerk_config():
    """Frontend contract for Clerk-based auth and tenant onboarding."""

    return jsonify(build_clerk_frontend_contract())


@auth_bp.route("/clerk/session", methods=["POST"])
@cross_origin()
def clerk_session_sync():
    """Verify a Clerk session JWT and exchange it for a Chatboc JWT."""

    data = request.get_json(silent=True) or {}
    clerk_token = _extract_bearer_token() or data.get("clerk_token") or data.get("session_token")
    try:
        claims = verify_clerk_session_token(clerk_token)
        user = upsert_user_from_clerk(claims, _clerk_profile_from_payload(data))
        if not user.email_verified and not user.email_verification_token:
            user.email_verification_token = _generate_email_verification_token()
            user.email_verification_sent_at = datetime.now(timezone.utc)
        db.session.add(user)
        db.session.commit()

        if not user.email_verified and user.email_verification_token:
            _send_verification_email(user)

        payload = build_chatboc_session_payload(user)
        status_code = 200 if not payload.get("onboarding", {}).get("required") else 202
        return jsonify(payload), status_code
    except ClerkNotConfigured as exc:
        current_app.logger.warning("[clerk_auth] Not configured: %s", exc)
        return jsonify({"error": "Clerk auth is not configured", "reason_code": "clerk_not_configured"}), 503
    except ClerkAuthError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc), "reason_code": "invalid_clerk_session"}), 401
    except Exception as exc:  # pragma: no cover - defensive guard
        db.session.rollback()
        current_app.logger.error("[clerk_auth] Session sync failed: %s", exc, exc_info=True)
        return jsonify({"error": "Error interno", "reason_code": "clerk_session_failed"}), 500


@auth_bp.route("/clerk/onboarding", methods=["POST"])
@cross_origin()
def clerk_onboarding():
    """Complete tenant creation for a Clerk-authenticated owner."""

    data = request.get_json(silent=True) or {}
    clerk_token = _extract_bearer_token() or data.get("clerk_token") or data.get("session_token")
    try:
        claims = verify_clerk_session_token(clerk_token)
        user = upsert_user_from_clerk(claims, _clerk_profile_from_payload(data))
        db.session.commit()
        tenant = complete_clerk_onboarding(user, data)
        payload = build_chatboc_session_payload(user, tenant)
        payload["message"] = "Tenant creado y onboarding completado"
        return jsonify(payload), 201
    except ClerkNotConfigured as exc:
        current_app.logger.warning("[clerk_auth] Onboarding not configured: %s", exc)
        return jsonify({"error": "Clerk auth is not configured", "reason_code": "clerk_not_configured"}), 503
    except ClerkAuthError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc), "reason_code": "invalid_clerk_onboarding"}), 400
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc), "reason_code": "tenant_onboarding_invalid"}), 400
    except Exception as exc:  # pragma: no cover - defensive guard
        db.session.rollback()
        current_app.logger.error("[clerk_auth] Onboarding failed: %s", exc, exc_info=True)
        return jsonify({"error": "Error interno", "reason_code": "clerk_onboarding_failed"}), 500


@auth_bp.route("/clerk/webhook", methods=["POST"])
def clerk_webhook():
    """Receive Clerk user lifecycle events and keep local users synced."""

    raw_body = request.get_data() or b""
    try:
        verify_clerk_webhook_signature(raw_body, request.headers)
        event = json.loads(raw_body.decode("utf-8") or "{}")
        result = sync_clerk_webhook_event(event)
        return jsonify(result), 200
    except ClerkNotConfigured as exc:
        current_app.logger.warning("[clerk_auth] Webhook not configured: %s", exc)
        return jsonify({"error": "Clerk webhook is not configured", "reason_code": "clerk_webhook_not_configured"}), 503
    except (ClerkAuthError, json.JSONDecodeError) as exc:
        db.session.rollback()
        return jsonify({"error": str(exc), "reason_code": "invalid_clerk_webhook"}), 400
    except Exception as exc:  # pragma: no cover - defensive guard
        db.session.rollback()
        current_app.logger.error("[clerk_auth] Webhook failed: %s", exc, exc_info=True)
        return jsonify({"error": "Error interno", "reason_code": "clerk_webhook_failed"}), 500


def _tenant_for_user(user: User):
    if not user:
        return None
    return TenantProfile.query.filter(
        (TenantProfile.municipio_id == user.id) | (TenantProfile.pyme_id == user.id)
    ).first()

def _resolve_tipo_chat(
    user: User,
    tenant_obj: Optional[TenantProfile] = None,
    rubro_nombre: Optional[str] = None,
) -> str:
    if tenant_obj is not None and hasattr(tenant_obj, "tipo") and tenant_obj.tipo:
        return str(tenant_obj.tipo).lower()

    if getattr(user, "tipo_chat", None):
        return str(user.tipo_chat).lower()

    rubro_value = rubro_nombre
    if not rubro_value:
        rubro_obj = getattr(user, "rubro", None)
        rubro_value = getattr(rubro_obj, "nombre", None) or rubro_obj

    return "municipio" if es_rubro_publico(rubro_value) else "pyme"


def _resolve_tenant_for_user(
    user: User,
    tenant_hint: Optional[TenantProfile] = None,
) -> Optional[TenantProfile]:
    if tenant_hint is not None and isinstance(tenant_hint, TenantProfile):
        return tenant_hint
    if tenant_hint:
        try:
            tenant_obj = resolve_tenant_only(tenant_slug=str(tenant_hint))
        except Exception:
            tenant_obj = None
        if tenant_obj:
            return tenant_obj

    tenant_obj = _tenant_for_user(user)
    if tenant_obj:
        return tenant_obj

    if getattr(user, "tenant_id", None):
        tenant_obj = TenantProfile.query.get(user.tenant_id)
        if tenant_obj:
            return tenant_obj

    tenant_slug = getattr(user, "tenant_slug", None)
    if tenant_slug:
        try:
            tenant_obj = resolve_tenant_only(tenant_slug=tenant_slug)
        except Exception:
            tenant_obj = None
        if tenant_obj:
            return tenant_obj

    empresa_id = getattr(user, "empresa_id", None)
    if empresa_id:
        owner_user = _user_query().get(empresa_id)
        if owner_user:
            return _tenant_for_owner(owner_user) or _tenant_for_user(owner_user)

    return None


def _apply_welcome_points_if_configured(user: User):
    try:
        recompensas_service().apply_welcome_points(user, _tenant_for_user(user))
    except Exception as exc:  # pragma: no cover - logging only
        current_app.logger.warning("[welcome_points] No se pudieron acreditar puntos: %s", exc)


def _timestamp_to_iso(value: Optional[object]) -> Optional[str]:
    """Convert a timestamp-like value to an ISO8601 string."""

    if value in (None, ""):
        return None

    try:
        ts_int = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None

    try:
        return datetime.utcfromtimestamp(ts_int).replace(microsecond=0).isoformat() + "Z"
    except (OverflowError, OSError, ValueError):
        return None


_CAPABILITY_ALIASES = {
    "crm.tickets.read": "tickets.read",
    "crm.tickets.admin": "tickets.read",
    "tickets.admin": "tickets.read",
    "claims.read": "tickets.read",
    "claims.admin": "tickets.read",
    "reclamos.read": "tickets.read",
    "reclamos.admin": "tickets.read",
    "crm_reclamos": "tickets.read",
    "tickets_read": "tickets.read",
    "tickets_update": "tickets.write",
    "tickets_assign": "tickets.assign",
    "pedidos.read": "market.orders.read",
    "orders.read": "market.orders.read",
    "commerce.orders.read": "market.orders.read",
}


def _flatten_capability_values(raw: object) -> list[str]:
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(raw, dict):
        values: list[str] = []
        for key, enabled in raw.items():
            if enabled:
                values.append(str(key).strip())
        return [value for value in values if value]
    if isinstance(raw, (list, tuple, set)):
        values = []
        for item in raw:
            values.extend(_flatten_capability_values(item))
        return values
    return [str(raw).strip()] if str(raw).strip() else []


def _normalize_capability_tokens(*raw_values: object) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()

    for raw in raw_values:
        for value in _flatten_capability_values(raw):
            token = value.strip().lower()
            if not token:
                continue
            canonical = _CAPABILITY_ALIASES.get(token, token)
            for candidate in (token, canonical):
                if candidate and candidate not in seen:
                    normalized.append(candidate)
                    seen.add(candidate)

    return normalized


def _profile_capabilities_for_user(user: User) -> list[str]:
    """Return frontend-facing capability tokens from role and stored profile scope."""

    role = canonical_role(getattr(user, "rol", None))
    metadata = getattr(user, "accesibilidad", None)
    if not isinstance(metadata, dict):
        metadata = {}

    employee_scope = metadata.get("employee_scope") if isinstance(metadata.get("employee_scope"), dict) else {}
    raw_values: list[object] = [
        metadata.get("permissions"),
        metadata.get("permisos"),
        metadata.get("capabilities"),
        metadata.get("scopes"),
        employee_scope.get("permissions"),
        employee_scope.get("permisos"),
        employee_scope.get("capabilities"),
        employee_scope.get("scopes"),
    ]

    if role in {"admin", "empleado", "super_admin"}:
        raw_values.append(["tickets.read", "crm.tickets.read", "reclamos.read"])
    if role == "admin":
        raw_values.append(["settings.tenant.write", "market.catalog.write", "market.orders.read"])
    if role == "super_admin":
        raw_values.append(["*", "tickets.admin", "settings.tenant.write"])
    if getattr(user, "ticket_categorias", None):
        raw_values.append("tickets.read")

    return _normalize_capability_tokens(*raw_values)


@auth_bp.route('/plans', methods=['GET'])
@cross_origin()
def public_plan_catalog():
    """Expose the available subscription plans for the frontend."""

    return jsonify({"planes": serialize_plan_catalog()})


def build_profile_payload(user: User) -> Dict[str, Any]:
    """Assemble the profile payload shared by the legacy and new endpoints."""

    tenant_profile = None
    rubro_obj = getattr(user, "rubro", None)
    rubro_nombre = getattr(rubro_obj, "nombre", None) or "General"

    try:
        rubro_es_publico = es_rubro_publico(rubro_obj or rubro_nombre)
    except Exception:
        rubro_es_publico = False

    owner_user = None
    if getattr(user, "empresa_id", None):
        owner_user = _user_query().get(user.empresa_id)

    tenant_profile = getattr(g, "tenant_profile", None) or getattr(g, "current_tenant", None)
    tenant_profile = _resolve_tenant_for_user(user, tenant_profile)
    if not tenant_profile and owner_user:
        tenant_profile = _resolve_tenant_for_user(owner_user)
    if not tenant_profile:
        token_payload = getattr(g, "token_payload", {}) or {}
        tenant_slug_hint = token_payload.get("tenant_slug") or token_payload.get("tenant")
        if tenant_slug_hint:
            try:
                tenant_profile = resolve_tenant_only(tenant_slug=str(tenant_slug_hint))
            except Exception:
                tenant_profile = None

    tipo_chat = _resolve_tipo_chat(user, tenant_obj=tenant_profile, rubro_nombre=rubro_nombre)
    catalogo_label = (
        "Cargar Catálogo de Trámites" if tipo_chat == "municipio" else "Cargar Catálogo de Productos"
    )
    integration_guide_url = current_app.config.get(
        "INTEGRATION_GUIDE_URL",
        "https://docs.chatboc.ar/widget-integration",
    ) or "https://docs.chatboc.ar/widget-integration"

    plan_value = (
        tenant_profile.plan
        if tenant_profile and tenant_profile.plan
        else getattr(owner_user, "plan", None)
        or getattr(user, "plan", None)
    )

    profile_identity = get_user_profile_identity(user)
    profile_avatar_url = profile_identity.get("avatar_url")
    profile_avatar_source = profile_identity.get("avatar_source")
    profile_avatar_consent = bool(profile_identity.get("avatar_consent"))
    avatar_policy_contract = build_profile_avatar_policy_contract()
    profile_identity_payload = {
        "avatar_url": profile_avatar_url,
        "picture": profile_avatar_url,
        "avatar_source": profile_avatar_source,
        "avatar_consent": profile_avatar_consent,
        "profile_picture_consent": profile_avatar_consent,
        **avatar_policy_contract,
    }
    profile_capabilities = _profile_capabilities_for_user(user)

    profile_data: Dict[str, Any] = {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "rol": user.rol,
        "empresa_id": user.empresa_id,
        "rubro": rubro_nombre,
        "tipo_chat": tipo_chat,
        "categorias": getattr(user, "categorias_lista", []),
        "nombre_empresa": getattr(user, "nombre_empresa", None),
        "badge_tipo": getattr(user, "badge_tipo", None),
        "catalogo_label": catalogo_label,
        "integration_guide_url": integration_guide_url,
        "ciudad": getattr(user, "ciudad", None),
        "color_primario": getattr(user, "color_primario", None),
        "color_secundario": getattr(user, "color_secundario", None),
        "plan": plan_value,
        "limite_preguntas": limite_para_usuario(user),
        "acepta_marketing": getattr(user, "acepta_marketing", None),
        "tags": getattr(user, "tags", None),
        "horario": getattr(user, "horario", None),
        "horario_json": getattr(user, "horario_json", None),
        "telefono": getattr(user, "telefono", None),
        "direccion": getattr(user, "direccion", None),
        "provincia": getattr(user, "provincia", None),
        "pais": getattr(user, "pais", None),
        "latitud": getattr(user, "latitud", None),
        "longitud": getattr(user, "longitud", None),
        "link_web": getattr(user, "link_web", None),
        "logo_url": getattr(user, "logo_url", None),
        "avatar_url": profile_avatar_url,
        "picture": profile_avatar_url,
        "avatar_source": profile_avatar_source,
        "avatar_consent": profile_avatar_consent,
        "profile_picture_consent": profile_avatar_consent,
        "identity": profile_identity_payload,
        "permissions": profile_capabilities,
        "capabilities": profile_capabilities,
        "scopes": profile_capabilities,
        "preguntas_usadas": getattr(user, "preguntas_usadas", None),
    }

    profile_data["map_config"] = get_map_config()

    plan_metadata = get_plan_metadata(profile_data.get("plan"))
    profile_data["plan_detalle"] = serialize_plan_for_response(plan_metadata)
    profile_data["planes_disponibles"] = serialize_plan_catalog()
    integration_access = integration_access_payload(tenant_profile)
    profile_data["integration_access"] = integration_access
    profile_data["integrations_locked"] = not bool(integration_access.get("enabled"))
    profile_data["widget_embed_token"] = None
    profile_data["widget_embed_token_kind"] = "plan_required"
    tenant_slug_value = (
        getattr(user, "tenant_slug", None)
        or getattr(tenant_profile, "slug", None)
    )
    profile_data["tenant_slug"] = tenant_slug_value
    profile_data["tenantSlug"] = tenant_slug_value
    profile_data["rubro_id"] = getattr(user, "rubro_id", None)
    profile_data["rubro_clave"] = getattr(rubro_obj, "clave", None) if rubro_obj else None
    profile_data["profile_sections"] = _dashboard_panels_for_user(user, tipo_chat)
    profile_data["requires_rubro_selection"] = bool(not getattr(user, "rubro_id", None) and tipo_chat == "pyme")

    owner_token = _resolve_owner_token(user)
    widget_session_active = bool(getattr(g, "widget_session", False))
    widget_owner = getattr(g, "widget_owner_user", None)
    auth_token = getattr(g, "auth_token", None)
    token_payload = getattr(g, "token_payload", {}) or {}

    if auth_token:
        profile_data["token"] = auth_token
        profile_data["auth_token"] = auth_token
    else:
        fallback_token = owner_token or getattr(user, "token", None)
        if fallback_token and not _looks_like_jwt(fallback_token):
            profile_data["token"] = fallback_token

    profile_data["auth_token"] = auth_token

    session_kind = token_payload.get("session_kind")
    if not session_kind:
        session_kind = "widget" if widget_session_active else ("panel" if auth_token else "none")

    profile_data["session_token"] = auth_token
    profile_data["session_kind"] = session_kind
    profile_data["session_expires_at"] = _timestamp_to_iso(token_payload.get("exp"))
    profile_data["session_renew_until"] = _timestamp_to_iso(token_payload.get("renew_until"))
    profile_data["widget_session_active"] = widget_session_active
    profile_data["widget_session_owner_id"] = getattr(widget_owner, "id", None)

    if owner_token:
        profile_data["entity_token"] = owner_token
        profile_data["entityToken"] = owner_token
        profile_data["owner_token"] = owner_token
        if integration_access.get("enabled") and not widget_session_active:
            profile_data["widget_embed_token"] = owner_token
            profile_data["widget_embed_token_kind"] = "entity"
    elif getattr(user, "token", None) and not _looks_like_jwt(user.token):
        profile_data["entity_token"] = user.token
        profile_data.setdefault("entityToken", user.token)
        if integration_access.get("enabled") and not widget_session_active:
            profile_data.setdefault("widget_embed_token", user.token)
            profile_data.setdefault("widget_embed_token_kind", "legacy")

    widget_cookie_name = current_app.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token")
    profile_data["widget_token_cookie_name"] = widget_cookie_name
    profile_data["widget_access_minutes"] = current_app.config.get("WIDGET_ACCESS_MINUTES", 45)
    profile_data["widget_renew_days"] = current_app.config.get("WIDGET_RENEW_DAYS", 7)

    return profile_data


def _now():
    return int(datetime.now(timezone.utc).timestamp())


def _conf(k, d):
    try:
        return int(os.getenv(k, d))
    except Exception:
        return d


def _sign(payload, minutes, renew_days=None):
    widget_alg = str(current_app.config.get("WIDGET_JWT_ALG", "HS256")).upper()
    widget_kid = current_app.config.get("WIDGET_JWT_KID", "widget-hs256")
    widget_private_key = (
        current_app.config.get("WIDGET_JWT_PRIVATE_KEY")
        or current_app.config.get("WIDGET_JWT_SECRET")
        or current_app.config.get("SECRET_KEY")
    )

    payload = dict(payload)
    payload.setdefault("session_kind", "widget")
    now_ts = _now()
    payload.update({"iat": now_ts - 1, "exp": now_ts + minutes * 60})
    if renew_days:
        payload["renew_until"] = now_ts + renew_days * 86400
    tok = jwt.encode(payload, widget_private_key, algorithm=widget_alg, headers={"kid": widget_kid})
    return tok, payload["exp"]


def _refresh(tok, minutes):
    widget_alg = str(current_app.config.get("WIDGET_JWT_ALG", "HS256")).upper()
    widget_public_key = (
        current_app.config.get("WIDGET_JWT_PUBLIC_KEY")
        or current_app.config.get("WIDGET_JWT_SECRET")
        or current_app.config.get("SECRET_KEY")
    )
    try:
        p = jwt.decode(
            tok,
            widget_public_key,
            algorithms=[widget_alg],
            options={"verify_exp": False, "verify_iat": False},
        )
    except Exception:
        return None
    if p.get("renew_until") and _now() > int(p["renew_until"]):
        return None
    p.pop("exp", None)
    ntok, _ = _sign(p, minutes, None)
    return ntok


_DEFAULT_CORS_HEADERS = (
    "Content-Type, Authorization, Origin, X-Anon-Id, Anon-Id, "
    "X-Widget-Token, X-Tenant, x-tenant, X-Tenant-Id, x-tenant-id"
)


def _add_cors(
    resp,
    *,
    allow_credentials: bool = False,
    allow_methods: list[str] | tuple[str, ...] | None = None,
    allow_headers: str | None = None,
):
    origin = request.headers.get("Origin")
    allowed = os.getenv("CORS_ALLOWED_ORIGINS", "*").strip()

    # Permitir orígenes explícitos; si se necesitan credenciales no podemos usar "*".
    if allowed == "*" and origin and allow_credentials:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    elif allowed == "*" and origin:
        resp.headers["Access-Control-Allow-Origin"] = "*"
    else:
        allowed_set = {x.strip() for x in allowed.split(",") if x.strip()}
        if origin in allowed_set:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Vary"] = "Origin"

    methods_header = ", ".join(allow_methods) if allow_methods else "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Methods"] = methods_header
    resp.headers["Access-Control-Allow-Headers"] = allow_headers or _DEFAULT_CORS_HEADERS
    resp.headers["Access-Control-Max-Age"] = "600"
    if allow_credentials:
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers.setdefault(
            "Access-Control-Expose-Headers",
            "X-Entity-Token, X-Widget-Token, X-Anon-Id, Anon-Id",
        )
    return resp


def _widget_features_for_tenant(tenant: TenantProfile) -> dict[str, object]:
    """Return feature flags for widget/marketplace consumption."""

    features: dict[str, object] = {}
    cfg = tenant.configuracion or {}

    widget_features = cfg.get("widget_features")
    if isinstance(widget_features, dict):
        features.update(widget_features)

    catalog_enabled = cfg.get("widget_catalog_enabled")
    if isinstance(catalog_enabled, bool):
        features.setdefault("catalog_enabled", catalog_enabled)
    else:
        features.setdefault("catalog_enabled", (tenant.tipo or "").lower() == "pyme")

    loyalty_enabled = cfg.get("widget_loyalty_enabled")
    if isinstance(loyalty_enabled, bool):
        features.setdefault("loyalty_enabled", loyalty_enabled)

    return features


WIDGET_BOOTSTRAP_CONTRACT_VERSION = "auth.widget_bootstrap.v1"
WIDGET_TOKEN_CONTRACT_VERSION = "auth.widget_token.v1"


def _widget_jwks_payload() -> dict[str, list[dict[str, str]]]:
    """Expose a minimal JWKS for widget token verification."""

    alg = str(current_app.config.get("WIDGET_JWT_ALG", "HS256")).upper()
    kid = current_app.config.get("WIDGET_JWT_KID", "widget-hs256")

    if alg in {"RS256", "ES256"}:
        public_key = current_app.config.get("WIDGET_JWT_PUBLIC_KEY")
        if not public_key:
            current_app.logger.warning("[auth] WIDGET_JWT_PUBLIC_KEY missing for %s widget JWT.", alg)
            return {"keys": []}

        try:
            algorithm_impl = jwt_algorithms.get_default_algorithms()[alg]
            prepared_key = algorithm_impl.prepare_key(public_key)
            jwk_payload = json.loads(algorithm_impl.to_jwk(prepared_key))
        except Exception:
            current_app.logger.exception("[auth] Failed to build JWKS payload for algorithm %s", alg)
            return {"keys": []}

        jwk_payload.update({"use": "sig", "alg": alg, "kid": kid})
        return {"keys": [jwk_payload]}

    secret = str(current_app.config.get("WIDGET_JWT_SECRET") or current_app.config.get("SECRET_KEY", ""))
    encoded_secret = base64.urlsafe_b64encode(secret.encode("utf-8")).rstrip(b"=").decode("utf-8")
    return {
        "keys": [
            {
                "kty": "oct",
                "use": "sig",
                "alg": "HS256",
                "kid": kid,
                "k": encoded_secret,
            }
        ]
    }


@auth_bp.route("/.well-known/jwks.json", methods=["GET"], strict_slashes=False)
@auth_bp.route("/widget/jwks.json", methods=["GET"], strict_slashes=False)
def widget_jwks():
    payload = _widget_jwks_payload()
    resp = jsonify(payload)
    resp.headers.setdefault("Cache-Control", "public, max-age=3600")
    return resp


@auth_bp.route("/widget/bootstrap", methods=["GET", "POST", "OPTIONS"], strict_slashes=False)
def widget_bootstrap():
    if request.method == "OPTIONS":
        return _add_cors(
            make_response("", 200),
            allow_credentials=True,
            allow_methods=("GET", "OPTIONS"),
        )

    tenant = getattr(g, "tenant_profile", None) or getattr(g, "current_tenant", None)
    if not tenant:
        resp = _add_cors(
            jsonify({"error": "tenant requerido"}),
            allow_credentials=True,
            allow_methods=("GET", "OPTIONS"),
        )
        return resp, 400

    try:
        from routes.pwa_public import _build_public_cart_url
        full_url, base_url, path = _build_public_cart_url(tenant)
    except ImportError:
        base_url = (request.url_root or "").rstrip("/")
        path = f"m/{tenant.slug}"
        full_url = f"{base_url}/{path}"
    market_payload = _tenant_market_payload(tenant)
    market_payload.setdefault("public_base_url", base_url)
    market_payload.setdefault("public_path", path)
    market_payload.setdefault("public_market_url", full_url)

    jwks_url = current_app.config.get("WIDGET_JWKS_URL")
    if not jwks_url:
        try:
            jwks_url = url_for("auth.widget_jwks", _external=True)
        except Exception:
            jwks_url = None

    response_payload = {
        "contract_version": WIDGET_BOOTSTRAP_CONTRACT_VERSION,
        "tenant": tenant.to_public_dict(),
        "marketplace": market_payload,
        "features": _widget_features_for_tenant(tenant),
        "jwks": {
            "url": jwks_url,
            "alg": str(current_app.config.get("WIDGET_JWT_ALG", "HS256")).upper(),
            "kid": current_app.config.get("WIDGET_JWT_KID", "widget-hs256"),
        },
        "widget": {
            "token_cookie_name": current_app.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token"),
            "access_minutes": _conf("WIDGET_ACCESS_MINUTES", 45),
            "renew_days": _conf("WIDGET_RENEW_DAYS", 7),
        },
    }

    return _add_cors(
        jsonify(response_payload),
        allow_credentials=True,
        allow_methods=("GET", "OPTIONS"),
    )


@auth_bp.route("/widget/token", methods=["POST", "OPTIONS"], strict_slashes=False)
@auth_bp.route("/widget-token", methods=["POST", "OPTIONS"], strict_slashes=False)
def widget_token():
    if request.method == "OPTIONS":
        return _add_cors(make_response("", 200))
    raw_authorization = (request.headers.get("Authorization") or "").strip()
    if raw_authorization.lower().startswith("bearer "):
        raw_authorization = raw_authorization[7:].strip()
    token = (
        (request.headers.get("X-Owner-Token") or "").strip()
        or (request.headers.get("X-Entity-Token") or "").strip()
        or raw_authorization
        or (request.args.get("owner_token") or "").strip()
        or obtener_token()
    )
    owner_user = None
    if token:
        jwt_user = user_from_token(token)
        if jwt_user:
            owner_user = (
                _user_query().get(jwt_user.empresa_id)
                if jwt_user.empresa_id
                else jwt_user
            )
        else:
            owner_user = _user_query().filter_by(token=token).first()
    if not owner_user:
        resp = _add_cors(jsonify({"error": "invalid_owner"}))
        return resp, 401

    tenant = _resolve_tenant_for_user(owner_user) or _tenant_for_owner(owner_user)
    if not plan_allows_full_integrations(tenant):
        resp = _add_cors(
            jsonify(
                _integration_plan_required_payload(
                    tenant,
                    contract_version=WIDGET_TOKEN_CONTRACT_VERSION,
                )
            )
        )
        return resp, 403

    # Reutilizar un token de widget ya emitido para este owner si sigue siendo válido.
    widget_cookie_name = current_app.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token")
    existing_widget_token = request.cookies.get(widget_cookie_name)
    if existing_widget_token:
        widget_alg = str(current_app.config.get("WIDGET_JWT_ALG", "HS256")).upper()
        widget_public_key = (
            current_app.config.get("WIDGET_JWT_PUBLIC_KEY")
            or current_app.config.get("WIDGET_JWT_SECRET")
            or current_app.config.get("SECRET_KEY")
        )
        try:
            payload = jwt.decode(
                existing_widget_token,
                widget_public_key,
                algorithms=[widget_alg],
                options={"verify_iat": False},
            )
        except Exception:
            payload = None

        if payload and payload.get("session_kind") == "widget":
            exp_ts = payload.get("exp")
            user_id = payload.get("user_id")
            if exp_ts and user_id == owner_user.id:
                remaining = int(exp_ts - _now())
                if remaining > 0:
                    return _add_cors(
                        jsonify(
                            {
                                "contract_version": WIDGET_TOKEN_CONTRACT_VERSION,
                                "token": existing_widget_token,
                                "expires_in": remaining,
                            }
                        )
                    )
    owner = {
        "user_id": owner_user.id,
        "rol": owner_user.rol,
        "tipo_chat": owner_user.tipo_chat,
        "municipio_id": owner_user.municipio_id,
        "pyme_id": owner_user.pyme_id,
    }
    minutes = _conf("WIDGET_ACCESS_MINUTES", 45)
    renew = _conf("WIDGET_RENEW_DAYS", 7)
    tok, _ = _sign(owner, minutes, renew)
    return _add_cors(
        jsonify(
            {
                "contract_version": WIDGET_TOKEN_CONTRACT_VERSION,
                "token": tok,
                "expires_in": minutes * 60,
            }
        )
    )


@auth_bp.route("/widget/refresh", methods=["POST", "OPTIONS"], strict_slashes=False)
@auth_bp.route("/widget-refresh", methods=["POST", "OPTIONS"], strict_slashes=False)
def widget_refresh():
    if request.method == "OPTIONS":
        return _add_cors(make_response("", 200))
    tok = (request.get_json(silent=True) or {}).get("token")
    if not tok:
        resp = _add_cors(jsonify({"error": "missing_token"}))
        return resp, 401

    widget_alg = str(current_app.config.get("WIDGET_JWT_ALG", "HS256")).upper()
    widget_public_key = (
        current_app.config.get("WIDGET_JWT_PUBLIC_KEY")
        or current_app.config.get("WIDGET_JWT_SECRET")
        or current_app.config.get("SECRET_KEY")
    )
    try:
        decoded_payload = jwt.decode(
            tok,
            widget_public_key,
            algorithms=[widget_alg],
            options={"verify_exp": False, "verify_iat": False},
        )
    except Exception:
        decoded_payload = None

    if not decoded_payload or decoded_payload.get("session_kind") != "widget":
        resp = _add_cors(jsonify({"error": "invalid_widget_token"}))
        return resp, 401

    owner_user = _user_query().get(decoded_payload.get("user_id"))
    tenant = _resolve_tenant_for_user(owner_user) or _tenant_for_owner(owner_user)
    if not plan_allows_full_integrations(tenant):
        resp = _add_cors(
            jsonify(
                _integration_plan_required_payload(
                    tenant,
                    contract_version=WIDGET_TOKEN_CONTRACT_VERSION,
                )
            )
        )
        return resp, 403

    minutes = _conf("WIDGET_ACCESS_MINUTES", 45)
    ntok = _refresh(tok, minutes)
    if not ntok:
        resp = _add_cors(jsonify({"error": "renew_window_expired"}))
        return resp, 401
    return _add_cors(
        jsonify(
            {
                "contract_version": WIDGET_TOKEN_CONTRACT_VERSION,
                "token": ntok,
                "expires_in": minutes * 60,
            }
        )
    )






def _default_demo_key_for_tipo(tipo_chat: str, demos: Optional[list[Any]] = None) -> Optional[str]:
    normalized = (tipo_chat or "").strip().lower()
    if normalized not in {"pyme", "municipio"}:
        return None

    source = demos if demos is not None else _load_demo_rubros_cached()
    for demo in source:
        if (demo.tipo_chat or "").strip().lower() == normalized and (demo.key or "").strip():
            return demo.key
    return None


def _resolve_demo_tenant_slug(rubro_raw: Optional[str]) -> Optional[str]:
    normalized = (rubro_raw or "").strip().lower()
    if not normalized:
        normalized = "municipio"

    demos = _load_demo_rubros_cached()
    allowed_keys = {
        (demo.key or "").strip().lower()
        for demo in demos
        if (demo.key or "").strip()
    }

    alias_map = {
        "municipio": _default_demo_key_for_tipo("municipio", demos=demos) or "municipio",
        "municipal": _default_demo_key_for_tipo("municipio", demos=demos) or "municipio",
        "gobierno": _default_demo_key_for_tipo("municipio", demos=demos) or "municipio",
        "government": _default_demo_key_for_tipo("municipio", demos=demos) or "municipio",
        "pyme": _default_demo_key_for_tipo("pyme", demos=demos) or "local_comercial_general",
        "empresa": _default_demo_key_for_tipo("pyme", demos=demos) or "local_comercial_general",
        "empresas": _default_demo_key_for_tipo("pyme", demos=demos) or "local_comercial_general",
        "comercio": _default_demo_key_for_tipo("pyme", demos=demos) or "local_comercial_general",
        "retail": "local_comercial_general",
        "mayorista": "bodega",
        "bodega": "bodega",
        "ferreteria": "ferreteria",
    }

    candidate = alias_map.get(normalized)
    if candidate and candidate in allowed_keys:
        return candidate

    for demo in demos:
        key = (demo.key or "").strip().lower()
        if not key:
            continue
        if normalized in {
            key,
            (demo.rubro_clave or "").lower() if demo.rubro_clave else "",
            (demo.label or "").strip().lower().replace(" ", "_"),
        }:
            return demo.key
        if normalized in {(a or "").lower() for a in (demo.aliases or [])}:
            return demo.key

    return None


def _normalize_rubro_lookup(value: Optional[str]) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[\s\-]+", "_", text)
    return text


def _resolve_rubro_from_input(rubro_raw: Any) -> Optional[Rubro]:
    if rubro_raw is None:
        return None

    raw_text = str(rubro_raw).strip()
    if not raw_text:
        return None

    if raw_text.isdigit():
        return Rubro.query.filter_by(id=int(raw_text)).first()

    normalized = _normalize_rubro_lookup(raw_text)
    rubros = Rubro.query.all()
    for rubro in rubros:
        normalized_clave = _normalize_rubro_lookup(getattr(rubro, "clave", None))
        normalized_nombre = _normalize_rubro_lookup(getattr(rubro, "nombre", None))
        if normalized in {normalized_clave, normalized_nombre}:
            return rubro

    return None


def _resolve_demo_rubro_for_tenant(tenant: TenantProfile) -> Optional[Rubro]:
    owner = _tenant_owner(tenant)
    if owner and getattr(owner, "rubro_id", None):
        rubro = Rubro.query.get(owner.rubro_id)
        if rubro:
            return rubro

    rubro_from_slug = _resolve_rubro_from_input(getattr(tenant, "slug", None))
    if rubro_from_slug:
        return rubro_from_slug

    if (tenant.tipo or "").strip().lower() == "municipio":
        return (
            Rubro.query.filter(func.lower(Rubro.clave) == "municipio").first()
            or Rubro.query.filter(Rubro.es_publico.is_(True)).order_by(Rubro.id.asc()).first()
        )

    return (
        Rubro.query.filter(func.lower(Rubro.clave) == "pyme").first()
        or Rubro.query.filter(func.lower(Rubro.clave) == "local_comercial_general").first()
        or Rubro.query.filter(Rubro.es_publico.is_(False)).order_by(Rubro.id.asc()).first()
    )


def _demo_superadmin_credentials() -> dict:
    return {
        "email": os.getenv("DEMO_SUPERADMIN_EMAIL", "superadmin.demo@chatboc.ar"),
        "password": os.getenv("DEMO_SUPERADMIN_PASSWORD", "demo1234"),
    }


def _ensure_demo_superadmin() -> User:
    creds = _demo_superadmin_credentials()
    email = (creds.get("email") or "").strip().lower()
    user = _user_query().filter(func.lower(User.email) == email).first() if email else None
    if user:
        updated = False
        if user.rol not in {"super_admin", "superadmin"}:
            user.rol = "super_admin"
            updated = True
        if user.tipo_chat != "admin":
            user.tipo_chat = "admin"
            updated = True
        if updated:
            db.session.add(user)
            db.session.commit()
        return user

    user = User(
        name="Super Admin Demo",
        email=creds["email"],
        rol="super_admin",
        tipo_chat="admin",
    )
    user.set_password(creds["password"])
    db.session.add(user)
    db.session.commit()
    return user


def _supported_demo_languages() -> list[dict[str, str]]:
    return [
        {"code": "es", "label": "Español", "locale": "es-AR"},
        {"code": "en", "label": "English", "locale": "en-US"},
        {"code": "pt", "label": "Português", "locale": "pt-BR"},
    ]


def _run_post_login_migrations(*, app, user_id: int, tenant_id: Optional[int], anon_id: str) -> None:
    """Move heavy anon adoption work out of the login critical path."""

    with app.app_context():
        try:
            from services.ticket_service import servicio_tickets

            servicio_tickets.migrar_tickets_de_anonimo(anon_id, user_id)
        except Exception as exc:  # pragma: no cover - defensive logging
            app.logger.warning("Failed to migrate anon tickets during deferred login flow: %s", exc)

        try:
            from routes.market import _get_or_create_cart_for_user

            user_obj = _user_query().get(user_id)
            if not user_obj:
                return
            target_tenant = None
            if tenant_id:
                target_tenant = TenantProfile.query.get(tenant_id)
            if not target_tenant:
                target_tenant = _tenant_for_user(user_obj)
            if target_tenant:
                _get_or_create_cart_for_user(target_tenant, user_obj, create_if_missing=False)
        except Exception as exc:  # pragma: no cover - defensive logging
            app.logger.warning("Failed to migrate anon cart during deferred login flow: %s", exc)

def _get_or_create_demo_user_for_tenant(tenant: TenantProfile) -> User:
    """Return an isolated demo admin account for the tenant.

    We intentionally avoid reusing tenant owner/admin accounts to prevent demo
    logins from inheriting real operator identities.
    """

    demo_email = f"demo.{tenant.slug}@chatboc.ar"
    demo_rubro = _resolve_demo_rubro_for_tenant(tenant)
    user = _user_query().filter_by(email=demo_email).first()
    if user:
        _attach_user_to_tenant(user, tenant)
        updated = False
        if demo_rubro and getattr(user, "rubro_id", None) != demo_rubro.id:
            user.rubro_id = demo_rubro.id
            updated = True
        if user.rol != "admin":
            user.rol = "admin"
            updated = True
        if updated:
            db.session.add(user)
            db.session.commit()
        return user

    user = User(
        name=f"Demo {tenant.nombre}",
        email=demo_email,
        rol="admin",
        tipo_chat=(tenant.tipo or "pyme").lower(),
        tenant_slug=tenant.slug,
        tenant_id=getattr(tenant, "id", None),
        pyme_id=tenant.pyme_id,
        municipio_id=tenant.municipio_id,
        rubro_id=getattr(demo_rubro, "id", None),
    )
    user.set_password("demo")
    db.session.add(user)
    db.session.commit()
    return user


def _resolve_default_demo_slug() -> Optional[str]:
    """Return a stable demo tenant slug for one-click demo logins."""

    default_slug = current_app.config.get("DEFAULT_DEMO_TENANT_SLUG")
    if isinstance(default_slug, str) and default_slug.strip():
        normalized = _resolve_demo_tenant_slug(default_slug.strip())
        if normalized:
            return normalized

    try:
        demos = _load_demo_rubros_cached()
    except Exception:
        demos = []

    for demo in demos:
        slug = _resolve_demo_tenant_slug(demo.key) or _resolve_demo_tenant_slug(demo.rubro_clave)
        if slug:
            return slug

    first_tenant = (
        TenantProfile.query.filter(TenantProfile.is_active.is_(True))
        .order_by(TenantProfile.created_at.asc(), TenantProfile.id.asc())
        .first()
    )
    return getattr(first_tenant, "slug", None)




def _first_active_tenant_for_demo(tipo: Optional[str]) -> Optional[TenantProfile]:
    """Return the first active tenant for demo fallback by tipo."""

    normalized = (tipo or "").strip().lower()
    query = TenantProfile.query.filter(TenantProfile.is_active.is_(True))
    if normalized in {"municipio", "gobierno", "municipal", ""}:
        tenant = query.filter_by(tipo="municipio").order_by(TenantProfile.created_at.asc(), TenantProfile.id.asc()).first()
        if tenant:
            return tenant
        return query.order_by(TenantProfile.created_at.asc(), TenantProfile.id.asc()).first()
    elif normalized in {"pyme", "empresa", "empresas", "comercio"}:
        tenant = query.filter_by(tipo="pyme").order_by(TenantProfile.created_at.asc(), TenantProfile.id.asc()).first()
        if tenant:
            return tenant
        return query.order_by(TenantProfile.created_at.asc(), TenantProfile.id.asc()).first()
    else:
        return None


def _demo_menu_for_tipo(tipo_chat: str) -> list[dict[str, Any]]:
    normalized = (tipo_chat or "").strip().lower()
    if normalized == "municipio":
        return [
            {"id": "reclamos", "label": "Crear reclamo", "intent": "iniciar_reclamo"},
            {"id": "sugerencias", "label": "Enviar sugerencia", "intent": "enviar_sugerencia"},
            {"id": "estado_ticket", "label": "Estado del ticket", "intent": "consultar_ticket"},
            {"id": "encuestas", "label": "Responder encuesta", "intent": "encuestas_publicas"},
            {"id": "mapa_calor", "label": "Ver mapa de calor", "intent": "analytics_heatmap"},
        ]
    return [
        {"id": "catalogo", "label": "Ver catálogo", "intent": "ver_catalogo"},
        {"id": "crear_pedido", "label": "Crear pedido", "intent": "crear_pedido"},
        {"id": "estado_pedido", "label": "Estado del pedido", "intent": "estado_pedido"},
        {"id": "subir_catalogo", "label": "Subir PDF / Excel", "intent": "subir_catalogo"},
        {"id": "consultar_producto", "label": "Consultar producto", "intent": "consulta_producto"},
    ]


def _build_twilio_trial_instructions() -> dict[str, Any]:
    join_phrase = "join brief-yesterday"
    sandbox_number = "+14155238886"
    message_encoded = quote_plus(join_phrase)
    return {
        "provider": "twilio_sandbox",
        "enabled": True,
        "number_e164": sandbox_number,
        "display_number": "+1 (415) 523-8886",
        "join_phrase": join_phrase,
        "wa_deeplink": f"https://wa.me/{sandbox_number[1:]}?text={message_encoded}",
        "cta_label": "Activar demo en WhatsApp",
        "steps": [
            {"order": 1, "text": "Abrí WhatsApp y enviá el mensaje de activación al número de prueba."},
            {"order": 2, "text": "Mensaje exacto: join brief-yesterday"},
            {"order": 3, "text": "Volvé al panel y probá texto, audio, imagen, reclamos y pedidos en tiempo real."},
        ],
        "security_limits": {
            "messages_per_session": 10,
            "trial_mode": True,
            "upgrade_required_for": [
                "indexacion_catalogo_qdrant",
                "automatizaciones_avanzadas",
                "multiagente_en_produccion",
            ],
            "upgrade_copy": "Llegaste al límite de demo. Activá plan Full para continuar.",
        },
    }


def _demo_mode_disabled_response(request_id: str | None = None):
    payload = {
        "contract_version": AUTH_DEMO_CONTRACT_VERSION,
        "error": {
            "code": 404,
            "message": "Demo mode disabled",
        },
    }
    if request_id:
        payload["request_id"] = request_id
    response = make_response(jsonify(payload), 404)
    if request_id:
        response.headers.setdefault("X-Request-Id", request_id)
    return response


def _normalized_request_id() -> str:
    raw = request.headers.get("X-Request-Id")
    if isinstance(raw, str):
        cleaned = raw.strip()
        if cleaned:
            return cleaned
    return uuid.uuid4().hex

@auth_bp.route('/demo/catalog', methods=['GET', 'OPTIONS'])
@cross_origin(supports_credentials=True)
def demo_catalog():
    if request.method == 'OPTIONS':
        return '', 204
    request_id = _normalized_request_id()
    if not bool(current_app.config.get("ENABLE_DEMO_MODE", False)):
        return _demo_mode_disabled_response(request_id)
    ensure_users = str(request.args.get('ensure_users') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    now = time.time()
    if not ensure_users:
        cache_key = _demo_catalog_fingerprint()
        cached_payload = _DEMO_CATALOG_CACHE.get("payload")
        cached_expires = float(_DEMO_CATALOG_CACHE.get("expires_at") or 0.0)
        cached_fingerprint = str(_DEMO_CATALOG_CACHE.get("fingerprint") or "")
        if cached_payload and cached_expires > now and cached_fingerprint == cache_key:
            cached_response = jsonify(cached_payload)
            cached_response.headers.setdefault("X-Request-Id", request_id)
            return cached_response

    if ensure_users:
        _ensure_demo_superadmin()

    demo_items = []
    try:
        demos = _load_demo_rubros_cached()
    except Exception as exc:
        current_app.logger.warning("[demo_catalog] fallback to static demo catalog: %s", exc)
        demos = []

    for demo in demos:
        tenant_slug = _resolve_demo_tenant_slug(demo.key) or _resolve_demo_tenant_slug(demo.rubro_clave)
        sector = "gobierno" if (demo.tipo_chat or "").strip().lower() == "municipio" else "empresas"
        tipo_chat = (demo.tipo_chat or "pyme").strip().lower()
        demo_items.append({
            "key": demo.key,
            "label": demo.label,
            "tipo_chat": tipo_chat,
            "sector": sector,
            "rubro_clave": demo.rubro_clave,
            "tenant_slug": tenant_slug,
            "login_payload": {"rubro": demo.key},
            "menu_preview": _demo_menu_for_tipo(tipo_chat),
        })

    if not demo_items:
        for tipo, label in (("municipio", "Demo Municipio"), ("pyme", "Demo PyME")):
            tenant = _first_active_tenant_for_demo(tipo)
            if not tenant:
                continue
            demo_items.append({
                "key": tipo,
                "label": label,
                "tipo_chat": tipo,
                "tenant_slug": tenant.slug,
                "login_payload": {"rubro": tipo},
                "menu_preview": _demo_menu_for_tipo(tipo),
            })

    quick_login_slug = None
    try:
        quick_login_slug = _resolve_default_demo_slug()
    except Exception as exc:
        current_app.logger.warning("[demo_catalog] unable to resolve default demo slug: %s", exc)

    payload = {
        "request_id": request_id,
        "frontend_contract_version": "2026-02-demo-onboarding-v2",
        "demo_login_enabled": True,
        "demo_login_endpoint": "/auth/demo",
        "demo_login_methods": ["POST"],
        "entry_points": [
            {"key": "municipio", "label": "Demo Municipio", "enabled": True, "login_payload": {"rubro": "municipio", "tipo_chat": "municipio"}},
            {"key": "pyme", "label": "Demo PyME", "enabled": True, "login_payload": {"rubro": "pyme", "tipo_chat": "pyme"}},
        ],
        "quick_login_payload": {"tenant_slug": quick_login_slug},
        "super_admin_demo": {
            **_demo_superadmin_credentials(),
            "role": "super_admin",
            "login_endpoint": "/auth/login",
        },
        "tenant_demos": [{**item, "enabled": True, "login_endpoint": "/auth/demo"} for item in demo_items],
        "supported_languages": _supported_demo_languages(),
        "onboarding": {
            "requires_rubro_selection_for": ["empresas"],
            "default_sector": "gobierno",
            "twilio_trial": _build_twilio_trial_instructions(),
            "demo_feature_access": {
                "allow_audio": True,
                "allow_images": True,
                "allow_reclamos": True,
                "allow_pedidos": True,
                "allow_sugerencias": True,
                "allow_heatmaps": True,
                "allow_catalog_upload_pdf": True,
                "allow_catalog_upload_excel": True,
                "qdrant_demo_enabled": True,
                "qdrant_demo_max_items": 50,
            },
            "menus_by_tipo": {
                "municipio": _demo_menu_for_tipo("municipio"),
                "pyme": _demo_menu_for_tipo("pyme"),
            },
            "experience_templates": {
                "municipio": build_demo_experience_contract(tenant_type="municipio", rubro_label="Municipio", max_messages=10),
                "pyme": build_demo_experience_contract(tenant_type="pyme", rubro_label="PyME", max_messages=10),
            },
            "steps": [
                {"key": "sector", "label": "Elegí tu sector", "required": True},
                {"key": "rubro", "label": "Elegí un rubro demo", "required": False, "required_for": ["empresas"]},
                {"key": "whatsapp_activation", "label": "Activar WhatsApp demo", "required": True},
            ],
            "sector_options": [
                {
                    "key": "gobierno",
                    "label": "Municipio / Gobierno",
                    "default_login_payload": {"rubro": "municipio", "tipo_chat": "municipio", "sector": "gobierno"},
                    "rubros": [
                        {
                            "key": "municipio",
                            "label": "Municipio",
                            "tipo_chat": "municipio",
                            "login_payload": {"rubro": "municipio", "tipo_chat": "municipio", "sector": "gobierno"},
                        }
                    ],
                },
                {
                    "key": "empresas",
                    "label": "Empresas",
                    "rubros": [
                        {
                            "key": item.get("key"),
                            "label": item.get("label") or item.get("key"),
                            "tipo_chat": "pyme",
                            "tenant_slug": item.get("tenant_slug"),
                            "menu_preview": item.get("menu_preview") or _demo_menu_for_tipo("pyme"),
                            "login_payload": {
                                "rubro": item.get("key"),
                                "tipo_chat": "pyme",
                                "tenant_slug": item.get("tenant_slug"),
                                "sector": "empresas",
                            },
                        }
                        for item in demo_items
                        if (item.get("tipo_chat") or "").strip().lower() == "pyme"
                    ],
                },
            ],
        },
        "frontend": {
            "demo_selector": {
                "mode": "sector_first",
                "default_sector": "gobierno",
                "require_rubro_for_sector": {"gobierno": False, "empresas": True},
                "tenant_slug_field": "login_payload.tenant_slug",
            },
            "preload_before_login": [
                {"name": "demo_catalog", "method": "GET", "endpoint": "/auth/demo/catalog"},
                {"name": "tenant_info", "method": "GET", "endpoint": "/api/pwa/tenant-info", "query": ["tenant", "tenant_slug"]},
                {"name": "anon_id", "method": "GET", "endpoint": "/api/pwa/anon-id", "query": ["tenant"]},
            ],
        },
    }

    if not ensure_users:
        _DEMO_CATALOG_CACHE["fingerprint"] = _demo_catalog_fingerprint()
        _DEMO_CATALOG_CACHE["payload"] = payload
        _DEMO_CATALOG_CACHE["expires_at"] = time.time() + 60.0

    response = jsonify(payload)
    response.headers.setdefault("X-Request-Id", request_id)
    return response


@auth_bp.route('/demo', methods=['POST', 'OPTIONS'])
@cross_origin(supports_credentials=True)
@limiter.limit("10 per minute")
def login_demo():
    if request.method == 'OPTIONS':
        return '', 204
    request_id = _normalized_request_id()
    if not bool(current_app.config.get("ENABLE_DEMO_MODE", False)):
        return _demo_mode_disabled_response(request_id)

    data = request.get_json(silent=True) or {}
    requested_sector = str(data.get("sector") or "").strip().lower()
    preferred_slug = data.get('tenant_slug') or data.get('tenantSlug') or request.args.get('tenant_slug') or request.args.get('tenant')
    rubro = data.get('rubro') or data.get('segmento') or data.get('demo') or data.get('tipo_chat')
    if not rubro and requested_sector in {"gobierno", "municipio", "publico", "public"}:
        rubro = "municipio"
    if not rubro and requested_sector in {"empresa", "empresas", "pyme", "privado", "private"}:
        try:
            pyme_demo = next((demo for demo in _load_demo_rubros_cached() if (demo.tipo_chat or "").strip().lower() == "pyme"), None)
        except Exception:
            pyme_demo = None
        rubro = (pyme_demo.key if pyme_demo else "pyme")
    candidate = preferred_slug or rubro
    if not candidate:
        candidate = _resolve_default_demo_slug()
    demo_slug = _resolve_demo_tenant_slug(candidate)

    tenant_obj = None
    if demo_slug:
        try:
            tenant_obj = resolve_tenant_only(tenant_slug=demo_slug, require_explicit_slug=True)
        except Exception:
            tenant_obj = None

    if tenant_obj and requested_sector in {"gobierno", "municipio", "publico", "public"}:
        if (getattr(tenant_obj, "tipo", "") or "").strip().lower() != "municipio":
            tenant_obj = _first_active_tenant_for_demo("municipio")
    if tenant_obj and requested_sector in {"empresa", "empresas", "pyme", "privado", "private"}:
        if (getattr(tenant_obj, "tipo", "") or "").strip().lower() != "pyme":
            tenant_obj = _first_active_tenant_for_demo("pyme")

    if not tenant_obj:
        requested_tipo = str(data.get("tipo_chat") or rubro or "municipio").strip().lower()
        tenant_obj = _first_active_tenant_for_demo(requested_tipo)

    if not tenant_obj:
        return jsonify({"error": f"Rubro demo '{candidate or demo_slug or ''}' no válido"}), 404

    demo_user = _get_or_create_demo_user_for_tenant(tenant_obj)
    _attach_user_to_tenant(demo_user, tenant_obj)
    db.session.commit()

    tipo_chat = _resolve_tipo_chat(demo_user, tenant_obj=tenant_obj)
    if requested_sector in {"gobierno", "municipio", "publico", "public"}:
        tipo_chat = "municipio"
    elif requested_sector in {"empresa", "empresas", "pyme", "privado", "private"}:
        tipo_chat = "pyme"
    jwt_payload = {
        'user_id': demo_user.id,
        'rol': demo_user.rol,
        'tipo_chat': tipo_chat,
        'empresa_id': demo_user.empresa_id,
        'municipio_id': demo_user.municipio_id,
        'tenant_slug': tenant_obj.slug,
        'demo_mode': True,
        'exp': datetime.utcnow() + timedelta(hours=12),
    }
    jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm='HS256')

    response_payload = {
        "mensaje": "Demo login exitoso",
        "id": demo_user.id,
        "token": jwt_token,
        "email": demo_user.email,
        "name": demo_user.name,
        "rol": demo_user.rol,
        "empresa_id": demo_user.empresa_id,
        "municipio_id": demo_user.municipio_id,
        "tipo_chat": tipo_chat,
        "tenant_id": tenant_obj.id,
        "tenant_slug": tenant_obj.slug,
        "tenantSlug": tenant_obj.slug,
        "marketplace": _tenant_market_payload(tenant_obj),
        "demo_mode": True,
        "rubro": (
            getattr(getattr(demo_user, "rubro", None), "clave", None)
            or rubro
            or candidate
            or tenant_obj.slug
        ),
        "rubro_id": getattr(demo_user, "rubro_id", None),
        "sector": requested_sector or ("gobierno" if tipo_chat == "municipio" else "empresas"),
    }
    experience = build_demo_experience_contract(
        tenant_type=tipo_chat,
        rubro_label=getattr(tenant_obj, "nombre", None),
        max_messages=int(current_app.config.get("DEMO_MAX_MESSAGES_PER_SESSION", 10) or 10),
    )
    guided_onboarding = experience.get("guided_onboarding") or {}
    response_payload["demo_onboarding"] = {
        "autostart_chat": bool(guided_onboarding.get("autostart_chat", True)),
        "open_widget": bool(guided_onboarding.get("open_widget", True)),
        "entry_prompt": guided_onboarding.get("entry_prompt") or "¿Sobre qué te gustaría preguntar primero?",
        "starter_prompts": guided_onboarding.get("starter_prompts") or [],
        "suggested_workflows": guided_onboarding.get("suggested_workflows") or [],
        "quick_actions": experience.get("quick_actions") or [],
        "experience_version": experience.get("version"),
        "analytics_kpis": experience.get("analytics_kpis") or [],
        "integrations": experience.get("integrations") or {},
    }
    response_payload["widget"] = {
        "autostart": True,
        "tenant_slug": tenant_obj.slug,
        "hint": "Podés abrir el widget y usar los prompts sugeridos para empezar.",
    }
    response = jsonify(response_payload)

    cookie_name = current_app.config.get("AUTH_TOKEN_COOKIE_NAME", "auth_token")
    cookie_args = {
        "key": cookie_name,
        "value": jwt_token,
        "secure": current_app.config.get("SESSION_COOKIE_SECURE", True),
        "httponly": True,
        "samesite": current_app.config.get("SESSION_COOKIE_SAMESITE", "None"),
    }
    cookie_domain = current_app.config.get("SESSION_COOKIE_DOMAIN")
    if cookie_domain:
        cookie_args["domain"] = cookie_domain
    response.set_cookie(**cookie_args)
    return response

def solo_admin_requerido(f):
    """Permite solo a usuarios administradores (empresa_id None)."""
    @wraps(f)
    def decorated(user: User, *args, **kwargs):
        if user.empresa_id is not None:
            return jsonify({"error": "Permisos insuficientes"}), 403
        return f(user, *args, **kwargs)

    return decorated

@auth_bp.route('/login', methods=['GET', 'POST', 'OPTIONS'])
@cross_origin(supports_credentials=True)
def login():
    anon_id = get_or_create_anon_id()
    request_started = time.perf_counter()
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex

    def _finalize_auth_response(resp):
        elapsed_ms = round((time.perf_counter() - request_started) * 1000.0, 2)
        resp.headers.setdefault("X-Request-Id", request_id)
        resp.headers.setdefault("Server-Timing", f"auth_total;dur={elapsed_ms}")
        resp.headers.setdefault('X-Anon-Id', anon_id)
        resp.headers.setdefault('Anon-Id', anon_id)
        return resp, elapsed_ms
    if request.method == 'OPTIONS':
        resp = jsonify({'status': 'ok'})
        resp, _ = _finalize_auth_response(resp)
        return resp
    if request.method == 'GET':
        resp = jsonify({'status': 'ok'})
        resp, _ = _finalize_auth_response(resp)
        return resp
    if not request.is_json:
        resp = jsonify({"error": "La solicitud debe ser de tipo JSON."})
        resp, _ = _finalize_auth_response(resp)
        return resp, 400
    data = request.get_json()
    if not data or not data.get('email') or not data.get('password'):
        resp = jsonify({"error": "Email y contraseña requeridos."})
        resp, _ = _finalize_auth_response(resp)
        return resp, 400

    stage_timings = {
        "db_lookup_ms": 0.0,
        "password_verify_ms": 0.0,
        "tenant_resolve_ms": 0.0,
        "token_sign_ms": 0.0,
    }

    db_lookup_started = time.perf_counter()
    try:
        user = _user_query().filter_by(email=data.get("email").strip().lower()).first()
    except ProgrammingError:
        db.session.rollback()
        current_app.logger.error("[auth] DB error during login", exc_info=True)
        resp = jsonify({"error": "internal_error"})
        resp, _ = _finalize_auth_response(resp)
        return resp, 500

    stage_timings["db_lookup_ms"] = round((time.perf_counter() - db_lookup_started) * 1000.0, 2)

    password_verify_started = time.perf_counter()
    password_ok = bool(user and user.check_password(data.get("password")))
    stage_timings["password_verify_ms"] = round(
        (time.perf_counter() - password_verify_started) * 1000.0, 2
    )

    if not password_ok:
        current_app.logger.warning(f"Intento de login fallido para el email: {data.get('email')}")
        resp = jsonify({"error": "Email o contraseña incorrectos."})
        resp, _ = _finalize_auth_response(resp)
        return resp, 401

    current_app.logger.info(f"Login exitoso para: {user.email}")

    rubro_nombre = user.rubro.nombre if user.rubro else "General"

    # Integrar Flask-Login
    from flask_login import login_user
    login_user(user) # Establecer la sesión para el usuario
    current_app.logger.info(f"Usuario {user.email} logueado y sesión Flask-Login establecida.")

    # Ensure user is linked to their tenant if missing, to prevent permission errors
    tenant_resolve_started = time.perf_counter()
    tenant_obj = _tenant_for_user(user)
    if not tenant_obj:
        if user.municipio_id:
            tenant_obj = TenantProfile.query.filter_by(municipio_id=user.municipio_id).first()
        elif user.pyme_id:
            tenant_obj = TenantProfile.query.filter_by(pyme_id=user.pyme_id).first()
        else:
            # Check for tenant_slug in request to link user (e.g. demo flow)
            req_tenant_slug = data.get("tenant_slug") or data.get("tenantSlug") or request.args.get("tenant_slug")
            if req_tenant_slug:
                try:
                    tenant_obj = resolve_tenant_only(tenant_slug=req_tenant_slug)
                except Exception:
                    pass

    tenant_obj = _resolve_tenant_for_user(user, tenant_obj)
    if tenant_obj:
        tenant_changed = _attach_user_to_tenant(user, tenant_obj)
        if tenant_changed:
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                current_app.logger.warning("Failed to attach user to tenant during login")
    tipo_chat = _resolve_tipo_chat(user, tenant_obj=tenant_obj, rubro_nombre=rubro_nombre)
    stage_timings["tenant_resolve_ms"] = round(
        (time.perf_counter() - tenant_resolve_started) * 1000.0, 2
    )

    # Migrate anonymous data if anon_id is present.
    # This can be expensive (ticket + cart adoption), so default to async to
    # keep login response times fast.
    req_anon_id = request.headers.get("X-Anon-Id") or request.headers.get("Anon-Id") or data.get("anon_id")
    if req_anon_id:
        deferred_migration = str(
            current_app.config.get("DEFER_ANON_MIGRATION_ON_LOGIN", True)
        ).strip().lower() not in {"0", "false", "no", "off"}
        try:
            if deferred_migration:
                app_obj = current_app._get_current_object()
                tenant_id = getattr(tenant_obj, "id", None)
                thread = threading.Thread(
                    target=_run_post_login_migrations,
                    kwargs={
                        "app": app_obj,
                        "user_id": user.id,
                        "tenant_id": tenant_id,
                        "anon_id": req_anon_id,
                    },
                    daemon=True,
                    name=f"login-migrate-{user.id}",
                )
                thread.start()
            else:
                _run_post_login_migrations(
                    app=current_app._get_current_object(),
                    user_id=user.id,
                    tenant_id=getattr(tenant_obj, "id", None),
                    anon_id=req_anon_id,
                )
        except Exception as e:
            current_app.logger.warning(f"Failed to migrate anon data during login: {e}")

    owner_token = _resolve_owner_token(user)

    effective_municipio_id = getattr(tenant_obj, "municipio_id", None) or user.municipio_id

    # Generar el token JWT
    jwt_payload = {
        'user_id': user.id,
        'rol': user.rol,
        'tipo_chat': tipo_chat,
        'empresa_id': user.empresa_id,
        'municipio_id': effective_municipio_id,
        'exp': datetime.utcnow() + timedelta(days=current_app.config.get("JWT_EXPIRATION_DAYS", 7))
    }
    token_sign_started = time.perf_counter()
    jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")
    stage_timings["token_sign_ms"] = round((time.perf_counter() - token_sign_started) * 1000.0, 2)

    # Reuse already resolved tenant to avoid extra DB round-trips on login.
    response_slug = getattr(user, "tenant_slug", None)
    if tenant_obj and not response_slug:
        response_slug = tenant_obj.slug

    current_app.logger.debug(
        "[AUTH_DEBUG] Login user=%s tenant_slug=%s", user.id, response_slug
    )

    response_payload = {
        "mensaje": "Login exitoso",
        "id": user.id,
        "token": jwt_token,
        "email": user.email,
        "name": user.name,
        "rol": user.rol,
        "empresa_id": user.empresa_id,
        "rubro": rubro_nombre,
        "tipo_chat": tipo_chat,
        "municipio_id": effective_municipio_id,
        "categorias": getattr(user, "categorias_lista", []),
        "tenant_slug": response_slug,
        "tenantSlug": response_slug,
        "bootstrap": {
            "mode": "lite",
            "endpoint": "/auth/session/bootstrap",
            "recommended_async_endpoints": [
                "/auth/me",
                "/auth/me/dashboard",
            ],
            "request_id": request_id,
        },
        "ui": {
            "panels": _dashboard_panels_for_user(user, tipo_chat),
        },
        "timing": {
            **stage_timings,
            "mode": "shell_first",
        },
    }

    response_payload["timing"]["total_ms"] = round((time.perf_counter() - request_started) * 1000.0, 2)
    integration_access = integration_access_payload(tenant_obj)
    response_payload["integration_access"] = integration_access
    response_payload["integrations_locked"] = not bool(integration_access.get("enabled"))
    entity_token_value = _include_entity_token_fields(response_payload, owner_token)
    if entity_token_value:
        if integration_access.get("enabled"):
            response_payload.setdefault("widget_embed_token", entity_token_value)
            response_payload.setdefault("widget_embed_token_kind", "entity")
        else:
            response_payload["widget_embed_token"] = None
            response_payload["widget_embed_token_kind"] = "plan_required"

    response = jsonify(response_payload)

    cookie_name = current_app.config.get("AUTH_TOKEN_COOKIE_NAME", "auth_token")

    if jwt_token:
        cookie_args = {
            "key": cookie_name,
            "value": jwt_token,
            "secure": current_app.config.get("SESSION_COOKIE_SECURE", True),
            "httponly": True,
            "samesite": current_app.config.get("SESSION_COOKIE_SAMESITE", "None"),
        }

        cookie_domain = current_app.config.get("SESSION_COOKIE_DOMAIN")
        if cookie_domain:
            cookie_args["domain"] = cookie_domain

        response.set_cookie(**cookie_args)

    response, elapsed_ms = _finalize_auth_response(response)
    current_app.logger.info(
        "[auth.login] request_id=%s user_id=%s role=%s tenant_slug=%s db_lookup_ms=%s total_ms=%s",
        request_id,
        user.id,
        user.rol,
        response_slug,
        stage_timings["db_lookup_ms"],
        elapsed_ms,
    )
    if entity_token_value:
        response.headers.setdefault("X-Entity-Token", entity_token_value)
    return response


@auth_bp.route("/integracion/regenerar-token", methods=["POST"])
@token_requerido
def regenerar_token_integracion(user):
    """Regenerates the persistent integration token for the current owner."""

    owner_user = getattr(g, "owner_user", None) or user
    empresa_id = getattr(owner_user, "empresa_id", None)
    if empresa_id:
        parent = _user_query().get(empresa_id)
        if parent:
            owner_user = parent

    tenant = _resolve_tenant_for_user(owner_user) or _tenant_for_owner(owner_user)
    if not plan_allows_full_integrations(tenant):
        return (
            jsonify(
                _integration_plan_required_payload(
                    tenant,
                    contract_version="auth.integration_token.v1",
                )
            ),
            403,
        )

    try:
        owner_user.entity_token = None
        db.session.add(owner_user)
        db.session.commit()
    except Exception:
        current_app.logger.exception(
            "[integracion] No se pudo limpiar el entity_token para regeneración"
        )
        db.session.rollback()
        return jsonify({"error": "No se pudo regenerar el token de integración."}), 500

    owner_token = _resolve_owner_token(owner_user)
    if not owner_token:
        return jsonify({"error": "No se pudo generar un token estable."}), 500

    response_payload = {"entity_token": owner_token, "entityToken": owner_token}
    resp = jsonify(response_payload)
    resp.headers.setdefault("X-Entity-Token", owner_token)
    return resp

@auth_bp.route('/google-client-id', methods=['GET'])
def get_google_client_id():
    """Devuelve el primer Google Client ID configurado."""
    ids = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
    first = ids.split(',')[0].strip() if ids else ""
    return jsonify({"client_id": first})


@auth_bp.route('/google-maps-key', methods=['GET'])
def get_google_maps_key():
    """Expone la configuración de mapas para clientes autenticados."""
    return jsonify(get_map_config())

@auth_bp.route('/google-login', methods=['POST'])
def google_login():
    """Inicia sesión utilizando un token de Google."""
    data = request.get_json(silent=True) or {}
    token_id = (
        data.get('id_token')
        or request.form.get('id_token')
    )
    tipo_chat = data.get('tipo_chat') or request.form.get('tipo_chat')
    rol = data.get('rol') or request.form.get('rol')
    if not token_id:
        return jsonify({"error": "id_token requerido"}), 400
    try:
        user = login_o_crear_usuario(token_id, rol=rol, tipo_chat=tipo_chat)
        current_app.logger.info(f"Login Google para: {user.email}")

        owner_token = _resolve_owner_token(user)
        profile_identity = get_user_profile_identity(user)

        if not getattr(user, "rubro_id", None):
            # Aún si falta el rubro, generamos un token para que pueda continuar
            jwt_payload = {
                'user_id': user.id,
                'exp': datetime.utcnow() + timedelta(days=current_app.config.get("JWT_EXPIRATION_DAYS", 7))
            }
            jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")
            response_payload = {
                "status": "falta_rubro",
                "token": jwt_token,
                "email": user.email,
                "avatar_url": profile_identity.get("avatar_url"),
                "picture": profile_identity.get("avatar_url"),
                "avatar_source": profile_identity.get("avatar_source"),
                "avatar_consent": bool(profile_identity.get("avatar_consent")),
                "profile_picture_consent": bool(profile_identity.get("avatar_consent")),
            }
            entity_token_value = _include_entity_token_fields(response_payload, owner_token)
            resp = jsonify(response_payload)
            if entity_token_value:
                resp.headers.setdefault("X-Entity-Token", entity_token_value)
            return resp

        rubro_nombre = user.rubro.nombre if user.rubro else "General"
        tenant_obj = _resolve_tenant_for_user(user)
        tipo_chat = _resolve_tipo_chat(user, tenant_obj=tenant_obj, rubro_nombre=rubro_nombre)

        # Integrar Flask-Login
        from flask_login import login_user
        login_user(user) # Establecer la sesión para el usuario
        current_app.logger.info(f"Usuario {user.email} logueado vía Google y sesión Flask-Login establecida.")
        # Generar el token JWT
        jwt_payload = {
            'user_id': user.id,
            'rol': user.rol,
            'tipo_chat': tipo_chat,
            'empresa_id': user.empresa_id,
            'municipio_id': user.municipio_id,
            'exp': datetime.utcnow() + timedelta(days=current_app.config.get("JWT_EXPIRATION_DAYS", 7))
        }
        jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")

        # Determine tenant_slug for response
        tenant_slug_out = getattr(user, "tenant_slug", None)
        if not tenant_slug_out:
            if tenant_obj:
                tenant_slug_out = tenant_obj.slug

        response_payload = {
            "id": user.id,
            "token": jwt_token,
            "name": user.name,
            "email": user.email,
            "rol": user.rol,
            "empresa_id": user.empresa_id,
            "rubro": rubro_nombre,
            "tipo_chat": tipo_chat,
            "categorias": getattr(user, "categorias_lista", []),
            "tenant_slug": tenant_slug_out,
            "tenantSlug": tenant_slug_out,
            "avatar_url": profile_identity.get("avatar_url"),
            "picture": profile_identity.get("avatar_url"),
            "avatar_source": profile_identity.get("avatar_source"),
            "avatar_consent": bool(profile_identity.get("avatar_consent")),
            "profile_picture_consent": bool(profile_identity.get("avatar_consent")),
        }

        entity_token_value = _include_entity_token_fields(response_payload, owner_token)

        response = jsonify(response_payload)

        cookie_name = current_app.config.get("AUTH_TOKEN_COOKIE_NAME", "auth_token")

        if jwt_token:
            cookie_args = {
                "key": cookie_name,
                "value": jwt_token,
                "secure": current_app.config.get("SESSION_COOKIE_SECURE", True),
                "httponly": True,
                "samesite": current_app.config.get("SESSION_COOKIE_SAMESITE", "None"),
            }

            cookie_domain = current_app.config.get("SESSION_COOKIE_DOMAIN")
            if cookie_domain:
                cookie_args["domain"] = cookie_domain

            response.set_cookie(**cookie_args)

        if entity_token_value:
            response.headers.setdefault("X-Entity-Token", entity_token_value)

        return response
    except ValueError as e:
        current_app.logger.error(f"Error de valor en google_login: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 401
    except Exception as e:  # pragma: no cover - unexpected errors
        current_app.logger.error(f"Error en google_login: {e}", exc_info=True)
        return jsonify({"error": "Error interno"}), 500

@auth_bp.route('/register', methods=['POST'])
def register():
    data = request.get_json(silent=True) or {}

    if not data:
        return jsonify({
            "error": "Solicitud JSON inválida.",
            "botones": [{"texto": "Volver al chat"}],
        }), 400

    # Check if this is an end-user registration for a specific tenant
    tenant_slug = data.get("tenant_slug") or data.get("tenantSlug") or request.args.get("tenant_slug") or request.args.get("tenantSlug")
    if tenant_slug:
        current_app.logger.debug(f"Processing tenant_slug={tenant_slug}")
        try:
            try:
                tenant = resolve_tenant_only(tenant_slug=tenant_slug)
                current_app.logger.debug(f"Tenant resolved: {tenant}")
            except Exception:
                tenant = None

            if not tenant:
                return jsonify({"error": "Tenant no encontrado"}), 404

            # Registration for end-user (usuario)
            name = data.get("name") or data.get("nombre")
            email = data.get("email")
            password = data.get("password")
            telefono = data.get("telefono")

            if not name or not email or not password:
                return jsonify({"error": "Faltan datos obligatorios (nombre, email, password)."}), 400

            if _user_query().filter_by(email=email.strip().lower()).first():
                return jsonify({"error": "Email ya registrado."}), 409

            nuevo = User(
                name=name.strip(),
                email=email.strip().lower(),
                token=generate_token(),
                rol="usuario",
                telefono=telefono,
                acepta_marketing=True, # Default for portal users? Or passed from UI
                fecha_aceptacion_marketing=datetime.utcnow(),
                email_verified=False,
                email_verification_token=_generate_email_verification_token(),
                email_verification_sent_at=datetime.now(timezone.utc),
            )
            nuevo.set_password(password)

            # Link to tenant
            current_app.logger.debug("Attaching tenant")
            _attach_user_to_tenant(nuevo, tenant)

            # Set rubro/tipo_chat from tenant owner context if needed, or leave generic
            current_app.logger.debug("Finding owner")
            owner = _tenant_owner(tenant)
            if owner:
                nuevo.rubro_id = owner.rubro_id
                nuevo.tipo_chat = owner.tipo_chat
                nuevo.empresa_id = owner.id # Link as client of the owner

            db.session.add(nuevo)
            db.session.commit()

            # Migrate anon data if present
            anon_id = data.get("anon_id") or request.headers.get("X-Anon-Id") or request.headers.get("Anon-Id")
            if anon_id:
                try:
                    from services.ticket_service import servicio_tickets
                    servicio_tickets.migrar_tickets_de_anonimo(anon_id, nuevo.id)

                    # Migrate cart
                    from routes.market import _get_or_create_cart_for_user
                    if tenant:
                        _get_or_create_cart_for_user(tenant, nuevo, create_if_missing=False)
                except Exception as e:
                    current_app.logger.warning(f"Failed to migrate anon data: {e}")

            # Auto-login token
            jwt_payload = {
                'user_id': nuevo.id,
                'rol': nuevo.rol,
                'tipo_chat': nuevo.tipo_chat,
                'empresa_id': nuevo.empresa_id,
                'municipio_id': nuevo.municipio_id,
                'exp': datetime.utcnow() + timedelta(days=current_app.config.get("JWT_EXPIRATION_DAYS", 7))
            }
            jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")

            return jsonify({
                "token": jwt_token,
                "user": {
                    "id": nuevo.id,
                    "name": nuevo.name,
                    "email": nuevo.email,
                    "rol": nuevo.rol
                }
            }), 201
        except Exception as e:
            import traceback
            traceback.print_exc()
            current_app.logger.error(f"Error in end-user register: {e}")
            return jsonify({"error": str(e)}), 500

    empresa_token = data.get("empresa_token") or obtener_token()

    # Permitir nombres alternativos para el rubro y términos
    rubro_raw = data.get("rubro") or data.get("rubro_id") or data.get("sector")
    terminos_flag = (
        data.get("acepto_terminos")
        if "acepto_terminos" in data
        else data.get("acepta_terminos")
        if "acepta_terminos" in data
        else data.get("terminos")
        if "terminos" in data
        else data.get("terms")
    )

    # Si viene desde el widget/entidad y no se envía rubro, redirigir al flujo
    # de registro simplificado para usuarios finales.
    if empresa_token and not rubro_raw:
        current_app.logger.info(
            "[register] Delegando a chatuser_register_panel porque faltan datos de rubro/empresa"
        )
        return chatuser_register_panel()

    required_campos = {
        "name": data.get("name"),
        "email": data.get("email"),
        "password": data.get("password"),
        "nombre_empresa": data.get("nombre_empresa"),
        "rubro": rubro_raw,
        "tipo_chat": data.get("tipo_chat") or data.get("tipo") or data.get("tipo_empresa"),
    }

    missing = [k for k, v in required_campos.items() if not v]
    if missing or terminos_flag is None:
        return jsonify({
            "error": "Todos los campos son obligatorios.",
            "faltantes": missing + ([] if terminos_flag is not None else ["acepto_terminos"]),
            "botones": [{"texto": "Volver al chat"}],
        }), 400

    if not bool(terminos_flag):
        return jsonify({
            "error": "Es necesario aceptar los términos y condiciones.",
            "botones": [{"texto": "Volver al chat"}],
        }), 400

    if _user_query().filter_by(email=required_campos["email"].strip().lower()).first():
        return jsonify({
            "error": "Ya existe un usuario con ese correo electrónico.",
            "botones": [{"texto": "Volver al chat"}],
        }), 409

    # Buscar el rubro por id/clave/nombre (normalizado con alias básicos).
    rubro = _resolve_rubro_from_input(rubro_raw)

    if not rubro:
        return jsonify({
            "error": f"El rubro '{rubro_raw}' no es válido.",
            "botones": [{"texto": "Volver al chat"}],
        }), 400

    acepta_marketing = bool(data.get('acepta_marketing'))
    tags = data.get('tags')
    if isinstance(tags, list):
        tags_value = ','.join(tags)
    elif isinstance(tags, str):
        tags_value = tags
    else:
        tags_value = ''

    # BEGIN: MODIFIED LOGIC FOR tipo_chat
    rubro_nombre_normalizado = normalizar_rubro(rubro.nombre)
    
    # Determinar tipo_chat basado en el rubro, ignorando el input del usuario si el rubro es público.
    if es_rubro_publico(rubro):
        tipo_chat_final = "municipio"
        current_app.logger.info(f"Rubro '{rubro.nombre}' es público. Forzando tipo_chat a 'municipio'.")
    else:
        # Si no es un rubro público, respetar el `tipo_chat` del formulario, con 'pyme' como default.
        tipo_chat_in = required_campos.get('tipo_chat')
        sinonimos = {
            'muni': 'municipio',
            'municipios': 'municipio',
            'municipio': 'municipio',
            'pymes': 'pyme',
            'pyme': 'pyme',
        }
        tipo_chat_normalizado = sinonimos.get(str(tipo_chat_in).strip().lower()) if tipo_chat_in else 'pyme'
        
        # Asegurarse de que el tipo de chat sea válido, si no, usar 'pyme'.
        if tipo_chat_normalizado not in ('pyme', 'municipio'):
            tipo_chat_final = 'pyme'
            current_app.logger.warning(f"Valor de tipo_chat inválido: '{tipo_chat_in}'. Usando 'pyme' por defecto.")
        else:
            tipo_chat_final = tipo_chat_normalizado
    
    current_app.logger.info(f"Tipo de chat final determinado: '{tipo_chat_final}'")
    # END: MODIFIED LOGIC FOR tipo_chat

    rol_asignado = 'admin'
    empresa_id = None
    current_app.logger.info(f"[register] Attempting to register user with data: {data}")
    user = User(
        name=data['name'].strip(),
        email=data['email'].strip().lower(),
        token=generate_token(), # FIX: Generate and assign the legacy token on registration
        nombre_empresa=data['nombre_empresa'].strip(),
        rubro_id=rubro.id,
        plan="gratis",
        rol=rol_asignado,
        empresa_id=empresa_id,
        acepto_terminos=True,
        fecha_aceptacion_terminos=datetime.utcnow(),
        acepta_marketing=acepta_marketing,
        fecha_aceptacion_marketing=datetime.utcnow() if acepta_marketing else None,
        tags=tags_value,
        tipo_chat=tipo_chat_final,  # Usar la variable final determinada
        email_verified=False,
        email_verification_token=_generate_email_verification_token(),
        email_verification_sent_at=datetime.now(timezone.utc),
    )
    user.set_password(data['password'])

    try:
        db.session.add(user)
        db.session.commit()
        current_app.logger.info(f"Usuario registrado: {user.email} con ID {user.id}")

        _send_verification_email(user)
        _apply_welcome_points_if_configured(user)

        # Generar el token JWT
        jwt_payload = {
            'user_id': user.id,
            'rol': user.rol,
            'tipo_chat': user.tipo_chat,
            'empresa_id': user.empresa_id,
            'municipio_id': user.municipio_id,
            'exp': datetime.utcnow() + timedelta(days=current_app.config.get("JWT_EXPIRATION_DAYS", 7))
        }
        jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")

        owner_token = _resolve_owner_token(user)
        response_payload = {
            "mensaje": "Usuario registrado exitosamente.",
            "id": user.id,
            "token": jwt_token,
            "name": user.name,
            "email": user.email,
            "rol": user.rol,
            "tipo_chat": user.tipo_chat,
            "empresa_id": user.empresa_id,
            "tenant_slug": getattr(user, "tenant_slug", None),
            "tenantSlug": getattr(user, "tenant_slug", None),
        }
        entity_token_value = _include_entity_token_fields(response_payload, owner_token)

        resp = jsonify(response_payload)
        if entity_token_value:
            resp.headers.setdefault("X-Entity-Token", entity_token_value)
        return resp, 201
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al registrar usuario: {e}", exc_info=True)
        return jsonify({
            "error": "Error interno al guardar el usuario.",
            "botones": [{"texto": "Volver al chat"}],
        }), 500


@auth_bp.route('/verify-email', methods=['GET'])
def verify_email():
    token = request.args.get('token')
    if not token:
        return jsonify({"error": "Token requerido"}), 400

    user = _user_query().filter_by(email_verification_token=token).first()
    if not user:
        return jsonify({"error": "Token inválido"}), 400

    user.email_verified = True
    user.email_verification_token = None
    user.email_verification_sent_at = None
    db.session.add(user)
    db.session.commit()

    return jsonify({"mensaje": "Email verificado", "email_verified": True})


@auth_bp.route('/widget/register', methods=['POST'])
@token_requerido
def register_from_widget(user):
    """Registro rápido desde el widget asociado al token."""
    # Aceptar tanto JSON como formularios tradicionales
    data = request.get_json(silent=True)
    if not data:
        data = request.form.to_dict() if request.form else {}
    name = data.get('name') or "Sin nombre"
    email = data.get('email')
    password = data.get('password')
    telefono_raw = data.get('telefono') or data.get('phone')
    telefono = telefono_raw.strip() if isinstance(telefono_raw, str) and telefono_raw.strip() else None
    anon_id = (
        request.headers.get("X-Anon-Id")
        or request.headers.get("Anon-Id")
        or data.get("anon_id")
    )

    if not name or not email or not password:
        return jsonify({
            "error": "Faltan datos obligatorios.",
            "botones": [{"texto": "Volver al chat"}],
        }), 400

    owner_tenant = _tenant_for_owner(user)
    if not owner_tenant:
        return (
            jsonify({"error": "Tenant no especificado o no encontrado para el widget"}),
            404,
        )

    if _user_query().filter_by(email=email.strip().lower()).first():
        return jsonify({
            "error": "Email ya registrado.",
            "botones": [{"texto": "Volver al chat"}],
        }), 409

    acepta_marketing = bool(data.get('acepta_marketing'))
    tags = data.get('tags')
    if isinstance(tags, list):
        tags_value = ','.join(tags)
    elif isinstance(tags, str):
        tags_value = tags
    else:
        tags_value = ''
    current_app.logger.info(f"[register_from_widget] Attempting to register user with data: {data}")
    nuevo = User(
        name=name.strip(),
        email=email.strip().lower(),
        token=generate_token(), # FIX: Generate and assign the legacy token on registration
        rubro_id=user.rubro_id,
        empresa_id=user.id,
        plan="gratis",
        rol="usuario",
        tipo_chat=getattr(user, "tipo_chat", None) or ("municipio" if es_rubro_publico(user.rubro) else "pyme"),
        telefono=telefono,
        acepta_marketing=acepta_marketing,
        fecha_aceptacion_marketing=datetime.utcnow() if acepta_marketing else None,
        tags=tags_value,
        email_verified=False,
        email_verification_token=_generate_email_verification_token(),
        email_verification_sent_at=datetime.now(timezone.utc),
    )
    nuevo.set_password(password)
    try:
        db.session.add(nuevo)
        db.session.commit()

        _attach_user_to_tenant(nuevo, owner_tenant)
        db.session.commit()

        # --------- BLOQUE CRÍTICO --------------
        if anon_id:
            from services.ticket_service import servicio_tickets
            servicio_tickets.migrar_tickets_de_anonimo(anon_id, nuevo.id)
        # --------- FIN BLOQUE CRÍTICO ----------

        # Generar el token JWT
        jwt_payload = {
            'user_id': nuevo.id,
            'rol': nuevo.rol,
            'tipo_chat': nuevo.tipo_chat,
            'empresa_id': nuevo.empresa_id,
            'municipio_id': nuevo.municipio_id,
            'exp': datetime.utcnow() + timedelta(days=current_app.config.get("JWT_EXPIRATION_DAYS", 7))
        }
        jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")

        _send_verification_email(nuevo)
        _apply_welcome_points_if_configured(nuevo)

        owner_token = _resolve_owner_token(user)
        response_payload = {
            "id": nuevo.id,
            "token": jwt_token,
            "name": nuevo.name,
            "email": nuevo.email,
            "rol": nuevo.rol,
            "tipo_chat": nuevo.tipo_chat,
            "empresa_id": nuevo.empresa_id,
            "tenant_id": owner_tenant.id if owner_tenant else None,
            "tenant_slug": owner_tenant.slug if owner_tenant else getattr(nuevo, "tenant_slug", None),
            "tenantSlug": owner_tenant.slug if owner_tenant else getattr(nuevo, "tenant_slug", None),
            "marketplace": _tenant_market_payload(owner_tenant),
        }
        entity_token_value = _include_entity_token_fields(response_payload, owner_token)

        resp = jsonify(response_payload)
        if anon_id:
            resp.headers["X-Anon-Id"] = anon_id
            resp.headers["Anon-Id"] = anon_id
        if entity_token_value:
            resp.headers.setdefault("X-Entity-Token", entity_token_value)
        return resp, 201
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error en register_from_widget: {e}", exc_info=True)
        return jsonify({
            "error": "Error interno al registrar usuario.",
            "botones": [{"texto": "Volver al chat"}],
        }), 500

@auth_bp.route('/widget/login', methods=['POST'])
@token_requerido
def login_from_widget(owner_user):
    """Login desde el widget asociado al token."""
    data = request.get_json(silent=True)
    if not data:
        data = request.form.to_dict() if request.form else {}
    email = data.get('email')
    password = data.get('password')
    anon_id = (
        request.headers.get("X-Anon-Id")
        or request.headers.get("Anon-Id")
        or data.get("anon_id")
    )
    if not email or not password:
        return jsonify({"error": "Email y contraseña requeridos."}), 400

    owner_tenant = _resolve_tenant_for_user(owner_user, _tenant_for_owner(owner_user))
    if not owner_tenant:
        return jsonify({"error": "Tenant no especificado o no encontrado para el widget"}), 404

    user = _user_query().filter_by(
        email=email.strip().lower(), empresa_id=owner_user.id
    ).first()
    if not user or not user.check_password(password):
        return jsonify({"error": "Credenciales inválidas."}), 401

    if anon_id:
        from services.ticket_service import servicio_tickets
        servicio_tickets.migrar_tickets_de_anonimo(anon_id, user.id)

    _attach_user_to_tenant(user, owner_tenant)
    db.session.add(user)
    db.session.commit()

    user_rubro = getattr(user, "rubro", None)
    owner_rubro = getattr(owner_user, "rubro", None)
    rubro_nombre = user_rubro.nombre if user_rubro else owner_rubro.nombre if owner_rubro else "General"
    tipo_chat = _resolve_tipo_chat(user, tenant_obj=owner_tenant, rubro_nombre=rubro_nombre)

    effective_municipio_id = getattr(owner_tenant, "municipio_id", None) or user.municipio_id

    # Generar el token JWT
    jwt_payload = {
        'user_id': user.id,
        'rol': user.rol,
        'tipo_chat': tipo_chat,
        'empresa_id': user.empresa_id,
        'municipio_id': effective_municipio_id,
        'exp': datetime.utcnow() + timedelta(days=current_app.config.get("JWT_EXPIRATION_DAYS", 7))
    }
    jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")

    owner_token = _resolve_owner_token(owner_user)
    response_payload = {
        "id": user.id,
        "token": jwt_token,
        "name": user.name,
        "email": user.email,
        "rol": user.rol,
        "empresa_id": user.empresa_id,
        "rubro": rubro_nombre,
        "tipo_chat": tipo_chat,
        "categorias": getattr(user, "categorias_lista", []),
        "tenant_id": owner_tenant.id if owner_tenant else None,
        "tenant_slug": owner_tenant.slug if owner_tenant else getattr(user, "tenant_slug", None),
        "tenantSlug": owner_tenant.slug if owner_tenant else getattr(user, "tenant_slug", None),
        "marketplace": _tenant_market_payload(owner_tenant),
    }

    entity_token_value = _include_entity_token_fields(response_payload, owner_token)

    resp = jsonify(response_payload)
    if anon_id:
        resp.headers["X-Anon-Id"] = anon_id
        resp.headers["Anon-Id"] = anon_id
    if entity_token_value:
        resp.headers.setdefault("X-Entity-Token", entity_token_value)
    return resp


# Nuevos endpoints para el panel de usuarios de chat

@auth_bp.route('/chatuserregisterpanel', methods=['POST'])
def chatuser_register_panel():
    """Permite registrar un usuario final indicando el token de la entidad."""
    data = request.get_json(silent=True)
    if not data:
        data = request.form.to_dict() if request.form else {}

    empresa_token = data.get('empresa_token') or obtener_token()
    # Log received data for debugging, excluding password
    logged_data = {k: v for k, v in data.items() if k != 'password'}
    current_app.logger.info(f"[chatuser_register_panel] Received data (password excluded): {logged_data}")
    current_app.logger.info(
        f"[chatuser_register_panel] X-Anon-Id header: {request.headers.get('X-Anon-Id') or request.headers.get('Anon-Id')}"
    )


    if not empresa_token:
        current_app.logger.warning("[chatuser_register_panel] Registration attempt failed: Falta empresa_token")
        return jsonify({"error": "Falta empresa_token"}), 400

    owner_user = get_or_create_pyme_user_by_token(empresa_token.strip())
    if not owner_user:
        current_app.logger.warning(
            f"[chatuser_register_panel] Registration attempt failed: Token de empresa inválido o no encontrado: {empresa_token}"
        )
        return (
            jsonify({"error": "Token de empresa inválido o no encontrado"}),
            404,
        )

    owner_tenant = _tenant_for_owner(owner_user)

    name = data.get('name')
    email = data.get('email')
    password = data.get('password')
    anon_id = (
        request.headers.get("X-Anon-Id")
        or request.headers.get("Anon-Id")
        or data.get("anon_id")
    )

    owner_tipo_chat = getattr(owner_user, "tipo_chat", None) or (
        "municipio" if es_rubro_publico(owner_user.rubro) else "pyme"
    )
    owner_municipio_id = getattr(owner_user, "municipio_id", None)
    telefono_raw = data.get('telefono') or data.get('phone')
    telefono = telefono_raw.strip() if isinstance(telefono_raw, str) and telefono_raw.strip() else None

    # If the user is anonymous, we can assign a default password
    if not password:
        password = str(uuid.uuid4())

    if not name or not email:
        return (
            jsonify({"error": "Faltan datos obligatorios: nombre y email son requeridos.", "botones": [{"texto": "Volver al chat"}]}),
            400,
        )

    # Check if user with this email already exists
    existing_user = _user_query().filter(func.lower(User.email) == func.lower(email.strip())).first()

    if existing_user:
        current_app.logger.info(f"[chatuser_register_panel] Email '{email}' ya existe. User ID: {existing_user.id}, Empresa ID: {existing_user.empresa_id}. Owner User ID: {owner_user.id}")
        # User exists. Check if they belong to the same 'empresa'
        if existing_user.empresa_id == owner_user.id:
            # Email exists and is associated with the same empresa_id. Simulate login.
            current_app.logger.info(f"[chatuser_register_panel] Usuario existente '{email}' pertenece a la misma entidad (Owner ID: {owner_user.id}). Devolviendo datos del usuario existente.")
            if owner_municipio_id and existing_user.municipio_id != owner_municipio_id:
                existing_user.municipio_id = owner_municipio_id
            if not existing_user.tipo_chat:
                existing_user.tipo_chat = owner_tipo_chat
            if telefono and not existing_user.telefono:
                existing_user.telefono = telefono
            _attach_user_to_tenant(existing_user, owner_tenant)
            db.session.add(existing_user)
            db.session.commit()
            # Migrate tickets if anon_id is present
            if anon_id:
                from services.ticket_service import servicio_tickets
                servicio_tickets.migrar_tickets_de_anonimo(anon_id, existing_user.id)

            # Generar el token JWT
            jwt_payload = {
                'user_id': existing_user.id,
                'rol': existing_user.rol,
                'tipo_chat': existing_user.tipo_chat,
                'empresa_id': existing_user.empresa_id,
                'municipio_id': existing_user.municipio_id,
                'exp': datetime.utcnow() + timedelta(days=current_app.config.get("JWT_EXPIRATION_DAYS", 7))
            }
            jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")

            resp = jsonify({
                "id": existing_user.id,
                "token": jwt_token,
                "name": existing_user.name,
                "email": existing_user.email,
                "rol": existing_user.rol,
                "tipo_chat": existing_user.tipo_chat or owner_tipo_chat,
                "municipio_id": existing_user.municipio_id,
                "empresa_id": existing_user.empresa_id,
                "tenant_id": owner_tenant.id if owner_tenant else None,
                "tenant_slug": owner_tenant.slug if owner_tenant else None,
                "already_registered": True,
                "message": "Usuario ya registrado con esta entidad.",
                "marketplace": _tenant_market_payload(owner_tenant),
            })
            if anon_id:
                resp.headers["X-Anon-Id"] = anon_id
                resp.headers["Anon-Id"] = anon_id
            return resp, 200
        else:
            # Email exists but is associated with a different empresa_id.
            current_app.logger.warning(f"[chatuser_register_panel] Usuario existente '{email}' (Empresa ID: {existing_user.empresa_id}) intentó registrarse bajo una entidad diferente (Owner ID: {owner_user.id}).")
            return jsonify({
                "error": "El email ya está registrado en otra entidad.",
                "already_registered": True, # From the perspective of the email, it is registered.
                "conflicting_entity": True # More specific flag
            }), 409

    # If user does not exist, proceed with creation
    current_app.logger.info(f"[chatuser_register_panel] Email '{email}' no existe. Creando nuevo usuario para Owner ID: {owner_user.id} with data: {data}")
    acepta_marketing = bool(data.get('acepta_marketing'))
    tags = data.get('tags')
    if isinstance(tags, list):
        tags_value = ','.join(tags)
    elif isinstance(tags, str):
        tags_value = tags
    else:
        tags_value = ''

    nuevo = User(
        name=name.strip(),
        email=email.strip().lower(),
        # token=str(uuid.uuid4()), # El token ahora es JWT y se genera bajo demanda
        rubro_id=owner_user.rubro_id,
        empresa_id=owner_user.id,
        municipio_id=owner_municipio_id,
        plan="gratis",
        rol="lead" if not data.get('password') else "usuario",
        tipo_chat=owner_tipo_chat,
        telefono=telefono,
        acepta_marketing=acepta_marketing,
        fecha_aceptacion_marketing=datetime.utcnow() if acepta_marketing else None,
        tags=tags_value,
        email_verified=False,
        email_verification_token=_generate_email_verification_token(),
        email_verification_sent_at=datetime.now(timezone.utc),
    )
    nuevo.set_password(password)
    try:
        db.session.add(nuevo)
        db.session.commit()

        if owner_tenant:
            _attach_user_to_tenant(nuevo, owner_tenant)
            db.session.commit()

        # Log successful registration and association
        current_app.logger.info(f"[chatuser_register_panel] Nuevo usuario '{nuevo.email}' (ID: {nuevo.id}) registrado y asociado con la empresa/owner ID: {owner_user.id} ({owner_user.nombre_empresa if owner_user.nombre_empresa else owner_user.email}).")

        if anon_id:
            from services.ticket_service import servicio_tickets
            servicio_tickets.migrar_tickets_de_anonimo(anon_id, nuevo.id)

        # Update ChatSessionContext
        chat_session_id = request.headers.get("X-Chat-Session-Id")
        if chat_session_id:
            chat_context = ChatSessionContext.query.get(chat_session_id)
            if chat_context:
                chat_context.user_id = nuevo.id
                chat_context.anon_id = None
                if chat_context.context_data is None:
                    chat_context.context_data = {}
                chat_context.context_data['just_logged_in_flag'] = True
                flag_modified(chat_context, "context_data")
                db.session.add(chat_context)
                db.session.commit()
                current_app.logger.info(f"Updated ChatSessionContext {chat_session_id} for new user {nuevo.id}")

        # Generar el token JWT
        jwt_payload = {
            'user_id': nuevo.id,
            'rol': nuevo.rol,
            'tipo_chat': nuevo.tipo_chat,
            'empresa_id': nuevo.empresa_id,
            'municipio_id': nuevo.municipio_id,
            'exp': datetime.utcnow() + timedelta(days=current_app.config.get("JWT_EXPIRATION_DAYS", 7))
        }
        jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")

        _send_verification_email(nuevo)
        _apply_welcome_points_if_configured(nuevo)

        resp = jsonify({
            "id": nuevo.id,
            "token": jwt_token,
            "name": nuevo.name,
            "email": nuevo.email,
            "rol": nuevo.rol,
            "tipo_chat": nuevo.tipo_chat,
            "municipio_id": nuevo.municipio_id,
            "empresa_id": nuevo.empresa_id,
            "tenant_id": owner_tenant.id if owner_tenant else None,
            "tenant_slug": owner_tenant.slug if owner_tenant else None,
            "already_registered": False,
            "marketplace": _tenant_market_payload(owner_tenant),
        })
        if anon_id:
            resp.headers["X-Anon-Id"] = anon_id
            resp.headers["Anon-Id"] = anon_id
        return resp, 201
    except Exception as e:  # pragma: no cover - por si falla la DB
        db.session.rollback()
        current_app.logger.error(f"Error en chatuser_register_panel: {e}", exc_info=True)
        return (
            jsonify({"error": "Error interno al registrar usuario.", "botones": [{"texto": "Volver al chat"}]}),
            500,
        )


@auth_bp.route('/chatuserloginpanel', methods=['POST'])
def chatuser_login_panel():
    """Login de usuarios finales indicando el token de la empresa."""
    data = request.get_json(silent=True)
    if not data:
        data = request.form.to_dict() if request.form else {}

    empresa_token = data.get('empresa_token')
    if not empresa_token:
        return jsonify({"error": "Falta empresa_token"}), 400

    owner_user = _user_query().filter_by(token=empresa_token.strip()).first()
    if not owner_user:
        return jsonify({"error": "Token de empresa inválido"}), 404

    owner_tenant = _tenant_for_owner(owner_user)

    email = data.get('email')
    password = data.get('password')
    anon_id = (
        request.headers.get("X-Anon-Id")
        or request.headers.get("Anon-Id")
        or data.get("anon_id")
    )

    if not email or not password:
        return jsonify({"error": "Email y contraseña requeridos."}), 400

    user = _user_query().filter_by(email=email.strip().lower(), empresa_id=owner_user.id).first()
    if not user or not user.check_password(password):
        return jsonify({"error": "Credenciales inválidas."}), 401

    if anon_id:
        from services.ticket_service import servicio_tickets
        servicio_tickets.migrar_tickets_de_anonimo(anon_id, user.id)

    _attach_user_to_tenant(user, owner_tenant)
    db.session.add(user)
    db.session.commit()

    rubro_nombre = user.rubro.nombre if user.rubro else owner_user.rubro.nombre if owner_user else "General"
    tipo_chat = _resolve_tipo_chat(user, tenant_obj=owner_tenant, rubro_nombre=rubro_nombre)

    effective_municipio_id = getattr(owner_tenant, "municipio_id", None) or user.municipio_id

    # Generar el token JWT
    jwt_payload = {
        'user_id': user.id,
        'rol': user.rol,
        'tipo_chat': tipo_chat,
        'empresa_id': user.empresa_id,
        'municipio_id': effective_municipio_id,
        'exp': datetime.utcnow() + timedelta(days=current_app.config.get("JWT_EXPIRATION_DAYS", 7))
    }
    jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")

    resp = jsonify({
        "id": user.id,
        "token": jwt_token,
        "name": user.name,
        "email": user.email,
        "rol": user.rol,
        "empresa_id": user.empresa_id,
        "rubro": rubro_nombre,
        "tipo_chat": tipo_chat,
        "tenant_id": owner_tenant.id if owner_tenant else None,
        "tenant_slug": owner_tenant.slug if owner_tenant else None,
        "marketplace": _tenant_market_payload(owner_tenant),
    })
    if anon_id:
        resp.headers["X-Anon-Id"] = anon_id
        resp.headers["Anon-Id"] = anon_id
    return resp


# Nueva ruta para obtener información básica del token
@auth_bp.route('/token-info', methods=['GET'])
@token_requerido
def token_info(user):
    """Devuelve el rubro y la empresa asociados al token."""
    rubro_nombre = user.rubro.nombre if user.rubro else "General"
    return jsonify({
        "id": user.id,
        "rubro": rubro_nombre,
        "nombre_empresa": user.nombre_empresa,
        "rol": user.rol,
        "empresa_id": user.empresa_id,
        "tipo_chat": getattr(user, "tipo_chat", None) or ("municipio" if es_rubro_publico(rubro_nombre) else "pyme"),
    })


@auth_bp.route('/token-info', methods=['OPTIONS'])
def token_info_options():
    """Preflight CORS para /token-info."""
    anon_id = get_or_create_anon_id()
    resp = jsonify({'status': 'ok'})
    resp.headers.setdefault('X-Anon-Id', anon_id)
    resp.headers.setdefault('Anon-Id', anon_id)
    return resp, 204


def _dashboard_panels_for_user(user: User, tipo_chat: str) -> list[str]:
    """Return a stable panel list for dashboard/bootstrap contracts."""

    panels = ["perfil"]
    role = canonical_role(getattr(user, "rol", None))

    if role in ["admin", "empleado", "super_admin"]:
        panels.extend([
            "tickets",
            "usuarios_crm",
            "estadisticas",
            "analiticas_crm",
            "mapa_tickets",
        ])
        if tipo_chat == "pyme":
            panels.append("pedidos")
        elif tipo_chat == "municipio":
            panels.append("sugerencias_ciudadano")

    if role in ["admin", "super_admin"]:
        panels.append("empleados")

    if role == "super_admin":
        panels.append("tenants")

    return sorted(list(set(panels)))


@auth_bp.route('/session/bootstrap', methods=['GET'])
@token_requerido
def session_bootstrap(user: User):
    """Lightweight bootstrap contract so frontend can render shell immediately."""

    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
    started = time.perf_counter()

    rubro = user.rubro
    tipo_chat = user.tipo_chat or ("municipio" if es_rubro_publico(rubro) else "pyme")
    panels = _dashboard_panels_for_user(user, tipo_chat)
    profile_capabilities = _profile_capabilities_for_user(user)

    tenant_obj = _tenant_for_user(user)
    tenant_slug = getattr(user, "tenant_slug", None) or getattr(tenant_obj, "slug", None)

    payload = {
        "request_id": request_id,
        "user": {
            "id": user.id,
            "rol": user.rol,
            "tipo_chat": tipo_chat,
            "tenant_slug": tenant_slug,
            "permissions": profile_capabilities,
            "capabilities": profile_capabilities,
            "scopes": profile_capabilities,
        },
        "ui": {
            "panels": panels,
            "mobile_priority_panels": ["tickets", "pedidos", "estadisticas", "encuestas"],
        },
        "bootstrap": {
            "mode": "lite",
            "next": {
                "profile": "/auth/me",
                "dashboard": "/auth/me/dashboard",
            },
            "analytics": {
                "dashboard_fast_query": "fast=1",
                "seed_demo_endpoint_template": "/admin/encuestas/{encuesta_id}/seed-demo/bulk",
            },
        },
    }
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
    payload.setdefault("bootstrap", {})["timing_ms"] = elapsed_ms
    response = jsonify(payload)
    response.headers.setdefault("X-Request-Id", request_id)
    response.headers.setdefault("Server-Timing", f"bootstrap_total;dur={elapsed_ms}")
    current_app.logger.info(
        "[auth.bootstrap] request_id=%s user_id=%s panels=%s total_ms=%s",
        request_id,
        user.id,
        len(panels),
        elapsed_ms,
    )
    return response


@auth_bp.route('/me/dashboard', methods=['GET', 'OPTIONS'])
@token_requerido
def dashboard_info(user: User):
    """Devuelve las secciones disponibles para el usuario actual."""
    rubro = user.rubro
    tipo_chat = user.tipo_chat or ("municipio" if es_rubro_publico(rubro) else "pyme")
    final_panels = _dashboard_panels_for_user(user, tipo_chat)
    profile_capabilities = _profile_capabilities_for_user(user)

    return jsonify({
        "id": user.id,
        "rol": user.rol,
        "tipo_chat": tipo_chat,
        "panels": final_panels,
        "permissions": profile_capabilities,
        "capabilities": profile_capabilities,
        "scopes": profile_capabilities,
    })

@auth_bp.route(
    '/me', methods=['GET', 'PUT', 'OPTIONS'], provide_automatic_options=False
)
@auth_bp.route(
    '/perfil', methods=['GET', 'PUT', 'OPTIONS'], provide_automatic_options=False
)
@auth_bp.route(
    '/profile', methods=['GET', 'PUT', 'OPTIONS'], provide_automatic_options=False
)
@cross_origin(supports_credentials=True, automatic_options=False)
@token_requerido
def me_perfil(user):
    """
    Obtiene (GET) o actualiza (PUT) el perfil del usuario.
    """
    if request.method == 'GET':
        tenant_arg = request.args.get("tenant_slug") or request.args.get("tenant")
        entity_token_hint = request.args.get("entityToken") or request.headers.get(
            "X-Entity-Token"
        )

        if tenant_arg and not entity_token_hint and _looks_like_uuid(tenant_arg):
            tenant_exists = (
                TenantProfile.query.filter(
                    func.lower(TenantProfile.slug) == str(tenant_arg).lower()
                )
                .order_by(TenantProfile.id.asc())
                .first()
            )
            if tenant_exists is None:
                return (
                    jsonify(
                        {
                            "error": "invalid_tenant",
                            "detail": (
                                "El parámetro 'tenant_slug' parece un entityToken. "
                                "Enviá entityToken en su propio parámetro en lugar de tenant_slug."
                            ),
                        }
                    ),
                    400,
                )

        profile_data = build_profile_payload(user)
        nullable_contract_fields = {
            "widget_embed_token",
            "avatar_url",
            "picture",
            "avatar_source",
        }
        return jsonify(
            {
                k: v
                for k, v in profile_data.items()
                if v is not None or k in nullable_contract_fields
            }
        )

    elif request.method == 'PUT':
        data = request.get_json(silent=True) or {}
        if not data:
            return jsonify({"error": "No se recibieron datos."}), 400

        # Usar el servicio centralizado para actualizar el perfil
        if update_user_profile(user, data):
            return jsonify({"mensaje": "Perfil actualizado correctamente."})
        else:
            message, status = getattr(g, "profile_update_error", ("Error interno al guardar el perfil.", 500))
            return jsonify({"error": message}), status


PROFILE_AVATAR_UPLOAD_MAX_BYTES = 5 * 1024 * 1024
PROFILE_AVATAR_ALLOWED_MIMES = {"image/jpeg", "image/png", "image/webp"}
PROFILE_AVATAR_ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}


def _profile_avatar_payload(user: User, *, message: str) -> Dict[str, Any]:
    identity = get_user_profile_identity(user)
    avatar_url = identity.get("avatar_url")
    avatar_consent = bool(identity.get("avatar_consent"))
    avatar_policy_contract = build_profile_avatar_policy_contract()
    identity_payload = {
        "avatar_url": avatar_url,
        "picture": avatar_url,
        "avatar_source": identity.get("avatar_source"),
        "avatar_consent": avatar_consent,
        "profile_picture_consent": avatar_consent,
        **avatar_policy_contract,
    }
    return {
        "mensaje": message,
        "avatar_url": avatar_url,
        "picture": avatar_url,
        "avatar_source": identity.get("avatar_source"),
        "avatar_consent": avatar_consent,
        "profile_picture_consent": avatar_consent,
        "identity": identity_payload,
    }


def _request_avatar_consent_state() -> Optional[bool]:
    for key in ("avatar_consent", "profile_picture_consent", "consent", "consented"):
        if key not in request.form:
            continue
        value = str(request.form.get(key) or "").strip().lower()
        if value in {"0", "false", "no", "n", "denied", "rejected", "unconsented"}:
            return False
        if value in {"1", "true", "yes", "y", "si", "sí", "accepted", "consented"}:
            return True
    return None


@auth_bp.route('/profile/avatar', methods=['POST', 'DELETE'])
@auth_bp.route('/me/avatar', methods=['POST', 'DELETE'])
@cross_origin(supports_credentials=True)
@token_requerido
def upload_profile_avatar(user):
    """Persist a consented profile avatar uploaded by the authenticated user."""

    if request.method == 'DELETE':
        success, message, status = set_user_profile_avatar(
            user,
            "",
            source="profile_upload",
            overwrite=True,
        )
        if not success:
            return jsonify({"error": message}), status
        return jsonify(_profile_avatar_payload(user, message=message))

    if request.content_length and request.content_length > PROFILE_AVATAR_UPLOAD_MAX_BYTES + 2048:
        return jsonify({"error": "La imagen de perfil no puede superar 5 MB."}), 413

    uploaded = (
        request.files.get("avatar")
        or request.files.get("file")
        or request.files.get("image")
        or request.files.get("imagen")
    )
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "Envia una imagen de perfil."}), 400

    if _request_avatar_consent_state() is not True:
        return jsonify({"error": "Necesitamos tu consentimiento para usar esta imagen de perfil."}), 400

    filename = str(uploaded.filename or "")
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    mimetype = (uploaded.mimetype or "").lower()
    if extension not in PROFILE_AVATAR_ALLOWED_EXTENSIONS or mimetype not in PROFILE_AVATAR_ALLOWED_MIMES:
        return jsonify({"error": "Formato de avatar no permitido. Usa JPG, PNG o WebP."}), 400

    previous_user = getattr(g, "current_user", None)
    g.current_user = user
    try:
        upload_result = upload_to_gcs(uploaded, kind="profile_avatars")
    finally:
        g.current_user = previous_user

    if not upload_result:
        return jsonify({"error": "No se pudo subir la imagen de perfil."}), 500

    avatar_url = (
        upload_result.get("original_url")
        or upload_result.get("public_url")
        or upload_result.get("url")
    )
    success, message, status = set_user_profile_avatar(
        user,
        avatar_url,
        source="profile_upload",
        overwrite=True,
    )
    if not success:
        return jsonify({"error": message}), status

    payload = _profile_avatar_payload(user, message=message)
    payload["upload"] = {
        "original_name": upload_result.get("original_name"),
        "mimetype": upload_result.get("mimetype"),
        "size": upload_result.get("size"),
        "thumb_url": upload_result.get("thumbUrl")
        or (upload_result.get("thumb_meta") or {}).get("url"),
    }
    return jsonify(payload), 201

@auth_bp.route('/update_personal_data', methods=['POST'])
@token_requerido
def update_personal_data(current_user: User):
    """
    Actualiza los datos personales de un usuario.
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "No se recibieron datos."}), 400

    # Campos que se pueden actualizar
    email_value = data.get('email')
    if email_value is not None and email_value != current_user.email:
        success, message, status = change_user_email(
            current_user,
            email_value,
            data.get('current_password'),
            commit=False,
        )
        if not success:
            return jsonify({"error": message}), status

    allowed_fields = ['name', 'telefono', 'direccion']

    for field in allowed_fields:
        if field in data:
            setattr(current_user, field, data[field])

    try:
        db.session.commit()
        return jsonify({"mensaje": "Datos personales actualizados correctamente."})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(
            f"Error al actualizar datos personales para {current_user.email}: {e}", exc_info=True
        )
        return jsonify({"error": "Error interno al guardar los datos."}), 500


@auth_bp.route('/password/reset/request', methods=['POST'])
def request_password_reset():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()

    if not email:
        return jsonify({"error": "El email es requerido."}), 400

    try:
        user = _user_query().filter(func.lower(User.email) == email).first()
    except Exception:
        current_app.logger.exception("[auth] Error buscando usuario para reset de contraseña")
        return jsonify({"error": "No se pudo iniciar el reseteo de contraseña."}), 500

    generic_message = "Si el email está registrado, enviaremos las instrucciones para restablecer la contraseña."
    response_payload = {"mensaje": generic_message}

    if not user:
        return jsonify(response_payload)

    try:
        reset_token = create_password_reset_request(user)
    except Exception:
        current_app.logger.exception(
            "[auth] Error generando el token de reseteo para el usuario %s", getattr(user, 'id', None)
        )
        return jsonify({"error": "No se pudo iniciar el reseteo de contraseña."}), 500

    if current_app.config.get('TESTING'):
        response_payload['reset_token'] = reset_token
        response_payload['user_id'] = user.id
    else:
        current_app.logger.info(
            "[auth] Token de reseteo generado para %s. Pendiente de envío de email.", user.email
        )

    return jsonify(response_payload)


@auth_bp.route('/password/reset/confirm', methods=['POST'])
def confirm_password_reset():
    data = request.get_json(silent=True) or {}
    raw_token = data.get('token')
    new_password = data.get('new_password')
    confirm_password = data.get('confirm_password')

    if not raw_token or not new_password:
        return jsonify({"error": "Token y nueva contraseña son requeridos."}), 400

    if confirm_password and confirm_password != new_password:
        return jsonify({"error": "La confirmación de contraseña no coincide."}), 400

    token_parts = split_password_reset_token(raw_token)
    if not token_parts:
        return jsonify({"error": "Token inválido."}), 400

    selector, verifier = token_parts
    success, message, status = reset_password_with_token(selector, verifier, new_password)
    if not success:
        return jsonify({"error": message}), status

    return jsonify({"mensaje": message})


@auth_bp.route('/password/change', methods=['POST'])
@token_requerido
def change_password(current_user: User):
    data = request.get_json(silent=True) or {}
    current_password = data.get('current_password')
    new_password = data.get('new_password')
    confirm_password = data.get('confirm_password')

    if not current_password or not new_password:
        return jsonify({"error": "Debes indicar la contraseña actual y la nueva contraseña."}), 400

    if confirm_password and confirm_password != new_password:
        return jsonify({"error": "La confirmación de contraseña no coincide."}), 400

    success, message, status = change_user_password(current_user, current_password, new_password)
    if not success:
        return jsonify({"error": message}), status

    return jsonify({"mensaje": message})


@auth_bp.route('/email/change', methods=['POST'])
@token_requerido
def change_email(current_user: User):
    data = request.get_json(silent=True) or {}
    new_email = data.get('new_email')
    current_password = data.get('current_password')

    if not new_email or not current_password:
        return jsonify({"error": "Debes indicar el nuevo email y tu contraseña actual."}), 400

    success, message, status = change_user_email(current_user, new_email, current_password)
    if not success:
        return jsonify({"error": message}), status

    return jsonify({"mensaje": message, "email": current_user.email})

@auth_bp.route('/refresh', methods=['POST'])
@cross_origin(supports_credentials=True)
def refresh_token_endpoint():
    """Renueva el token actual si es válido."""
    token = obtener_token()
    if not token:
        return jsonify({"error": "Token requerido"}), 401

    try:
        payload = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=["HS256"])
        user_id = payload.get('user_id')
        user = _user_query().get(user_id)
        if not user:
            return jsonify({"error": "Usuario no encontrado"}), 401

        # Re-issue logic (duplicated for now, refactor later)
        tenant_slug = getattr(user, "tenant_slug", None)
        if not tenant_slug:
            tenant = _tenant_for_user(user)
            if tenant:
                tenant_slug = tenant.slug

        jwt_payload = {
            'user_id': user.id,
            'rol': user.rol,
            'tipo_chat': user.tipo_chat,
            'empresa_id': user.empresa_id,
            'municipio_id': user.municipio_id,
            'tenant_slug': tenant_slug,
            'exp': datetime.now(timezone.utc) + timedelta(days=7)
        }
        new_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")

        resp = jsonify({"token": new_token, "expires_in": 7 * 86400})
        return resp
    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Token expirado, por favor inicia sesión nuevamente"}), 401
    except Exception as e:
        return jsonify({"error": "Token inválido"}), 401

@auth_bp.route('/admin/login', methods=['POST', 'OPTIONS'])
@cross_origin(supports_credentials=True)
def admin_login():
    """Login exclusivo para administradores y empleados."""
    if request.method == 'OPTIONS':
        return jsonify({'status': 'ok'})

    data = request.get_json(silent=True) or {}
    email = data.get('email')
    password = data.get('password')

    if not email or not password:
        return jsonify({"error": "Credenciales requeridas"}), 400

    user = _user_query().filter_by(email=email.strip().lower()).first()

    if not user:
        current_app.logger.warning(f"[admin_login] User not found for email: {email.strip().lower()}")
        return jsonify({"error": "Credenciales inválidas"}), 401

    current_app.logger.info(
        "[admin_login] Found user: %s, email: %s, role: %s",
        user.id,
        user.email,
        user.rol,
    )

    if not user.check_password(password):
        current_app.logger.warning(f"[admin_login] Invalid password for user: {user.email}")
        return jsonify({"error": "Credenciales inválidas"}), 401

    # Check Role
    admin_role = canonical_role(getattr(user, "rol", None))
    if admin_role not in ['admin', 'empleado', 'super_admin']:
        return jsonify({"error": "Acceso denegado: No tienes permisos administrativos."}), 403

    # Generate Token
    # Prioritize the tenant owned by the user to ensure correct context.
    owned_tenant = _resolve_tenant_for_user(user)
    if owned_tenant:
        tenant_slug = owned_tenant.slug
    else:
        # Fallback to the tenant_slug attached to the user if no owned tenant is found.
        tenant_slug = getattr(user, "tenant_slug", None)

    current_app.logger.debug("[ADMIN_LOGIN_DEBUG] user=%s tenant=%s fallback_tenant_slug=%s final_slug=%s", user.id, getattr(owned_tenant, "slug", None), getattr(user, "tenant_slug", None), tenant_slug)

    tipo_chat = _resolve_tipo_chat(user, tenant_obj=owned_tenant)
    jwt_payload = {
        'user_id': user.id,
        'rol': user.rol,
        'tipo_chat': tipo_chat,
        'empresa_id': user.empresa_id,
        'municipio_id': user.municipio_id,
        'tenant_slug': tenant_slug,
        'exp': datetime.now(timezone.utc) + timedelta(days=7)
    }
    token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")
    profile_capabilities = _profile_capabilities_for_user(user)

    return jsonify({
        "token": token,
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "rol": user.rol,
            "permissions": profile_capabilities,
            "capabilities": profile_capabilities,
            "scopes": profile_capabilities,
            "tenant_slug": tenant_slug
        }
    })

# Alias exported for app.py compatibility
login_view_func = login

# Alias for app.py
me_perfil_view_func = me_perfil
