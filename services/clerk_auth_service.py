import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

import jwt
from flask import current_app
from jwt import PyJWKClient
from sqlalchemy.orm.attributes import flag_modified

from database import db
from models import TenantProfile, User, generate_token
from services.auth_notification_service import (
    send_onboarding_whatsapp,
    send_verification_email,
)
from services.logic import es_rubro_publico
from services.tenant_factory import create_tenant_from_template
from services.user_service import get_user_profile_identity, set_user_profile_avatar
from utils.auth_helpers import generar_token
from utils.roles import (
    ROLE_CLIENTE,
    ROLE_SUPERADMIN,
    is_authorized_superadmin_email,
    is_super_admin_role,
    normalize_tenant_type,
    role_for_tenant_type,
)

CLERK_AUTH_CONTRACT_VERSION = "auth.clerk.v1"
DEFAULT_SOCIAL_PROVIDERS = ("google", "facebook", "linkedin")


class ClerkAuthError(ValueError):
    pass


class ClerkNotConfigured(ClerkAuthError):
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


def _clerk_publishable_key() -> Optional[str]:
    value = (
        os.getenv("VITE_CLERK_PUBLISHABLE_KEY")
        or os.getenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY")
        or os.getenv("CLERK_PUBLISHABLE_KEY")
    )
    return str(value).strip() if value and str(value).strip() else None


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


def _clerk_configuration_warnings(*, publishable_key: Optional[str], verification_configured: bool) -> list[dict]:
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
    return warnings


