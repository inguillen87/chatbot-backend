from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlencode
import base64
import os
import re

import requests

from services.render_env_sync import sync_render_env_var


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


def _frontend_base_url(config: Mapping[str, Any]) -> str:
    return (
        _clean(config.get("PUBLIC_FRONTEND_URL"))
        or _clean(config.get("FRONTEND_URL"))
        or _clean(config.get("WEB_APP_URL"))
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


def _twilio_post_json(
    *,
    url: str,
    account_sid: str,
    auth_token: str,
    payload: Mapping[str, Any],
    timeout: int = 20,
) -> dict[str, Any]:
    response = requests.post(
        url,
        json={key: value for key, value in payload.items() if value is not None},
        headers={
            "Authorization": _twilio_basic_auth(account_sid, auth_token),
            "Content-Type": "application/json; charset=utf-8",
        },
        timeout=timeout,
    )
    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text}
    if response.status_code >= 400:
        raise RuntimeError(f"twilio_api_error status={response.status_code} payload={body}")
    return body if isinstance(body, dict) else {"payload": body}


def _twilio_get_json(
    *,
    url: str,
    account_sid: str,
    auth_token: str,
    timeout: int = 20,
) -> dict[str, Any]:
    response = requests.get(
        url,
        headers={"Authorization": _twilio_basic_auth(account_sid, auth_token)},
        timeout=timeout,
    )
    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text}
    if response.status_code >= 400:
        raise RuntimeError(f"twilio_api_error status={response.status_code} payload={body}")
    return body if isinstance(body, dict) else {"payload": body}


def _safe_env_suffix(value: Any) -> str:
    raw = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "").strip()).strip("_")
    return raw.upper() or "TENANT"


def _subaccount_token_ref_names(account_sid: str | None, tenant_slug: str | None = None) -> list[str]:
    names: list[str] = []
    if account_sid:
        names.append(f"TWILIO_SUBACCOUNT_AUTH_TOKEN_{_safe_env_suffix(account_sid)}")
    if tenant_slug:
        names.append(f"TWILIO_SUBACCOUNT_AUTH_TOKEN_{_safe_env_suffix(tenant_slug)}")
    names.append("TWILIO_SUBACCOUNT_AUTH_TOKEN")
    return list(dict.fromkeys(names))


def _read_config_or_env(config: Mapping[str, Any], key: str) -> str:
    return _clean(config.get(key)) or _clean(os.environ.get(key))


def _resolve_subaccount_auth_token(
    *,
    state: Mapping[str, Any],
    tenant_slug: str | None,
    app_config: Mapping[str, Any],
) -> tuple[str | None, list[str]]:
    subaccount_sid = _clean(state.get("twilio_account_sid"))
    candidates: list[str] = []
    explicit_ref = _clean(state.get("twilio_subaccount_token_ref"))
    if explicit_ref:
        candidates.append(explicit_ref)
    for alias in state.get("twilio_subaccount_token_ref_aliases") or []:
        alias_key = _clean(alias)
        if alias_key:
            candidates.append(alias_key)
    candidates.extend(_subaccount_token_ref_names(subaccount_sid, tenant_slug))
    candidates = list(dict.fromkeys(candidates))
    for key in candidates:
        token = _read_config_or_env(app_config, key)
        if token:
            return token, candidates
    return None, candidates


def _twilio_sender_response_sid(payload: Mapping[str, Any]) -> str | None:
    return _clean(payload.get("sid") or payload.get("Sid")) or None


def _twilio_sender_response_status(payload: Mapping[str, Any]) -> str | None:
    return _clean(payload.get("status") or payload.get("Status")) or None


def _twilio_sender_response_id(payload: Mapping[str, Any]) -> str | None:
    return _clean(payload.get("sender_id") or payload.get("senderId") or payload.get("sender") or payload.get("Sender")) or None


def _twilio_application_response_sid(payload: Mapping[str, Any]) -> str | None:
    return _clean(payload.get("sid") or payload.get("Sid")) or None


def _normalize_whatsapp_sender_id(value: Any) -> str | None:
    raw = _clean(value)
    if not raw:
        return None
    if raw.startswith("whatsapp:"):
        phone = raw.replace("whatsapp:", "", 1)
    else:
        phone = raw
    if not phone.startswith("+"):
        phone = f"+{_digits(phone)}"
    return f"whatsapp:{phone}" if phone and phone != "+" else None


