import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import quote

import jwt
import requests
from flask import current_app
from jwt import PyJWKClient
from sqlalchemy.orm.attributes import flag_modified

from database import db
from models import TenantProfile, User, generate_token
from services.auth_notification_service import (
    send_onboarding_whatsapp,
    send_verification_email,
)
from services.channel_activation import build_channel_activation_payload
from services.logic import es_rubro_publico
from services.tenant_factory import create_tenant_from_template
from services.user_service import get_user_profile_identity, set_user_profile_avatar
from utils.auth_helpers import (
    auth_session_version,
    bump_auth_session_version,
    generar_token,
)
from utils.roles import (
    ROLE_CLIENTE,
    ROLE_SUPERADMIN,
    is_authorized_superadmin_email,
    is_authorized_superadmin_user,
    is_super_admin_role,
    normalize_tenant_type,
    role_for_tenant_type,
    superadmin_email_allowlist_configured,
)

CLERK_AUTH_CONTRACT_VERSION = "auth.clerk.v1"
CLERK_TERMS_VERSION = "2026-07-11"
DEFAULT_SOCIAL_PROVIDERS = ("google", "facebook", "linkedin")
DEFAULT_CLERK_AUTHORIZED_PARTIES = (
    "https://chatboc.ar",
    "https://www.chatboc.ar",
)
DEFAULT_REQUIRED_DASHBOARD_SETUP: tuple[str, ...] = ()
_REVOKED_CLERK_SESSIONS: dict[str, dict[str, Any]] = {}
_REVOKED_CLERK_SESSIONS_LOCK = threading.Lock()
ONBOARDING_STARTER_MODULES = [
    {
        "id": "crm_operativo",
        "label": "CRM operativo",
        "description": "Bandeja de reclamos, pedidos, chats y responsables desde el primer ingreso.",
    },
    {
        "id": "whatsapp_widget",
        "label": "WhatsApp y widget",
        "description": "Canales listos para conectar IA, atencion humana y seguimiento por ticket.",
    },
    {
        "id": "marketplace_catalogo",
        "label": "Catalogo / marketplace",
        "description": "Base para productos, promociones, pedidos asistidos y carga por foto o archivo.",
    },
    {
        "id": "analytics_heatmaps",
        "label": "Analitica y mapas",
        "description": "Metricas iniciales para encuestas, reclamos, zonas calientes y actividad del canal.",
    },
]
ONBOARDING_VERTICAL_PRESETS = {
    "municipio": {
        "rubro": "atencion ciudadana",
        "primary_goal": "crm_reclamos",
        "preferred_channels": ["whatsapp", "webchat", "email"],
        "headline": "Reclamos, turnos, encuestas y mapa ciudadano",
        "recommended_modules": ["crm_operativo", "whatsapp_widget", "analytics_heatmaps"],
        "starter_questions": [
            "Que areas reciben mas reclamos o consultas?",
            "Que horario real de atencion humana debe respetar el bot?",
            "Que estados publicos quiere mostrar al vecino?",
        ],
    },
    "colegio": {
        "rubro": "educacion",
        "primary_goal": "whatsapp_ai",
        "preferred_channels": ["whatsapp", "webchat", "email"],
        "headline": "Familias, cuotas, comprobantes, turnos y admisiones",
        "recommended_modules": ["crm_operativo", "whatsapp_widget", "analytics_heatmaps"],
        "starter_questions": [
            "Que consultas hacen las familias todos los meses?",
            "Hay cuotas, comprobantes o certificados para automatizar?",
            "Que roles del colegio responden cada tipo de solicitud?",
        ],
    },
    "pyme": {
        "rubro": "ventas y atencion",
        "primary_goal": "ventas",
        "preferred_channels": ["whatsapp", "webchat", "instagram"],
        "headline": "Ventas, pedidos, catalogo y promociones",
        "recommended_modules": ["crm_operativo", "whatsapp_widget", "marketplace_catalogo"],
        "starter_questions": [
            "Que productos o servicios se venden por WhatsApp?",
            "Que promociones o descuentos por cantidad quiere ofrecer?",
            "Como debe derivarse una compra a un vendedor humano?",
        ],
    },
    "salud": {
        "rubro": "turnos y pacientes",
        "primary_goal": "whatsapp_ai",
        "preferred_channels": ["whatsapp", "webchat", "email"],
        "headline": "Turnos, recordatorios, admision y soporte al paciente",
        "recommended_modules": ["crm_operativo", "whatsapp_widget", "analytics_heatmaps"],
        "starter_questions": [
            "Que especialidades o profesionales deben ofrecer turnos?",
            "Que datos minimos se piden antes de derivar?",
            "Que mensajes requieren revision humana obligatoria?",
        ],
    },
    "inmobiliaria": {
        "rubro": "propiedades",
        "primary_goal": "ventas",
        "preferred_channels": ["whatsapp", "webchat", "instagram"],
        "headline": "Consultas, propiedades, visitas y leads comerciales",
        "recommended_modules": ["crm_operativo", "whatsapp_widget", "marketplace_catalogo"],
        "starter_questions": [
            "Que tipos de propiedades se publican?",
            "Que datos califican un lead antes de enviarlo al asesor?",
            "Como se agenda una visita desde el chat?",
        ],
    },
    "profesionales": {
        "rubro": "servicios profesionales",
        "primary_goal": "whatsapp_ai",
        "preferred_channels": ["whatsapp", "webchat", "email"],
        "headline": "Agenda, consultas frecuentes, presupuestos y seguimiento",
        "recommended_modules": ["crm_operativo", "whatsapp_widget", "analytics_heatmaps"],
        "starter_questions": [
            "Que servicio debe presupuestarse primero?",
            "Que preguntas califican mejor a un cliente?",
            "Que mensajes requieren derivacion inmediata?",
        ],
    },
    "otro": {
        "rubro": "operacion general",
        "primary_goal": "whatsapp_ai",
        "preferred_channels": ["whatsapp", "webchat"],
        "headline": "Atencion omnicanal, CRM y analitica inicial",
        "recommended_modules": ["crm_operativo", "whatsapp_widget", "analytics_heatmaps"],
        "starter_questions": [
            "Que quiere resolver primero con Chatboc?",
            "Que canal usan hoy sus clientes o vecinos?",
            "Que tarea repetitiva consume mas tiempo del equipo?",
        ],
    },
}


class ClerkAuthError(ValueError):
    pass


class ClerkNotConfigured(ClerkAuthError):
    pass