def build_clerk_frontend_contract() -> dict:
    providers = _env_list("CLERK_SOCIAL_PROVIDERS", DEFAULT_SOCIAL_PROVIDERS)
    publishable_key = _clerk_publishable_key()
    verification_configured = _clerk_verification_configured()
    ui_enabled = bool(clerk_enabled() and publishable_key and verification_configured)
    return {
        "contract_version": CLERK_AUTH_CONTRACT_VERSION,
        "enabled": ui_enabled,
        "provider": "clerk",
        "session_sync_endpoint": "/auth/clerk/session",
        "onboarding_endpoint": "/auth/clerk/onboarding",
        "webhook_endpoint": "/auth/clerk/webhook",
        "publishable_key": publishable_key,
        "publishable_key_configured": bool(publishable_key),
        "issuer_configured": bool(_clerk_issuer()),
        "jwks_configured": verification_configured,
        "webhook_configured": bool(os.getenv("CLERK_WEBHOOK_SECRET")),
        "ready_for_session_sync": verification_configured,
        "configuration_warnings": _clerk_configuration_warnings(
            publishable_key=publishable_key,
            verification_configured=verification_configured,
        ),
        "social_providers": providers,
        "frontend_env": {
            "VITE_CLERK_PUBLISHABLE_KEY": "required for Vite frontend",
            "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY": "supported for Next.js frontends",
        },
        "backend_env": {
            "CLERK_ISSUER": "required unless CLERK_JWKS_URL is set",
            "CLERK_JWKS_URL": "optional",
            "CLERK_AUDIENCE": "optional",
            "CLERK_WEBHOOK_SECRET": "recommended",
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
        return jwt.decode(token, signing_key.key, **decode_kwargs)
    except jwt.PyJWTError as exc:
        raise ClerkAuthError("Invalid Clerk session token") from exc


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


def extract_clerk_identity(claims: dict, profile: Optional[dict] = None) -> dict:
    profile = profile or {}
    clerk_user_id = claims.get("sub") or profile.get("id") or profile.get("user_id")
    if not clerk_user_id:
        raise ClerkAuthError("Clerk user id is missing")

    email, profile_verified = _primary_email_from_profile(profile)
    if not email:
        email = (
            claims.get("email")
            or claims.get("primary_email_address")
            or claims.get("email_address")
        )
        email = str(email).strip().lower() if email else None

    first = profile.get("first_name") or claims.get("first_name")
    last = profile.get("last_name") or claims.get("last_name")
    name = (
        profile.get("full_name")
        or claims.get("name")
        or " ".join(part for part in [first, last] if part).strip()
        or profile.get("username")
        or claims.get("username")
        or (email.split("@")[0] if email else "Usuario Chatboc")
    )

    return {
        "clerk_user_id": str(clerk_user_id),
        "email": email,
        "name": str(name).strip() or "Usuario Chatboc",
        "phone": _phone_from_profile(profile),
        "email_verified": bool(
            profile_verified
            or claims.get("email_verified")
            or claims.get("email_verified_at")
            or claims.get("evt") == "email_verified"
        ),
        "social_providers": _providers_from_profile(profile, claims),
        "avatar_url": _avatar_url_from_profile(profile, claims),
        "created_at": profile.get("created_at") or claims.get("iat"),
        "updated_at": profile.get("updated_at"),
    }


def _find_user_by_clerk_id(clerk_user_id: str) -> Optional[User]:
    # JSON contains queries differ across SQLite/PostgreSQL. This bounded scan is
    # only a fallback for users whose session token lacks email.
    candidates = User.query.filter(User.accesibilidad.isnot(None)).limit(1000).all()
    for user in candidates:
        metadata = user.accesibilidad if isinstance(user.accesibilidad, dict) else {}
        auth_meta = metadata.get("auth") if isinstance(metadata.get("auth"), dict) else {}
        clerk_meta = auth_meta.get("clerk") if isinstance(auth_meta.get("clerk"), dict) else {}
        if clerk_meta.get("user_id") == clerk_user_id:
            return user
    return None


def _merge_clerk_metadata(user: User, identity: dict) -> None:
    meta = user.accesibilidad if isinstance(user.accesibilidad, dict) else {}
    auth_meta = meta.get("auth") if isinstance(meta.get("auth"), dict) else {}
    auth_meta["provider"] = "clerk"
    auth_meta["clerk"] = {
        "user_id": identity["clerk_user_id"],
        "email": identity.get("email"),
        "social_providers": identity.get("social_providers") or [],
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

    if is_clerk_superadmin_email(identity.get("email")):
        user.rol = ROLE_SUPERADMIN
        user.tipo_chat = user.tipo_chat or "plataforma"
        return

    if is_super_admin_role(getattr(user, "rol", None)):
        user.rol = _fallback_role_for_clerk_user(user)


def upsert_user_from_clerk(claims: dict, profile: Optional[dict] = None) -> User:
    identity = extract_clerk_identity(claims, profile)
    email = identity.get("email")
    user = User.query.filter_by(email=email).first() if email else None
    if user is None:
        user = _find_user_by_clerk_id(identity["clerk_user_id"])

    if user is None:
        if not email:
            raise ClerkAuthError("Clerk user does not include an email address")
        user = User(
            name=identity["name"],
            email=email,
            token=generate_token(),
            rol="admin",
            plan="gratis",
            acepto_terminos=True,
            fecha_aceptacion_terminos=datetime.now(timezone.utc),
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


def _session_identity_payload(user: User) -> dict:
    identity = get_user_profile_identity(user)
    avatar_url = identity.get("avatar_url")
    avatar_consent = bool(identity.get("avatar_consent"))
    return {
        **identity,
        "picture": avatar_url,
        "profile_picture_consent": avatar_consent,
    }


def build_chatboc_session_payload(user: User, tenant: Optional[TenantProfile] = None) -> dict:
    tenant = tenant or tenant_for_user(user)
    token = generar_token(
        user.id,
        user.rol,
        user.tipo_chat,
        user.municipio_id,
        user.pyme_id,
    )
    onboarding = build_onboarding_contract(user, tenant)
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


def build_onboarding_contract(user: User, tenant: Optional[TenantProfile] = None) -> dict:
    if is_super_admin_role(getattr(user, "rol", None)):
        return {
            "required": False,
            "status": "platform_admin",
            "title": "Acceso superadmin activo",
            "description": "Tu cuenta Clerk esta vinculada al panel global de Chatboc.",
            "submit_endpoint": "/auth/clerk/onboarding",
            "modal": {"steps": [], "vertical_options": [], "goal_options": []},
        }

    tenant = tenant or tenant_for_user(user)
    return {
        "required": tenant is None,
        "status": "complete" if tenant else "pending",
        "title": "Completa tu espacio Chatboc",
        "description": "Con estos datos creamos el tenant, plantilla inicial, CRM y canales.",
        "submit_endpoint": "/auth/clerk/onboarding",
        "modal": {
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
            ],
            "social_login": {
                "provider": "clerk",
                "enabled_providers": _env_list("CLERK_SOCIAL_PROVIDERS", DEFAULT_SOCIAL_PROVIDERS),
                "required_dashboard_setup": ["facebook", "linkedin"],
            },
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


def _unique_tenant_slug(base: str) -> str:
    slug = _slugify(base) or f"tenant-{secrets.token_hex(3)}"
    candidate = slug
    suffix = 2
    while TenantProfile.query.filter_by(slug=candidate).first():
        candidate = f"{slug}-{suffix}"
        suffix += 1
    return candidate


def complete_clerk_onboarding(user: User, payload: dict) -> TenantProfile:
    existing = tenant_for_user(user)
    if existing:
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
    requested_slug = payload.get("tenant_slug") or payload.get("slug") or tenant_name
    slug = _unique_tenant_slug(str(requested_slug))
    plan = str(payload.get("plan") or "gratis").strip().lower()
    plan_for_factory = "full" if plan in {"full", "pro", "premium"} else "free"

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

    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    cfg["auth"] = {
        **(cfg.get("auth") if isinstance(cfg.get("auth"), dict) else {}),
        "provider": "clerk",
        "social_providers": _env_list("CLERK_SOCIAL_PROVIDERS", DEFAULT_SOCIAL_PROVIDERS),
    }
    cfg["onboarding"] = {
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "source": "clerk",
        "rubro": payload.get("rubro"),
        "primary_goal": payload.get("primary_goal"),
        "team_size": payload.get("team_size"),
        "preferred_channels": payload.get("preferred_channels") or [],
    }
    tenant.configuracion = cfg
    tenant.vertical = tenant.vertical or str(payload.get("vertical") or tenant_type)
    tenant.subvertical = tenant.subvertical or (str(payload.get("rubro")) if payload.get("rubro") else None)
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
    secret = os.getenv("CLERK_WEBHOOK_SECRET")
    if not secret:
        raise ClerkNotConfigured("CLERK_WEBHOOK_SECRET is required")

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


def sync_clerk_webhook_event(event: dict) -> dict:
    event_type = event.get("type") or ""
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if event_type in {"user.created", "user.updated"}:
        claims = {"sub": data.get("id")}
        user = upsert_user_from_clerk(claims, data)
        db.session.commit()
        return {"status": "synced", "user_id": user.id, "event_type": event_type}
    if event_type == "user.deleted":
        clerk_user_id = data.get("id")
        user = _find_user_by_clerk_id(str(clerk_user_id)) if clerk_user_id else None
        if user:
            meta = user.accesibilidad if isinstance(user.accesibilidad, dict) else {}
            auth_meta = meta.get("auth") if isinstance(meta.get("auth"), dict) else {}
            clerk_meta = auth_meta.get("clerk") if isinstance(auth_meta.get("clerk"), dict) else {}
            clerk_meta["deleted_at"] = datetime.now(timezone.utc).isoformat()
            auth_meta["clerk"] = clerk_meta
            meta["auth"] = auth_meta
            user.accesibilidad = meta
            flag_modified(user, "accesibilidad")
            db.session.add(user)
            db.session.commit()
            return {"status": "marked_deleted", "user_id": user.id, "event_type": event_type}
    return {"status": "ignored", "event_type": event_type}