def _profile_from_payload(tenant, payload: Mapping[str, Any], request_payload: Mapping[str, Any]) -> dict[str, Any]:
    profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
    public_url = _clean(payload.get("website") or getattr(tenant, "dominio", None) or "https://www.chatboc.ar")
    if public_url and not public_url.startswith(("http://", "https://")):
        public_url = f"https://{public_url}"
    display_name = _clean(
        profile.get("name")
        or payload.get("display_name")
        or request_payload.get("display_name")
        or getattr(tenant, "nombre", None)
    )
    vertical = _clean(profile.get("vertical") or payload.get("vertical"))
    if not vertical:
        tenant_vertical = _clean(getattr(tenant, "vertical", None) or getattr(tenant, "tipo", None))
        vertical = "Education" if tenant_vertical == "educacion" else "Public Service" if tenant_vertical == "municipio" else "Professional Services"
    result = {
        "name": display_name,
        "about": _clean(profile.get("about") or payload.get("about") or f"Canal oficial de {display_name}."),
        "description": _clean(profile.get("description") or payload.get("description") or f"Atencion automatizada y humana de {display_name}."),
        "vertical": vertical,
    }
    if public_url:
        result["websites"] = [{"website": public_url, "label": "Sitio web"}]
        result["privacy_url"] = _clean(profile.get("privacy_url") or payload.get("privacy_url") or f"{public_url.rstrip('/')}/privacidad")
        result["terms_of_service_url"] = _clean(
            profile.get("terms_of_service_url") or payload.get("terms_of_service_url") or f"{public_url.rstrip('/')}/terminos"
        )
    logo_url = _clean(profile.get("logo_url") or payload.get("logo_url") or getattr(tenant, "logo_url", None))
    if logo_url:
        result["logo_url"] = logo_url
    return {key: value for key, value in result.items() if value}


