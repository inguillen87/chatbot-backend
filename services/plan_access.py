from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from models import TenantProfile


FULL_INTEGRATION_PLANS = {
    "full",
    "enterprise",
    "premium",
    "municipio_full",
    "colegio_full",
    "pyme_full",
}

FULL_INTEGRATION_CAPABILITIES = {
    "integrations.full_access",
    "integrations.production",
    "whatsapp.production",
    "whatsapp_business_platform",
    "widget.production",
    "widget.embed",
}

INTEGRATION_FEATURES: dict[str, dict[str, str]] = {
    "widget_embed": {
        "label": "Widget web embebido",
        "capability": "widget.embed",
        "admin_route": "/integracion",
        "action": "copy_widget_embed",
    },
    "whatsapp_business_platform": {
        "label": "WhatsApp Business Platform",
        "capability": "whatsapp.production",
        "admin_route": "/integracion",
        "action": "connect_whatsapp_sender",
    },
    "whatsapp_sender_management": {
        "label": "Gestion de sender y plantillas",
        "capability": "whatsapp_business_platform",
        "admin_route": "/integracion",
        "action": "manage_whatsapp_sender",
    },
    "marketplace_sync": {
        "label": "Marketplaces y catalogos externos",
        "capability": "integrations.production",
        "admin_route": "/integracion",
        "action": "connect_marketplace",
    },
    "mercadopago_checkout": {
        "label": "Cobros y checkout seguro",
        "capability": "integrations.production",
        "admin_route": "/integracion",
        "action": "configure_payment_gateway",
    },
    "catalog_management": {
        "label": "Catalogo y pedidos conversacionales",
        "capability": "market.catalog.write",
        "admin_route": "/catalogo",
        "action": "publish_catalog",
    },
    "analytics_dashboard": {
        "label": "Metricas y analitica operativa",
        "capability": "analytics.operations.read",
        "admin_route": "/analytics",
        "action": "run_analytics_dashboard",
    },
    "heatmaps": {
        "label": "Mapas de calor y actividad territorial",
        "capability": "analytics.heatmap.read",
        "admin_route": "/perfil",
        "action": "open_heatmap",
    },
    "surveys_votings": {
        "label": "Encuestas y votaciones",
        "capability": "surveys.write",
        "admin_route": "/encuestas",
        "action": "create_surveys",
    },
    "comments_inbox": {
        "label": "Comentarios, inbox y derivacion humana",
        "capability": "inbox.comments.read",
        "admin_route": "/chat-en-vivo",
        "action": "manage_comments",
    },
    "realtime_voice": {
        "label": "Voz, notas de audio y accesibilidad",
        "capability": "voice.realtime",
        "admin_route": "/integracion",
        "action": "configure_realtime_voice",
    },
}


def normalize_plan(plan: Any) -> str:
    return str(plan or "").strip().lower()


def _iter_capability_values(raw: Any) -> Iterable[str]:
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if value:
                yield str(key).strip().lower()
        return

    if isinstance(raw, (list, tuple, set)):
        for item in raw:
            if isinstance(item, Mapping):
                for key, value in item.items():
                    if value:
                        yield str(key).strip().lower()
            elif item:
                yield str(item).strip().lower()
        return

    if isinstance(raw, str) and raw.strip():
        yield raw.strip().lower()


def tenant_capability_values(tenant: TenantProfile | None) -> set[str]:
    if tenant is None:
        return set()

    values: set[str] = set()
    for raw in (
        getattr(tenant, "capabilities_json", None),
        getattr(tenant, "capabilities", None),
    ):
        values.update(v for v in _iter_capability_values(raw) if v)

    cfg = getattr(tenant, "configuracion", None)
    if isinstance(cfg, Mapping):
        for key in ("capabilities", "feature_flags", "features"):
            values.update(v for v in _iter_capability_values(cfg.get(key)) if v)

    return values


def tenant_has_any_capability(
    tenant: TenantProfile | None,
    capability_names: Iterable[str] = FULL_INTEGRATION_CAPABILITIES,
) -> bool:
    values = tenant_capability_values(tenant)
    if "*" in values or "all" in values:
        return True
    required = {str(name).strip().lower() for name in capability_names}
    return bool(values.intersection(required))


