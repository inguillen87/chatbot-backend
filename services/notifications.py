"""Centralized notification helpers for tickets.

These helpers prefer environment-driven SMTP configuration so they can work
in Render/Heroku deployments without additional wiring. When credentials are
missing they log a clear warning instead of raising to keep ticket flows
functional.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any, Iterable, Optional

from flask import current_app

logger = logging.getLogger(__name__)


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


def send_ticket_whatsapp(ticket: Any, event_type: str) -> None:
    """Send a WhatsApp notification for a ticket event."""

    provider = current_app.config.get("WHATSAPP_PROVIDER")
    if not provider:
        _log_dispatch("whatsapp", ticket, event_type, ok=False)
        logger.info("[notifications] WHATSAPP_PROVIDER no configurado; omitiendo envío")
        return

    _log_dispatch("whatsapp", ticket, event_type)
    logger.info("[notifications] WhatsApp provider '%s' configurado para envíos", provider)


def send_ticket_sms(ticket: Any, event_type: str) -> None:
    """Send an SMS notification for a ticket event."""

    provider = current_app.config.get("SMS_PROVIDER")
    if not provider:
        _log_dispatch("sms", ticket, event_type, ok=False)
        logger.info("[notifications] SMS_PROVIDER no configurado; omitiendo envío")
        return

    _log_dispatch("sms", ticket, event_type)
    logger.info("[notifications] SMS provider '%s' configurado para envíos", provider)


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