def build_twilio_tech_provider_contract(tenant, app_config: Mapping[str, Any]) -> dict[str, Any]:
    cfg = tenant.configuracion if isinstance(getattr(tenant, "configuracion", None), dict) else {}
    state = cfg.get(STATE_KEY) if isinstance(cfg.get(STATE_KEY), dict) else {}
    env = _env_status(app_config)
    base_url = _backend_base_url(app_config)
    frontend_url = _frontend_base_url(app_config)
    tenant_slug = getattr(tenant, "slug", None)
    meta_app_id = _clean(app_config.get("TWILIO_META_APP_ID")) or None
    embedded_signup_config_id = _clean(app_config.get("TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID")) or None
    status = state.get("status") or ("ready_for_embedded_signup" if env["ready"] else "needs_platform_config")
    signup_query = urlencode(
        {
            "tenant": tenant_slug or "",
            "app_id": meta_app_id or "",
            "config_id": embedded_signup_config_id or "",
        }
    )
    embedded_signup_start_url = f"{frontend_url}/integracion/whatsapp/connect?{signup_query}"

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
            "tenant_auto_bootstrap_enabled": _bool_config(app_config, "TWILIO_TENANT_AUTO_BOOTSTRAP_ENABLED", True),
            "tenant_auto_provision_enabled": _bool_config(app_config, "TWILIO_TENANT_AUTO_PROVISION_ENABLED"),
            "render_env_sync_enabled": _bool_config(app_config, "RENDER_ENV_SYNC_ENABLED"),
            "manual_twilio_console_allowed": False,
            "customer_sees_twilio_console": False,
            "env": env,
        },
        "state": {
            "twilio_account_sid": state.get("twilio_account_sid"),
            "messaging_service_sid": state.get("messaging_service_sid"),
            "sender_sid": state.get("sender_sid"),
            "sender_id": state.get("sender_id") or getattr(tenant, "whatsapp_sender_id", None),
            "sender_status": state.get("sender_status"),
            "waba_id": state.get("waba_id"),
            "phone_number_id": state.get("phone_number_id"),
            "last_step": state.get("last_step"),
            "updated_at": state.get("updated_at"),
        },
        "tenant_onboarding": cfg.get("whatsapp_onboarding") if isinstance(cfg.get("whatsapp_onboarding"), dict) else None,
        "webhooks": {
            "inbound_message_url": f"{base_url}/webhook/whatsapp",
            "status_callback_url": f"{base_url}/twilio/whatsapp/status",
        },
        "voice": {
            "status": state.get("voice_status") or ("ready" if state.get("voice_twiml_app_sid") else "pending"),
            "twiml_app_sid": state.get("voice_twiml_app_sid") or cfg.get("voice_twiml_app_sid"),
            "voice_url": state.get("voice_url") or cfg.get("voice_url") or f"{base_url}/twilio/voice?tenant={tenant_slug}",
            "fallback_url": state.get("voice_fallback_url") or cfg.get("voice_fallback_url") or f"{base_url}/voice/fallback?tenant={tenant_slug}",
            "status_callback_url": state.get("voice_status_callback_url") or cfg.get("voice_status_callback_url") or f"{base_url}/voice/status",
            "vertical": state.get("voice_vertical") or cfg.get("voice_vertical"),
            "intent": state.get("voice_intent") or cfg.get("voice_intent"),
            "completion_endpoint": f"/api/v2/tenants/{tenant_slug}/whatsapp/tech-provider/voice-app",
        },
        "embedded_signup": {
            "enabled": env["ready"],
            "meta_app_id": meta_app_id,
            "configuration_id": embedded_signup_config_id,
            "start_url": embedded_signup_start_url if env["ready"] else None,
            "url": embedded_signup_start_url if env["ready"] else None,
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
                "id": "sync_subaccount_secret_to_render",
                "owner": "backend_secret_store",
                "method": "PUT",
                "endpoint": "https://api.render.com/v1/services/{ServiceId}/env-vars/{EnvVarKey}",
                "state": (
                    "done"
                    if state.get("render_subaccount_secret_synced")
                    else "enabled"
                    if _bool_config(app_config, "RENDER_ENV_SYNC_ENABLED")
                    else "manual_or_disabled"
                ),
            },
            {
                "id": "assign_or_register_whatsapp_sender",
                "owner": "backend_after_embedded_signup",
                "method": "POST",
                "endpoint": "https://messaging.twilio.com/v2/Channels/Senders",
                "state": "done" if state.get("sender_sid") else "pending_meta_signup",
            },
            {
                "id": "attach_sender_to_messaging_service",
                "owner": "backend_after_sender_registration",
                "method": "POST",
                "endpoint": "https://messaging.twilio.com/v1/Services/{MessagingServiceSid}/ChannelSenders",
                "state": "done" if state.get("channel_sender_attached") else "pending_sender",
            },
            {
                "id": "create_or_update_voice_twiml_app",
                "owner": "backend",
                "method": "POST",
                "endpoint": "https://api.twilio.com/2010-04-01/Accounts/{AccountSid}/Applications.json",
                "state": "done" if state.get("voice_twiml_app_sid") else "pending",
            },
            {
                "id": "attach_voice_app_to_sender",
                "owner": "backend_after_sender_registration",
                "method": "POST",
                "endpoint": "https://messaging.twilio.com/v2/Channels/Senders/{SenderSid}",
                "state": "done" if state.get("voice_sender_attached") else "pending_sender_or_voice_app",
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


def _voice_vertical_for_tenant(tenant, payload: Mapping[str, Any]) -> str:
    requested = _clean(payload.get("vertical") or payload.get("sector")).lower()
    if requested:
        aliases = {
            "gobierno": "municipio",
            "juni": "municipio",
            "junin": "municipio",
            "school": "educacion",
            "colegio": "educacion",
            "colegios": "educacion",
            "empresa": "pyme",
            "empresas": "pyme",
            "sales": "ventas",
        }
        return aliases.get(requested, requested)
    tenant_type = _clean(getattr(tenant, "tipo", None)).lower()
    tenant_vertical = _clean(getattr(tenant, "vertical", None)).lower()
    if tenant_type == "municipio" or tenant_vertical in {"municipio", "gobierno"}:
        return "municipio"
    if tenant_type == "educacion" or tenant_vertical in {"educacion", "colegio"}:
        return "educacion"
    if tenant_vertical in {"ventas", "sales"}:
        return "ventas"
    return "pyme"


def _voice_intent_for_vertical(vertical: str, payload: Mapping[str, Any]) -> str:
    requested = _clean(payload.get("intent")).lower()
    if requested:
        return requested
    if vertical == "municipio":
        return "reclamos"
    if vertical == "educacion":
        return "secretaria"
    if vertical == "ventas":
        return "sales"
    return "atencion"


def build_voice_application_request(tenant, payload: Mapping[str, Any], app_config: Mapping[str, Any]) -> dict[str, Any]:
    cfg = tenant.configuracion if isinstance(getattr(tenant, "configuracion", None), dict) else {}
    state = cfg.get(STATE_KEY) if isinstance(cfg.get(STATE_KEY), dict) else {}
    base_url = _backend_base_url(app_config)
    tenant_slug = _clean(payload.get("tenant_slug") or getattr(tenant, "slug", None))
    vertical = _voice_vertical_for_tenant(tenant, payload)
    intent = _voice_intent_for_vertical(vertical, payload)
    app_sid = _clean(payload.get("voice_twiml_app_sid") or state.get("voice_twiml_app_sid") or cfg.get("voice_twiml_app_sid"))
    friendly_name = _clean(payload.get("friendly_name")) or f"Chatboc Voice - {tenant_slug or getattr(tenant, 'id', 'tenant')}"
    voice_url = _clean(payload.get("voice_url")) or f"{base_url}/twilio/voice?tenant={tenant_slug}&vertical={vertical}&intent={intent}"
    fallback_url = _clean(payload.get("voice_fallback_url")) or f"{base_url}/voice/fallback?tenant={tenant_slug}&vertical={vertical}&intent={intent}"
    status_callback_url = _clean(payload.get("voice_status_callback_url")) or f"{base_url}/voice/status"
    sender_sid = _clean(payload.get("sender_sid") or state.get("sender_sid"))
    sender_id = _normalize_whatsapp_sender_id(payload.get("sender_id") or state.get("sender_id") or getattr(tenant, "whatsapp_sender_id", None))
    return {
        "friendly_name": friendly_name[:64],
        "voice_twiml_app_sid": app_sid or None,
        "voice_url": voice_url,
        "voice_method": "POST",
        "voice_fallback_url": fallback_url,
        "voice_fallback_method": "POST",
        "voice_status_callback_url": status_callback_url,
        "voice_status_callback_method": "POST",
        "tenant_slug": tenant_slug,
        "vertical": vertical,
        "intent": intent,
        "sender_sid": sender_sid or None,
        "sender_id": sender_id,
        "existing_state": state,
    }


def _twilio_voice_account_credentials(
    *,
    tenant,
    state: Mapping[str, Any],
    app_config: Mapping[str, Any],
) -> tuple[str | None, str | None, list[str]]:
    subaccount_sid = _clean(state.get("twilio_account_sid"))
    tenant_slug = getattr(tenant, "slug", None)
    if subaccount_sid:
        token, token_refs = _resolve_subaccount_auth_token(
            state=state,
            tenant_slug=tenant_slug,
            app_config=app_config,
        )
        if token:
            return subaccount_sid, token, token_refs
        return subaccount_sid, None, token_refs
    parent_sid = _read_config_or_env(app_config, "TWILIO_ACCOUNT_SID")
    parent_token = _read_config_or_env(app_config, "TWILIO_AUTH_TOKEN")
    return parent_sid or None, parent_token or None, ["TWILIO_AUTH_TOKEN"]


def provision_twilio_voice_application(tenant, payload: Mapping[str, Any], app_config: Mapping[str, Any]) -> dict[str, Any]:
    cfg = tenant.configuracion if isinstance(getattr(tenant, "configuracion", None), dict) else {}
    state = cfg.get(STATE_KEY) if isinstance(cfg.get(STATE_KEY), dict) else {}
    request_payload = build_voice_application_request(tenant, payload, app_config)
    live_enabled = _bool_config(app_config, "TWILIO_TECH_PROVIDER_LIVE_ENABLED") or _bool_config(
        app_config,
        "TWILIO_VOICE_PROVISIONING_LIVE_ENABLED",
    )
    now = datetime.now(timezone.utc).isoformat()
    account_sid, auth_token, token_refs = _twilio_voice_account_credentials(
        tenant=tenant,
        state=state,
        app_config=app_config,
    )

    result: dict[str, Any] = {
        "contract_version": "twilio.tech_provider.voice_application.v1",
        "ok": True,
        "mode": "live" if live_enabled else "dry_run",
        "request": request_payload,
        "steps": [],
        "state_patch": {
            "updated_at": now,
            "voice_status": "voice_application_plan_ready",
            "voice_last_step": "plan_ready",
            "voice_url": request_payload["voice_url"],
            "voice_fallback_url": request_payload["voice_fallback_url"],
            "voice_status_callback_url": request_payload["voice_status_callback_url"],
            "voice_vertical": request_payload["vertical"],
            "voice_intent": request_payload["intent"],
            "voice_account_sid": account_sid,
        },
        "tenant_config_patch": {
            "voice_twiml_app_sid": request_payload.get("voice_twiml_app_sid"),
            "voice_url": request_payload["voice_url"],
            "voice_fallback_url": request_payload["voice_fallback_url"],
            "voice_status_callback_url": request_payload["voice_status_callback_url"],
            "voice_vertical": request_payload["vertical"],
            "voice_intent": request_payload["intent"],
        },
    }

    if not live_enabled:
        result["steps"].append({"id": "create_or_update_voice_twiml_app", "status": "planned"})
        result["steps"].append({"id": "attach_voice_app_to_sender", "status": "planned" if request_payload.get("sender_sid") else "pending_sender"})
        return result

    if not (account_sid and auth_token):
        result["ok"] = False
        result["mode"] = "blocked"
        result["reason_code"] = "twilio_voice_credentials_missing"
        result["required_env"] = token_refs
        result["state_patch"].update({"voice_status": "voice_application_blocked", "voice_last_step": "resolve_credentials"})
        return result

    app_sid = request_payload.get("voice_twiml_app_sid")
    app_data = {
        "FriendlyName": request_payload["friendly_name"],
        "VoiceUrl": request_payload["voice_url"],
        "VoiceMethod": request_payload["voice_method"],
        "VoiceFallbackUrl": request_payload["voice_fallback_url"],
        "VoiceFallbackMethod": request_payload["voice_fallback_method"],
        "StatusCallback": request_payload["voice_status_callback_url"],
        "StatusCallbackMethod": request_payload["voice_status_callback_method"],
    }
    app_url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Applications/{app_sid}.json"
        if app_sid
        else f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Applications.json"
    )
    try:
        app_response = _twilio_post_form(
            url=app_url,
            account_sid=account_sid,
            auth_token=auth_token,
            data=app_data,
        )
    except Exception as exc:
        result["ok"] = False
        result["mode"] = "blocked"
        result["reason_code"] = "twilio_voice_application_upsert_failed"
        result["error"] = str(exc)
        result["steps"].append({"id": "create_or_update_voice_twiml_app", "status": "failed"})
        result["state_patch"].update({"voice_status": "voice_application_failed", "voice_last_step": "upsert_voice_twiml_app"})
        return result

    created_sid = _twilio_application_response_sid(app_response) or app_sid
    result["steps"].append(
        {
            "id": "create_or_update_voice_twiml_app",
            "status": "done",
            "sid": created_sid,
            "operation": "update" if app_sid else "create",
        }
    )
    result["state_patch"].update(
        {
            "voice_status": "voice_application_ready",
            "voice_last_step": "upsert_voice_twiml_app",
            "voice_twiml_app_sid": created_sid,
        }
    )
    result["tenant_config_patch"]["voice_twiml_app_sid"] = created_sid

    sender_sid = request_payload.get("sender_sid")
    if sender_sid and created_sid:
        try:
            sender_response = _twilio_post_json(
                url=f"https://messaging.twilio.com/v2/Channels/Senders/{sender_sid}",
                account_sid=account_sid,
                auth_token=auth_token,
                payload={"configuration": {"voice_application_sid": created_sid}},
            )
        except Exception as exc:
            result["ok"] = False
            result["mode"] = "blocked"
            result["reason_code"] = "twilio_voice_sender_attach_failed"
            result["error"] = str(exc)
            result["steps"].append({"id": "attach_voice_app_to_sender", "status": "failed"})
            result["state_patch"].update({"voice_status": "voice_sender_attach_failed", "voice_last_step": "attach_voice_app_to_sender"})
            return result

        result["steps"].append(
            {
                "id": "attach_voice_app_to_sender",
                "status": "done",
                "sid": _twilio_sender_response_sid(sender_response) or sender_sid,
            }
        )
        result["state_patch"].update(
            {
                "voice_status": "voice_sender_attached",
                "voice_last_step": "attach_voice_app_to_sender",
                "voice_sender_attached": True,
            }
        )
    else:
        result["steps"].append({"id": "attach_voice_app_to_sender", "status": "pending_sender"})
        result["state_patch"].update({"voice_sender_attached": False})

    return result


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

    try:
        subaccount = _twilio_post_form(
            url="https://api.twilio.com/2010-04-01/Accounts.json",
            account_sid=account_sid,
            auth_token=auth_token,
            data={"FriendlyName": request_payload["friendly_name"]},
        )
    except Exception as exc:
        result["ok"] = False
        result["mode"] = "blocked"
        result["reason_code"] = "twilio_subaccount_creation_failed"
        result["error"] = str(exc)
        result["state_patch"].update({"status": "provisioning_failed", "last_step": "create_subaccount"})
        return result

    subaccount_sid = subaccount.get("sid")
    subaccount_token = subaccount.get("auth_token")
    token_refs = _subaccount_token_ref_names(subaccount_sid, getattr(tenant, "slug", None))
    result["steps"].append({"id": "create_subaccount", "status": "done", "sid": subaccount_sid})
    result["state_patch"].update(
        {
            "status": "subaccount_created" if not subaccount_token else "creating_messaging_service",
            "last_step": "create_subaccount",
            "twilio_account_sid": subaccount_sid,
            "twilio_subaccount_token_present": bool(subaccount_token),
            "twilio_subaccount_token_ref": token_refs[0],
            "twilio_subaccount_token_ref_aliases": token_refs[1:],
        }
    )

    if not (subaccount_sid and subaccount_token):
        result["ok"] = False
        result["mode"] = "blocked"
        result["reason_code"] = "twilio_subaccount_token_missing"
        result["steps"].append({"id": "create_messaging_service", "status": "blocked_subaccount_token_missing"})
        return result

    try:
        messaging_service = _twilio_post_form(
            url="https://messaging.twilio.com/v1/Services",
            account_sid=subaccount_sid,
            auth_token=subaccount_token,
            data={
                "FriendlyName": request_payload["friendly_name"][:64],
                "InboundRequestUrl": request_payload["webhook_url"],
                "InboundMethod": "POST",
                "StatusCallback": request_payload["status_callback_url"],
                "UseInboundWebhookOnNumber": "false",
                "Usecase": "notifications",
            },
        )
    except Exception as exc:
        result["ok"] = False
        result["mode"] = "blocked"
        result["reason_code"] = "twilio_messaging_service_creation_failed"
        result["error"] = str(exc)
        result["steps"].append({"id": "create_messaging_service", "status": "failed"})
        result["state_patch"].update({"status": "messaging_service_failed", "last_step": "create_messaging_service"})
        return result

    messaging_service_sid = messaging_service.get("sid")
    render_env_sync = sync_render_env_var(token_refs[0], subaccount_token, app_config)
    result["steps"].append({"id": "create_messaging_service", "status": "done", "sid": messaging_service_sid})
    result["steps"].append({"id": "embedded_signup", "status": "requires_customer"})
    result["steps"].append({"id": "register_sender", "status": "planned_after_embedded_signup"})
    result["secure_secret_required"] = {
        "reason_code": "store_subaccount_auth_token_for_later_sender_registration",
        "required_env": token_refs,
        "render_env_sync": render_env_sync,
        "do_not_store_in_database": True,
    }
    result["state_patch"].update(
        {
            "status": "ready_for_embedded_signup",
            "last_step": "create_messaging_service",
            "messaging_service_sid": messaging_service_sid,
            "render_subaccount_secret_synced": bool(render_env_sync.get("secret_value_stored")),
            "render_subaccount_secret_sync_status": render_env_sync.get("mode"),
        }
    )
    return result


