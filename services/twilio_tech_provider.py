from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
import base64

import requests


CONTRACT_VERSION = "twilio.tech_provider.v1"
STATE_KEY = "twilio_tech_provider"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _bool_config(config: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _backend_base_url(config: Mapping[str, Any]) -> str:
    return (
        _clean(config.get("PUBLIC_API_BASE_URL"))
        or _clean(config.get("BACKEND_URL"))
        or "https://www.chatboc.ar"
    ).rstrip("/")


def _env_status(config: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "TWILIO_ACCOUNT_SID": bool(_clean(config.get("TWILIO_ACCOUNT_SID"))),
        "TWILIO_AUTH_TOKEN": bool(_clean(config.get("TWILIO_AUTH_TOKEN"))),
        "TWILIO_META_APP_ID": bool(_clean(config.get("TWILIO_META_APP_ID"))),
        "TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID": bool(_clean(config.get("TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID"))),
    }
    optional = {
        "TWILIO_PARTNER_SOLUTION_ID": bool(_clean(config.get("TWILIO_PARTNER_SOLUTION_ID"))),
        "TWILIO_TECH_PROVIDER_LIVE_ENABLED": _bool_config(config, "TWILIO_TECH_PROVIDER_LIVE_ENABLED"),
    }
    return {
        "required": required,
        "optional": optional,
        "ready": all(required.values()),
        "missing": [key for key, ok in required.items() if not ok],
    }


def _twilio_basic_auth(account_sid: str, auth_token: str) -> str:
    raw = f"{account_sid}:{auth_token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _twilio_post_form(
    *,
    url: str,
    account_sid: str,
    auth_token: str,
    data: Mapping[str, Any],
    timeout: int = 20,
) -> dict[str, Any]:
    response = requests.post(
        url,
        data={key: value for key, value in data.items() if value is not None},
        headers={"Authorization": _twilio_basic_auth(account_sid, auth_token)},
        timeout=timeout,
    )
    try:
        payload = response.json()
    except Exception:
        payload = {"raw": response.text}
    if response.status_code >= 400:
        raise RuntimeError(f"twilio_api_error status={response.status_code} payload={payload}")
    return payload if isinstance(payload, dict) else {"payload": payload}


def build_twilio_tech_provider_contract(tenant, app_config: Mapping[str, Any]) -> dict[str, Any]:
    cfg = tenant.configuracion if isinstance(getattr(tenant, "configuracion", None), dict) else {}
    state = cfg.get(STATE_KEY) if isinstance(cfg.get(STATE_KEY), dict) else {}
    env = _env_status(app_config)
    base_url = _backend_base_url(app_config)
    tenant_slug = getattr(tenant, "slug", None)
    status = state.get("status") or ("ready_for_embedded_signup" if env["ready"] else "needs_platform_config")

    return {
        "contract_version": CONTRACT_VERSION,
        "provider": "twilio_tech_provider",
        "status": status,
        "tenant": {
            "id": getattr(tenant, "id", None),
            "slug": tenant_slug,
            "nombre": getattr(tenant, "nombre", None),
            "tipo": getattr(tenant, "tipo", None),
        },
        "automation": {
            "mode": "api_first",
            "live_enabled": _bool_config(app_config, "TWILIO_TECH_PROVIDER_LIVE_ENABLED"),
            "manual_twilio_console_allowed": False,
            "customer_sees_twilio_console": False,
            "env": env,
        },
        "state": {
            "twilio_account_sid": state.get("twilio_account_sid"),
            "messaging_service_sid": state.get("messaging_service_sid"),
            "sender_sid": state.get("sender_sid"),
            "sender_id": state.get("sender_id") or getattr(tenant, "whatsapp_sender_id", None),
            "waba_id": state.get("waba_id"),
            "last_step": state.get("last_step"),
            "updated_at": state.get("updated_at"),
        },
        "webhooks": {
            "inbound_message_url": f"{base_url}/webhook/whatsapp",
            "status_callback_url": f"{base_url}/twilio/whatsapp/status",
        },
        "embedded_signup": {
            "enabled": env["ready"],
            "meta_app_id": _clean(app_config.get("TWILIO_META_APP_ID")) or None,
            "configuration_id": _clean(app_config.get("TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID")) or None,
            "required_customer_action": "login_with_facebook_embedded_signup",
            "completion_endpoint": f"/api/v2/tenants/{tenant_slug}/whatsapp/tech-provider/embedded-signup",
        },
        "api_workflow": [
            {
                "id": "create_subaccount",
                "owner": "backend",
                "method": "POST",
                "endpoint": "https://api.twilio.com/2010-04-01/Accounts.json",
                "state": "done" if state.get("twilio_account_sid") else "pending",
            },
            {
                "id": "create_messaging_service",
                "owner": "backend",
                "method": "POST",
                "endpoint": "https://messaging.twilio.com/v1/Services",
                "state": "done" if state.get("messaging_service_sid") else "pending",
            },
            {
                "id": "assign_or_register_whatsapp_sender",
                "owner": "backend_after_embedded_signup",
                "method": "POST",
                "endpoint": "https://messaging.twilio.com/v2/Channels/Senders",
                "state": "done" if state.get("sender_sid") else "pending_meta_signup",
            },
            {
                "id": "poll_sender_status",
                "owner": "backend",
                "method": "GET",
                "endpoint": "https://messaging.twilio.com/v2/Channels/Senders/{SenderSid}",
                "state": "online" if state.get("sender_status") == "ONLINE" else "pending",
            },
        ],
        "frontend_contract": {
            "render_as": "twilio_tech_provider_onboarding",
            "show_twilio_brand": False,
            "show_manual_console_steps": False,
            "primary_action": "start_embedded_signup" if env["ready"] else "complete_platform_config",
            "show_phone_choice": True,
            "show_progress_steps": True,
        },
        "limitations": [
            "Meta Embedded Signup sigue siendo accion del cliente dentro del panel Chatboc.",
            "El aviso de partner Twilio dentro de Meta Embedded Signup puede ser obligatorio por programa.",
            "Meta puede requerir OTP para validar propiedad del numero.",
            "La aprobacion del display name y business verification dependen de Meta.",
        ],
    }


