from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any, Mapping

from models import (
    CatalogoItem,
    CatalogUpload,
    CategoriaTicket,
    EncEncuesta,
    MessageTemplateRegistry,
    PublicSurvey,
    TenantProfile,
    User,
)
from services.commerce_contracts import payment_capabilities
from services.live_chat_schedule import build_live_chat_status
from services.plan_access import integration_access_payload
from services.tenant_implementation_journey import build_implementation_journey
from services.organization_setup_journey import build_organization_setup_journey
from services.organization_branding import build_workspace_appearance
from services.plan_access import tenant_allows_workspace_branding
from services.twilio_tech_provider import STATE_KEY
from utils.roles import superadmin_email_allowlist_configured


CONTRACT_VERSION = "tenant.channel_activation.v1"
READY_STATES = {"ready", "online", "connected", "approved", "active", "enabled"}
LOCKED_STATES = {"locked", "blocked", "plan_required", "needs_platform_config"}


def _cfg(tenant: TenantProfile | None) -> dict[str, Any]:
    value = getattr(tenant, "configuracion", None) if tenant is not None else None
    return value if isinstance(value, dict) else {}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _env_value(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value and str(value).strip():
            return str(value).strip()
    return None


def _truthy_env(name: str) -> bool:
    value = str(os.getenv(name) or "").strip().lower()
    return value in {"1", "true", "yes", "y", "on"}


def _setting_value(*names: str) -> str | None:
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            for name in names:
                value = current_app.config.get(name)
                if value and str(value).strip():
                    return str(value).strip()
    except Exception:
        pass
    return _env_value(*names)


def _truthy_setting(name: str) -> bool:
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            value = current_app.config.get(name)
            if value is not None:
                return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        pass
    return _truthy_env(name)


def _clerk_environment_from_key(value: str | None) -> str:
    key = str(value or "").strip()
    if key.startswith("pk_live_"):
        return "production"
    if key.startswith("pk_test_"):
        return "development"
    if key:
        return "unknown"
    return "unconfigured"


def _safe_count(query: Any) -> int:
    try:
        return int(query.count())
    except Exception:
        return 0


def _tenant_ref(tenant: TenantProfile | None) -> dict[str, Any] | None:
    if tenant is None:
        return None
    return {
        "id": tenant.id,
        "slug": tenant.slug,
        "nombre": tenant.nombre,
        "tipo": tenant.tipo,
        "vertical": tenant.vertical,
        "subvertical": tenant.subvertical,
        "plan": tenant.plan,
        "is_active": bool(getattr(tenant, "is_active", True)),
    }


def _tenant_path(tenant: TenantProfile | None, path: str) -> str:
    slug = getattr(tenant, "slug", None)
    if slug:
        clean = path if path.startswith("/") else f"/{path}"
        return f"/t/{slug}{clean}"
    return path if path.startswith("/") else f"/{path}"


def _profile_path(tab: str) -> str:
    return "/perfil" if tab == "perfil" else f"/perfil?tab={tab}"


def _action(action_id: str, label: str, href: str, *, kind: str = "link", primary: bool = False) -> dict[str, Any]:
    return {"id": action_id, "label": label, "href": href, "kind": kind, "primary": primary}


def _channel(
    channel_id: str,
    label: str,
    status: str,
    description: str,
    *,
    actions: list[dict[str, Any]] | None = None,
    evidence: list[str] | None = None,
    reason_code: str | None = None,
    required_plan: str | None = None,
    progress_hint: str | None = None,
) -> dict[str, Any]:
    return {
        "id": channel_id,
        "label": label,
        "status": status,
        "state": status,
        "ready": status in READY_STATES,
        "locked": status in LOCKED_STATES,
        "description": description,
        "evidence": [item for item in (evidence or []) if item],
        "actions": actions or [],
        "reason_code": reason_code,
        "required_plan": required_plan,
        "progress_hint": progress_hint,
    }


def _whatsapp_status(cfg: Mapping[str, Any], access_enabled: bool) -> tuple[str, list[str], str | None]:
    onboarding = _as_mapping(cfg.get("whatsapp_onboarding"))
    state = _as_mapping(cfg.get(STATE_KEY))
    raw_status = str(onboarding.get("status") or "").strip().lower()
    sender_status = str(state.get("sender_status") or "").strip().lower()

    evidence: list[str] = []
    if onboarding.get("provider"):
        evidence.append(f"provider:{onboarding.get('provider')}")
    if sender_status:
        evidence.append(f"sender:{sender_status}")

    if not access_enabled:
        return "locked", evidence, "plan_full_required"
    if sender_status in READY_STATES or raw_status in READY_STATES:
        return "ready", evidence, None
    if raw_status == "sender_registered":
        return "pending", evidence, "sender_not_online"
    if raw_status == "pending_sender_registration":
        return "action_required", evidence, "register_sender"
    if raw_status in {"ready_for_embedded_signup", "plan_ready"}:
        return "action_required", evidence, raw_status
    if raw_status == "provisioning_started":
        return "pending", evidence, raw_status
    if raw_status in {"needs_platform_config", "disabled"}:
        return "blocked", evidence, raw_status
    return "action_required", evidence, "connect_whatsapp"


def _whatsapp_actions(
    tenant: TenantProfile | None,
    cfg: Mapping[str, Any],
    status: str,
    reason_code: str | None,
    access_enabled: bool,
) -> list[dict[str, Any]]:
    tenant_slug = getattr(tenant, "slug", "") or ""
    onboarding = _as_mapping(cfg.get("whatsapp_onboarding"))
    connect = _as_mapping(onboarding.get("connect"))

    def api_endpoint(name: str, fallback_suffix: str) -> str:
        configured = str(connect.get(name) or "").strip()
        if configured:
            return configured
        return f"/api/v2/tenants/{tenant_slug}/whatsapp/tech-provider/{fallback_suffix}"

    setup_action = _action(
        "open_whatsapp_setup",
        "Abrir panel WhatsApp",
        _tenant_path(tenant, "/integracion?channel=whatsapp"),
        primary=True,
    )
    if not access_enabled:
        return [
            _action(
                "upgrade_whatsapp",
                "Activar plan para WhatsApp",
                _tenant_path(tenant, "/integracion?channel=whatsapp"),
                primary=True,
            )
        ]
    if reason_code == "register_sender":
        return [
            _action(
                "open_register_sender",
                "Registrar sender",
                _tenant_path(tenant, "/integracion?channel=whatsapp&action=register-sender"),
                primary=True,
            ),
            _action(
                "register_sender_api",
                "Ejecutar registro sender",
                api_endpoint("register_sender_endpoint", "register-sender"),
                kind="api",
            ),
        ]
    if reason_code == "sender_not_online":
        return [
            _action(
                "open_sender_status",
                "Revisar aprobacion del sender",
                _tenant_path(tenant, "/integracion?channel=whatsapp&action=sender-status"),
                primary=True,
            ),
            _action(
                "poll_sender_status",
                "Actualizar estado sender",
                api_endpoint("sender_status_endpoint", "sender-status"),
                kind="api",
            ),
        ]
    if reason_code in {"ready_for_embedded_signup", "plan_ready", "connect_whatsapp"}:
        return [
            _action(
                "open_embedded_signup",
                "Conectar WhatsApp",
                _tenant_path(tenant, "/integracion?channel=whatsapp&action=embedded-signup"),
                primary=True,
            )
        ]
    if status == "ready":
        return [
            _action(
                "open_whatsapp_setup",
                "Ver WhatsApp",
                _tenant_path(tenant, "/integracion?channel=whatsapp"),
                primary=True,
            ),
            _action(
                "run_sandbox",
                "Probar sandbox",
                f"/api/v2/tenants/{tenant_slug}/whatsapp/sandbox-test",
                kind="api",
            ),
        ]
    return [setup_action]


def _live_chat_status(cfg: Mapping[str, Any]) -> tuple[str, list[str]]:
    schedule_cfg = _as_mapping(cfg.get("live_chat_schedule"))
    if not schedule_cfg:
        return "action_required", ["horario global disponible"]
    try:
        status = build_live_chat_status(schedule_override=schedule_cfg)
    except Exception:
        return "blocked", ["horario invalido"]
    if status.get("enabled") is False:
        return "blocked", ["atencion humana deshabilitada"]
    return "ready", [
        f"{status.get('label') or 'horario configurado'}",
        str(status.get("timezone") or schedule_cfg.get("timezone") or "").strip(),
    ]


def _payment_status(tenant: TenantProfile | None, access_enabled: bool) -> tuple[str, list[str], str | None]:
    if tenant is None:
        return "blocked", [], "tenant_missing"
    try:
        payment = payment_capabilities(tenant)
    except Exception:
        return "blocked", [], "payment_contract_unavailable"

    evidence = [f"gateway:{payment.get('gateway') or 'mercadopago'}"]
    if payment.get("gateway_configured") or payment.get("mercadopago_ready"):
        evidence.append("gateway configurado")
    missing = payment.get("missing") if isinstance(payment.get("missing"), list) else []
    evidence.extend(f"faltante:{item}" for item in missing[:3] if item)

    if not access_enabled:
        return "locked", evidence, "plan_full_required"
    if payment.get("payment_ready"):
        return "ready", evidence, None
    if not payment.get("gateway_configured"):
        return "action_required", evidence, "payment_gateway_not_configured"
    return "pending", evidence, "payment_verification_pending"


def _identity_auth_status(
    tenant: TenantProfile | None,
    cfg: Mapping[str, Any],
) -> tuple[str, list[str], str | None, str | None]:
    """Summarize whether tenant identity, portal login and Clerk are production-ready.

    This intentionally returns a secret-free operational view. It only exposes
    which pieces are configured, never the configured key or secret values.
    """

    auth_cfg = _as_mapping(cfg.get("auth"))
    onboarding = _as_mapping(cfg.get("onboarding"))
    provider = str(auth_cfg.get("provider") or "").strip().lower()
    linked_to_clerk = provider == "clerk" or str(onboarding.get("source") or "").strip().lower() == "clerk"
    publishable_key = _env_value("VITE_CLERK_PUBLISHABLE_KEY", "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "CLERK_PUBLISHABLE_KEY")
    publishable = bool(publishable_key)
    clerk_environment = _clerk_environment_from_key(publishable_key)
    jwt_verification = bool(_env_value("CLERK_JWKS_URL", "CLERK_ISSUER", "CLERK_JWT_ISSUER", "NEXT_PUBLIC_CLERK_FRONTEND_API"))
    webhook = bool(_env_value("CLERK_WEBHOOK_SIGNING_SECRET", "CLERK_WEBHOOK_SECRET"))
    enabled = not _truthy_env("CLERK_DISABLED") and (
        _truthy_env("CLERK_ENABLED") or publishable or jwt_verification
    )

    evidence: list[str] = []
    if linked_to_clerk:
        evidence.append("tenant vinculado a Clerk")
    elif getattr(tenant, "id", None):
        evidence.append("tenant usa auth legacy")
    if publishable:
        evidence.append("publishable key configurada")
        evidence.append(f"entorno Clerk:{clerk_environment}")
    if jwt_verification:
        evidence.append("JWT/JWKS configurado")
    if webhook:
        evidence.append("webhook Clerk configurado")
    if superadmin_email_allowlist_configured():
        evidence.append("allowlist superadmin explicita")
    else:
        evidence.append("allowlist superadmin default")

    missing_critical: list[str] = []
    if not enabled:
        missing_critical.append("CLERK_ENABLED")
    if not publishable:
        missing_critical.append("VITE_CLERK_PUBLISHABLE_KEY")
    if not jwt_verification:
        missing_critical.append("CLERK_ISSUER o CLERK_JWKS_URL")

    if missing_critical:
        return (
            "blocked",
            evidence,
            "clerk_config_missing",
            f"Completar configuracion Clerk: {', '.join(missing_critical)}.",
        )
    if clerk_environment != "production":
        return (
            "blocked",
            evidence,
            "clerk_production_keys_missing",
            "Clerk esta en development/test. Pasar la app Chatboc a produccion, configurar claves pk_live/sk_live, issuer/JWKS productivo y dominio en Cloudflare.",
        )
    if not linked_to_clerk:
        return (
            "action_required",
            evidence,
            "tenant_auth_not_linked",
            "Vincular el tenant al onboarding Clerk o mantenerlo como auth legacy hasta migrarlo.",
        )
    if not webhook:
        return (
            "pending",
            evidence,
            "clerk_webhook_recommended",
            "Configurar CLERK_WEBHOOK_SIGNING_SECRET para sincronizar altas, bajas, avatar social consentido y cambios de email.",
        )
    return "ready", evidence, None, "Login social, portal y guardrail superadmin listos para operar."


def _public_intake_security_status() -> tuple[str, list[str], str | None, str | None]:
    """Secret-free readiness for anonymous marketplace and survey protection."""

    site_key = bool(
        _setting_value(
            "VITE_CLOUDFLARE_TURNSTILE_SITE_KEY",
            "NEXT_PUBLIC_CLOUDFLARE_TURNSTILE_SITE_KEY",
            "CLOUDFLARE_TURNSTILE_SITE_KEY",
        )
    )
    secret = bool(_setting_value("CLOUDFLARE_TURNSTILE_SECRET_KEY", "TURNSTILE_SECRET_KEY"))
    enforced = _truthy_setting("CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE")

    evidence = ["protege marketplace asistido", "protege encuestas publicas"]
    if site_key:
        evidence.append("site key publica configurada")
    if secret:
        evidence.append("secret backend configurado")
    evidence.append("enforcement activo" if enforced else "enforcement pendiente")

    missing: list[str] = []
    if not site_key:
        missing.append("VITE_CLOUDFLARE_TURNSTILE_SITE_KEY")
    if not secret:
        missing.append("CLOUDFLARE_TURNSTILE_SECRET_KEY")

    if enforced and missing:
        return (
            "blocked",
            evidence,
            "turnstile_enforced_missing_config",
            f"Enforcement activo sin configuracion completa: {', '.join(missing)}.",
        )
    if missing:
        return (
            "action_required",
            evidence,
            "turnstile_config_missing",
            f"Configurar Cloudflare Turnstile antes de exigir desafio publico: {', '.join(missing)}.",
        )
    if not enforced:
        return (
            "pending",
            evidence,
            "turnstile_enforcement_pending",
            "Site key y secret estan configurados. Activar enforcement despues del deploy frontend/backend.",
        )
    return "ready", evidence, None, "Cargas anonimas protegidas con Cloudflare Turnstile."


def _counts(tenant: TenantProfile | None) -> dict[str, int]:
    if tenant is None or not getattr(tenant, "id", None):
        return {
            "catalog_items": 0,
            "catalog_imports_committed": 0,
            "approved_templates": 0,
            "surveys": 0,
            "team_members": 0,
            "ticket_categories": 0,
            "routed_team_members": 0,
        }
    return {
        "catalog_items": _safe_count(CatalogoItem.query.filter_by(tenant_id=tenant.id)),
        "catalog_imports_committed": _safe_count(
            CatalogUpload.query.filter_by(tenant_id=tenant.id, status="committed")
        ),
        "approved_templates": _safe_count(
            MessageTemplateRegistry.query.filter_by(tenant_id=tenant.id, channel="whatsapp", status="approved")
        ),
        "surveys": _safe_count(EncEncuesta.query.filter_by(tenant_id=tenant.id))
        + _safe_count(PublicSurvey.query.filter_by(tenant_id=tenant.id)),
        "team_members": _safe_count(User.query.filter_by(tenant_id=tenant.id, es_empleado=True)),
        "ticket_categories": _safe_count(
            CategoriaTicket.query.filter_by(tenant_id=tenant.id, tipo="ticket")
        ),
        "routed_team_members": _safe_count(
            User.query.join(User.categorias_ticket)
            .filter(
                User.tenant_id == tenant.id,
                User.es_empleado.is_(True),
                CategoriaTicket.tenant_id == tenant.id,
                CategoriaTicket.tipo == "ticket",
            )
            .distinct()
        ),
    }


def _mesa_unica_status(
    counts: Mapping[str, int],
) -> tuple[str, list[str], str | None, str]:
    category_count = max(0, int(counts.get("ticket_categories") or 0))
    routed_count = max(0, int(counts.get("routed_team_members") or 0))
    evidence: list[str] = []
    if category_count:
        evidence.append(f"{category_count} categorias operativas")
    if routed_count:
        evidence.append(f"{routed_count} responsables con enrutamiento")

    if not category_count:
        return (
            "action_required",
            evidence,
            "mesa_unica_categories_required",
            "Preparar las categorias institucionales antes de habilitar la Mesa Unica.",
        )
    if not routed_count:
        return (
            "pending",
            evidence,
            "mesa_unica_routing_required",
            "Las categorias estan preparadas; falta asignar responsables para operar casos reales.",
        )
    return (
        "ready",
        evidence,
        None,
        "Mesa Unica preparada con categorias y responsables enrutados.",
    )


def _team_routing_status(
    counts: Mapping[str, int],
) -> tuple[str, list[str], str | None, str]:
    member_count = max(0, int(counts.get("team_members") or 0))
    routed_count = max(0, int(counts.get("routed_team_members") or 0))
    evidence: list[str] = []
    if member_count:
        evidence.append(f"{member_count} operadores")
    if routed_count:
        evidence.append(f"{routed_count} con categorias asignadas")

    if not member_count:
        return (
            "action_required",
            evidence,
            "team_required",
            "Agregar operadores y responsables antes de recibir casos reales.",
        )
    if not routed_count:
        return (
            "pending",
            evidence,
            "team_category_routing_required",
            "El equipo existe, pero falta asignar categorias operativas a sus responsables.",
        )
    return (
        "ready",
        evidence,
        None,
        "Equipo operativo con categorias y responsables asignados.",
    )


def _knowledge_content_status(counts: Mapping[str, int]) -> tuple[str, list[str], str, str]:
    """Keep content presence separate from verified retrieval readiness."""

    item_count = max(0, int(counts.get("catalog_items") or 0))
    committed_imports = max(0, int(counts.get("catalog_imports_committed") or 0))
    evidence: list[str] = []
    if item_count:
        evidence.append(f"{item_count} contenidos estructurados")
    if committed_imports:
        evidence.append(f"{committed_imports} importaciones confirmadas")

    if not item_count:
        return (
            "action_required",
            evidence,
            "knowledge_content_required",
            "Cargar tramites, servicios o documentos institucionales y revisar su procedencia.",
        )
    return (
        "pending",
        evidence,
        "knowledge_retrieval_verification_required",
        "El contenido existe, pero falta un recibo verificable de indexacion y una prueba de recuperacion antes de declararlo listo.",
    )


def _institutional_branding_status(
    tenant: TenantProfile | None,
) -> tuple[str, list[str], str | None, str]:
    """Report explicit tenant branding without treating platform defaults as setup."""

    if tenant is None:
        return "blocked", [], "tenant_missing", "Crear el tenant antes de configurar su identidad visual."

    logo_configured = bool(str(getattr(tenant, "logo_url", None) or "").strip())
    palette_configured = bool(
        isinstance(getattr(tenant, "tema", None), Mapping)
        and getattr(tenant, "tema", None)
    ) or bool(
        isinstance(getattr(tenant, "theme_json", None), Mapping)
        and getattr(tenant, "theme_json", None)
    )
    domain_configured = bool(str(getattr(tenant, "dominio", None) or "").strip())

    evidence: list[str] = []
    if logo_configured:
        evidence.append("logo institucional configurado")
    if palette_configured:
        evidence.append("paleta institucional configurada")
    if domain_configured:
        evidence.append("dominio institucional registrado")

    missing: list[str] = []
    if not logo_configured:
        missing.append("logo")
    if not palette_configured:
        missing.append("paleta")
    if missing:
        return (
            "action_required",
            evidence,
            "institutional_branding_incomplete",
            f"Completar identidad institucional: {', '.join(missing)}. El dominio propio puede incorporarse en una etapa posterior.",
        )
    return (
        "ready",
        evidence,
        None,
        "Identidad institucional lista; el dominio propio es opcional para operar sobre Chatboc.ar.",
    )


def _accessibility_status(
    cfg: Mapping[str, Any],
) -> tuple[str, list[str], str | None, str]:
    """Require an explicit tenant policy instead of inferring readiness globally."""

    accessibility = _as_mapping(cfg.get("accessibility"))
    if not accessibility:
        return (
            "action_required",
            ["componentes accesibles disponibles en plataforma"],
            "accessibility_policy_required",
            "Definir la politica del tenant: lectura, contraste, navegacion por teclado, lenguaje claro y derivacion humana.",
        )

    if accessibility.get("enabled") is False:
        return (
            "blocked",
            ["politica del tenant deshabilitada"],
            "accessibility_disabled",
            "Habilitar la politica de accesibilidad antes de publicar los canales ciudadanos.",
        )

    configured_features = accessibility.get("features")
    feature_count = len(configured_features) if isinstance(configured_features, list) else 0
    evidence = ["politica del tenant configurada"]
    if feature_count:
        evidence.append(f"{feature_count} apoyos declarados")
    if accessibility.get("human_handoff") is True:
        evidence.append("derivacion humana declarada")

    if accessibility.get("enabled") is True and feature_count > 0:
        return (
            "ready",
            evidence,
            None,
            "Politica accesible declarada. La validacion con usuarios y dispositivos sigue siendo un gate de publicacion.",
        )
    return (
        "pending",
        evidence,
        "accessibility_validation_pending",
        "Completar apoyos accesibles y validar la experiencia con usuarios y dispositivos antes de publicar.",
    )


def _territorial_status(
    tenant: TenantProfile | None,
) -> tuple[str, list[str], str | None, str]:
    """Only accept the server-owned jurisdiction attestation as territorial readiness."""

    if tenant is None:
        return "blocked", [], "tenant_missing", "Crear el tenant antes de configurar su jurisdiccion."

    status = str(getattr(tenant, "jurisdiction_status", None) or "unverified").strip().lower()
    if status == "not_applicable":
        return (
            "ready",
            ["jurisdiccion no requerida para este tenant"],
            None,
            "El tenant declaro que no necesita agregacion territorial oficial.",
        )

    verified = bool(
        status == "verified"
        and str(getattr(tenant, "jurisdiction_ref", None) or "").strip()
        and str(getattr(tenant, "jurisdiction_evidence_ref", None) or "").strip()
        and getattr(tenant, "jurisdiction_verified_by_user_id", None)
        and getattr(tenant, "jurisdiction_verified_at", None)
    )
    if verified:
        return (
            "ready",
            ["jurisdiccion oficial verificada", "evidencia institucional registrada"],
            None,
            "La delimitacion oficial esta lista para validar puntos, zonas y agregaciones.",
        )
    return (
        "action_required",
        [f"estado:{status}"],
        "official_jurisdiction_required",
        "Registrar y aprobar limites oficiales antes de publicar rankings, tasas o comparaciones por zona.",
    )


def build_channel_activation_payload(tenant: TenantProfile | None, *, actor=None) -> dict[str, Any]:
    """Build a public, secret-free checklist for post-onboarding channel activation."""

    cfg = _cfg(tenant)
    access = integration_access_payload(tenant)
    access_enabled = bool(access.get("enabled"))
    counts = _counts(tenant)
    onboarding = _as_mapping(cfg.get("onboarding"))
    provisioning = _as_mapping(cfg.get("provisioning"))
    preferred_channels = onboarding.get("preferred_channels") if isinstance(onboarding.get("preferred_channels"), list) else []
    identity_status, identity_evidence, identity_reason, identity_hint = _identity_auth_status(tenant, cfg)
    whatsapp_status, whatsapp_evidence, whatsapp_reason = _whatsapp_status(cfg, access_enabled)
    live_status, live_evidence = _live_chat_status(cfg)
    payment_status, payment_evidence, payment_reason = _payment_status(tenant, access_enabled)
    public_security_status, public_security_evidence, public_security_reason, public_security_hint = (
        _public_intake_security_status()
    )
    branding_status, branding_evidence, branding_reason, branding_hint = _institutional_branding_status(tenant)
    accessibility_status, accessibility_evidence, accessibility_reason, accessibility_hint = _accessibility_status(cfg)
    territory_status, territory_evidence, territory_reason, territory_hint = _territorial_status(tenant)
    knowledge_status, knowledge_evidence, knowledge_reason, knowledge_hint = _knowledge_content_status(counts)
    crm_status, crm_evidence, crm_reason, crm_hint = _mesa_unica_status(counts)
    team_status, team_evidence, team_reason, team_hint = _team_routing_status(counts)

    widget_configured = bool(
        getattr(tenant, "widget_settings", None)
        or getattr(tenant, "widget_config", None)
        or cfg.get("widget_tokens")
    )
    widget_status = "locked" if not access_enabled else ("ready" if widget_configured else "action_required")
    template_status = "locked" if not access_enabled else ("ready" if counts["approved_templates"] > 0 else "action_required")
    catalog_status = "ready" if counts["catalog_items"] > 0 else "action_required"
    analytics_status = "ready" if counts["surveys"] > 0 else "action_required"

    channels = [
        _channel(
            "institutional_branding",
            "Identidad institucional",
            branding_status,
            "Logo, paleta y dominio opcional para una experiencia white-label gobernada por tenant.",
            actions=[_action("open_branding", "Configurar identidad", _profile_path("perfil"), primary=True)],
            evidence=branding_evidence,
            reason_code=branding_reason,
            progress_hint=branding_hint,
        ),
        _channel(
            "accessibility",
            "Accesibilidad e inclusion",
            accessibility_status,
            "Politica accesible para web, WhatsApp, formularios y derivacion humana.",
            actions=[_action("open_accessibility", "Configurar accesibilidad", _profile_path("perfil"), primary=True)],
            evidence=accessibility_evidence,
            reason_code=accessibility_reason,
            progress_hint=accessibility_hint,
        ),
        _channel(
            "territorial_intelligence",
            "Territorio y mapas",
            territory_status,
            "Jurisdiccion oficial para validar direcciones, zonas, categorias y mapas de calor.",
            actions=[_action("open_territory", "Configurar territorio", _profile_path("mapas"), primary=True)],
            evidence=territory_evidence,
            reason_code=territory_reason,
            progress_hint=territory_hint,
        ),
        _channel(
            "crm",
            "Mesa Unica y CRM operativo",
            crm_status,
            "Bandeja de reclamos, pedidos y conversaciones con categorias y responsables verificables.",
            actions=[
                _action(
                    "prepare_service_desk" if crm_reason == "mesa_unica_categories_required" else "open_crm",
                    "Preparar Mesa Unica" if crm_reason == "mesa_unica_categories_required" else "Abrir reclamos/tickets",
                    _profile_path("categorias") if crm_reason == "mesa_unica_categories_required" else _profile_path("tickets"),
                    primary=True,
                )
            ],
            evidence=crm_evidence,
            reason_code=crm_reason,
            progress_hint=crm_hint,
        ),
        _channel(
            "identity_auth",
            "Identidad y login social",
            identity_status,
            "Portal de usuario, login social con Clerk, avatar consentido y guardrail superadmin por email.",
            actions=[
                _action("open_profile", "Abrir perfil", _profile_path("perfil"), primary=identity_status != "ready"),
                _action("open_user_portal", "Ver portal usuario", _tenant_path(tenant, "/portal")),
            ],
            evidence=identity_evidence,
            reason_code=identity_reason,
            progress_hint=identity_hint,
        ),
        _channel(
            "whatsapp",
            "WhatsApp Business",
            whatsapp_status,
            "Sender productivo, proveedor Twilio/Meta y pruebas de conversacion.",
            actions=_whatsapp_actions(tenant, cfg, whatsapp_status, whatsapp_reason, access_enabled),
            evidence=whatsapp_evidence,
            reason_code=whatsapp_reason,
            required_plan=None if access_enabled else "full",
        ),
        _channel(
            "widget",
            "Widget web",
            widget_status,
            "Chat embebible para sitios, landing pages y portales de usuario.",
            actions=[_action("open_integrations", "Configurar widget", _tenant_path(tenant, "/integracion"), primary=True)],
            evidence=["widget token listo"] if widget_configured else [],
            reason_code=None if access_enabled else "plan_full_required",
            required_plan=None if access_enabled else "full",
        ),
        _channel(
            "public_intake_security",
            "Proteccion publica",
            public_security_status,
            "Cloudflare Turnstile para cargas anonimas de marketplace, reclamos asistidos y encuestas.",
            actions=[_action("configure_turnstile", "Configurar Cloudflare", _tenant_path(tenant, "/integracion"), primary=True)],
            evidence=public_security_evidence,
            reason_code=public_security_reason,
            progress_hint=public_security_hint,
        ),
        _channel(
            "templates",
            "Plantillas aprobadas",
            template_status,
            "Mensajes transaccionales, menus accesibles y webviews aprobados para WhatsApp.",
            actions=[_action("manage_templates", "Gestionar plantillas", "/perfil/plantillas-respuesta", primary=True)],
            evidence=[f"{counts['approved_templates']} aprobadas"] if counts["approved_templates"] else [],
            reason_code=None if access_enabled else "plan_full_required",
            required_plan=None if access_enabled else "full",
        ),
        _channel(
            "catalog_marketplace",
            "Catalogo y marketplace",
            catalog_status,
            "Productos, tramites, promociones y pedidos asistidos por IA desde WhatsApp o web.",
            actions=[_action("open_catalog", "Cargar catalogo", _profile_path("catalogo"), primary=True)],
            evidence=[f"{counts['catalog_items']} items"] if counts["catalog_items"] else [],
        ),
        _channel(
            "knowledge_content",
            "Conocimiento institucional",
            knowledge_status,
            "Tramites, servicios y documentos con procedencia, indexacion y recuperacion verificables.",
            actions=[_action("open_knowledge", "Gestionar contenidos", _profile_path("catalogo"), primary=True)],
            evidence=knowledge_evidence,
            reason_code=knowledge_reason,
            progress_hint=knowledge_hint,
        ),
        _channel(
            "payments_checkout",
            "Cobros y checkout",
            payment_status,
            "Links de pago, webviews seguros y confirmacion por webhook para pedidos, cuotas y comprobantes.",
            actions=[_action("configure_payments", "Configurar cobros", _tenant_path(tenant, "/integracion"), primary=True)],
            evidence=payment_evidence,
            reason_code=payment_reason,
            required_plan=None if access_enabled else "full",
        ),
        _channel(
            "team_routing",
            "Equipo y responsables",
            team_status,
            "Operadores, permisos y categorias para que reclamos, pedidos y chats no queden sin responsable.",
            actions=[_action("open_team", "Configurar equipo", _profile_path("empleados"), primary=True)],
            evidence=team_evidence,
            reason_code=team_reason,
            progress_hint=team_hint,
        ),
        _channel(
            "live_chat",
            "Atencion humana",
            live_status,
            "Horario, cola offline y derivacion a operadores para tickets sensibles.",
            actions=[_action("configure_live_chat", "Configurar horario", _tenant_path(tenant, "/integracion"), primary=True)],
            evidence=live_evidence,
            reason_code=None if live_status == "ready" else "schedule_required",
        ),
        _channel(
            "analytics_surveys",
            "Encuestas y analitica",
            analytics_status,
            "Encuestas, votaciones, reportes y mapas de calor para decisiones operativas.",
            actions=[_action("open_analytics", "Abrir analitica", _profile_path("analytics"), primary=True)],
            evidence=[f"{counts['surveys']} encuestas/votaciones"] if counts["surveys"] else [],
        ),
    ]

    ready_count = sum(1 for item in channels if item["ready"])
    locked_count = sum(1 for item in channels if item["locked"])
    attention_count = len(channels) - ready_count - locked_count
    progress = round((ready_count / max(1, len(channels))) * 100)
    first_actionable = next((item for item in channels if not item["ready"] and item["actions"]), None)
    implementation_journey = build_implementation_journey(channels)
    blockers = [
        {
            "id": item["id"],
            "label": item["label"],
            "reason_code": item.get("reason_code") or item.get("required_plan") or "action_required",
            "required_plan": item.get("required_plan"),
        }
        for item in channels
        if item["locked"] or item.get("reason_code") in {"plan_full_required", "needs_platform_config"}
    ]

    from services.tenant_conversation_guide import guide_access_descriptor
    from utils.tenant_admin_access import can_manage_tenant_control_plane
    guide = guide_access_descriptor(tenant, can_read=can_manage_tenant_control_plane(actor, tenant))

    return {
        "contract_version": CONTRACT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant": _tenant_ref(tenant),
        "status": "ready" if ready_count == len(channels) else ("locked" if locked_count else "needs_attention"),
        "summary": {
            "total": len(channels),
            "ready": ready_count,
            "locked": locked_count,
            "attention": attention_count,
            "progress": progress,
            "primary_next_action": (first_actionable or {}).get("actions", [{}])[0],
            "health_label": "Listo para operar" if progress >= 85 else "Activacion en progreso",
        },
        "preferred_channels": preferred_channels,
        "counts": counts,
        "channels": channels,
        "implementation_journey": implementation_journey,
        "organization_setup": build_organization_setup_journey(tenant, channels, workspace_appearance=build_workspace_appearance(tenant, entitled=tenant_allows_workspace_branding(tenant)), conversation_guide=guide),
        "blockers": blockers,
        "integration_access": {
            "enabled": access.get("enabled"),
            "status": access.get("status"),
            "required_plan": access.get("required_plan"),
            "current_plan": access.get("current_plan"),
            "reason_code": access.get("reason_code"),
            "message": access.get("message"),
        },
        "provisioning": {
            "status": provisioning.get("status"),
            "blocked_reason": provisioning.get("blocked_reason"),
            "channel_strategy": provisioning.get("channel_strategy"),
        },
        "endpoints": {
            "self": f"/api/v2/tenants/{getattr(tenant, 'slug', '')}/activation/channels",
            "profile": "/auth/me",
            "bootstrap": "/auth/session/bootstrap",
            "whatsapp_status": f"/api/v2/tenants/{getattr(tenant, 'slug', '')}/integrations/whatsapp/status",
            "live_chat_schedule": f"/api/admin/tenants/{getattr(tenant, 'slug', '')}/live-chat/schedule",
            "mesa_unica_preview": f"/api/v2/tenants/{getattr(tenant, 'slug', '')}/blueprints/government-core/launch/mesa-unica/preview",
            "mesa_unica_apply": f"/api/v2/tenants/{getattr(tenant, 'slug', '')}/blueprints/government-core/launch/mesa-unica/apply",
        },
        "security": {
            "secret_free": True,
            "widget_tokens_exposed": False,
            "provider_credentials_exposed": False,
        },
    }