def register_whatsapp_sender(tenant, payload: Mapping[str, Any], app_config: Mapping[str, Any]) -> dict[str, Any]:
    cfg = tenant.configuracion if isinstance(getattr(tenant, "configuracion", None), dict) else {}
    state = cfg.get(STATE_KEY) if isinstance(cfg.get(STATE_KEY), dict) else {}
    request_payload = build_provisioning_request(tenant, payload, app_config)
    live_enabled = _bool_config(app_config, "TWILIO_TECH_PROVIDER_LIVE_ENABLED")
    subaccount_sid = _clean(state.get("twilio_account_sid"))
    messaging_service_sid = _clean(state.get("messaging_service_sid"))
    tenant_slug = getattr(tenant, "slug", None)
    token, token_refs = _resolve_subaccount_auth_token(
        state=state,
        tenant_slug=tenant_slug,
        app_config=app_config,
    )
    sender_id = _normalize_whatsapp_sender_id(
        payload.get("sender_id")
        or payload.get("phone_number")
        or payload.get("whatsapp_number")
        or state.get("requested_phone_number")
        or getattr(tenant, "whatsapp_sender_id", None)
    )
    waba_id = _clean(payload.get("waba_id") or payload.get("wabaId") or state.get("waba_id"))
    verification_method = _clean(payload.get("verification_method") or payload.get("verificationMethod") or "sms")
    verification_code = _clean(payload.get("verification_code") or payload.get("verificationCode"))
    now = datetime.now(timezone.utc).isoformat()

    result: dict[str, Any] = {
        "contract_version": "twilio.tech_provider.sender_registration.v1",
        "ok": True,
        "mode": "live" if live_enabled else "dry_run",
        "steps": [],
        "state_patch": {
            "updated_at": now,
            "requested_phone_number": (sender_id or "").replace("whatsapp:", "", 1) or state.get("requested_phone_number"),
            "sender_id": sender_id,
            "waba_id": waba_id,
            "phone_number_id": payload.get("phone_number_id") or payload.get("phoneNumberId") or state.get("phone_number_id"),
        },
    }

    missing = []
    if not subaccount_sid:
        missing.append("twilio_account_sid")
    if not messaging_service_sid:
        missing.append("messaging_service_sid")
    if not sender_id:
        missing.append("sender_id")
    if not waba_id:
        missing.append("waba_id")
    if live_enabled and not token:
        missing.append("twilio_subaccount_auth_token")

    if missing:
        result["ok"] = False
        result["mode"] = "blocked"
        result["reason_code"] = "sender_registration_prerequisites_missing"
        result["missing"] = missing
        if "twilio_subaccount_auth_token" in missing:
            result["required_env"] = token_refs
        result["state_patch"].update({"status": "sender_registration_blocked", "last_step": "register_sender"})
        return result

    if not live_enabled:
        result["steps"].append({"id": "register_sender", "status": "planned"})
        result["steps"].append({"id": "attach_sender_to_messaging_service", "status": "planned"})
        result["state_patch"].update({"status": "sender_registration_plan_ready", "last_step": "register_sender_plan"})
        return result

    sender_payload: dict[str, Any] = {
        "sender_id": sender_id,
        "configuration": {
            "waba_id": waba_id,
            "verification_method": verification_method,
        },
        "webhook": {
            "callback_url": request_payload["webhook_url"],
            "callback_method": "POST",
            "status_callback_url": request_payload["status_callback_url"],
            "status_callback_method": "POST",
        },
        "profile": _profile_from_payload(tenant, payload, request_payload),
    }
    if verification_code:
        sender_payload["configuration"]["verification_code"] = verification_code

    try:
        sender = _twilio_post_json(
            url="https://messaging.twilio.com/v2/Channels/Senders",
            account_sid=subaccount_sid,
            auth_token=token or "",
            payload=sender_payload,
        )
    except Exception as exc:
        result["ok"] = False
        result["mode"] = "blocked"
        result["reason_code"] = "twilio_sender_registration_failed"
        result["error"] = str(exc)
        result["steps"].append({"id": "register_sender", "status": "failed"})
        result["state_patch"].update({"status": "sender_registration_failed", "last_step": "register_sender"})
        return result

    sender_sid = _twilio_sender_response_sid(sender)
    sender_status = _twilio_sender_response_status(sender)
    sender_response_id = _twilio_sender_response_id(sender) or sender_id
    result["steps"].append({"id": "register_sender", "status": "done", "sid": sender_sid, "sender_status": sender_status})
    result["state_patch"].update(
        {
            "status": "sender_registered",
            "last_step": "register_sender",
            "sender_sid": sender_sid,
            "sender_status": sender_status,
            "sender_id": sender_response_id,
        }
    )

    try:
        channel_sender = _twilio_post_form(
            url=f"https://messaging.twilio.com/v1/Services/{messaging_service_sid}/ChannelSenders",
            account_sid=subaccount_sid,
            auth_token=token or "",
            data={"Sid": sender_sid},
        )
    except Exception as exc:
        result["ok"] = False
        result["mode"] = "blocked"
        result["reason_code"] = "twilio_channel_sender_attach_failed"
        result["error"] = str(exc)
        result["steps"].append({"id": "attach_sender_to_messaging_service", "status": "failed"})
        result["state_patch"].update({"status": "sender_attach_failed", "last_step": "attach_sender_to_messaging_service"})
        return result

    result["steps"].append(
        {
            "id": "attach_sender_to_messaging_service",
            "status": "done",
            "sid": _twilio_sender_response_sid(channel_sender) or sender_sid,
        }
    )
    result["state_patch"].update(
        {
            "status": "sender_attached",
            "last_step": "attach_sender_to_messaging_service",
            "channel_sender_attached": True,
        }
    )
    return result


