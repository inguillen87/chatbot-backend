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

PUBLIC_DEMO_UPLOAD_CAPABILITIES = {
    "demo.public_uploads",
    "uploads.public_demo",
}

SELF_SERVICE_INTEGRATION_FEATURES = {
    "catalog_management",
    "education_management",
    "analytics_dashboard",
    "heatmaps",
    "surveys_votings",
    "comments_inbox",
}

PRODUCTIVE_INTEGRATION_FEATURES = {
    "widget_embed",
    "whatsapp_business_platform",
    "whatsapp_sender_management",
    "marketplace_sync",
    "mercadopago_checkout",
    "realtime_voice",
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
    "education_management": {
        "label": "Colegios, familias y tramites escolares",
        "capability": "education.management",
        "admin_route": "/perfil",
        "action": "manage_education_operations",
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


def tenant_allows_public_demo_uploads(tenant: TenantProfile | None) -> bool:
    """Allow anonymous demo attachments only through an explicit server-side grant."""

    if tenant is None or not bool(getattr(tenant, "is_active", True)):
        return False
    if tenant_is_demo_context(tenant):
        return True
    cfg = getattr(tenant, "configuracion", None)
    if isinstance(cfg, Mapping) and cfg.get("public_demo_uploads_enabled") is True:
        return True
    return tenant_has_any_capability(tenant, PUBLIC_DEMO_UPLOAD_CAPABILITIES)


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


def _feature_capability_names(feature_id: str) -> set[str]:
    feature = INTEGRATION_FEATURES.get(feature_id)
    names = {feature_id}
    if feature:
        names.add(str(feature.get("capability") or "").strip().lower())
    return {name for name in names if name}


def plan_allows_integration_feature(tenant: TenantProfile | None, feature_id: str) -> bool:
    if plan_allows_full_integrations(tenant):
        return True
    if tenant is None:
        return False
    if not bool(getattr(tenant, "is_active", True)):
        return False
    if tenant_is_demo_context(tenant):
        return False
    if tenant_has_any_capability(tenant, _feature_capability_names(feature_id)):
        return True
    return feature_id in SELF_SERVICE_INTEGRATION_FEATURES


def _integration_lock_reason(tenant: TenantProfile | None) -> str:
    if tenant is None:
        return "tenant_missing"
    if not bool(getattr(tenant, "is_active", True)):
        return "tenant_inactive"
    if tenant_is_demo_context(tenant):
        return "demo_tenant_locked"
    return "plan_full_required"


def _feature_payload(
    feature_id: str,
    enabled: bool,
    lock_reason: str | None,
    *,
    full_enabled: bool = False,
) -> dict[str, Any]:
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
        "access_scope": "productive" if full_enabled else ("self_service" if enabled else "locked"),
    }


def integration_access_payload(tenant: TenantProfile | None) -> dict[str, Any]:
    enabled = plan_allows_full_integrations(tenant)
    current_plan = normalize_plan(getattr(tenant, "plan", None) if tenant is not None else None) or "free"
    lock_reason = None if enabled else _integration_lock_reason(tenant)
    features = {
        feature_id: _feature_payload(
            feature_id,
            plan_allows_integration_feature(tenant, feature_id),
            lock_reason,
            full_enabled=enabled,
        )
        for feature_id in INTEGRATION_FEATURES
    }
    allowed_actions = [
        feature["action"]
        for feature_id, feature in INTEGRATION_FEATURES.items()
        if features[feature_id]["enabled"]
    ]
    blocked_actions = [
        feature["action"]
        for feature_id, feature in INTEGRATION_FEATURES.items()
        if not features[feature_id]["enabled"]
    ]
    has_self_service_access = bool(allowed_actions)
    widget_enabled = bool(features["widget_embed"]["enabled"])
    whatsapp_enabled = bool(features["whatsapp_business_platform"]["enabled"])
    payments_enabled = bool(features["mercadopago_checkout"]["enabled"])
    payload = {
        "contract_version": "tenant.integration_access.v1",
        "enabled": enabled,
        "status": "enabled" if enabled else ("partial" if has_self_service_access else "locked"),
        "reason_code": None if enabled else "plan_full_required",
        "lock_reason_code": lock_reason,
        "required_plan": "full",
        "current_plan": current_plan,
        "message": (
            "Integraciones productivas habilitadas."
            if enabled
            else (
                "Modulos operativos habilitados. WhatsApp productivo, cobros, widget embebido y marketplaces externos requieren plan Full activo."
                if has_self_service_access
                else "Las integraciones productivas de WhatsApp, widget embebido y marketplaces requieren plan Full activo."
            )
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
            "verticals": ["education_management"],
        },
        "allowed_actions": allowed_actions,
        "blocked_actions": blocked_actions,
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
            "render_locked_state": not has_self_service_access,
            "hide_embed_copy": not widget_enabled,
            "hide_provider_connect": not whatsapp_enabled,
            "hide_payment_credentials_form": not payments_enabled,
            "show_upgrade_cta": not enabled,
            "show_readiness_checklist": True,
            "primary_locked_reason": lock_reason,
            "self_service_enabled": has_self_service_access and not enabled,
            "productive_channels_locked": not enabled,
        },
    }
    return payload


