"""Consent and lifecycle boundary for Twilio voice calls.

The Media Streams HMAC proves transport provenance.  It does *not* prove that
the caller consented to AI audio processing.  This service provides that
second, tenant-bound authorization from durable state.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from extensions import db
from models import ProviderSender, TenantProfile, WhatsappNumero
from models_voice_lifecycle import (
    VoiceCallLifecycle,
    VoiceCallLifecycleEvent,
)
from services.tenant_ticket_scope import (
    resolve_unique_tenant_for_owner,
    tenant_owner_ids,
)


_CALL_SID_RE = re.compile(r"^[A-Za-z0-9_-]{2,80}$")
_POLICY_VERSION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_TERMINAL_STATES = {"completed", "failed"}
_PROVIDER_STATUSES = {
    "queued",
    "initiated",
    "ringing",
    "in-progress",
    "answered",
    "completed",
    "busy",
    "failed",
    "no-answer",
    "canceled",
}
_FAILED_PROVIDER_STATUSES = {"busy", "failed", "no-answer", "canceled"}
_SAFE_FAILURE_REASONS = {
    "bridge_connect_failed",
    "openai_api_key_missing",
    "policy_disabled",
    "tenant_resolution_failed",
}


class VoiceConsentLifecycleError(RuntimeError):
    """A bounded, non-PII error safe for control-plane logs."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class VoiceConsentPolicy:
    version: str
    ai_processing: str
    recording_allowed: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def voice_consent_lifecycle_enabled(config: Mapping[str, Any] | None = None) -> bool:
    value = config.get("ENABLE_VOICE_CONSENT_LIFECYCLE_V1") if config is not None else None
    if value in (None, ""):
        value = os.environ.get("ENABLE_VOICE_CONSENT_LIFECYCLE_V1")
    return value is True or _truthy(value)