class ClerkTenantInactive(ClerkAuthError):
    pass


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _slugify(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    raw = re.sub(r"[^a-z0-9]+", "-", raw)
    return raw.strip("-")


def _env_list(name: str, fallback: Iterable[str]) -> list[str]:
    raw = os.getenv(name)
    if not raw:
        return list(fallback)
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _clerk_secret_key() -> Optional[str]:
    value = os.getenv("CLERK_SECRET_KEY")
    return str(value).strip() if value and str(value).strip() else None


def _clerk_webhook_secret() -> Optional[str]:
    value = os.getenv("CLERK_WEBHOOK_SIGNING_SECRET") or os.getenv("CLERK_WEBHOOK_SECRET")
    return str(value).strip() if value and str(value).strip() else None


def _clerk_revocation_ttl_seconds() -> int:
    try:
        configured = int(os.getenv("CLERK_REVOKED_SESSION_TTL_SECONDS") or "86400")
    except (TypeError, ValueError):
        configured = 86400
    return max(3600, min(configured, 604800))


def register_revoked_clerk_session(session_id: str | None, *, reason: str) -> bool:
    """Remember terminal Clerk sessions for immediate single-worker revocation."""

    sid = str(session_id or "").strip()
    if not sid:
        return False
    now = time.time()
    expires_at = now + _clerk_revocation_ttl_seconds()
    with _REVOKED_CLERK_SESSIONS_LOCK:
        expired = [key for key, value in _REVOKED_CLERK_SESSIONS.items() if value["expires_at"] <= now]
        for key in expired:
            _REVOKED_CLERK_SESSIONS.pop(key, None)
        _REVOKED_CLERK_SESSIONS[sid] = {
            "reason": str(reason or "session_terminal"),
            "revoked_at": now,
            "expires_at": expires_at,
        }
    return True


def is_clerk_session_revoked(session_id: str | None) -> bool:
    sid = str(session_id or "").strip()
    if not sid:
        return False
    now = time.time()
    with _REVOKED_CLERK_SESSIONS_LOCK:
        entry = _REVOKED_CLERK_SESSIONS.get(sid)
        if not entry:
            return False
        if entry["expires_at"] <= now:
            _REVOKED_CLERK_SESSIONS.pop(sid, None)
            return False
        return True


def _clerk_authorized_parties() -> list[str]:
    parties = _env_list("CLERK_AUTHORIZED_PARTIES", DEFAULT_CLERK_AUTHORIZED_PARTIES)
    if current_app.config.get("TESTING") or current_app.config.get("DEBUG"):
        parties.extend(
            [
                "http://127.0.0.1:4174",
                "http://localhost:4174",
                "http://127.0.0.1:5173",
                "http://localhost:5173",
            ]
        )
    return list(dict.fromkeys(party.rstrip("/") for party in parties if party))


def _clerk_authorized_parties_explicitly_configured() -> bool:
    return bool(str(os.getenv("CLERK_AUTHORIZED_PARTIES") or "").strip())


def _clerk_requires_authorized_party() -> bool:
    configured = os.getenv("CLERK_REQUIRE_AZP")
    if configured is not None:
        return _truthy(configured)
    return not bool(current_app.config.get("TESTING") or current_app.config.get("DEBUG"))


def _current_terms_version() -> str:
    return str(os.getenv("CHATBOC_TERMS_VERSION") or CLERK_TERMS_VERSION).strip()


def _vertical_preset(value: str | None) -> dict:
    key = str(value or "").strip().lower()
    return ONBOARDING_VERTICAL_PRESETS.get(key) or ONBOARDING_VERTICAL_PRESETS["otro"]


def _clerk_publishable_key() -> Optional[str]:
    value = (
        os.getenv("VITE_CLERK_PUBLISHABLE_KEY")
        or os.getenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY")
        or os.getenv("CLERK_PUBLISHABLE_KEY")
    )
    return str(value).strip() if value and str(value).strip() else None


def _clerk_runtime_environment(publishable_key: Optional[str]) -> str:
    key = str(publishable_key or "").strip()
    if key.startswith("pk_live_"):
        return "production"
    if key.startswith("pk_test_"):
        return "development"
    if key:
        return "unknown"
    return "unconfigured"


def is_clerk_superadmin_email(email: str | None) -> bool:
    return is_authorized_superadmin_email(email)


def clerk_enabled() -> bool:
    if _truthy(os.getenv("CLERK_DISABLED")):
        return False
    return _truthy(os.getenv("CLERK_ENABLED")) or bool(os.getenv("CLERK_ISSUER") or os.getenv("CLERK_JWKS_URL"))


def _clerk_issuer() -> Optional[str]:
    issuer = (
        os.getenv("CLERK_ISSUER")
        or os.getenv("CLERK_JWT_ISSUER")
        or os.getenv("NEXT_PUBLIC_CLERK_FRONTEND_API")
    )
    if issuer and not str(issuer).startswith("http"):
        issuer = f"https://{issuer}"
    return str(issuer).rstrip("/") if issuer else None


def _clerk_jwks_url() -> str:
    explicit = os.getenv("CLERK_JWKS_URL")
    if explicit:
        return explicit
    issuer = _clerk_issuer()
    if not issuer:
        raise ClerkNotConfigured("CLERK_ISSUER or CLERK_JWKS_URL is required")
    return f"{issuer}/.well-known/jwks.json"


def _clerk_verification_configured() -> bool:
    return bool(os.getenv("CLERK_JWKS_URL") or _clerk_issuer())


def _explicit_clerk_social_providers_configured() -> bool:
    return bool(str(os.getenv("CLERK_SOCIAL_PROVIDERS") or "").strip())


def _configured_clerk_social_providers(publishable_key: Optional[str] = None) -> list[str]:
    if _explicit_clerk_social_providers_configured():
        return _env_list("CLERK_SOCIAL_PROVIDERS", ())
    # Clerk shared OAuth credentials are acceptable in test/dev, but production
    # must not advertise social buttons until provider credentials are loaded.
    if str(publishable_key or "").startswith("pk_live_"):
        return []
    return list(DEFAULT_SOCIAL_PROVIDERS)


def _clerk_configuration_warnings(
    *,
    publishable_key: Optional[str],
    verification_configured: bool,
    social_providers: list[str],
    runtime_environment: str,
    backend_api_configured: bool,
    webhook_configured: bool,
) -> list[dict]:
    warnings: list[dict] = []
    if not publishable_key:
        warnings.append(
            {
                "code": "publishable_key_missing",
                "message": "Configure VITE_CLERK_PUBLISHABLE_KEY or NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY for the web client.",
            }
        )
    if not verification_configured:
        warnings.append(
            {
                "code": "jwt_verification_missing",
                "message": "Configure CLERK_ISSUER or CLERK_JWKS_URL on the backend. CLERK_SECRET_KEY alone does not validate session JWTs.",
            }
        )
    if runtime_environment == "development":
        warnings.append(
            {
                "code": "development_key_in_use",
                "message": "Clerk esta usando claves de development/test. Para produccion configure claves pk_live/sk_live, issuer/JWKS de produccion y dominio productivo.",
            }
        )
    if runtime_environment == "production" and not backend_api_configured:
        warnings.append(
            {
                "code": "backend_identity_api_missing",
                "message": "Configure CLERK_SECRET_KEY para obtener la identidad verificada desde Clerk y no confiar en datos enviados por el navegador.",
            }
        )
    if runtime_environment == "production" and not webhook_configured:
        warnings.append(
            {
                "code": "webhook_signing_secret_missing",
                "message": "Configure CLERK_WEBHOOK_SIGNING_SECRET para reconciliar altas, cambios y bajas de identidad.",
            }
        )
    if runtime_environment == "production" and not _clerk_authorized_parties_explicitly_configured():
        warnings.append(
            {
                "code": "authorized_parties_missing",
                "message": "Configure CLERK_AUTHORIZED_PARTIES explicitamente para limitar el origen de los session tokens.",
            }
        )
    if runtime_environment == "production" and not _clerk_requires_authorized_party():
        warnings.append(
            {
                "code": "authorized_party_not_required",
                "message": "Configure CLERK_REQUIRE_AZP=true en produccion.",
            }
        )
    if str(publishable_key or "").startswith("pk_live_") and not social_providers:
        warnings.append(
            {
                "code": "oauth_providers_missing",
                "message": "Configure production OAuth credentials in Clerk and set CLERK_SOCIAL_PROVIDERS before showing social login buttons.",
            }
        )
    return warnings


def build_clerk_frontend_contract() -> dict:
    publishable_key = _clerk_publishable_key()
    runtime_environment = _clerk_runtime_environment(publishable_key)
    providers = _configured_clerk_social_providers(publishable_key)
    verification_configured = _clerk_verification_configured()
    base_ui_enabled = bool(clerk_enabled() and publishable_key and verification_configured)
    backend_api_configured = bool(_clerk_secret_key())
    webhook_configured = bool(_clerk_webhook_secret())
    authorized_parties = _clerk_authorized_parties()
    authorized_parties_explicit = _clerk_authorized_parties_explicitly_configured()
    authorized_party_required = _clerk_requires_authorized_party()
    production_ready = bool(
        base_ui_enabled
        and runtime_environment == "production"
        and backend_api_configured
        and webhook_configured
        and authorized_parties_explicit
        and authorized_party_required
        and superadmin_email_allowlist_configured()
    )
    ui_enabled = bool(
        base_ui_enabled
        and (runtime_environment != "production" or production_ready)
    )
    return {
        "contract_version": CLERK_AUTH_CONTRACT_VERSION,
        "enabled": ui_enabled,
        "provider": "clerk",
        "environment": runtime_environment,
        "production_ready": production_ready,
        "session_sync_endpoint": "/auth/clerk/session",
        "onboarding_endpoint": "/auth/clerk/onboarding",
        "webhook_endpoint": "/auth/clerk/webhook",
        "webhook_required_events": [
            "user.created",
            "user.updated",
            "user.deleted",
            "session.ended",
            "session.removed",
            "session.revoked",
        ],
        "oauth_callback_path": "/sso-callback",
        "publishable_key": publishable_key,
        "publishable_key_configured": bool(publishable_key),
        "issuer_configured": bool(_clerk_issuer()),
        "jwks_configured": verification_configured,
        "backend_identity_api_configured": backend_api_configured,
        "webhook_configured": webhook_configured,
        "authorized_parties_configured": authorized_parties_explicit,
        "authorized_party_required": authorized_party_required,
        "ready_for_session_sync": bool(
            verification_configured
            and (runtime_environment != "production" or production_ready)
        ),
        "configuration_warnings": _clerk_configuration_warnings(
            publishable_key=publishable_key,
            verification_configured=verification_configured,
            social_providers=providers,
            runtime_environment=runtime_environment,
            backend_api_configured=backend_api_configured,
            webhook_configured=webhook_configured,
        ),
        "production_requirements": {
            "live_publishable_key": runtime_environment == "production",
            "session_verification": verification_configured,
            "backend_identity_api": backend_api_configured,
            "webhook_secret": webhook_configured,
            "authorized_parties": authorized_parties_explicit,
            "authorized_party_required": authorized_party_required,
            "superadmin_allowlist": superadmin_email_allowlist_configured(),
            "custom_domain_or_production_instance": runtime_environment == "production",
        },
        "social_providers": providers,
        "superadmin_policy": {
            "mode": "email_allowlist",
            "default_owner_guardrail": True,
            "allowlist_env_configured": superadmin_email_allowlist_configured(),
        },
        "frontend_env": {
            "VITE_CLERK_PUBLISHABLE_KEY": "required for Vite frontend",
            "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY": "supported for Next.js frontends",
        },
        "backend_env": {
            "CLERK_ISSUER": "required unless CLERK_JWKS_URL is set",
            "CLERK_JWKS_URL": "optional",
            "CLERK_AUDIENCE": "optional",
            "CLERK_AUTHORIZED_PARTIES": "required explicit allowlist for azp",
            "CLERK_REQUIRE_AZP": "must be true in production",
            "CLERK_SECRET_KEY": "required for trusted identity lookup",
            "CLERK_WEBHOOK_SIGNING_SECRET": "recommended",
            "ZOHO_SMTP_USER": "recommended for email verification",
            "ZOHO_SMTP_PASSWORD": "recommended for email verification",
        },
    }


def verify_clerk_session_token(token: str) -> dict:
    if not token or token.count(".") != 2:
        raise ClerkAuthError("Clerk session JWT is required")
    if not clerk_enabled():
        raise ClerkNotConfigured("Clerk auth is not enabled")

    issuer = _clerk_issuer()
    jwks_url = _clerk_jwks_url()
    audience = os.getenv("CLERK_AUDIENCE") or None
    options = {"verify_aud": bool(audience)}
    decode_kwargs: dict[str, Any] = {
        "algorithms": ["RS256"],
        "options": options,
    }
    if issuer:
        decode_kwargs["issuer"] = issuer
    elif not current_app.config.get("TESTING"):
        raise ClerkNotConfigured("CLERK_ISSUER is required outside tests")
    if audience:
        decode_kwargs["audience"] = audience

    try:
        signing_key = PyJWKClient(jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(token, signing_key.key, **decode_kwargs)
    except jwt.PyJWTError as exc:
        raise ClerkAuthError("Invalid Clerk session token") from exc

    authorized_parties = _clerk_authorized_parties()
    authorized_party = str(claims.get("azp") or "").strip().lower().rstrip("/")
    require_authorized_party = _clerk_requires_authorized_party()
    if authorized_party and authorized_party not in authorized_parties:
        raise ClerkAuthError("Clerk session authorized party is not allowed")
    if require_authorized_party and not authorized_party:
        raise ClerkAuthError("Clerk session authorized party is required")
    if not str(claims.get("sid") or "").strip():
        raise ClerkAuthError("Clerk session id is required")
    if str(claims.get("sts") or "").strip().lower() == "pending":
        raise ClerkAuthError("Clerk session is not active")
    verify_active_clerk_session(claims)
    return claims


def _clerk_session_lookup_required() -> bool:
    if current_app.config.get("TESTING") and not _clerk_secret_key():
        return False
    runtime_environment = _clerk_runtime_environment(_clerk_publishable_key())
    flask_environment = str(
        os.getenv("FLASK_ENV") or os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or ""
    ).strip().lower()
    return runtime_environment == "production" or flask_environment == "production" or bool(_clerk_secret_key())


def verify_active_clerk_session(claims: dict) -> dict:
    """Confirm that the signed JWT still belongs to an active Clerk session."""

    sid = str(claims.get("sid") or "").strip()
    clerk_user_id = str(claims.get("sub") or "").strip()
    if not sid:
        raise ClerkAuthError("Clerk session id is required")
    if is_clerk_session_revoked(sid):
        raise ClerkAuthError("Clerk session has been revoked")

    secret_key = _clerk_secret_key()
    if not secret_key:
        if _clerk_session_lookup_required():
            raise ClerkNotConfigured("CLERK_SECRET_KEY is required for active session verification")
        return {}

    base_url = str(os.getenv("CLERK_BACKEND_API_URL") or "https://api.clerk.com").rstrip("/")
    try:
        response = requests.get(
            f"{base_url}/v1/sessions/{quote(sid, safe='')}",
            headers={"Authorization": f"Bearer {secret_key}"},
            timeout=5,
        )
    except requests.RequestException as exc:
        raise ClerkNotConfigured("Clerk Backend API session lookup is unavailable") from exc

    if response.status_code in {404, 410}:
        register_revoked_clerk_session(sid, reason="backend_api_not_found")
        raise ClerkAuthError("Clerk session no longer exists")
    if response.status_code >= 400:
        raise ClerkNotConfigured("Clerk Backend API session lookup failed")

    try:
        session_data = response.json()
    except ValueError as exc:
        raise ClerkNotConfigured("Clerk Backend API returned invalid session data") from exc
    if not isinstance(session_data, dict) or str(session_data.get("id") or "").strip() != sid:
        raise ClerkAuthError("Clerk Backend API session mismatch")
    if clerk_user_id and str(session_data.get("user_id") or "").strip() != clerk_user_id:
        raise ClerkAuthError("Clerk Backend API session user mismatch")

    status = str(session_data.get("status") or "").strip().lower()
    if status != "active":
        register_revoked_clerk_session(sid, reason=f"backend_api_status:{status or 'missing'}")
        raise ClerkAuthError("Clerk session is not active")
    return session_data


def fetch_trusted_clerk_profile(claims: dict) -> dict:
    """Load security-sensitive identity fields from Clerk's authenticated Backend API."""

    clerk_user_id = str(claims.get("sub") or "").strip()
    if not clerk_user_id:
        raise ClerkAuthError("Clerk user id is missing")
    secret_key = _clerk_secret_key()
    if not secret_key:
        if current_app.config.get("TESTING"):
            return {}
        raise ClerkNotConfigured("CLERK_SECRET_KEY is required for trusted identity lookup")

    base_url = str(os.getenv("CLERK_BACKEND_API_URL") or "https://api.clerk.com").rstrip("/")
    try:
        response = requests.get(
            f"{base_url}/v1/users/{quote(clerk_user_id, safe='')}",
            headers={"Authorization": f"Bearer {secret_key}"},
            timeout=5,
        )
    except requests.RequestException as exc:
        raise ClerkNotConfigured("Clerk Backend API identity lookup is unavailable") from exc

    if response.status_code == 404:
        raise ClerkAuthError("Clerk user no longer exists")
    if response.status_code >= 400:
        raise ClerkNotConfigured("Clerk Backend API identity lookup failed")

    try:
        profile = response.json()
    except ValueError as exc:
        raise ClerkNotConfigured("Clerk Backend API returned invalid identity data") from exc
    if not isinstance(profile, dict) or str(profile.get("id") or "") != clerk_user_id:
        raise ClerkAuthError("Clerk Backend API identity mismatch")
    return profile


def _primary_email_from_profile(profile: dict) -> Tuple[Optional[str], bool]:
    candidates = profile.get("email_addresses")
    primary_id = profile.get("primary_email_address_id")
    if isinstance(candidates, list):
        primary = None
        for item in candidates:
            if not isinstance(item, dict):
                continue
            if item.get("id") == primary_id:
                primary = item
                break
            primary = primary or item
        if primary:
            email = primary.get("email_address") or primary.get("email")
            verification = primary.get("verification") or {}
            verified = (
                primary.get("verified")
                or verification.get("status") == "verified"
                or bool(primary.get("verified_at"))
            )
            return (str(email).strip().lower() if email else None, bool(verified))

    email = (
        profile.get("email")
        or profile.get("email_address")
        or profile.get("primary_email")
        or profile.get("primary_email_address")
    )
    verified = bool(profile.get("email_verified") or profile.get("email_verified_at"))
    return (str(email).strip().lower() if email else None, verified)


def _phone_from_profile(profile: dict) -> Optional[str]:
    explicit = profile.get("phone") or profile.get("telefono") or profile.get("phone_number")
    if explicit:
        return str(explicit).strip()
    candidates = profile.get("phone_numbers")
    primary_id = profile.get("primary_phone_number_id")
    if isinstance(candidates, list):
        for item in candidates:
            if not isinstance(item, dict):
                continue
            if primary_id and item.get("id") != primary_id:
                continue
            value = item.get("phone_number") or item.get("phone")
            if value:
                return str(value).strip()
        for item in candidates:
            if isinstance(item, dict) and (item.get("phone_number") or item.get("phone")):
                return str(item.get("phone_number") or item.get("phone")).strip()
    return None


def _providers_from_profile(profile: dict, claims: dict) -> list[str]:
    providers: set[str] = set()
    raw_claims = claims.get("external_accounts") or claims.get("providers") or claims.get("social_providers")
    if isinstance(raw_claims, list):
        for item in raw_claims:
            if isinstance(item, str):
                providers.add(item.replace("oauth_", "").replace("_oidc", "").lower())
            elif isinstance(item, dict):
                provider = item.get("provider") or item.get("strategy")
                if provider:
                    providers.add(str(provider).replace("oauth_", "").replace("_oidc", "").lower())

    raw_accounts = profile.get("external_accounts")
    if isinstance(raw_accounts, list):
        for item in raw_accounts:
            if not isinstance(item, dict):
                continue
            provider = item.get("provider") or item.get("strategy")
            if provider:
                providers.add(str(provider).replace("oauth_", "").replace("_oidc", "").lower())
    return sorted(providers)


def _avatar_url_from_profile(profile: dict, claims: dict) -> Optional[str]:
    for source in (profile, claims):
        for key in ("image_url", "profile_image_url", "avatar_url", "picture"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    raw_accounts = profile.get("external_accounts") or claims.get("external_accounts")
    if isinstance(raw_accounts, list):
        for account in raw_accounts:
            if not isinstance(account, dict):
                continue
            for key in ("image_url", "profile_image_url", "avatar_url", "picture"):
                value = account.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def extract_clerk_identity(
    claims: dict,
    profile: Optional[dict] = None,
    *,
    profile_is_trusted: bool = False,
) -> dict:
    trusted_profile = profile if profile_is_trusted and isinstance(profile, dict) else {}
    clerk_user_id = claims.get("sub") or trusted_profile.get("id") or trusted_profile.get("user_id")
    if not clerk_user_id:
        raise ClerkAuthError("Clerk user id is missing")

    profile_email, profile_verified = _primary_email_from_profile(trusted_profile)
    if profile_is_trusted:
        email = profile_email
        email_verified = bool(profile_verified)
    else:
        email = (
            claims.get("email")
            or claims.get("primary_email_address")
            or claims.get("email_address")
        )
        email = str(email).strip().lower() if email else None
        email_verified = bool(
            claims.get("email_verified")
            or claims.get("email_verified_at")
            or claims.get("evt") == "email_verified"
        )

    first = trusted_profile.get("first_name") or claims.get("first_name")
    last = trusted_profile.get("last_name") or claims.get("last_name")
    name = (
        trusted_profile.get("full_name")
        or claims.get("name")
        or " ".join(part for part in [first, last] if part).strip()
        or trusted_profile.get("username")
        or claims.get("username")
        or (email.split("@")[0] if email else "Usuario Chatboc")
    )

    return {
        "clerk_user_id": str(clerk_user_id),
        "email": email,
        "name": str(name).strip() or "Usuario Chatboc",
        "phone": _phone_from_profile(trusted_profile),
        "email_verified": email_verified,
        "identity_trusted": bool(profile_is_trusted),
        "social_providers": _providers_from_profile(trusted_profile, claims),
        "avatar_url": _avatar_url_from_profile(trusted_profile, claims),
        "created_at": trusted_profile.get("created_at") or claims.get("iat"),
        "updated_at": trusted_profile.get("updated_at"),
        "identity_source": "clerk_backend_api" if profile_is_trusted else "clerk_session_claims",
    }


def _find_user_by_clerk_id(clerk_user_id: str) -> Optional[User]:
    try:
        direct_match = User.query.filter(
            User.accesibilidad["auth"]["clerk"]["user_id"].as_string() == clerk_user_id
        ).first()
        if direct_match:
            return direct_match
    except Exception:
        # Older SQLite builds may lack JSON path support. The portable fallback
        # below is unbounded but streams only IDs and metadata.
        db.session.rollback()

    candidates = (
        db.session.query(User.id, User.accesibilidad)
        .filter(User.accesibilidad.isnot(None))
        .execution_options(yield_per=500)
    )
    for user_id, raw_metadata in candidates:
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        auth_meta = metadata.get("auth") if isinstance(metadata.get("auth"), dict) else {}
        clerk_meta = auth_meta.get("clerk") if isinstance(auth_meta.get("clerk"), dict) else {}
        if clerk_meta.get("user_id") == clerk_user_id:
            return User.query.get(user_id)
    return None


def _merge_clerk_metadata(user: User, identity: dict) -> None:
    meta = user.accesibilidad if isinstance(user.accesibilidad, dict) else {}
    auth_meta = meta.get("auth") if isinstance(meta.get("auth"), dict) else {}
    previous_clerk_meta = auth_meta.get("clerk") if isinstance(auth_meta.get("clerk"), dict) else {}
    auth_meta["provider"] = "clerk"
    auth_meta["session_version"] = auth_session_version(user)
    auth_meta["clerk"] = {
        **previous_clerk_meta,
        "user_id": identity["clerk_user_id"],
        "email": identity.get("email"),
        "social_providers": identity.get("social_providers") or [],
        "identity_source": identity.get("identity_source"),
        "last_sync_at": datetime.now(timezone.utc).isoformat(),
    }
    meta["auth"] = auth_meta
    user.accesibilidad = meta
    flag_modified(user, "accesibilidad")


def _fallback_role_for_clerk_user(user: User) -> str:
    tenant = tenant_for_user(user)
    if tenant:
        return role_for_tenant_type(getattr(tenant, "tipo", None))
    if getattr(user, "tipo_chat", None):
        return role_for_tenant_type(getattr(user, "tipo_chat", None))
    return ROLE_CLIENTE


def apply_clerk_role_guardrail(user: User, identity: dict) -> None:
    """Keep platform-wide superadmin access bound to an explicit email allowlist."""

    if not identity.get("identity_trusted"):
        return

    if identity.get("email_verified") and is_clerk_superadmin_email(identity.get("email")):
        user.rol = ROLE_SUPERADMIN
        user.tipo_chat = user.tipo_chat or "plataforma"
        return

    if is_super_admin_role(getattr(user, "rol", None)):
        user.rol = _fallback_role_for_clerk_user(user)


def upsert_user_from_clerk(
    claims: dict,
    profile: Optional[dict] = None,
    *,
    profile_is_trusted: bool = False,
) -> User:
    identity = extract_clerk_identity(
        claims,
        profile,
        profile_is_trusted=profile_is_trusted,
    )
    email = identity.get("email")
    user = _find_user_by_clerk_id(identity["clerk_user_id"])
    email_match = User.query.filter_by(email=email).first() if email else None
    if user is None and email_match is not None:
        if not identity.get("email_verified"):
            raise ClerkAuthError("Verified Clerk email is required to link an existing Chatboc account")
        user = email_match

    if user is not None:
        user_meta = user.accesibilidad if isinstance(user.accesibilidad, dict) else {}
        auth_meta = user_meta.get("auth") if isinstance(user_meta.get("auth"), dict) else {}
        clerk_meta = auth_meta.get("clerk") if isinstance(auth_meta.get("clerk"), dict) else {}
        if auth_meta.get("disabled") or clerk_meta.get("disabled") or clerk_meta.get("deleted_at"):
            raise ClerkAuthError("This Clerk identity is disabled in Chatboc")

    if user is None:
        if not email:
            raise ClerkNotConfigured("Trusted Clerk identity does not include an email address")
        user = User(
            name=identity["name"],
            email=email,
            token=generate_token(),
            rol=ROLE_CLIENTE,
            plan="gratis",
            acepto_terminos=False,
            fecha_aceptacion_terminos=None,
            email_verified=identity["email_verified"],
        )
        user.set_password(secrets.token_urlsafe(32))
        db.session.add(user)
    else:
        user.name = user.name or identity["name"]
        if identity.get("phone") and not user.telefono:
            user.telefono = identity["phone"]
        if identity["email_verified"]:
            user.email_verified = True

    _merge_clerk_metadata(user, identity)
    apply_clerk_role_guardrail(user, identity)
    if identity.get("avatar_url"):
        avatar_source = "clerk"
        providers = identity.get("social_providers") or []
        if len(providers) == 1:
            avatar_source = providers[0]
        success, message, _status = set_user_profile_avatar(
            user,
            identity["avatar_url"],
            source=avatar_source,
            overwrite=False,
            commit=False,
        )
        if not success:
            current_app.logger.warning(
                "[clerk] Avatar social descartado para usuario %s: %s",
                getattr(user, "id", None),
                message,
            )
    db.session.flush()
    return user


def tenant_for_user(user: User) -> Optional[TenantProfile]:
    if getattr(user, "tenant_id", None):
        tenant = TenantProfile.query.get(user.tenant_id)
        if tenant:
            return tenant
    if getattr(user, "tenant_slug", None):
        tenant = TenantProfile.query.filter_by(slug=user.tenant_slug).first()
        if tenant:
            return tenant
    return TenantProfile.query.filter(
        (TenantProfile.municipio_id == user.id) | (TenantProfile.pyme_id == user.id)
    ).first()


def require_active_tenant(tenant: Optional[TenantProfile]) -> Optional[TenantProfile]:
    if tenant is not None and getattr(tenant, "is_active", True) is False:
        raise ClerkTenantInactive("El tenant esta desactivado")
    return tenant


def _session_identity_payload(user: User) -> dict:
    identity = get_user_profile_identity(user)
    avatar_url = identity.get("avatar_url")
    avatar_consent = bool(identity.get("avatar_consent"))
    return {
        **identity,
        "picture": avatar_url,
        "profile_picture_consent": avatar_consent,
    }


def _issue_clerk_chatboc_token(
    user: User,
    tenant: Optional[TenantProfile],
    clerk_claims: Optional[dict],
) -> str:
    claims = clerk_claims if isinstance(clerk_claims, dict) else {}
    clerk_sid = str(claims.get("sid") or "").strip()
    clerk_user_id = str(claims.get("sub") or "").strip()
    if not clerk_sid:
        raise ClerkAuthError("Clerk session id is required to issue a Chatboc session")

    return generar_token(
        user.id,
        user.rol,
        user.tipo_chat,
        user.municipio_id,
        user.pyme_id,
        expires_in=timedelta(hours=1),
        extra_claims={
            "auth_provider": "clerk",
            "session_kind": "clerk",
            "clerk_sid": clerk_sid,
            "clerk_user_id": clerk_user_id or None,
            "sid": clerk_sid,
            "jti": secrets.token_urlsafe(24),
            "sv": auth_session_version(user),
            "tenant_id": getattr(tenant, "id", None),
            "tenant_slug": getattr(tenant, "slug", None),
            "empresa_id": user.empresa_id,
        },
    )


def build_chatboc_session_payload(
    user: User,
    tenant: Optional[TenantProfile] = None,
    clerk_claims: Optional[dict] = None,
) -> dict:
    tenant = require_active_tenant(tenant or tenant_for_user(user))
    onboarding = build_onboarding_contract(user, tenant)
    token = None
    if not onboarding.get("required"):
        token = _issue_clerk_chatboc_token(user, tenant, clerk_claims)
    channel_activation = build_channel_activation_payload(tenant)
    identity = _session_identity_payload(user)
    return {
        "contract_version": CLERK_AUTH_CONTRACT_VERSION,
        "token": token,
        "auth_provider": "clerk",
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "rol": user.rol,
            "role": user.rol,
            "tipo_chat": user.tipo_chat,
            "tenant_id": getattr(tenant, "id", None),
            "tenant_slug": getattr(tenant, "slug", None),
            "tenantSlug": getattr(tenant, "slug", None),
            "email_verified": bool(user.email_verified),
            "telefono": user.telefono,
            "avatar_url": identity.get("avatar_url"),
            "picture": identity.get("picture"),
            "avatar_source": identity.get("avatar_source"),
            "avatar_consent": bool(identity.get("avatar_consent")),
            "profile_picture_consent": bool(identity.get("profile_picture_consent")),
            "identity": identity,
        },
        "tenant": serialize_tenant(tenant),
        "onboarding": onboarding,
        "channel_activation": channel_activation,
    }


def serialize_tenant(tenant: Optional[TenantProfile]) -> Optional[dict]:
    if not tenant:
        return None
    return {
        "id": tenant.id,
        "slug": tenant.slug,
        "nombre": tenant.nombre,
        "tipo": tenant.tipo,
        "vertical": tenant.vertical,
        "subvertical": tenant.subvertical,
        "plan": tenant.plan,
        "is_active": tenant.is_active,
    }


def _accepted_clerk_terms_version(user: User) -> Optional[str]:
    metadata = user.accesibilidad if isinstance(user.accesibilidad, dict) else {}
    auth_meta = metadata.get("auth") if isinstance(metadata.get("auth"), dict) else {}
    clerk_meta = auth_meta.get("clerk") if isinstance(auth_meta.get("clerk"), dict) else {}
    terms = clerk_meta.get("terms") if isinstance(clerk_meta.get("terms"), dict) else {}
    version = str(terms.get("version") or "").strip()
    return version or None


def _has_current_terms_acceptance(user: User) -> bool:
    return bool(
        getattr(user, "acepto_terminos", False)
        and _accepted_clerk_terms_version(user) == _current_terms_version()
    )


def _record_clerk_terms_acceptance(
    user: User,
    tenant: Optional[TenantProfile],
    *,
    source: str,
) -> datetime:
    accepted_at = datetime.now(timezone.utc)
    terms_version = _current_terms_version()
    user.acepto_terminos = True
    user.fecha_aceptacion_terminos = accepted_at

    user_meta = user.accesibilidad if isinstance(user.accesibilidad, dict) else {}
    user_auth_meta = user_meta.get("auth") if isinstance(user_meta.get("auth"), dict) else {}
    clerk_meta = user_auth_meta.get("clerk") if isinstance(user_auth_meta.get("clerk"), dict) else {}
    clerk_meta["terms"] = {
        "accepted": True,
        "version": terms_version,
        "accepted_at": accepted_at.isoformat(),
        "source": source,
    }
    user_auth_meta["clerk"] = clerk_meta
    user_meta["auth"] = user_auth_meta
    user.accesibilidad = user_meta
    flag_modified(user, "accesibilidad")

    if tenant is not None:
        cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
        auth_cfg = cfg.get("auth") if isinstance(cfg.get("auth"), dict) else {}
        auth_cfg["terms"] = {
            "accepted": True,
            "version": terms_version,
            "accepted_at": accepted_at.isoformat(),
            "source": source,
        }
        cfg["auth"] = auth_cfg
        tenant.configuracion = cfg
        flag_modified(tenant, "configuracion")

    return accepted_at


def build_onboarding_contract(user: User, tenant: Optional[TenantProfile] = None) -> dict:
    if is_authorized_superadmin_user(user):
        return {
            "required": False,
            "status": "platform_admin",
            "title": "Acceso superadmin activo",
            "description": "Tu cuenta Clerk esta vinculada al panel global de Chatboc.",
            "submit_endpoint": "/auth/clerk/onboarding",
            "modal": {"steps": [], "vertical_options": [], "goal_options": []},
        }

    tenant = tenant or tenant_for_user(user)
    terms_pending = bool(tenant and not _has_current_terms_acceptance(user))
    onboarding_required = tenant is None or terms_pending
    onboarding_status = "terms_pending" if terms_pending else ("complete" if tenant else "pending")
    return {
        "required": onboarding_required,
        "status": onboarding_status,
        "title": "Actualiza tu consentimiento" if terms_pending else "Completa tu espacio Chatboc",
        "description": (
            "Acepta la version vigente de los Terminos y la Politica de Privacidad para continuar."
            if terms_pending
            else "Con estos datos creamos el tenant, plantilla inicial, CRM y canales."
        ),
        "submit_endpoint": "/auth/clerk/onboarding",
        "modal": {
            "mode": "terms_only" if terms_pending else "tenant_setup",
            "existing_tenant": serialize_tenant(tenant),
            "summary_cards": [
                {
                    "id": "identity",
                    "label": "Identidad verificada",
                    "description": "Usamos Clerk para login social, email y foto solo con fuente consentida.",
                },
                {
                    "id": "workspace",
                    "label": "Tenant y CRM",
                    "description": "Creamos el espacio operativo con rol, permisos y bandeja inicial.",
                },
                {
                    "id": "channels",
                    "label": "Canales y plantillas",
                    "description": "Preparamos widget y plantillas base; WhatsApp productivo requiere plan Full y configuracion Meta/Twilio.",
                },
                {
                    "id": "analytics",
                    "label": "Metricas accionables",
                    "description": "Dejamos preparado el tablero para reclamos, pedidos, encuestas y mapas.",
                },
            ],
            "starter_modules": ONBOARDING_STARTER_MODULES,
            "vertical_presets": ONBOARDING_VERTICAL_PRESETS,
            "steps": [
                {
                    "id": "organization",
                    "title": "Organizacion",
                    "fields": [
                        {"name": "tenant_name", "type": "text", "required": True},
                        {"name": "vertical", "type": "select", "required": True},
                        {"name": "rubro", "type": "text", "required": True},
                    ],
                },
                {
                    "id": "contact",
                    "title": "Contacto",
                    "fields": [
                        {"name": "telefono", "type": "tel", "required": False},
                        {"name": "website", "type": "url", "required": False},
                        {"name": "ciudad", "type": "text", "required": False},
                    ],
                },
                {
                    "id": "setup",
                    "title": "Operacion",
                    "fields": [
                        {"name": "primary_goal", "type": "select", "required": False},
                        {"name": "team_size", "type": "select", "required": False},
                        {"name": "preferred_channels", "type": "multi_select", "required": False},
                    ],
                },
            ],
            "vertical_options": [
                {"value": "municipio", "label": "Municipio / gobierno"},
                {"value": "colegio", "label": "Colegio / educacion"},
                {"value": "pyme", "label": "Empresa / comercio"},
                {"value": "salud", "label": "Salud"},
                {"value": "inmobiliaria", "label": "Inmobiliaria"},
                {"value": "profesionales", "label": "Servicios profesionales"},
                {"value": "otro", "label": "Otro"},
            ],
            "goal_options": [
                {"value": "whatsapp_ai", "label": "Atender WhatsApp con IA"},
                {"value": "crm_reclamos", "label": "Gestionar reclamos/tickets"},
                {"value": "ventas", "label": "Vender y tomar pedidos"},
                {"value": "encuestas", "label": "Encuestas y analitica"},
                {"value": "marketplace", "label": "Publicar catalogo / marketplace"},
                {"value": "omnicanal", "label": "Centralizar conversaciones"},
            ],
            "social_login": {
                "provider": "clerk",
                "enabled_providers": _configured_clerk_social_providers(_clerk_publishable_key()),
                "required_dashboard_setup": _env_list(
                    "CLERK_REQUIRED_DASHBOARD_SETUP",
                    DEFAULT_REQUIRED_DASHBOARD_SETUP,
                ),
                "connection_aliases": {
                    "linkedin": "linkedin_oidc",
                },
            },
            "whatsapp_business_requirements": {
                "production_enabled_by_default": False,
                "required_plan": "full",
                "required_provider_setup": ["meta_business", "twilio_whatsapp_sender"],
                "free_plan_state": "created_without_waba_provisioning",
                "message": "El tenant se crea en modo free. WhatsApp productivo y creacion real de plantillas se activan con plan Full y sender Meta/Twilio configurado.",
            },
            "plan_policy": {
                "self_service_plan": "free",
                "requested_plan_allowed": False,
                "productive_plan": "full",
                "upgrade_requires": "superadmin_or_commercial_approval",
                "message": "El registro publico siempre crea un espacio Free. El plan Full se solicita para revision comercial y solo se concede desde administracion.",
            },
            "terms": {
                "required": True,
                "version": _current_terms_version(),
                "terms_url": "/terminos",
                "privacy_url": "/privacidad",
                "label": "Acepto los Terminos y la Politica de Privacidad",
            },
            "profile_picture_policy": "consented_upload_or_social_only",
        },
    }


def _tenant_type_from_payload(payload: dict) -> str:
    raw = (
        payload.get("tenant_type")
        or payload.get("tipo")
        or payload.get("vertical")
        or payload.get("rubro")
        or "pyme"
    )
    raw_text = str(raw or "").strip().lower()
    if raw_text in {"gobierno", "government", "municipalidad"}:
        return "municipio"
    if raw_text in {"colegio", "educacion", "escuela", "school"}:
        return "colegio"
    try:
        if es_rubro_publico(raw_text):
            return "municipio"
    except Exception:
        pass
    return normalize_tenant_type(raw_text if raw_text in {"municipio", "colegio", "pyme"} else "pyme")


def _requested_plan_from_payload(payload: dict) -> str:
    raw = str(payload.get("plan") or payload.get("requested_plan") or "free").strip().lower()
    if raw in {"gratis", "free", "trial", "basic"}:
        return "free"
    if raw in {"full", "pro", "premium"}:
        return raw
    return "custom" if raw else "free"


def _unique_tenant_slug(base: str) -> str:
    slug = _slugify(base) or f"tenant-{secrets.token_hex(3)}"
    candidate = slug
    suffix = 2
    while TenantProfile.query.filter_by(slug=candidate).first():
        candidate = f"{slug}-{suffix}"
        suffix += 1
    return candidate


def complete_clerk_onboarding(user: User, payload: dict) -> TenantProfile:
    if not _truthy(payload.get("terms_accepted")):
        raise ClerkAuthError("Debes aceptar los Terminos y la Politica de Privacidad para crear el tenant")
    terms_version = _current_terms_version()
    if not terms_version:
        raise ClerkAuthError("terms_version is required")
    submitted_terms_version = str(payload.get("terms_version") or "").strip()
    if submitted_terms_version != terms_version:
        raise ClerkAuthError("La version de Terminos cambio. Recarga la pagina antes de aceptar")

    existing = tenant_for_user(user)
    if existing:
        require_active_tenant(existing)
        _record_clerk_terms_acceptance(
            user,
            existing,
            source="clerk_terms_reacceptance",
        )
        db.session.add(user)
        db.session.add(existing)
        db.session.commit()
        return existing

    tenant_name = (
        payload.get("tenant_name")
        or payload.get("nombre_empresa")
        or payload.get("organization_name")
        or payload.get("empresa")
        or user.nombre_empresa
        or user.name
    )
    tenant_name = str(tenant_name or "").strip()
    if not tenant_name:
        raise ClerkAuthError("tenant_name is required")

    tenant_type = _tenant_type_from_payload(payload)
    preset = _vertical_preset(payload.get("vertical") or tenant_type)
    rubro = str(payload.get("rubro") or preset.get("rubro") or tenant_type).strip()
    primary_goal = str(payload.get("primary_goal") or preset.get("primary_goal") or "whatsapp_ai").strip()
    preferred_channels = payload.get("preferred_channels") or preset.get("preferred_channels") or ["whatsapp", "webchat"]
    requested_slug = payload.get("tenant_slug") or payload.get("slug") or tenant_name
    slug = _unique_tenant_slug(str(requested_slug))
    requested_plan = _requested_plan_from_payload(payload)
    plan_for_factory = "free"

    tenant = create_tenant_from_template(
        nombre=tenant_name,
        slug=slug,
        tipo=tenant_type,
        plan=plan_for_factory,
        owner_email=user.email,
        owner_password=None,
        allow_existing_owner=True,
        reset_existing_owner_password=False,
    )

    # Reload the possibly updated user object after tenant_factory commit.
    db.session.refresh(user)
    user.rol = role_for_tenant_type(tenant_type)
    user.tipo_chat = tenant_type
    user.nombre_empresa = tenant_name
    user.tenant_id = tenant.id
    user.tenant_slug = tenant.slug
    user.telefono = str(payload.get("telefono") or payload.get("phone") or user.telefono or "").strip() or None
    user.link_web = str(payload.get("website") or payload.get("link_web") or user.link_web or "").strip() or None
    user.ciudad = str(payload.get("ciudad") or payload.get("city") or user.ciudad or "").strip() or None
    user.provincia = str(payload.get("provincia") or payload.get("state") or user.provincia or "").strip() or None
    user.pais = str(payload.get("pais") or payload.get("country") or user.pais or "").strip() or None
    accepted_at = _record_clerk_terms_acceptance(
        user,
        tenant,
        source="clerk_tenant_onboarding",
    )

    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    cfg["auth"] = {
        **(cfg.get("auth") if isinstance(cfg.get("auth"), dict) else {}),
        "provider": "clerk",
        "social_providers": _configured_clerk_social_providers(_clerk_publishable_key()),
    }
    cfg["onboarding"] = {
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "source": "clerk",
        "rubro": rubro,
        "primary_goal": primary_goal,
        "team_size": payload.get("team_size"),
        "preferred_channels": preferred_channels,
        "vertical_preset": payload.get("vertical") or tenant_type,
        "starter_modules": preset.get("recommended_modules") or [],
        "requested_plan": requested_plan,
        "granted_plan": plan_for_factory,
        "plan_policy": "self_service_creates_free_until_admin_upgrade",
        "terms": {
            "accepted": True,
            "version": terms_version,
            "accepted_at": accepted_at.isoformat(),
        },
    }
    tenant.configuracion = cfg
    tenant.vertical = tenant.vertical or str(payload.get("vertical") or tenant_type)
    tenant.subvertical = tenant.subvertical or rubro
    flag_modified(tenant, "configuracion")

    db.session.add(user)
    db.session.add(tenant)
    db.session.commit()

    if not user.email_verified:
        if not user.email_verification_token:
            user.email_verification_token = secrets.token_urlsafe(48)
            user.email_verification_sent_at = datetime.now(timezone.utc)
            db.session.add(user)
            db.session.commit()
        send_verification_email(user, reason="clerk_onboarding")
    send_onboarding_whatsapp(user, tenant_name=tenant.nombre, tenant_slug=tenant.slug)
    return tenant


def verify_clerk_webhook_signature(raw_body: bytes, headers: dict) -> None:
    secret = _clerk_webhook_secret()
    if not secret:
        raise ClerkNotConfigured("CLERK_WEBHOOK_SIGNING_SECRET is required")

    msg_id = headers.get("svix-id") or headers.get("Svix-Id")
    timestamp = headers.get("svix-timestamp") or headers.get("Svix-Timestamp")
    signature_header = headers.get("svix-signature") or headers.get("Svix-Signature")
    if not msg_id or not timestamp or not signature_header:
        raise ClerkAuthError("Missing Clerk webhook signature headers")

    try:
        ts_int = int(timestamp)
    except ValueError as exc:
        raise ClerkAuthError("Invalid Clerk webhook timestamp") from exc
    tolerance = int(os.getenv("CLERK_WEBHOOK_TOLERANCE_SECONDS") or "300")
    if abs(int(time.time()) - ts_int) > tolerance:
        raise ClerkAuthError("Expired Clerk webhook timestamp")

    key_text = secret.split("_", 1)[1] if secret.startswith("whsec_") else secret
    try:
        key = base64.b64decode(key_text)
    except Exception:
        key = key_text.encode("utf-8")

    signed_content = b".".join([str(msg_id).encode(), str(timestamp).encode(), raw_body])
    expected = base64.b64encode(hmac.new(key, signed_content, hashlib.sha256).digest()).decode()
    provided = [
        part.split(",", 1)[1] if "," in part else part
        for part in str(signature_header).split()
        if part
    ]
    if not any(hmac.compare_digest(expected, sig) for sig in provided):
        raise ClerkAuthError("Invalid Clerk webhook signature")


def _revoke_chatboc_sessions_for_clerk_user(user: User, *, reason: str) -> int:
    next_version = bump_auth_session_version(user)
    meta = user.accesibilidad if isinstance(user.accesibilidad, dict) else {}
    auth_meta = meta.get("auth") if isinstance(meta.get("auth"), dict) else {}
    auth_meta["last_revocation"] = {
        "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    meta["auth"] = auth_meta
    user.accesibilidad = meta
    flag_modified(user, "accesibilidad")
    return next_version


def sync_clerk_webhook_event(event: dict) -> dict:
    event_type = event.get("type") or ""
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if event_type in {"user.created", "user.updated"}:
        clerk_user_id = str(data.get("id") or "").strip()
        existing = _find_user_by_clerk_id(clerk_user_id) if clerk_user_id else None
        if existing:
            existing_meta = existing.accesibilidad if isinstance(existing.accesibilidad, dict) else {}
            existing_auth = existing_meta.get("auth") if isinstance(existing_meta.get("auth"), dict) else {}
            existing_clerk = existing_auth.get("clerk") if isinstance(existing_auth.get("clerk"), dict) else {}
            if existing_auth.get("disabled") or existing_clerk.get("disabled") or existing_clerk.get("deleted_at"):
                return {
                    "status": "ignored_disabled",
                    "user_id": existing.id,
                    "event_type": event_type,
                }
        claims = {"sub": data.get("id")}
        user = upsert_user_from_clerk(claims, data, profile_is_trusted=True)
        db.session.commit()
        return {"status": "synced", "user_id": user.id, "event_type": event_type}

    if event_type in {"session.ended", "session.removed", "session.revoked"}:
        clerk_session_id = str(data.get("id") or data.get("session_id") or "").strip()
        register_revoked_clerk_session(clerk_session_id, reason=event_type)
        clerk_user_id = str(data.get("user_id") or "").strip()
        user = _find_user_by_clerk_id(clerk_user_id) if clerk_user_id else None
        if user:
            session_version = _revoke_chatboc_sessions_for_clerk_user(user, reason=event_type)
            db.session.add(user)
            db.session.commit()
            return {
                "status": "sessions_revoked",
                "user_id": user.id,
                "session_version": session_version,
                "event_type": event_type,
                "clerk_session_id": clerk_session_id or None,
                "clerk_user_id": clerk_user_id or None,
            }
        return {
            "status": "session_revoked_unlinked",
            "event_type": event_type,
            "clerk_session_id": clerk_session_id or None,
            "clerk_user_id": clerk_user_id or None,
        }
    if event_type == "user.deleted":
        clerk_user_id = data.get("id")
        user = _find_user_by_clerk_id(str(clerk_user_id)) if clerk_user_id else None
        if user:
            meta = user.accesibilidad if isinstance(user.accesibilidad, dict) else {}
            auth_meta = meta.get("auth") if isinstance(meta.get("auth"), dict) else {}
            clerk_meta = auth_meta.get("clerk") if isinstance(auth_meta.get("clerk"), dict) else {}
            clerk_meta["deleted_at"] = datetime.now(timezone.utc).isoformat()
            clerk_meta["disabled"] = True
            auth_meta["clerk"] = clerk_meta
            meta["auth"] = auth_meta
            user.accesibilidad = meta
            flag_modified(user, "accesibilidad")
            user.rol = ROLE_CLIENTE
            user.set_password(secrets.token_urlsafe(48))
            user.token = generate_token()
            user.entity_token = None
            user.password_reset_selector = None
            user.password_reset_verifier_hash = None
            user.password_reset_sent_at = None
            _revoke_chatboc_sessions_for_clerk_user(user, reason=event_type)
            db.session.add(user)
            db.session.commit()
            return {"status": "marked_deleted", "user_id": user.id, "event_type": event_type}
    return {"status": "ignored", "event_type": event_type}
