"""Durable, privacy-safe external effects for newly created tickets.

The outbox stores only tenant-scoped opaque references.  TenantTicket reply
handlers reload an immutable reply event (including its pinned recipient) from
the database during preflight, before the generic runtime records
``io_started_at`` and invokes the provider closure.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from html import escape
from types import SimpleNamespace
from typing import Any, Literal, Mapping
from urllib.parse import urljoin, urlparse

from flask import current_app, has_app_context

from models import (
    ArchivoAdjunto,
    MunicipioTicket,
    PymeTicket,
    TenantTicket,
    TenantTicketReplyEvent,
    TenantProfile,
    TicketComentario,
    User,
    db,
)
from services.domain_effect_gate import (
    DomainEffectOutboxConfigurationError,
    resolve_domain_effect_outbox_policy,
)
from services.domain_effect_outbox import (
    AmbiguousDomainEffectError,
    DeliveredDomainEffect,
    DomainEffectClaim,
    DomainEffectRegistry,
    DomainEffectValidationError,
    PermanentDomainEffectError,
    PreparedDomainEffect,
    SkippedDomainEffect,
    stage_domain_effect,
)
from services.tenant_twilio_messaging import (
    TenantTwilioScopeError,
    build_tenant_twilio_sender_binding,
    prepare_bound_tenant_twilio_message,
    send_prepared_tenant_twilio_message,
)
from services.ticket_crm_action_delivery import register_crm_action_handlers
from utils.validators import validate_email_address


TicketType = Literal["municipio", "pyme"]

MUNICIPAL_TICKET_AGGREGATE = "municipio_ticket"
PYME_TICKET_AGGREGATE = "pyme_ticket"
MUNICIPAL_COMMENT_AGGREGATE = "municipio_ticket_comment"
PYME_COMMENT_AGGREGATE = "pyme_ticket_comment"
TENANT_REPLY_AGGREGATE = "tenant_ticket_reply"

SIGEM_HANDLER = "ticket.created.sigem.v1"
ADMIN_EMAIL_HANDLER = "ticket.created.email.admin.v1"
REQUESTER_EMAIL_HANDLER = "ticket.created.email.requester.v1"
COMMENT_ADMIN_EMAIL_HANDLER = "ticket.comment.email.admin.v1"
COMMENT_REQUESTER_EMAIL_HANDLER = "ticket.comment.email.requester.v1"
COMMENT_REQUESTER_SMS_HANDLER = "ticket.comment.sms.requester.v1"
COMMENT_REQUESTER_WHATSAPP_HANDLER = "ticket.comment.whatsapp.requester.v1"
COMMENT_REALTIME_HANDLER = "ticket.comment.realtime.v1"
TENANT_REPLY_EMAIL_HANDLER = "tenant_ticket.reply.email.requester.v1"
TENANT_REPLY_WHATSAPP_HANDLER = "tenant_ticket.reply.whatsapp.requester.v1"
TENANT_REPLY_REALTIME_HANDLER = "tenant_ticket.reply.realtime.v1"

TENANT_ADMIN_RECIPIENT = "role:tenant.admins"
TICKET_REQUESTER_RECIPIENT = "role:ticket.requester"
SIGEM_RECIPIENT = "integration:tenant.sigem"
TICKET_TIMELINE_RECIPIENT = "room:tenant.ticket.timeline"


def pyme_whatsapp_chat_enabled() -> bool:
    """Return the deployment-wide PyME ticket WhatsApp policy.

    The flag predates the durable outbox and historically defaulted to enabled
    in socket and HTTP ticket replies. Normalize string overrides as well as
    booleans so ``"false"`` cannot accidentally enable sends.
    """

    value = current_app.config.get("ENABLE_PYME_WHATSAPP_CHAT", True)
    if isinstance(value, bool):
        return value
    if value is None:
        return True
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _skip(reason_code: str) -> SkippedDomainEffect:
    return SkippedDomainEffect(reason_code=reason_code, result={})


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return normalized if normalized > 0 else None


def _owner_binding(tenant_id: int, ticket_type: TicketType, owner_id: int) -> str:
    material = f"ticket-owner.v1:{tenant_id}:{ticket_type}:{owner_id}".encode("ascii")
    return hashlib.sha256(material).hexdigest()


def _validate_hex_binding(value: Any) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or not hmac.compare_digest(value, value.lower())
    ):
        raise DomainEffectValidationError("ticket_effect_payload_invalid")
    try:
        int(value, 16)
    except ValueError as exc:
        raise DomainEffectValidationError("ticket_effect_payload_invalid") from exc


def _validate_ticket_effect_payload(payload: dict[str, Any]) -> None:
    if set(payload) != {"owner_binding"}:
        raise DomainEffectValidationError("ticket_effect_payload_invalid")
    _validate_hex_binding(payload.get("owner_binding"))


def _validate_ticket_twilio_effect_payload(payload: dict[str, Any]) -> None:
    if set(payload) != {"owner_binding", "provider_sender_binding"}:
        raise DomainEffectValidationError("ticket_effect_payload_invalid")
    _validate_hex_binding(payload.get("owner_binding"))
    _validate_hex_binding(payload.get("provider_sender_binding"))


def _validate_tenant_reply_effect_payload(payload: dict[str, Any]) -> None:
    if set(payload) != {"tenant_binding"}:
        raise DomainEffectValidationError("tenant_ticket_reply_payload_invalid")
    _validate_hex_binding(payload.get("tenant_binding"))


def _validate_tenant_reply_twilio_effect_payload(payload: dict[str, Any]) -> None:
    if set(payload) != {"tenant_binding", "provider_sender_binding"}:
        raise DomainEffectValidationError("tenant_ticket_reply_payload_invalid")
    _validate_hex_binding(payload.get("tenant_binding"))
    _validate_hex_binding(payload.get("provider_sender_binding"))


def _profile_owner_id(profile: TenantProfile, ticket_type: TicketType) -> int | None:
    return _positive_int(
        getattr(
            profile,
            "municipio_id" if ticket_type == "municipio" else "pyme_id",
            None,
        )
    )


def _load_scoped_ticket(
    claim: DomainEffectClaim,
) -> tuple[MunicipioTicket | PymeTicket, TicketType] | None:
    ticket_id = _positive_int(claim.aggregate_ref)
    tenant_id = _positive_int(claim.tenant_id)
    if ticket_id is None or tenant_id is None:
        return None

    if claim.aggregate_type == MUNICIPAL_TICKET_AGGREGATE:
        ticket_type: TicketType = "municipio"
        model = MunicipioTicket
    elif claim.aggregate_type == PYME_TICKET_AGGREGATE:
        ticket_type = "pyme"
        model = PymeTicket
    else:
        return None

    ticket = model.query.filter_by(id=ticket_id, tenant_id=tenant_id).one_or_none()
    if ticket is None:
        return None

    profile = db.session.get(TenantProfile, tenant_id)
    if (
        profile is None
        or str(getattr(profile, "tipo", "")).strip() != ticket_type
        or not bool(getattr(profile, "is_active", False))
    ):
        raise PermanentDomainEffectError("ticket_tenant_binding_invalid")
    owner_id = _profile_owner_id(profile, ticket_type)
    expected_binding = str(claim.payload.get("owner_binding") or "")
    if owner_id is None or not hmac.compare_digest(
        expected_binding,
        _owner_binding(tenant_id, ticket_type, owner_id),
    ):
        raise PermanentDomainEffectError("ticket_tenant_binding_invalid")
    if ticket_type == "municipio" and _positive_int(
        getattr(ticket, "municipio_id", None)
    ) != owner_id:
        raise PermanentDomainEffectError("ticket_tenant_binding_invalid")
    return ticket, ticket_type


def _load_scoped_comment(
    claim: DomainEffectClaim,
) -> tuple[TicketComentario, MunicipioTicket | PymeTicket, TicketType] | None:
    comment_id = _positive_int(claim.aggregate_ref)
    tenant_id = _positive_int(claim.tenant_id)
    if comment_id is None or tenant_id is None:
        return None

    if claim.aggregate_type == MUNICIPAL_COMMENT_AGGREGATE:
        ticket_type: TicketType = "municipio"
        ticket_fk = "municipio_ticket_id"
        ticket_model = MunicipioTicket
    elif claim.aggregate_type == PYME_COMMENT_AGGREGATE:
        ticket_type = "pyme"
        ticket_fk = "pyme_ticket_id"
        ticket_model = PymeTicket
    else:
        return None

    comment = db.session.get(TicketComentario, comment_id)
    ticket_id = _positive_int(getattr(comment, ticket_fk, None)) if comment else None
    if comment is None or ticket_id is None:
        return None
    ticket = ticket_model.query.filter_by(id=ticket_id, tenant_id=tenant_id).one_or_none()
    if ticket is None:
        return None

    profile = db.session.get(TenantProfile, tenant_id)
    if (
        profile is None
        or str(getattr(profile, "tipo", "")).strip() != ticket_type
        or not bool(getattr(profile, "is_active", False))
    ):
        raise PermanentDomainEffectError("ticket_comment_tenant_binding_invalid")
    owner_id = _profile_owner_id(profile, ticket_type)
    expected_binding = str(claim.payload.get("owner_binding") or "")
    if owner_id is None or not hmac.compare_digest(
        expected_binding,
        _owner_binding(tenant_id, ticket_type, owner_id),
    ):
        raise PermanentDomainEffectError("ticket_comment_tenant_binding_invalid")
    if ticket_type == "municipio" and _positive_int(
        getattr(ticket, "municipio_id", None)
    ) != owner_id:
        raise PermanentDomainEffectError("ticket_comment_tenant_binding_invalid")

    attachment = getattr(comment, "archivo_adjunto", None)
    if attachment is not None:
        expected_fk = (
            getattr(attachment, "municipio_ticket_id", None)
            if ticket_type == "municipio"
            else getattr(attachment, "pyme_ticket_id", None)
        )
        opposite_fk = (
            getattr(attachment, "pyme_ticket_id", None)
            if ticket_type == "municipio"
            else getattr(attachment, "municipio_ticket_id", None)
        )
        if _positive_int(expected_fk) != ticket_id or opposite_fk is not None:
            raise PermanentDomainEffectError("ticket_comment_attachment_binding_invalid")
    return comment, ticket, ticket_type


def _tenant_reply_binding(tenant_id: int, ticket_id: int) -> str:
    material = f"tenant-ticket-reply.v1:{tenant_id}:{ticket_id}".encode("ascii")
    return hashlib.sha256(material).hexdigest()


def _tenant_reply_recipient_ref(
    *,
    secret: str,
    reply_event: TenantTicketReplyEvent,
    channel: str,
) -> str:
    """Bind one outbox row to an immutable reply and recipient without PII."""

    normalized_channel = str(channel or "").strip().lower()
    if normalized_channel not in {"email", "whatsapp", "realtime"}:
        raise DomainEffectOutboxConfigurationError(
            "tenant_ticket_reply_channel_invalid"
        )
    if not isinstance(secret, str) or len(secret.encode("utf-8")) < 32:
        raise DomainEffectOutboxConfigurationError(
            "tenant_ticket_reply_secret_invalid"
        )
    if normalized_channel == "email":
        recipient = str(reply_event.recipient_email or "").strip().casefold()
    elif normalized_channel == "whatsapp":
        recipient = str(reply_event.recipient_phone or "").strip()
    else:
        recipient = f"tenant-room:{reply_event.tenant_id}"
    material = json.dumps(
        {
            "contract_version": reply_event.contract_version,
            "reply_event_id": reply_event.id,
            "tenant_id": reply_event.tenant_id,
            "ticket_id": reply_event.ticket_id,
            "event_id": reply_event.event_id,
            "channel": normalized_channel,
            "recipient": recipient or "missing",
            "body": reply_event.body,
            "visibility": reply_event.visibility,
            "actor_user_id": reply_event.actor_user_id,
            "actor_name": reply_event.actor_name,
            "actor_role": reply_event.actor_role,
            "created_at": reply_event.to_event_dict().get("created_at"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), material, hashlib.sha256).hexdigest()
    return f"recipient_hash:{digest}"


def tenant_ticket_reply_contact(ticket: TenantTicket) -> dict[str, str]:
    """Resolve reply contact data without crossing the ticket tenant boundary."""

    extra = ticket.datos_extra if isinstance(ticket.datos_extra, Mapping) else {}
    assisted_request = (
        extra.get("assisted_request")
        if isinstance(extra.get("assisted_request"), Mapping)
        else {}
    )
    public_follow_up = (
        extra.get("public_follow_up")
        if isinstance(extra.get("public_follow_up"), Mapping)
        else {}
    )
    assisted_follow_up = (
        assisted_request.get("public_follow_up")
        if isinstance(assisted_request.get("public_follow_up"), Mapping)
        else {}
    )
    crm_review_card = (
        extra.get("crm_review_card")
        if isinstance(extra.get("crm_review_card"), Mapping)
        else {}
    )
    ticket_user = getattr(ticket, "user", None)
    if ticket_user is not None and _positive_int(
        getattr(ticket_user, "tenant_id", None)
    ) != _positive_int(getattr(ticket, "tenant_id", None)):
        ticket_user = None

    candidates = [
        extra.get("contact"),
        extra.get("customer_profile"),
        assisted_request.get("contact"),
        crm_review_card.get("contact"),
        public_follow_up.get("contact"),
        assisted_follow_up.get("contact"),
        extra,
        {
            "name": getattr(ticket_user, "name", None),
            "phone": getattr(ticket_user, "telefono", None),
            "email": getattr(ticket_user, "email", None),
        },
    ]
    aliases = {
        "name": ("name", "nombre", "contact_name"),
        "phone": (
            "phone",
            "telefono",
            "whatsapp",
            "contact_phone",
            "phone_number",
            "wa_id",
        ),
        "email": ("email", "correo", "contact_email", "email_address"),
    }
    contact: dict[str, str] = {}
    for field, keys in aliases.items():
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            value = next((candidate.get(key) for key in keys if candidate.get(key)), None)
            if value is not None and str(value).strip():
                contact[field] = str(value).strip()
                break
    return contact


def _load_scoped_tenant_reply(
    claim: DomainEffectClaim,
) -> tuple[
    TenantTicket,
    dict[str, Any],
    TenantProfile,
    TenantTicketReplyEvent | None,
] | None:
    if claim.aggregate_type != TENANT_REPLY_AGGREGATE:
        return None
    tenant_id = _positive_int(claim.tenant_id)
    ref_parts = str(claim.aggregate_ref or "").split(":", 1)
    if tenant_id is None or len(ref_parts) != 2:
        return None
    ticket_id = _positive_int(ref_parts[0])
    event_id = str(ref_parts[1] or "").strip()
    if ticket_id is None or not event_id:
        return None

    ticket = TenantTicket.query.filter_by(
        id=ticket_id,
        tenant_id=tenant_id,
    ).one_or_none()
    if ticket is None:
        return None
    profile = db.session.get(TenantProfile, tenant_id)
    if profile is None or not bool(getattr(profile, "is_active", False)):
        raise PermanentDomainEffectError("tenant_ticket_reply_tenant_binding_invalid")
    expected_binding = str(claim.payload.get("tenant_binding") or "")
    if not hmac.compare_digest(
        expected_binding,
        _tenant_reply_binding(tenant_id, ticket_id),
    ):
        raise PermanentDomainEffectError("tenant_ticket_reply_tenant_binding_invalid")

    reply_event = TenantTicketReplyEvent.query.filter_by(
        tenant_id=tenant_id,
        ticket_id=ticket_id,
        event_id=event_id,
    ).one_or_none()
    if str(claim.recipient_ref or "").startswith("recipient_hash:"):
        if reply_event is None:
            raise PermanentDomainEffectError("tenant_ticket_reply_event_missing")
        policy = resolve_domain_effect_outbox_policy(
            current_app.config,
            tenant_id=tenant_id,
        )
        if not policy.enabled or not policy.secret:
            raise PermanentDomainEffectError(
                "tenant_ticket_reply_recipient_binding_invalid"
            )
        expected_recipient_ref = _tenant_reply_recipient_ref(
            secret=policy.secret,
            reply_event=reply_event,
            channel=claim.channel,
        )
        if not hmac.compare_digest(
            str(claim.recipient_ref or ""),
            expected_recipient_ref,
        ):
            raise PermanentDomainEffectError(
                "tenant_ticket_reply_recipient_binding_invalid"
            )
        event = reply_event.to_event_dict()
    else:
        # Explicit legacy compatibility for already-staged rows created before
        # durable reply events.  New rows always use recipient_hash references.
        extra = ticket.datos_extra if isinstance(ticket.datos_extra, Mapping) else {}
        comments = extra.get("comments") if isinstance(extra.get("comments"), list) else []
        event = next(
            (
                dict(item)
                for item in comments
                if isinstance(item, Mapping) and str(item.get("id") or "") == event_id
            ),
            None,
        )
        if event is None:
            return None
        reply_event = None
    if (
        str(event.get("action") or "").strip().lower() != "reply"
        or str(event.get("origin") or "").strip().lower() != "admin_panel"
        or not str(event.get("body") or "").strip()
    ):
        raise PermanentDomainEffectError("tenant_ticket_reply_event_invalid")
    return ticket, event, profile, reply_event


def _load_scoped_ticket_user(
    ticket: MunicipioTicket | PymeTicket,
) -> User | None:
    user_id = _positive_int(getattr(ticket, "user_id", None))
    if user_id is None:
        return None

    user = db.session.get(User, user_id)
    if user is None:
        return None
    tenant_id = _positive_int(getattr(ticket, "tenant_id", None))
    if (
        tenant_id is None
        or _positive_int(getattr(user, "tenant_id", None)) != tenant_id
    ):
        raise PermanentDomainEffectError("ticket_requester_tenant_binding_invalid")
    return user


def _ticket_requester_email(ticket: MunicipioTicket | PymeTicket) -> str:
    email = (
        getattr(ticket, "email_vecino", None)
        or getattr(ticket, "email", None)
    )
    if not email and getattr(ticket, "user_id", None):
        user = _load_scoped_ticket_user(ticket)
        email = getattr(user, "email", None) if user else None
    return str(email or "").strip()


def _ticket_requester_phone(ticket: MunicipioTicket | PymeTicket) -> str:
    phone = (
        getattr(ticket, "telefono_vecino", None)
        or getattr(ticket, "telefono", None)
    )
    if not phone and getattr(ticket, "user_id", None):
        user = _load_scoped_ticket_user(ticket)
        phone = getattr(user, "telefono", None) if user else None
    return str(phone or "").strip()


def _comment_notification_messages(
    ticket: MunicipioTicket | PymeTicket,
    comment: TicketComentario,
) -> tuple[str, str]:
    comment_text = str(getattr(comment, "comentario", None) or "")
    short_message = (
        f"Nuevo comentario en tu ticket #{ticket.nro_ticket}: "
        f"{comment_text[:50]}..."
    )
    return comment_text.strip() or short_message, short_message


def _claim_provider_sender_binding(claim: DomainEffectClaim) -> str:
    binding = str(claim.payload.get("provider_sender_binding") or "").strip()
    if len(binding) != 64:
        raise PermanentDomainEffectError(
            "ticket_comment_provider_sender_binding_missing"
        )
    return binding


def _comment_attachment_media_urls(
    comment: TicketComentario,
) -> tuple[tuple[str, ...], str | None]:
    attachment = getattr(comment, "archivo_adjunto", None)
    if attachment is None:
        return (), None
    raw_url = str(getattr(attachment, "url", None) or "").strip()
    if not raw_url:
        return (), "whatsapp_attachment_url_missing"
    parsed = urlparse(raw_url)
    if not parsed.scheme:
        base_url = str(current_app.config.get("APP_BASE_URL") or "").strip()
        base_parsed = urlparse(base_url)
        if base_parsed.scheme not in {"http", "https"} or not base_parsed.netloc:
            return (), "whatsapp_attachment_base_url_missing"
        raw_url = urljoin(base_url.rstrip("/") + "/", raw_url.lstrip("/"))
        parsed = urlparse(raw_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        return (), "whatsapp_attachment_url_invalid"
    return (raw_url,), None


def _bind_comment_attachment(
    comment: TicketComentario,
    ticket: MunicipioTicket | PymeTicket,
    ticket_type: TicketType,
    *,
    session,
) -> None:
    attachment_id = _positive_int(getattr(comment, "archivo_adjunto_id", None))
    if attachment_id is None:
        return
    attachment = session.get(ArchivoAdjunto, attachment_id)
    if attachment is None:
        raise DomainEffectOutboxConfigurationError(
            "domain_effect_comment_attachment_missing"
        )

    duplicate = TicketComentario.query.filter(
        TicketComentario.archivo_adjunto_id == attachment_id,
        TicketComentario.id != comment.id,
    ).first()
    if duplicate is not None:
        raise DomainEffectOutboxConfigurationError(
            "domain_effect_comment_attachment_duplicate"
        )

    expected_attr = (
        "municipio_ticket_id" if ticket_type == "municipio" else "pyme_ticket_id"
    )
    opposite_attr = (
        "pyme_ticket_id" if ticket_type == "municipio" else "municipio_ticket_id"
    )
    expected_ticket_id = _positive_int(getattr(attachment, expected_attr, None))
    if expected_ticket_id not in {None, int(ticket.id)} or getattr(
        attachment,
        opposite_attr,
        None,
    ) is not None:
        raise DomainEffectOutboxConfigurationError(
            "domain_effect_comment_attachment_binding_invalid"
        )
    setattr(attachment, expected_attr, int(ticket.id))
    session.add(attachment)


def _load_tenant_admin(
    tenant_id: int,
    ticket_type: TicketType,
) -> User | SimpleNamespace | None:
    """Resolve the current tenant recipient without persisting its address."""

    profile = db.session.get(TenantProfile, tenant_id)
    if profile is None or str(getattr(profile, "tipo", "")).strip() != ticket_type:
        return None

    owner_id = (
        getattr(profile, "municipio_id", None)
        if ticket_type == "municipio"
        else getattr(profile, "pyme_id", None)
    )
    admin_user = db.session.get(User, owner_id) if owner_id else None
    if admin_user is not None and _positive_int(
        getattr(admin_user, "tenant_id", None)
    ) != _positive_int(tenant_id):
        raise PermanentDomainEffectError("ticket_admin_tenant_binding_invalid")
    if admin_user is not None and getattr(admin_user, "email", None):
        return admin_user

    dispatch_email = str(getattr(profile, "dispatch_email", None) or "").strip()
    if not dispatch_email:
        return None
    return SimpleNamespace(
        email=dispatch_email,
        name=getattr(profile, "nombre", None),
        rubro=None,
    )


def _email_preflight_error() -> str | None:
    from services import email_service

    if not email_service._email_notifications_enabled():
        return "email_notifications_disabled"
    smtp_valid, _ = email_service.validar_configuracion_smtp(require_auth=True)
    if not smtp_valid:
        return "email_configuration_missing"
    return None


def _email_recipient_preflight_error(
    recipient: Any,
    *,
    recipient_type: Literal["admin", "requester"],
) -> str | None:
    normalized = str(recipient or "").strip()
    if not normalized:
        return f"{recipient_type}_recipient_missing"
    if not validate_email_address(normalized):
        return f"{recipient_type}_recipient_invalid"
    return None


def _ticket_delivery_data(
    ticket: MunicipioTicket | PymeTicket,
    ticket_type: TicketType,
) -> dict[str, Any]:
    # Template helpers may still expect ownership hints, but all citizen fields
    # are reloaded from the aggregate rather than copied into the outbox.
    result: dict[str, Any] = {"tenant_id": getattr(ticket, "tenant_id", None)}
    if ticket_type == "municipio":
        result["municipio_id"] = getattr(ticket, "municipio_id", None)
    return result


def _provider_accepted(delivered: Any) -> DeliveredDomainEffect:
    if not delivered:
        raise AmbiguousDomainEffectError("provider_acceptance_unknown")
    # A synchronous SMTP/Twilio/integration response is acceptance, not proof
    # of final delivery to the recipient. Provider callbacks own that state.
    return DeliveredDomainEffect(result={"delivery": "provider_accepted"})


def _prepare_admin_email(
    claim: DomainEffectClaim,
) -> PreparedDomainEffect | SkippedDomainEffect:
    loaded = _load_scoped_ticket(claim)
    if loaded is None:
        return _skip("ticket_not_found")
    ticket, ticket_type = loaded

    admin_user = _load_tenant_admin(int(claim.tenant_id), ticket_type)
    recipient_error = _email_recipient_preflight_error(
        getattr(admin_user, "email", None) if admin_user is not None else None,
        recipient_type="admin",
    )
    if recipient_error:
        return _skip(recipient_error)
    preflight_error = _email_preflight_error()
    if preflight_error:
        return _skip(preflight_error)

    def deliver() -> DeliveredDomainEffect:
        from services.email_service import enviar_email_ticket_admin

        delivered = enviar_email_ticket_admin(
            ticket,
            admin_user=admin_user,
            tipo_ticket=ticket_type,
            ticket_data=_ticket_delivery_data(ticket, ticket_type),
        )
        return _provider_accepted(delivered)

    return PreparedDomainEffect(deliver=deliver)


def _prepare_requester_email(
    claim: DomainEffectClaim,
) -> PreparedDomainEffect | SkippedDomainEffect:
    loaded = _load_scoped_ticket(claim)
    if loaded is None:
        return _skip("ticket_not_found")
    ticket, ticket_type = loaded

    recipient = (
        getattr(ticket, "email_vecino", None)
        if ticket_type == "municipio"
        else getattr(ticket, "email", None)
    )
    recipient_error = _email_recipient_preflight_error(
        recipient,
        recipient_type="requester",
    )
    if recipient_error:
        return _skip(recipient_error)
    preflight_error = _email_preflight_error()
    if preflight_error:
        return _skip(preflight_error)

    tenant_admin = _load_tenant_admin(int(claim.tenant_id), ticket_type)

    def deliver() -> DeliveredDomainEffect:
        from services.email_service import enviar_email_ticket_cliente

        delivered = enviar_email_ticket_cliente(
            ticket,
            tipo_ticket=ticket_type,
            admin_user=tenant_admin,
            ticket_data=_ticket_delivery_data(ticket, ticket_type),
        )
        return _provider_accepted(delivered)

    return PreparedDomainEffect(deliver=deliver)


def _prepare_sigem(
    claim: DomainEffectClaim,
) -> PreparedDomainEffect | SkippedDomainEffect:
    loaded = _load_scoped_ticket(claim)
    if loaded is None:
        return _skip("ticket_not_found")
    ticket, ticket_type = loaded
    if ticket_type != "municipio":
        return _skip("sigem_not_applicable")
    if not bool(current_app.config.get("SIGEM_LIVE_ENABLED", False)):
        return _skip("sigem_not_configured")

    from services import integracion_municipal

    # The current adapter is intentionally a stub.  Requiring an explicit
    # capability marker prevents a log-only implementation from ever being
    # recorded as a successful delivery.
    if not bool(getattr(integracion_municipal, "SIGEM_TRANSPORT_IMPLEMENTED", False)):
        return _skip("sigem_transport_unavailable")

    def deliver() -> DeliveredDomainEffect:
        delivered = integracion_municipal.enviar_ticket_a_sigem(ticket)
        return _provider_accepted(delivered)

    return PreparedDomainEffect(deliver=deliver)


def _prepare_comment_admin_email(
    claim: DomainEffectClaim,
) -> PreparedDomainEffect | SkippedDomainEffect:
    loaded = _load_scoped_comment(claim)
    if loaded is None:
        return _skip("ticket_comment_not_found")
    comment, ticket, ticket_type = loaded
    if bool(getattr(comment, "es_admin", False)):
        raise PermanentDomainEffectError("ticket_comment_direction_invalid")

    admin_user = _load_tenant_admin(int(claim.tenant_id), ticket_type)
    recipient_error = _email_recipient_preflight_error(
        getattr(admin_user, "email", None) if admin_user is not None else None,
        recipient_type="admin",
    )
    if recipient_error:
        return _skip(recipient_error)
    preflight_error = _email_preflight_error()
    if preflight_error:
        return _skip(preflight_error)
    full_message, _ = _comment_notification_messages(ticket, comment)

    def deliver() -> DeliveredDomainEffect:
        from services.email_service import enviar_email_ticket_admin

        delivered = enviar_email_ticket_admin(
            ticket,
            admin_user=admin_user,
            tipo_ticket=ticket_type,
            ticket_data=_ticket_delivery_data(ticket, ticket_type),
            comentario_reciente=comment,
            mensaje_resumen=full_message,
        )
        return _provider_accepted(delivered)

    return PreparedDomainEffect(deliver=deliver)


def _prepare_comment_requester_email(
    claim: DomainEffectClaim,
) -> PreparedDomainEffect | SkippedDomainEffect:
    loaded = _load_scoped_comment(claim)
    if loaded is None:
        return _skip("ticket_comment_not_found")
    comment, ticket, _ = loaded
    if not bool(getattr(comment, "es_admin", False)):
        raise PermanentDomainEffectError("ticket_comment_direction_invalid")

    recipient = _ticket_requester_email(ticket)
    recipient_error = _email_recipient_preflight_error(
        recipient,
        recipient_type="requester",
    )
    if recipient_error:
        return _skip(recipient_error)
    preflight_error = _email_preflight_error()
    if preflight_error:
        return _skip(preflight_error)
    full_message, _ = _comment_notification_messages(ticket, comment)

    def deliver() -> DeliveredDomainEffect:
        from services.email_service import enviar_email_ticket_novedad

        # MunicipioTicket stores these fields with the ``_vecino`` suffix,
        # while the legacy provider adapter reads the generic attribute.
        if not getattr(ticket, "email", None):
            setattr(ticket, "email", recipient)
        delivered = enviar_email_ticket_novedad(
            ticket,
            full_message,
            comentario_reciente=comment,
        )
        return _provider_accepted(delivered)

    return PreparedDomainEffect(deliver=deliver)


def _prepare_comment_requester_sms(
    claim: DomainEffectClaim,
) -> PreparedDomainEffect | SkippedDomainEffect:
    loaded = _load_scoped_comment(claim)
    if loaded is None:
        return _skip("ticket_comment_not_found")
    comment, ticket, _ = loaded
    if not bool(getattr(comment, "es_admin", False)):
        raise PermanentDomainEffectError("ticket_comment_direction_invalid")

    raw_phone = _ticket_requester_phone(ticket)
    if not raw_phone:
        return _skip("requester_phone_missing")
    from utils.validators import normalize_phone

    normalized_phone = normalize_phone(raw_phone)
    if not normalized_phone:
        return _skip("requester_phone_invalid")
    _, short_message = _comment_notification_messages(ticket, comment)

    try:
        twilio_preflight = prepare_bound_tenant_twilio_message(
            tenant_id=int(claim.tenant_id),
            channel="sms",
            expected_sender_binding=_claim_provider_sender_binding(claim),
            recipient=normalized_phone,
            body=short_message,
        )
    except TenantTwilioScopeError as exc:
        raise PermanentDomainEffectError(exc.code) from exc
    if twilio_preflight.reason_code:
        return _skip(twilio_preflight.reason_code)
    prepared_message = twilio_preflight.prepared
    if prepared_message is None:
        raise PermanentDomainEffectError("ticket_comment_twilio_preflight_invalid")

    def deliver() -> DeliveredDomainEffect:
        provider_ref = send_prepared_tenant_twilio_message(prepared_message)
        if not provider_ref:
            raise AmbiguousDomainEffectError("provider_acceptance_unknown")
        return DeliveredDomainEffect(
            provider_ref=provider_ref,
            result={"delivery": "provider_accepted"},
        )

    return PreparedDomainEffect(deliver=deliver)


def _prepare_comment_requester_whatsapp(
    claim: DomainEffectClaim,
) -> PreparedDomainEffect | SkippedDomainEffect:
    loaded = _load_scoped_comment(claim)
    if loaded is None:
        return _skip("ticket_comment_not_found")
    comment, ticket, ticket_type = loaded
    if not bool(getattr(comment, "es_admin", False)):
        raise PermanentDomainEffectError("ticket_comment_direction_invalid")
    if ticket_type == "pyme" and not pyme_whatsapp_chat_enabled():
        return _skip("pyme_whatsapp_disabled")
    if ticket_type not in {"municipio", "pyme"}:
        return _skip("whatsapp_not_applicable")

    raw_phone = _ticket_requester_phone(ticket)
    if not raw_phone:
        return _skip("requester_phone_missing")
    from utils.validators import normalize_phone

    normalized_phone = normalize_phone(raw_phone)
    if not normalized_phone:
        return _skip("requester_phone_invalid")
    _, short_message = _comment_notification_messages(ticket, comment)
    media_urls, media_error = _comment_attachment_media_urls(comment)
    if media_error:
        return _skip(media_error)

    try:
        twilio_preflight = prepare_bound_tenant_twilio_message(
            tenant_id=int(claim.tenant_id),
            channel="whatsapp",
            expected_sender_binding=_claim_provider_sender_binding(claim),
            recipient=normalized_phone,
            body=short_message,
            media_urls=media_urls,
        )
    except TenantTwilioScopeError as exc:
        raise PermanentDomainEffectError(exc.code) from exc
    if twilio_preflight.reason_code:
        return _skip(twilio_preflight.reason_code)
    prepared_message = twilio_preflight.prepared
    if prepared_message is None:
        raise PermanentDomainEffectError("ticket_comment_twilio_preflight_invalid")

    def deliver() -> DeliveredDomainEffect:
        provider_ref = send_prepared_tenant_twilio_message(
            prepared_message,
        )
        if not provider_ref:
            raise AmbiguousDomainEffectError("provider_acceptance_unknown")
        return DeliveredDomainEffect(
            provider_ref=provider_ref,
            result={"delivery": "provider_accepted"},
        )

    return PreparedDomainEffect(deliver=deliver)


def _prepare_comment_realtime(
    claim: DomainEffectClaim,
) -> PreparedDomainEffect | SkippedDomainEffect:
    loaded = _load_scoped_comment(claim)
    if loaded is None:
        return _skip("ticket_comment_not_found")
    comment, ticket, ticket_type = loaded

    try:
        from socket_service import emit_new_chat_message
    except (ImportError, RuntimeError):
        return _skip("realtime_unavailable")
    if not callable(emit_new_chat_message):
        return _skip("realtime_unavailable")

    def deliver() -> DeliveredDomainEffect:
        emit_new_chat_message(
            {
                "tenant_type": ticket_type,
                "ticket_id": ticket.id,
                "tenant_profile_id": getattr(ticket, "tenant_id", None),
                "municipio_id": getattr(ticket, "municipio_id", None),
                "message": comment.to_dict(),
            }
        )
        return DeliveredDomainEffect(result={"dispatch": "realtime_event_emitted"})

    return PreparedDomainEffect(deliver=deliver)


def _prepare_tenant_reply_email(
    claim: DomainEffectClaim,
) -> PreparedDomainEffect | SkippedDomainEffect:
    loaded = _load_scoped_tenant_reply(claim)
    if loaded is None:
        return _skip("tenant_ticket_reply_not_found")
    ticket, event, profile, reply_event = loaded
    if str(event.get("visibility") or "public").strip().lower() != "public":
        raise PermanentDomainEffectError("tenant_ticket_reply_visibility_invalid")
    if reply_event is None:
        # Pre-v2 rows never pinned their destination. Re-resolving mutable
        # ticket/User contact could redirect a delayed reply, so quarantine it
        # for explicit operator review instead of performing provider I/O.
        raise PermanentDomainEffectError(
            "tenant_ticket_reply_legacy_recipient_unpinned"
        )
    recipient = reply_event.recipient_email
    recipient_error = _email_recipient_preflight_error(
        recipient,
        recipient_type="requester",
    )
    if recipient_error:
        return _skip(recipient_error)
    preflight_error = _email_preflight_error()
    if preflight_error:
        return _skip(preflight_error)
    body = str(event.get("body") or "").strip()
    tenant_name = str(getattr(profile, "nombre", None) or "el equipo").strip()
    subject = f"Respuesta de {tenant_name} - solicitud #{ticket.id}"
    body_html = "<p>" + escape(body).replace("\n", "<br>") + "</p>"

    def deliver() -> DeliveredDomainEffect:
        from services.email_service import enviar_email

        return _provider_accepted(
            enviar_email(
                recipient,
                subject,
                body_html,
                cuerpo_texto=body,
            )
        )

    return PreparedDomainEffect(deliver=deliver)


def _prepare_tenant_reply_whatsapp(
    claim: DomainEffectClaim,
) -> PreparedDomainEffect | SkippedDomainEffect:
    loaded = _load_scoped_tenant_reply(claim)
    if loaded is None:
        return _skip("tenant_ticket_reply_not_found")
    ticket, event, _profile, reply_event = loaded
    if str(event.get("visibility") or "public").strip().lower() != "public":
        raise PermanentDomainEffectError("tenant_ticket_reply_visibility_invalid")
    if reply_event is None:
        raise PermanentDomainEffectError(
            "tenant_ticket_reply_legacy_recipient_unpinned"
        )
    raw_phone = reply_event.recipient_phone
    if not raw_phone:
        return _skip("requester_phone_missing")
    from utils.validators import normalize_phone

    normalized_phone = normalize_phone(raw_phone)
    if not normalized_phone:
        return _skip("requester_phone_invalid")
    try:
        twilio_preflight = prepare_bound_tenant_twilio_message(
            tenant_id=int(claim.tenant_id),
            channel="whatsapp",
            expected_sender_binding=_claim_provider_sender_binding(claim),
            recipient=normalized_phone,
            body=str(event.get("body") or "").strip(),
        )
    except TenantTwilioScopeError as exc:
        raise PermanentDomainEffectError(exc.code) from exc
    if twilio_preflight.reason_code:
        return _skip(twilio_preflight.reason_code)
    prepared_message = twilio_preflight.prepared
    if prepared_message is None:
        raise PermanentDomainEffectError("tenant_ticket_reply_twilio_preflight_invalid")

    def deliver() -> DeliveredDomainEffect:
        provider_ref = send_prepared_tenant_twilio_message(prepared_message)
        if not provider_ref:
            raise AmbiguousDomainEffectError("provider_acceptance_unknown")
        return DeliveredDomainEffect(
            provider_ref=provider_ref,
            result={"delivery": "provider_accepted"},
        )

    return PreparedDomainEffect(deliver=deliver)


def tenant_ticket_reply_realtime_payload(
    ticket: TenantTicket,
    event: Mapping[str, Any],
) -> dict[str, Any]:
    # Tenant rooms contain every operator for the tenant, including employees
    # with narrower ticket-category scopes.  This payload therefore carries
    # routing data only; authorized clients refetch through the HTTP boundary.
    return {
        "contract_version": "tenant_ticket.reply.realtime.v1",
        "tenant_type": "tenant",
        "tipo": "tenant",
        "tenant_profile_id": ticket.tenant_id,
        "delivery": "tenant_collection_invalidation",
    }


def emit_tenant_ticket_reply_realtime(
    ticket: TenantTicket,
    event: Mapping[str, Any],
) -> None:
    from socket_service import emit_new_chat_message

    emit_new_chat_message(tenant_ticket_reply_realtime_payload(ticket, event))


def _prepare_tenant_reply_realtime(
    claim: DomainEffectClaim,
) -> PreparedDomainEffect | SkippedDomainEffect:
    loaded = _load_scoped_tenant_reply(claim)
    if loaded is None:
        return _skip("tenant_ticket_reply_not_found")
    ticket, event, _profile, _reply_event = loaded

    try:
        from socket_service import emit_new_chat_message
    except (ImportError, RuntimeError):
        return _skip("realtime_unavailable")
    if not callable(emit_new_chat_message):
        return _skip("realtime_unavailable")

    def deliver() -> DeliveredDomainEffect:
        emit_tenant_ticket_reply_realtime(ticket, event)
        return DeliveredDomainEffect(result={"dispatch": "realtime_event_emitted"})

    return PreparedDomainEffect(deliver=deliver)


TICKET_DOMAIN_EFFECT_REGISTRY = DomainEffectRegistry()
TICKET_DOMAIN_EFFECT_REGISTRY.register(
    SIGEM_HANDLER,
    _prepare_sigem,
    payload_validator=_validate_ticket_effect_payload,
)
TICKET_DOMAIN_EFFECT_REGISTRY.register(
    ADMIN_EMAIL_HANDLER,
    _prepare_admin_email,
    payload_validator=_validate_ticket_effect_payload,
)
TICKET_DOMAIN_EFFECT_REGISTRY.register(
    REQUESTER_EMAIL_HANDLER,
    _prepare_requester_email,
    payload_validator=_validate_ticket_effect_payload,
)
TICKET_DOMAIN_EFFECT_REGISTRY.register(
    COMMENT_ADMIN_EMAIL_HANDLER,
    _prepare_comment_admin_email,
    payload_validator=_validate_ticket_effect_payload,
)
TICKET_DOMAIN_EFFECT_REGISTRY.register(
    COMMENT_REQUESTER_EMAIL_HANDLER,
    _prepare_comment_requester_email,
    payload_validator=_validate_ticket_effect_payload,
)
TICKET_DOMAIN_EFFECT_REGISTRY.register(
    COMMENT_REQUESTER_SMS_HANDLER,
    _prepare_comment_requester_sms,
    payload_validator=_validate_ticket_twilio_effect_payload,
)
TICKET_DOMAIN_EFFECT_REGISTRY.register(
    COMMENT_REQUESTER_WHATSAPP_HANDLER,
    _prepare_comment_requester_whatsapp,
    payload_validator=_validate_ticket_twilio_effect_payload,
)
TICKET_DOMAIN_EFFECT_REGISTRY.register(
    COMMENT_REALTIME_HANDLER,
    _prepare_comment_realtime,
    payload_validator=_validate_ticket_effect_payload,
)
TICKET_DOMAIN_EFFECT_REGISTRY.register(
    TENANT_REPLY_EMAIL_HANDLER,
    _prepare_tenant_reply_email,
    payload_validator=_validate_tenant_reply_effect_payload,
)
TICKET_DOMAIN_EFFECT_REGISTRY.register(
    TENANT_REPLY_WHATSAPP_HANDLER,
    _prepare_tenant_reply_whatsapp,
    payload_validator=_validate_tenant_reply_twilio_effect_payload,
)
TICKET_DOMAIN_EFFECT_REGISTRY.register(
    TENANT_REPLY_REALTIME_HANDLER,
    _prepare_tenant_reply_realtime,
    payload_validator=_validate_tenant_reply_effect_payload,
)

# CRM location/form actions share this registry so the existing worker remains
# the single provider-I/O authority.
register_crm_action_handlers(TICKET_DOMAIN_EFFECT_REGISTRY)


def stage_ticket_created_effects(
    ticket: MunicipioTicket | PymeTicket,
    *,
    tipo_ticket: TicketType,
    expected_owner_id: Any = None,
    session=None,
    registry: DomainEffectRegistry | None = None,
) -> bool:
    """Stage ticket effects for one canary tenant without committing.

    ``True`` means direct legacy senders must not run after commit.  ``False``
    means this tenant remains on the existing direct-send path.
    """

    if tipo_ticket not in {"municipio", "pyme"}:
        raise ValueError("ticket_effect_type_invalid")
    if not has_app_context():
        raise DomainEffectOutboxConfigurationError("domain_effect_app_context_required")

    tenant_id = _positive_int(getattr(ticket, "tenant_id", None))
    if tenant_id is None:
        mode = str(
            current_app.config.get("DOMAIN_EFFECT_OUTBOX_MODE", "legacy") or "legacy"
        ).strip().lower()
        if mode == "queue":
            raise DomainEffectOutboxConfigurationError("domain_effect_tenant_invalid")
        return False

    policy = resolve_domain_effect_outbox_policy(
        current_app.config,
        tenant_id=tenant_id,
    )
    if not policy.enabled:
        return False

    effect_session = session or db.session
    profile = effect_session.get(TenantProfile, tenant_id)
    expected_owner = _positive_int(expected_owner_id)
    if tipo_ticket == "municipio":
        ticket_owner = _positive_int(getattr(ticket, "municipio_id", None))
        if expected_owner is None:
            expected_owner = ticket_owner
    else:
        ticket_owner = None
    profile_owner = (
        _profile_owner_id(profile, tipo_ticket) if profile is not None else None
    )
    if (
        profile is None
        or str(getattr(profile, "tipo", "")).strip() != tipo_ticket
        or not bool(getattr(profile, "is_active", False))
        or expected_owner is None
        or profile_owner != expected_owner
        or (tipo_ticket == "municipio" and ticket_owner != expected_owner)
    ):
        raise DomainEffectOutboxConfigurationError(
            "domain_effect_ticket_tenant_binding_invalid"
        )

    ticket_id = _positive_int(getattr(ticket, "id", None))
    if ticket_id is None:
        raise DomainEffectOutboxConfigurationError("domain_effect_aggregate_invalid")

    expected_model = MunicipioTicket if tipo_ticket == "municipio" else PymeTicket
    if not isinstance(ticket, expected_model):
        raise ValueError("ticket_effect_aggregate_mismatch")

    aggregate_type = (
        MUNICIPAL_TICKET_AGGREGATE
        if tipo_ticket == "municipio"
        else PYME_TICKET_AGGREGATE
    )
    effect_registry = registry or TICKET_DOMAIN_EFFECT_REGISTRY
    common = {
        "tenant_id": tenant_id,
        "aggregate_type": aggregate_type,
        "aggregate_ref": str(ticket_id),
        "intent_secret": policy.secret,
        "registry": effect_registry,
        "payload": {
            "owner_binding": _owner_binding(tenant_id, tipo_ticket, expected_owner),
        },
        "max_attempts": policy.max_attempts,
        "max_payload_bytes": policy.max_payload_bytes,
        "session": effect_session,
    }
    specs = [
        {
            "effect_type": "ticket.created.email.admin",
            "handler_name": ADMIN_EMAIL_HANDLER,
            "channel": "email",
            "recipient_ref": TENANT_ADMIN_RECIPIENT,
        },
        {
            "effect_type": "ticket.created.email.requester",
            "handler_name": REQUESTER_EMAIL_HANDLER,
            "channel": "email",
            "recipient_ref": TICKET_REQUESTER_RECIPIENT,
        },
    ]
    if tipo_ticket == "municipio":
        specs.insert(
            0,
            {
                "effect_type": "ticket.created.sigem",
                "handler_name": SIGEM_HANDLER,
                "channel": "sigem",
                "recipient_ref": SIGEM_RECIPIENT,
            },
        )

    for spec in specs:
        stage_domain_effect(
            **common,
            **spec,
            effect_key=(
                f"{aggregate_type}:{ticket_id}:created:"
                f"{spec['channel']}:{spec['recipient_ref']}"
            ),
        )
    return True


def stage_ticket_comment_effects(
    comment: TicketComentario,
    ticket: MunicipioTicket | PymeTicket,
    *,
    tipo_ticket: TicketType,
    emit_notifications: bool = True,
    emit_socket: bool = True,
    session=None,
    registry: DomainEffectRegistry | None = None,
) -> bool:
    """Stage all externally visible comment effects in the comment transaction."""

    if tipo_ticket not in {"municipio", "pyme"}:
        raise ValueError("ticket_comment_effect_type_invalid")
    if not has_app_context():
        raise DomainEffectOutboxConfigurationError("domain_effect_app_context_required")

    tenant_id = _positive_int(getattr(ticket, "tenant_id", None))
    if tenant_id is None:
        mode = str(
            current_app.config.get("DOMAIN_EFFECT_OUTBOX_MODE", "legacy") or "legacy"
        ).strip().lower()
        if mode == "queue":
            raise DomainEffectOutboxConfigurationError("domain_effect_tenant_invalid")
        return False
    policy = resolve_domain_effect_outbox_policy(
        current_app.config,
        tenant_id=tenant_id,
    )
    if not policy.enabled:
        return False

    effect_session = session or db.session
    expected_ticket_model = (
        MunicipioTicket if tipo_ticket == "municipio" else PymeTicket
    )
    if not isinstance(ticket, expected_ticket_model) or not isinstance(
        comment,
        TicketComentario,
    ):
        raise ValueError("ticket_comment_effect_aggregate_mismatch")
    ticket_id = _positive_int(getattr(ticket, "id", None))
    comment_id = _positive_int(getattr(comment, "id", None))
    if ticket_id is None or comment_id is None:
        raise DomainEffectOutboxConfigurationError("domain_effect_aggregate_invalid")
    linked_ticket_id = _positive_int(
        getattr(
            comment,
            "municipio_ticket_id" if tipo_ticket == "municipio" else "pyme_ticket_id",
            None,
        )
    )
    opposite_ticket_id = getattr(
        comment,
        "pyme_ticket_id" if tipo_ticket == "municipio" else "municipio_ticket_id",
        None,
    )
    if linked_ticket_id != ticket_id or opposite_ticket_id is not None:
        raise DomainEffectOutboxConfigurationError(
            "domain_effect_comment_ticket_binding_invalid"
        )

    profile = effect_session.get(TenantProfile, tenant_id)
    profile_owner = (
        _profile_owner_id(profile, tipo_ticket) if profile is not None else None
    )
    if (
        profile is None
        or str(getattr(profile, "tipo", "")).strip() != tipo_ticket
        or not bool(getattr(profile, "is_active", False))
        or profile_owner is None
        or (
            tipo_ticket == "municipio"
            and _positive_int(getattr(ticket, "municipio_id", None)) != profile_owner
        )
    ):
        raise DomainEffectOutboxConfigurationError(
            "domain_effect_comment_tenant_binding_invalid"
        )

    _bind_comment_attachment(
        comment,
        ticket,
        tipo_ticket,
        session=effect_session,
    )

    aggregate_type = (
        MUNICIPAL_COMMENT_AGGREGATE
        if tipo_ticket == "municipio"
        else PYME_COMMENT_AGGREGATE
    )
    effect_registry = registry or TICKET_DOMAIN_EFFECT_REGISTRY
    common = {
        "tenant_id": tenant_id,
        "aggregate_type": aggregate_type,
        "aggregate_ref": str(comment_id),
        "intent_secret": policy.secret,
        "registry": effect_registry,
        "max_attempts": policy.max_attempts,
        "max_payload_bytes": policy.max_payload_bytes,
        "session": effect_session,
    }
    owner_binding = _owner_binding(
        tenant_id,
        tipo_ticket,
        profile_owner,
    )
    specs: list[dict[str, str]] = []
    if emit_notifications:
        if bool(getattr(comment, "es_admin", False)):
            specs.extend(
                [
                    {
                        "effect_type": "ticket.comment.email.requester",
                        "handler_name": COMMENT_REQUESTER_EMAIL_HANDLER,
                        "channel": "email",
                        "recipient_ref": TICKET_REQUESTER_RECIPIENT,
                    },
                    {
                        "effect_type": "ticket.comment.sms.requester",
                        "handler_name": COMMENT_REQUESTER_SMS_HANDLER,
                        "channel": "sms",
                        "recipient_ref": TICKET_REQUESTER_RECIPIENT,
                    },
                ]
            )
            if tipo_ticket == "municipio" or (
                tipo_ticket == "pyme" and pyme_whatsapp_chat_enabled()
            ):
                specs.append(
                    {
                        "effect_type": "ticket.comment.whatsapp.requester",
                        "handler_name": COMMENT_REQUESTER_WHATSAPP_HANDLER,
                        "channel": "whatsapp",
                        "recipient_ref": TICKET_REQUESTER_RECIPIENT,
                    }
                )
        else:
            specs.append(
                {
                    "effect_type": "ticket.comment.email.admin",
                    "handler_name": COMMENT_ADMIN_EMAIL_HANDLER,
                    "channel": "email",
                    "recipient_ref": TENANT_ADMIN_RECIPIENT,
                }
            )
    if emit_socket:
        specs.append(
            {
                "effect_type": "ticket.comment.realtime",
                "handler_name": COMMENT_REALTIME_HANDLER,
                "channel": "realtime",
                "recipient_ref": TICKET_TIMELINE_RECIPIENT,
            }
        )

    for spec in specs:
        payload = {"owner_binding": owner_binding}
        if spec["channel"] in {"sms", "whatsapp"}:
            payload["provider_sender_binding"] = (
                build_tenant_twilio_sender_binding(
                    tenant_id=tenant_id,
                    channel=spec["channel"],
                    session=effect_session,
                )
            )
        stage_domain_effect(
            **common,
            **spec,
            payload=payload,
            effect_key=(
                f"{aggregate_type}:{comment_id}:created:{spec['handler_name']}"
            ),
        )
    return True


def stage_tenant_ticket_reply_effects(
    ticket: TenantTicket,
    reply_event: TenantTicketReplyEvent,
    *,
    requested_channels: list[str] | tuple[str, ...] = (),
    emit_socket: bool = True,
    session=None,
    registry: DomainEffectRegistry | None = None,
) -> bool:
    """Stage TenantTicket reply delivery in the timeline transaction."""

    if not has_app_context():
        raise DomainEffectOutboxConfigurationError("domain_effect_app_context_required")
    if not isinstance(ticket, TenantTicket) or not isinstance(
        reply_event,
        TenantTicketReplyEvent,
    ):
        raise ValueError("tenant_ticket_reply_aggregate_mismatch")
    tenant_id = _positive_int(getattr(ticket, "tenant_id", None))
    ticket_id = _positive_int(getattr(ticket, "id", None))
    event_id = str(getattr(reply_event, "event_id", None) or "").strip()
    reply_event_id = _positive_int(getattr(reply_event, "id", None))
    if (
        tenant_id is None
        or ticket_id is None
        or reply_event_id is None
        or not event_id
        or _positive_int(getattr(reply_event, "tenant_id", None)) != tenant_id
        or _positive_int(getattr(reply_event, "ticket_id", None)) != ticket_id
    ):
        raise DomainEffectOutboxConfigurationError(
            "tenant_ticket_reply_aggregate_invalid"
        )
    policy = resolve_domain_effect_outbox_policy(
        current_app.config,
        tenant_id=tenant_id,
    )
    if not policy.enabled:
        return False

    effect_session = session or db.session
    profile = effect_session.get(TenantProfile, tenant_id)
    if profile is None or not bool(getattr(profile, "is_active", False)):
        raise DomainEffectOutboxConfigurationError(
            "tenant_ticket_reply_tenant_binding_invalid"
        )
    visibility = str(reply_event.visibility or "public").strip().lower()
    normalized_channels = []
    for raw_channel in requested_channels or ():
        channel = str(raw_channel or "").strip().lower()
        if channel in {"email", "whatsapp"} and channel not in normalized_channels:
            normalized_channels.append(channel)
    if visibility != "public" and normalized_channels:
        raise DomainEffectOutboxConfigurationError(
            "tenant_ticket_reply_internal_external_dispatch_invalid"
        )

    aggregate_ref = f"{ticket_id}:{event_id}"
    effect_registry = registry or TICKET_DOMAIN_EFFECT_REGISTRY
    common = {
        "tenant_id": tenant_id,
        "aggregate_type": TENANT_REPLY_AGGREGATE,
        "aggregate_ref": aggregate_ref,
        "intent_secret": policy.secret,
        "registry": effect_registry,
        "max_attempts": policy.max_attempts,
        "max_payload_bytes": policy.max_payload_bytes,
        "session": effect_session,
    }
    specs: list[dict[str, str]] = []
    if "email" in normalized_channels:
        specs.append(
            {
                "effect_type": "tenant_ticket.reply.email.requester",
                "handler_name": TENANT_REPLY_EMAIL_HANDLER,
                "channel": "email",
            }
        )
    if "whatsapp" in normalized_channels:
        specs.append(
            {
                "effect_type": "tenant_ticket.reply.whatsapp.requester",
                "handler_name": TENANT_REPLY_WHATSAPP_HANDLER,
                "channel": "whatsapp",
            }
        )
    if emit_socket:
        specs.append(
            {
                "effect_type": "tenant_ticket.reply.realtime",
                "handler_name": TENANT_REPLY_REALTIME_HANDLER,
                "channel": "realtime",
            }
        )

    tenant_binding = _tenant_reply_binding(tenant_id, ticket_id)
    for spec in specs:
        payload = {"tenant_binding": tenant_binding}
        if spec["channel"] == "whatsapp":
            payload["provider_sender_binding"] = build_tenant_twilio_sender_binding(
                tenant_id=tenant_id,
                channel="whatsapp",
                session=effect_session,
            )
        stage_domain_effect(
            **common,
            **spec,
            recipient_ref=_tenant_reply_recipient_ref(
                secret=str(policy.secret or ""),
                reply_event=reply_event,
                channel=spec["channel"],
            ),
            payload=payload,
            effect_key=(
                f"{TENANT_REPLY_AGGREGATE}:{ticket_id}:{event_id}:"
                f"{spec['handler_name']}"
            ),
        )
    return bool(specs)


__all__ = [
    "ADMIN_EMAIL_HANDLER",
    "COMMENT_ADMIN_EMAIL_HANDLER",
    "COMMENT_REALTIME_HANDLER",
    "COMMENT_REQUESTER_EMAIL_HANDLER",
    "COMMENT_REQUESTER_SMS_HANDLER",
    "COMMENT_REQUESTER_WHATSAPP_HANDLER",
    "MUNICIPAL_COMMENT_AGGREGATE",
    "MUNICIPAL_TICKET_AGGREGATE",
    "PYME_COMMENT_AGGREGATE",
    "PYME_TICKET_AGGREGATE",
    "REQUESTER_EMAIL_HANDLER",
    "SIGEM_HANDLER",
    "TENANT_REPLY_AGGREGATE",
    "TENANT_REPLY_EMAIL_HANDLER",
    "TENANT_REPLY_REALTIME_HANDLER",
    "TENANT_REPLY_WHATSAPP_HANDLER",
    "TICKET_DOMAIN_EFFECT_REGISTRY",
    "emit_tenant_ticket_reply_realtime",
    "pyme_whatsapp_chat_enabled",
    "stage_tenant_ticket_reply_effects",
    "stage_ticket_comment_effects",
    "stage_ticket_created_effects",
    "tenant_ticket_reply_contact",
    "tenant_ticket_reply_realtime_payload",
]
