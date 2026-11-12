"""Optional tenant-bound WhatsApp receipt for a committed ticket closure.

Only an existing outbox canary with a verified sender and open service window
may own this notification. Other tickets retain the legacy notification path.
The generic outbox owns leases, its I/O boundary and ambiguous-outcome policy.
Provider acceptance here is not a ticket delivery/read receipt.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from flask import current_app

from models import MunicipioTicket, PymeTicket, TenantProfile, TicketComentario, db
from services.domain_effect_gate import resolve_domain_effect_outbox_policy
from services.domain_effect_outbox import (
    AmbiguousDomainEffectError, DeliveredDomainEffect, DomainEffectValidationError,
    PermanentDomainEffectError, PreparedDomainEffect, SkippedDomainEffect,
    stage_domain_effect,
)
from services.tenant_ticket_reply_delivery import whatsapp_service_window
from services.tenant_twilio_messaging import (
    TenantTwilioScopeError, _normalized_e164, prepare_bound_tenant_twilio_message,
    resolve_tenant_twilio_sender_snapshot, send_prepared_tenant_twilio_message,
)
from services.whatsapp_enterprise_rules import WhatsAppEnterpriseRulesService
from services.whatsapp_receipt_branding import resolve_whatsapp_receipt_header
from utils.validators import normalize_phone


CLOSED_WHATSAPP_HANDLER = "ticket.state.closed.whatsapp.requester.v1"
CLOSED_WHATSAPP_EFFECT = "ticket.state.closed.whatsapp.requester"
_PAYLOAD_KEYS = {"owner_binding", "provider_sender_binding", "event_binding", "requester_binding"}


def validate_closed_whatsapp_payload(payload):
    from services.ticket_domain_effects import _validate_hex_binding

    if set(payload) != _PAYLOAD_KEYS:
        raise DomainEffectValidationError("ticket_closed_payload_invalid")
    for value in payload.values():
        _validate_hex_binding(value)


def _digest(secret, namespace, values):
    material = json.dumps([namespace, *values], ensure_ascii=False,
        separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hmac.new(str(secret).encode("utf-8"), material, hashlib.sha256).hexdigest()


def _event_binding(secret, tenant_id, tipo_ticket, ticket, comment):
    return _digest(secret, "ticket-closure-event.v1", [tenant_id, tipo_ticket,
        ticket.id, comment.id, comment.user_id, comment.origen, comment.es_admin,
        comment.estado_ticket, comment.comentario, comment.archivo_adjunto_id])


def _requester_binding(secret, tenant_id, tipo_ticket, ticket, recipient):
    return _digest(secret, "ticket-closure-requester.v1", [tenant_id, tipo_ticket,
        ticket.id, ticket.user_id, recipient])


def _aggregate(tipo_ticket):
    return "municipio_ticket_comment" if tipo_ticket == "municipio" else "pyme_ticket_comment"


def _effect_key(tipo_ticket, comment_id):
    return f"{_aggregate(tipo_ticket)}:{comment_id}:closed:{CLOSED_WHATSAPP_HANDLER}"


def _closed_event(comment, ticket, tipo_ticket):
    own_fk = "municipio_ticket_id" if tipo_ticket == "municipio" else "pyme_ticket_id"
    other_fk = "pyme_ticket_id" if tipo_ticket == "municipio" else "municipio_ticket_id"
    return bool(isinstance(comment, TicketComentario) and comment.id
        and getattr(comment, own_fk) == ticket.id and getattr(comment, other_fk) is None
        and comment.es_admin is True and comment.origen == "sistema"
        and comment.estado_ticket == "cerrado" and ticket.estado == "cerrado"
        and comment.user_id and comment.archivo_adjunto_id is None)


def _caption(ticket):
    return f"Tu ticket {ticket.nro_ticket or ticket.id} fue actualizado a cerrado."


def _requester_address(ticket):
    from services.ticket_domain_effects import _ticket_requester_phone

    raw = _ticket_requester_phone(ticket)
    # Contact-window evidence already stores international E.164. Preserve its
    # country instead of applying the legacy Argentina formatter to it again.
    return _normalized_e164(raw) or normalize_phone(raw)


def stage_ticket_closed_whatsapp(comment, ticket, *, tipo_ticket, actor, session=None):
    """Stage before the state/comment commit; True owns only WhatsApp.

    No new gate is enabled and this function never performs provider I/O. A
    malformed existing queue configuration fails the domain transaction rather
    than silently bypassing that gate through the deployment-wide sender.
    """
    from services.ticket_domain_effects import (
        TICKET_DOMAIN_EFFECT_REGISTRY, _owner_binding, _profile_owner_id,
        pyme_whatsapp_chat_enabled,
    )
    if tipo_ticket not in {"municipio", "pyme"}:
        raise ValueError("ticket_closed_type_invalid")
    expected_model = MunicipioTicket if tipo_ticket == "municipio" else PymeTicket
    if not isinstance(ticket, expected_model):
        raise ValueError("ticket_closed_aggregate_invalid")
    tenant_id = getattr(ticket, "tenant_id", None)
    if not tenant_id:
        return False
    policy = resolve_domain_effect_outbox_policy(current_app.config, tenant_id=tenant_id)
    if not policy.enabled:
        return False
    effect_session = session or db.session
    profile = effect_session.get(TenantProfile, tenant_id)
    if (profile is None or profile.tipo != tipo_ticket or profile.is_active is not True
            or not _profile_owner_id(profile, tipo_ticket)
            or (tipo_ticket == "municipio" and ticket.municipio_id != profile.municipio_id)):
        return False
    if tipo_ticket == "pyme" and not pyme_whatsapp_chat_enabled():
        return False
    if not _closed_event(comment, ticket, tipo_ticket) or comment.user_id != getattr(actor, "id", None):
        raise ValueError("ticket_closed_event_invalid")
    try:
        recipient = _requester_address(ticket)
    except PermanentDomainEffectError:
        return False
    if not recipient:
        return False
    snapshot = resolve_tenant_twilio_sender_snapshot(tenant_id=tenant_id,
        channel="whatsapp", session=effect_session)
    if snapshot.reason_code or snapshot.sender is None:
        return False
    window = whatsapp_service_window(tenant_id=tenant_id,
        provider_sender_id=snapshot.sender.id, recipient=recipient, session=effect_session)
    if not window["free_form_allowed"]:
        return False
    # Validate credentials/account/callback before suppressing the legacy path.
    try:
        preflight = prepare_bound_tenant_twilio_message(tenant_id=tenant_id, channel="whatsapp",
            expected_sender_binding=snapshot.binding, recipient=recipient, body=_caption(ticket),
            session=effect_session)
    except TenantTwilioScopeError:
        return False
    if preflight.reason_code or preflight.prepared is None:
        return False
    payload = {
        "owner_binding": _owner_binding(tenant_id, tipo_ticket, _profile_owner_id(profile, tipo_ticket)),
        "provider_sender_binding": snapshot.binding,
        "event_binding": _event_binding(policy.secret, tenant_id, tipo_ticket, ticket, comment),
        "requester_binding": _requester_binding(policy.secret, tenant_id, tipo_ticket, ticket, recipient),
    }
    stage_domain_effect(tenant_id=tenant_id, aggregate_type=_aggregate(tipo_ticket),
        aggregate_ref=str(comment.id), effect_type=CLOSED_WHATSAPP_EFFECT,
        handler_name=CLOSED_WHATSAPP_HANDLER, channel="whatsapp",
        recipient_ref="recipient_hash:"+payload["requester_binding"],
        effect_key=_effect_key(tipo_ticket, comment.id), intent_secret=policy.secret,
        registry=TICKET_DOMAIN_EFFECT_REGISTRY, payload=payload,
        max_attempts=policy.max_attempts, max_payload_bytes=policy.max_payload_bytes,
        session=effect_session)
    return True


def prepare_ticket_closed_whatsapp(claim):
    from services.ticket_domain_effects import (
        _load_scoped_comment, pyme_whatsapp_chat_enabled,
    )
    validate_closed_whatsapp_payload(claim.payload)
    if (claim.handler_name != CLOSED_WHATSAPP_HANDLER or claim.effect_type != CLOSED_WHATSAPP_EFFECT
            or claim.channel != "whatsapp"):
        raise PermanentDomainEffectError("ticket_closed_claim_invalid")
    loaded = _load_scoped_comment(claim)
    if loaded is None:
        return SkippedDomainEffect(reason_code="ticket_closed_event_missing")
    comment, ticket, tipo_ticket = loaded
    if claim.effect_key != _effect_key(tipo_ticket, comment.id):
        raise PermanentDomainEffectError("ticket_closed_claim_invalid")
    if not _closed_event(comment, ticket, tipo_ticket):
        return SkippedDomainEffect(reason_code="ticket_closed_event_superseded")
    policy = resolve_domain_effect_outbox_policy(current_app.config, tenant_id=claim.tenant_id)
    if not policy.enabled:
        return SkippedDomainEffect(reason_code="ticket_closed_gate_disabled")
    if tipo_ticket == "pyme" and not pyme_whatsapp_chat_enabled():
        return SkippedDomainEffect(reason_code="pyme_whatsapp_disabled")
    if not hmac.compare_digest(claim.payload["event_binding"],
            _event_binding(policy.secret, claim.tenant_id, tipo_ticket, ticket, comment)):
        raise PermanentDomainEffectError("ticket_closed_event_binding_changed")
    recipient = _requester_address(ticket)
    if not recipient:
        return SkippedDomainEffect(reason_code="requester_phone_missing")
    requester = _requester_binding(policy.secret, claim.tenant_id, tipo_ticket, ticket, recipient)
    if (not hmac.compare_digest(claim.payload["requester_binding"], requester)
            or not hmac.compare_digest(claim.recipient_ref, "recipient_hash:"+requester)):
        raise PermanentDomainEffectError("ticket_closed_requester_changed")
    snapshot = resolve_tenant_twilio_sender_snapshot(tenant_id=claim.tenant_id, channel="whatsapp")
    if not hmac.compare_digest(claim.payload["provider_sender_binding"], snapshot.binding):
        raise PermanentDomainEffectError("ticket_closed_sender_changed")
    if snapshot.reason_code or snapshot.sender is None:
        return SkippedDomainEffect(reason_code="ticket_closed_sender_unavailable")
    window = whatsapp_service_window(tenant_id=claim.tenant_id,
        provider_sender_id=snapshot.sender.id, recipient=recipient)
    if not window["free_form_allowed"]:
        return SkippedDomainEffect(reason_code="ticket_closed_window_expired")
    profile = db.session.get(TenantProfile, claim.tenant_id)
    header = resolve_whatsapp_receipt_header(profile,
        public_base_url=current_app.config.get("APP_BASE_URL", ""))
    caption = _caption(ticket)
    try:
        preflight = prepare_bound_tenant_twilio_message(tenant_id=claim.tenant_id,
            channel="whatsapp", expected_sender_binding=claim.payload["provider_sender_binding"],
            recipient=recipient, body=caption, media_urls=(header,) if header else ())
    except TenantTwilioScopeError as exc:
        raise PermanentDomainEffectError(exc.code) from exc
    if preflight.reason_code:
        return SkippedDomainEffect(reason_code=preflight.reason_code)
    prepared = preflight.prepared
    if prepared is None:
        raise PermanentDomainEffectError("ticket_closed_preflight_invalid")
    allowed, reason = WhatsAppEnterpriseRulesService(claim.tenant_id).reserve_outbound(
        body=caption, reservation_key=f"ticket-closed:{claim.tenant_id}:{tipo_ticket}:{comment.id}",
        metadata={"recipient": recipient, "provider_sender_id": prepared.provider_sender_id,
            "within_24h_window": True, "is_template": False},
        provider="twilio", provider_sender_id=prepared.provider_sender_id,
        source="ticket_state_closed")
    if not allowed:
        return SkippedDomainEffect(reason_code="ticket_closed_policy_blocked")
    # Reservation uses its own transaction; do not rely on a stale window after it.
    if not whatsapp_service_window(tenant_id=claim.tenant_id,
            provider_sender_id=prepared.provider_sender_id, recipient=recipient)["free_form_allowed"]:
        return SkippedDomainEffect(reason_code="ticket_closed_window_expired")

    def deliver():
        provider_ref = send_prepared_tenant_twilio_message(prepared)
        if not provider_ref:
            raise AmbiguousDomainEffectError("provider_acceptance_unknown")
        return DeliveredDomainEffect(provider_ref=provider_ref,
            result={"delivery": "provider_accepted", "header_included": bool(header)})

    return PreparedDomainEffect(deliver=deliver)
