"""Public operation availability; configuration is not delivery acceptance.

No provider I/O, token decryption, mutations or cached ORM credential lookup.
The legacy token candidate list is shared with its actual executor resolver.
"""
from __future__ import annotations

import os
import re
from typing import Any, Mapping

from services.tenant_provider_credentials import tenant_has_internal_provider_credentials


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _enabled(config: Mapping[str, Any], key: str) -> bool:
    return _clean(config.get(key)).lower() in {"1", "true", "yes", "on"}


def tenant_configuration_snapshot(tenant: Any) -> dict[str, Any]:
    """An expired tenant identity must not flush a dirty credential connection."""
    from contextlib import nullcontext
    from flask import has_app_context
    from models import db
    context = db.session.no_autoflush if has_app_context() else nullcontext()
    with context:
        configuration = getattr(tenant, "configuracion", None)
        return dict(configuration) if isinstance(configuration, dict) else {}


def subaccount_token_ref_names(account_sid: str | None, tenant_slug: str | None = None) -> list[str]:
    def suffix(value: Any) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "_", _clean(value)).strip("_").upper() or "TENANT"
    names = []
    if account_sid:
        names.append(f"TWILIO_SUBACCOUNT_AUTH_TOKEN_{suffix(account_sid)}")
    if tenant_slug:
        names.append(f"TWILIO_SUBACCOUNT_AUTH_TOKEN_{suffix(tenant_slug)}")
    names.append("TWILIO_SUBACCOUNT_AUTH_TOKEN")
    return list(dict.fromkeys(names))


def legacy_subaccount_token_candidates(
    state: Mapping[str, Any], tenant_slug: str | None, *, allow_global_fallback: bool = True,
) -> list[str]:
    candidates = [_clean(state.get("twilio_subaccount_token_ref"))]
    candidates.extend(_clean(value) for value in state.get("twilio_subaccount_token_ref_aliases") or [])
    candidates.extend(
        name for name in subaccount_token_ref_names(_clean(state.get("twilio_account_sid")), tenant_slug)
        if allow_global_fallback or name != "TWILIO_SUBACCOUNT_AUTH_TOKEN"
    )
    return list(dict.fromkeys(name for name in candidates if name))


def normalize_whatsapp_sender_id(value: Any) -> str | None:
    raw = _clean(value)
    if not raw:
        return None
    phone = raw.replace("whatsapp:", "", 1) if raw.startswith("whatsapp:") else raw
    if not phone.startswith("+"):
        phone = "+" + "".join(char for char in phone if char.isdigit())
    return f"whatsapp:{phone}" if phone and phone != "+" else None


def build_whatsapp_operation_availability(
    tenant: Any, state: Mapping[str, Any], app_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Describe supported operations with the same owned-vault guard as executors.

    Flags and existing IDs never implement live smoke or durable provisioning.
    Environment values are checked only for presence and are never returned.
    """
    owned_vault = tenant_has_internal_provider_credentials(tenant)
    live = _enabled(app_config, "TWILIO_TECH_PROVIDER_LIVE_ENABLED")
    voice_live = live or _enabled(app_config, "TWILIO_VOICE_PROVISIONING_LIVE_ENABLED")
    account = bool(_clean(state.get("twilio_account_sid")))
    # The owned-vault branch must stop before reading any fallback environment.
    subaccount_token = False
    parent_voice = False
    if not owned_vault:
        subaccount_token = any(
            bool(_clean(app_config.get(name)) or _clean(os.environ.get(name)))
            for name in legacy_subaccount_token_candidates(state, getattr(tenant, "slug", None))
        )
        parent_voice = all(
            bool(_clean(app_config.get(name)) or _clean(os.environ.get(name)))
            for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN")
        )

    def onboarding(*, prerequisites: bool, enabled: bool, missing_reason: str) -> dict[str, Any]:
        reason = (
            "twilio_vault_onboarding_integration_required" if owned_vault
            else "live_operation_disabled" if not enabled
            else missing_reason if not prerequisites else None
        )
        return {
            "implemented": True, "can_execute": reason is None,
            "dry_run_available": not owned_vault,
            "reason_code": reason,
            "message": (
                "Chatboc debe completar la activación segura de esta organización. La configuración registrada se conserva."
                if owned_vault else "La operación real no está habilitada; podés revisar su preparación."
                if not enabled else "Faltan requisitos para ejecutar esta operación." if reason else None
            ),
        }

    pending = {
        "implemented": False, "can_execute": False, "dry_run_available": True,
        "reason_code": "twilio_tenant_credential_provisioning_integration_required",
        "message": "Chatboc debe completar la activación segura antes de crear la conexión de WhatsApp.",
    }
    operations = {
        "create_subaccount": dict(pending), "create_messaging_service": dict(pending),
        "register_sender": onboarding(
            prerequisites=bool(account and _clean(state.get("messaging_service_sid"))
                               and normalize_whatsapp_sender_id(state.get("requested_phone_number") or getattr(tenant, "whatsapp_sender_id", None))
                               and _clean(state.get("waba_id")) and subaccount_token),
            enabled=live, missing_reason="sender_registration_prerequisites_missing",
        ),
        "poll_sender_status": onboarding(
            prerequisites=bool(account and _clean(state.get("sender_sid")) and subaccount_token),
            enabled=live, missing_reason="sender_status_prerequisites_missing",
        ),
        "prepare_voice": onboarding(
            prerequisites=subaccount_token if account else parent_voice,
            enabled=voice_live, missing_reason="twilio_voice_credentials_missing",
        ),
        "live_whatsapp_message": {
            "implemented": False, "can_execute": False, "dry_run_available": False,
            "reason_code": "execution_not_implemented",
            "message": "La prueba real todavía no está habilitada; la configuración registrada se conserva.",
        },
    }
    return {"contract_version": "whatsapp.operation_availability.v1", "operations": operations}
