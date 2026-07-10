from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy.orm.attributes import flag_modified

from services.provider_platform import is_sender_ready_status, sync_twilio_provider_records
from services.twilio_tech_provider import (
    STATE_KEY,
    build_twilio_tech_provider_contract,
    merge_twilio_state,
    provision_twilio_subaccount,
    provision_twilio_voice_application,
)


CONTRACT_VERSION = "tenant.whatsapp_onboarding_bootstrap.v1"
ONBOARDING_KEY = "whatsapp_onboarding"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _bool_config(config: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _tenant_ref(tenant) -> dict[str, Any]:
    return {
        "id": getattr(tenant, "id", None),
        "slug": getattr(tenant, "slug", None),
        "nombre": getattr(tenant, "nombre", None),
        "tipo": getattr(tenant, "tipo", None),
        "vertical": getattr(tenant, "vertical", None),
    }


def _onboarding_status(contract: Mapping[str, Any], state: Mapping[str, Any], auto_provision_enabled: bool) -> str:
    sender_status = _clean(state.get("sender_status")).lower()
    if is_sender_ready_status(sender_status):
        return "online"
    if state.get("sender_sid"):
        return "sender_registered"
    if state.get("waba_id") or state.get("phone_number_id"):
        return "pending_sender_registration"
    if state.get("messaging_service_sid") and state.get("twilio_account_sid"):
        return "ready_for_embedded_signup"
    if auto_provision_enabled:
        return "provisioning_started"
    if contract.get("status") == "needs_platform_config":
        return "needs_platform_config"
    return "plan_ready"


def _step_state(workflow: list[Mapping[str, Any]], step_id: str, fallback: str = "pending") -> str:
    for item in workflow:
        if item.get("id") == step_id:
            return _clean(item.get("state")) or fallback
    return fallback


def _public_payload_for_config(
    *,
    tenant,
    contract: Mapping[str, Any],
    state: Mapping[str, Any],
    auto_provision_enabled: bool,
    source: str,
) -> dict[str, Any]:
    tenant_slug = getattr(tenant, "slug", None)
    workflow = contract.get("api_workflow") if isinstance(contract.get("api_workflow"), list) else []
    embedded_signup = contract.get("embedded_signup") if isinstance(contract.get("embedded_signup"), dict) else {}
    status = _onboarding_status(contract, state, auto_provision_enabled)
    return {
        "contract_version": "tenant.whatsapp_onboarding.v1",
        "provider": "twilio_tech_provider",
        "status": status,
        "source": source,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "auto_provision_enabled": auto_provision_enabled,
        "customer_next_action": (
            "start_embedded_signup"
            if status in {"ready_for_embedded_signup", "plan_ready"}
            else "wait_for_platform_config"
            if status == "needs_platform_config"
            else "complete_phone_validation"
        ),
        "admin_next_action": (
            "enable_twilio_meta_render_env"
            if status == "needs_platform_config"
            else "review_provider_status"
        ),
        "connect": {
            "status_endpoint": f"/api/v2/tenants/{tenant_slug}/integrations/whatsapp/status",
            "tech_provider_endpoint": f"/api/v2/tenants/{tenant_slug}/whatsapp/tech-provider",
            "provision_endpoint": f"/api/v2/tenants/{tenant_slug}/whatsapp/tech-provider/provision",
            "embedded_signup_endpoint": f"/api/v2/tenants/{tenant_slug}/whatsapp/tech-provider/embedded-signup",
            "register_sender_endpoint": f"/api/v2/tenants/{tenant_slug}/whatsapp/tech-provider/register-sender",
            "voice_app_endpoint": f"/api/v2/tenants/{tenant_slug}/whatsapp/tech-provider/voice-app",
        },
        "steps": [
            {"id": "create_tenant", "owner": "backend", "state": "done"},
            {"id": "create_subaccount", "owner": "backend", "state": _step_state(workflow, "create_subaccount")},
            {"id": "create_messaging_service", "owner": "backend", "state": _step_state(workflow, "create_messaging_service")},
            {
                "id": "sync_subaccount_secret_to_render",
                "owner": "backend_secret_store",
                "state": _step_state(workflow, "sync_subaccount_secret_to_render", "manual_or_disabled"),
            },
            {
                "id": "embedded_signup",
                "owner": "customer",
                "state": "done" if state.get("waba_id") else ("enabled" if embedded_signup.get("enabled") else "blocked"),
            },
            {
                "id": "register_sender",
                "owner": "backend",
                "state": "done" if state.get("sender_sid") else "pending_customer_signup",
            },
            {
                "id": "voice_twiml_app",
                "owner": "backend",
                "state": _step_state(workflow, "create_or_update_voice_twiml_app"),
            },
        ],
        "embedded_signup": {
            "enabled": bool(embedded_signup.get("enabled")),
            "meta_app_id_present": bool(embedded_signup.get("meta_app_id")),
            "configuration_id_present": bool(embedded_signup.get("configuration_id")),
        },
        "state": {
            "twilio_account_sid": state.get("twilio_account_sid"),
            "messaging_service_sid": state.get("messaging_service_sid"),
            "sender_sid": state.get("sender_sid"),
            "sender_id": state.get("sender_id"),
            "sender_status": state.get("sender_status"),
            "voice_twiml_app_sid": state.get("voice_twiml_app_sid"),
            "render_subaccount_secret_synced": state.get("render_subaccount_secret_synced"),
            "render_subaccount_secret_sync_status": state.get("render_subaccount_secret_sync_status"),
        },
    }


def refresh_tenant_whatsapp_onboarding(
    tenant,
    *,
    app_config: Mapping[str, Any],
    source: str = "provider_state_changed",
) -> dict[str, Any]:
    """Refresh the secret-free public onboarding snapshot from provider state.

    The Twilio tech-provider state is the source of truth after provisioning,
    embedded signup, sender registration and sender polling. This keeps the
    tenant-facing checklist aligned without requiring every route to duplicate
    the mapping rules.
    """

    cfg = tenant.configuracion if isinstance(getattr(tenant, "configuracion", None), dict) else {}
    contract = build_twilio_tech_provider_contract(tenant, app_config)
    state = cfg.get(STATE_KEY) if isinstance(cfg.get(STATE_KEY), dict) else {}
    onboarding = _public_payload_for_config(
        tenant=tenant,
        contract=contract,
        state=state,
        auto_provision_enabled=_bool_config(app_config, "TWILIO_TENANT_AUTO_PROVISION_ENABLED", False),
        source=source,
    )
    cfg[ONBOARDING_KEY] = onboarding
    tenant.configuracion = cfg
    flag_modified(tenant, "configuracion")
    return onboarding


def bootstrap_tenant_whatsapp_onboarding(
    tenant,
    *,
    app_config: Mapping[str, Any],
    payload: Mapping[str, Any] | None = None,
    actor_user: Any = None,
    request_id: str | None = None,
    source: str = "tenant_created",
) -> dict[str, Any]:
    """Create or refresh the provider onboarding plan for a new tenant.

    This function is intentionally idempotent and fail-soft: tenant creation must
    not fail just because a platform credential is missing.
    """

    payload = dict(payload or {})
    cfg = tenant.configuracion if isinstance(getattr(tenant, "configuracion", None), dict) else {}
    auto_bootstrap_enabled = _bool_config(app_config, "TWILIO_TENANT_AUTO_BOOTSTRAP_ENABLED", True)
    auto_provision_enabled = _bool_config(app_config, "TWILIO_TENANT_AUTO_PROVISION_ENABLED", False)
    now = datetime.now(timezone.utc).isoformat()
    result: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "ok": True,
        "source": source,
        "tenant": _tenant_ref(tenant),
        "auto_bootstrap_enabled": auto_bootstrap_enabled,
        "auto_provision_enabled": auto_provision_enabled,
        "updated_at": now,
        "steps": [],
    }

    if not auto_bootstrap_enabled:
        result.update({"mode": "disabled", "status": "disabled"})
        cfg[ONBOARDING_KEY] = {
            "contract_version": "tenant.whatsapp_onboarding.v1",
            "provider": "twilio_tech_provider",
            "status": "disabled",
            "source": source,
            "updated_at": now,
        }
        tenant.configuracion = cfg
        flag_modified(tenant, "configuracion")
        return result

    if not payload.get("display_name"):
        payload["display_name"] = getattr(tenant, "nombre", None)
    if not payload.get("phone_number") and getattr(tenant, "whatsapp_sender_id", None):
        payload["phone_number"] = getattr(tenant, "whatsapp_sender_id", None)

    try:
        if auto_provision_enabled:
            provision = provision_twilio_subaccount(tenant, payload, app_config)
            state = merge_twilio_state(tenant, provision.get("state_patch") or {})
            sync_twilio_provider_records(
                tenant,
                state,
                app_config=app_config,
                actor_user=actor_user,
                request_id=request_id,
                event_type="tenant_whatsapp_bootstrap_provision",
            )
            result["steps"].append({"id": "provision", "status": "done" if provision.get("ok", True) else "blocked", "result": provision})
        else:
            state = cfg.get(STATE_KEY) if isinstance(cfg.get(STATE_KEY), dict) else {}
            result["steps"].append({"id": "provision", "status": "planned"})

        voice = provision_twilio_voice_application(tenant, payload, app_config)
        state = merge_twilio_state(tenant, voice.get("state_patch") or {})
        cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
        cfg.update({key: value for key, value in (voice.get("tenant_config_patch") or {}).items() if value is not None})
        tenant.configuracion = cfg
        sync_twilio_provider_records(
            tenant,
            state,
            app_config=app_config,
            actor_user=actor_user,
            request_id=request_id,
            event_type="tenant_whatsapp_bootstrap_voice",
        )
        result["steps"].append({"id": "voice_app", "status": "done" if voice.get("ok", True) else "blocked", "result": voice})
    except Exception as exc:
        result.update({"ok": False, "status": "bootstrap_failed", "error": str(exc)})
        state = cfg.get(STATE_KEY) if isinstance(cfg.get(STATE_KEY), dict) else {}

    onboarding = refresh_tenant_whatsapp_onboarding(
        tenant,
        app_config=app_config,
        source=source,
    )
    result["status"] = onboarding["status"]
    result["onboarding"] = onboarding
    result["contract"] = build_twilio_tech_provider_contract(tenant, app_config)
    return result
