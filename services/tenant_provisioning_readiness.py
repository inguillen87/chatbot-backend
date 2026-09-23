"""Server-authoritative tenant configuration readiness.

This module intentionally evaluates only persisted, tenant-scoped evidence.  It
does not contact providers, enqueue work, mutate tenant configuration, or claim
that infrastructure/production cutover has been certified.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from models import CatalogoItem, CategoriaTicket, TenantConfig, TenantProfile, User
from services.tenant_factory import (
    PROVISIONING_READINESS_CONTRACT,
    REQUIRED_TEMPLATE_CONFIGS,
)
from services.twilio_tech_provider import STATE_KEY


_READY_STATES = {"active", "approved", "connected", "enabled", "online", "ready", "verified"}
_CHANNEL_ALIASES = {
    "chat": "widget",
    "chat_widget": "widget",
    "web": "widget",
    "webchat": "widget",
    "web_chat": "widget",
    "widget": "widget",
    "whatsapp": "whatsapp",
    "whatsapp_business": "whatsapp",
    "whatsapp_business_platform": "whatsapp",
    "live_chat": "live_chat",
    "human": "live_chat",
    "humano": "live_chat",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _normalized_channel(value: Any) -> str:
    normalized = _clean(value).lower().replace("-", "_").replace(" ", "_")
    return _CHANNEL_ALIASES.get(normalized, normalized)


def _palette_configured(tenant: TenantProfile) -> bool:
    for raw_theme in (getattr(tenant, "tema", None), getattr(tenant, "theme_json", None)):
        theme = _mapping(raw_theme)
        if any(_clean(theme.get(key)) for key in ("primary", "primaryColor", "primary_color")):
            return True
        for mode in ("light", "dark"):
            palette = _mapping(theme.get(mode))
            if any(_clean(palette.get(key)) for key in ("primary", "primaryColor", "primary_color")):
                return True
    return False


def _config_payloads(tenant_id: int) -> tuple[dict[str, list[Mapping[str, Any]]], list[str]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    configured_keys: set[str] = set()
    rows = TenantConfig.query.filter(TenantConfig.tenant_id == tenant_id).all()
    for row in rows:
        payload = _mapping(row.json_value)
        grouped.setdefault(str(row.key or ""), []).append(payload)
        if payload:
            configured_keys.add(str(row.key or ""))
    return grouped, sorted(configured_keys)


def _menu_content_count(configs: Mapping[str, list[Mapping[str, Any]]]) -> int:
    count = 0
    for payload in configs.get("menu", []):
        items = payload.get("items")
        if isinstance(items, list):
            count += sum(1 for item in items if isinstance(item, Mapping) and item)
    return count


def _tenant_team_evidence(tenant_id: int) -> dict[str, int]:
    categories = CategoriaTicket.query.filter(
        CategoriaTicket.tenant_id == tenant_id,
        CategoriaTicket.tipo == "ticket",
    ).all()
    category_names = {
        _clean(category.nombre).casefold()
        for category in categories
        if _clean(category.nombre)
    }
    employees = User.query.filter(
        User.tenant_id == tenant_id,
        User.es_empleado.is_(True),
    ).all()

    routed = 0
    for employee in employees:
        relation_matches = any(
            getattr(category, "tenant_id", None) == tenant_id
            for category in (getattr(employee, "categorias_ticket", None) or [])
        )
        scope = _mapping(_mapping(getattr(employee, "accesibilidad", None)).get("employee_scope"))
        scope_categories = scope.get("categorias") or scope.get("categories") or []
        scope_matches = isinstance(scope_categories, list) and any(
            _clean(category).casefold() in category_names
            for category in scope_categories
            if _clean(category)
        )
        if relation_matches or scope_matches:
            routed += 1

    return {
        "members": len(employees),
        "ticket_categories": len(categories),
        "routed_members": routed,
    }


def _widget_verified(
    tenant: TenantProfile,
    configs: Mapping[str, list[Mapping[str, Any]]],
    tenant_cfg: Mapping[str, Any],
) -> bool:
    explicit_enabled = any(payload.get("enabled") is True for payload in configs.get("widget", []))
    tokens = tenant_cfg.get("widget_tokens")
    token_present = isinstance(tokens, list) and any(_clean(value) for value in tokens)
    configured_model = bool(getattr(tenant, "widget_settings", None) or getattr(tenant, "widget_config", None))
    return explicit_enabled and (token_present or configured_model)


def _whatsapp_verified(tenant: TenantProfile, tenant_cfg: Mapping[str, Any]) -> bool:
    onboarding = _mapping(tenant_cfg.get("whatsapp_onboarding"))
    provider_state = _mapping(tenant_cfg.get(STATE_KEY))
    onboarding_ready = _clean(onboarding.get("status")).lower() in _READY_STATES
    sender_ready = _clean(provider_state.get("sender_status")).lower() in _READY_STATES
    sender_present = bool(
        _clean(getattr(tenant, "whatsapp_sender_id", None))
        or _clean(provider_state.get("sender_id"))
        or _clean(provider_state.get("sender_sid"))
    )
    return sender_present and (onboarding_ready or sender_ready)


def _live_chat_verified(tenant_cfg: Mapping[str, Any]) -> bool:
    schedule = _mapping(tenant_cfg.get("live_chat_schedule"))
    return bool(
        schedule.get("enabled") is True
        and _clean(schedule.get("timezone"))
        and schedule.get("days")
        and _clean(schedule.get("start_time"))
        and _clean(schedule.get("end_time"))
    )


def _channel_evidence(
    tenant: TenantProfile,
    configs: Mapping[str, list[Mapping[str, Any]]],
    tenant_cfg: Mapping[str, Any],
) -> dict[str, Any]:
    verified = {
        "widget": _widget_verified(tenant, configs, tenant_cfg),
        "whatsapp": _whatsapp_verified(tenant, tenant_cfg),
        "live_chat": _live_chat_verified(tenant_cfg),
    }
    onboarding = _mapping(tenant_cfg.get("onboarding"))
    raw_selected = onboarding.get("preferred_channels")
    selected: list[str] = []
    if isinstance(raw_selected, list):
        for value in raw_selected:
            normalized = _normalized_channel(value)
            if normalized and normalized not in selected:
                selected.append(normalized)

    verified_channels = sorted(channel for channel, is_ready in verified.items() if is_ready)
    if not selected:
        selected = list(verified_channels)
    missing = sorted(channel for channel in selected if not verified.get(channel, False))
    complete = bool(selected) and not missing
    return {
        "selected": selected,
        "verified": verified_channels,
        "missing": missing,
        "complete": complete,
        "provider_activation_performed": bool(verified.get("whatsapp")),
    }


def build_tenant_provisioning_readiness(tenant: TenantProfile | None) -> dict[str, Any]:
    """Return a side-effect-free readiness snapshot derived from current rows."""

    if tenant is None or not getattr(tenant, "id", None):
        return {
            "contract_version": PROVISIONING_READINESS_CONTRACT,
            "readiness_scope": "tenant_configuration",
            "evaluated_stage": "tenant_missing",
            "requires_revalidation": True,
            "status": "blocked",
            "ready": False,
            "production_ready": False,
            "checks": {
                "base_configuration_valid": False,
                "branding_configuration_complete": False,
                "operator_configuration_complete": False,
                "service_content_configured": False,
                "channel_verification_complete": False,
                "provider_activation_performed": False,
            },
            "configured_keys": [],
            "missing": ["tenant"],
            "next_action": "create_tenant",
            "evidence": {},
            "safety": {
                "server_derived": True,
                "side_effects_performed": False,
                "provider_calls_performed": False,
                "production_cutover_assessed": False,
            },
        }

    tenant_cfg = _mapping(getattr(tenant, "configuracion", None))
    configs, configured_keys = _config_payloads(int(tenant.id))
    missing_config_keys = sorted(set(REQUIRED_TEMPLATE_CONFIGS) - set(configured_keys))
    base_valid = not missing_config_keys

    logo_configured = bool(_clean(getattr(tenant, "logo_url", None)))
    palette_configured = _palette_configured(tenant)
    branding_complete = logo_configured and palette_configured

    team = _tenant_team_evidence(int(tenant.id))
    operator_complete = team["members"] > 0 and team["routed_members"] > 0

    catalog_items = int(
        CatalogoItem.query.filter(CatalogoItem.tenant_id == tenant.id).count()
    )
    menu_items = _menu_content_count(configs)
    service_content_configured = catalog_items > 0 or menu_items > 0

    channels = _channel_evidence(tenant, configs, tenant_cfg)
    channel_complete = bool(channels["complete"])

    is_active = getattr(tenant, "is_active", True) is True
    checks = {
        "base_configuration_valid": base_valid,
        "branding_configuration_complete": branding_complete,
        "operator_configuration_complete": operator_complete,
        "service_content_configured": service_content_configured,
        "channel_verification_complete": channel_complete,
        "provider_activation_performed": bool(channels["provider_activation_performed"]),
    }
    required_checks = (
        "base_configuration_valid",
        "branding_configuration_complete",
        "operator_configuration_complete",
        "service_content_configured",
        "channel_verification_complete",
    )
    ready = is_active and all(checks[key] for key in required_checks)

    missing: list[str] = []
    if not is_active:
        missing.append("tenant_active")
    if not base_valid:
        missing.append("base_configuration")
    if not branding_complete:
        missing.append("branding")
    if not operator_complete:
        missing.append("operator_team")
    if not service_content_configured:
        missing.append("service_content")
    if not channel_complete:
        missing.append("channel_verification")

    progressed = sum(
        int(value)
        for value in (
            branding_complete,
            operator_complete,
            service_content_configured,
            channel_complete,
        )
    )
    if not is_active:
        stage = "tenant_inactive"
    elif not base_valid:
        stage = "configuration_blocked"
    elif ready:
        stage = "configuration_complete"
    elif progressed:
        stage = "configuration_in_progress"
    else:
        stage = "tenant_created"

    next_actions = {
        "tenant_active": "activate_tenant",
        "base_configuration": "repair_base_configuration",
        "branding": "configure_branding",
        "operator_team": "configure_operator_team",
        "service_content": "publish_service_content",
        "channel_verification": "verify_tenant_channel",
    }
    next_action = "review_activation" if ready else next_actions.get(missing[0], "review_configuration")
    template = _mapping(tenant_cfg.get("template"))

    return {
        "contract_version": PROVISIONING_READINESS_CONTRACT,
        "readiness_scope": "tenant_configuration",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant": {
            "id": tenant.id,
            "slug": tenant.slug,
            "tipo": tenant.tipo,
        },
        "template_key": template.get("key"),
        "evaluated_stage": stage,
        "requires_revalidation": not ready,
        "status": "ready" if ready else ("blocked" if not is_active or not base_valid else "configuration_required"),
        "ready": ready,
        "production_ready": False,
        "checks": checks,
        "configured_keys": configured_keys,
        "missing": missing,
        "next_action": next_action,
        "evidence": {
            "base_configuration": {
                "required_keys": list(REQUIRED_TEMPLATE_CONFIGS),
                "configured_keys": configured_keys,
                "missing_keys": missing_config_keys,
            },
            "branding": {
                "logo_configured": logo_configured,
                "palette_configured": palette_configured,
            },
            "operator_team": team,
            "service_content": {
                "catalog_items": catalog_items,
                "menu_items": menu_items,
            },
            "channels": channels,
        },
        "safety": {
            "server_derived": True,
            "side_effects_performed": False,
            "provider_calls_performed": False,
            "production_cutover_assessed": False,
        },
    }
