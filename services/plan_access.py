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


def integration_access_payload(tenant: TenantProfile | None) -> dict[str, Any]:
    enabled = plan_allows_full_integrations(tenant)
    current_plan = normalize_plan(getattr(tenant, "plan", None) if tenant is not None else None) or "free"
    payload = {
        "contract_version": "tenant.integration_access.v1",
        "enabled": enabled,
        "status": "enabled" if enabled else "locked",
        "reason_code": None if enabled else "plan_full_required",
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
    }
    return payload