def build_provisioning_request(tenant, payload: Mapping[str, Any], app_config: Mapping[str, Any]) -> dict[str, Any]:
    cfg = tenant.configuracion if isinstance(getattr(tenant, "configuracion", None), dict) else {}
    state = cfg.get(STATE_KEY) if isinstance(cfg.get(STATE_KEY), dict) else {}
    requested_phone = _clean(payload.get("phone_number") or payload.get("whatsapp_number") or payload.get("sender_id"))
    if requested_phone.startswith("whatsapp:"):
        requested_phone = requested_phone.replace("whatsapp:", "", 1)
    if requested_phone and not requested_phone.startswith("+"):
        requested_phone = f"+{_digits(requested_phone)}"
    display_name = _clean(payload.get("display_name") or getattr(tenant, "nombre", None))
    base_url = _backend_base_url(app_config)
    return {
        "friendly_name": f"Chatboc - {getattr(tenant, 'slug', getattr(tenant, 'id', 'tenant'))}",
        "display_name": display_name,
        "phone_number": requested_phone or None,
        "webhook_url": f"{base_url}/webhook/whatsapp",
        "status_callback_url": f"{base_url}/twilio/whatsapp/status",
        "existing_state": state,
    }


def provision_twilio_subaccount(tenant, payload: Mapping[str, Any], app_config: Mapping[str, Any]) -> dict[str, Any]:
    request_payload = build_provisioning_request(tenant, payload, app_config)
    live_enabled = _bool_config(app_config, "TWILIO_TECH_PROVIDER_LIVE_ENABLED")
    account_sid = _clean(app_config.get("TWILIO_ACCOUNT_SID"))
    auth_token = _clean(app_config.get("TWILIO_AUTH_TOKEN"))
    env = _env_status(app_config)

    result = {
        "contract_version": "twilio.tech_provider.provisioning.v1",
        "ok": True,
        "mode": "live" if live_enabled else "dry_run",
        "request": request_payload,
        "steps": [],
        "state_patch": {
            "status": "provisioning_plan_ready",
            "last_step": "plan_ready",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "requested_phone_number": request_payload.get("phone_number"),
            "display_name": request_payload.get("display_name"),
        },
    }

    if not live_enabled:
        result["steps"].append({"id": "create_subaccount", "status": "planned"})
        result["steps"].append({"id": "create_messaging_service", "status": "planned"})
        result["steps"].append({"id": "embedded_signup", "status": "requires_customer"})
        result["steps"].append({"id": "register_sender", "status": "planned_after_embedded_signup"})
        return result

    if not (account_sid and auth_token):
        result["ok"] = False
        result["mode"] = "blocked"
        result["reason_code"] = "twilio_credentials_missing"
        result["missing_env"] = env["missing"]
        return result

    subaccount = _twilio_post_form(
        url="https://api.twilio.com/2010-04-01/Accounts.json",
        account_sid=account_sid,
        auth_token=auth_token,
        data={"FriendlyName": request_payload["friendly_name"]},
    )
    subaccount_sid = subaccount.get("sid")
    subaccount_token = subaccount.get("auth_token")
    result["steps"].append({"id": "create_subaccount", "status": "done", "sid": subaccount_sid})
    result["state_patch"].update(
        {
            "status": "subaccount_created",
            "last_step": "create_subaccount",
            "twilio_account_sid": subaccount_sid,
            "twilio_subaccount_token_present": bool(subaccount_token),
        }
    )
    # We intentionally stop here unless the platform is explicitly extended to
    # store subaccount credentials securely. Messaging API subdomains require
    # subaccount credentials or subaccount API keys, so continuing without a
    # secure secret store would create operational risk.
    result["steps"].append({"id": "create_messaging_service", "status": "blocked_secure_secret_store_required"})
    return result


def merge_twilio_state(tenant, state_patch: Mapping[str, Any]) -> dict[str, Any]:
    cfg = tenant.configuracion if isinstance(getattr(tenant, "configuracion", None), dict) else {}
    existing = cfg.get(STATE_KEY) if isinstance(cfg.get(STATE_KEY), dict) else {}
    merged = dict(existing)
    merged.update({key: value for key, value in state_patch.items() if value is not None})
    cfg[STATE_KEY] = merged
    tenant.configuracion = cfg
    return merged