def poll_whatsapp_sender_status(tenant, app_config: Mapping[str, Any]) -> dict[str, Any]:
    cfg = tenant.configuracion if isinstance(getattr(tenant, "configuracion", None), dict) else {}
    state = cfg.get(STATE_KEY) if isinstance(cfg.get(STATE_KEY), dict) else {}
    live_enabled = _bool_config(app_config, "TWILIO_TECH_PROVIDER_LIVE_ENABLED")
    subaccount_sid = _clean(state.get("twilio_account_sid"))
    sender_sid = _clean(state.get("sender_sid"))
    token, token_refs = _resolve_subaccount_auth_token(
        state=state,
        tenant_slug=getattr(tenant, "slug", None),
        app_config=app_config,
    )
    result: dict[str, Any] = {
        "contract_version": "twilio.tech_provider.sender_status.v1",
        "ok": True,
        "mode": "live" if live_enabled else "dry_run",
        "state_patch": {"updated_at": datetime.now(timezone.utc).isoformat()},
    }
    missing = []
    if not subaccount_sid:
        missing.append("twilio_account_sid")
    if not sender_sid:
        missing.append("sender_sid")
    if live_enabled and not token:
        missing.append("twilio_subaccount_auth_token")
    if missing:
        result["ok"] = False
        result["mode"] = "blocked"
        result["reason_code"] = "sender_status_prerequisites_missing"
        result["missing"] = missing
        if "twilio_subaccount_auth_token" in missing:
            result["required_env"] = token_refs
        return result
    if not live_enabled:
        result["state_patch"].update({"last_step": "poll_sender_status_plan"})
        return result
    try:
        sender = _twilio_get_json(
            url=f"https://messaging.twilio.com/v2/Channels/Senders/{sender_sid}",
            account_sid=subaccount_sid,
            auth_token=token or "",
        )
    except Exception as exc:
        result["ok"] = False
        result["mode"] = "blocked"
        result["reason_code"] = "twilio_sender_status_failed"
        result["error"] = str(exc)
        return result

    status = _twilio_sender_response_status(sender)
    sender_id = _twilio_sender_response_id(sender)
    result["sender"] = sender
    result["state_patch"].update(
        {
            "status": "sender_online" if status == "ONLINE" else "sender_pending",
            "last_step": "poll_sender_status",
            "sender_status": status,
            "sender_id": sender_id or state.get("sender_id"),
        }
    )
    return result


def merge_twilio_state(tenant, state_patch: Mapping[str, Any]) -> dict[str, Any]:
    cfg = tenant.configuracion if isinstance(getattr(tenant, "configuracion", None), dict) else {}
    existing = cfg.get(STATE_KEY) if isinstance(cfg.get(STATE_KEY), dict) else {}
    merged = dict(existing)
    merged.update({key: value for key, value in state_patch.items() if value is not None})
    cfg[STATE_KEY] = merged
    tenant.configuracion = cfg
    return merged