def normalize_voice_direction(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized.startswith("outbound"):
        return "outbound"
    if normalized in {"", "inbound"}:
        return "inbound"
    raise VoiceConsentLifecycleError("invalid_direction")


def _validate_call_sid(value: Any) -> str:
    call_sid = str(value or "").strip()
    if not _CALL_SID_RE.fullmatch(call_sid):
        raise VoiceConsentLifecycleError("invalid_call_sid")
    return call_sid


def _normalize_phone(value: Any) -> str:
    return str(value or "").replace("whatsapp:", "").replace(" ", "").strip()


def voice_phone_candidates(value: Any) -> set[str]:
    normalized = _normalize_phone(value)
    if not normalized:
        return set()
    compact = normalized.replace("+", "")
    return {
        normalized,
        compact,
        f"whatsapp:{normalized}",
        f"whatsapp:{compact}",
    }


def _tenant_for_legacy_owner(owner: Any) -> TenantProfile | None:
    if owner is None:
        return None
    owner_id = getattr(owner, "id", None)
    if not owner_id:
        return None
    try:
        owner_id = int(owner_id)
    except (TypeError, ValueError):
        return None

    tenant_id = getattr(owner, "tenant_id", None)
    if tenant_id:
        try:
            tenant = db.session.get(TenantProfile, int(tenant_id))
        except (TypeError, ValueError):
            return None
        return tenant if tenant is not None and owner_id in tenant_owner_ids(tenant) else None

    declared = [
        tenant
        for tenant in (
            getattr(owner, "tenant", None),
            getattr(owner, "tenant_profile", None),
            getattr(owner, "tenant_profile_municipio", None),
            getattr(owner, "tenant_profile_pyme", None),
        )
        if getattr(tenant, "id", None) is not None
    ]
    declared_ids = {int(tenant.id) for tenant in declared}
    if len(declared_ids) == 1:
        tenant = declared[0]
        return tenant if owner_id in tenant_owner_ids(tenant) else None
    if declared_ids:
        return None

    try:
        resolution = resolve_unique_tenant_for_owner(owner_id)
    except ValueError:
        return None
    return resolution.tenant if resolution.status == "unique" else None


def _mapped_tenants_for_number(value: Any) -> dict[int, TenantProfile]:
    candidates = voice_phone_candidates(value)
    if not candidates:
        return {}

    tenants: dict[int, TenantProfile] = {}
    senders = ProviderSender.query.filter(
        ProviderSender.channel.in_(("voice", "whatsapp")),
        ProviderSender.status.in_(("online", "approved", "connected", "active")),
        or_(
            ProviderSender.phone_number.in_(candidates),
            ProviderSender.sender_id.in_(candidates),
        ),
    ).all()
    for sender in senders:
        tenant = getattr(sender, "tenant", None)
        tenant_id = getattr(tenant, "id", None)
        if tenant_id and getattr(tenant, "is_active", True):
            tenants[int(tenant_id)] = tenant

    # Compatibility for installations that have not yet migrated sender
    # inventory.  The lookup remains authoritative because it resolves through
    # the registered owner, never through user-supplied tenant text.
    normalized = _normalize_phone(value).replace("+", "")
    if normalized:
        # Never use suffix/substring matching here: a provider number is an
        # authorization boundary and a partial match can select another tenant.
        mappings = WhatsappNumero.query.filter(
            WhatsappNumero.numero_whatsapp.in_(voice_phone_candidates(value)),
            WhatsappNumero.is_active.is_(True),
        ).all()
        for mapping in mappings:
            tenant = _tenant_for_legacy_owner(getattr(mapping, "user", None))
            tenant_id = getattr(tenant, "id", None)
            if tenant_id and getattr(tenant, "is_active", True):
                tenants[int(tenant_id)] = tenant
    return tenants


def _configured_demo_numbers(config: Mapping[str, Any] | None) -> set[str]:
    raw = (config.get("CHATBOC_DEMO_WHATSAPP_NUMBERS") if config is not None else None) or os.environ.get(
        "CHATBOC_DEMO_WHATSAPP_NUMBERS"
    ) or ""
    values = [part for part in str(raw).replace(";", ",").split(",") if part.strip()]
    values.append("+18564858589")
    return {_normalize_phone(value) for value in values if _normalize_phone(value)}


def _configured_demo_tenants(config: Mapping[str, Any] | None) -> set[str]:
    raw = (config.get("CHATBOC_DEMO_ALLOWED_TENANT_SLUGS") if config is not None else None) or os.environ.get(
        "CHATBOC_DEMO_ALLOWED_TENANT_SLUGS"
    ) or ""
    values = {
        part.strip()
        for part in str(raw).replace(";", ",").split(",")
        if part.strip()
    }
    values.update({"chatboc-demo", "chatboc-platform"})
    return values


def resolve_authoritative_voice_tenant(
    *,
    from_number: Any,
    to_number: Any,
    direction: Any,
    requested_tenant_slug: Any = None,
    config: Mapping[str, Any] | None = None,
) -> TenantProfile:
    """Resolve one tenant from registered provider numbers.

    The optional requested slug can only narrow the authoritative mapping.  A
    bounded demo-number override mirrors the existing shared demo hub and is
    constrained to an explicit allowlist.
    """

    normalized_direction = normalize_voice_direction(direction)
    bot_number = from_number if normalized_direction == "outbound" else to_number
    mapped = _mapped_tenants_for_number(bot_number)
    requested_slug = str(requested_tenant_slug or "").strip()

    if requested_slug:
        requested = TenantProfile.query.filter_by(slug=requested_slug).first()
        if requested is None or not getattr(requested, "is_active", True):
            raise VoiceConsentLifecycleError("requested_tenant_unknown")
        requested_id = int(requested.id)
        if requested_id in mapped:
            return requested

        is_demo_number = _normalize_phone(bot_number) in _configured_demo_numbers(config)
        if is_demo_number and requested_slug in _configured_demo_tenants(config):
            return requested
        if mapped:
            raise VoiceConsentLifecycleError("requested_tenant_mismatch")
        raise VoiceConsentLifecycleError("sender_not_registered")

    if len(mapped) != 1:
        raise VoiceConsentLifecycleError(
            "sender_tenant_ambiguous" if mapped else "sender_not_registered"
        )
    return next(iter(mapped.values()))


def resolve_voice_consent_policy(tenant: TenantProfile) -> VoiceConsentPolicy:
    config = getattr(tenant, "configuracion", None)
    config = config if isinstance(config, dict) else {}
    raw = config.get("voice_consent_policy")
    if not isinstance(raw, dict):
        raise VoiceConsentLifecycleError("voice_consent_policy_missing")
    if not {"version", "ai_processing", "recording"}.issubset(raw):
        raise VoiceConsentLifecycleError("voice_consent_policy_incomplete")

    version = str(raw.get("version") or "").strip()
    if not _POLICY_VERSION_RE.fullmatch(version):
        raise VoiceConsentLifecycleError("invalid_policy_version")

    ai_processing = str(raw.get("ai_processing") or "").strip().lower()
    if ai_processing not in {"explicit_per_call", "disabled"}:
        raise VoiceConsentLifecycleError("invalid_ai_consent_policy")

    recording_value = raw.get("recording")
    recording_disabled = recording_value is False or str(recording_value).strip().lower() in {
        "disabled",
        "false",
        "off",
        "0",
    }
    if not recording_disabled:
        raise VoiceConsentLifecycleError("recording_not_supported")

    return VoiceConsentPolicy(
        version=version,
        ai_processing=ai_processing,
        recording_allowed=False,
    )


def _append_event(
    lifecycle: VoiceCallLifecycle,
    *,
    event_key: str,
    state: str,
    reason_code: str,
    provider_status: str | None = None,
) -> bool:
    existing = VoiceCallLifecycleEvent.query.filter_by(
        lifecycle_id=lifecycle.id,
        event_key=event_key,
    ).first()
    if existing is not None:
        return False
    db.session.add(
        VoiceCallLifecycleEvent(
            tenant_id=lifecycle.tenant_id,
            lifecycle_id=lifecycle.id,
            event_key=event_key,
            state=state,
            reason_code=reason_code,
            provider_status=provider_status,
            policy_version=lifecycle.consent_policy_version,
        )
    )
    return True


def begin_voice_consent(
    *,
    tenant_id: int,
    call_sid: Any,
    direction: Any,
    policy: VoiceConsentPolicy,
) -> VoiceCallLifecycle:
    call_sid_value = _validate_call_sid(call_sid)
    direction_value = normalize_voice_direction(direction)
    try:
        lifecycle = VoiceCallLifecycle.query.filter_by(
            provider="twilio",
            provider_call_sid=call_sid_value,
        ).with_for_update().first()
        if lifecycle is not None:
            if lifecycle.tenant_id != int(tenant_id):
                raise VoiceConsentLifecycleError("call_tenant_mismatch")
            if lifecycle.direction != direction_value:
                raise VoiceConsentLifecycleError("call_direction_mismatch")
            if lifecycle.consent_policy_version != policy.version:
                raise VoiceConsentLifecycleError("call_policy_mismatch")
            return lifecycle

        lifecycle = VoiceCallLifecycle(
            tenant_id=int(tenant_id),
            provider="twilio",
            provider_call_sid=call_sid_value,
            direction=direction_value,
            state="received",
            consent_status="required",
            consent_policy_version=policy.version,
            ai_processing_allowed=False,
            recording_allowed=False,
            recording_enabled=False,
        )
        db.session.add(lifecycle)
        db.session.flush()
        _append_event(
            lifecycle,
            event_key="lifecycle:received",
            state="received",
            reason_code="provider_call_received",
        )
        lifecycle.state = "consent_pending"
        _append_event(
            lifecycle,
            event_key=f"consent:{policy.version}:required",
            state="consent_pending",
            reason_code="explicit_consent_required",
        )
        db.session.commit()
        return lifecycle
    except IntegrityError:
        db.session.rollback()
        lifecycle = VoiceCallLifecycle.query.filter_by(
            provider="twilio",
            provider_call_sid=call_sid_value,
        ).first()
        if lifecycle is None:
            raise VoiceConsentLifecycleError("lifecycle_conflict")
        if lifecycle.tenant_id != int(tenant_id):
            raise VoiceConsentLifecycleError("call_tenant_mismatch")
        if lifecycle.direction != direction_value:
            raise VoiceConsentLifecycleError("call_direction_mismatch")
        if lifecycle.consent_policy_version != policy.version:
            raise VoiceConsentLifecycleError("call_policy_mismatch")
        return lifecycle
    except Exception:
        db.session.rollback()
        raise


def record_voice_consent_decision(
    *, tenant_id: int, call_sid: Any, decision: str
) -> VoiceCallLifecycle:
    call_sid_value = _validate_call_sid(call_sid)
    decision_value = str(decision or "").strip().lower()
    if decision_value not in {"granted", "declined"}:
        raise VoiceConsentLifecycleError("invalid_consent_decision")
    try:
        lifecycle = VoiceCallLifecycle.query.filter_by(
            tenant_id=int(tenant_id),
            provider_call_sid=call_sid_value,
        ).with_for_update().first()
        if lifecycle is None:
            raise VoiceConsentLifecycleError("lifecycle_not_found")
        if lifecycle.state in _TERMINAL_STATES:
            raise VoiceConsentLifecycleError("lifecycle_terminal")
        if lifecycle.consent_status == decision_value:
            return lifecycle
        if lifecycle.consent_status != "required":
            raise VoiceConsentLifecycleError("consent_decision_terminal")

        lifecycle.consent_status = decision_value
        if decision_value == "granted":
            lifecycle.state = "stream_authorized"
            lifecycle.ai_processing_allowed = True
            reason = "explicit_dtmf_consent_granted"
        else:
            lifecycle.ai_processing_allowed = False
            reason = "explicit_dtmf_consent_declined"
        _append_event(
            lifecycle,
            event_key=f"consent:{lifecycle.consent_policy_version}:{decision_value}",
            state=lifecycle.state,
            reason_code=reason,
        )
        db.session.commit()
        return lifecycle
    except Exception:
        db.session.rollback()
        raise


def record_voice_consent_missing(
    *, tenant_id: int, call_sid: Any
) -> VoiceCallLifecycle:
    call_sid_value = _validate_call_sid(call_sid)
    try:
        lifecycle = VoiceCallLifecycle.query.filter_by(
            tenant_id=int(tenant_id), provider_call_sid=call_sid_value
        ).with_for_update().first()
        if lifecycle is None:
            raise VoiceConsentLifecycleError("lifecycle_not_found")
        if lifecycle.state in _TERMINAL_STATES:
            return lifecycle
        if lifecycle.consent_status == "required":
            # An empty Gather result is a fail-closed refusal for this call.
            # A late/replayed DTMF callback cannot reverse it.
            lifecycle.consent_status = "declined"
            lifecycle.ai_processing_allowed = False
        _append_event(
            lifecycle,
            event_key=f"consent:{lifecycle.consent_policy_version}:missing",
            state=lifecycle.state,
            reason_code="consent_missing_or_timeout",
        )
        db.session.commit()
        return lifecycle
    except Exception:
        db.session.rollback()
        raise


def assert_voice_stream_authorized(
    *, tenant_id: int, call_sid: Any
) -> VoiceCallLifecycle:
    call_sid_value = _validate_call_sid(call_sid)
    lifecycle = VoiceCallLifecycle.query.filter_by(
        tenant_id=int(tenant_id), provider_call_sid=call_sid_value
    ).first()
    if lifecycle is None:
        raise VoiceConsentLifecycleError("consent_missing")
    if (
        lifecycle.state != "stream_authorized"
        or lifecycle.consent_status != "granted"
        or lifecycle.ai_processing_allowed is not True
        or lifecycle.recording_allowed is not False
        or lifecycle.recording_enabled is not False
    ):
        raise VoiceConsentLifecycleError("consent_not_authorized")
    return lifecycle


def claim_voice_stream_authorization(
    *, tenant_id: int, call_sid: Any, policy_version: Any
) -> VoiceCallLifecycle:
    """Atomically allow at most one Media Stream to consume a call grant.

    Transport envelopes may legitimately be re-issued when Twilio retries an
    HTTP webhook.  The durable, tenant-bound claim below is the final authority
    and prevents a second envelope/nonce from opening another OpenAI bridge.
    """

    call_sid_value = _validate_call_sid(call_sid)
    policy_version_value = str(policy_version or "").strip()
    if not _POLICY_VERSION_RE.fullmatch(policy_version_value):
        raise VoiceConsentLifecycleError("invalid_policy_version")
    try:
        # Re-read and lock the tenant policy in the same transaction as the
        # one-stream claim. A matching version alone is not authorization: an
        # operator may have disabled AI processing without rotating it yet.
        tenant = (
            TenantProfile.query.filter_by(id=int(tenant_id))
            .populate_existing()
            .with_for_update()
            .first()
        )
        if tenant is None or not getattr(tenant, "is_active", True):
            raise VoiceConsentLifecycleError("consent_tenant_unknown")
        current_policy = resolve_voice_consent_policy(tenant)
        if current_policy.version != policy_version_value:
            raise VoiceConsentLifecycleError("call_policy_mismatch")
        if current_policy.ai_processing != "explicit_per_call":
            raise VoiceConsentLifecycleError("policy_disabled")

        lifecycle = VoiceCallLifecycle.query.filter_by(
            tenant_id=int(tenant_id), provider_call_sid=call_sid_value
        ).with_for_update().first()
        if lifecycle is None:
            raise VoiceConsentLifecycleError("consent_missing")
        if lifecycle.consent_policy_version != policy_version_value:
            raise VoiceConsentLifecycleError("call_policy_mismatch")
        if (
            lifecycle.state != "stream_authorized"
            or lifecycle.consent_status != "granted"
            or lifecycle.ai_processing_allowed is not True
            or lifecycle.recording_allowed is not False
            or lifecycle.recording_enabled is not False
        ):
            raise VoiceConsentLifecycleError("consent_not_authorized")
        if VoiceCallLifecycleEvent.query.filter_by(
            lifecycle_id=lifecycle.id,
            event_key="stream:claimed",
        ).first() is not None:
            raise VoiceConsentLifecycleError("stream_already_claimed")
        _append_event(
            lifecycle,
            event_key="stream:claimed",
            state="stream_authorized",
            reason_code="twilio_media_stream_claimed",
        )
        db.session.commit()
        return lifecycle
    except IntegrityError as exc:
        db.session.rollback()
        raise VoiceConsentLifecycleError("stream_already_claimed") from exc
    except Exception:
        db.session.rollback()
        raise


def record_voice_provider_status(
    *,
    tenant_id: int,
    call_sid: Any,
    direction: Any,
    policy: VoiceConsentPolicy,
    provider_status: Any,
) -> VoiceCallLifecycle:
    status = str(provider_status or "").strip().lower()
    if status not in _PROVIDER_STATUSES:
        raise VoiceConsentLifecycleError("invalid_provider_status")
    lifecycle = begin_voice_consent(
        tenant_id=tenant_id,
        call_sid=call_sid,
        direction=direction,
        policy=policy,
    )
    try:
        lifecycle = VoiceCallLifecycle.query.filter_by(
            tenant_id=int(tenant_id),
            provider_call_sid=_validate_call_sid(call_sid),
        ).with_for_update().first()
        event_key = f"provider:{status}"
        if VoiceCallLifecycleEvent.query.filter_by(
            lifecycle_id=lifecycle.id, event_key=event_key
        ).first() is not None:
            return lifecycle

        prior_state = lifecycle.state
        reason = "provider_status_observed"
        if prior_state not in _TERMINAL_STATES:
            if status == "completed":
                lifecycle.state = "completed"
                lifecycle.terminal_at = _utc_now()
            elif status in _FAILED_PROVIDER_STATUSES:
                lifecycle.state = "failed"
                lifecycle.terminal_at = _utc_now()
            lifecycle.last_provider_status = status
        else:
            reason = "provider_status_ignored_terminal"

        _append_event(
            lifecycle,
            event_key=event_key,
            state=lifecycle.state,
            reason_code=reason,
            provider_status=status,
        )
        db.session.commit()
        return lifecycle
    except Exception:
        db.session.rollback()
        raise


def mark_voice_lifecycle_failed(
    *, tenant_id: int, call_sid: Any, reason_code: str
) -> VoiceCallLifecycle:
    reason = str(reason_code or "").strip().lower()
    if reason not in _SAFE_FAILURE_REASONS:
        raise VoiceConsentLifecycleError("invalid_failure_reason")
    try:
        lifecycle = VoiceCallLifecycle.query.filter_by(
            tenant_id=int(tenant_id),
            provider_call_sid=_validate_call_sid(call_sid),
        ).with_for_update().first()
        if lifecycle is None:
            raise VoiceConsentLifecycleError("lifecycle_not_found")
        if lifecycle.state in _TERMINAL_STATES:
            return lifecycle
        lifecycle.state = "failed"
        lifecycle.terminal_at = _utc_now()
        _append_event(
            lifecycle,
            event_key=f"failure:{reason}",
            state="failed",
            reason_code=reason,
        )
        db.session.commit()
        return lifecycle
    except Exception:
        db.session.rollback()
        raise
