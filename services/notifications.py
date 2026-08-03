"""Centralized notification helpers for tickets.

These helpers prefer environment-driven SMTP configuration so they can work
in Render/Heroku deployments without additional wiring. When credentials are
missing they log a clear warning instead of raising to keep ticket flows
functional.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import smtplib
from email.message import EmailMessage
from typing import Any, Iterable, Optional

from flask import current_app

logger = logging.getLogger(__name__)


# These compatibility helpers do not implement a provider transport.  Keep the
# capability flags explicit and fail closed so callers cannot mistake a log
# line for provider acceptance.  Durable, tenant-bound workers may opt in only
# after they own a complete sender/template/idempotency contract.
WHATSAPP_TEMPLATE_TRANSPORT_IMPLEMENTED = False
ORDER_WHATSAPP_TRANSPORT_IMPLEMENTED = False
SMS_TRANSPORT_IMPLEMENTED = False


@dataclass(frozen=True)
class NotificationDispatchResult:
    """PII-free acknowledgement returned by compatibility notification APIs."""

    channel: str
    accepted: bool
    reason_code: str
    tenant_id: int | None = None
    template_registry_id: int | None = None
    idempotency_bound: bool = False
    provider_message_id: str | None = None

    def __bool__(self) -> bool:
        return self.accepted


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _safe_idempotency_key(value: Any) -> str | None:
    rendered = str(value or "").strip()
    if not 8 <= len(rendered) <= 128:
        return None
    if any(character in rendered for character in "\r\n\x00"):
        return None
    return rendered


def _blocked_dispatch_result(
    channel: str,
    reason_code: str,
    *,
    tenant_id: Any = None,
    template_registry_id: Any = None,
    idempotency_key: Any = None,
) -> NotificationDispatchResult:
    return NotificationDispatchResult(
        channel=channel,
        accepted=False,
        reason_code=reason_code,
        tenant_id=_positive_int(tenant_id),
        template_registry_id=_positive_int(template_registry_id),
        idempotency_bound=_safe_idempotency_key(idempotency_key) is not None,
    )


def _log_legacy_channel_blocked(
    *,
    channel: str,
    reason_code: str,
    tenant_id: Any = None,
    template_registry_id: Any = None,
    idempotency_key: Any = None,
    has_recipient: bool,
    body_length: int = 0,
) -> None:
    """Log only allowlisted operational metadata, never recipient/body PII."""

    logger.info(
        "[notifications] legacy dispatch blocked channel=%s reason=%s "
        "tenant_id=%s template_registry_id=%s idempotency_bound=%s "
        "has_recipient=%s body_length=%s",
        channel,
        reason_code,
        _positive_int(tenant_id),
        _positive_int(template_registry_id),
        _safe_idempotency_key(idempotency_key) is not None,
        bool(has_recipient),
        max(0, int(body_length or 0)),
    )


def _log_dispatch(channel: str, ticket: Any, event_type: str, *, ok: bool = True) -> None:
    """Log a dispatch attempt in a consistent format.

    The ticket object is treated opaquely to avoid tight coupling with the
    ticket models while still providing traceability for future integrations.
    """

    ticket_id = getattr(ticket, "id", None)
    tenant_id = getattr(ticket, "tenant_id", None)
    status = "ok" if ok else "skipped"
    logger.info(
        "[notifications] %s channel=%s event=%s ticket_id=%s tenant_id=%s",
        status,
        channel,
        event_type,
        ticket_id,
        tenant_id,
    )


def _build_smtp_client():
    """Create an SMTP client using environment-driven config."""

    host = current_app.config.get("SMTP_HOST")
    port = int(current_app.config.get("SMTP_PORT") or 0)
    username = current_app.config.get("SMTP_USERNAME")
    password = current_app.config.get("SMTP_PASSWORD")
    use_tls = bool(current_app.config.get("SMTP_USE_TLS"))
    use_ssl = bool(current_app.config.get("SMTP_USE_SSL"))

    if not host or not port:
        logger.warning("[notifications] SMTP credentials faltantes; omitiendo envío")
        return None

    if use_ssl:
        client = smtplib.SMTP_SSL(host, port)
    else:
        client = smtplib.SMTP(host, port)

    client.ehlo()
    if use_tls and not use_ssl:
        client.starttls()
        client.ehlo()

    if username and password:
        try:
            client.login(username, password)
        except smtplib.SMTPException:
            logger.exception("[notifications] Error autenticando con el servidor SMTP")
            client.quit()
            return None

    return client


def _format_sender() -> Optional[str]:
    address = current_app.config.get("MAIL_FROM_ADDRESS")
    name = current_app.config.get("MAIL_FROM_NAME")
    if not address:
        return None
    if name:
        return f"{name} <{address}>"
    return address


def _send_email_message(to_addresses: Iterable[str], subject: str, body: str) -> None:
    sender = _format_sender()
    if not sender:
        logger.warning("[notifications] MAIL_FROM_ADDRESS no configurado; omitiendo envío")
        return

    recipients = [addr for addr in to_addresses if addr]
    if not recipients:
        logger.info("[notifications] Sin destinatarios; no se envía correo")
        return

    client = _build_smtp_client()
    if client is None:
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    try:
        client.send_message(msg)
    except smtplib.SMTPException:
        logger.exception("[notifications] Error enviando correo de ticket")
    finally:
        try:
            client.quit()
        except Exception:
            logger.debug("[notifications] No se pudo cerrar la conexión SMTP limpiamente")


def _ticket_recipient(ticket: Any) -> Optional[str]:
    """Infer a recipient email from the ticket object if available."""

    for attr in ("email_vecino", "email", "usuario_email"):
        value = getattr(ticket, attr, None)
        if value:
            return value
    return None


def send_ticket_email(ticket: Any, event_type: str) -> None:
    """Send an email notification for a ticket event."""

    recipient = _ticket_recipient(ticket)
    subject = f"Actualización de ticket #{getattr(ticket, 'nro_ticket', getattr(ticket, 'id', ''))}".strip()
    body = (
        f"Tu ticket ha generado el evento '{event_type}'.\n"
        "Gracias por contactarte con nosotros."
    )

    _send_email_message([recipient] if recipient else [], subject, body)
    _log_dispatch("email", ticket, event_type, ok=bool(recipient))


def send_ticket_whatsapp(ticket: Any, event_type: str) -> NotificationDispatchResult:
    """Fail closed until a durable tenant/template-bound adapter owns delivery."""

    tenant_id = _positive_int(getattr(ticket, "tenant_id", None))
    _log_dispatch("whatsapp", ticket, event_type, ok=False)
    reason = (
        "whatsapp_tenant_scope_required"
        if tenant_id is None
        else "whatsapp_template_transport_unavailable"
    )
    _log_legacy_channel_blocked(
        channel="whatsapp",
        reason_code=reason,
        tenant_id=tenant_id,
        has_recipient=bool(
            getattr(ticket, "telefono", None)
            or getattr(ticket, "telefono_vecino", None)
            or getattr(ticket, "telefono_cliente", None)
        ),
    )
    return _blocked_dispatch_result("whatsapp", reason, tenant_id=tenant_id)


def send_ticket_sms(ticket: Any, event_type: str) -> NotificationDispatchResult:
    """Fail closed until a durable tenant-bound SMS adapter owns delivery."""

    tenant_id = _positive_int(getattr(ticket, "tenant_id", None))
    _log_dispatch("sms", ticket, event_type, ok=False)
    reason = "sms_tenant_scope_required" if tenant_id is None else "sms_transport_unavailable"
    _log_legacy_channel_blocked(
        channel="sms",
        reason_code=reason,
        tenant_id=tenant_id,
        has_recipient=bool(
            getattr(ticket, "telefono", None)
            or getattr(ticket, "telefono_vecino", None)
            or getattr(ticket, "telefono_cliente", None)
        ),
    )
    return _blocked_dispatch_result("sms", reason, tenant_id=tenant_id)


def send_ticket_history_email(ticket: Any, history_html: str | None = None) -> None:
    """Send a ticket history email to the citizen if possible."""

    recipient = _ticket_recipient(ticket)
    if not recipient:
        _log_dispatch("email_history", ticket, "ticket_history", ok=False)
        logger.info("[notifications] Ticket sin destinatario para historial")
        return

    subject = (
        f"Historial del ticket #{getattr(ticket, 'nro_ticket', getattr(ticket, 'id', ''))}"
    ).strip()
    body = "Se adjunta el historial de tu ticket." if history_html else ""
    _send_email_message([recipient], subject, body or "Historial disponible en el panel.")
    _log_dispatch("email_history", ticket, "ticket_history")


# --- Backwards-compatibility helpers ---
# Legacy imports expect these names from historical modules. They now
# delegate to the centralized logging so older call sites keep working
# without breaking deployments while full channel support is built out.


def enviar_notificacion_whatsapp_con_plantilla(
    telefono: str | None,
    nombre: str | None,
    ticket_id: str | int | None,
    categoria: str | None = None,
    mensaje: str | None = None,
    *,
    tenant_id: int | None = None,
    template_registry_id: int | None = None,
    expected_sender_binding: str | None = None,
    idempotency_key: str | None = None,
) -> NotificationDispatchResult:
    """Compatibility shim with an explicit, fail-closed delivery contract.

    The positional legacy arguments contain no trustworthy tenant, sender,
    approved registry row, or idempotency binding. They are therefore never
    enough to contact Twilio. Keyword-only bindings document what a future
    durable adapter must supply, but this shim remains unavailable until the
    real transport replaces it.
    """

    del nombre, ticket_id, categoria  # User-controlled values must not reach logs.
    normalized_tenant_id = _positive_int(tenant_id)
    normalized_template_id = _positive_int(template_registry_id)
    normalized_idempotency = _safe_idempotency_key(idempotency_key)
    sender_binding_valid = bool(
        isinstance(expected_sender_binding, str)
        and len(expected_sender_binding.strip()) == 64
        and all(
            character in "0123456789abcdefABCDEF"
            for character in expected_sender_binding.strip()
        )
    )

    if not telefono:
        reason = "whatsapp_recipient_required"
    elif normalized_tenant_id is None:
        reason = "whatsapp_tenant_scope_required"
    elif normalized_idempotency is None:
        reason = "whatsapp_idempotency_key_required"
    elif normalized_template_id is None:
        reason = "whatsapp_template_registry_required"
    elif not sender_binding_valid:
        reason = "whatsapp_sender_binding_required"
    else:
        # Even complete-looking caller input is not provider proof. The real
        # worker must re-load and validate registry/sender after its durable
        # I/O claim, then persist either provider SID or uncertainty.
        reason = "whatsapp_template_transport_unavailable"

    _log_legacy_channel_blocked(
        channel="whatsapp",
        reason_code=reason,
        tenant_id=normalized_tenant_id,
        template_registry_id=normalized_template_id,
        idempotency_key=normalized_idempotency,
        has_recipient=bool(telefono),
        body_length=len(str(mensaje or "")),
    )
    return _blocked_dispatch_result(
        "whatsapp",
        reason,
        tenant_id=normalized_tenant_id,
        template_registry_id=normalized_template_id,
        idempotency_key=normalized_idempotency,
    )


def enviar_notificacion_sms(
    telefono: str | None,
    body: str | None = None,
    *,
    tenant_id: int | None = None,
    expected_sender_binding: str | None = None,
    idempotency_key: str | None = None,
) -> NotificationDispatchResult:
    """Compatibility SMS shim that never treats configuration as acceptance."""

    normalized_tenant_id = _positive_int(tenant_id)
    normalized_idempotency = _safe_idempotency_key(idempotency_key)
    sender_binding_valid = bool(
        isinstance(expected_sender_binding, str)
        and len(expected_sender_binding.strip()) == 64
        and all(
            character in "0123456789abcdefABCDEF"
            for character in expected_sender_binding.strip()
        )
    )
    if not telefono:
        reason = "sms_recipient_required"
    elif normalized_tenant_id is None:
        reason = "sms_tenant_scope_required"
    elif normalized_idempotency is None:
        reason = "sms_idempotency_key_required"
    elif not sender_binding_valid:
        reason = "sms_sender_binding_required"
    else:
        reason = "sms_transport_unavailable"
    _log_legacy_channel_blocked(
        channel="sms",
        reason_code=reason,
        tenant_id=normalized_tenant_id,
        idempotency_key=normalized_idempotency,
        has_recipient=bool(telefono),
        body_length=len(str(body or "")),
    )
    return _blocked_dispatch_result(
        "sms",
        reason,
        tenant_id=normalized_tenant_id,
        idempotency_key=normalized_idempotency,
    )