def tenant_is_demo_context(tenant: TenantProfile | None) -> bool:
    cfg = getattr(tenant, "configuracion", None) if tenant is not None else None
    if not isinstance(cfg, Mapping):
        return False
    return bool(
        cfg.get("demo_mode")
        or cfg.get("trial_mode")
        or cfg.get("chatboc_demo_hub")
        or cfg.get("is_demo_tenant")
    )


def plan_allows_full_integrations(tenant: TenantProfile | None) -> bool:
    if tenant is None:
        return False
    if not bool(getattr(tenant, "is_active", True)):
        return False
    if tenant_is_demo_context(tenant):
        return False
    if normalize_plan(getattr(tenant, "plan", None)) in FULL_INTEGRATION_PLANS:
        return True
    return tenant_has_any_capability(tenant)


def _integration_lock_reason(tenant: TenantProfile | None) -> str:
    if tenant is None:
        return "tenant_missing"
    if not bool(getattr(tenant, "is_active", True)):
        return "tenant_inactive"
    if tenant_is_demo_context(tenant):
        return "demo_tenant_locked"
    return "plan_full_required"


def _feature_payload(feature_id: str, enabled: bool, lock_reason: str | None) -> dict[str, Any]:
    feature = INTEGRATION_FEATURES[feature_id]
    return {
        "id": feature_id,
        "label": feature["label"],
        "capability": feature["capability"],
        "admin_route": feature["admin_route"],
        "action": feature["action"],
        "enabled": enabled,
        "status": "enabled" if enabled else "locked",
        "reason_code": None if enabled else "plan_full_required",
        "lock_reason_code": None if enabled else lock_reason,
        "required_plan": "full",
    }


def integration_access_payload(tenant: TenantProfile | None) -> dict[str, Any]:
    enabled = plan_allows_full_integrations(tenant)
    current_plan = normalize_plan(getattr(tenant, "plan", None) if tenant is not None else None) or "free"
    lock_reason = None if enabled else _integration_lock_reason(tenant)
    features = {
        feature_id: _feature_payload(feature_id, enabled, lock_reason)
        for feature_id in INTEGRATION_FEATURES
    }
    actions = [feature["action"] for feature in INTEGRATION_FEATURES.values()]
    payload = {
        "contract_version": "tenant.integration_access.v1",
        "enabled": enabled,
        "status": "enabled" if enabled else "locked",
        "reason_code": None if enabled else "plan_full_required",
        "lock_reason_code": lock_reason,
        "required_plan": "full",
        "current_plan": current_plan,
        "message": (
            "Integraciones productivas habilitadas."
            if enabled
            else "Las integraciones productivas de WhatsApp, widget embebido y marketplaces requieren plan Full activo."
        ),
        "upgrade": {
            "label": "Solicitar upgrade a Full",
            "channel": "sales",
            "url": "https://www.chatboc.ar/#precios",
        },
        "features": features,
        "feature_groups": {
            "channels": ["widget_embed", "whatsapp_business_platform", "whatsapp_sender_management", "realtime_voice"],
            "commerce": ["catalog_management", "mercadopago_checkout", "marketplace_sync"],
            "operations": ["analytics_dashboard", "heatmaps", "surveys_votings", "comments_inbox"],
        },
        "allowed_actions": actions if enabled else [],
        "blocked_actions": [] if enabled else actions,
        "security": {
            "demo_tenants_blocked": True,
            "inactive_tenants_blocked": True,
            "requires_authenticated_admin": True,
            "requires_tenant_authorization": True,
            "requires_widget_token_for_embed": True,
            "card_data_in_chat_allowed": False,
            "public_widget_resolves_readonly_contract": True,
        },
        "frontend_contract": {
            "render_locked_state": not enabled,
            "hide_embed_copy": not enabled,
            "hide_provider_connect": not enabled,
            "hide_payment_credentials_form": not enabled,
            "show_upgrade_cta": not enabled,
            "show_readiness_checklist": True,
            "primary_locked_reason": lock_reason,
        },
    }
    return payload