def integration_feature_payload(access: Mapping[str, Any] | None, feature_id: str) -> dict[str, Any]:
    access_map = access if isinstance(access, Mapping) else {}
    features = access_map.get("features") if isinstance(access_map.get("features"), Mapping) else {}
    feature = features.get(feature_id)
    if isinstance(feature, Mapping):
        return dict(feature)

    meta = INTEGRATION_FEATURES.get(
        feature_id,
        {
            "label": feature_id.replace("_", " ").strip().title() or "Integracion",
            "capability": "integrations.production",
            "admin_route": "/integracion",
            "action": "manage_integration",
        },
    )
    enabled = bool(access_map.get("enabled"))
    reason_code = access_map.get("reason_code") or (None if enabled else "plan_full_required")
    lock_reason_code = access_map.get("lock_reason_code")
    return {
        "id": feature_id,
        "label": meta["label"],
        "capability": meta["capability"],
        "admin_route": meta["admin_route"],
        "action": meta["action"],
        "enabled": enabled,
        "status": "enabled" if enabled else "locked",
        "reason_code": reason_code,
        "lock_reason_code": None if enabled else lock_reason_code,
        "required_plan": access_map.get("required_plan") or "full",
    }


def integration_frontend_contract(
    access: Mapping[str, Any] | None,
    feature_id: str,
    *,
    render_as: str = "integration_locked",
    primary_action: str = "upgrade_to_full",
    **overrides: Any,
) -> dict[str, Any]:
    access_map = access if isinstance(access, Mapping) else {}
    feature = integration_feature_payload(access_map, feature_id)
    enabled = bool(feature.get("enabled"))
    contract = dict(access_map.get("frontend_contract") or {})
    contract.update(
        {
            "render_as": render_as,
            "feature_id": feature_id,
            "feature_label": feature.get("label"),
            "feature_action": feature.get("action"),
            "primary_action": primary_action,
            "required_plan": access_map.get("required_plan") or feature.get("required_plan") or "full",
            "current_plan": access_map.get("current_plan") or "free",
            "render_locked_state": not enabled,
            "show_upgrade_cta": not enabled,
            "lock_reason_code": feature.get("lock_reason_code") or access_map.get("lock_reason_code"),
            "reason_code": feature.get("reason_code") or access_map.get("reason_code"),
            "primary_locked_reason": feature.get("lock_reason_code") or access_map.get("lock_reason_code"),
        }
    )
    contract.update(overrides)
    return contract


def integration_plan_required_payload(
    tenant: TenantProfile | None,
    feature_id: str,
    *,
    contract_version: str | None = None,
    action_hint: str = "upgrade_to_full",
    render_as: str = "integration_locked",
    status_code: int = 403,
    extra: Mapping[str, Any] | None = None,
    **frontend_overrides: Any,
) -> dict[str, Any]:
    access = integration_access_payload(tenant)
    feature = integration_feature_payload(access, feature_id)
    payload: dict[str, Any] = {
        "ok": False,
        "error": "plan_required",
        "contract_version": contract_version or access.get("contract_version") or "tenant.integration_access.v1",
        "status_code": status_code,
        "reason_code": feature.get("reason_code") or access.get("reason_code") or "plan_full_required",
        "lock_reason_code": feature.get("lock_reason_code") or access.get("lock_reason_code"),
        "action_hint": action_hint,
        "message": access.get("message"),
        "feature_id": feature_id,
        "feature": feature,
        "access": access,
        "upgrade": access.get("upgrade"),
        "frontend_contract": integration_frontend_contract(
            access,
            feature_id,
            render_as=render_as,
            primary_action=action_hint,
            **frontend_overrides,
        ),
    }
    if tenant is not None:
        payload["tenant_id"] = getattr(tenant, "id", None)
        payload["tenant_slug"] = getattr(tenant, "slug", None)
    if extra:
        payload.update(dict(extra))
    return payload


def tenant_allows_workspace_branding(tenant: TenantProfile | None) -> bool:
    """Explicit server plan only; unrelated configurable capabilities do not grant branding."""
    return bool(tenant is not None and getattr(tenant,'is_active',False) is True
        and not tenant_is_demo_context(tenant)
        and normalize_plan(getattr(tenant,'plan',None)) in FULL_INTEGRATION_PLANS)


def tenant_allows_module_selection(tenant: TenantProfile | None) -> bool:
    """Same explicit Full policy as workspace customization; never a client-editable feature flag."""
    return tenant_allows_workspace_branding(tenant)
